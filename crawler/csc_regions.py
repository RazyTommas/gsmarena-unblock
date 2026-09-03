"""
csc_regions.py — Samsung CSC (Country-Specific Code) → country/zone map.

Purpose: let the crawler and the explorer app filter/sort firmware by a real
geographic ZONE (e.g. "the Levant", "Lebanon", "Israel", "Iraq") instead of by
the opaque 3-letter CSC codes GSMArena/samfw expose.

A CSC is Samsung's per-market firmware channel. One phone model has many builds,
one (or more) per CSC. To answer "firmware for Iraq" you filter roms.region to
the CSC codes that map to Iraq.

Provenance / confidence — codes are VERIFIED against two public CSC lists
(github.com/zerotet/samsung-csc-codes and androidsage's 600+ list, fetched
2026-09-03). Each entry carries a confidence tier:
  "verified"  — same code→country in ≥1 authoritative list, unambiguous
  "tentative" — widely cited but not confirmed in the lists we read, OR the
                lists disagreed. Treat as a CANDIDATE to confirm against the
                model's actual samfw page during the crawl — do NOT present it
                to a user as fact without that confirmation.
Never invent a CSC. If a country has no confirmed code here, it stays absent
rather than guessed — the crawl is what proves a CSC exists for a given model.
"""

# zone -> { country -> {"codes": [...], "confidence": "verified"|"tentative"} }
ZONES = {
    "Levant": {
        "Iraq":      {"codes": ["MID"],                     "confidence": "verified"},
        "Lebanon":   {"codes": ["LEB", "DAM"],              "confidence": "verified"},
        "Israel":    {"codes": ["ILO", "PTR", "CEL",
                                 "MIR", "PCL"],             "confidence": "verified"},
        "Jordan":    {"codes": ["LEV"],                     "confidence": "verified"},
        "Syria":     {"codes": [],                          "confidence": "tentative"},
        "Palestine": {"codes": [],                          "confidence": "tentative"},
    },
    "Gulf": {
        "Saudi Arabia":        {"codes": ["KSA", "JED"],    "confidence": "verified"},
        "United Arab Emirates":{"codes": ["XSG", "ARB"],    "confidence": "tentative"},
        "Kuwait":              {"codes": [],                "confidence": "tentative"},
        "Qatar":               {"codes": ["QAT"],           "confidence": "tentative"},
        "Bahrain":             {"codes": [],                "confidence": "tentative"},
        "Oman":                {"codes": [],                "confidence": "tentative"},
        "Yemen":               {"codes": [],                "confidence": "tentative"},
    },
    "Wider Middle East": {
        "Egypt":  {"codes": ["EGY"],  "confidence": "verified"},
        "Iran":   {"codes": ["THR"],  "confidence": "verified"},
        "Turkey": {"codes": ["TUR"],  "confidence": "verified"},
    },
}

# A regional multi-country bucket seen in samfw data — not a single country.
REGIONAL_BUCKETS = {
    "ARB": "Arab / generic Middle East multi-CSC build",
    "MID": "Middle East (primary Iraq/Levant channel)",
}


def _flatten():
    """Yield (zone, country, code, confidence) for every mapped code."""
    for zone, countries in ZONES.items():
        for country, info in countries.items():
            for code in info["codes"]:
                yield zone, country, code, info["confidence"]


# Reverse index: CSC code -> (country, zone, confidence)
CODE_TO_COUNTRY = {
    code: (country, zone, conf) for zone, country, code, conf in _flatten()
}


def codes_for(name: str) -> list[str]:
    """
    Resolve a country OR zone name to its CSC code list.

    >>> codes_for("Iraq")
    ['MID']
    >>> sorted(codes_for("Levant"))
    ['CEL', 'DAM', 'ILO', 'LEB', 'LEV', 'MID', 'MIR', 'PCL', 'PTR']
    """
    key = name.strip().lower()
    # zone match
    for zone, countries in ZONES.items():
        if zone.lower() == key:
            out = []
            for info in countries.values():
                out.extend(info["codes"])
            return sorted(set(out))
    # country match
    for zone, countries in ZONES.items():
        for country, info in countries.items():
            if country.lower() == key:
                return list(info["codes"])
    return []


def country_for(code: str) -> str | None:
    """CSC code -> country name (None if unmapped)."""
    hit = CODE_TO_COUNTRY.get(code.upper())
    return hit[0] if hit else None


def all_zone_codes() -> set[str]:
    """Every CSC code across all zones — the full Middle East filter set."""
    return {code for _, _, code, _ in _flatten()}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
        codes = codes_for(q)
        if codes:
            print(f"{q} -> CSC codes: {', '.join(codes)}")
            for c in codes:
                country, zone, conf = CODE_TO_COUNTRY[c]
                flag = "" if conf == "verified" else "  (tentative — confirm on samfw)"
                print(f"    {c:5} {country} / {zone}{flag}")
        else:
            print(f"No CSC codes mapped for '{q}'. Known zones: {', '.join(ZONES)}")
    else:
        print("Middle East CSC zone map\n")
        for zone, countries in ZONES.items():
            print(f"[{zone}]")
            for country, info in countries.items():
                codes = ", ".join(info["codes"]) or "(no confirmed code yet)"
                tag = "" if info["confidence"] == "verified" else "  ~tentative"
                print(f"  {country:22} {codes}{tag}")
            print()
