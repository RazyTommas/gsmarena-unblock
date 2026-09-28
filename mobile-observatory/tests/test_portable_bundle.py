"""The deployment path has to work, and nothing checked that it did.

tools/package_portable.py is the only way in this repository to get a corpus
onto the offline box. It read <repo>/crawler/data/devices.db unconditionally --
a path that does not exist here -- so it raised on the first run, every run. The
consequence was not a broken tool, it was that there was no supported way to
ship at all: the update script someone wrote on another machine was the only
working path and was never committed, so losing it lost everything.

These tests package a corpus and then use the result: unpack it into a
directory that has never seen this repository, check every file against the
manifest's own hashes, and open the corpus the way the server does. A bundle
that cannot be opened is not a bundle, and the only way to know is to open it.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402


class PortableBundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        (self.data / "ledger").mkdir(parents=True)
        db = Database.migrated(self.data / "corpus.sqlite")
        seed_demonstration(db, ROOT / "fixtures" / "supported_catalog.sample.json")
        # The packager verifies that every artifact's storage_uri is a real file
        # whose sha256 still matches, and refuses otherwise -- correctly: a
        # bundle whose evidence is absent is not verifiable. The demonstration
        # seed records a pseudo-URI ("fixture:...") rather than a path, so the
        # artifact is repointed at a real file here. That is what a captured
        # corpus looks like, and it is the only shape the packager can ship.
        import hashlib

        evidence = self.data / "evidence" / "artifacts"
        evidence.mkdir(parents=True, exist_ok=True)
        payload = (ROOT / "fixtures" / "supported_catalog.sample.json").read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        stored = evidence / f"{digest}.bin"
        stored.write_bytes(payload)
        db.connection.execute("UPDATE artifacts SET storage_uri=?, sha256=?, byte_length=?",
                              (str(stored), digest, len(payload)))
        db.connection.commit()
        db.close()
        Database.migrated(self.data / "local.sqlite").close()

    def package(self) -> Path:
        output = self.root / "bundle.zip"
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "package_portable.py"),
             "--data-dir", str(self.data),
             "--baseline", str(self.data / "history" / "none"),
             "--ledger", str(self.data / "ledger"),
             "--output", str(output)],
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode,
                         f"packaging failed:\n{result.stderr[-2000:]}")
        return output

    def test_a_corpus_with_no_baseline_and_no_legacy_database_still_packages(self) -> None:
        """The case that could not run at all.

        A new deployment has no earlier review round to compare against and no
        legacy crawler database beside it. Requiring either meant the first
        bundle could never be built.
        """
        output = self.package()
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 0)

    def test_every_file_matches_the_hash_the_manifest_claims(self) -> None:
        """A manifest nobody verifies is a list, not a checksum."""
        import hashlib

        output = self.package()
        unpacked = self.root / "unpacked"
        with zipfile.ZipFile(output) as archive:
            archive.extractall(unpacked)
        manifest = json.loads((unpacked / "manifest.json").read_text())
        self.assertEqual("mobile-observatory-portable-v1", manifest["format"])

        mismatches = []
        for relative, meta in manifest["files"].items():
            path = unpacked / relative
            if not path.is_file():
                mismatches.append(f"{relative}: missing")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != meta["sha256"]:
                mismatches.append(f"{relative}: hash differs")
        self.assertEqual([], mismatches)
        self.assertGreater(len(manifest["files"]), 0, "an empty bundle is not a bundle")

    def test_the_manifest_only_claims_databases_it_shipped(self) -> None:
        """It used to list five unconditionally, including ones it had skipped."""
        output = self.package()
        unpacked = self.root / "unpacked2"
        with zipfile.ZipFile(output) as archive:
            archive.extractall(unpacked)
        manifest = json.loads((unpacked / "manifest.json").read_text())
        for name in manifest["databases"]:
            self.assertTrue((unpacked / name).is_file(),
                            f"manifest claims {name} but the bundle does not contain it")

    def test_the_unpacked_corpus_opens_and_serves_the_devices_it_packaged(self) -> None:
        """The whole point: a bundle that cannot be read is not a deliverable."""
        from mobile_observatory.server import ObservatoryService

        output = self.package()
        unpacked = self.root / "deployed"
        with zipfile.ZipFile(output) as archive:
            archive.extractall(unpacked)
        manifest = json.loads((unpacked / "manifest.json").read_text())
        expected = manifest["corpus_row_counts"]["hardware_models"]
        self.assertGreater(expected, 0, "precondition: the fixture has devices")

        corpus = Database(unpacked / "corpus.sqlite", check_same_thread=False)
        self.addCleanup(corpus.close)
        corpus.apply_migrations()
        service = ObservatoryService(corpus, unpacked / "local.sqlite", demonstration=False)
        self.addCleanup(service.local.close)
        page = service.devices_page({"limit": ["500"]})
        self.assertEqual(expected, page.total,
                         "the deployed bundle must serve the devices it says it packaged")


if __name__ == "__main__":
    unittest.main()
