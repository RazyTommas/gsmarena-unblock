#!/usr/bin/env python3
"""
osv_spl.py — the authoritative CVE -> Android Security Patch Level table.

WHY THIS REPLACED A DATE COMPARISON
The first cut of this pipeline inferred "was it fixed?" by comparing a build's SPL
month against the CVE's NVD *publication* month. That is unsound, and measurably so:
Android lags the chipset vendor by 0-3 months, and same-month agreement is only 52%
for MediaTek (n=93) and 38% for Qualcomm (n=68). Month alignment is not a join.

The only thing that states which patch level fixes a CVE is the Android Security
Bulletin, and OSV publishes it as structured JSON:

    https://osv-vulnerabilities.storage.googleapis.com/Android/all.zip
    -> ~9.4 MB, ~3,666 records, every one carrying a CVE alias, SPL months 2020-07 on

    "affected":[{"ecosystem_specific":{"spl":"2026-09-05","severity":"High"},
                 "ranges":[{"events":[{"fixed":"SoCVersion:2026-09-05"}]}]}]

Validated against an independent scrape of the bulletins themselves: restricted to
records with a MediaTek vendor ref, OSV 92 vs scraped 93, overlap 90, and the SPL
date agrees on 90/90.

THE TIER IS NOT COSMETIC. Every chipset CVE in Android history sits at the `-05`
patch level; `-01` carries Framework/System/Runtime/Play only, never a chipset CVE.
So a device reporting an SPL that ends `-01` cannot adjudicate ANY chipset CVE, no
matter how recent the month is. That is stored as spl_tier and enforced in vuln.py.

    python3 osv_spl.py            # download + load
    python3 osv_spl.py --stats
"""
from __future__ import annotations
import argparse, io, json, re, sqlite3, urllib.request, zipfile
from common import DB_PATH, USER_AGENT, log_run, connect as db_connect

OSV_ZIP = "https://osv-vulnerabilities.storage.googleapis.com/Android/all.zip"


def ensure(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS cve_spl(
      cve TEXT NOT NULL, spl TEXT NOT NULL, spl_tier INTEGER,
      osv_id TEXT, severity TEXT, vendor_ref TEXT, asb_url TEXT,
      PRIMARY KEY (cve, spl));
    CREATE INDEX IF NOT EXISTS ix_cvespl_cve ON cve_spl(cve);
    CREATE INDEX IF NOT EXISTS ix_cvespl_spl ON cve_spl(spl);
    """)
    con.commit()


def fetch():
    req = urllib.request.Request(OSV_ZIP, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def load(blob, con, verbose=True):
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = [n for n in zf.namelist() if n.endswith(".json")]
    rows, n_rec, no_cve, no_spl = [], 0, 0, 0
    for n in names:
        try:
            d = json.loads(zf.read(n))
        except Exception:
            continue
        n_rec += 1
        aliases = d.get("aliases") or []
        cves = [a for a in aliases if a.startswith("CVE-")]
        # The vendor-internal id (M-ALPS…, M-MOLY…, U-…) is what the Android bulletin
        # prints next to the CVE; keep it, it is how a scrape reconciles with OSV.
        vref = next((a for a in aliases if re.match(r"^(M-|U-|QC-|PP-|B-|N-)", a)), None)
        asb = next((r.get("url") for r in d.get("references") or []
                    if "source.android.com" in (r.get("url") or "")), None)
        if not cves:
            no_cve += 1
            continue
        spls = set()
        for aff in d.get("affected") or []:
            es = aff.get("ecosystem_specific") or {}
            if es.get("spl"):
                spls.add((es["spl"], es.get("severity")))
            for rng in aff.get("ranges") or []:
                for ev in rng.get("events") or []:
                    f = ev.get("fixed") or ""
                    m = re.search(r"(\d{4}-\d{2}-\d{2})", f)
                    if m:
                        spls.add((m.group(1), es.get("severity")))
        if not spls:
            no_spl += 1
            continue
        for cve in cves:
            for spl, sev in spls:
                tier = 5 if spl.endswith("-05") else (1 if spl.endswith("-01") else None)
                rows.append((cve, spl, tier, d.get("id"), sev, vref, asb))
    con.executemany("INSERT OR REPLACE INTO cve_spl VALUES(?,?,?,?,?,?,?)", rows)
    con.commit()
    if verbose:
        print(f"OSV Android: {n_rec:,} records -> {len(rows):,} (cve, spl) rows")
        print(f"  {no_cve:,} records with no CVE alias · {no_spl:,} with no patch level")
        t = dict(con.execute("SELECT spl_tier, COUNT(*) FROM cve_spl GROUP BY spl_tier"))
        print(f"  tier -01: {t.get(1,0):,}   tier -05: {t.get(5,0):,}   other: {t.get(None,0):,}")
        mn, mx = con.execute("SELECT MIN(spl), MAX(spl) FROM cve_spl").fetchone()
        print(f"  patch levels span {mn} .. {mx}")
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    con = db_connect()
    ensure(con)
    if not a.stats:
        print(f"downloading {OSV_ZIP} …", flush=True)
        blob = fetch()
        print(f"  {len(blob):,} bytes", flush=True)
        n = load(blob, con)
        log_run("osv-spl", n)
    # How much of our chipset-CVE corpus can now actually be adjudicated?
    tot = con.execute("SELECT COUNT(DISTINCT cve) FROM chipset_cve").fetchone()[0]
    adj = con.execute("SELECT COUNT(DISTINCT c.cve) FROM chipset_cve c "
                      "JOIN cve_spl s ON s.cve=c.cve").fetchone()[0]
    print(f"\nchipset CVEs with an Android patch level: {adj:,} / {tot:,} "
          f"({100*adj/max(tot,1):.0f}%)")
    print("  the remainder are NOT 'unfixed' — they are vendor-published CVEs that")
    print("  never entered an Android bulletin, so no patch level can speak to them.")
    for v, n, a_ in con.execute("""
        SELECT c.vendor, COUNT(DISTINCT c.cve),
               COUNT(DISTINCT CASE WHEN s.cve IS NOT NULL THEN c.cve END)
        FROM chipset_cve c LEFT JOIN cve_spl s ON s.cve=c.cve GROUP BY c.vendor"""):
        print(f"    {v:9} {a_:4} / {n:4} adjudicable")
    con.close()


if __name__ == "__main__":
    main()
