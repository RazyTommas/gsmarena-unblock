"""Per-source silence detection.

A collector that silently stopped looks identical to one with nothing to
report -- unless something compares "when did this source last produce a
run" against "how often does this source normally produce a run". This
module does exactly that comparison and nothing else.

It is advisory: it never blocks ingestion, never retries anything, and never
mutates the corpus. It only labels each source with a status a consumer can
act on. Callers (the admin health API, the scheduled batch script) decide
what to do with that label; see docs/SOURCE_SILENCE_DETECTION.md.

The baseline "how often does this source normally run" is learned from the
source's own history (the median gap between its own finished runs), not a
hardcoded number -- sources here have wildly different natural cadences
(a captured-replay adapter re-run manually vs. a source meant to run hourly).
A source needs at least two finished runs before a cadence claim is possible;
until then it is reported `insufficient_data`, never `silent`. Silence is a
claim about a broken expectation, and there is no expectation without a
baseline.
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timezone

STATUS_SILENT = "silent"
STATUS_HEALTHY = "healthy"
STATUS_INSUFFICIENT_DATA = "insufficient_data"

DEFAULT_MULTIPLIER = 3.0
DEFAULT_MIN_GRACE_HOURS = 1.0


def _parse(ts: str) -> datetime:
    value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def detect_silence(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    multiplier: float = DEFAULT_MULTIPLIER,
    min_grace_hours: float = DEFAULT_MIN_GRACE_HOURS,
) -> list[dict]:
    """Return one finding per row in `sources`.

    `status` is one of:
      - "silent": the source has an established cadence (>=2 finished runs)
        and is overdue for its next run by more than `multiplier` times its
        own historical median gap (floored at `min_grace_hours`).
      - "healthy": the source has an established cadence and is within it.
      - "insufficient_data": fewer than two finished runs exist, so no
        cadence can be claimed yet. This is NOT evidence of health; it is an
        honest "cannot judge yet".

    `now` is injectable for testing; defaults to the real current time.
    """
    now = now or datetime.now(timezone.utc)
    findings: list[dict] = []
    sources = connection.execute("SELECT id, name FROM sources ORDER BY name").fetchall()
    for source in sources:
        source_id, name = source["id"], source["name"]
        runs = connection.execute(
            "SELECT started_at, finished_at FROM ingestion_runs WHERE source_id=? ORDER BY started_at ASC",
            (source_id,),
        ).fetchall()
        if not runs:
            findings.append({
                "source_id": source_id, "source_name": name, "status": STATUS_INSUFFICIENT_DATA,
                "last_activity_at": None, "expected_interval_hours": None, "overdue_hours": None,
                "finished_run_count": 0, "checked_at": now.isoformat(),
            })
            continue
        finished = [r for r in runs if r["finished_at"]]
        activity_times = [
            _parse(r["finished_at"]) if r["finished_at"] else _parse(r["started_at"]) for r in runs
        ]
        last_activity = max(activity_times)
        if len(finished) < 2:
            findings.append({
                "source_id": source_id, "source_name": name, "status": STATUS_INSUFFICIENT_DATA,
                "last_activity_at": last_activity.isoformat(), "expected_interval_hours": None,
                "overdue_hours": None, "finished_run_count": len(finished), "checked_at": now.isoformat(),
            })
            continue
        starts = sorted(_parse(r["started_at"]) for r in finished)
        gaps_hours = [(b - a).total_seconds() / 3600.0 for a, b in zip(starts, starts[1:])]
        expected_interval = statistics.median(gaps_hours)
        threshold = max(expected_interval * multiplier, min_grace_hours)
        overdue_hours = (now - last_activity).total_seconds() / 3600.0
        status = STATUS_SILENT if overdue_hours > threshold else STATUS_HEALTHY
        findings.append({
            "source_id": source_id, "source_name": name, "status": status,
            "last_activity_at": last_activity.isoformat(),
            "expected_interval_hours": round(expected_interval, 3),
            "overdue_hours": round(overdue_hours, 3),
            "finished_run_count": len(finished), "checked_at": now.isoformat(),
        })
    return findings
