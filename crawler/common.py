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
def log_run(source, rows=None):
    """Record that an ingester/crawler for `source` just ran. Best-effort."""
    import sqlite3
    from datetime import datetime, timezone
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("CREATE TABLE IF NOT EXISTS crawl_log(source TEXT PRIMARY KEY, ran_at TEXT, rows INTEGER)")
        con.execute("INSERT OR REPLACE INTO crawl_log(source, ran_at, rows) VALUES(?,?,?)",
                    (source, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), rows))
        con.commit(); con.close()
    except Exception:
        pass
