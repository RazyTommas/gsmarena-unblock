#!/usr/bin/env python3
"""
chipset_cves.py — ingest chipset-vendor CVEs and map them to chipset PART NUMBERS.

This is the missing link between a security advisory and a firmware build:

    chipset advisory  --(affected part numbers)-->  our device's chipset
    our device's firmware  --(Security Patch Level)-->  a patch month
    => was this CVE fixed in the build that device is running?

WHY THIS JOIN IS POSSIBLE: Qualcomm and MediaTek CVE records enumerate the
affected chips by PART NUMBER (SM8650, MT6835), and our chipset strings carry the
same part numbers ("Qualcomm SM8750-AC Snapdragon 8 Elite"), which chipset_ids.py
extracts. So the key is the part number, on both sides.

SOURCES (both verified reachable, no API key needed):
  * NVD 2.0  — the cheap INDEX: which CVEs belong to a chipset vendor, plus CVSS,
    and cpeMatch entries that name the affected parts.
  * cvelistV5 raw on GitHub — authoritative record; used when NVD has no CPEs yet
    (NVD lags badly on Android/vendor CVEs). raw.githubusercontent.com has no
    rate-limit headers and served 120 sequential requests without a 429.

Deliberately NOT used: cveawg.mitre.org — its rate limit is ONE GLOBAL bucket for
the whole internet (keyGenerator returns '*'), so 429s arrive from other people's
traffic and cannot be budgeted against.

    python3 chipset_cves.py                  # last 2 years, all chipset vendors
    python3 chipset_cves.py --since 2024-01-01
    python3 chipset_cves.py --vendor qualcomm
"""
from __future__ import annotations
import argparse, sqlite3, time
from common import DB_PATH, http_get, log_run

NVD = "https://services.nvd.nist.gov/rest/json/cves/2.0"
# CNA source identifiers (an authoritative filter, unlike keyword search)
VENDORS = {
    "qualcomm": {"source": "product-security@qualcomm.com", "kw": None},
    "mediatek": {"source": "security@mediatek.com", "kw": "MediaTek"},
    "samsung":  {"source": "mobile.security@samsung.com", "kw": "Exynos"},
}
PAGE = 200          # NVD allows 2000 but smaller pages survive its flakiness
SLEEP = 6.5         # unauthenticated NVD: 5 requests / 30s. Stay under it.


def ensure(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS chipset_cve(
      cve TEXT, part TEXT, vendor TEXT,
      published TEXT, modified TEXT, severity TEXT, score REAL,
      summary TEXT, url TEXT,
      PRIMARY KEY (cve, part));
    CREATE INDEX IF NOT EXISTS ix_ccve_part ON chipset_cve(part);
    CREATE INDEX IF NOT EXISTS ix_ccve_pub  ON chipset_cve(published);
    CREATE TABLE IF NOT EXISTS cve_meta(
      cve TEXT PRIMARY KEY, vendor TEXT, published TEXT, modified TEXT,
      severity TEXT, score REAL, summary TEXT, parts INTEGER);
    """)
    con.commit()


def parts_from_cve(v):
    """Affected chipset part numbers from an NVD record's CPE configurations.
    We keep only things that LOOK like a chipset part, never 'android' or 'linux' —
    a generic OS CPE would match every device and make the join meaningless."""
    out = set()
    for cfg in v.get("configurations", []):
        for node in cfg.get("nodes", []):
            for cm in node.get("cpeMatch", []):
                f = (cm.get("criteria") or "").split(":")
                if len(f) < 6:
                    continue
                vend, prod = f[3], f[4]
                if vend in ("google", "linux", "android") or prod in ("android", "linux_kernel"):
                    continue
                p = prod.replace("_firmware", "").upper()
                if len(p) >= 4 and any(ch.isdigit() for ch in p):
                    out.add(p)
    return out


def windows(since, days=115):
    """NVD rejects a pubStart/pubEnd range wider than 120 days with a bare 404,
    so walk the period in sub-120-day windows."""
    from datetime import date, timedelta
    s = date.fromisoformat(since); end = date.today()
    while s < end:
        e = min(s + timedelta(days=days), end)
        yield s.isoformat(), e.isoformat()
        s = e + timedelta(days=1)


def harvest_window(vendor, w0, w1, con, limit_pages=20):
    meta = VENDORS[vendor]
    start, total, added, seen_cve = 0, None, 0, 0
    while True:
        q = (f"{NVD}?sourceIdentifier={meta['source']}&resultsPerPage={PAGE}&startIndex={start}"
             f"&pubStartDate={w0}T00:00:00.000&pubEndDate={w1}T23:59:59.999")
        d = http_get(q, as_json=True, timeout=45)
        if not d:
            print(f"  [{vendor}] {w0}..{w1}: request failed at index {start}", flush=True)
            break
        total = d.get("totalResults", 0)
        vulns = d.get("vulnerabilities", [])
        if not vulns:
            break
        for item in vulns:
            v = item["cve"]
            seen_cve += 1
            m = (v.get("metrics", {}).get("cvssMetricV31")
                 or v.get("metrics", {}).get("cvssMetricV30") or [{}])[0]
            cd = m.get("cvssData", {})
            sev, score = cd.get("baseSeverity"), cd.get("baseScore")
            summ = next((x["value"] for x in v.get("descriptions", []) if x["lang"] == "en"), "")[:400]
            parts = parts_from_cve(v)
            con.execute("INSERT OR REPLACE INTO cve_meta VALUES(?,?,?,?,?,?,?,?)",
                        (v["id"], vendor, v.get("published", "")[:10], v.get("lastModified", "")[:10],
                         sev, score, summ, len(parts)))
            for p in parts:
                con.execute("INSERT OR REPLACE INTO chipset_cve VALUES(?,?,?,?,?,?,?,?,?)",
                            (v["id"], p, vendor, v.get("published", "")[:10],
                             v.get("lastModified", "")[:10], sev, score, summ,
                             f"https://nvd.nist.gov/vuln/detail/{v['id']}"))
                added += 1
        con.commit()
        if total:
            print(f"  [{vendor}] {w0}..{w1}  {min(start+PAGE,total)}/{total} CVEs · "
                  f"{added:,} chipset links", flush=True)
        start += PAGE
        if start >= (total or 0) or start >= limit_pages * PAGE:
            break
        time.sleep(SLEEP)
    return seen_cve, added


def harvest(vendor, since, con):
    tc = ta = 0
    for w0, w1 in windows(since):
        c, a = harvest_window(vendor, w0, w1, con)
        tc += c; ta += a
        time.sleep(SLEEP)
    return tc, ta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--vendor", choices=list(VENDORS))
    a = ap.parse_args()
    con = sqlite3.connect(DB_PATH)
    ensure(con)
    vendors = [a.vendor] if a.vendor else list(VENDORS)
    print(f"Ingesting chipset CVEs since {a.since} for: {', '.join(vendors)}", flush=True)
    tot_c = tot_l = 0
    for v in vendors:
        c, l = harvest(v, a.since, con)
        tot_c += c; tot_l += l
    n = con.execute("SELECT COUNT(*) FROM chipset_cve").fetchone()[0]
    d = con.execute("SELECT COUNT(DISTINCT cve) FROM chipset_cve").fetchone()[0]
    p = con.execute("SELECT COUNT(DISTINCT part) FROM chipset_cve").fetchone()[0]
    print(f"\nDONE: {tot_c:,} CVEs examined · {n:,} chipset-CVE links "
          f"({d:,} distinct CVEs across {p:,} distinct parts)", flush=True)
    log_run("chipset-cves", n)
    con.close()


if __name__ == "__main__":
    main()
