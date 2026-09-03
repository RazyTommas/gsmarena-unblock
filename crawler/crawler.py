#!/usr/bin/env python3
"""
device-crawler — gather device spec sheets from gsmarena.com by brand/family.

Design:
  * HTTP-first (httpx) with realistic headers + polite rate limiting + on-disk cache.
  * Lazy Playwright fallback: only spun up if plain HTTP starts getting blocked
    (Cloudflare "Just a moment", 403/503). Never imported unless needed.
  * One JSON file per device, all spec sections preserved, focus sections flagged.

Usage:
  python crawler.py samsung
  python crawler.py xiaomi --limit 20 --delay 2.5 --out output
  python crawler.py "samsung" --sections Network,Launch,Body,Platform,Misc
  python crawler.py --url https://www.gsmarena.com/samsung_galaxy_s26_fe_5g-14870.php
  python crawler.py --list-brands            # print all brands + ids

Extending to other sites (mifirm.net, etc.): add a parser in the SITES section
and route by hostname in scrape_device(). gsmarena is implemented here.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

import mifirm
import firmwarefile
import romprovider

BASE = "https://www.gsmarena.com/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE,
    "Connection": "keep-alive",
}
FOCUS_DEFAULT = ["Network", "Launch", "Body", "Platform", "Misc"]
CACHE_DIR = Path(".cache")
BLOCK_MARKERS = ("just a moment", "cf-browser-verification", "checking your browser",
                 "attention required", "enable javascript and cookies")


# --------------------------------------------------------------------------- #
# Fetching                                                                     #
# --------------------------------------------------------------------------- #
class Fetcher:
    """HTTP-first fetcher with disk cache, rate limiting, and a lazy browser fallback."""

    def __init__(self, delay: float = 2.0, use_cache: bool = True,
                 allow_browser: bool = True, verbose: bool = True):
        self.delay = delay
        self.use_cache = use_cache
        self.allow_browser = allow_browser
        self.verbose = verbose
        self._last = 0.0
        self._client = httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True)
        self._browser = None          # lazy playwright browser
        self._pw = None
        if use_cache:
            CACHE_DIR.mkdir(exist_ok=True)

    def _log(self, *a):
        if self.verbose:
            print(*a, file=sys.stderr, flush=True)

    def _cache_path(self, url: str) -> Path:
        key = re.sub(r"[^a-zA-Z0-9._-]", "_", url.replace(BASE, ""))[:180] or "index"
        return CACHE_DIR / f"{key}.html"

    def _throttle(self):
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, self.delay * 0.4))
        self._last = time.time()

    @staticmethod
    def _looks_blocked(status: int, text: str) -> bool:
        if status in (403, 429, 503):
            return True
        low = text[:4000].lower()
        return any(m in low for m in BLOCK_MARKERS)

    def get(self, url: str) -> str:
        url = urljoin(BASE, url)
        cp = self._cache_path(url)
        if self.use_cache and cp.exists():
            self._log(f"  [cache] {url}")
            return cp.read_text(encoding="utf-8", errors="replace")

        self._throttle()
        html, status = "", 0
        for attempt in range(3):
            try:
                r = self._client.get(url)
                status, html = r.status_code, r.text
                if not self._looks_blocked(status, html):
                    break
                self._log(f"  [http {status}] blocked, retry {attempt+1}/3 {url}")
                time.sleep(2 ** attempt + random.uniform(0, 1))
            except httpx.HTTPError as e:
                self._log(f"  [http error] {e} (attempt {attempt+1}/3)")
                time.sleep(2 ** attempt)

        if self._looks_blocked(status, html) and self.allow_browser:
            self._log(f"  [fallback] plain HTTP blocked ({status}); trying headless browser")
            html = self._get_browser(url) or html

        if self.use_cache and html and not self._looks_blocked(status, html):
            cp.write_text(html, encoding="utf-8")
        self._log(f"  [http {status or 'browser'}] {url}")
        return html

    # -- lazy Playwright fallback ------------------------------------------- #
    def _ensure_browser(self):
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._log("  [fallback] playwright not installed — `pip install playwright` "
                      "then `playwright install chromium`. Skipping browser fallback.")
            self.allow_browser = False
            return
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True, args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-dev-shm-usage"])
        except Exception as e:  # noqa: BLE001 — browsers may not be installed
            self._log(f"  [fallback] could not launch chromium ({e}). "
                      "Run `playwright install chromium`. Skipping browser fallback.")
            self.allow_browser = False

    def _get_browser(self, url: str) -> str | None:
        self._ensure_browser()
        if not self._browser:
            return None
        ctx = self._browser.new_context(
            user_agent=UA, locale="en-US", timezone_id="Europe/London",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
        # hide the most obvious automation tells before any page script runs
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});")
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # wait for a Cloudflare interstitial to clear (challenge auto-solves)
            for _ in range(20):  # up to ~20s
                html = page.content()
                if not self._looks_blocked(200, html):
                    return html
                page.wait_for_timeout(1000)
            return None
        except Exception as e:  # noqa: BLE001
            self._log(f"  [fallback] browser fetch failed: {e}")
            return None
        finally:
            ctx.close()

    def close(self):
        self._client.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


# --------------------------------------------------------------------------- #
# gsmarena parsers                                                             #
# --------------------------------------------------------------------------- #
def list_brands(fetcher: Fetcher) -> list[dict]:
    """Return [{name, slug_url, id, devices}] from the makers page."""
    html = fetcher.get("makers.php3")
    tree = HTMLParser(html)
    brands = []
    for a in tree.css("table td a"):
        href = a.attributes.get("href", "") or ""
        m = re.match(r"([a-z0-9_]+)-phones-(\d+)\.php$", href)
        if not m:
            continue
        raw = a.text(strip=True)
        cnt = re.search(r"(\d+)\s*devices?", raw)
        name = re.sub(r"\d+\s*devices?$", "", raw).strip()
        brands.append({"name": name, "slug": m.group(1), "id": int(m.group(2)),
                       "url": href, "devices": int(cnt.group(1)) if cnt else None})
    return brands


def resolve_brand(fetcher: Fetcher, query: str) -> dict:
    brands = list_brands(fetcher)
    q = query.strip().lower()
    exact = [b for b in brands if b["name"].lower() == q or b["slug"] == q]
    if exact:
        return exact[0]
    partial = [b for b in brands if q in b["name"].lower() or q in b["slug"]]
    if not partial:
        raise SystemExit(f"No brand matching {query!r}. Try --list-brands.")
    partial.sort(key=lambda b: (b["devices"] or 0), reverse=True)
    if len(partial) > 1:
        print(f"[i] {query!r} matched {len(partial)} brands; using "
              f"{partial[0]['name']!r}. Others: "
              f"{', '.join(b['name'] for b in partial[1:6])}", file=sys.stderr)
    return partial[0]


def enumerate_devices(fetcher: Fetcher, brand: dict, limit: int | None = None) -> list[dict]:
    """Page through a brand listing; return [{url, name}]."""
    first_html = fetcher.get(brand["url"])
    tree = HTMLParser(first_html)

    max_page = 1
    for a in tree.css("div.nav-pages a"):
        m = re.search(r"-p(\d+)\.php", a.attributes.get("href", "") or "")
        if m:
            max_page = max(max_page, int(m.group(1)))

    def links_from(t: HTMLParser) -> list[dict]:
        out = []
        for a in t.css("div.makers ul li a"):
            href = a.attributes.get("href", "") or ""
            if re.search(r"-\d+\.php$", href):
                name = a.css_first("span")
                out.append({"url": urljoin(BASE, href),
                            "name": name.text(strip=True) if name else a.text(strip=True)})
        return out

    devices = links_from(tree)
    for p in range(2, max_page + 1):
        if limit and len(devices) >= limit:
            break
        page_url = f"{brand['slug']}-phones-f-{brand['id']}-0-p{p}.php"
        devices += links_from(HTMLParser(fetcher.get(page_url)))
    return devices[:limit] if limit else devices


def parse_device(html: str, url: str, focus: list[str]) -> dict:
    tree = HTMLParser(html)
    name_el = tree.css_first("h1.specs-phone-name-title") or tree.css_first("h1")
    name = name_el.text(strip=True) if name_el else None
    m = re.search(r"-(\d+)\.php", url)

    sections: dict[str, dict] = {}
    for table in tree.css("#specs-list table"):
        th = table.css_first("th")
        section = th.text(strip=True) if th else None
        if not section:
            continue
        rows: dict[str, str] = {}
        last_key = None
        for tr in table.css("tr"):
            nfo = tr.css_first("td.nfo")
            if nfo is None:
                continue
            ttl = tr.css_first("td.ttl")
            key = ttl.text(strip=True) if ttl and ttl.text(strip=True) else None
            spec = nfo.attributes.get("data-spec")  # stable machine key
            val = nfo.text(strip=True).replace("\xa0", " ")
            # rows with no title continue the previous field (e.g. extra cameras)
            label = key or spec or last_key or "info"
            if label in rows:
                rows[label] += " | " + val
            else:
                rows[label] = val
            last_key = key or last_key
        if rows:
            sections[section] = rows

    focus_data = {s: sections[s] for s in focus if s in sections}
    img = tree.css_first(".specs-photo-main img")
    return {
        "source": "gsmarena.com",
        "url": url,
        "gsmarena_id": int(m.group(1)) if m else None,
        "name": name,
        "image": img.attributes.get("src") if img else None,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "focus": focus_data,
        "sections": sections,
    }


def scrape_device(fetcher: Fetcher, url: str, focus: list[str]) -> dict:
    """Route a single URL to the right site parser by hostname."""
    if "mifirm.net" in url:
        return mifirm.scrape_model(fetcher, url)
    if "firmwarefile.com" in url:
        return firmwarefile.scrape_device(fetcher, url)
    return parse_device(fetcher.get(url), urljoin(BASE, url), focus)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "device").lower()).strip("_")


def main():
    ap = argparse.ArgumentParser(description="Crawl device specs from gsmarena by brand.")
    ap.add_argument("brand", nargs="?", help="brand/family name, e.g. samsung, xiaomi")
    ap.add_argument("--url", help="scrape a single device/model page URL (gsmarena or mifirm)")
    ap.add_argument("--mifirm", nargs="?", const="", metavar="FILTER",
                    help="crawl mifirm.net ROMs; optional name filter e.g. 'redmi note', 'poco'")
    ap.add_argument("--ff", metavar="BRAND",
                    help="crawl firmwarefile.com for a brand: samsung, infinix, itel, "
                         "realme, oppo, vivo, xiaomi …")
    ap.add_argument("--rp", metavar="BRAND",
                    help="crawl romprovider.com firmware for a brand (e.g. tecno, infinix)")
    ap.add_argument("--list-brands", action="store_true", help="print all brands and exit")
    ap.add_argument("--list-models", action="store_true",
                    help="print all mifirm models (codename/name) and exit")
    ap.add_argument("--sections", default=",".join(FOCUS_DEFAULT),
                    help="comma-separated focus sections (default: %(default)s)")
    ap.add_argument("--limit", type=int, help="max devices to scrape")
    ap.add_argument("--delay", type=float, default=2.0, help="base seconds between requests")
    ap.add_argument("--out", default="output", help="output directory for JSON files")
    ap.add_argument("--no-cache", action="store_true", help="disable on-disk HTML cache")
    ap.add_argument("--no-browser", action="store_true", help="disable Playwright fallback")
    args = ap.parse_args()

    focus = [s.strip() for s in args.sections.split(",") if s.strip()]
    fetcher = Fetcher(delay=args.delay, use_cache=not args.no_cache,
                      allow_browser=not args.no_browser)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        if args.list_brands:
            for b in sorted(list_brands(fetcher), key=lambda x: -(x["devices"] or 0)):
                print(f"{b['devices'] or '?':>5}  {b['name']:<22} {b['url']}")
            return

        if args.list_models:
            for m in mifirm.list_models(fetcher):
                print(f"{m['codename']:<14} {m['name']:<40} {m['regions']}")
            return

        # ----- mifirm.net ROM crawl ----- #
        if args.mifirm is not None:
            models = mifirm.list_models(fetcher)
            flt = args.mifirm.strip().lower()
            if flt:
                models = [m for m in models
                          if flt in m["name"].lower() or flt in m["codename"].lower()]
            if args.limit:
                models = models[:args.limit]
            suffix = f" matching {flt!r}" if flt else ""
            print(f"[i] mifirm: {len(models)} models{suffix}", file=sys.stderr)
            dest = out / "mifirm"
            dest.mkdir(parents=True, exist_ok=True)
            ok = 0
            for i, m in enumerate(models, 1):
                try:
                    data = mifirm.scrape_model(fetcher, m["url"])
                    fname = f"{data['codename']}_{slugify(data.get('name'))}.json"
                    (dest / fname).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
                    latest = data.get("latest_rom") or {}
                    print(f"[{i}/{len(models)}] {data.get('name')} ({data['codename']})  "
                          f"{data['rom_count']} ROMs  latest={latest.get('version','-')}  "
                          f"-> {fname}", file=sys.stderr)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    print(f"[{i}/{len(models)}] FAILED {m['url']}: {e}", file=sys.stderr)
            print(f"\n[done] {ok}/{len(models)} models -> {dest}/", file=sys.stderr)
            return

        # ----- firmwarefile.com multi-brand ROM crawl ----- #
        if args.ff:
            brand = args.ff.strip().lower()
            devs = firmwarefile.list_category(fetcher, brand, limit=args.limit)
            print(f"[i] firmwarefile {brand}: {len(devs)} device pages", file=sys.stderr)
            dest = out / f"firmwarefile_{brand}"
            dest.mkdir(parents=True, exist_ok=True)
            ok = 0
            for i, dv in enumerate(devs, 1):
                try:
                    data = firmwarefile.scrape_device(fetcher, dv["url"])
                    fname = f"{slugify(data.get('model') or data.get('name'))}.json"
                    (dest / fname).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
                    print(f"[{i}/{len(devs)}] {data.get('name')}  model={data.get('model')}  "
                          f"{len(data.get('downloads', []))} mirrors  -> {fname}", file=sys.stderr)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    print(f"[{i}/{len(devs)}] FAILED {dv['url']}: {e}", file=sys.stderr)
            print(f"\n[done] {ok}/{len(devs)} devices -> {dest}/", file=sys.stderr)
            return

        # ----- romprovider.com firmware crawl (Tecno etc.) ----- #
        if args.rp:
            brand = args.rp.strip().lower()
            devs = romprovider.list_firmware(fetcher, brand, limit=args.limit)
            print(f"[i] romprovider {brand}: {len(devs)} firmware pages", file=sys.stderr)
            dest = out / f"romprovider_{brand}"
            dest.mkdir(parents=True, exist_ok=True)
            ok = 0
            for i, dv in enumerate(devs, 1):
                try:
                    data = romprovider.scrape_device(fetcher, dv["url"])
                    fname = f"{slugify(data.get('model') or data.get('name'))}_{i}.json"
                    (dest / fname).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
                    dl = "yes" if data.get("download_url") else "no-link"
                    print(f"[{i}/{len(devs)}] {data.get('name')}  model={data.get('model')}  "
                          f"ver={data.get('version')}  dl={dl}", file=sys.stderr)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    print(f"[{i}/{len(devs)}] FAILED {dv['url']}: {e}", file=sys.stderr)
            print(f"\n[done] {ok}/{len(devs)} firmware -> {dest}/", file=sys.stderr)
            return

        if args.url:
            targets = [{"url": args.url, "name": None}]
            brand_slug = "single"
        elif args.brand:
            brand = resolve_brand(fetcher, args.brand)
            brand_slug = brand["slug"]
            print(f"[i] {brand['name']}: {brand['devices']} devices listed", file=sys.stderr)
            targets = enumerate_devices(fetcher, brand, limit=args.limit)
            print(f"[i] enumerated {len(targets)} device pages", file=sys.stderr)
        else:
            ap.error("provide a brand name, --url, or --list-brands")

        dest = out / brand_slug
        dest.mkdir(parents=True, exist_ok=True)
        ok = 0
        for i, t in enumerate(targets, 1):
            try:
                data = scrape_device(fetcher, t["url"], focus)
                # guard: a rate-limited / challenge page parses to no spec sections —
                # don't persist it as a device (it would poison the dataset).
                if "sections" in data and len(data.get("sections") or {}) < 3:
                    raise RuntimeError(f"no specs (likely rate-limited: {data.get('name')!r})")
                fname = f"{data.get('gsmarena_id') or i}_{slugify(data.get('name'))}.json"
                (dest / fname).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
                got = ", ".join(k for k in focus if k in data["focus"])
                print(f"[{i}/{len(targets)}] {data.get('name')}  ->  {fname}  "
                      f"[{got or 'no focus sections'}]", file=sys.stderr)
                ok += 1
            except Exception as e:  # noqa: BLE001 — keep crawling on a single bad page
                print(f"[{i}/{len(targets)}] FAILED {t['url']}: {e}", file=sys.stderr)
        print(f"\n[done] {ok}/{len(targets)} devices -> {dest}/", file=sys.stderr)
    finally:
        fetcher.close()


if __name__ == "__main__":
    main()
