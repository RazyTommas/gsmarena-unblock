from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

"""Merge products that BOTH the spelling and the vendor catalogue call one device.

This runs INSIDE run_batch, not as a one-off tool, and the reason is a mistake worth
recording. The duplicate was merged by hand, the batch was re-run, and the duplicate
came straight back: the TECNO source emits both spellings every time, so a data fix
the pipeline undoes is not a fix at all. Same shape as an enrichment with no restore
line -- if an ingest can destroy it, the ingest has to rebuild it.

THE RULE requires two independent signals, because neither is sufficient:
  * names differing only by spacing/punctuation -- a string signal, and string signals
    on device names are what collapsed 'Galaxy S25+' into 'S25'
  * a shared Google Play model code -- a vendor signal, and that alone binds
    'CAMON 20' and 'CAMON 20 PRO' to CK6n, which are different phones
Together they are strong: the catalogue confirms what the spelling suggests. Derived
from ESC-0001, where two independent agents established SPARK 8 P and SPARK 8P are one
device. The rule names no vendor and no product.
"""

CHILD_TABLES = ("observation_product_links", "source_identity_registry",
                "identity_conclusions", "observed_product_silicon",
                "product_firmware_releases", "product_security_publications",
                "source_specifications", "source_build_product_links",
                "product_hardware_links")


def squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())




def merge_confirmed_duplicates(connection: sqlite3.Connection, catalog: Path) -> dict:
    """Returns counts; safe to call on every batch run."""
    if not Path(catalog).is_file():
        return {"merged": 0, "reason": "catalogue absent; refusing to merge on spelling alone"}
    name_codes: dict[str, set[str]] = {}
    with Path(catalog).open(encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            nm = (r.get("Marketing Name") or "").strip().lower()
            code = (r.get("Model") or "").strip()
            if nm and code:
                name_codes.setdefault(nm, set()).add(code)
    rows = connection.execute(
        "SELECT id, manufacturer, canonical_name FROM source_products").fetchall()
    groups: dict[tuple, list] = {}
    for pid, man, name in rows:
        for code in name_codes.get(name.strip().lower(), ()):
            groups.setdefault((man, code, squash(name)), []).append((pid, name))
    merged = 0
    for _, members in groups.items():
        if len({p for p, _ in members}) < 2:
            continue
        counts = {pid: connection.execute(
            "SELECT COUNT(*) FROM observation_product_links WHERE product_id=?",
            (pid,)).fetchone()[0] for pid, _ in members}
        keep = sorted(members, key=lambda m: (-counts[m[0]], m[0]))[0]
        for pid, _ in (m for m in members if m[0] != keep[0]):
            for t in CHILD_TABLES:
                try:
                    connection.execute(
                        f"UPDATE OR IGNORE {t} SET product_id=? WHERE product_id=?",
                        (keep[0], pid))
                    connection.execute(f"DELETE FROM {t} WHERE product_id=?", (pid,))
                except sqlite3.OperationalError:
                    pass
            connection.execute("DELETE FROM source_products WHERE id=?", (pid,))
            merged += 1
    connection.commit()
    return {"merged": merged}
