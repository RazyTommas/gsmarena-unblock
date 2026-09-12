#!/usr/bin/env python3
"""
ios_security.py — attach Apple security-patch info to RECENT iPhone firmware.

Kept deliberately lean: only builds released in the last N days (default 90) get a
security link, so the DB doesn't bloat with a link for every historical build.
Source: AppleDB (api.appledb.dev), field `securityNotes` = Apple's official
security-content page (the CVE list) for that iOS version.

  python ios_security.py            # last 90 days
  python ios_security.py --days 120

Adds a `security_patch` column to `roms` (the Apple CVE URL) for those builds.
Idempotent; safe to run on the refresh schedule. Stdlib + common.py.
"""
from __future__ import annotations
import argparse, re, sqlite3, time
from datetime import datetime, timedelta, timezone
from common import DB_PATH as DB, http_get, log_run, connect as db_connect

APPLEDB = "https://api.appledb.dev/ios/iOS;{build}.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="only builds newer than this (default 90)")
    ap.add_argument("--all", action="store_true",
                    help="every build in the corpus, not just recent ones. The --days "
                         "window is right for the security-notes sweep (old advisories "
                         "do not change) but wrong for baseband: a build's modem version "
                         "is a permanent fact about that build, so restricting the "
                         "backfill by date left 128 of ~4,400 rows filled and the column "
                         "looking broken rather than unpopulated.")
    args = ap.parse_args()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    con = db_connect()
    cols = [c[1] for c in con.execute("PRAGMA table_info(roms)")]
    if "security_patch" not in cols:
        con.execute('ALTER TABLE roms ADD COLUMN security_patch TEXT')
    if "baseband" not in cols:
        con.execute('ALTER TABLE roms ADD COLUMN baseband TEXT')  # modem fw version per device per build
    con.commit()

    # distinct recent iOS builds (extract the build id from "iOS 26.6.1 (23G83)")
    builds = {}
    q = ("SELECT DISTINCT version, updated_at FROM roms WHERE source='ipsw.me' "
         "AND version LIKE '%(%)'")
    params = ()
    if not args.all:
        q += " AND updated_at>=?"
        params = (cutoff,)
    for ver, upd in con.execute(q, params):
        m = re.search(r"\(([A-Za-z0-9]+)\)", ver or "")
        if m:
            builds[m.group(1)] = ver
    print(f"iOS builds to check: {len(builds)}"
          + ("" if args.all else f" (since {cutoff}; use --all for the full corpus)"),
          flush=True)

    con.executescript("""
    CREATE TABLE IF NOT EXISTS apple_baseband(
      build TEXT, identifier TEXT, baseband TEXT, fetched_at TEXT,
      PRIMARY KEY (build, identifier));
    CREATE INDEX IF NOT EXISTS ix_ab_build ON apple_baseband(build);
    """)
    filled = bb = 0
    for build in sorted(builds):
        d = http_get(APPLEDB.format(build=build), as_json=True) or {}
        url = d.get("securityNotes")
        if url:
            con.execute("UPDATE roms SET security_patch=? WHERE source='ipsw.me' AND version LIKE ?",
                        (url, f"%({build})"))
            filled += 1
        # baseband (modem) firmware version, keyed by device identifier for this build.
        # Written to its OWN table as well as to roms. roms is replaced wholesale by
        # every ipsw.me re-ingest, so a value that lives only there is destroyed on the
        # next scheduled refresh -- 4,381 rows were, at 09:58, silently. An externally
        # fetched value is not derivable from the corpus, so it cannot be recomputed by
        # derive.py unless derive.py has somewhere to read it FROM. This is that place.
        for ident, ver in (d.get("basebandVersions") or {}).items():
            if ver:
                con.execute("INSERT OR REPLACE INTO apple_baseband VALUES(?,?,?,?)",
                            (build, ident, ver,
                             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                con.execute("UPDATE roms SET baseband=? WHERE source='ipsw.me' AND model=? AND version LIKE ?",
                            (ver, ident, f"%({build})"))
                bb += 1
        time.sleep(0.1)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM roms WHERE security_patch IS NOT NULL AND security_patch!=''").fetchone()[0]
    print(f"DONE: {filled}/{len(builds)} recent builds have a security page; {n} rows tagged; "
          f"{bb} device-build baseband versions set", flush=True)
    log_run("ios-security", n)
    con.close()


if __name__ == "__main__":
    main()
