#!/usr/bin/env python3
"""
ios.py — ingest Apple iOS/iPadOS firmware into the corpus from the ipsw.me API.

Apple firmware (IPSW) is published with a clean public JSON API — version, build,
release date, size, and a direct Apple-CDN download URL (no login). This pulls
every device's full firmware history into the `roms` table (source='ipsw.me'),
so iPhones/iPads show up in Firmware Atlas alongside Android, with real dates and
downloadable links.

  python ios.py            # ingest/refresh all Apple devices
  python ios.py --signed   # only currently-signed (installable) builds

Idempotent: clears prior ipsw.me rows and re-inserts, so re-running refreshes.
Stdlib only.
"""
from __future__ import annotations
import argparse, sqlite3, time, urllib.request
from common import replace_rows, DB_PATH as DB, http_get, log_run

API = "https://api.ipsw.me/v4"


def get(url):
    return http_get(url, as_json=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="replace even if far fewer builds came back than are stored")
    ap.add_argument("--signed", action="store_true", help="only currently-signed builds")
    args = ap.parse_args()

    devices = get(f"{API}/devices?type=ipsw")
    if not devices:
        raise SystemExit("could not reach ipsw.me API")
    # iPhones only, all versions each (identifiers like iPhone16,2)
    devices = [d for d in devices if (d.get("identifier") or "").startswith("iPhone")]
    print(f"iPhone devices: {len(devices)}", flush=True)

    con = sqlite3.connect(DB)
    # Collect FIRST, replace atomically after. The previous version committed
    # `DELETE FROM roms WHERE source='ipsw.me'` before fetching a single device
    # detail, so any failure mid-run left the Apple slice destroyed with nothing to
    # show for it — and the scheduler runs this every few hours. Same defect that was
    # fixed in samsung.py; this is the second of the two refresh-by-replace ingesters.
    # It also silently wipes the derived `vendor` column, which is why derive.py must
    # follow every ingest.
    buf, dev_done = [], 0
    for i, d in enumerate(devices, 1):
        ident = d.get("identifier")
        if not ident:
            continue
        info = get(f"{API}/device/{urllib.request.quote(ident)}?type=ipsw")
        if not info:
            continue
        name = info.get("name") or ident
        for f in info.get("firmwares", []):
            if args.signed and not f.get("signed"):
                continue
            ver, build = f.get("version"), f.get("buildid")
            rel = (f.get("releasedate") or "")[:10]
            size = f.get("filesize") or 0
            buf.append((name, ident, ident, "Global", "ipsw",
                        "signed" if f.get("signed") else "",
                        f"iOS {ver} ({build})" if ver else build,
                        None, f"{size/1e9:.2f}GB" if size else None, rel, None,
                        f.get("url"), f"https://ipsw.me/{ident}", ""))
        dev_done += 1
        if i % 25 == 0:
            print(f"  {i}/{len(devices)} devices, {len(buf)} builds", flush=True)
        time.sleep(0.05)

    ins_sql = ("INSERT INTO roms(source,device,model,codename,region,type,branch,version,"
               "android,size,updated_at,downloads,download_url,model_url,matched_devices)"
               " VALUES('ipsw.me',?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    n, outcome = replace_rows(con, "roms", "source='ipsw.me'", (), buf, ins_sql,
                              source="ipsw.me", force=getattr(args, "force", False))
    tot = con.execute("SELECT COUNT(*) FROM roms WHERE source='ipsw.me'").fetchone()[0]
    if outcome == "ok":
        print(f"DONE: {dev_done} Apple devices, {tot} iOS builds ingested", flush=True)
        log_run("ipsw.me", tot, outcome="ok")
    elif outcome in ("blocked", "refused"):
        note = (f"fetched {len(buf)} builds against {tot} stored — below the floor, so "
                f"the existing rows were KEPT. ipsw.me may be partially unreachable; "
                f"re-run with --force if the shrink is real.")
        print(f"REFUSED TO REPLACE: {note}", flush=True)
        log_run("ipsw.me", tot, outcome=outcome, note=note)
    else:
        print(f"DONE: {tot} iOS builds (corpus was empty before this run)", flush=True)
        log_run("ipsw.me", tot, outcome=outcome)
    con.close()


if __name__ == "__main__":
    main()
