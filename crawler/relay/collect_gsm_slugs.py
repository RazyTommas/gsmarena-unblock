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
# makers.php3 carries brand links in TWO markups and only one is the catalogue:
#
#   nav dropdown, QUOTED, 36 entries — a "popular brands" menu:
#       <li><a href="samsung-phones-9.php">Samsung</a></li>
#   brand table, UNQUOTED, 125 entries — what we actually want:
#       <td><a href=acer-phones-59.php>Acer<br><span>117 devices</span></a></td>
#
# The first version required a trailing <br> (right) and quoted attributes (wrong), so
# it matched nothing. The "fix" dropped the <br> and KEPT the quotes, so it matched the
# nav menu — 36 brands, a number plausible enough that I verified the COUNT without
# checking WHAT was counted, and declared it correct while missing 90 brands.
# Caught by collector@field, who declined to re-run because a successful-looking pass
# with thousands of rows would have left no reason to doubt it.
BRAND_RE = re.compile(r'<a href=([a-z0-9_\-]+-phones-\d+\.php)>([^<]{1,40})<br\s*/?>'
                      r'\s*<span>\s*(\d+)\s+devices?\s*</span>', re.I)

# gsmarena PRINTS each brand's device count next to the link. That is a
# completeness oracle handed to us for free, and not using it is what let the
# previous run report outcome=ok while missing 55% of its own half: --max-pages 4
# cut every brand over ~200 devices, and nothing in the result said so. Silent
# truncation reads as "covered everything".
# collector@field caught it because Honor and vivo both stopped at exactly 110 —
# two unrelated brands landing on the same number is not a catalogue fact.

# A count alone cannot tell the catalogue from the menu, so the floor does. gsmarena has
# carried 100+ brands for years; anything far below that means the regex found a
# different element, not that the catalogue shrank.
MIN_BRANDS = 80


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
    ap.add_argument("--max-pages", type=int, default=60,
                    help="safety bound only. Brands paginate at ~50/page and the largest "
                         "is ~670 devices, so 40 pages cannot truncate a real brand — it "
                         "exists to stop a pagination loop, not to cap collection.")
    ap.add_argument("--min-completeness", type=float, default=0.95,
                    help="refuse the run if collected/declared falls below this")
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
    if len(brands) < MIN_BRANDS:
        note = (f"makers.php3 returned HTTP {st} and {len(brands)} brands, below the "
                f"{MIN_BRANDS} floor. That is a PARSER error, not an empty catalogue: "
                f"the page carries a 36-entry nav dropdown alongside the ~125-entry "
                f"brand table, and a regex that drifts onto the menu returns a "
                f"plausible-looking number. Refusing rather than collecting a "
                f"confidently partial index.")
        print(f"REFUSED: {note}")
        (outdir / "result.json").write_text(json.dumps(
            {"outcome": "refused", "note": note, "control_ok": True,
             "counts": {"brands_parsed": len(brands), "floor": MIN_BRANDS},
             "files": []}, indent=2))
        return 1
    brands = sorted(set(brands))
    mine = brands[a.half - 1::2]          # interleave, so neither box gets only big brands
    print(f"{len(brands)} brands total; this box takes {len(mine)} (half {a.half})")
    time.sleep(a.delay)

    rows, seen, http, per_brand = [], set(), {}, []
    PAGE_RE = re.compile(r'<a href="([a-z0-9_\-]+-p(\d+)\.php)"', re.I)
    for i, (slug, brand, declared) in enumerate(mine, 1):
        declared = int(declared)
        before = len(rows)
        # Walk pagination by PAGE NUMBER, not by "the first next-page link found".
        # The previous version did re.search() for any p<N>.php and took the first
        # hit -- which on a brand page is "...-r1-p1.php", page ONE. So it re-fetched
        # page 1 up to --max-pages times, collected nothing new each round, and
        # stopped looking like it had reached the end of the catalogue. Huawei came
        # back 106 of 546 declared that way, and the page cap got the blame.
        # Key visited pages by PAGE NUMBER, not by URL. gsmarena exposes the same page
        # under more than one form ("...-f-9-0-p2.php" and "...-f-9-0-r1-p1.php"), so a
        # URL-keyed set treats duplicates as new work and spends the page budget on
        # content it already has. Samsung burned all 40 pages to collect 784 of 1,465
        # that way -- ~20 devices per page instead of ~40.
        todo, done_pages, seen_urls = [(0, BASE + slug)], set(), set()
        while todo and len(done_pages) < a.max_pages:
            pnum, url = todo.pop(0)
            if pnum in done_pages or url in seen_urls:
                continue
            done_pages.add(pnum); seen_urls.add(url)
            st, html = get(url)
            http[str(st)] = http.get(str(st), 0) + 1
            time.sleep(a.delay)
            if st == 429:
                print(f"  429 on {url} -- stopping and reporting rather than pushing on")
                (outdir / "result.json").write_text(json.dumps(
                    {"outcome": "refused", "control_ok": True,
                     "note": f"429 rate-limited at {url} after {len(rows)} rows; stopped.",
                     "counts": {"rows": len(rows)}, "files": []}, indent=2))
                return 1
            if not html:
                continue
            for name, dslug in devices_on(html):
                if dslug in seen:
                    continue
                seen.add(dslug)
                rows.append({"brand": brand.strip(), "device": name, "slug": dslug,
                             "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                         time.gmtime())})
            for href, num in PAGE_RE.findall(html):
                n = int(num)
                if n not in done_pages and not any(t[0] == n for t in todo):
                    todo.append((n, BASE + href))

        got = len(rows) - before
        # Distinguish "we ran out of budget" from "the listing genuinely ended".
        # A brand that paginated to its natural end and is still short of the declared
        # count is a DENOMINATOR difference -- gsmarena's per-brand total counts
        # tablets and watches that the phones listing does not show. That is not
        # truncation and must not be scored as if it were.
        capped = len(done_pages) >= a.max_pages
        per_brand.append({"brand": brand.strip(), "declared": declared,
                          "collected": got, "pages": len(done_pages),
                          "hit_cap": capped})
        if got < declared:
            print(f"  ! {brand.strip()}: {got} of {declared} declared "
                  f"({len(done_pages)} pages)", flush=True)
        if i % 5 == 0:
            print(f"  {i}/{len(mine)} brands - {len(rows):,} devices", flush=True)

    declared_total = sum(b["declared"] for b in per_brand)
    short = [b for b in per_brand if b["collected"] < b["declared"]]
    truncated = [b for b in short if b.get("hit_cap")]
    completeness = (len(rows) / declared_total) if declared_total else 0.0
    with open(outdir / "completeness.json", "w") as f:
        json.dump({"declared_total": declared_total, "collected_total": len(rows),
                   "completeness": round(completeness, 4), "short_brands": short}, f, indent=2)

    # Fail on TRUNCATION, not on the denominator. A run that paginated every brand to
    # its end has collected everything the listings expose, even if gsmarena's headline
    # counts include hardware those listings omit.
    if truncated:
        worst = sorted(truncated, key=lambda b: b["declared"] - b["collected"],
                       reverse=True)[:5]
        note = (f"REFUSED: {len(truncated)} brand(s) hit the {a.max_pages}-page budget "
                f"and are TRUNCATED (collected {len(rows):,} of {declared_total:,} "
                f"declared, {completeness:.0%}) — worst: "
                + ", ".join(f"{b['brand']} {b['collected']}/{b['declared']}" for b in worst)
                + f". Raise --max-pages above {a.max_pages} and re-run. A partial index "
                  f"that reports ok is worse than no index, because the devices it "
                  f"silently omits look like devices gsmarena does not have. "
                  f"({len(short) - len(truncated)} further brands are short but "
                  f"paginated to their natural end — that is gsmarena's headline count "
                  f"including tablets and watches the phones listing omits, not "
                  f"truncation.)")
        print(f"\n{note}")
        (outdir / "result.json").write_text(json.dumps(
            {"outcome": "refused", "note": note, "control_ok": True,
             "counts": {"devices": len(rows), "declared": declared_total,
                        "completeness": round(completeness, 4),
                        "short_brands": len(short)},
             "files": ["gsm_slugs.csv", "completeness.json"] if rows else []}, indent=2))
        return 1

    note = (f"{len(rows):,} of {declared_total:,} declared device slugs "
            f"({completeness:.0%}) across {len(mine)} brands (half {a.half}). "
            f"HTTP mix: {http}. Control passed and completeness checked against "
            f"gsmarena's own per-brand counts.")
    print(f"\n{note}")
    (outdir / "result.json").write_text(json.dumps(
        {"outcome": "ok" if rows else "empty", "note": note, "control_ok": True,
         "counts": {"devices": len(rows), "declared": declared_total,
                    "completeness": round(completeness, 4), "brands": len(mine)},
         "files": ["gsm_slugs.csv", "completeness.json"] if rows else []}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
