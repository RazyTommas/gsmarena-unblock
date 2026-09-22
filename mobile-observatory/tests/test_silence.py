from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database
from mobile_observatory.server import ObservatoryService
from mobile_observatory.silence import detect_silence

ISO = "%Y-%m-%dT%H:%M:%SZ"


def _iso(dt: datetime) -> str:
    return dt.strftime(ISO)


def _insert_source(conn, source_id: str, name: str, created_at: str) -> None:
    conn.execute("INSERT INTO sources VALUES(?,?,NULL,'secondary',1,?)", (source_id, name, created_at))


def _insert_run(conn, run_id: str, source_id: str, started_at: str, finished_at: str | None,
                 outcome: str = "succeeded") -> None:
    conn.execute(
        """INSERT INTO ingestion_runs(id,source_id,started_at,finished_at,outcome,parser_name,parser_version,
             fetched_count,accepted_count,rejected_count,error)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, source_id, started_at, finished_at, outcome, "test-parser", "1.0", 1, 1, 0, None),
    )


class SilenceDetectorUnitTests(unittest.TestCase):
    """Direct tests of detect_silence(): a fixed `now` makes these fully
    deterministic, independent of wall-clock time."""

    def setUp(self):
        self.db = Database.migrated()
        self.now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.db.close()

    def _seed_regular_source(self, source_id: str, *, gap_hours: float, run_count: int,
                              last_finished_offset_hours: float) -> None:
        """Plant `run_count` finished runs spaced `gap_hours` apart, with the
        most recent one finishing `last_finished_offset_hours` before self.now."""
        _insert_source(self.db.connection, source_id, source_id, _iso(self.now - timedelta(days=365)))
        last_finished = self.now - timedelta(hours=last_finished_offset_hours)
        for n in range(run_count):
            started = last_finished - timedelta(hours=gap_hours * (run_count - 1 - n))
            finished = started + timedelta(minutes=5)
            _insert_run(self.db.connection, f"{source_id}-run-{n}", source_id, _iso(started), _iso(finished))

    def test_fires_on_planted_silent_source(self):
        # A source with an established 6-hour cadence whose last run finished
        # 5 days ago is unambiguously overdue.
        self._seed_regular_source("planted.silent.source", gap_hours=6, run_count=6,
                                   last_finished_offset_hours=24 * 5)
        findings = {f["source_id"]: f for f in detect_silence(self.db.connection, now=self.now)}
        finding = findings["planted.silent.source"]
        self.assertEqual(finding["status"], "silent")
        self.assertAlmostEqual(finding["expected_interval_hours"], 6.0, places=2)
        self.assertGreater(finding["overdue_hours"], 100)

    def test_stays_quiet_on_healthy_source(self):
        # Same 6-hour cadence, but the last run finished 1 hour ago -- well
        # inside the expected window. Must NOT be flagged silent.
        self._seed_regular_source("planted.healthy.source", gap_hours=6, run_count=6,
                                   last_finished_offset_hours=1)
        findings = {f["source_id"]: f for f in detect_silence(self.db.connection, now=self.now)}
        finding = findings["planted.healthy.source"]
        self.assertEqual(finding["status"], "healthy")
        self.assertAlmostEqual(finding["expected_interval_hours"], 6.0, places=2)
        self.assertLess(finding["overdue_hours"], 18)  # multiplier=3 * 6h = 18h threshold

    def test_both_planted_at_once_only_the_silent_one_fires(self):
        # The single most important property: the detector must discriminate
        # between sources, not just react to "any silence anywhere."
        self._seed_regular_source("mixed.silent", gap_hours=2, run_count=8, last_finished_offset_hours=48)
        self._seed_regular_source("mixed.healthy", gap_hours=2, run_count=8, last_finished_offset_hours=0.5)
        findings = {f["source_id"]: f["status"] for f in detect_silence(self.db.connection, now=self.now)}
        self.assertEqual(findings["mixed.silent"], "silent")
        self.assertEqual(findings["mixed.healthy"], "healthy")

    def test_single_run_is_insufficient_data_not_silent(self):
        # One data point cannot establish a cadence. A brand-new source must
        # never be flagged silent just because it only just started.
        _insert_source(self.db.connection, "brand.new.source", "brand.new.source", _iso(self.now - timedelta(days=1)))
        _insert_run(self.db.connection, "r1", "brand.new.source",
                    _iso(self.now - timedelta(days=200)), _iso(self.now - timedelta(days=200)))
        finding = next(f for f in detect_silence(self.db.connection, now=self.now)
                       if f["source_id"] == "brand.new.source")
        self.assertEqual(finding["status"], "insufficient_data")
        self.assertIsNone(finding["expected_interval_hours"])

    def test_zero_runs_is_insufficient_data(self):
        _insert_source(self.db.connection, "never.run.source", "never.run.source", _iso(self.now))
        finding = next(f for f in detect_silence(self.db.connection, now=self.now)
                       if f["source_id"] == "never.run.source")
        self.assertEqual(finding["status"], "insufficient_data")
        self.assertIsNone(finding["last_activity_at"])
        self.assertEqual(finding["finished_run_count"], 0)

    def test_running_unfinished_run_counts_as_recent_activity_not_silence(self):
        # A run that started but has not finished yet is still evidence the
        # source is alive; it must not itself be scored against the cadence.
        self._seed_regular_source("in.flight.source", gap_hours=4, run_count=3, last_finished_offset_hours=40)
        _insert_run(self.db.connection, "still-running", "in.flight.source", _iso(self.now - timedelta(minutes=5)),
                    None, outcome="running")
        finding = next(f for f in detect_silence(self.db.connection, now=self.now)
                       if f["source_id"] == "in.flight.source")
        self.assertEqual(finding["status"], "healthy")
        self.assertLess(finding["overdue_hours"], 1)


class SilenceReachesTheHealthApiTests(unittest.TestCase):
    """Prove the detector's output actually reaches a consumer: the admin
    health rows and the radar overview's sourceWarnings count, both of which
    the web dashboard already renders (apps/web/app.js Operations page)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database.migrated()
        self.service = ObservatoryService(self.db, Path(self.temp.name) / "local.sqlite", demonstration=False)

    def tearDown(self):
        self.service.local.close()
        self.db.close()
        self.temp.cleanup()

    def _plant(self, source_id: str, *, gap_hours: float, run_count: int, last_finished_offset_hours: float) -> None:
        now = datetime.now(timezone.utc)
        _insert_source(self.db.connection, source_id, source_id, _iso(now - timedelta(days=365)))
        last_finished = now - timedelta(hours=last_finished_offset_hours)
        for n in range(run_count):
            started = last_finished - timedelta(hours=gap_hours * (run_count - 1 - n))
            finished = started + timedelta(minutes=5)
            _insert_run(self.db.connection, f"{source_id}-run-{n}", source_id, _iso(started), _iso(finished))

    def test_silent_source_is_labelled_and_bumps_source_warnings_advisory(self):
        self._plant("dashboard.silent", gap_hours=1, run_count=5, last_finished_offset_hours=1000)
        self._plant("dashboard.healthy", gap_hours=1, run_count=5, last_finished_offset_hours=0.1)

        rows = {r["source_id"]: r for r in self.service.health()}
        silent_row = rows["dashboard.silent"]
        healthy_row = rows["dashboard.healthy"]

        self.assertTrue(silent_row["silent"])
        self.assertEqual(silent_row["silenceStatus"], "silent")
        self.assertTrue(silent_row["silenceAdvisory"])  # explicitly labelled advisory, never a hard gate
        self.assertFalse(healthy_row["silent"])
        self.assertEqual(healthy_row["silenceStatus"], "healthy")

        overview = self.service.overview()
        self.assertGreaterEqual(overview["sourceWarnings"], 1)

    def test_healthy_only_corpus_raises_no_source_warnings_from_silence(self):
        self._plant("only.healthy.a", gap_hours=3, run_count=4, last_finished_offset_hours=0.2)
        self._plant("only.healthy.b", gap_hours=3, run_count=4, last_finished_offset_hours=0.5)
        rows = {r["source_id"]: r for r in self.service.health()}
        self.assertFalse(rows["only.healthy.a"]["silent"])
        self.assertFalse(rows["only.healthy.b"]["silent"])
        self.assertEqual(self.service.overview()["sourceWarnings"], 0)


if __name__ == "__main__":
    unittest.main()
