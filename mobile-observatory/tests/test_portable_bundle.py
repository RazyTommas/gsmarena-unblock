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

The same reasoning is why `BundleCanReIngestTest` below exists. Serving was
only half the deployment. The bundle shipped the corpus, the ledger and the
evidence and none of the batch's INPUTS -- the ~16 captured CSV/YAML files
`run_batch` reads from `--legacy-root`, whose default points at a sibling
crawler checkout that does not exist beside an unpacked bundle. An air-gapped
box could therefore serve whatever corpus it arrived with and could never get
another one: the batch died at its second step, `seed_samsung_history_identities`,
on a FileNotFoundError. Opening the bundle proved it could be read; nothing
proved it could be refreshed, so nothing noticed that it could not.
"""
from __future__ import annotations

import ast
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.batch_inputs import (BUNDLE_INPUTS_DIRECTORY,  # noqa: E402
                                             REQUIRED_LEGACY_INPUTS)
from mobile_observatory.seed import seed_demonstration  # noqa: E402

#: A complete `--legacy-root`, small enough to ingest inside a test. Real
#: captured rows, truncated; see its README for what that does and does not
#: license anyone to conclude.
SAMPLE_LEGACY_ROOT = ROOT / "fixtures" / "legacy-root-sample"


class PackagedCorpusFixture(unittest.TestCase):
    """A captured-shaped corpus plus the one call that packages it.

    No test methods of its own: it is the shared setUp for everything below,
    kept a TestCase so `package()` can use the assertion helpers.
    """

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

    def run_packager(self, *extra: str, name: str = "bundle.zip") -> tuple[Path, subprocess.CompletedProcess]:
        output = self.root / name
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "package_portable.py"),
             "--data-dir", str(self.data),
             "--baseline", str(self.data / "history" / "none"),
             "--ledger", str(self.data / "ledger"),
             "--output", str(output), *extra],
            capture_output=True, text=True)
        return output, result

    def package(self, legacy_root: Path | None = None, name: str = "bundle.zip") -> Path:
        extra = ("--legacy-root", str(legacy_root)) if legacy_root is not None else ()
        output, result = self.run_packager(*extra, name=name)
        self.assertEqual(0, result.returncode,
                         f"packaging failed:\n{result.stderr[-2000:]}")
        return output

    def unpack(self, output: Path, into: str) -> Path:
        unpacked = self.root / into
        with zipfile.ZipFile(output) as archive:
            archive.extractall(unpacked)
        return unpacked


class PortableBundleTest(PackagedCorpusFixture):
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


def _observation_count(corpus: Path) -> int:
    with sqlite3.connect(corpus) as db:
        return db.execute("SELECT count(*) FROM observations").fetchone()[0]


class BundleCanReIngestTest(PackagedCorpusFixture):
    """A bundle that cannot be refreshed is a dead end, not a deployment.

    These run the real batch as a subprocess against the unpacked tree, the way
    the offline operator would. Nothing is stubbed: the adapters read the
    packaged CSV/YAML bytes, the ledger is written, and the corpus in the
    bundle is the corpus that grows.
    """

    def test_the_unpacked_bundle_re_ingests_from_its_own_packaged_inputs(self) -> None:
        """The defect, stated as a test.

        Unpack into a directory that has never seen this repository, point the
        batch at `--data-dir <unpacked>` and `--legacy-root <unpacked>/inputs`,
        and require both that it exits 0 and that the corpus is bigger
        afterwards. Exit 0 alone is not enough: every adapter whose file is
        absent is recorded as a `failed` run and imports nothing, so a bundle
        with no inputs at all would still exit 0 if the batch's direct readers
        did not raise first. Only the row count distinguishes "ingested" from
        "ran and did nothing".
        """
        output = self.package(legacy_root=SAMPLE_LEGACY_ROOT)
        unpacked = self.unpack(output, "airgapped")

        inputs = unpacked / BUNDLE_INPUTS_DIRECTORY
        self.assertEqual([], [name for name in REQUIRED_LEGACY_INPUTS
                              if not (inputs / name).is_file()],
                         "the bundle must carry every file the batch opens")

        before = _observation_count(unpacked / "corpus.sqlite")
        elsewhere = self.root / "a directory that has never seen this repository"
        elsewhere.mkdir()
        result = subprocess.run(
            [sys.executable, "-m", "mobile_observatory.batch",
             "--data-dir", str(unpacked), "--legacy-root", str(inputs)],
            capture_output=True, text=True, cwd=elsewhere,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(0, result.returncode,
                         f"the batch must run against the unpacked bundle:\n"
                         f"{result.stdout[-3000:]}\n{result.stderr[-3000:]}")

        after = _observation_count(unpacked / "corpus.sqlite")
        self.assertGreater(after, before,
                           "the batch exited 0 without ingesting anything: a bundle whose batch "
                           "does nothing is not a fixed bundle")
        # And it ingested from the PACKAGED inputs, not from something it found
        # lying around: every source below exists only in inputs/.
        with sqlite3.connect(unpacked / "corpus.sqlite") as db:
            sources = {row[0] for row in db.execute(
                "SELECT DISTINCT source_id FROM observations")}
        self.assertIn("samsung.doc.aspl", sources)
        self.assertIn("mifirm.community.firmware_archive", sources)

    def test_a_bundle_without_inputs_says_serve_only_in_the_manifest(self) -> None:
        """Omitting the inputs is allowed. Omitting them silently is not.

        The packager can still build a serve-only bundle -- that is what every
        existing caller does -- but the bundle has to admit it, in the manifest
        and in its own README, so nobody carries it to an offline box expecting
        to refresh from it.
        """
        output = self.package(name="serve-only.zip")
        unpacked = self.unpack(output, "serve-only")
        manifest = json.loads((unpacked / "manifest.json").read_text())

        self.assertFalse(manifest["inputs"]["packaged"])
        self.assertTrue(manifest["inputs"]["serve_only"])
        self.assertIn("CANNOT re-ingest", manifest["inputs"]["reason"])
        self.assertIn("SERVE-ONLY", manifest["notes"])
        self.assertIn("SERVE-ONLY", (unpacked / "README.md").read_text())
        self.assertFalse((unpacked / BUNDLE_INPUTS_DIRECTORY).exists(),
                         "a serve-only bundle must not ship a half-filled inputs directory")
        self.assertEqual(list(REQUIRED_LEGACY_INPUTS), manifest["inputs"]["required"],
                         "it still has to name what it would have needed")

    def test_packaging_refuses_an_input_tree_that_is_missing_one_file(self) -> None:
        """Refuse, do not ship 15 of 16.

        A partial input set is the quiet failure: the adapters whose files are
        present ingest, the missing ones are recorded as `failed` runs, and the
        operator gets a smaller corpus and an exit code of 0. So the packager
        refuses the whole build and names the file, and writes no archive.
        """
        incomplete = self.root / "incomplete-inputs"
        shutil.copytree(SAMPLE_LEGACY_ROOT, incomplete)
        absent = REQUIRED_LEGACY_INPUTS[4]
        (incomplete / absent).unlink()

        output, result = self.run_packager("--legacy-root", str(incomplete),
                                           name="refused.zip")
        self.assertNotEqual(0, result.returncode, "packaging a partial input set must fail")
        self.assertIn(absent, result.stderr, "the refusal must name the file it could not find")
        self.assertFalse(output.exists(), "a refused build must leave no archive behind")

    def test_every_packaged_input_is_hashed_in_the_manifest(self) -> None:
        """Same rule as every other shipped file, applied to the new ones."""
        import hashlib

        output = self.package(legacy_root=SAMPLE_LEGACY_ROOT, name="hashed.zip")
        unpacked = self.unpack(output, "hashed")
        manifest = json.loads((unpacked / "manifest.json").read_text())
        listed = {name for name in manifest["files"]
                  if name.startswith(f"{BUNDLE_INPUTS_DIRECTORY}/")}
        self.assertEqual({f"{BUNDLE_INPUTS_DIRECTORY}/{name}" for name in REQUIRED_LEGACY_INPUTS},
                         listed)
        for name in sorted(listed):
            path = unpacked / name
            self.assertEqual(manifest["files"][name]["sha256"],
                             hashlib.sha256(path.read_bytes()).hexdigest(), name)
            self.assertEqual(manifest["files"][name]["bytes"], path.stat().st_size, name)

    def test_the_manifest_documents_the_command_that_re_ingests(self) -> None:
        """The offline box has the archive and nothing else to read."""
        output = self.package(legacy_root=SAMPLE_LEGACY_ROOT, name="documented.zip")
        unpacked = self.unpack(output, "documented")
        manifest = json.loads((unpacked / "manifest.json").read_text())
        command = manifest["inputs"]["batch_command"]
        self.assertIn("mobile_observatory.batch", command)
        self.assertIn("--data-dir", command)
        self.assertIn(f"--legacy-root <unpacked>/{BUNDLE_INPUTS_DIRECTORY}", command)
        readme = (unpacked / "README.md").read_text()
        self.assertIn("--legacy-root", readme)
        self.assertIn(BUNDLE_INPUTS_DIRECTORY, readme)


class PackagedInputsMatchTheBatchTest(unittest.TestCase):
    """The packaged input set and the set the batch reads must be one list.

    They are two lists: string literals inside `run_batch`, and
    `REQUIRED_LEGACY_INPUTS` in batch_inputs.py, which is the only one the
    packager can see. Nothing at runtime relates them -- the batch would simply
    crash on the offline box, months later, with a FileNotFoundError naming a
    file the bundle was never asked to carry. So they are compared here, by
    parsing batch.py rather than running it.
    """

    def test_batch_reads_exactly_the_inputs_the_packager_ships(self) -> None:
        tree = ast.parse((ROOT / "src" / "mobile_observatory" / "batch.py").read_text())
        # `a / b / c` nests left, so the inner `a / b` is itself a BinOp and
        # would otherwise be collected as the bogus path "b". Only the
        # outermost expression of each chain counts.
        nested = {id(node.left) for node in ast.walk(tree)
                  if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)}
        found = set()
        for node in ast.walk(tree):
            if (not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div)
                    or id(node) in nested):
                continue
            parts, cursor = [], node
            while isinstance(cursor, ast.BinOp) and isinstance(cursor.op, ast.Div):
                if not isinstance(cursor.right, ast.Constant) or not isinstance(cursor.right.value, str):
                    parts = None
                    break
                parts.append(cursor.right.value)
                cursor = cursor.left
            if parts and isinstance(cursor, ast.Name) and cursor.id == "legacy_root":
                found.add("/".join(reversed(parts)))

        self.assertEqual(set(REQUIRED_LEGACY_INPUTS), found,
                         "batch.py reads a legacy-root file that batch_inputs.py does not list "
                         "(or lists one it no longer reads). The packager ships the list, so the "
                         "bundle would be missing exactly that file.")


if __name__ == "__main__":
    unittest.main()
