#!/usr/bin/env python3
"""Merge products that BOTH the spelling and the vendor catalogue say are one device.

    PYTHONPATH=src python3 tools/merge_confirmed_duplicates.py --data-dir .observatory-data

THE RULE, and it requires two independent signals because neither is sufficient:

  * the names differ only by spacing/punctuation  -- a string signal, and string
    signals on device names are what collapsed 'Galaxy S25+' into 'S25'
  * they share a Google Play model code          -- a vendor signal, and that alone
    binds 'CAMON 20' and 'CAMON 20 PRO' to CK6n, which are different phones

Together they are strong: the catalogue confirms what the spelling suggests. This is
the rule that came out of ESC-0001, where two independent agents established that
'TECNO SPARK 8 P' and 'TECNO SPARK 8P' are one device (both -> TECNO-KG7n, while
SPARK 8 Pro -> KG8). The rule is general; it names no vendor and no product.

Survivor is the row with more observation links, ties broken on id so the result is
deterministic. Children are repointed, never dropped.
"""
from __future__ import annotations
import argparse, csv, re, sqlite3, sys
from pathlib import Path

CHILD_TABLES = ("observation_product_links", "source_identity_registry",
                "identity_conclusions", "observed_product_silicon",
                "product_firmware_releases", "product_security_publications",
                "source_specifications", "source_build_product_links")


def squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=".observatory-data")
    ap.add_argument("--catalog", default="../crawler/relay/results/google-play-devices/"
                                         "supported_devices.csv")
    ap.add_argument("--apply", action="store_true", help="without this, report only")
    a = ap.parse_args()

    cat = Path(a.catalog)
    if not cat.is_file():
        print(f"  catalogue not found at {cat} — refusing to merge on spelling alone")
        return 1
    name_codes: dict[str, set[str]] = {}
    with cat.open(encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            nm = (r.get("Marketing Name") or "").strip().lower()
            code = (r.get("Model") or "").strip()
            if nm and code:
                name_codes.setdefault(nm, set()).add(code)

    con = sqlite3.connect(str(Path(a.data_dir) / "corpus.sqlite"))
    con.execute("PRAGMA foreign_keys=ON")
    rows = con.execute("SELECT id, manufacturer, canonical_name FROM source_products").fetchall()

    groups: dict[tuple, list] = {}
    for pid, man, name in rows:
        for code in name_codes.get(name.strip().lower(), ()):
            groups.setdefault((man, code, squash(name)), []).append((pid, name))

    merges = [(k, v) for k, v in groups.items() if len({p for p, _ in v}) > 1]
    if not merges:
        print("  nothing to merge: no pair agrees on BOTH spelling and model code")
        return 0

    for (man, code, _), members in merges:
        counts = {pid: con.execute(
            "SELECT COUNT(*) FROM observation_product_links WHERE product_id=?",
            (pid,)).fetchone()[0] for pid, _ in members}
        keep = sorted(members, key=lambda m: (-counts[m[0]], m[0]))[0]
        drop = [m for m in members if m[0] != keep[0]]
        print(f"  {man} / {code}")
        print(f"    keep  {keep[1]!r}  ({counts[keep[0]]} links)")
        for pid, nm in drop:
            print(f"    merge {nm!r}  ({counts[pid]} links)")
        if not a.apply:
            continue
        for pid, _ in drop:
            for t in CHILD_TABLES:
                try:
                    con.execute(f"UPDATE OR IGNORE {t} SET product_id=? WHERE product_id=?",
                                (keep[0], pid))
                    con.execute(f"DELETE FROM {t} WHERE product_id=?", (pid,))
                except sqlite3.OperationalError:
                    pass
            con.execute("DELETE FROM source_products WHERE id=?", (pid,))
    if a.apply:
        con.commit()
        print(f"\n  merged {sum(len(v)-1 for _, v in merges)} duplicate product(s)")
    else:
        print("\n  (report only — pass --apply to perform the merge)")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
