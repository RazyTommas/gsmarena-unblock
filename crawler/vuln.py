#!/usr/bin/env python3
"""
vuln.py — the join: CHIPSET SECURITY UPDATE  <->  FIRMWARE VERSION.

The question a user actually has: "the build my phone is on — does it contain the
fix for the chipset vulnerabilities that affect its SoC?"

Three links, each measured rather than assumed:

  1. firmware -> chipset    roms.chipset, then chipset_ids.parse() for identifiers.
  2. chipset  -> advisory   an advisory names silicon in one of two namespaces, and
                            which one is available differs by vendor (measured in our
                            own ingest, 1,088 CVEs):
                              Qualcomm   109 part-number CPEs AND 363 SNAPDRAGON_*
                                         marketing CPEs -> either path works
                              MediaTek   283 MT* part CPEs and ZERO Dimensity names,
                                         while our Dimensity spec strings carry NO
                                         part number -> chipset_map is the ONLY path
                              Samsung    1 Exynos CPE in total. Per-chip Exynos CVE
                                         attribution does not exist in NVD; Samsung
                                         publishes device-level monthly bulletins
                                         instead, which is what the SPL already
                                         encodes. We report that, we do not fake it.
  3. advisory -> firmware   compare the month the fix shipped against the build's
                            Android Security Patch Level.

WHERE THIS IS HONEST, AND WHY IT MATTERS
A CVE's NVD *published* date is not the bulletin month the fix shipped in; NVD lags.
So every verdict carries a `basis`:
    bulletin        the vendor bulletin month is known -> the comparison is sound
    nvd-published   proxy only -> the verdict is 'likely', never asserted
and anything missing an SPL or a date is UNKNOWN. A false "patched" is worse than an
admitted gap, so UNKNOWN is the default and the counts below always show it.

PART CLASSIFICATION IS PART OF THE JOIN, not a nicety. An advisory names PMICs, RF
front-ends, Wi-Fi and codecs alongside the application processor (measured: only 16%
of 476 parts in one Qualcomm advisory were APs). Joining without filtering to the AP
class invents coverage that does not exist.

    python3 vuln.py --build          # (re)build device_vuln
    python3 vuln.py --device "Samsung Galaxy A07"
"""
from __future__ import annotations
import argparse, re, sqlite3, sys
from common import DB_PATH, log_run
import chipset_ids
from chipset_map import classify_part, canon

# Marketing CPE products are their own namespace with their own suffix vocabulary.
# 'SNAPDRAGON_8_GEN_3' is a phone AP; '..._WEARABLE_PLATFORM' and '..._AUTO' are not,
# and counting them would attribute a watch CVE to a handset.
CPE_SUFFIX_CLASS = [
    ("wearable", re.compile(r"_WEARABLE(_PLATFORM)?$")),
    ("auto",     re.compile(r"(_AUTO|_AUTOMOTIVE)(_PLATFORM)?$")),
    ("iot",      re.compile(r"(_IOT|_IOT_MODEM|_EMBEDDED)(_PLATFORM)?$")),
    ("compute",  re.compile(r"(_COMPUTE|_PC)(_PLATFORM)?$")),
    ("modem",    re.compile(r"(_LTE|_LTE_MODEM|_MODEM|_5G_MODEM)$")),
    ("fwa",      re.compile(r"_FIXED_WIRELESS_ACCESS(_PLATFORM)?$")),
    ("ap",       re.compile(r"^(SNAPDRAGON|QUALCOMM)_.*(_MOBILE(_PLATFORM)?|_PLATFORM)?$")),
]


def classify_cpe(product: str) -> str:
    p = (product or "").upper()
    for name, pat in CPE_SUFFIX_CLASS:
        if pat.search(p):
            return name
    return classify_part(p)


def to_cpe_form(marketing: str) -> str:
    """'Snapdragon 8 Gen 3' -> 'SNAPDRAGON_8_GEN_3', the form NVD CPE products use."""
    s = canon(marketing).upper()
    s = re.sub(r"[^A-Z0-9]+", "_", s).strip("_")
    return s


def month(s: str) -> str | None:
    """YYYY-MM from an SPL or a date. Returns None rather than guessing."""
    m = re.match(r"(\d{4})-(\d{2})", (s or "").strip())
    return f"{m.group(1)}-{m.group(2)}" if m else None


def candidate_keys(con, chipset_str: str) -> tuple[set, dict]:
    """Every advisory identifier that could name this chipset, with how we got there.
    Returns (keys, provenance) — provenance is kept so a verdict can say WHY it
    matched, which is the difference between a finding and a guess."""
    keys, prov = set(), {}
    p = chipset_ids.parse(chipset_str or "")
    if not p:
        return keys, prov

    def add(k, how):
        if k:
            keys.add(k); prov.setdefault(k, how)

    # a) the part number printed in the spec string itself (strongest)
    if p.get("part"):
        base = p["part"].upper()
        add(base, "part-in-spec")
        if p.get("variant"):
            add(p["variant"].upper(), "variant-in-spec")
        # BIN VARIANTS. The same silicon is catalogued under several part names that
        # differ only by a bin/speed suffix, and they are NOT cross-linked: measured
        # in our own table, 'SM8650' carries 2 CVEs while 'SM8650Q' carries 117.
        # Keying on the printed part alone therefore under-reports by ~98%. An empty
        # result here would be measuring our alias coverage, not the device.
        for (v,) in con.execute(
                "SELECT DISTINCT part FROM chipset_cve WHERE part LIKE ?||'%' "
                "AND LENGTH(part) <= ?", (base, len(base) + 2)):
            add(v.upper(), "bin-variant")
    # b) the marketing name rendered into the CPE namespace. Same fragmentation:
    #    snapdragon_8_gen_3_mobile_platform carries 167 CVEs where sm8650 carries 2.
    if p.get("marketing"):
        cf = to_cpe_form(p["marketing"])
        for suffix in ("", "_MOBILE", "_MOBILE_PLATFORM", "_PLATFORM"):
            add(cf + suffix, "marketing-cpe")
        # c) the bridge — the ONLY route for Dimensity, which never prints a part
        for part in con.execute(
                "SELECT part FROM chipset_map WHERE LOWER(marketing)=? "
                "AND confidence!='disputed' AND part_class='ap'",
                (canon(p["marketing"]),)):
            add(part[0].upper(), "bridge:" + p["marketing"])
    return keys, prov


def build(con=None, verbose=True):
    own = con is None
    con = con or sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS device_vuln(
      device TEXT, vendor TEXT, region TEXT, version TEXT, chipset TEXT,
      spl TEXT, cve TEXT, cve_vendor TEXT, matched_part TEXT, match_via TEXT,
      severity TEXT, score REAL, fix_month TEXT, basis TEXT, status TEXT,
      summary TEXT, url TEXT,
      PRIMARY KEY (device, region, version, cve, matched_part));
    CREATE INDEX IF NOT EXISTS ix_dv_dev ON device_vuln(device);
    CREATE INDEX IF NOT EXISTS ix_dv_status ON device_vuln(status);
    CREATE INDEX IF NOT EXISTS ix_dv_sev ON device_vuln(severity);
    """)
    con.execute("DELETE FROM device_vuln")

    # CVEs indexed by the identifier they name, AP-class only.
    cve_by_key: dict[str, list] = {}
    dropped_class = 0
    for r in con.execute("SELECT cve,part,vendor,published,severity,score,summary,url "
                         "FROM chipset_cve"):
        if classify_cpe(r[1]) != "ap":
            dropped_class += 1
            continue
        cve_by_key.setdefault(r[1].upper(), []).append(r)

    # Grain: the LATEST build per (device, region). A vulnerability verdict is about
    # what the phone is running now, so rolling up to the newest build per region is
    # the only grain that answers the question. Older builds are history, not status.
    rows = list(con.execute("""
        SELECT device, vendor, region, version, chipset, security_level, updated_at
        FROM roms WHERE IFNULL(chipset,'')!=''
        ORDER BY device, region, IFNULL(updated_at,'') DESC, version DESC"""))
    latest, seen = [], set()
    for r in rows:
        k = (r[0], r[2])
        if k not in seen:
            seen.add(k); latest.append(r)

    # The ONLY statement about which patch level fixes a CVE: the Android bulletin,
    # via OSV. Chipset CVEs live exclusively at the -05 tier, so that is what we take.
    fix_spl = {}
    for cve, spl in con.execute(
            "SELECT cve, MIN(spl) FROM cve_spl WHERE spl_tier=5 GROUP BY cve"):
        fix_spl[cve] = spl

    cache: dict[str, tuple] = {}
    ins, stat = [], {}
    for dev, vend, region, version, chipset, spl, _ in latest:
        if chipset not in cache:
            cache[chipset] = candidate_keys(con, chipset)
        keys, prov = cache[chipset]
        dev_spl = (spl or "").strip()
        dev_tier = 5 if dev_spl.endswith("-05") else (1 if dev_spl.endswith("-01") else None)
        hit = set()
        for k in keys:
            for (cve, part, cvend, pub, sev, score, summ, url) in cve_by_key.get(k, []):
                if (cve, part) in hit:
                    continue
                hit.add((cve, part))
                fx = fix_spl.get(cve)
                if not fx:
                    # Vendor-published but never in an Android bulletin — and this is
                    # the MAJORITY case (75% of our chipset CVEs). Reporting these as
                    # open is as wrong as reporting them fixed; no patch level speaks
                    # to them at all, so they get their own state.
                    st, basis = "unadjudicable-not-in-bulletin", "no-bulletin"
                elif not dev_spl:
                    st, basis = "unknown-no-spl", "bulletin"
                elif dev_tier == 1:
                    # An SPL ending -01 covers AOSP platform only. Every chipset CVE in
                    # Android history ships at -05, so a -01 level cannot adjudicate
                    # this CVE however recent its month is.
                    st, basis = "unadjudicable-tier01", "bulletin"
                elif dev_spl >= fx:
                    # A vendor COMPLIANCE CLAIM (Android CDD MUST + CTS/STS), not a
                    # verified absence of the bug — and it is a claim about the system
                    # partition while chipset fixes land in vendor/boot, which carry
                    # their own independently-set levels we cannot read from metadata.
                    st, basis = "claimed-fixed", "bulletin"
                else:
                    st, basis = "open", "bulletin"
                stat[st] = stat.get(st, 0) + 1
                ins.append((dev, vend, region, version, chipset, spl, cve, cvend,
                            part, prov.get(k, "?"), sev, score, fx, basis, st, summ, url))
    con.executemany("INSERT OR REPLACE INTO device_vuln VALUES("
                    + ",".join("?" * 17) + ")", ins)
    con.commit()

    if verbose:
        print(f"device_vuln: {len(ins):,} device-CVE verdicts over {len(latest):,} "
              f"latest-per-(device,region) builds")
        print(f"  {dropped_class:,} CVE links dropped as non-application-processor "
              f"(PMIC/RF/Wi-Fi/wearable/auto) — joining these would invent coverage")
        for k in ("open", "claimed-fixed", "unadjudicable-tier01",
                  "unadjudicable-not-in-bulletin", "unknown-no-spl"):
            if stat.get(k):
                print(f"  {k:22} {stat[k]:7,}")
        nd = con.execute("SELECT COUNT(DISTINCT device) FROM device_vuln").fetchone()[0]
        allv = con.execute("SELECT COUNT(DISTINCT device) FROM roms").fetchone()[0]
        print(f"  devices with any chipset-CVE verdict: {nd:,} / {allv:,}")
        for v, n, d in con.execute("SELECT cve_vendor,COUNT(*),COUNT(DISTINCT device) "
                                   "FROM device_vuln GROUP BY cve_vendor"):
            print(f"    via {v:9} {n:7,} verdicts on {d:4} devices")
        unres = con.execute("""
            SELECT COUNT(DISTINCT chipset) FROM roms WHERE IFNULL(chipset,'')!=''
            AND chipset NOT IN (SELECT DISTINCT chipset FROM device_vuln)""").fetchone()[0]
        print(f"  chipset strings with NO advisory match at all: {unres}")
    if own:
        con.close()
    return stat


def for_device(con, device: str, region: str = ""):
    """Per-CVE verdicts for a device. Deduplicated on (cve, part): the same CVE
    repeats once per region, and a reader wants the list of vulnerabilities, not the
    cartesian product of vulnerabilities x markets. Where regions disagree (they ship
    different patch levels) the WORST verdict wins and the regions are listed, so the
    roll-up can never hide an exposed market behind a patched one."""
    w, a = ["device=?"], [device]
    if region:
        w.append("region=?"); a.append(region)
    cur = con.execute(
        f"SELECT * FROM device_vuln WHERE {' AND '.join(w)} "
        f"ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'unadjudicable-tier01' THEN 1 "
        f"WHEN 'unadjudicable-not-in-bulletin' THEN 2 ELSE 3 END, "
        f"IFNULL(score,0) DESC", a)
    cols = [c[0] for c in cur.description]
    out, seen = [], {}
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        k = (d["cve"], d["matched_part"])
        if k in seen:
            # already ordered worst-first, so the first hit is the worst verdict
            prev = seen[k]
            if d.get("region") and d["region"] not in prev["regions"]:
                prev["regions"].append(d["region"])
            continue
        d["regions"] = [d["region"]] if d.get("region") else []
        seen[k] = d
        out.append(d)
    return out


def summary(con, chip: str = "", vendor: str = "", only_open: bool = False):
    """Per-device rollup for the UI: how exposed is each build, and on what basis."""
    w, a = ["1=1"], []
    if chip:
        w.append("LOWER(chipset) LIKE ?"); a.append(f"%{chip.lower()}%")
    if vendor:
        w.append("vendor=?"); a.append(vendor)
    q = f"""SELECT device, vendor, region, version, chipset, spl,
              SUM(status='open') AS open_n,
              SUM(status='claimed-fixed') AS fixed_n,
              SUM(status LIKE 'unadjudicable%' OR status LIKE 'unknown%') AS unknown_n,
              SUM(status='open' AND severity='CRITICAL') AS crit_n,
              SUM(status='open' AND severity='HIGH') AS high_n,
              MAX(basis) AS basis, COUNT(*) AS total
            FROM device_vuln WHERE {' AND '.join(w)}
            GROUP BY device, region, version"""
    if only_open:
        q += " HAVING open_n > 0"
    q += " ORDER BY crit_n DESC, high_n DESC, open_n DESC"
    cur = con.execute(q, a)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--device")
    ap.add_argument("--chip", default="")
    a = ap.parse_args()
    con = sqlite3.connect(DB_PATH)
    if a.build or not a.device:
        build(con)
        log_run("vuln-join", con.execute("SELECT COUNT(*) FROM device_vuln").fetchone()[0])
    if a.device:
        rows = for_device(con, a.device)
        print(f"\n{a.device}: {len(rows)} chipset-CVE verdicts")
        for r in rows[:25]:
            print(f"  {r['status']:14} {r['cve']:16} {str(r['severity']):8} "
                  f"{str(r['score']):4} fix~{r['fix_month']} spl={r['spl']} "
                  f"via {r['match_via']} ({r['matched_part']})")
    con.close()


if __name__ == "__main__":
    main()
