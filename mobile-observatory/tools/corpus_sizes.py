#!/usr/bin/env python3
"""Report where the corpus's bytes are, from dbstat -- measured, not estimated.

    python3 tools/corpus_sizes.py --data-dir .observatory-data
    python3 tools/corpus_sizes.py --data-dir .observatory-data --json

Reads the database. Writes nothing, and opens it read-only so it cannot.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mobile_observatory import storage  # noqa: E402


def _mb(value: int) -> str:
    return f"{value / 1048576:9.3f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=".observatory-data")
    parser.add_argument("--database", default="corpus.sqlite",
                        help="which file in the data dir to measure (default corpus.sqlite)")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    path = data_dir / args.database
    if not path.is_file():
        parser.error(f"no database at {path}")

    # Read-only URI, not a plain connect: a tool whose job is to look must not be
    # able to be the thing that changed the file.
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    measurement = storage.measure(connection)
    directories = storage.directory_sizes(data_dir)

    if args.json:
        print(json.dumps({"database": str(path), "storage": measurement,
                          "directory": directories}, indent=2, sort_keys=True))
        return 0 if measurement.get("available") else 1

    if not measurement.get("available"):
        print(f"dbstat unavailable: {measurement['reason']}")
        return 1

    print(f"{path}")
    print(f"  file            {_mb(measurement['file_bytes'])}"
          f"   ({measurement['page_count']} pages of {measurement['page_size']} B)")
    print(f"  tables          {_mb(measurement['table_bytes'])}")
    print(f"  indexes         {_mb(measurement['index_bytes'])}"
          f"   ({measurement['index_bytes'] / measurement['file_bytes']:.1%} of the file)")
    if measurement["internal_bytes"]:
        print(f"  sqlite internal {_mb(measurement['internal_bytes'])}")
    print(f"  VACUUM reclaims {_mb(measurement['reclaimable_bytes'])}"
          f"   ({measurement['reclaimable_bytes'] / measurement['file_bytes']:.2%}"
          f", {measurement['freelist_pages']} free pages)")
    residual = measurement["unaccounted_bytes"]
    verdict = "complete" if abs(residual) < measurement["page_size"] else "INCOMPLETE ACCOUNT"
    print(f"  unaccounted     {_mb(residual)}   ({verdict})")

    objects = measurement["objects"]
    print(f"\ntop {args.top} objects by bytes (of {len(objects)}):")
    print(f"  {'bytes':>12}  {'pages':>7}  {'fill':>5}  kind    name")
    for item in storage.largest(measurement, args.top):
        print(f"  {_mb(item['bytes'])}  {item['pages']:7d}  {item['fill_ratio']:5.2f}"
              f"  {item['kind']:7s} {item['name']}")
    print(f"  ({len(objects) - min(args.top, len(objects))} more not shown)")

    rolled = storage.by_table(measurement, args.top)
    print(f"\ntop {args.top} tables by bytes, rows plus their indexes:")
    print(f"  {'total':>12}  {'rows':>12}  {'indexes':>12}  n  table")
    for item in rolled:
        print(f"  {_mb(item['bytes'])}  {_mb(item['table_bytes'])}  {_mb(item['index_bytes'])}"
              f"  {item['indexes']:2d} {item['table']}")

    if directories:
        print(f"\n{data_dir} on disk (dbstat cannot see any of this):")
        for item in directories:
            if item["bytes"] < 1024 and item["depth"] > 1:
                continue
            print(f"  {_mb(item['bytes'])}  {item['files']:6d} files  "
                  f"{'  ' * (item['depth'] - 1)}{item['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
