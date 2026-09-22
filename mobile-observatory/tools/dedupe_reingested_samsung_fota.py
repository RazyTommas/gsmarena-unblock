#!/usr/bin/env python3
"""Collapse the samsung.fota double-ingest (2827a10 reshaped release_time under a
fixed run_id; see dedupe.py's incident note above `dedupe_reingested_observations`).

    PYTHONPATH=src python3 tools/dedupe_reingested_samsung_fota.py --data-dir .observatory-data
    PYTHONPATH=src python3 tools/dedupe_reingested_samsung_fota.py --data-dir .observatory-data --apply

Without --apply this only reports what a run would collapse (via a rolled-back
transaction against the real file -- nothing is written). Safe to run repeatedly:
once collapsed, a further --apply run finds nothing left to do.

IngestionImporter now retires this shape of duplicate automatically as soon as it
reappears in a fresh import (see collectors/importer.py's _retire_superseded), so this
tool is for the corpus that already has the 2026-09 incident's duplicates sitting in
it -- not something batch runs need going forward.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mobile_observatory.dedupe import dedupe_reingested_observations  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=".observatory-data")
    ap.add_argument("--source-id", default="samsung.fota")
    ap.add_argument("--run-id", default="samsung-fota-history-captured")
    ap.add_argument("--apply", action="store_true", help="without this, report only -- nothing is written")
    args = ap.parse_args()

    db_path = Path(args.data_dir) / "corpus.sqlite"
    if not db_path.is_file():
        print(f"no corpus at {db_path}")
        return 1

    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA foreign_keys=ON")
    try:
        # commit=False leaves everything in the open transaction -- rolling back here
        # is a REAL preview, not just a promise, because nothing was ever committed.
        result = dedupe_reingested_observations(con, source_id=args.source_id, run_id=args.run_id,
                                                commit=args.apply)
        if not args.apply:
            con.rollback()
            print("DRY RUN (rolled back, nothing written):")
        else:
            print("APPLIED:")
        for key, value in sorted(result.items()):
            print(f"  {key}: {value}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
