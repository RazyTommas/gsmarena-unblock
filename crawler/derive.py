#!/usr/bin/env python3
"""
derive.py — re-derive every computed column. Run after ANY ingest.

The bug this exists to prevent: several ingesters refresh by DELETE + re-INSERT
(ios.py, samsung.py). That is correct for the raw fields, but it silently wipes
every column we derived afterwards — vendor, chipset, security_url/level,
link_kind, android_num, region_kind, ingested_at. The row comes back looking
fine while the enrichment is gone, and nothing errors.

So: derived columns are never "done once". They are re-derived, idempotently,
after every ingest. refresh.sh and the Updates tab both end with this.
"""
from __future__ import annotations
import sqlite3, sys, time
from common import DB_PATH, log_run

VENDOR_RULES = [
    ("Apple",   ["iPhone%", "iPad%", "iPod%"]),
    ("Samsung", ["Samsung%", "SM-%", "GT-%"]),
    ("Xiaomi",  ["Xiaomi%", "Redmi%", "Poco%", "Pocophone%", "Mi %", "Mi", "Mix%", "Black Shark%"]),
    ("Tecno",   ["Tecno%", "TECNO%"]),
    ("Realme",  ["Realme%", "realme%", "RMX%"]),
    ("Google",  ["Pixel%", "Google%"]),
    ("OnePlus", ["OnePlus%"]),
    ("Oppo",    ["Oppo%", "OPPO%"]),
    ("Vivo",    ["Vivo%", "vivo%"]),
    ("Motorola", ["Moto%", "Motorola%", "XT%"]),
]


def log(m):
    print(f"  {m}", flush=True)


def main():
    con = sqlite3.connect(DB_PATH)
    have = {d[1] for d in con.execute("PRAGMA table_info(roms)")}
    for col, typ in (("vendor", "TEXT"), ("chipset", "TEXT"), ("security_url", "TEXT"),
                     ("security_level", "TEXT"), ("link_kind", "TEXT"),
                     ("android_num", "REAL"), ("region_kind", "TEXT"), ("ingested_at", "TEXT")):
        if col not in have:
            con.execute(f'ALTER TABLE roms ADD COLUMN "{col}" {typ}')
    con.commit()

    # 1. vendor — by source first (authoritative), then by name pattern
    con.execute("UPDATE roms SET vendor='Apple' WHERE source='ipsw.me' AND (vendor IS NULL OR vendor='')")
    con.execute("UPDATE roms SET vendor='Samsung' WHERE source IN ('samfw.com','samfw-live','fota-cloud') "
                "AND (vendor IS NULL OR vendor='')")
    con.execute("UPDATE roms SET vendor='Xiaomi' WHERE source='mifirm.net' AND (vendor IS NULL OR vendor='')")
    for vend, pats in VENDOR_RULES:
        cond = " OR ".join("device LIKE ? OR model LIKE ?" for _ in pats)
        args = []
        for p in pats:
            args += [p, p]
        con.execute(f"UPDATE roms SET vendor=? WHERE (vendor IS NULL OR vendor='') AND ({cond})",
                    [vend] + args)
    con.execute("UPDATE roms SET vendor='Other' WHERE vendor IS NULL OR vendor=''")
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM roms WHERE vendor IS NOT NULL AND vendor!=''").fetchone()[0]
    log(f"vendor set on {n:,} rows")

    # 2. security: one raw column carried two meanings — split it, every time
    con.execute("UPDATE roms SET security_url=security_patch "
                "WHERE security_patch LIKE 'http%' AND (security_url IS NULL OR security_url='')")
    con.execute("UPDATE roms SET security_level=security_patch "
                "WHERE security_patch GLOB '[0-9][0-9][0-9][0-9]-*' "
                "AND (security_level IS NULL OR security_level='')")
    con.commit()
    u = con.execute("SELECT COUNT(*) FROM roms WHERE security_url!=''").fetchone()[0]
    l = con.execute("SELECT COUNT(*) FROM roms WHERE security_level!=''").fetchone()[0]
    log(f"security: {u:,} advisory URLs · {l:,} patch levels")

    # 3. link honesty
    con.execute("UPDATE roms SET link_kind = CASE "
                "WHEN download_url IS NULL OR download_url='' THEN 'none' "
                "WHEN download_url=model_url THEN 'page' "
                "WHEN download_url LIKE '%.zip' OR download_url LIKE '%.rar' OR download_url LIKE '%.ipsw' "
                "  OR download_url LIKE '%.7z' OR download_url LIKE '%.tar%' THEN 'file' "
                "ELSE 'redirect' END")
    # 4. numeric OS (NULL when unparseable — never a silent 0)
    con.execute("UPDATE roms SET android_num = CASE WHEN android IS NULL OR android='' THEN NULL "
                "WHEN CAST(android AS REAL)>0 THEN CAST(android AS REAL) ELSE NULL END")
    # 5. region vocabulary
    con.execute("UPDATE roms SET region_kind = CASE WHEN region IS NULL OR region='' THEN 'none' "
                "WHEN region GLOB '[A-Z][A-Z][A-Z]' THEN 'csc' ELSE 'market' END")
    # 6. first-seen for rows an ingester inserted without one
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    con.execute("UPDATE roms SET ingested_at=? WHERE ingested_at IS NULL OR ingested_at=''", (now,))
    con.commit()
    log("link_kind · android_num · region_kind · ingested_at re-derived")
    con.close()

    # 7. chipset (its own module — normalised-name matching)
    try:
        import link_chipsets
        link_chipsets.main()
    except Exception as e:
        log(f"chipset link skipped: {type(e).__name__}: {e}")

    con = sqlite3.connect(DB_PATH)
    tot = con.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    con.close()
    log_run("derive", tot)


if __name__ == "__main__":
    main()
