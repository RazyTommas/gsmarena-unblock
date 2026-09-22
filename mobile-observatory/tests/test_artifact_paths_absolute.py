"""Artifact storage_uri must be absolute, whatever cwd the ingest ran from.

package_portable verifies every artifact's bytes against its recorded sha256
before building a bundle. A relative storage_uri only resolves from the directory
the ingest happened to run in, so the packager reports the evidence as MISSING and
refuses -- which is exactly what it did on 2026-09-22 for four artifacts whose
bytes were present and whose hashes matched perfectly.
"""
import sqlite3
import unittest
from pathlib import Path


class ArtifactPathsAreAbsolute(unittest.TestCase):
    def test_no_relative_storage_uri_in_the_live_corpus(self):
        db = Path(__file__).resolve().parents[1] / ".observatory-data" / "corpus.sqlite"
        if not db.is_file():
            self.skipTest("no local corpus in this checkout")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        bad = [r[0] for r in con.execute(
            "SELECT storage_uri FROM artifacts WHERE storage_uri NOT LIKE '/%'")]
        self.assertEqual(bad, [], f"relative artifact storage_uri found: {bad}")

    def test_importer_resolves_the_path_it_records(self):
        src = (Path(__file__).resolve().parents[1] / "src" / "mobile_observatory"
               / "collectors" / "importer.py").read_text()
        self.assertIn("str(binary_path.resolve())", src,
                      "importer must record an absolute storage_uri")
        self.assertNotIn("str(binary_path),", src,
                         "a non-resolved binary_path is still being recorded")


if __name__ == "__main__":
    unittest.main()
