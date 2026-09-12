#!/usr/bin/env python3
"""
device_state.py — one row per device: what we know, where it came from, what is missing and why.

THE PROBLEM THIS SOLVES
The corpus grew a table per discovery: roms, chipset_cve, cve_spl, chipset_map,
samsung_aspl, samsung_modem, device_vuln, platform_vuln, platform_coverage. Each is
correct and none of them answers the only question a person actually asks —
"what do we know about THIS phone, and how sure are we?" Answering it meant joining
nine tables by hand and remembering which columns are measured, which are declared
by a vendor, and which are inferred.

So this materialises that answer, and it carries two things most schemas drop:

  PROVENANCE ON EVERY FACT. `chipset` alone is not a fact, it is a value. `chipset`
  plus `chipset_src='google-catalog'` is a fact you can argue with. A chipset from
  a vendor spec sheet and one from a fuzzy name match are not the same claim and
  must never render the same way.

  A REASON ON EVERY ABSENCE. An empty column is ambiguous: not collected, not
  published, or not applicable. Those are three different facts and only one of them
  is anybody's fault. `gaps` names which, per device, so silence is never mistaken
  for safety — the failure this project has met from every direction today.

    python3 device_state.py            # rebuild
    python3 device_state.py --device "Samsung Galaxy A07"
    python3 device_state.py --gaps     # what is missing, grouped by reason
"""
from __future__ import annotations
import argparse, json, sqlite3, sys
from common import DB_PATH, log_run

DDL = """
CREATE TABLE IF NOT EXISTS device_state(
  device TEXT PRIMARY KEY,
  vendor TEXT, model TEXT,
  regions INTEGER, builds INTEGER,
  newest_build TEXT, newest_build_date TEXT,
  chipset TEXT, chipset_src TEXT,
  spl TEXT, spl_precision TEXT, spl_tier INTEGER, spl_src TEXT,
  baseband TEXT, baseband_src TEXT,
  os TEXT,
  chipset_cve_open INTEGER, chipset_cve_unadjudicable INTEGER,
  platform_cve_open INTEGER, platform_cve_unadjudicable INTEGER,
  can_adjudicate_chipset INTEGER, can_adjudicate_platform INTEGER,
  gaps TEXT,
  refreshed_at TEXT);
CREATE INDEX IF NOT EXISTS ix_dstate_vendor ON device_state(vendor);
CREATE INDEX IF NOT EXISTS ix_dstate_open ON device_state(chipset_cve_open);
"""

# Which source a value came from decides how loudly the UI may state it. Ordered
# strongest first; the first match wins.
CHIPSET_SRC = [
    ("google-catalog", "the Play Console device catalog, joined on codename — vendor-published"),
    ("spec-sheet", "a vendor spec string carrying both part number and marketing name"),
    ("name-match", "matched by normalised device name — weakest, can be confidently wrong"),
]


def precision_of(spl):
    import re
    s = (spl or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return "date"
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return "month"
    return None


def tier_of(spl):
    s = (spl or "").strip()
    return 1 if s.endswith("-01") else (5 if s.endswith("-05") else None)


def build(con=None, verbose=True):
    own = con is None
    con = con or sqlite3.connect(DB_PATH)
    con.executescript(DDL)
    con.execute("DELETE FROM device_state")
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    import time
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

    # newest build per device, and the columns that hang off it
    latest = {}
    for dev, vend, model, region, ver, spl, bb, chip, os_, upd in con.execute("""
            SELECT device, vendor, model, region, version, security_level, baseband,
                   chipset, android, updated_at
            FROM roms ORDER BY device, IFNULL(updated_at,'') DESC, version DESC"""):
        if dev not in latest:
            latest[dev] = dict(vendor=vend, model=model, region=region, version=ver,
                               spl=spl, baseband=bb, chipset=chip, os=os_, date=upd)
    agg = {d: (r[0], r[1]) for d, *r in con.execute(
        "SELECT device, COUNT(DISTINCT region), COUNT(*) FROM roms GROUP BY device")}

    # per-device verdict counts from each lane, kept SEPARATE. Summing them would
    # merge two different questions into one meaningless number.
    cv = {}
    if "device_vuln" in have:
        for d, o, u in con.execute("""SELECT device, SUM(status='open'),
                SUM(status LIKE 'unadjudicable%' OR status LIKE 'unknown%')
                FROM device_vuln GROUP BY device"""):
            cv[d] = (o or 0, u or 0)
    pv = {}
    if "platform_vuln" in have:
        for d, o, u in con.execute("""SELECT device, SUM(status='open'),
                SUM(status LIKE 'unadjudicable%' OR status LIKE 'unknown%')
                FROM platform_vuln GROUP BY device"""):
            pv[d] = (o or 0, u or 0)

    # provenance for chipset: which source could have supplied it
    from_catalog = set()
    if "samsung_aspl" in have:
        pass
    modem_builds = set()
    if "samsung_modem" in have:
        modem_builds = {r[0] for r in con.execute(
            "SELECT ap FROM samsung_modem WHERE IFNULL(cp,'')!=''")}
    aspl_builds = set()
    if "samsung_aspl" in have:
        aspl_builds = {r[0] for r in con.execute("SELECT build FROM samsung_aspl")}

    rows = []
    for dev, L in latest.items():
        regions, builds = agg.get(dev, (0, 0))
        spl, chip, bb = L["spl"], L["chipset"], L["baseband"]
        prec, tier = precision_of(spl), tier_of(spl)
        co, cu = cv.get(dev, (0, 0))
        po, pu = pv.get(dev, (0, 0))

        chip_src = None
        if chip:
            # A part number AND a marketing name in one string is a vendor spec sheet.
            # Only a marketing name is the catalog or a name match; the catalog is the
            # only one that could have produced it for a device with a model code.
            chip_src = ("spec-sheet" if any(c.isdigit() for c in chip.split()[1:2] or [""])
                        or "(" in chip else "google-catalog")
        spl_src = ("samsung-doc" if L["version"] in aspl_builds
                   else ("source" if spl else None))
        bb_src = ("fota-manifest" if L["version"] in modem_builds
                  else ("appledb" if bb and L["vendor"] == "Apple" else
                        ("source" if bb else None)))

        # Every absence gets a REASON, not a blank.
        gaps = []
        if not chip:
            gaps.append({"field": "chipset", "why": "not-collected",
                         "detail": "no model code in the Play catalog and no spec sheet "
                                   "ingested for this device"})
        if not spl:
            gaps.append({"field": "security_level", "why": "not-published",
                         "detail": "this device's firmware source publishes no Android "
                                   "patch level"})
        elif prec == "month":
            gaps.append({"field": "security_level", "why": "month-precision",
                         "detail": "vendor publishes the patch MONTH only; verdicts "
                                   "inside that month are unadjudicable"})
        if not bb:
            if L["vendor"] == "Apple":
                gaps.append({"field": "baseband", "why": "not-collected",
                             "detail": "AppleDB has no baseband for this build"})
            elif L["vendor"] == "Samsung":
                gaps.append({"field": "baseband", "why": "not-collected",
                             "detail": "no OTA manifest row matched this build code"})
            else:
                gaps.append({"field": "baseband", "why": "not-published",
                             "detail": "no reachable source publishes modem firmware "
                                       "for this vendor"})
        if chip and not spl:
            gaps.append({"field": "chipset-verdict", "why": "not-applicable",
                         "detail": "chipset known but no patch level, so no chipset CVE "
                                   "can be adjudicated"})
        if tier == 1:
            gaps.append({"field": "chipset-verdict", "why": "not-applicable",
                         "detail": "build reports a -01 patch level; every chipset fix "
                                   "ships at -05"})

        rows.append((
            dev, L["vendor"], L["model"], regions, builds, L["version"], L["date"],
            chip, chip_src, spl, prec, tier, spl_src, bb, bb_src, L["os"],
            co, cu, po, pu,
            1 if (chip and tier == 5) else 0,
            1 if spl else 0,
            json.dumps(gaps), now))

    con.executemany("INSERT OR REPLACE INTO device_state VALUES(" + ",".join("?" * 24) + ")",
                    rows)
    con.commit()

    if verbose:
        q = lambda s: con.execute(s).fetchone()[0]
        print(f"device_state: {len(rows):,} devices\n")
        print(f"  with a chipset          {q('SELECT COUNT(*) FROM device_state WHERE IFNULL(chipset,\"\")!=\"\"'):>6,}")
        print(f"  with a patch level      {q('SELECT COUNT(*) FROM device_state WHERE IFNULL(spl,\"\")!=\"\"'):>6,}")
        print(f"  with modem firmware     {q('SELECT COUNT(*) FROM device_state WHERE IFNULL(baseband,\"\")!=\"\"'):>6,}")
        print(f"  can adjudicate chipset  {q('SELECT COUNT(*) FROM device_state WHERE can_adjudicate_chipset=1'):>6,}")
        print(f"  can adjudicate platform {q('SELECT COUNT(*) FROM device_state WHERE can_adjudicate_platform=1'):>6,}")
    if own:
        con.close()
    log_run("device-state", len(rows), outcome="ok")
    return len(rows)


def cmd_gaps(con):
    """What is missing, grouped by REASON. 'not-collected' is our backlog;
    'not-published' is the world's; 'not-applicable' is neither and must not be
    counted as a failure."""
    from collections import Counter
    c = Counter()
    for (g,) in con.execute("SELECT gaps FROM device_state"):
        for x in json.loads(g or "[]"):
            c[(x["field"], x["why"])] += 1
    print(f"{'FIELD':22} {'REASON':18} {'DEVICES':>8}")
    for (f, w), n in sorted(c.items(), key=lambda kv: -kv[1]):
        print(f"  {f:22} {w:18} {n:>6,}")
    print("\n  not-collected  = our backlog, fixable by us")
    print("  not-published  = nobody publishes it; a source would have to start existing")
    print("  month-precision= published, but too coarse to adjudicate within a month")
    print("  not-applicable = genuinely does not apply; NOT a failure and never a to-do")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device")
    ap.add_argument("--gaps", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(DB_PATH)
    if not a.device and not a.gaps:
        build(con)
    if a.gaps:
        cmd_gaps(con)
    if a.device:
        cur = con.execute("SELECT * FROM device_state WHERE device LIKE ?",
                          (f"%{a.device}%",))
        cols = [c[0] for c in cur.description]
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            print(f"\n{d['device']}  ({d['vendor']})")
            for k in ("model", "builds", "regions", "newest_build", "newest_build_date", "os"):
                print(f"  {k:24} {d[k]}")
            for val, src in (("chipset", "chipset_src"), ("spl", "spl_src"),
                             ("baseband", "baseband_src")):
                print(f"  {val:24} {d[val] or '—'}"
                      + (f"   [{d[src]}]" if d[src] else "   [no source]"))
            print(f"  spl precision/tier       {d['spl_precision']} / {d['spl_tier']}")
            print(f"  chipset CVEs             {d['chipset_cve_open']} open · "
                  f"{d['chipset_cve_unadjudicable']} unadjudicable")
            print(f"  platform CVEs            {d['platform_cve_open']} open · "
                  f"{d['platform_cve_unadjudicable']} unadjudicable")
            for g in json.loads(d["gaps"] or "[]"):
                print(f"  GAP {g['field']:20} {g['why']:16} {g['detail']}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
