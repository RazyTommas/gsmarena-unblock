"""
romprovider.com parser — Tecno / Infinix / itel (and more) stock firmware.
Plain HTTP, no Cloudflare. This is the working **Tecno** source (firmwarefile has no
Tecno category; givemerom is Cloudflare/Turnstile-gated).

  search /?s=<brand>+firmware  -> firmware-flash-file device pages
  device page                  -> name, model code, version, direct download link

Public API (all take a Fetcher from crawler.py):
    list_firmware(fetcher, brand, limit=None) -> [{name, url}]
    parse_device(html, url)                   -> {source, brand, name, model, ...}
    scrape_device(fetcher, url)               -> parse_device(fetch(url))
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urljoin, quote

from selectolax.parser import HTMLParser

BASE = "https://romprovider.com/"
_DL_HOSTS = re.compile(r"drive\.google|mega\.nz|mediafire|androidfilehost|sfile|mega\.co", re.I)
_TOOL = re.compile(r"tool|driver|rom2box|backup|restore|sp-flash|scatter|usb|root|gcam|pinout", re.I)


def list_firmware(fetcher, brand: str, limit: int | None = None) -> list[dict]:
    brand = brand.strip().lower()
    out, seen = [], set()
    page = 1
    while True:
        url = urljoin(BASE, f"page/{page}/?s={quote(brand + ' firmware flash file')}") if page > 1 \
            else urljoin(BASE, f"?s={quote(brand + ' firmware flash file')}")
        tree = HTMLParser(fetcher.get(url))
        found = 0
        for a in tree.css("h2 a, h3 a, .entry-title a, article a"):
            href = (a.attributes.get("href") or "").rstrip("/")
            txt = a.text(strip=True)
            if (brand in href.lower() and "firmware" in href.lower()
                    and "flash-file" in href.lower() and href not in seen and txt):
                seen.add(href)
                out.append({"name": txt, "url": href})
                found += 1
                if limit and len(out) >= limit:
                    return out
        if not found or page >= 8:
            break
        page += 1
    return out


def _model(name: str) -> str | None:
    # Tecno/Infinix/itel model codes: CM8, KJ5, LI7, X6895, etc. Prefer the code in ().
    m = re.search(r"\b([A-Z]{1,3}\d[A-Z0-9]*)\b", re.sub(r"\(.*?\)", " ", name or ""))
    return m.group(1).upper() if m else None


def parse_device(html: str, url: str) -> dict:
    tree = HTMLParser(html)
    h1 = tree.css_first("h1")
    name = h1.text(strip=True) if h1 else None
    # strip the "... Firmware Flash File (Stock ROM)" boilerplate to leave the device name
    if name:
        name = re.sub(r"\s*(Firmware\s*)?(Flash\s*File\s*)?(Firmware\s*)?\(?Stock ROM\)?.*$",
                      "", name, flags=re.I).strip()
        name = re.sub(r"\s*(Firmware|Flash File)+\s*$", "", name, flags=re.I).strip()

    best = None  # the firmware download link (drive/mega/... with a version-like label)
    for a in tree.css("a"):
        href = (a.attributes.get("href") or "").strip()
        txt = a.text(strip=True)
        if not href.startswith("http") or not txt or _TOOL.search(txt) or _TOOL.search(href):
            continue
        if _DL_HOSTS.search(href):
            # prefer the label that carries a version/build string
            score = len(txt) + (20 if re.search(r"\d+\.\d+", txt) else 0)
            if best is None or score > best[0]:
                best = (score, txt, href)

    version = best[1] if best else None
    android = None
    if version:
        am = re.search(r"\b(1[0-9]|[7-9])\b", version)  # crude Android hint
        android = am.group(1) if am else None
    return {
        "source": "romprovider.com",
        "url": url,
        "brand": (name or "").split()[0].lower() if name else None,
        "name": name,
        "model": _model(name),
        "version": version,
        "android": None,      # romprovider rarely states Android cleanly; leave null
        "download_url": best[2] if best else None,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def scrape_device(fetcher, url: str) -> dict:
    return parse_device(fetcher.get(url), urljoin(BASE, url))
