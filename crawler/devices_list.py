#!/usr/bin/env python3
"""
devices_list.py — what devices do we actually hold? The inventory question.

    python3 devices_list.py                    # summary by vendor + source
    python3 devices_list.py --all              # every device, one per line
    python3 devices_list.py --vendor Samsung   # just one vendor
    python3 devices_list.py --csv devices.csv  # export the full inventory
    python3 devices_list.py --search a07       # find one

Shows, per device: builds held, newest version + date, region count, whether we
have a spec sheet (chipset) and whether any build carries a security patch — so
"imported" is never confused with "fully populated".
"""
from __future__ import annotations
import argparse, csv, sqlite3, sys
from common import DB_PATH

Q = """SELECT vendor, device,
         COUNT(*) builds,
         COUNT(DISTINCT region) regions,
         MAX(updated_at) newest,
         MAX(chipset) chipset,
         SUM(CASE WHEN (security_level IS NOT NULL AND security_level!='')
                    OR (security_url IS NOT NULL AND security_url!='') THEN 1 ELSE 0 END) sec,
         GROUP_CONCAT(DISTINCT source) sources
       FROM roms WHERE device!='' GROUP BY vendor, device"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="list every device")
    ap.add_argument("--vendor"); ap.add_argument("--search"); ap.add_argument("--csv")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(Q)]
    if a.vendor:
        rows = [r for r in rows if (r["vendor"] or "").lower() == a.vendor.lower()]
    if a.search:
        s = a.search.lower()
        rows = [r for r in rows if s in (r["device"] or "").lower()]
    rows.sort(key=lambda r: (r["vendor"] or "", -(r["builds"] or 0)))

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                               ["vendor", "device", "builds", "regions", "newest", "chipset", "sec", "sources"])
            w.writeheader(); w.writerows(rows)
        print(f"wrote {a.csv} — {len(rows):,} devices"); return

    # summary
    print(f"\n{len(rows):,} distinct devices imported\n")
    print(f"{'VENDOR':<10} {'DEVICES':>8} {'BUILDS':>8} {'W/ CHIPSET':>11} {'W/ SECURITY':>12}")
    print("-" * 54)
    by = {}
    for r in rows:
        v = r["vendor"] or "?"
        d = by.setdefault(v, [0, 0, 0, 0])
        d[0] += 1; d[1] += r["builds"]
        d[2] += 1 if r["chipset"] else 0
        d[3] += 1 if r["sec"] else 0
    for v, d in sorted(by.items(), key=lambda x: -x[1][1]):
        print(f"{v:<10} {d[0]:>8,} {d[1]:>8,} {d[2]:>11,} {d[3]:>12,}")
    print("-" * 54)
    t = [sum(d[i] for d in by.values()) for i in range(4)]
    print(f"{'TOTAL':<10} {t[0]:>8,} {t[1]:>8,} {t[2]:>11,} {t[3]:>12,}")

    show = rows if a.all else rows[:a.limit]
    if a.all or a.search or a.vendor:
        print(f"\n{'DEVICE':<44} {'BUILDS':>7} {'RGN':>4}  {'NEWEST':<11} {'SEC':>4}  CHIPSET")
        print("-" * 118)
        for r in show:
            print(f"{(r['device'] or '')[:43]:<44} {r['builds']:>7,} {r['regions']:>4}  "
                  f"{(r['newest'] or '—')[:10]:<11} {(r['sec'] or 0):>4}  {(r['chipset'] or '')[:38]}")
        if not a.all and len(rows) > len(show):
            print(f"\n… {len(rows)-len(show):,} more — use --all or --csv devices.csv")
    else:
        print("\nuse --all to list every device, --csv to export, --search <name> to find one")
    con.close()


if __name__ == "__main__":
    main()
