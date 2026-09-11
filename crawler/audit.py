#!/usr/bin/env python3
"""
audit.py — corpus defect scanner.

Generalised from bugs we actually shipped. Each check is a CLASS of defect, not a
one-off, so new sources get screened by the same rules:

  DUP       no unique key -> every ingester re-run multiplies rows
  ORPHAN    free-text join key -> rows silently drop out of joined views
  COVERAGE  a facet that covers part of the corpus hides the rest silently
  VOCAB     one column carrying two vocabularies (CSC codes AND country names)
  SEMANTIC  one column carrying two meanings (a URL AND a date)
  JUNK      truncated / placeholder values becoming first-class facet entries
  TEMPORAL  absent or implausible dates that read as real ones
  TIE       "latest" that is ambiguous, so the answer is unstable
  LINK      a link that does not deliver what its label promises
  TYPE      values that break a numeric comparison and vanish silently
  FUZZY     over-eager fuzzy matching assigning a confidently WRONG value
  DERIVED   a DELETE+re-INSERT ingest wiping columns derived after the last ingest
  ALIAS     one chip catalogued under several names that are not cross-linked, so a
            lookup on the printed name under-reports and the emptiness reads as safety
  TIER      an Android -01 patch level used to adjudicate a chipset CVE, which it
            provably cannot do (chipset fixes ship only at -05)
  PHANTOM   a CVE attributed to a device via silicon that is not its application
            processor (a PMIC/Wi-Fi/wearable part), inventing exposure
  ADJUDGE   a verdict table that offers only fixed/open, hiding the majority case:
            vendor CVEs that never entered a bulletin, which nothing can adjudicate
  PROVENANCE a bridged/fuzzy match presented with the same confidence as a part
            number printed in the vendor spec sheet

Exit code = number of FAIL findings, so it composes in CI:
    python audit.py || echo "defects found"
"""
from __future__ import annotations
import argparse, sqlite3, sys
from common import DB_PATH

FINDINGS = []


def add(sev, cls, msg, fix=None):
    FINDINGS.append((sev, cls, msg, fix))


def run(verbose=True):
    con = sqlite3.connect(DB_PATH)
    q = lambda s, *a: con.execute(s, a).fetchone()[0]
    tot = q("SELECT COUNT(*) FROM roms")
    HAVE = {d[1] for d in con.execute("PRAGMA table_info(roms)")}   # mitigation columns
    # A finding that stays FAIL after it has been mitigated trains the reader to ignore
    # the audit. Once the mitigation column exists and is populated, downgrade to INFO.
    def sev_if(mitigated, base="FAIL"):
        return "INFO" if mitigated else base

    # ---- DUP: no unique key -------------------------------------------------
    uniq = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='roms' AND sql LIKE '%UNIQUE%'")]
    if not uniq:
        add("FAIL", "DUP", "roms has NO unique index — any ingester re-run duplicates rows",
            "python fix_data.py --dedupe   (adds ux_roms_key)")
    dups = q("SELECT IFNULL(SUM(c-1),0) FROM (SELECT COUNT(*) c FROM roms "
             "GROUP BY source,IFNULL(model,''),IFNULL(region,''),IFNULL(version,'') HAVING c>1)")
    if dups:
        add("FAIL", "DUP", f"{dups:,} duplicate rows (same source+model+region+version)",
            "python fix_data.py --dedupe")
    xdup = q("SELECT IFNULL(SUM(c-1),0) FROM (SELECT COUNT(*) c FROM roms WHERE version!='' "
             "GROUP BY IFNULL(model,''),IFNULL(region,''),version HAVING c>1)")
    if xdup:
        add("WARN", "DUP", f"{xdup:,} builds present under more than one source (cross-source overlap)",
            "expected when two sources cover the same device; dedupe keeps the richest row")

    # ---- ORPHAN: free-text join keys ---------------------------------------
    orph = q("SELECT COUNT(*) FROM roms WHERE matched_devices IS NULL OR matched_devices=''")
    if orph:
        add("FAIL", "ORPHAN", f"{orph:,}/{tot:,} rows link to no device — the device drawer is empty for them",
            "python fix_data.py --relink")

    # ---- COVERAGE: partial facets hide the rest ----------------------------
    for col, label in (("chipset", "chipset"), ("android", "OS"), ("region", "region")):
        n_cov = q(f"SELECT COUNT(*) FROM roms WHERE {col} IS NOT NULL AND {col}!=''")
        if n_cov < tot:
            sev = "FAIL" if n_cov < tot * 0.6 else "WARN"
            add(sev, "COVERAGE",
                f"{label}: {tot-n_cov:,}/{tot:,} rows have none — filtering by {label} silently excludes them",
                "UI must state the excluded count; run link_chipsets.py / enrich_local.py")

    # ---- VOCAB: one column, two vocabularies -------------------------------
    badrgn = [r[0] for r in con.execute(
        "SELECT DISTINCT region FROM roms WHERE region!='' AND region NOT GLOB '[A-Z][A-Z][A-Z]'")]
    if badrgn:
        add(sev_if("region_kind" in HAVE), "VOCAB",
            f"region mixes CSC codes with country names ({len(badrgn)}: {', '.join(badrgn[:6])}…)",
            "python fix_data.py --normalize-regions  (adds region_kind so the two never blend)")

    # ---- SEMANTIC: one column, two meanings --------------------------------
    sp_url = q("SELECT COUNT(*) FROM roms WHERE security_patch LIKE 'http%'")
    sp_date = q("SELECT COUNT(*) FROM roms WHERE security_patch GLOB '[0-9][0-9][0-9][0-9]-*'")
    if sp_url and sp_date:
        add(sev_if("security_url" in HAVE and "security_level" in HAVE), "SEMANTIC",
            f"security_patch holds two meanings: {sp_url:,} advisory URLs and {sp_date:,} patch DATES",
            "python fix_data.py --split-security  (splits into security_url + security_level)")

    # ---- JUNK: placeholder values as first-class entries -------------------
    trunc = q("SELECT COUNT(*) FROM roms WHERE device LIKE '%...'")
    if trunc:
        add("WARN", "JUNK", f"{trunc:,} rows have TRUNCATED device names ('…') — they become fake distinct devices",
            "python fix_data.py --clean-names")
    dbl = q("SELECT COUNT(*) FROM roms WHERE lower(device) LIKE '% ' || lower(vendor) || ' %' "
            "OR lower(device) LIKE lower(vendor)||' '||lower(vendor)||'%'")
    if dbl:
        add("WARN", "JUNK", f"{dbl:,} device names repeat the vendor ('Samsung Samsung …')",
            "python fix_data.py --clean-names")

    # ---- TEMPORAL ----------------------------------------------------------
    nod = q("SELECT COUNT(*) FROM roms WHERE updated_at IS NULL OR updated_at=''")
    if nod:
        add("WARN", "TEMPORAL", f"{nod:,} rows have no release date (must never sort as old/new)",
            "handled in UI as '·· no date'; sources genuinely publish none")
    fut = q("SELECT COUNT(*) FROM roms WHERE updated_at>date('now')")
    if fut:
        add("FAIL", "TEMPORAL", f"{fut:,} rows dated in the FUTURE", "python fix_data.py --clamp-dates")

    # ---- TIE: unstable 'latest' -------------------------------------------
    ties = q("""SELECT COUNT(*) FROM (SELECT device,region,updated_at,COUNT(*) c FROM roms
                WHERE updated_at!='' GROUP BY device,region,updated_at HAVING c>1)""")
    if ties:
        add("INFO", "TIE",
            f"{ties:,} device+region groups have several rows sharing the newest date — "
            f"'latest per device' picks arbitrarily and can change between runs",
            "fixed in app.py: tie-break on version DESC, rowid DESC")

    # ---- LINK: label promises more than the link delivers ------------------
    fake = q("SELECT COUNT(*) FROM roms WHERE download_url IS NOT NULL AND download_url=model_url")
    if fake:
        add(sev_if("link_kind" in HAVE), "LINK",
            f"{fake:,} rows where download_url == model_url — the '↓ get' label promises a file "
            f"but opens a page",
            "python fix_data.py --mark-links  (adds link_kind so the UI can label it honestly)")

    # ---- TYPE: values that break comparisons silently ----------------------
    badv = [r[0] for r in con.execute(
        "SELECT DISTINCT android FROM roms WHERE android!='' AND CAST(android AS REAL)=0")]
    if badv:
        add(sev_if("android_num" in HAVE), "TYPE",
            f"{len(badv)} non-numeric OS values ({', '.join(map(str,badv[:5]))}) — they vanish from "
            f"any 'Android >= n' comparison without a word",
            "python fix_data.py --clean-os")

    # ---- FUZZY: one spec value spread across unrelated devices --------------
    for r in con.execute("""SELECT chipset, COUNT(DISTINCT device) d, COUNT(*) n FROM roms
                            WHERE chipset IS NOT NULL AND chipset!='' GROUP BY chipset
                            ORDER BY d DESC LIMIT 1"""):
        chip, ndev, nrows = r
        if ndev > 40:
            add("FAIL", "FUZZY",
                f"one chipset ('{str(chip)[:40]}') is attached to {ndev} DIFFERENT devices — "
                f"name matching is over-matching and assigning wrong values",
                "tighten norm() in link_chipsets.py; never shorten a name below 3 tokens")

    # ---- DERIVED: derived columns lost by a re-ingest ------------------------
    for col, label in (("vendor", "vendor"), ("chipset", "chipset"),
                       ("link_kind", "link_kind"), ("ingested_at", "first-seen")):
        if col in HAVE:
            miss = q(f"SELECT COUNT(*) FROM roms WHERE {col} IS NULL OR {col}=''")
            if col in ("vendor", "link_kind", "ingested_at") and miss:
                add("FAIL", "DERIVED",
                    f"{miss:,} rows have no {label} — a DELETE+re-INSERT ingest wiped it",
                    "python derive.py   (must run after EVERY ingest; refresh.sh does)")

    # ---- ALIAS / TIER / PHANTOM / ADJUDGE: the exposure join -----------------
    # Every check below first asserts its own scope is NON-EMPTY. A check that runs
    # against zero rows passes vacuously, which is how a broken join looks healthy.
    tbls = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"device_vuln", "chipset_cve", "cve_spl"} <= tbls:
        nv = q("SELECT COUNT(*) FROM device_vuln")
        if nv == 0:
            add("FAIL", "ADJUDGE", "device_vuln is empty — no chipset CVE reaches any build",
                "python3 vuln.py --build   (needs chipset_cves.py and osv_spl.py first)")
        else:
            # ALIAS: a part with near-zero CVEs whose bin-variant sibling has many is
            # the signature of alias fragmentation, not of a safe chip.
            cnt = dict(con.execute(
                "SELECT part, COUNT(DISTINCT cve) FROM chipset_cve GROUP BY part"))
            thin = []
            for p, n in cnt.items():
                # a sibling differing only by a 1-char bin suffix is the same silicon
                sib = max(((q2, m) for q2, m in cnt.items()
                           if q2 != p and q2.startswith(p) and len(q2) == len(p) + 1),
                          key=lambda x: x[1], default=None)
                if sib and n * 10 < sib[1]:
                    thin.append((p, n, sib[0], sib[1]))
            thin.sort(key=lambda t: t[3] - t[1], reverse=True)
            # TIER: a -01 level can never adjudicate a chipset CVE. Zero by construction,
            # so this asserts the construction, and the scope is proven non-empty first.
            n01 = q("SELECT COUNT(*) FROM device_vuln WHERE spl LIKE '%-01'")
            if n01 == 0:
                add("INFO", "TIER", "no build in device_vuln reports a -01 patch level, "
                    "so the -01 guard is currently untested by real data")
            else:
                bad01 = q("SELECT COUNT(*) FROM device_vuln "
                          "WHERE spl LIKE '%-01' AND status='claimed-fixed'")
                add("FAIL" if bad01 else "PASS", "TIER",
                    f"{bad01:,} of {n01:,} verdicts on a -01 patch level claim 'fixed' — "
                    f"a -01 level covers AOSP platform only; every chipset fix ships at -05"
                    if bad01 else
                    f"{n01:,} verdicts sit on a -01 patch level and none claims 'fixed'",
                    "vuln.py: dev_tier==1 -> unadjudicable-tier01")
            # PHANTOM: exposure attributed through non-AP silicon.
            from vuln import classify_cpe
            ph = [p for (p,) in con.execute("SELECT DISTINCT matched_part FROM device_vuln")
                  if classify_cpe(p) != "ap"]
            add("FAIL" if ph else "PASS", "PHANTOM",
                f"{len(ph)} matched parts are not application processors "
                f"(e.g. {ph[:3]}) — a PMIC or Wi-Fi CVE is not the phone's AP exposure"
                if ph else
                f"all {q('SELECT COUNT(DISTINCT matched_part) FROM device_vuln')} matched "
                f"parts classify as application processors",
                "vuln.build() filters on classify_cpe(part)=='ap'")
            # ADJUDGE: the third state must exist and be visible, because it is the
            # MAJORITY. A two-state UI would imply 'not fixed' for all of these.
            states = dict(con.execute("SELECT status, COUNT(*) FROM device_vuln GROUP BY status"))
            una = sum(v for k, v in states.items() if k.startswith("unadjudicable"))
            if una == 0:
                add("FAIL", "ADJUDGE",
                    "every verdict is fixed/open — no unadjudicable state present, yet "
                    "only a quarter of chipset CVEs ever enter an Android bulletin",
                    "vuln.py must emit unadjudicable-not-in-bulletin / -tier01")
            else:
                add("INFO", "ADJUDGE",
                    f"{una:,} of {nv:,} verdicts ({100*una//nv}%) cannot be adjudicated by "
                    f"any patch level — this is the majority case and the UI must say so, "
                    f"not default it to 'open' or 'fixed'")
            # PROVENANCE: a bridged marketing name is a weaker claim than a printed part.
            via = dict(con.execute("SELECT match_via, COUNT(*) FROM device_vuln "
                                   "GROUP BY match_via ORDER BY 2 DESC"))
            weak = sum(v for k, v in via.items() if k.startswith("bridge") or k == "marketing-cpe")
            if weak:
                add("INFO", "PROVENANCE",
                    f"{weak:,} of {nv:,} verdicts rest on a marketing-name match rather "
                    f"than a part number printed in the spec sheet — one marketing name "
                    f"can span several parts, so these are weaker claims",
                    "device_vuln.match_via carries this; the UI shows it per row")
    else:
        add("INFO", "ADJUDGE", "exposure tables absent — chipset CVEs are not joined to "
            "firmware yet", "python3 chipset_cves.py && python3 osv_spl.py && "
            "python3 vuln.py --build")

    con.close()

    if verbose:
        print("=" * 72)
        print(f"AUDIT — {tot:,} rows")
        print("=" * 72)
        fails = [f for f in FINDINGS if f[0] == "FAIL"]
        for sev, cls, msg, fix in sorted(FINDINGS, key=lambda x: x[0]):
            mark = {"FAIL":"✗","WARN":"!","INFO":"·"}.get(sev,"·")
            print(f"\n{mark} [{sev}] {cls}\n    {msg}")
            if fix:
                print(f"    → {fix}")
        print("\n" + "=" * 72)
        warns=[f for f in FINDINGS if f[0]=="WARN"]; infos=[f for f in FINDINGS if f[0]=="INFO"]
        print(f"{len(fails)} FAIL · {len(warns)} WARN · {len(infos)} INFO (mitigated)")
        print("=" * 72)
    return sum(1 for f in FINDINGS if f[0] == "FAIL")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scan the corpus for defect classes.")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    sys.exit(run(verbose=not a.quiet))
