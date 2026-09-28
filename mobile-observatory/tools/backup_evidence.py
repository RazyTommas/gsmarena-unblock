#!/usr/bin/env python3
"""Back up the part of the corpus that cannot be rebuilt.

WHY THIS IS NOT A BACKUP OF corpus.sqlite

corpus.sqlite is 243 MB and almost all of it is derived. The captured inputs
under crawler/relay/results are in git; the code is in git; a batch rebuilds the
database from them in about thirteen minutes. Copying 243 MB nightly to protect
something regenerable is waste.

But a rebuild is not a restore, and the difference was measured, not assumed.
Rebuilding the live corpus from the current inputs produces 759 of the same
devices, 106 that only the live corpus has, and 95 that only the rebuild has --
201 devices resolving differently. Two causes:

  * 1,111 identity conclusions are remembered under RULE_VERSION 1. Remembered
    conclusions are deliberately final, so the corpus does not depend on when it
    last ran -- which makes it depend on the ORDER it ran in. A rebuild applies
    the current rules to everything and reaches different answers.

  * 1,316 observations rest on an input version that is no longer on disk. The
    capture tree is overwritten in place; the corpus accumulates. Six artifact
    digests exist in the live corpus and nowhere in a fresh build.

So the corpus is an archive whose inputs are a moving window, not a cache of
them. Losing it costs those 201 devices and those decisions. That is survivable
and should be a decision made knowingly, not discovered during an incident.

WHAT IS ACTUALLY IRREPLACEABLE

Asked of the corpus rather than hardcoded: every file an `artifacts` row points
at that lives inside the data directory. Those bytes are what every served value
traces back to, and they are gitignored. Measured on the live corpus: 20 files,
20.6 MB -- eleven under evidence/, nine under ledger/raw/ -- against 243 MB for
the database. Plus local.sqlite (0.7 MB), which holds 5,785 acknowledgements and
the watch list: human decisions no ingest reproduces.

Deriving the list from the artifacts table rather than listing directories is
what keeps this correct when a new source writes somewhere new. A backup that
silently stops covering a source is worse than no backup, because it reports
success either way.

    python3 tools/backup_evidence.py --data-dir .observatory-data --output backups/
    python3 tools/backup_evidence.py --verify backups/evidence-2026-09-28.tar.gz
    python3 tools/backup_evidence.py --restore backups/... --data-dir /new/box/data
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

MANIFEST = "manifest.json"
FORMAT = "mobile-observatory-evidence-backup-v1"


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def irreplaceable_files(corpus: Path, data_dir: Path) -> tuple[list[dict], list[str]]:
    """Every artifact file inside `data_dir`, with the hash the corpus expects.

    Files OUTSIDE the data directory are the repository's own captured inputs.
    They are in git, and git is their backup; copying them here would grow the
    archive by 24 MB to duplicate something already versioned.

    Returns (files, problems). A problem is an artifact whose bytes are absent
    or no longer match their recorded sha256 -- reported, never skipped
    silently, because an artifact the corpus cites and cannot produce is
    exactly the thing a backup exists to catch.
    """
    connection = sqlite3.connect(f"file:{corpus}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    data_dir = data_dir.resolve()
    files: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    try:
        rows = connection.execute(
            "SELECT id, source_id, storage_uri, sha256 FROM artifacts").fetchall()
    finally:
        connection.close()

    for row in rows:
        raw = row["storage_uri"] or ""
        if not raw or "://" in raw and not raw.startswith("file://"):
            continue                      # a pseudo-URI, not a path on this box
        path = Path(raw.removeprefix("file://"))
        try:
            resolved = path.resolve()
        except OSError:
            problems.append(f"{row['source_id']}: unreadable path {raw}")
            continue
        if data_dir not in resolved.parents:
            continue                      # lives in the repo; git holds it
        if not resolved.is_file():
            problems.append(f"{row['source_id']}: artifact bytes absent at {resolved}")
            continue
        if str(resolved) in seen:
            continue
        seen.add(str(resolved))
        actual = _digest(resolved)
        if row["sha256"] and actual != row["sha256"]:
            problems.append(
                f"{row['source_id']}: {resolved.name} no longer matches its recorded sha256")
            continue
        files.append({
            "relative": str(resolved.relative_to(data_dir)),
            "sha256": actual,
            "bytes": resolved.stat().st_size,
            "source_id": row["source_id"],
        })
    files.sort(key=lambda entry: entry["relative"])
    return files, problems


def _backup_sqlite(source: Path, target: Path) -> None:
    """Copy a live SQLite database safely.

    Never `cp`. A plain file copy of an open database omits its -wal, and the
    copy is then missing every committed transaction that has not been
    checkpointed -- silently, producing a file that opens fine and is simply
    out of date. sqlite3's backup API takes a consistent snapshot while the
    server keeps serving.
    """
    origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(target)
        try:
            origin.backup(destination)
        finally:
            destination.close()
    finally:
        origin.close()


def build(data_dir: Path, output: Path, *, stamp: str) -> dict:
    corpus = data_dir / "corpus.sqlite"
    if not corpus.is_file():
        raise SystemExit(f"no corpus at {corpus}")
    files, problems = irreplaceable_files(corpus, data_dir)
    if not files:
        raise SystemExit(
            "the corpus cites no artifact files inside the data directory. That is "
            "either an empty corpus or a changed layout; refusing to write a backup "
            "that would restore nothing.")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        staging = Path(scratch)
        local = data_dir / "local.sqlite"
        local_entry = None
        if local.is_file():
            _backup_sqlite(local, staging / "local.sqlite")
            local_entry = {"relative": "local.sqlite",
                           "sha256": _digest(staging / "local.sqlite"),
                           "bytes": (staging / "local.sqlite").stat().st_size}
        else:
            # Half of what this tool exists to protect. A fresh box legitimately
            # has none, so this is not fatal -- but an archive that quietly
            # contains no human decisions while printing the same success output
            # as one that does is how a backup becomes a belief rather than a
            # fact. Found by running the restore drill: the first archive had no
            # local.sqlite and both backup and restore reported success.
            problems.append(
                "local.sqlite is absent from the data directory, so acknowledgements, "
                "watches and collection requests are NOT in this archive")

        manifest = {
            "format": FORMAT,
            "created_at": stamp,
            "data_dir": str(data_dir.resolve()),
            "artifact_files": files,
            "local_database": local_entry,
            "total_bytes": sum(f["bytes"] for f in files) + (local_entry or {}).get("bytes", 0),
            "problems": problems,
            "note": ("This is NOT a backup of corpus.sqlite. The database is derived and "
                     "rebuildable; these bytes are not. Restoring them lets a rebuilt corpus "
                     "cite the same evidence. See the module docstring: a rebuild is not a "
                     "restore -- it resolves some devices differently."),
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        with tarfile.open(output, "w:gz") as archive:
            archive.add(staging / MANIFEST, arcname=MANIFEST)
            if local_entry:
                archive.add(staging / "local.sqlite", arcname="local.sqlite")
            for entry in files:
                archive.add(data_dir / entry["relative"], arcname=f"data/{entry['relative']}")
    return manifest


def verify(archive_path: Path) -> dict:
    """Check every file in a backup against the manifest's own hashes.

    A manifest nobody verifies is a list, not a checksum.
    """
    with tarfile.open(archive_path, "r:gz") as archive:
        manifest = json.loads(archive.extractfile(MANIFEST).read().decode("utf-8"))
        if manifest.get("format") != FORMAT:
            raise SystemExit(f"not a {FORMAT} archive")
        bad: list[str] = []
        expected = [(f"data/{e['relative']}", e["sha256"]) for e in manifest["artifact_files"]]
        if manifest.get("local_database"):
            expected.append(("local.sqlite", manifest["local_database"]["sha256"]))
        for name, want in expected:
            member = archive.extractfile(name)
            if member is None:
                bad.append(f"{name}: absent from the archive")
                continue
            sha = hashlib.sha256()
            for chunk in iter(lambda: member.read(1 << 20), b""):
                sha.update(chunk)
            if sha.hexdigest() != want:
                bad.append(f"{name}: hash differs")
    return {"checked": len(expected), "bad": bad}


def restore(archive_path: Path, data_dir: Path) -> dict:
    """Unpack a backup into `data_dir`, verifying as it goes.

    Existing files are overwritten: the archive's copy is the one whose hash the
    corpus recorded. Restoring is refused outright if anything in the archive
    fails verification, rather than leaving a half-restored directory.
    """
    report = verify(archive_path)
    if report["bad"]:
        raise SystemExit("refusing to restore a damaged backup:\n  " + "\n  ".join(report["bad"]))
    data_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        manifest = json.loads(archive.extractfile(MANIFEST).read().decode("utf-8"))
        for entry in manifest["artifact_files"]:
            target = data_dir / entry["relative"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(f"data/{entry['relative']}").read())
            written += 1
        local_restored = bool(manifest.get("local_database"))
        if local_restored:
            (data_dir / "local.sqlite").write_bytes(archive.extractfile("local.sqlite").read())
    # Report what was NOT restored as prominently as what was. "restored: 20"
    # read as complete success while the human-decision database was missing
    # from the archive entirely.
    return {
        "artifact_files_restored": written,
        "local_database_restored": local_restored,
        "local_database_note": None if local_restored else
            "this archive contained no local.sqlite: acknowledgements, watches and "
            "collection requests were NOT restored",
        "data_dir": str(data_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path(".observatory-data"))
    parser.add_argument("--output", type=Path, help="archive to write")
    parser.add_argument("--verify", type=Path, help="check an existing archive and exit")
    parser.add_argument("--restore", type=Path, help="restore an archive into --data-dir")
    parser.add_argument("--stamp", default=None,
                        help="timestamp recorded in the manifest (default: now, UTC)")
    args = parser.parse_args()

    if args.verify:
        report = verify(args.verify)
        print(json.dumps(report, indent=2))
        raise SystemExit(1 if report["bad"] else 0)

    if args.restore:
        print(json.dumps(restore(args.restore, args.data_dir), indent=2))
        return

    if not args.output:
        parser.error("one of --output, --verify or --restore is required")
    stamp = args.stamp
    if stamp is None:
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).isoformat()
    manifest = build(args.data_dir, args.output, stamp=stamp)
    summary = {k: v for k, v in manifest.items() if k != "artifact_files"}
    summary["artifact_file_count"] = len(manifest["artifact_files"])
    print(json.dumps(summary, indent=2))
    if manifest["problems"]:
        # Worth its own exit code: the archive was written and is valid, but it
        # does not cover everything it was asked to. A scheduler that treats
        # exit 0 as "backed up" would otherwise be wrong without being told.
        print(f"\n{len(manifest['problems'])} gap(s) in this backup:", file=sys.stderr)
        for problem in manifest["problems"]:
            print(f"  - {problem}", file=sys.stderr)
        print("\nThe archive is written and verifies; it is incomplete, not corrupt.",
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
