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
from common import DB_PATH as DB

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


# base model -> modem/baseband (vendor + part + gen). Blank where not confidently known.
MODEM = {
    "iPhone 2G":  "Infineon PMB8876 (S-GOLD2) — GSM/EDGE",
    "iPhone 3G":  "Infineon PMB8878 (X-GOLD 608) — 3G/HSDPA",
    "iPhone 3GS": "Infineon PMB8878 (X-GOLD 608) — 3G",
    "iPhone 4":   "Infineon X-GOLD 618 (PMB9800) — 3G  ·  CDMA: Qualcomm MDM6600",
    "iPhone 4s":  "Qualcomm MDM6610 — 3G",
    "iPhone 5":   "Qualcomm MDM9615 — LTE Cat 3 (first LTE iPhone)",
    "iPhone 5c":  "Qualcomm MDM9615 — LTE Cat 3",
    "iPhone 5s":  "Qualcomm MDM9615 — LTE Cat 4",
    "iPhone 6":   "Qualcomm MDM9625 — LTE Cat 4",
    "iPhone 6 Plus": "Qualcomm MDM9625 — LTE Cat 4",
    "iPhone 6s":  "Qualcomm MDM9635 — LTE Cat 6",
    "iPhone 6s Plus": "Qualcomm MDM9635 — LTE Cat 6",
    "iPhone SE":  "Qualcomm MDM9625 — LTE Cat 4",
    "iPhone 7":   "Qualcomm MDM9645 / Intel XMM7360 — LTE Cat 9 (dual-sourced by carrier)",
    "iPhone 7 Plus": "Qualcomm MDM9645 / Intel XMM7360 — LTE Cat 9",
    "iPhone 8":   "Qualcomm MDM9655 / Intel XMM7480 — LTE Cat 16",
    "iPhone 8 Plus": "Qualcomm MDM9655 / Intel XMM7480 — LTE Cat 16",
    "iPhone X":   "Qualcomm MDM9655 / Intel XMM7480 — LTE Cat 16",
    "iPhone XR":  "Intel XMM7560 — LTE Cat 16 (Intel-only year)",
    "iPhone XS":  "Intel XMM7560 — LTE Cat 16",
    "iPhone XS Max": "Intel XMM7560 — LTE Cat 16",
    "iPhone 11":  "Intel XMM7660 — LTE Cat 16",
    "iPhone 11 Pro": "Intel XMM7660 — LTE Cat 16",
    "iPhone 11 Pro Max": "Intel XMM7660 — LTE Cat 16",
    "iPhone SE (2020)": "Intel XMM7660 — LTE (no 5G)",
    "iPhone 12":  "Qualcomm Snapdragon X55 — 5G (first 5G iPhone)",
    "iPhone 12 mini": "Qualcomm Snapdragon X55 — 5G",
    "iPhone 12 Pro": "Qualcomm Snapdragon X55 — 5G",
    "iPhone 12 Pro Max": "Qualcomm Snapdragon X55 — 5G",
    "iPhone 13":  "Qualcomm Snapdragon X60 — 5G",
    "iPhone 13 mini": "Qualcomm Snapdragon X60 — 5G",
    "iPhone 13 Pro": "Qualcomm Snapdragon X60 — 5G",
    "iPhone 13 Pro Max": "Qualcomm Snapdragon X60 — 5G",
    "iPhone SE (3rd generation)": "Qualcomm Snapdragon X57 — 5G",
    "iPhone 14":  "Qualcomm Snapdragon X65 — 5G",
    "iPhone 14 Plus": "Qualcomm Snapdragon X65 — 5G",
    "iPhone 14 Pro": "Qualcomm Snapdragon X65 — 5G",
    "iPhone 14 Pro Max": "Qualcomm Snapdragon X65 — 5G",
    "iPhone 15":  "Qualcomm Snapdragon X70 — 5G",
    "iPhone 15 Plus": "Qualcomm Snapdragon X70 — 5G",
    "iPhone 15 Pro": "Qualcomm Snapdragon X70 — 5G",
    "iPhone 15 Pro Max": "Qualcomm Snapdragon X70 — 5G",
    "iPhone 16":  "Qualcomm Snapdragon X71 — 5G",
    "iPhone 16 Plus": "Qualcomm Snapdragon X71 — 5G",
    "iPhone 16 Pro": "Qualcomm Snapdragon X71 — 5G",
    "iPhone 16 Pro Max": "Qualcomm Snapdragon X71 — 5G",
    "iPhone 16e": "Apple C1 — 5G (Apple's first in-house modem)",
    "iPhone 17":  "Qualcomm (Snapdragon X-series) — 5G",
    "iPhone 17 Pro": "Qualcomm (Snapdragon X-series) — 5G",
    "iPhone 17 Pro Max": "Qualcomm (Snapdragon X-series) — 5G",
    "iPhone Air": "Apple C1X — 5G",
    "iPhone 17e": "",
}

# chipset -> (CPU config, GPU, process node, transistors) — public Apple facts
CHIP = {
    "Samsung S5L8900":  ("1-core 412 MHz ARM11 (ARM1176JZ)", "PowerVR MBX Lite", "90 nm", ""),
    "Samsung S5PC100":  ("1-core 600 MHz ARM Cortex-A8", "PowerVR SGX535", "65 nm", ""),
    "Apple A4":         ("1-core 1.0 GHz Cortex-A8", "PowerVR SGX535", "45 nm", ""),
    "Apple A5":         ("2-core 0.8 GHz Cortex-A9", "PowerVR SGX543MP2", "45 nm", ""),
    "Apple A6":         ("2-core 1.3 GHz Swift", "PowerVR SGX543MP3", "32 nm", ""),
    "Apple A7":         ("2-core 1.3 GHz Cyclone (64-bit)", "PowerVR G6430", "28 nm", "1.0B"),
    "Apple A8":         ("2-core 1.4 GHz Typhoon", "PowerVR GX6450", "20 nm", "2.0B"),
    "Apple A9":         ("2-core 1.85 GHz Twister", "PowerVR GT7600", "14/16 nm", "2.0B"),
    "Apple A10 Fusion": ("4-core 2.34 GHz (2 Hurricane + 2 Zephyr)", "PowerVR GT7600 Plus (6-core)", "16 nm", "3.3B"),
    "Apple A11 Bionic": ("6-core 2.39 GHz (2 Monsoon + 4 Mistral)", "Apple GPU (3-core)", "10 nm", "4.3B"),
    "Apple A12 Bionic": ("6-core 2.49 GHz (2 Vortex + 4 Tempest)", "Apple GPU (4-core)", "7 nm", "6.9B"),
    "Apple A13 Bionic": ("6-core 2.65 GHz (2 Lightning + 4 Thunder)", "Apple GPU (4-core)", "7 nm (N7P)", "8.5B"),
    "Apple A14 Bionic": ("6-core 3.1 GHz (2 Firestorm + 4 Icestorm)", "Apple GPU (4-core)", "5 nm (N5)", "11.8B"),
    "Apple A15 Bionic": ("6-core 3.23 GHz (2 Avalanche + 4 Blizzard)", "Apple GPU (4/5-core)", "5 nm (N5P)", "15B"),
    "Apple A16 Bionic": ("6-core 3.46 GHz (2 Everest + 4 Sawtooth)", "Apple GPU (5-core)", "4 nm (N4P)", "16B"),
    "Apple A17 Pro":    ("6-core 3.78 GHz (2 + 4)", "Apple GPU (6-core, hardware ray tracing)", "3 nm (N3B)", "19B"),
    "Apple A18":        ("6-core 4.04 GHz (2 + 4)", "Apple GPU (5-core, ray tracing)", "3 nm (N3E)", ""),
    "Apple A18 Pro":    ("6-core 4.05 GHz (2 + 4)", "Apple GPU (6-core, ray tracing)", "3 nm (N3E)", ""),
    "Apple A19":        ("6-core (2 + 4)", "Apple GPU (5-core)", "3 nm", ""),
    "Apple A19 Pro":    ("6-core (2 + 4)", "Apple GPU (6-core)", "3 nm", ""),
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
    if "Network — Modem" not in dcols:
        con.execute('ALTER TABLE devices ADD COLUMN "Network — Modem" TEXT')
        dcols.append("Network — Modem")
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
        modem = MODEM.get(base(name))
        if modem:
            row["Network — Modem"] = modem
        chip = ""
        if spec:
            chip, disp, batt, ann = spec
            row["Platform — Chipset"] = chip
            row["Platform — OS"] = "iOS"
            cd = CHIP.get(chip)
            if cd:
                row["Platform — CPU"] = cd[0]
                row["Platform — GPU"] = cd[1]
                if cd[2]:
                    row["Platform — Chipset"] = f"{chip} ({cd[2]})"
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
