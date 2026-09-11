#!/usr/bin/env python3
"""
collect_gsm_slugs.py — the device -> gsmarena slug index, returned as DATA.

WHY: chipset is the other binding constraint. 22,173 of 34,261 rows carry no chipset,
so the chipset-CVE lane can only see 84 devices. Chipset comes from gsmarena spec
pages, and reaching a spec page needs a slug — gsmarena has no stable id we can
derive, and its search endpoint (`res.php3?sSearch=`) is Turnstile-gated: it answers
200 with a 2,748-byte challenge page, which is a false green, not a result. The
brand listing pages are not gated, so the index is built from those instead.

This is split across two boxes with `--half`, not because either is blocked — both
reach gsmarena fine — but because pulling ~30 brand pages and every device link from
one address is worse manners than splitting it. 2s between requests by default.

Returns a flat CSV. The asking box owns the join and every verdict, as always.

    python3 collect_gsm_slugs.py --out <dir> --half 2 --delay 2.0
"""
from __future__ import annotations
import argparse, csv, json, re, socket, ssl, sys, time, urllib.error, urllib.request
from pathlib import Path

BASE = "https://www.gsmarena.com/"
MAKERS = BASE + "makers.php3"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")
# The control: a brand page we know carries device links. If THIS is empty the run
# measured the network, not gsmarena's catalogue.
CONTROL_BRAND = "samsung-phones-9.php"


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError):
        return None, ""


DEV_RE = re.compile(r'<a href="([a-z0-9_\-]+-\d+\.php)"[^>]*>.*?<strong><span>(.*?)</span>',
                    re.S | re.I)
# The brand name is the anchor text and nothing follows it. The first version
# expected a trailing <br>, which does not exist in the real markup, so makers.php3
# parsed as zero brands and the run reported 'empty' from a page that had 100+ of
# them. Written blind, shipped to another box, and only then discovered — which is
# what tests/test_gsm_parse.py now prevents.
BRAND_RE = re.compile(r'<a href="([a-z0-9_\-]+-phones-\d+\.php)"\s*>([^<]{2,40})</a>', re.I)


def devices_on(html):
    out = []
    for slug, name in DEV_RE.findall(html):
        name = re.sub(r"<[^>]+>", "", name)
        name = re.sub(r"\s+", " ", name).strip()
        if name and slug:
            out.append((name, slug))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--half", type=int, choices=[1, 2], default=1,
                    help="which half of the brand list this box takes")
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--max-pages", type=int, default=4,
                    help="pages per brand; brands paginate and the tail is old devices")
    a = ap.parse_args()
    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)

    # ---- control first -----------------------------------------------------
    st, html = get(BASE + CONTROL_BRAND)
    ctl = devices_on(html)
    control_ok = bool(ctl)
    print(f"CONTROL {CONTROL_BRAND}: HTTP {st}, {len(ctl)} device links")
    if not control_ok:
        note = (f"control brand page returned HTTP {st} with no device links — this is "
                f"a reading of the network, not of gsmarena. Collected nothing.")
        print(f"BLOCKED: {note}")
        (outdir / "result.json").write_text(json.dumps(
            {"outcome": "blocked", "note": note, "control_ok": False,
             "counts": {}, "files": []}, indent=2))
        return 1
    time.sleep(a.delay)

    st, html = get(MAKERS)
    brands = BRAND_RE.findall(html)
    if not brands:
        (outdir / "result.json").write_text(json.dumps(
            {"outcome": "empty", "note": f"makers.php3 returned HTTP {st} with no brands",
             "control_ok": True, "counts": {}, "files": []}, indent=2))
        return 1
    brands = sorted(set(brands))
    mine = brands[a.half - 1::2]          # interleave, so neither box gets only big brands
    print(f"{len(brands)} brands total; this box takes {len(mine)} (half {a.half})")
    time.sleep(a.delay)

    rows, seen, http = [], set(), {}
    for i, (slug, brand) in enumerate(mine, 1):
        page, url = 1, BASE + slug
        while page <= a.max_pages:
            st, html = get(url)
            http[str(st)] = http.get(str(st), 0) + 1
            time.sleep(a.delay)
            if st == 429:
                print(f"  429 on {url} — stopping and reporting rather than pushing on")
                (outdir / "result.json").write_text(json.dumps(
                    {"outcome": "refused", "control_ok": True,
                     "note": f"429 rate-limited at {url} after {len(rows)} rows; stopped.",
                     "counts": {"rows": len(rows)}, "files": []}, indent=2))
                return 1
            if not html:
                break
            found = devices_on(html)
            for name, dslug in found:
                if dslug in seen:
                    continue
                seen.add(dslug)
                rows.append({"brand": brand.strip(), "device": name, "slug": dslug,
                             "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            nxt = re.search(rf'<a href="({re.escape(slug.split("-")[0])}[^"]*p(\d+)\.php)"', html)
            if not found or not nxt:
                break
            url, page = BASE + nxt.group(1), page + 1
        if i % 5 == 0:
            print(f"  {i}/{len(mine)} brands · {len(rows):,} devices", flush=True)

    csv_path = outdir / "gsm_slugs.csv"
    if rows:
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
    note = (f"{len(rows):,} device slugs across {len(mine)} brands (half {a.half}). "
            f"HTTP mix: {http}. Control passed, so an empty brand is a real absence.")
    print(f"\n{note}")
    (outdir / "result.json").write_text(json.dumps(
        {"outcome": "ok" if rows else "empty", "note": note, "control_ok": True,
         "counts": {"devices": len(rows), "brands": len(mine)},
         "files": ["gsm_slugs.csv"] if rows else []}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
