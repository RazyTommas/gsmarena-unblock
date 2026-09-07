#!/usr/bin/env python3
"""
enrich_specs.py — backfill chipset/OS for firmware devices via the Wayback bypass.

Firmware sources (mifirm/samfw/androidmtk) don't publish chipset. gsmarena does,
but the live origin is IP-banned — so we read ARCHIVED gsmarena pages through
archive.org (vendored wayback_fallback, non-evasive) and cache the result in a
`device_specs` table keyed by device name. The app LEFT-JOINs it onto the ROMs
view, so chipset appears for every firmware row whose device we could resolve.

Resumable: skips device names already in device_specs. Prioritised by firmware
build count (most-covered devices first). Commits as it goes, so partial runs
are useful. Stdlib + selectolax only.

  python enrich_specs.py                 # Xiaomi + Samsung + Tecno, all distinct devices
  python enrich_specs.py --vendors xiaomi samsung
  python enrich_specs.py --limit 300     # just the top-N by build count this run
"""
from __future__ import annotations
import argparse, json, re, sqlite3, sys, time
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).with_name("vendor")))
from wayback_fallback import wayback_fallback  # noqa: E402
from selectolax.parser import HTMLParser        # noqa: E402

DB = Path(__file__).with_name("data") / "devices.db"
UA = "Mozilla/5.0 (compatible; device-crawler/1.0; +wayback)"


def ensure_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS device_specs(
        device TEXT PRIMARY KEY, vendor TEXT, chipset TEXT, os TEXT, android TEXT,
        gsmarena_url TEXT, status TEXT, checked_at TEXT)""")
    con.commit()


def vendor_of(name):
    s = (name or "").lower()
    if s.startswith(("redmi", "poco", "pocophone", "mi ", "mix", "xiaomi", "black shark")):
        return "xiaomi"
    if s.startswith("samsung") or re.match(r"^sm-[a-z]", s):
        return "samsung"
    if s.startswith("tecno"):
        return "tecno"
    return "other"


def clean_name(name):
    """Strip firmware-source noise so a device name maps to a gsmarena slug.
    Removes parentheticals, trailing '...', codename suffixes (CM6, KA9, BE8…),
    trailing region/model junk. Keeps the human model name."""
    s = name.strip()
    s = re.sub(r"\.\.\.$", "", s)                       # truncation marker
    s = re.sub(r"\([^)]*\)", " ", s)                    # (CM5), (2024) etc.
    s = s.split("/")[0]                                  # combined names: keep first
    s = re.sub(r"\bTECNO\b", "Tecno", s)                 # de-shout casing
    # drop a trailing codename-like token: 2-4 chars mixing letters+digits (CM6, KA9, BE8, LJ8, KM5)
    s = re.sub(r"\s+[A-Za-z]{1,2}\d[A-Za-z0-9]{0,2}\s*$", "", s)
    s = re.sub(r"\s+(4G|5G)\b", "", s, flags=re.I)       # gsmarena usually omits these
    return re.sub(r"\s+", " ", s).strip()


def slugs_for(name):
    """Candidate gsmarena slug prefixes for a device display name."""
    v = vendor_of(name)
    variants = []
    for nm in (clean_name(name), name):                  # cleaned first, then raw
        b = re.sub(r"[^a-z0-9]+", "_", nm.lower()).strip("_")
        if b and b not in variants:
            variants.append(b)
    out = []
    for base in variants:
        nog = re.sub(r"_(5g|4g)$", "", base)     # gsmarena often drops the 5G/4G suffix
        for b in (base, nog):
            if v == "xiaomi":
                out += ["xiaomi_" + b, b]
            elif v == "samsung":
                out.append(b if b.startswith("samsung_") else "samsung_" + b)
            else:
                out.append(b)                    # tecno_* already includes vendor
    seen = set(); return [s for s in out if not (s in seen or seen.add(s))]


def _cdx(slug, timeout):
    q = ("http://web.archive.org/cdx/search/cdx?url=" + quote_plus("gsmarena.com/" + slug)
         + "*&output=json&collapse=urlkey&filter=statuscode:200&limit=15")
    for _ in range(3):                        # archive.org CDX throws intermittent 503s
        try:
            rows = json.loads(urlopen(Request(q, headers={"User-Agent": UA}), timeout=timeout).read().decode())
            cands = [r[2] for r in rows[1:]] if len(rows) > 1 else []
            # canonical device page only — drop -pictures-/-price-/-reviews-/-versus- variants
            cands = [u for u in cands if re.search(r"-\d+\.php$", u)
                     and not re.search(r"-(pictures|price|reviews|opinions|versus|specs)-", u)]
            cands.sort(key=len)
            return cands
        except Exception:
            time.sleep(0.8)
    return []


def resolve_url(name, timeout=15):
    for slug in slugs_for(name):
        cands = _cdx(slug, timeout)
        if cands:
            u = cands[0]
            return "https://" + u if not u.startswith("http") else u
    return None


def extract(html):
    t = HTMLParser(html)
    def spec(k):
        el = t.css_first(f'[data-spec="{k}"]')
        return el.text(strip=True) if el else None
    os = spec("os")
    andr = None
    if os:
        m = re.search(r"Android\s+(\d{1,2})", os)
        andr = m.group(1) if m else None
    return spec("chipset"), os, andr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendors", nargs="+", default=["xiaomi", "samsung", "tecno"])
    ap.add_argument("--limit", type=int, default=0, help="max devices this run (0 = all)")
    ap.add_argument("--timeout", type=int, default=15)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    ensure_table(con)
    done = set(r[0] for r in con.execute("SELECT device FROM device_specs"))
    # distinct devices by build count, filtered to requested vendors, not yet cached
    rows = con.execute("SELECT device, COUNT(*) n FROM roms WHERE device!='' GROUP BY device ORDER BY n DESC").fetchall()
    todo = [(d, n) for d, n in rows if vendor_of(d) in args.vendors and d not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"device_specs cached: {len(done)} | to do this run: {len(todo)}", flush=True)

    hit = miss = 0
    for i, (dev, n) in enumerate(todo, 1):
        v = vendor_of(dev)
        chip = os = andr = url = None; status = "no-page"
        try:
            url = resolve_url(dev, timeout=args.timeout)
            if url:
                html = wayback_fallback(url, timeout=args.timeout + 20)
                if html:
                    chip, os, andr = extract(html)
                    status = "ok" if chip else "no-chipset"
        except Exception:
            status = "error"
        con.execute("INSERT OR REPLACE INTO device_specs(device,vendor,chipset,os,android,gsmarena_url,status,checked_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (dev, v, chip, os, andr, url, status, time.strftime("%Y-%m-%d %H:%M", time.gmtime())))
        if chip: hit += 1
        else: miss += 1
        con.commit()   # persist every device — runs are slow, keep partial progress
        if i % 5 == 0:
            print(f"  {i}/{len(todo)}  hit={hit} miss={miss}  last: {dev[:30]} -> {chip or status}", flush=True)
        time.sleep(0.05)
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM device_specs WHERE chipset IS NOT NULL AND chipset!=''").fetchone()[0]
    print(f"DONE this run: hit={hit} miss={miss} | device_specs with chipset now: {tot}", flush=True)
    con.close()


if __name__ == "__main__":
    main()
