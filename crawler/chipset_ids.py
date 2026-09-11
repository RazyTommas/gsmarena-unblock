#!/usr/bin/env python3
"""
chipset_ids.py — normalise a chipset string into the identifiers advisories use.

The join problem: chipset security bulletins name chips by PART NUMBER
(MT6835, SM8650, S5E9945) while device spec sheets carry a mixed human string
("Qualcomm SM8750-AC Snapdragon 8 Elite (3 nm)"). Luckily gsmarena-style strings
usually carry BOTH, so we can extract a canonical part number and a marketing
name from the same text and key advisories off the part number.

    parse("Qualcomm SM8750-AC Snapdragon 8 Elite (3 nm)")
    -> {vendor: 'qualcomm', part: 'SM8750', variant: 'SM8750-AC',
        marketing: 'Snapdragon 8 Elite', node: '3 nm', key: 'qualcomm:sm8750'}

Where a part number is absent we fall back to the marketing name as the key
(`mediatek:dimensity 9400`) and flag it, because a marketing-name key is weaker:
one marketing name can span several part numbers.
"""
from __future__ import annotations
import re

PART_PATTERNS = [
    ("qualcomm", re.compile(r"\b(S[DM]M?\d{3,4}|SM\d{4}|QCS\d{3,4}|APQ\d{4}|MSM\d{4})(-[A-Z0-9]+)?\b", re.I)),
    ("mediatek", re.compile(r"\b(MT\d{4,5})([A-Z]{0,2})\b", re.I)),
    ("samsung",  re.compile(r"\b(S5E\d{4}|S5P[A-Z]?\d{3,4}|S5L\d{4})\b", re.I)),
]

MARKETING = [
    # Ordered longest-form-first and anchored per form. A single lazy pattern does NOT
    # work here: with `Snapdragon[\w\s]*?(Gen\s*\d+|\d[\w]*)` the engine satisfies the
    # lazy prefix at zero width and matches the bare-number branch, so
    # "Snapdragon 8 Gen 3" silently became "Snapdragon 8" — a different chip, and the
    # key for 68 of our chipset strings. Alternation order is load-bearing.
    ("qualcomm", re.compile(
        r"(Snapdragon\s+(?:"
        r"\d{1,2}[sS]?\+?\s+Elite(?:\s+Gen\s*\d+)?"       # 8 Elite Gen 5
        r"|Elite(?:\s+Gen\s*\d+)?"                         # Elite Gen 5
        r"|\d{1,3}[sS]?\+?\s+Gen\s*\d+(?:\s+for\s+Galaxy)?"  # 8 Gen 3 for Galaxy / 7s Gen 3
        r"|\d{3,4}[A-Za-z\+]*(?:\s+Plus)?"                 # 680 / 778G+ / 750G
        r"|\d{1,2}[sS]?\+?"                                # bare "Snapdragon 4" (last resort)
        r"))", re.I)),
    ("mediatek", re.compile(
        r"((?:Dimensity|Helio)\s+[A-Z]?\d+[\w\+]*"
        r"(?:\s+(?:Ultra|Ultimate|Pro|Plus))?)", re.I)),
    ("samsung",  re.compile(r"(Exynos\s+[\w\d]+)", re.I)),
    ("apple",    re.compile(r"(Apple\s+[AM]\d{1,2}(?:\s+(?:Pro|Max|Ultra|Bionic|Fusion))?)", re.I)),
    ("google",   re.compile(r"(Tensor\s*[\w\d]*)", re.I)),
    ("unisoc",   re.compile(r"(Unisoc\s+[\w\d]+)", re.I)),
    ("hisilicon", re.compile(r"(Kirin\s+[\w\d]+)", re.I)),
]

NODE = re.compile(r"\((\d+\s*nm)[^)]*\)", re.I)


def parse(s: str) -> dict | None:
    """Chipset string -> identifiers. None when nothing recognisable."""
    if not s or not s.strip():
        return None
    txt = s.strip()
    out = {"raw": txt, "vendor": None, "part": None, "variant": None,
           "marketing": None, "node": None, "key": None, "key_kind": None}

    for vend, pat in PART_PATTERNS:
        m = pat.search(txt)
        if m:
            out["vendor"] = vend
            out["part"] = m.group(1).upper()
            out["variant"] = m.group(0).upper()
            break

    for vend, pat in MARKETING:
        m = pat.search(txt)
        if m:
            out["vendor"] = out["vendor"] or vend
            out["marketing"] = re.sub(r"\s+", " ", m.group(1)).strip()
            break

    n = NODE.search(txt)
    if n:
        out["node"] = re.sub(r"\s+", "", n.group(1)).lower().replace("nm", " nm")

    # canonical key: part number is the strong key advisories use; marketing is weak
    if out["part"]:
        out["key"] = f"{out['vendor']}:{out['part'].lower()}"
        out["key_kind"] = "part"
    elif out["marketing"]:
        out["key"] = f"{out['vendor']}:{out['marketing'].lower()}"
        out["key_kind"] = "marketing"
    else:
        return None
    return out


def key_of(s: str) -> str | None:
    p = parse(s)
    return p["key"] if p else None


if __name__ == "__main__":
    import sqlite3, sys
    from common import DB_PATH
    con = sqlite3.connect(DB_PATH)
    rows = [r[0] for r in con.execute(
        "SELECT DISTINCT chipset FROM roms WHERE chipset IS NOT NULL AND chipset!=''")]
    rows += [r[0] for r in con.execute(
        'SELECT DISTINCT "Platform — Chipset" FROM devices WHERE "Platform — Chipset" IS NOT NULL')]
    seen, part, mkt, bad = set(), 0, 0, []
    for r in rows:
        if r in seen:
            continue
        seen.add(r)
        p = parse(r)
        if not p:
            bad.append(r); continue
        if p["key_kind"] == "part":
            part += 1
        else:
            mkt += 1
    print(f"{len(seen)} distinct chipset strings")
    print(f"  {part} resolved to a PART number (strong key — advisories use these)")
    print(f"  {mkt} only to a marketing name (weak key)")
    print(f"  {len(bad)} unparseable")
    for b in bad[:8]:
        print(f"     ? {b[:70]}")
    print("\nsamples:")
    for r in list(seen)[:8]:
        p = parse(r)
        if p:
            print(f"  {r[:44]:46} -> {p['key']:28} [{p['key_kind']}]")
    con.close()
