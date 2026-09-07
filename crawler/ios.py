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
import argparse, json, sqlite3, time, urllib.request
from pathlib import Path

DB = Path(__file__).with_name("data") / "devices.db"
API = "https://api.ipsw.me/v4"
UA = {"User-Agent": "Mozilla/5.0 (device-crawler)", "Accept": "application/json"}


def get(url, timeout=25, retries=3):
    for _ in range(retries):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=timeout).read())
        except Exception:
            time.sleep(0.6)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signed", action="store_true", help="only currently-signed builds")
    args = ap.parse_args()

    devices = get(f"{API}/devices?type=ipsw")
    if not devices:
        raise SystemExit("could not reach ipsw.me API")
    # keep the real product lines (drop AudioAccessory/AppleTV? keep all — they're firmware too)
    print(f"Apple devices: {len(devices)}", flush=True)

    con = sqlite3.connect(DB)
    con.execute("DELETE FROM roms WHERE source='ipsw.me'")     # refresh cleanly
    con.commit()

    ins = dev_done = 0
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
            con.execute(
                "INSERT INTO roms(source,device,model,codename,region,type,branch,version,"
                "android,size,updated_at,downloads,download_url,model_url,matched_devices)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ipsw.me", name, ident, ident, "Global", "ipsw",
                 "signed" if f.get("signed") else "",
                 f"iOS {ver} ({build})" if ver else build,
                 None, f"{size/1e9:.2f}GB" if size else None, rel, None,
                 f.get("url"), f"https://ipsw.me/{ident}", ""))
            ins += 1
        dev_done += 1
        if i % 25 == 0:
            con.commit()
            print(f"  {i}/{len(devices)} devices, {ins} builds", flush=True)
        time.sleep(0.05)
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM roms WHERE source='ipsw.me'").fetchone()[0]
    print(f"DONE: {dev_done} Apple devices, {tot} iOS builds ingested", flush=True)
    con.close()


if __name__ == "__main__":
    main()
