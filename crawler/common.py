#!/usr/bin/env python3
"""
common.py — shared helpers for the device-crawler / Firmware Atlas tools.

Centralises the things that had drifted into copy-paste across the ingesters
(ios.py, add_iphones.py, enrich_specs.py, check_updates.py, …): the corpus DB
path, a retrying HTTP GET with one User-Agent, and the Samsung PDA date decode.
Import from here instead of re-implementing.
"""
from __future__ import annotations
import json as _json
import re as _re
import time as _time
import urllib.request
from pathlib import Path

# --- corpus location (cwd-independent) -------------------------------------
DB_PATH = Path(__file__).resolve().parent / "data" / "devices.db"

# --- one User-Agent for every outbound request -----------------------------
USER_AGENT = "Mozilla/5.0 (compatible; device-crawler/1.0)"


def http_get(url, timeout=25, retries=3, backoff=0.6, as_json=False):
    """GET a URL with the shared UA and simple retry. Returns text (or parsed
    JSON when as_json=True), or None on repeated failure."""
    for attempt in range(retries):
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
                timeout=timeout).read()
            return _json.loads(raw) if as_json else raw.decode("utf-8", "ignore")
        except Exception:
            if attempt < retries - 1:
                _time.sleep(backoff)
    return None


# --- Samsung PDA build date decode -----------------------------------------
_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def decode_pda(pda):
    """(year, month, minor) from the trailing 3 chars of a Samsung PDA build, or
    None. Calibrated against dated builds: year letter …X=2024,Y=2025,Z=2026;
    month letter A..L = Jan..Dec; last char is a minor build counter (used only as
    a same-month tiebreaker when ordering)."""
    m = _re.search(r"([A-Z0-9]{3})$", pda or "")
    if not m:
        return None
    y, mo, minor = m.group(1)
    if not (y.isalpha() and mo.isalpha()):
        return None
    year = 2024 + (ord(y) - ord("X"))
    month = ord(mo) - ord("A") + 1
    return (year, month, minor) if 1 <= month <= 12 else None


def pda_month(pda):
    """'Aug 2026' from a PDA build, or '' if undecodable."""
    d = decode_pda(pda)
    return f"{_MONTHS[d[1]]} {d[0]}" if d else ""


# --- crawl/pull run log (so the UI can show when data was last refreshed) ----
def log_run(source, rows=None, outcome=None, note=None):
    """Record that an ingester/crawler for `source` just ran.

    `outcome` is NOT optional in spirit: a run that fetched nothing and a run that
    fetched 4,000 rows both used to land here as a row with a count, and the UI
    rendered both green. `samsung.py` committed a DELETE before fetching, so on a
    WAF-blocked box every scheduled refresh destroyed the corpus and logged
    ('fota-cloud', <now>, 0) — an erasure that read as a successful run.

    Outcomes: ok | empty | blocked | refused | error. Anything that is not 'ok'
    must be visible to a reader of the Updates pane without them running a query.
    """
    import sqlite3
    from datetime import datetime, timezone
    if outcome is None:
        outcome = "ok" if rows else "empty"
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("CREATE TABLE IF NOT EXISTS crawl_log(source TEXT PRIMARY KEY, "
                    "ran_at TEXT, rows INTEGER)")
        have = {d[1] for d in con.execute("PRAGMA table_info(crawl_log)")}
        for col in ("outcome", "note"):
            if col not in have:
                con.execute(f"ALTER TABLE crawl_log ADD COLUMN {col} TEXT")
        con.execute("INSERT OR REPLACE INTO crawl_log(source, ran_at, rows, outcome, note) "
                    "VALUES(?,?,?,?,?)",
                    (source, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                     rows, outcome, note))
        con.commit(); con.close()
    except Exception:
        pass


def replace_rows(con, table, where_sql, params, new_rows, insert_sql, *,
                 source, floor_ratio=0.5, force=False):
    """Replace a source's rows ONLY if the new set is plausible. Returns (n, outcome).

    The defect this exists to prevent: DELETE + commit, then fetch, then insert. If the
    fetch fails the rows are already gone and nothing errors. Every refresh-by-replace
    ingester must come through here, so the refusal is one auditable place rather than a
    rule each author has to remember.

    Refuses when the new set is empty, or smaller than floor_ratio of what is already
    stored — the shape of a blocked fetch or a changed upstream layout, never of a real
    shrink. `force=True` is the escape hatch for a genuine upstream removal.
    """
    cur = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where_sql}", params).fetchone()[0]
    n = len(new_rows)
    if not force and cur:
        if n == 0:
            return 0, "blocked"
        if n < cur * floor_ratio:
            return 0, "refused"
    try:
        con.execute("BEGIN")
        con.execute(f"DELETE FROM {table} WHERE {where_sql}", params)
        con.executemany(insert_sql, new_rows)
        con.commit()
    except Exception:
        con.rollback()
        raise
    return n, ("ok" if n else "empty")
