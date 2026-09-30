#!/usr/bin/env python3
"""List, inspect and REVERT the changesets the batch records.

    python3 tools/corpus_changeset.py list   --data-dir .observatory-data
    python3 tools/corpus_changeset.py show   --data-dir .observatory-data <name>
    python3 tools/corpus_changeset.py revert --data-dir .observatory-data <name>

`revert` applies the inverse of a recorded batch. It refuses unless it can apply
cleanly and in full: a conflict rolls the whole thing back and changes nothing,
because half a rollback leaves a state no run ever produced.

WHAT THIS IS NOT
  - Not a backup. `tools/backup_evidence.py` is what survives losing the
    directory; a changeset only undoes a write against the corpus it was
    recorded on. See docs/CHANGESETS.md.
  - Not a schema rollback. A changeset holds rows, not DDL. `show` prints
    `schema_changed` and `revert` refuses when it is set unless --schema-moved is
    passed, because a row-level undo across a migration is a partial undo.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mobile_observatory import changesets  # noqa: E402
from mobile_observatory.database import Database  # noqa: E402


def _mb(value: int) -> str:
    return f"{value / 1048576:.3f} MB"


def _store(args) -> changesets.ChangesetStore:
    return changesets.ChangesetStore(Path(args.data_dir) / "changesets")


def do_list(args) -> int:
    entries = _store(args).entries()
    if not entries:
        print(f"no changesets recorded in {Path(args.data_dir) / 'changesets'}")
        return 0
    print(f"{'bytes':>12}  {'tables':>6}  schema  name")
    for entry in entries:
        print(f"{entry['bytes']:12d}  {entry.get('tables_changed', '?'):>6}  "
              f"{'MOVED' if entry.get('schema_changed') else '  -  '}   "
              f"{Path(entry['path']).name}")
    total = sum(entry["bytes"] for entry in entries)
    print(f"\n{len(entries)} changeset(s), {_mb(total)} total")
    return 0


def do_show(args) -> int:
    blob = _store(args).read(args.name)
    summary = changesets.summarise(blob)
    meta_path = (Path(args.data_dir) / "changesets" / args.name).with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    print(f"{args.name}: {len(blob)} bytes ({_mb(len(blob))})")
    print(f"  recorded_at     {meta.get('recorded_at', '?')}")
    print(f"  schema_changed  {meta.get('schema_changed', '?')}")
    if meta.get("tables_untracked"):
        print(f"  NOT recorded    {', '.join(meta['tables_untracked'])} "
              f"(no primary key; a revert leaves these untouched)")
    print(f"  tables changed  {len(summary)}")
    print(f"\n  {'insert':>8} {'update':>8} {'delete':>8}  table")
    for name, counts in sorted(summary.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {counts['insert']:8d} {counts['update']:8d} {counts['delete']:8d}  {name}")
    return 0


def do_revert(args) -> int:
    support = changesets.session_support()
    if not support.available:
        print(f"cannot revert: {support.reason}")
        return 1
    store = _store(args)
    blob = store.read(args.name)
    meta_path = (Path(args.data_dir) / "changesets" / args.name).with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    if meta.get("schema_changed") and not args.schema_moved:
        print(f"refusing: {args.name} also changed the schema, and a changeset holds rows only.\n"
              f"Reverting its rows across a migration is a PARTIAL undo. Pass --schema-moved if "
              f"that is what you want.")
        return 2
    if meta.get("tables_untracked"):
        print(f"note: {', '.join(meta['tables_untracked'])} has no primary key and was never "
              f"recorded; this revert leaves it as it is.")

    database = Database(Path(args.data_dir) / "corpus.sqlite")
    inverse = changesets.invert(blob)
    before = changesets.table_digests(database.connection)
    try:
        changesets.apply_changeset(database.connection, inverse)
    except changesets.ChangesetConflict as error:
        print(f"refused, and NOTHING was changed: {error}")
        return 3
    finally:
        after = changesets.table_digests(database.connection)
        database.close()
    changed = sorted(name for name in before if before.get(name) != after.get(name))
    print(f"reverted {args.name} ({len(blob)} bytes). Tables whose content changed: "
          f"{', '.join(changed) if changed else 'none'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=".observatory-data")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("name")
    revert = sub.add_parser("revert")
    revert.add_argument("name")
    revert.add_argument("--schema-moved", action="store_true",
                        help="revert the rows even though the run also changed the schema")
    args = parser.parse_args()
    return {"list": do_list, "show": do_show, "revert": do_revert}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
