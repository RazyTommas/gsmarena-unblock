from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BUNDLE_FORMAT_VERSION = "1"


class SnapshotVerificationError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_counts(path: Path) -> dict[str, int]:
    uri = f"file:{path.resolve()}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    try:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in tables}
    finally:
        db.close()


def _schema_version(path: Path) -> str:
    db = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        exists = db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_migrations'").fetchone()
        if not exists:
            return "unversioned"
        row = db.execute("SELECT max(version) FROM schema_migrations").fetchone()
        return str(row[0]) if row and row[0] is not None else "0"
    finally:
        db.close()


def _create_local_database(path: Path, snapshot_id: str) -> None:
    db = sqlite3.connect(path)
    try:
        db.executescript(
            """
            PRAGMA journal_mode=DELETE;
            CREATE TABLE local_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
            CREATE TABLE preferences(key TEXT PRIMARY KEY, value_json TEXT NOT NULL CHECK(json_valid(value_json)), updated_at TEXT NOT NULL) STRICT;
            CREATE TABLE saved_searches(id TEXT PRIMARY KEY, name TEXT NOT NULL, query_json TEXT NOT NULL CHECK(json_valid(query_json)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;
            CREATE TABLE watch_definitions(id TEXT PRIMARY KEY, name TEXT NOT NULL, query_json TEXT NOT NULL CHECK(json_valid(query_json)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;
            CREATE TABLE seen_events(event_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL) STRICT;
            """
        )
        db.execute("INSERT INTO local_metadata VALUES('local_schema_version','1')")
        db.execute("INSERT INTO local_metadata VALUES('created_for_snapshot',?)", (snapshot_id,))
        db.commit()
    finally:
        db.close()


class SnapshotBuilder:
    def __init__(self, corpus: Path, web_assets: Path, api_version: str = "v1"):
        self.corpus = corpus
        self.web_assets = web_assets
        self.api_version = api_version

    def build(self, output: Path, snapshot_at: str | None = None) -> Path:
        if output.exists():
            raise FileExistsError(f"snapshot output already exists: {output}")
        if not self.corpus.is_file() or not self.web_assets.is_dir():
            raise FileNotFoundError("corpus database and web asset directory are required")
        snapshot_at = snapshot_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        snapshot_id = hashlib.sha256(f"{snapshot_at}\0{_sha256(self.corpus)}\0{self.api_version}".encode()).hexdigest()[:20]
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        try:
            corpus_out = temporary / "corpus.sqlite"
            source = sqlite3.connect(f"file:{self.corpus.resolve()}?mode=ro", uri=True)
            destination = sqlite3.connect(corpus_out)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            local_out = temporary / "local.sqlite"
            _create_local_database(local_out, snapshot_id)
            shutil.copytree(self.web_assets, temporary / "web")
            files: dict[str, dict[str, Any]] = {}
            for path in sorted(p for p in temporary.rglob("*") if p.is_file()):
                relative = path.relative_to(temporary).as_posix()
                files[relative] = {"sha256": _sha256(path), "bytes": path.stat().st_size}
            manifest = {
                "bundle_format_version": BUNDLE_FORMAT_VERSION,
                "snapshot_id": snapshot_id,
                "snapshot_at": snapshot_at,
                "schema_version": _schema_version(corpus_out),
                "api_version": self.api_version,
                "corpus": {"path": "corpus.sqlite", "immutable": True, "row_counts": _table_counts(corpus_out)},
                "local": {"path": "local.sqlite", "preserve_on_upgrade": True, "schema_version": "1"},
                "web_root": "web",
                "files": files,
            }
            (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            verify_bundle(temporary)
            corpus_out.chmod(0o444)
            os.replace(temporary, output)
            return output
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def verify_bundle(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise SnapshotVerificationError("manifest.json is missing")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("bundle_format_version") != BUNDLE_FORMAT_VERSION:
        raise SnapshotVerificationError("unsupported bundle format")
    for relative, expected in manifest.get("files", {}).items():
        path = root / relative
        if not path.is_file() or _sha256(path) != expected["sha256"] or path.stat().st_size != expected["bytes"]:
            raise SnapshotVerificationError(f"file verification failed: {relative}")
    corpus = root / manifest["corpus"]["path"]
    local = root / manifest["local"]["path"]
    for label, database in (("corpus", corpus), ("local", local)):
        db = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise SnapshotVerificationError(f"{label} database failed integrity check")
        finally:
            db.close()
    if _table_counts(corpus) != manifest["corpus"]["row_counts"]:
        raise SnapshotVerificationError("corpus row counts do not match manifest")
    if not (root / manifest["web_root"] / "index.html").is_file():
        raise SnapshotVerificationError("web/index.html is missing")
    return manifest
