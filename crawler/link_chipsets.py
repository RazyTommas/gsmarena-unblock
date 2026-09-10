#!/usr/bin/env python3
"""link_chipsets.py — write chipset onto every firmware row we can resolve.

The chipset used to be looked up with an EXACT device-name join, so a firmware row
named "Tecno Spark 30 KL6" never matched the spec row "Tecno Spark 30" and the chip
silently vanished — 77% of rows had no chipset. This folds the devices table's own
chipsets into the spec cache and matches on a NORMALISED name (parens, 5G/4G and
trailing codenames stripped, then progressively shortened), writing the result to
roms.chipset so nothing depends on a fragile join at query time.

Idempotent; safe on the refresh schedule.
"""
import re, sqlite3
from common import DB_PATH, log_run

def norm(s):
    """Normalise a device name for matching.

    CAREFUL: the trailing-token strip exists for source codenames that FOLLOW a
    complete name ("Tecno Spark 30 KL6" -> "tecno spark 30"). It must never eat the
    model itself ("Samsung Galaxy A07" -> "samsung galaxy" would make every A-series
    device match the same spec row and get a confidently WRONG chipset). So it only
    fires when >=3 tokens survive.
    """
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = s.split("/")[0]
    s = re.sub(r"\b(5g|4g|lte|dual|sim)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    parts = s.split()
    # strip a trailing codename ONLY if the name still has >=3 real tokens after it
    if len(parts) >= 4 and re.fullmatch(r"[a-z]{1,2}\d[a-z0-9]{0,2}", parts[-1]):
        parts = parts[:-1]
    return " ".join(parts)

def main():
    con = sqlite3.connect(DB_PATH)
    if "chipset" not in {d[1] for d in con.execute("PRAGMA table_info(roms)")}:
        con.execute("ALTER TABLE roms ADD COLUMN chipset TEXT")
    for name, chip in con.execute('SELECT name,"Platform — Chipset" FROM devices '
                                  'WHERE "Platform — Chipset" IS NOT NULL AND "Platform — Chipset"!=""'):
        cur = con.execute("SELECT chipset FROM device_specs WHERE device=?", (name,)).fetchone()
        if not cur or not cur[0]:
            con.execute("INSERT OR REPLACE INTO device_specs(device,vendor,chipset,status,checked_at)"
                        " VALUES(?,?,?,'from-devices',datetime('now'))", (name, "?", chip))
    con.commit()
    lut = {}
    for dev, chip in con.execute("SELECT device,chipset FROM device_specs "
                                 "WHERE chipset IS NOT NULL AND chipset!=''"):
        k = norm(dev)
        if k:
            lut.setdefault(k, chip)
    filled = 0
    for rid, dev in con.execute("SELECT rowid,device FROM roms "
                                "WHERE chipset IS NULL OR chipset=''").fetchall():
        k = norm(dev); c = lut.get(k)
        if not c:
            # shorten only while >=3 tokens remain: "samsung galaxy" is not an identity
            parts = k.split()
            while len(parts) > 3 and not c:
                parts = parts[:-1]; c = lut.get(" ".join(parts))
        if c:
            con.execute("UPDATE roms SET chipset=? WHERE rowid=?", (c, rid)); filled += 1
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    have = con.execute("SELECT COUNT(*) FROM roms WHERE chipset IS NOT NULL AND chipset!=''").fetchone()[0]
    print(f"chipset linked on {have}/{tot} rows ({100*have/tot:.0f}%); +{filled} this run", flush=True)
    log_run("chipset-link", have)
    con.close()

if __name__ == "__main__":
    main()
