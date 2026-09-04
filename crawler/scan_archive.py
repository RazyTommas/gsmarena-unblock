#!/usr/bin/env python3
"""
scan_archive.py — sweep archive.org for real phone firmware (large files, no login)
and write them as source 'archive.org' entries for export.py to fold into the corpus.

archive.org is open + direct-download, so every hit here is fetchable with no account.
We keep only items whose largest archive file is big enough to be genuine firmware.

Run:  python scan_archive.py            # default: brands below, >=120MB
      python scan_archive.py --min-mb 300 --rows 40
"""
from __future__ import annotations
import argparse, json, re, urllib.parse, urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0"}
BRANDS = ["tecno", "infinix", "itel", "samsung", "xiaomi", "redmi", "realme",
          "oppo", "vivo", "huawei", "nokia", "lenovo", "motorola"]
FW_WORDS = re.compile(r"firmware|stock ?rom|flash ?file|scatter|MT6\d|SPD|SM-|"
                      r"spark|camon|phantom|pova|pouvoir|redmi|galaxy|note", re.I)
JUNK = re.compile(r"melody|podcast|tecnolog|cast|review|music|mp3|cia-|keyboard|"
                  r"game|movie|album|song", re.I)


def get(u):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30).read())


def model_of(title):
    m = re.search(r"\b(SM-[A-Z0-9]+|[A-Z]{2,3}\d[A-Z0-9]*|X\d{3,4}[A-Z]?|RMX\d+|CPH\d+)\b",
                  title.upper())
    return m.group(1) if m else None


def brand_of(title):
    t = title.lower()
    for b in BRANDS:
        if b in t:
            return b
    return None


def scan(min_bytes, rows):
    dest = Path(__file__).with_name("output") / "archive.org"
    dest.mkdir(parents=True, exist_ok=True)
    seen_ids, wrote = set(), 0
    for brand in BRANDS:
        q = urllib.parse.quote(f'title:({brand}) AND (firmware OR "stock rom" OR '
                               f'"flash file" OR MT6737 OR MT6755 OR MT6765 OR MT6580 OR rom)')
        url = (f"https://archive.org/advancedsearch.php?q={q}"
               f"&fl[]=identifier&fl[]=title&rows={rows}&output=json")
        try:
            docs = get(url)["response"]["docs"]
        except Exception as e:
            print(f"  [{brand}] query failed: {e}"); continue
        kept = 0
        for d in docs:
            ident = d["identifier"]; title = d.get("title", "") or ""
            if ident in seen_ids:
                continue
            if JUNK.search(title) and not FW_WORDS.search(title):
                continue
            if not FW_WORDS.search(title + " " + ident):
                continue
            try:
                meta = get(f"https://archive.org/metadata/{ident}")
                files = [f for f in meta.get("files", [])
                         if f["name"].lower().endswith((".zip", ".rar", ".gz", ".7z", ".tar", ".md5"))]
                if not files:
                    continue
                big = max(files, key=lambda f: int(f.get("size", 0) or 0))
                size = int(big.get("size", 0) or 0)
                if size < min_bytes:
                    continue
                seen_ids.add(ident)
                url_dl = f"https://archive.org/download/{ident}/" + urllib.parse.quote(big["name"])
                name = re.sub(r"\s+", " ", re.sub(r"\bby\b.*|\(.*?\)", "", title, flags=re.I)).strip()[:56]
                rec = {"source": "archive.org", "url": f"https://archive.org/details/{ident}",
                       "name": name or ident, "brand": brand_of(title) or brand,
                       "model": model_of(title), "version": big["name"][:60],
                       "size": f"{size/1e6:.0f}MB", "android": None,
                       "download_url": url_dl,
                       "downloads": [{"label": f"archive.org direct ({size/1e6:.0f}MB, no login)",
                                      "url": url_dl}]}
                (dest / f"{re.sub(r'[^a-z0-9]+','_',ident.lower())[:60]}.json").write_text(
                    json.dumps(rec, ensure_ascii=False, indent=2))
                wrote += 1; kept += 1
            except Exception:
                pass
        print(f"  [{brand}] +{kept} firmware items")
    print(f"\n[scan] wrote {wrote} archive.org firmware entries (>= {min_bytes/1e6:.0f}MB, direct/no-login)")
    return wrote


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-mb", type=int, default=120)
    ap.add_argument("--rows", type=int, default=40)
    args = ap.parse_args()
    scan(args.min_mb * 1_000_000, args.rows)


if __name__ == "__main__":
    main()
