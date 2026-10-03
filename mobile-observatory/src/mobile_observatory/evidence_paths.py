"""Where the corpus thinks its evidence is, after the bytes have moved.

`artifacts.storage_uri` is an ABSOLUTE path. That is fine while the data
directory stays where it was written and wrong the moment it does not -- which
is precisely the case a backup exists for.

MEASURED, on the documented restore path, before this existed:

    backup_evidence.restore(archive, <a different directory>)
    -> artifact_files_restored: 1
    -> artifacts in corpus: 2, absolute URIs NOT under the restored data dir: 1
       art-0 -> /tmp/.../data/evidence/artifacts/a.bin   (does not exist)

The bytes arrived. The corpus went on citing the path they came from. In the
rehearsal that found it, the restore "worked" only because the original
directory still existed on the same box -- so the one disaster the backup is
for, a different box, is the one case it did not survive.

ONE RULE, TWO CALLERS, which is the point of this module. `run.py` already
solved this for the portable bundle (`rebase_evidence` +
`provenance-paths.json`) and `tools/backup_evidence.py` did not. A rule that
lives in one path and not the other is this codebase's recurring failure shape
-- see HANDOFF.md, "fix the pattern, not the instance", where one bug lived at
five call sites and was fixed three times.

KEYED BY DIGEST, not by artifact id or by the old path. The digest is the only
thing that survives a relocation: the path is what moved, and the id is not in
every caller's hand (a backup manifest records `relative` and `sha256`, the
portable bundle records an id). It is also what the retention work already
found to be the durable half -- verified on a copy where 0 of 21 stored absolute
paths resolved and all 28 raw files were still identified by digest.

IT REPORTS WHAT IT COULD NOT DO. `unresolved` names every artifact row still
pointing at a path that does not exist after the rebase. A partial relocation
that reported only its successes would read exactly like a complete one, which
is the shape of the backup bug this module was written for.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _as_path(raw: str) -> Path | None:
    """A storage_uri as a filesystem path, or None when it is not one.

    `fixture:...` and other pseudo-URIs are not paths on this box and must not
    be reported as broken ones -- the demonstration seed ships exactly such a
    row, and counting it as unresolved would make every fresh corpus look
    damaged.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if "://" in value and not value.startswith("file://"):
        return None
    if ":" in value.split("/")[0] and not value.startswith(("/", ".", "file://")):
        return None
    return Path(value.removeprefix("file://"))


def rebase(connection, data_dir, entries) -> dict:
    """Repoint `artifacts.storage_uri` at bytes that now live under `data_dir`.

    `entries` is an iterable of `(relative_path, expected_sha256_or_None)` --
    the shape both callers already have. For each one the file must exist under
    `data_dir`, must be inside it (a `..` in a manifest is somebody else's
    archive, not ours), and its digest must match what the caller claims; then
    every artifact row carrying that digest is pointed at it.

    Nothing is deleted and nothing outside `data_dir` is touched. A row already
    pointing at the right absolute path is counted as `already_correct` rather
    than rewritten, so a no-op restore writes nothing.
    """
    root = Path(data_dir).resolve()
    report = {"rebased": 0, "already_correct": 0, "missing": [], "mismatched": [],
              "unresolved": [], "unresolved_count": 0}
    for relative, expected in entries:
        target = (root / relative).resolve()
        if root != target and root not in target.parents:
            report["mismatched"].append(
                f"{relative}: resolves outside the data directory ({target})")
            continue
        if not target.is_file():
            report["missing"].append(f"{relative}: not present under {root}")
            continue
        actual = _digest(target)
        if expected and actual != expected:
            report["mismatched"].append(
                f"{relative}: sha256 {actual[:12]} does not match the expected "
                f"{str(expected)[:12]}; leaving the corpus pointing where it was")
            continue
        rows = connection.execute(
            "SELECT id, storage_uri FROM artifacts WHERE sha256=?", (actual,)).fetchall()
        for row in rows:
            identifier = row[0] if not hasattr(row, "keys") else row["id"]
            stored = row[1] if not hasattr(row, "keys") else row["storage_uri"]
            if stored == str(target):
                report["already_correct"] += 1
                continue
            connection.execute("UPDATE artifacts SET storage_uri=? WHERE id=?",
                               (str(target), identifier))
            report["rebased"] += 1
    connection.commit()

    # What is STILL broken. Reported after the rebase, over every row, because
    # the entries the caller supplied are what it knew about and the question an
    # operator has is about the corpus.
    for row in connection.execute("SELECT id, storage_uri FROM artifacts").fetchall():
        identifier = row[0] if not hasattr(row, "keys") else row["id"]
        stored = row[1] if not hasattr(row, "keys") else row["storage_uri"]
        path = _as_path(stored)
        if path is None or not path.is_absolute():
            continue
        if not path.is_file():
            report["unresolved"].append(f"{identifier} -> {stored}")
    report["unresolved_count"] = len(report["unresolved"])
    # Named, but bounded: a corpus with 20,000 broken rows must not put 20,000
    # lines into a restore report. The COUNT is always exact.
    report["unresolved"] = report["unresolved"][:20]
    return report


def entries_from_backup_manifest(manifest: dict):
    """`(relative, sha256)` for every artifact file a backup archive carries."""
    return [(entry["relative"], entry.get("sha256"))
            for entry in (manifest.get("artifact_files") or [])]


def entries_from_provenance(records, connection):
    """`(relative, sha256)` for a portable bundle's provenance-paths.json.

    That file records an artifact id and a relative path; the digest comes from
    the row, so the one rule above can be keyed the same way for both callers.
    """
    entries = []
    for record in records or []:
        row = connection.execute("SELECT sha256 FROM artifacts WHERE id=?",
                                 (record.get("artifact_id"),)).fetchone()
        digest = None if row is None else (row[0] if not hasattr(row, "keys") else row["sha256"])
        entries.append((record["portable_storage_uri"], digest))
    return entries
