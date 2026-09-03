"""
gsmarena_fetch — a drop-in, tiered fetcher for GSMArena pages.

Importable dependency for other tools (e.g. device-crawler). One call,
`fetch(url) -> html`, that transparently climbs a ladder of access tiers until
one returns real content.

    from gsmarena_fetch import fetch, Tiers
    html = fetch("https://www.gsmarena.com/samsung_galaxy_s23-12082.php")

DEFAULT POLICY (deliberate): only NON-evasive tiers are on by default —
  1. DIRECT   — plain request to the live origin
  3. WAYBACK  — public Web Archive snapshot (clean, no circumvention)

The middle tier —
  2. PROXY    — fetch the LIVE page through a clean-egress CORS proxy, which
                sidesteps an IP/ASN 429 ban —
is IP-ban circumvention. It is OFF BY DEFAULT and must be enabled explicitly by
the caller (a deployment decision, not a library default):

    html = fetch(url, tiers=Tiers(proxy=True))        # opt in per call
    # or globally:
    from gsmarena_fetch import DEFAULT_TIERS
    DEFAULT_TIERS.proxy = True

Cookie note: on the direct tier we set GSMArena's `DeviceID` cookie to an even
value, which neutralises the ad-block *detection* flag (its parity encodes the
verdict). That is not ban circumvention — it just avoids being mislabelled an
ad-block user — so it rides with the direct tier.

Stdlib only. No third-party deps.
"""

from __future__ import annotations

import gzip
import json
import random
import sys
import time
from dataclasses import dataclass
from http.cookiejar import Cookie, CookieJar
from urllib.error import HTTPError
from urllib.parse import quote_plus
from urllib.request import Request, build_opener, HTTPCookieProcessor

BASE = "https://www.gsmarena.com/"
WAYBACK_CDX = "http://web.archive.org/cdx/search/cdx"

# Clean-egress live proxies (evasion tier). Fetch the LIVE page from an
# un-banned IP. (name, url_template).
LIVE_PROXIES = [
    ("proxy.cors.sh", "https://proxy.cors.sh/{url}"),
    ("r.jina.ai", "https://r.jina.ai/{url}"),
]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

MIN_DELAY = 5.0  # polite floor between direct hits — below this trips the wall


class TurnstileChallenge(Exception):
    """Endpoint gated behind a Cloudflare Turnstile CAPTCHA (interactive)."""


class AllTiersFailed(Exception):
    """Every enabled tier failed to return real content."""


@dataclass
class Tiers:
    """Which access tiers are permitted. `proxy` is IP-ban circumvention."""
    direct: bool = True
    proxy: bool = False   # OFF by default — opt-in evasion
    wayback: bool = True


# Module-level default; callers may flip DEFAULT_TIERS.proxy = True globally.
DEFAULT_TIERS = Tiers()


def _is_gsmarena_page(html: str) -> bool:
    """True if this looks like a real GSMArena page (not a proxy/error page)."""
    if "Too Many Requests" in html:
        return False
    return "fdn.gsmarena.com" in html or "gsmarena.com/vv/" in html or "data-spec=" in html


class _Fetcher:
    def __init__(self, delay: float = MIN_DELAY, verbose: bool = True):
        self.delay = max(delay, MIN_DELAY)
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self._last_ts = 0.0
        self.verbose = verbose

    def _log(self, msg: str):
        if self.verbose:
            sys.stderr.write(msg + "\n")

    def _force_clean_cookie(self):
        """Set DeviceID to an EVEN value — neutralises ad-block detection."""
        value = str(10000 + 2 * random.randint(0, 999))
        self.jar.set_cookie(Cookie(
            0, "DeviceID", value, None, False,
            ".gsmarena.com", True, True, "/", True,
            False, int(time.time()) + 7 * 86400, False, None, None, {},
        ))

    def _throttle(self):
        wait = self.delay - (time.time() - self._last_ts)
        if wait > 0:
            time.sleep(wait + random.uniform(0.3, 1.2))
        self._last_ts = time.time()

    # --- Tier 1: direct ---
    def direct(self, url: str) -> str:
        self._throttle()
        self._force_clean_cookie()
        resp = self.opener.open(Request(url, headers=HEADERS), timeout=30)
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data.decode("utf-8", errors="replace")

    # --- Tier 2: live proxy (evasion, opt-in) ---
    def proxy(self, url: str) -> str | None:
        for name, template in LIVE_PROXIES:
            try:
                req = Request(template.format(url=url), headers={
                    **HEADERS,
                    "Origin": "https://gsmarena-unblock.local",
                    "x-requested-with": "gsmarena-unblock",
                })
                data = self.opener.open(req, timeout=45).read()
                if data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                html = data.decode("utf-8", errors="replace")
            except Exception as e:
                self._log(f"  · proxy {name} failed ({type(e).__name__})")
                continue
            if "Turnstile check" in html:
                raise TurnstileChallenge(url)
            if not _is_gsmarena_page(html):
                self._log(f"  · proxy {name} returned non-GSMArena content")
                continue
            self._log(f"  ✓ live via {name}")
            return html
        return None

    # --- Tier 3: Wayback (clean) ---
    def wayback(self, url: str) -> str | None:
        cdx = (f"{WAYBACK_CDX}?url={quote_plus(url)}"
               "&output=json&filter=statuscode:200&limit=-12&collapse=digest")
        try:
            rows = json.loads(self.opener.open(
                Request(cdx, headers=HEADERS), timeout=30).read().decode())
        except Exception:
            return None
        for row in reversed(rows[1:]):
            ts = row[1]
            snap = f"https://web.archive.org/web/{ts}id_/{url}"
            try:
                data = self.opener.open(Request(snap, headers=HEADERS), timeout=90).read()
            except Exception:
                continue
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            html = data.decode("utf-8", errors="replace")
            if "Too Many Requests" in html or len(html) < 2000:
                continue
            self._log(f"  ✓ Wayback snapshot {ts}")
            return html
        return None

    def fetch(self, url: str, tiers: Tiers) -> str:
        # Tier 1
        if tiers.direct:
            try:
                return self.direct(url)
            except HTTPError as e:
                if e.code != 429:
                    raise
                self._log("⚠ Live origin 429 (IP banned).")
            except Exception as e:
                self._log(f"⚠ direct failed ({type(e).__name__})")
        # Tier 2 (opt-in)
        if tiers.proxy:
            self._log("  → trying clean-egress proxies (evasion tier enabled)...")
            html = self.proxy(url)
            if html is not None:
                return html
        elif tiers.direct:
            self._log("  · proxy tier disabled (evasion off) — skipping to Wayback")
        # Tier 3
        if tiers.wayback:
            html = self.wayback(url)
            if html is not None:
                return html
        raise AllTiersFailed(url)


def fetch(url: str, tiers: Tiers | None = None, delay: float = MIN_DELAY,
          verbose: bool = True) -> str:
    """
    Fetch a GSMArena page, climbing enabled tiers until one returns real HTML.

    tiers   — which tiers are allowed. Defaults to DEFAULT_TIERS
              (direct + wayback; proxy/evasion OFF). Pass Tiers(proxy=True) to
              opt into IP-ban circumvention.
    Raises TurnstileChallenge for CAPTCHA-gated endpoints, AllTiersFailed if
    nothing worked.
    """
    return _Fetcher(delay=delay, verbose=verbose).fetch(url, tiers or DEFAULT_TIERS)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Tiered GSMArena fetcher (demo)")
    ap.add_argument("url")
    ap.add_argument("--proxy", action="store_true",
                    help="enable the IP-ban circumvention tier (off by default)")
    a = ap.parse_args()
    print(fetch(a.url, tiers=Tiers(proxy=a.proxy))[:2000])
