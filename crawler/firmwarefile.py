"""
firmwarefile.com parser — multi-brand stock firmware (Samsung, Infinix, itel,
Realme, Oppo, Vivo, Xiaomi, …). One parser, routed by brand category.

Structure:
  category page  /category/<brand>[/page/N]  -> 40 device links /<brand>-<model>
  device page    /<slug>                      -> name, model code, download mirrors

Public API (all take a Fetcher from crawler.py):
    list_category(fetcher, brand, limit=None) -> [{name, url, model}]
    parse_device(html, url)                   -> {source, brand, name, model, downloads, ...}
    scrape_device(fetcher, url)               -> parse_device(fetch(url))
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

BASE = "https://firmwarefile.com/"
BRANDS = ["samsung", "infinix", "itel", "realme", "oppo", "vivo", "xiaomi",
          "huawei", "nokia", "motorola", "lenovo", "lg", "htc", "asus"]
_DEV_RE = re.compile(r"^https?://firmwarefile\.com/([a-z0-9][a-z0-9-]+)/?$", re.I)


def _model(name: str, slug: str = "") -> str | None:
    """Pull a model code from the title/slug (SM-S928B, X6895, RMX3999, CPH2451…)."""
    for pat in (r"\bSM-[A-Z0-9]+\b", r"\b[A-Z]{1,4}\d{3,5}[A-Z0-9]*\b"):
        m = re.search(pat, name, re.I)
        if m:
            return m.group(0).upper()
    tail = slug.rsplit("-", 1)[-1] if slug else ""
    return tail.upper() if re.search(r"\d", tail) else None


def list_category(fetcher, brand: str, limit: int | None = None) -> list[dict]:
    brand = brand.strip().lower()
    first = HTMLParser(fetcher.get(urljoin(BASE, f"category/{brand}")))
    max_page = 1
    for a in first.css("a"):
        m = re.search(rf"/category/{re.escape(brand)}/page/(\d+)", a.attributes.get("href", "") or "")
        if m:
            max_page = max(max_page, int(m.group(1)))

    def links(tree: HTMLParser) -> list[dict]:
        out, seen = [], set()
        for a in tree.css("a"):
            href = (a.attributes.get("href") or "").rstrip("/")
            txt = a.text(strip=True)
            m = _DEV_RE.match(href + "/")
            if not m or "category" in href or not txt or len(txt) < 6:
                continue
            slug = m.group(1)
            if brand not in slug and brand not in txt.lower():
                continue
            if href in seen:
                continue
            seen.add(href)
            out.append({"name": txt, "url": href, "model": _model(txt, slug)})
        return out

    devices = links(first)
    for p in range(2, max_page + 1):
        if limit and len(devices) >= limit:
            break
        devices += links(HTMLParser(fetcher.get(urljoin(BASE, f"category/{brand}/page/{p}"))))
    return devices[:limit] if limit else devices


def parse_device(html: str, url: str) -> dict:
    tree = HTMLParser(html)
    h1 = tree.css_first("h1")
    name = h1.text(strip=True) if h1 else None
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    brand = slug.split("-", 1)[0]

    # download mirrors (dedupe, keep label + target)
    downloads, seen = [], set()
    for a in tree.css("a"):
        href = (a.attributes.get("href") or "").strip()
        txt = a.text(strip=True)
        if not href.startswith("http") or not txt:
            continue
        if re.search(r"mirror|get link|download firmware|\.zip", txt, re.I) \
                and not re.search(r"odin|tool|driver|usb", txt, re.I):
            if href in seen:
                continue
            seen.add(href)
            downloads.append({"label": txt, "url": href})

    # best-effort firmware facts from body text
    body = tree.body.text(separator="\n") if tree.body else ""
    def find(pat):
        m = re.search(pat, body, re.I)
        return m.group(1).strip() if m else None
    android = find(r"Android\s*(?:version)?\s*[:\-]?\s*(1[0-9](?:\.\d+)?)")
    size = find(r"(?:File\s*)?Size\s*[:\-]?\s*([\d.]+\s*[GM]B)")
    build = find(r"Build\s*(?:Number)?\s*[:\-]?\s*([A-Z0-9.]{4,})")

    free = next((d["url"] for d in downloads if "free" in d["label"].lower()), None)
    return {
        "source": "firmwarefile.com",
        "url": url,
        "brand": brand,
        "name": name,
        "model": _model(name or "", slug),
        "android": android,
        "size": size,
        "build": build,
        "download_url": free or (downloads[0]["url"] if downloads else None),
        "downloads": downloads,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def scrape_device(fetcher, url: str) -> dict:
    return parse_device(fetcher.get(url), urljoin(BASE, url))
