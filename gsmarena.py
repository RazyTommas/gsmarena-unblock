#!/usr/bin/env python3
"""
gsmarena-unblock — Query GSMArena without triggering ad-block detection.

Handles:
  - DeviceID cookie parity (forced even)
  - counter-js.php3 beacon keepalive
  - Realistic browser fingerprint
  - Automatic rate limiting (≥5s between requests)
  - Session persistence across requests

Usage:
  python gsmarena.py search "Samsung Galaxy S24"
  python gsmarena.py specs samsung_galaxy_s24-12644
  python gsmarena.py brands
  python gsmarena.py brand samsung-phones-9
"""

import argparse
import json
import random
import re
import sys
import time
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, build_opener, HTTPCookieProcessor

BASE = "https://www.gsmarena.com/"
WAYBACK_CDX = "http://web.archive.org/cdx/search/cdx"
WAYBACK_FETCH = "https://web.archive.org/web/{ts}id_/{url}"
MIN_DELAY = 5.0  # seconds between requests — below this triggers the 429 wall
_last_request_time = 0.0

# --- Browser fingerprint ---
# A realistic Chrome-on-Windows fingerprint. GSMArena's server-side
# heuristics flag requests missing these headers.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-CH-UA": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Cache-Control": "max-age=0",
}


class GSMArenaClient:
    """HTTP client that bypasses GSMArena's ad-block detection."""

    def __init__(self, delay: float = MIN_DELAY):
        self.delay = max(delay, MIN_DELAY)
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self._last_ts = 0.0
        self._force_clean_cookie()

    def _force_clean_cookie(self):
        """
        Set DeviceID to an EVEN number before any request.

        GSMArena's misc.js encodes adblock status in the cookie's parity:
          ODD  = ad-blocker detected
          EVEN = clean
        We force it even. The value looks realistic (5-digit, random base).
        """
        from http.cookiejar import Cookie
        import time

        value = str(10000 + 2 * random.randint(0, 999))  # always even
        cookie = Cookie(
            version=0,
            name="DeviceID",
            value=value,
            port=None,
            port_specified=False,
            domain=".gsmarena.com",
            domain_specified=True,
            domain_initial_dot=True,
            path="/",
            path_specified=True,
            secure=False,
            expires=int(time.time()) + 7 * 86400,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
        self.jar.set_cookie(cookie)

    def _throttle(self):
        """Enforce minimum delay between requests."""
        now = time.time()
        wait = self.delay - (now - self._last_ts)
        if wait > 0:
            # Add jitter to look human
            wait += random.uniform(0.5, 2.0)
            time.sleep(wait)
        self._last_ts = time.time()

    def _send_beacon(self):
        """
        Fire the counter-js.php3 beacon.

        EasyPrivacy blocks this, which creates a pages-to-beacons ratio of 0
        on the server side — a clean adblock signal. We fire it to maintain
        a healthy ratio.
        """
        try:
            req = Request(BASE + "counter-js.php3", headers={
                "User-Agent": HEADERS["User-Agent"],
                "Referer": BASE,
                "X-Requested-With": "XMLHttpRequest",
            })
            self.opener.open(req, timeout=5)
        except Exception:
            pass  # non-critical — best effort

    def fetch(self, url: str, referer: str | None = None) -> str:
        """
        Fetch a page with full bypass active.

        Strategy:
          1. Try the live origin with the clean-cookie bypass.
          2. On HTTP 429 (IP/ASN ban — the cookie can't rescue an
             already-banned IP), fall back to the Wayback Machine, which
             serves the same content from a different infrastructure.
        """
        try:
            return self._fetch_live(url, referer)
        except HTTPError as e:
            if e.code == 429:
                sys.stderr.write(
                    "⚠ Live origin returned 429 (IP banned). "
                    "Falling back to Wayback Machine...\n"
                )
                return self._fetch_wayback(url)
            raise

    def _fetch_live(self, url: str, referer: str | None = None) -> str:
        """Fetch from the live origin with the ad-block bypass active."""
        self._throttle()
        self._force_clean_cookie()  # re-force before every request (misc.js re-sets on beforeunload)

        headers = dict(HEADERS)
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"

        req = Request(url, headers=headers)
        resp = self.opener.open(req, timeout=30)
        data = resp.read()

        # Handle gzip
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            data = gzip.decompress(data)
        elif resp.headers.get("Content-Encoding") == "br":
            try:
                import brotli
                data = brotli.decompress(data)
            except ImportError:
                pass

        html = data.decode("utf-8", errors="replace")

        # Fire beacon after page load (mimics real browser)
        self._send_beacon()

        return html

    def _fetch_wayback(self, url: str) -> str:
        """
        Fetch the newest 200-status snapshot of a URL from the Wayback Machine.

        Works from any IP — including datacenter/banned IPs — because the
        request goes to archive.org, not GSMArena. Content may be slightly
        stale, but GSMArena's data-spec markup is preserved intact.
        """
        # List archived 200-status snapshots, newest first
        cdx = (
            f"{WAYBACK_CDX}?url={quote_plus(url)}"
            "&output=json&filter=statuscode:200&limit=-12&collapse=digest"
        )
        req = Request(cdx, headers={"User-Agent": HEADERS["User-Agent"]})
        rows = json.loads(self.opener.open(req, timeout=30).read().decode())
        if len(rows) < 2:
            raise RuntimeError(f"No Wayback snapshot available for {url}")

        # Walk newest → oldest, skipping "poisoned" snapshots — captures where
        # Wayback's own crawler hit GSMArena's 429 wall and archived the block
        # page body. Return the newest snapshot with real content.
        for row in reversed(rows[1:]):  # rows[0] is the CDX header
            timestamp = row[1]
            snapshot = f"https://web.archive.org/web/{timestamp}id_/{url}"
            try:
                req = Request(snapshot, headers={"User-Agent": HEADERS["User-Agent"]})
                resp = self.opener.open(req, timeout=90)
                data = resp.read()
            except Exception:
                continue

            # The `id_` raw endpoint replays the ORIGINAL bytes, often gzip'd
            # with no Content-Encoding header. Detect the magic number.
            if data[:2] == b"\x1f\x8b" or resp.headers.get("Content-Encoding") == "gzip":
                import gzip
                data = gzip.decompress(data)

            html = data.decode("utf-8", errors="replace")
            if "Too Many Requests" in html or len(html) < 2000:
                sys.stderr.write(f"  · skipping poisoned snapshot {timestamp}\n")
                continue

            sys.stderr.write(f"  ✓ Wayback snapshot {timestamp}\n")
            return html

        raise RuntimeError(f"All recent Wayback snapshots for {url} are 429-poisoned")


# --- HTML parsers ---

class SearchResultParser(HTMLParser):
    """Extract phone names and URLs from search results."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._in_maker = False
        self._current = {}

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "div" and "makers" in d.get("class", ""):
            self._in_maker = True
        if self._in_maker and tag == "a" and d.get("href"):
            self._current = {"url": d["href"], "name": ""}
        if self._in_maker and tag == "span" and self._current:
            pass  # name is in the text

    def handle_data(self, data):
        if self._current:
            self._current["name"] += data.strip()

    def handle_endtag(self, tag):
        if tag == "a" and self._current and self._current.get("name"):
            self.results.append(self._current)
            self._current = {}


class SpecParser(HTMLParser):
    """Extract specs from a phone detail page."""

    def __init__(self):
        super().__init__()
        self.specs = {}
        self.phone_name = ""
        self._in_specs = False
        self._current_category = ""
        self._current_label = ""
        self._in_label = False
        self._in_value = False
        self._current_value = ""
        self._in_title = False
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = d.get("class", "")

        if tag == "h1" and "specs-phone-name" in cls:
            self._in_title = True

        if tag == "table" and "specs" in d.get("cellspacing", ""):
            self._in_specs = True

        if tag == "th" and self._in_specs:
            self._current_category = ""
            self._in_label = True

        if tag == "td" and "ttl" in cls:
            self._in_label = True
            self._current_label = ""

        if tag == "td" and "nfo" in cls:
            self._in_value = True
            self._current_value = ""

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.phone_name += text
        if self._in_label:
            if not self._current_category:
                self._current_category = text
            else:
                self._current_label = text
        if self._in_value:
            self._current_value += text + " "

    def handle_endtag(self, tag):
        if tag == "h1":
            self._in_title = False

        if tag == "td":
            if self._in_value and self._current_label:
                cat = self._current_category or "General"
                if cat not in self.specs:
                    self.specs[cat] = {}
                self.specs[cat][self._current_label] = self._current_value.strip()
            self._in_label = False
            self._in_value = False

        if tag == "table":
            self._in_specs = False


class BrandParser(HTMLParser):
    """Extract brand list from homepage."""

    def __init__(self):
        super().__init__()
        self.brands = []
        self._in_nav = False
        self._current = {}

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "div" and "brandmenu" in d.get("class", ""):
            self._in_nav = True
        if self._in_nav and tag == "a" and d.get("href"):
            self._current = {"url": d["href"], "name": ""}

    def handle_data(self, data):
        if self._current:
            self._current["name"] += data.strip()

    def handle_endtag(self, tag):
        if tag == "a" and self._current and self._current.get("name"):
            self.brands.append(self._current)
            self._current = {}
        if tag == "div":
            self._in_nav = False


# --- Commands ---

def cmd_search(client: GSMArenaClient, query: str):
    """Search for phones."""
    url = BASE + f"results.php3?sQuickSearch=yes&sName={quote_plus(query)}"
    html = client.fetch(url)

    # Quick regex fallback — works even if parser misses structure
    results = []
    for m in re.finditer(
        r'<a href="([^"]+\.php)"[^>]*>\s*<img[^>]*>\s*<span[^>]*>(?:<br\s*/?>)?([^<]+)',
        html,
    ):
        results.append({"url": m.group(1), "name": m.group(2).strip()})

    if not results:
        # Try parser
        parser = SearchResultParser()
        parser.feed(html)
        results = parser.results

    if not results:
        if "Too Many Requests" in html or "429" in html:
            print("⚠ Got 429 — your IP is banned. Change IP and retry.", file=sys.stderr)
            sys.exit(1)
        print(f"No results for '{query}'", file=sys.stderr)
        return

    print(json.dumps(results, indent=2))


def cmd_specs(client: GSMArenaClient, slug: str):
    """Get phone specs by slug (e.g., samsung_galaxy_s23-12082)."""
    if not slug.endswith(".php"):
        slug += ".php"
    url = urljoin(BASE, slug)
    html = client.fetch(url)

    # GSMArena tags every spec value with data-spec="<key>". This markup is
    # present in both live and Wayback HTML, so it's the robust parse path.
    specs = {}
    for m in re.finditer(r'data-spec="([a-z0-9_]+)"\s*>(.*?)</td>', html, re.S):
        key = m.group(1)
        # Strip inner tags and collapse whitespace
        value = re.sub(r"<[^>]+>", " ", m.group(2))
        value = re.sub(r"\s+", " ", value).strip()
        if value:
            specs[key] = value

    if not specs:
        # Fall back to the table parser
        parser = SpecParser()
        parser.feed(html)
        out = {"name": parser.phone_name.strip(), "specs": parser.specs}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    specs.pop("modelname", None)  # noisy — real name is in the <h1>
    m = re.search(r'<h1[^>]*class="specs-phone-name-title"[^>]*>([^<]+)</h1>', html)
    name = m.group(1).strip() if m else ""
    out = {"name": name, "url": url, "specs": specs}
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_brands(client: GSMArenaClient):
    """List all brands."""
    html = client.fetch(BASE)

    if "Too Many Requests" in html or "429" in html:
        print("⚠ Got 429 — your IP is banned. Change IP and retry.", file=sys.stderr)
        sys.exit(1)

    brands = []
    for m in re.finditer(
        r'<a href="([^"]*-phones-\d+\.php)"[^>]*>\s*([^<]+)', html
    ):
        brands.append({"url": m.group(1), "name": m.group(2).strip()})

    if not brands:
        parser = BrandParser()
        parser.feed(html)
        brands = parser.brands

    print(json.dumps(brands, indent=2))


def cmd_brand(client: GSMArenaClient, slug: str):
    """List phones for a brand."""
    if not slug.endswith(".php"):
        slug += ".php"
    url = urljoin(BASE, slug)
    html = client.fetch(url, referer=BASE)

    if "Too Many Requests" in html:
        print("⚠ Got 429 — your IP is banned.", file=sys.stderr)
        sys.exit(1)

    results = []
    for m in re.finditer(
        r'<a href="([^"]+\.php)"[^>]*>\s*<img[^>]*>\s*<span[^>]*>(?:<br\s*/?>)?([^<]+)',
        html,
    ):
        results.append({"url": m.group(1), "name": m.group(2).strip()})

    print(json.dumps(results, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Query GSMArena with ad-block detection bypass",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  %(prog)s search "iPhone 16 Pro"\n'
            "  %(prog)s specs apple_iphone_16_pro-12562\n"
            "  %(prog)s brands\n"
            "  %(prog)s brand apple-phones-48\n"
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=MIN_DELAY,
        help=f"Seconds between requests (min {MIN_DELAY}, default {MIN_DELAY})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Search for phones")
    p_search.add_argument("query", help="Search query")

    p_specs = sub.add_parser("specs", help="Get phone specs by slug")
    p_specs.add_argument("slug", help="Phone slug (e.g., samsung_galaxy_s24-12644)")

    sub.add_parser("brands", help="List all brands")

    p_brand = sub.add_parser("brand", help="List phones for a brand")
    p_brand.add_argument("slug", help="Brand slug (e.g., samsung-phones-9)")

    args = parser.parse_args()
    client = GSMArenaClient(delay=args.delay)

    if args.command == "search":
        cmd_search(client, args.query)
    elif args.command == "specs":
        cmd_specs(client, args.slug)
    elif args.command == "brands":
        cmd_brands(client)
    elif args.command == "brand":
        cmd_brand(client, args.slug)


if __name__ == "__main__":
    main()
