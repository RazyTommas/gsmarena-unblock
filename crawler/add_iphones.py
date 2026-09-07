#!/usr/bin/env python3
"""
add_iphones.py — give iPhones the full Android treatment (specs, chipset, dates)
in the devices table, and link their IPSW firmware.

iPhone specs are a small, well-known public set, and archive.org (our gsmarena
bypass) is too flaky to scrape reliably — so we use a curated spec map keyed by
model. Every iPhone name we hold firmware for gets a devices-table row (chipset,
display, battery, announced), its firmware linked (drawer), and chipset cached
for the ROMs view. Values left blank where not confidently known (never invented).

  python add_iphones.py
"""
from __future__ import annotations
import re, sqlite3, time
from pathlib import Path

DB = Path(__file__).with_name("data") / "devices.db"

# base model -> (chipset, display size, battery mAh, announced YYYY-MM)
SPECS = {
    "iPhone 2G":        ("Samsung S5L8900", "3.5 inch", "1400 mAh", "2007-06"),
    "iPhone 3G":        ("Samsung S5L8900", "3.5 inch", "1150 mAh", "2008-07"),
    "iPhone 3GS":       ("Samsung S5PC100", "3.5 inch", "1219 mAh", "2009-06"),
    "iPhone 4":         ("Apple A4", "3.5 inch", "1420 mAh", "2010-06"),
    "iPhone 4s":        ("Apple A5", "3.5 inch", "1432 mAh", "2011-10"),
    "iPhone 5":         ("Apple A6", "4.0 inch", "1440 mAh", "2012-09"),
    "iPhone 5c":        ("Apple A6", "4.0 inch", "1510 mAh", "2013-09"),
    "iPhone 5s":        ("Apple A7", "4.0 inch", "1560 mAh", "2013-09"),
    "iPhone 6":         ("Apple A8", "4.7 inch", "1810 mAh", "2014-09"),
    "iPhone 6 Plus":    ("Apple A8", "5.5 inch", "2915 mAh", "2014-09"),
    "iPhone 6s":        ("Apple A9", "4.7 inch", "1715 mAh", "2015-09"),
    "iPhone 6s Plus":   ("Apple A9", "5.5 inch", "2750 mAh", "2015-09"),
    "iPhone SE":        ("Apple A9", "4.0 inch", "1624 mAh", "2016-03"),
    "iPhone 7":         ("Apple A10 Fusion", "4.7 inch", "1960 mAh", "2016-09"),
    "iPhone 7 Plus":    ("Apple A10 Fusion", "5.5 inch", "2900 mAh", "2016-09"),
    "iPhone 8":         ("Apple A11 Bionic", "4.7 inch", "1821 mAh", "2017-09"),
    "iPhone 8 Plus":    ("Apple A11 Bionic", "5.5 inch", "2691 mAh", "2017-09"),
    "iPhone X":         ("Apple A11 Bionic", "5.8 inch", "2716 mAh", "2017-11"),
    "iPhone XR":        ("Apple A12 Bionic", "6.1 inch", "2942 mAh", "2018-09"),
    "iPhone XS":        ("Apple A12 Bionic", "5.8 inch", "2658 mAh", "2018-09"),
    "iPhone XS Max":    ("Apple A12 Bionic", "6.5 inch", "3174 mAh", "2018-09"),
    "iPhone 11":        ("Apple A13 Bionic", "6.1 inch", "3110 mAh", "2019-09"),
    "iPhone 11 Pro":    ("Apple A13 Bionic", "5.8 inch", "3046 mAh", "2019-09"),
    "iPhone 11 Pro Max":("Apple A13 Bionic", "6.5 inch", "3969 mAh", "2019-09"),
    "iPhone SE (2020)": ("Apple A13 Bionic", "4.7 inch", "1821 mAh", "2020-04"),
    "iPhone 12 mini":   ("Apple A14 Bionic", "5.4 inch", "2227 mAh", "2020-11"),
    "iPhone 12":        ("Apple A14 Bionic", "6.1 inch", "2815 mAh", "2020-10"),
    "iPhone 12 Pro":    ("Apple A14 Bionic", "6.1 inch", "2815 mAh", "2020-10"),
    "iPhone 12 Pro Max":("Apple A14 Bionic", "6.7 inch", "3687 mAh", "2020-11"),
    "iPhone 13 mini":   ("Apple A15 Bionic", "5.4 inch", "2438 mAh", "2021-09"),
    "iPhone 13":        ("Apple A15 Bionic", "6.1 inch", "3240 mAh", "2021-09"),
    "iPhone 13 Pro":    ("Apple A15 Bionic", "6.1 inch", "3095 mAh", "2021-09"),
    "iPhone 13 Pro Max":("Apple A15 Bionic", "6.7 inch", "4352 mAh", "2021-09"),
    "iPhone SE (3rd generation)": ("Apple A15 Bionic", "4.7 inch", "2018 mAh", "2022-03"),
    "iPhone 14":        ("Apple A15 Bionic", "6.1 inch", "3279 mAh", "2022-09"),
    "iPhone 14 Plus":   ("Apple A15 Bionic", "6.7 inch", "4325 mAh", "2022-09"),
    "iPhone 14 Pro":    ("Apple A16 Bionic", "6.1 inch", "3200 mAh", "2022-09"),
    "iPhone 14 Pro Max":("Apple A16 Bionic", "6.7 inch", "4323 mAh", "2022-09"),
    "iPhone 15":        ("Apple A16 Bionic", "6.1 inch", "3349 mAh", "2023-09"),
    "iPhone 15 Plus":   ("Apple A16 Bionic", "6.7 inch", "4383 mAh", "2023-09"),
    "iPhone 15 Pro":    ("Apple A17 Pro", "6.1 inch", "3274 mAh", "2023-09"),
    "iPhone 15 Pro Max":("Apple A17 Pro", "6.7 inch", "4441 mAh", "2023-09"),
    "iPhone 16":        ("Apple A18", "6.1 inch", "3561 mAh", "2024-09"),
    "iPhone 16 Plus":   ("Apple A18", "6.7 inch", "4674 mAh", "2024-09"),
    "iPhone 16 Pro":    ("Apple A18 Pro", "6.3 inch", "3582 mAh", "2024-09"),
    "iPhone 16 Pro Max":("Apple A18 Pro", "6.9 inch", "4685 mAh", "2024-09"),
    "iPhone 16e":       ("Apple A18", "6.1 inch", "4005 mAh", "2025-02"),
    "iPhone 17":        ("Apple A19", "6.3 inch", "", "2025-09"),
    "iPhone 17 Pro":    ("Apple A19 Pro", "6.3 inch", "", "2025-09"),
    "iPhone 17 Pro Max":("Apple A19 Pro", "6.9 inch", "", "2025-09"),
    "iPhone Air":       ("Apple A19 Pro", "6.5 inch", "", "2025-09"),
    "iPhone 17e":       ("Apple A19", "", "", ""),
}


def base(name):
    s = name.strip()
    s = re.sub(r"\s*\((GSM|Global|CDMA|China|GSM / 2012)\)\s*", "", s)
    s = s.replace("+", " Plus").replace("  ", " ").strip()
    s = re.sub(r"\bmini\b", "mini", s)
    return s


def main():
    con = sqlite3.connect(DB)
    dcols = [d[1] for d in con.execute("PRAGMA table_info(devices)")]
    con.execute("""CREATE TABLE IF NOT EXISTS device_specs(device TEXT PRIMARY KEY,vendor TEXT,
        chipset TEXT,os TEXT,android TEXT,gsmarena_url TEXT,status TEXT,checked_at TEXT)""")
    # clear any prior iPhone device rows for a clean refresh
    con.execute("DELETE FROM devices WHERE device_id LIKE 'ip%'")
    names = [n for (n,) in con.execute("SELECT DISTINCT device FROM roms WHERE source='ipsw.me' ORDER BY device")]
    print(f"iPhone names: {len(names)}", flush=True)

    added = miss = 0
    for i, name in enumerate(names, 1):
        spec = SPECS.get(base(name))
        did = "ip%d" % i
        ids = ",".join(sorted({m for (m,) in con.execute(
            "SELECT DISTINCT model FROM roms WHERE source='ipsw.me' AND device=?", (name,))}))
        romn = con.execute("SELECT COUNT(*) FROM roms WHERE source='ipsw.me' AND device=?", (name,)).fetchone()[0]
        row = {"device_id": did, "name": name, "codenames": ids, "rom_count": romn,
               "url": f"https://ipsw.me/{ids.split(',')[0]}" if ids else None}
        chip = ""
        if spec:
            chip, disp, batt, ann = spec
            row["Platform — Chipset"] = chip
            row["Platform — OS"] = "iOS"
            if disp: row["Display — Size"] = disp
            if batt: row["Battery — Type"] = batt
            if ann: row["Launch — Announced"] = ann
            row["Misc — Models"] = ids
            added += 1
        else:
            miss += 1
        cols = [c for c in row if c in dcols]
        con.execute(f'INSERT OR REPLACE INTO devices ({",".join(chr(34)+c+chr(34) for c in cols)}) '
                    f'VALUES ({",".join("?" for _ in cols)})', [row[c] for c in cols])
        con.execute("UPDATE roms SET matched_devices=? WHERE source='ipsw.me' AND device=?", (did, name))
        if chip:
            con.execute("INSERT OR REPLACE INTO device_specs(device,vendor,chipset,os,status,checked_at)"
                        " VALUES(?,?,?,?,?,?)", (name, "apple", chip, "iOS", "ok", time.strftime("%Y-%m-%d %H:%M")))
    con.commit()
    print(f"DONE: {added} iPhones with specs, {miss} without (name not in map)", flush=True)
    for r in con.execute("SELECT name,\"Platform — Chipset\",\"Display — Size\",\"Battery — Type\",\"Launch — Announced\" "
                         "FROM devices WHERE device_id LIKE 'ip%' AND \"Platform — Chipset\"!='' LIMIT 6"):
        print("  ", r)
    con.close()


if __name__ == "__main__":
    main()
