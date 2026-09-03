"""
mifirm.net parser — Xiaomi / Redmi / POCO firmware (ROM) repository.

Model pages live at /model/<codename>.ttt and list firmware builds grouped by
Type (Fastboot / Recovery-ZIP), Branch (Stable / Developer) and Region. Each build
row carries: MIUI/HyperOS version, Android version, file size, update date, download
count, and a direct download link.

Public API (all take a Fetcher from crawler.py):
    list_models(fetcher)            -> [{codename, name, regions, url}]
    parse_model(html, url)          -> {source, url, codename, name, roms:[...], ...}
    scrape_model(fetcher, url, ...) -> parse_model(fetch(url))
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

BASE = "https://mifirm.net/"
REGIONS = ["China", "Global", "Russian", "Indo", "EEA", "Taiwan",
           "Japan", "Turkey", "India", "EU"]
_H4_RE = re.compile(r"(Fastboot|ZIP)(Stable|Developer)(" + "|".join(REGIONS) + r")$")
_MODEL_RE = re.compile(r"/model/([a-z0-9_]+)\.ttt", re.I)


def list_models(fetcher) -> list[dict]:
    """All device models from the mifirm homepage."""
    tree = HTMLParser(fetcher.get(BASE))
    out, seen = [], set()
    for a in tree.css("a"):
        href = a.attributes.get("href", "") or ""
        m = _MODEL_RE.search(href)
        if not m:
            continue
        code = m.group(1)
        if code in seen:
            continue
        seen.add(code)
        # link text is "Marketing name(codename)REGIONS"
        raw = a.text(strip=True)
        name = re.sub(r"\(" + re.escape(code) + r"\).*$", "", raw).strip()
        regions = raw[raw.rfind(")") + 1:] if ")" in raw else ""
        out.append({"codename": code, "name": name or code,
                    "regions": regions, "url": urljoin(BASE, href)})
    return out


def _label(text: str):
    """'Xiaomi 17T ProFastbootStableChina' -> (name, type, branch, region)."""
    m = _H4_RE.search(text or "")
    if not m:
        return None
    name = text[:m.start()].strip()
    typ = "fastboot" if m.group(1) == "Fastboot" else "recovery"
    return name, typ, m.group(2).lower(), m.group(3)


def _prev_h4(table) -> str | None:
    cur = table
    for _ in range(8):
        prev = cur.prev
        while prev is not None and getattr(prev, "tag", None) != "h4":
            prev = prev.prev
        if prev is not None and prev.tag == "h4":
            return prev.text(strip=True)
        cur = cur.parent
        if cur is None:
            return None
    return None


def parse_model(html: str, url: str) -> dict:
    tree = HTMLParser(html)
    m = _MODEL_RE.search(url)
    codename = m.group(1) if m else None

    name = None
    roms: list[dict] = []
    for table in tree.css("div.table-responsive table"):
        lab = _label(_prev_h4(table) or "")
        if not lab:
            continue
        dev_name, typ, branch, region = lab
        name = name or dev_name
        for tr in (table.css("tbody tr") or table.css("tr")):
            td = tr.css("td")
            if len(td) < 6 or tr.css_first("th"):
                continue
            a = tr.css_first("a[href]")
            href = (a.attributes.get("href") or "").strip() if a else None
            roms.append({
                "version": td[0].text(strip=True),        # MIUI / HyperOS version
                "android": td[1].text(strip=True),
                "size": td[2].text(strip=True),
                "updated_at": td[3].text(strip=True),
                "downloads": td[4].text(strip=True),
                "type": typ,                              # fastboot | recovery
                "branch": branch,                         # stable | developer
                "region": region,
                "download_url": urljoin(BASE, href) if href else None,
            })

    # newest first by update date (string sort works on YYYY-MM-DD…)
    roms.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    title = tree.css_first("h1")
    return {
        "source": "mifirm.net",
        "url": url,
        "codename": codename,
        "name": name or (title.text(strip=True) if title else codename),
        "title": title.text(strip=True) if title else None,
        "rom_count": len(roms),
        "latest_rom": roms[0] if roms else None,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "roms": roms,
    }


def scrape_model(fetcher, url: str) -> dict:
    return parse_model(fetcher.get(url), urljoin(BASE, url))
