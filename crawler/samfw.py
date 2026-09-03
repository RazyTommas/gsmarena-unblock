"""
samfw.com parser — Samsung firmware by model + CSC (region/carrier).

samfw is Cloudflare-gated: it CANNOT be fetched with plain HTTP or headless Chromium
from this environment. Fetching requires a real browser that has cleared the Cloudflare
challenge (see tools/samfw_browser_fetch.md). This module is the *parser* only — give it
the HTML of a model page and it returns structured firmware rows.

Model page table columns:
  Device | CSC | Version | Bit/SW REV | Sec. Patch Lvl | OS / OneUI | Build date

Public API:
    parse_model(html, url) -> {source, url, model, name, roms:[...], ...}
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from selectolax.parser import HTMLParser

BASE = "https://samfw.com/"


def _col_index(headers: list[str]) -> dict:
    idx = {}
    for i, h in enumerate(headers):
        h = h.lower()
        if "device" in h:            idx["device"] = i
        elif "csc" in h:             idx["csc"] = i
        elif "version" in h:         idx["version"] = i
        elif "os" in h or "oneui" in h: idx["os"] = i
        elif "build date" in h:      idx["date"] = i
        elif "patch" in h:           idx["patch"] = i
    return idx


def parse_model(html: str, url: str) -> dict:
    tree = HTMLParser(html)
    m = re.search(r"/firmware/([A-Za-z0-9-]+)", url)
    model = (m.group(1) if m else None)

    table = None
    for t in tree.css("table"):
        head = " ".join(th.text(strip=True).lower() for th in t.css("th"))
        if "csc" in head and "version" in head:
            table = t
            break
    roms, name = [], None
    if table is not None:
        headers = [th.text(strip=True) for th in table.css("th")]
        ci = _col_index(headers)
        for tr in table.css("tbody tr"):
            td = tr.css("td")
            if len(td) < len(headers):
                continue
            def cell(key):
                i = ci.get(key)
                return td[i] if i is not None and i < len(td) else None
            dev = cell("device")
            if dev and not name:
                txt = dev.text(strip=True)
                name = txt.split("/", 1)[1] if "/" in txt else txt
            ver = cell("version")
            a = ver.css_first("a[href]") if ver else None
            os_cell = cell("os")
            os_txt = os_cell.text(separator=" ", strip=True) if os_cell else ""
            am = re.search(r"Android\s*(\d+)", os_txt)
            oneui = re.search(r"One ?UI\s*([\d.]+)", os_txt)
            csc = cell("csc")
            date = cell("date")
            roms.append({
                "region": csc.text(strip=True) if csc else None,   # CSC = region/carrier
                "version": ver.text(strip=True).split("Full")[0].strip() if ver else None,
                "android": am.group(1) if am else None,
                "oneui": oneui.group(1) if oneui else None,
                "build_date": (date.text(strip=True).split()[0] if date else None),
                "download_url": (a.attributes.get("href") if a else None),
            })

    roms.sort(key=lambda r: r.get("build_date") or "", reverse=True)
    return {
        "source": "samfw.com",
        "url": url,
        "model": model,
        "name": (f"Samsung {name}".strip() if name else model),
        "rom_count": len(roms),
        "latest_rom": roms[0] if roms else None,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "roms": roms,
    }
