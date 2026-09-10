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
from common import DB_PATH as DB, http_get, log_run

APPLEDB = "https://api.appledb.dev/ios/iOS;{build}.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="only builds newer than this (default 90)")
    args = ap.parse_args()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    con = sqlite3.connect(DB)
    cols = [c[1] for c in con.execute("PRAGMA table_info(roms)")]
    if "security_patch" not in cols:
        con.execute('ALTER TABLE roms ADD COLUMN security_patch TEXT')
    if "baseband" not in cols:
        con.execute('ALTER TABLE roms ADD COLUMN baseband TEXT')  # modem fw version per device per build
    con.commit()

    # distinct recent iOS builds (extract the build id from "iOS 26.6.1 (23G83)")
    builds = {}
    for ver, upd in con.execute(
            "SELECT DISTINCT version, updated_at FROM roms WHERE source='ipsw.me' "
            "AND updated_at>=? AND version LIKE '%(%)'", (cutoff,)):
        m = re.search(r"\(([A-Za-z0-9]+)\)", ver or "")
        if m:
            builds[m.group(1)] = ver
    print(f"recent iOS builds since {cutoff}: {len(builds)}", flush=True)

    filled = bb = 0
    for build in sorted(builds):
        d = http_get(APPLEDB.format(build=build), as_json=True) or {}
        url = d.get("securityNotes")
        if url:
            con.execute("UPDATE roms SET security_patch=? WHERE source='ipsw.me' AND version LIKE ?",
                        (url, f"%({build})"))
            filled += 1
        # baseband (modem) firmware version, keyed by device identifier for this build
        for ident, ver in (d.get("basebandVersions") or {}).items():
            if ver:
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
