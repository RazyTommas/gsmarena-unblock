"""Flat Xiaomi tracker feed parser, extracted from the existing source adapter."""
import re

def _yaml_rows(text: str) -> list[dict[str, str]]:
    """Parse the flat sequence-of-mappings emitted by Xiaomi Firmware Tracker.

    Keeping this tiny parser local avoids making replay of captured artifacts
    depend on a YAML package. The feed has no nested structures.
    """
    rows: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        match = re.match(r"^(?:- |  )([a-zA-Z0-9_]+):(?:\s*(.*))?$", raw)
        if not match:
            continue
        if raw.startswith("- "):
            if current is not None:
                rows.append(current)
            current = {}
        if current is None:
            continue
        value = (match.group(2) or "").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].replace("''", "'")
        current[match.group(1)] = value
    if current is not None:
        rows.append(current)
    return rows


def _region_from_codename_and_name(codename: str, name: str) -> str:
    # Regional feeds intentionally end in `_global` too (e.g. umi_tr_global).
    # Interpret the explicit market suffix before the broad Global label.
    markets = {'eea':'EEA','in':'IN','id':'ID','ru':'RU','tr':'TR','tw':'TW','jp':'JP'}
    specific = re.search(r'_(eea|in|id|ru|tr|tw|jp)(?:_global)?$', codename.lower())
    by_code = markets[specific.group(1)] if specific else None
    names = {'eea':'EEA','india':'IN','china':'CN','japan':'JP','taiwan':'TW',
             'turkey':'TR','russia':'RU','indonesia':'ID'}
    named = {region for token,region in names.items() if re.search(r'\b'+token+r'\b', name.lower())}
    if len(named)>1 or by_code and named and named!={by_code}:
        return 'SOURCE_UNSPECIFIED'  # Preserve conflicting source labels as unknown.
    if by_code or named:
        return by_code or next(iter(named))
    if codename.lower().endswith('_global') or re.search(r'\bglobal\b',name.lower()):
        return 'GLOBAL'
    return "SOURCE_UNSPECIFIED"
