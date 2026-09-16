from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mobile_observatory.snapshots import SnapshotBuilder, SnapshotVerificationError, verify_bundle


class SnapshotTest(unittest.TestCase):
    def _corpus(self, root: Path) -> Path:
        path = root / "source.sqlite"
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT); INSERT INTO schema_migrations VALUES(1,'core','now'); CREATE TABLE devices(id TEXT PRIMARY KEY); INSERT INTO devices VALUES('one');")
        db.close()
        return path

    def test_builds_and_verifies_atomic_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            web = root / "assets"
            web.mkdir()
            (web / "index.html").write_text("offline")
            output = root / "bundle"
            SnapshotBuilder(self._corpus(root), web, "v1").build(output, "2026-09-16T12:00:00Z")
            manifest = verify_bundle(output)
            self.assertEqual(manifest["schema_version"], "1")
            self.assertEqual(manifest["corpus"]["row_counts"]["devices"], 1)
            self.assertFalse(bool((output / "corpus.sqlite").stat().st_mode & 0o222))
            local = sqlite3.connect(output / "local.sqlite")
            self.assertEqual(local.execute("SELECT value FROM local_metadata WHERE key='created_for_snapshot'").fetchone()[0], manifest["snapshot_id"])
            local.close()

    def test_tampering_is_detected_and_existing_output_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            web = root / "assets"
            web.mkdir()
            (web / "index.html").write_text("offline")
            output = root / "bundle"
            builder = SnapshotBuilder(self._corpus(root), web)
            builder.build(output, "2026-09-16T12:00:00Z")
            (output / "web" / "index.html").write_text("tampered")
            with self.assertRaises(SnapshotVerificationError):
                verify_bundle(output)
            with self.assertRaises(FileExistsError):
                builder.build(output)


if __name__ == "__main__":
    unittest.main()
