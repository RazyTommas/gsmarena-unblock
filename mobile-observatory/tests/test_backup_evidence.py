"""A backup nobody has restored is a hypothesis.

corpus.sqlite is 243 MB of mostly derived data and is deliberately NOT backed
up: the captured inputs are in git and a batch rebuilds it. What cannot be
rebuilt is small -- measured at 20 files and 20.6 MB on the live corpus, every
one of them a file an `artifacts` row points at inside the data directory, plus
local.sqlite's 5,785 acknowledgements. Compressed, 2.0 MB.

The set is derived by asking the corpus which files it cites, not by listing
directories, so it stays correct when a new source writes somewhere new. A
backup that silently stops covering a source is worse than none, because it
reports success either way.

These tests exercise the round trip against a REAL loss: build an archive,
delete every irreplaceable byte, restore, and check the corpus can resolve
every artifact again and still serves. Verifying the archive alone would only
prove the archive is internally consistent, which is not the question anyone
asks during an incident.

One defect in this tool was found by running the drill rather than reading the
code: the first archive contained no local.sqlite, and both backup and restore
reported plain success. Half the thing being protected was missing and the exit
code said fine. That is what `test_an_archive_without_local_sqlite_says_so`
guards.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import backup_evidence  # noqa: E402

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.watches import migrate_watches  # noqa: E402

NOW = "2026-09-28T00:00:00Z"


class BackupRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.data = self.root / "data"
        (self.data / "evidence" / "artifacts").mkdir(parents=True)
        (self.data / "ledger" / "raw" / "some.source").mkdir(parents=True)

        db = Database.migrated(self.data / "corpus.sqlite")
        seed_demonstration(db, ROOT / "fixtures" / "supported_catalog.sample.json")

        # The seed ships exactly one artifact, whose storage_uri is a pseudo-URI
        # ("fixture:...") rather than a path. Insert real ones instead of
        # rewriting it: two whose bytes live inside the data directory, in the
        # two places real ones do.
        source_id, run_id = db.connection.execute(
            "SELECT source_id, id FROM ingestion_runs LIMIT 1").fetchone()
        self.source_id, self.run_id = source_id, run_id
        self.inside: list[Path] = []
        for index, target in enumerate([
                self.data / "evidence" / "artifacts" / "a.bin",
                self.data / "ledger" / "raw" / "some.source" / "b.bin"]):
            payload = f"captured-bytes-{index}".encode() * 64
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            self.inside.append(target)
            db.connection.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,'text/csv',NULL,?,?,?)",
                (f"art-{index}", source_id, run_id, hashlib.sha256(payload).hexdigest(),
                 NOW, str(target), len(payload)))
        db.connection.commit()
        db.close()

        local = Database.migrated(self.data / "local.sqlite")
        migrate_watches(local.connection)
        local.connection.execute(
            "INSERT OR IGNORE INTO watches(subject_type,subject_id) VALUES('hardware_model','w1')")
        local.connection.commit()
        local.close()

    def add_artifact(self, identifier: str, path: Path, payload: bytes) -> None:
        db = sqlite3.connect(self.data / "corpus.sqlite")
        db.execute("INSERT INTO artifacts VALUES(?,?,?,?,'text/csv',NULL,?,?,?)",
                   (identifier, self.source_id, self.run_id,
                    hashlib.sha256(payload).hexdigest(), NOW, str(path), len(payload)))
        db.commit(); db.close()

    def make(self, output_name: str = "backup.tar.gz") -> Path:
        output = self.root / output_name
        backup_evidence.build(self.data, output, stamp=NOW)
        return output

    def manifest_of(self, archive: Path) -> dict:
        with tarfile.open(archive, "r:gz") as handle:
            return json.loads(handle.extractfile("manifest.json").read().decode())

    # -- what it selects -------------------------------------------------------
    def test_it_backs_up_artifact_bytes_inside_the_data_dir_and_not_the_repo_s(self) -> None:
        """The repository's captured inputs are 24 MB and already in git.
        Copying them here would duplicate something versioned."""
        outside = self.root / "repo-input.csv"
        outside.write_bytes(b"in git already")
        self.add_artifact("art-outside", outside, b"in git already")

        files, problems = backup_evidence.irreplaceable_files(
            self.data / "corpus.sqlite", self.data)
        relatives = {f["relative"] for f in files}
        self.assertEqual({"evidence/artifacts/a.bin", "ledger/raw/some.source/b.bin"}, relatives)
        self.assertEqual([], problems)

    def test_the_set_comes_from_the_corpus_not_a_directory_listing(self) -> None:
        """A file nothing cites is not evidence; a new source writing somewhere
        new must be picked up without editing this tool."""
        stray = self.data / "evidence" / "artifacts" / "not-cited.bin"
        stray.write_bytes(b"orphan")
        elsewhere = self.data / "brand-new-place" / "c.bin"
        elsewhere.parent.mkdir(parents=True)
        payload = b"a source that writes somewhere new"
        elsewhere.write_bytes(payload)
        self.add_artifact("art-new-place", elsewhere, payload)

        relatives = {f["relative"] for f in backup_evidence.irreplaceable_files(
            self.data / "corpus.sqlite", self.data)[0]}
        self.assertIn("brand-new-place/c.bin", relatives, "a new location must be covered")
        self.assertNotIn("evidence/artifacts/not-cited.bin", relatives,
                         "an uncited file is not evidence")

    def test_an_artifact_whose_bytes_changed_is_reported_not_shipped(self) -> None:
        """Backing up bytes that no longer match what the corpus recorded would
        preserve corruption and call it a backup."""
        self.inside[0].write_bytes(b"tampered")
        files, problems = backup_evidence.irreplaceable_files(
            self.data / "corpus.sqlite", self.data)
        self.assertTrue(any("no longer matches" in p for p in problems), problems)
        self.assertNotIn("evidence/artifacts/a.bin", {f["relative"] for f in files})

    # -- the round trip --------------------------------------------------------
    def test_a_real_loss_is_survived(self) -> None:
        """The drill: destroy every irreplaceable byte, then restore."""
        archive = self.make()
        for path in self.inside:
            path.unlink()
        (self.data / "local.sqlite").unlink()

        db = sqlite3.connect(f"file:{self.data / 'corpus.sqlite'}?mode=ro", uri=True)
        gone = sum(1 for (uri,) in db.execute("SELECT storage_uri FROM artifacts")
                   if str(uri).startswith(str(self.data)) and not Path(uri).is_file())
        db.close()
        self.assertEqual(2, gone, "precondition: the bytes really are lost")

        report = backup_evidence.restore(archive, self.data)
        self.assertEqual(2, report["artifact_files_restored"])
        self.assertTrue(report["local_database_restored"])

        db = sqlite3.connect(f"file:{self.data / 'corpus.sqlite'}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        unresolved = []
        for row in db.execute("SELECT storage_uri, sha256 FROM artifacts"):
            path = Path(row["storage_uri"])
            if not str(path).startswith(str(self.data)):
                continue
            if not path.is_file():
                unresolved.append(f"{path.name}: absent")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                unresolved.append(f"{path.name}: hash differs")
        db.close()
        self.assertEqual([], unresolved, "every artifact must resolve after a restore")

        local = sqlite3.connect(f"file:{self.data / 'local.sqlite'}?mode=ro", uri=True)
        self.assertEqual(1, local.execute("SELECT COUNT(*) FROM watches").fetchone()[0],
                         "human decisions must come back too")
        local.close()

    def test_a_damaged_archive_is_refused_rather_than_half_restored(self) -> None:
        """Restoring half a backup leaves a directory nobody can reason about."""
        archive = self.make()
        with tarfile.open(archive, "r:gz") as handle:
            members = handle.getmembers()
            extracted = {m.name: handle.extractfile(m).read()
                         for m in members if m.isfile()}
        target = next(n for n in extracted if n.startswith("data/"))
        extracted[target] = b"corrupted"
        broken = self.root / "broken.tar.gz"
        with tarfile.open(broken, "w:gz") as handle:
            for name, payload in extracted.items():
                info = tarfile.TarInfo(name); info.size = len(payload)
                import io
                handle.addfile(info, io.BytesIO(payload))
        with self.assertRaises(SystemExit):
            backup_evidence.restore(broken, self.data)

    def test_verify_checks_every_file_against_the_manifest(self) -> None:
        report = backup_evidence.verify(self.make())
        self.assertEqual([], report["bad"])
        self.assertEqual(3, report["checked"], "two artifacts plus local.sqlite")

    # -- the defect the drill found -------------------------------------------
    def test_an_archive_without_local_sqlite_says_so(self) -> None:
        """Both backup and restore once reported plain success while the entire
        human-decision database was missing from the archive."""
        (self.data / "local.sqlite").unlink()
        output = self.root / "nolocal.tar.gz"
        manifest = backup_evidence.build(self.data, output, stamp=NOW)
        self.assertIsNone(manifest["local_database"])
        self.assertTrue(any("local.sqlite" in p for p in manifest["problems"]),
                        "an incomplete backup must name what it is missing")

        report = backup_evidence.restore(output, self.data)
        self.assertFalse(report["local_database_restored"])
        self.assertIn("NOT restored", report["local_database_note"])

    def test_the_cli_exits_nonzero_when_the_backup_is_incomplete(self) -> None:
        """A scheduler reads the exit code, not the prose."""
        (self.data / "local.sqlite").unlink()
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "backup_evidence.py"),
             "--data-dir", str(self.data), "--output", str(self.root / "cli.tar.gz")],
            capture_output=True, text=True)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("gap(s) in this backup", result.stderr)

    # -- the corpus's identity travels with its evidence ----------------------
    def test_the_identity_baseline_is_in_the_archive_and_comes_back(self) -> None:
        """Without it, a restore puts the evidence back and takes with it the one
        record that could have checked whether the corpus rebuilt on top of it is
        the corpus the backup came from. See docs/BACKUP.md and
        src/mobile_observatory/corpus_identity.py."""
        from mobile_observatory import corpus_identity

        corpus_identity.write_baseline(
            Database(self.data / "corpus.sqlite").connection,
            self.data / corpus_identity.BASELINE_FILENAME)
        output = self.make("withidentity.tar.gz")
        manifest = self.manifest_of(output)
        self.assertIsNotNone(manifest["identity_baseline"])
        self.assertEqual([], backup_evidence.verify(output)["bad"])

        (self.data / corpus_identity.BASELINE_FILENAME).unlink()
        report = backup_evidence.restore(output, self.data)
        self.assertTrue(report["identity_baseline_restored"])
        recovered, why = corpus_identity.read_baseline(
            self.data / corpus_identity.BASELINE_FILENAME)
        self.assertIsNone(why)
        self.assertIn("identity_conclusions", recovered["components"])

    def test_an_archive_without_the_identity_baseline_says_so(self) -> None:
        """The same rule as local.sqlite: an archive that quietly lacks it must
        not print the same success as one that has it."""
        from mobile_observatory import corpus_identity

        baseline = self.data / corpus_identity.BASELINE_FILENAME
        if baseline.exists():
            baseline.unlink()
        output = self.root / "noidentity.tar.gz"
        manifest = backup_evidence.build(self.data, output, stamp=NOW)
        self.assertIsNone(manifest["identity_baseline"])
        self.assertTrue(any("corpus-identity.json" in p for p in manifest["problems"]))
        report = backup_evidence.restore(output, self.data)
        self.assertFalse(report["identity_baseline_restored"])
        self.assertIn("cannot be compared", report["identity_baseline_note"])

    def test_an_older_archive_without_the_key_still_verifies(self) -> None:
        """The manifest key is optional on purpose: a backup taken before this
        existed must not become unrestorable because of it."""
        import json
        import tarfile

        source = self.make("tobedowngraded.tar.gz")
        downgraded = self.root / "downgraded.tar.gz"
        with tarfile.open(source, "r:gz") as old, tarfile.open(downgraded, "w:gz") as new:
            for member in old.getmembers():
                data = old.extractfile(member)
                if member.name == backup_evidence.MANIFEST:
                    manifest = json.loads(data.read().decode("utf-8"))
                    manifest.pop("identity_baseline", None)
                    payload = json.dumps(manifest, indent=2).encode()
                    member.size = len(payload)
                    import io
                    new.addfile(member, io.BytesIO(payload))
                elif member.name == "corpus-identity.json":
                    continue
                else:
                    new.addfile(member, data)
        self.assertEqual([], backup_evidence.verify(downgraded)["bad"])

    # -- the one disaster it exists for: a DIFFERENT directory ----------------
    def test_a_restore_into_a_different_directory_repoints_the_corpus(self) -> None:
        """`artifacts.storage_uri` is absolute, and nothing used to repoint it.

        The old drill restored /tmp/drill back into /tmp/drill, so it could never
        see this: the paths it checked were the paths it had written. Measured on
        the old code with a different target directory -- 1 of 1 artifact file
        restored, and the corpus still citing the ORIGINAL box's path, which does
        not exist there.
        """
        import shutil

        archive = self.make("move.tar.gz")
        elsewhere = self.root / "a-different-box" / "observatory-data"
        elsewhere.mkdir(parents=True)
        # You carried the corpus (docs/BACKUP.md: copy .observatory-data when you
        # need THIS corpus) and the evidence comes from the 2 MB archive.
        shutil.copy2(self.data / "corpus.sqlite", elsewhere / "corpus.sqlite")
        shutil.rmtree(self.data)                         # the original box is gone

        report = backup_evidence.restore(archive, elsewhere)
        self.assertEqual(0, report["evidence_paths"]["unresolved_count"],
                         f"the corpus still cites bytes it cannot produce: "
                         f"{report['evidence_paths']['unresolved']}")
        self.assertGreater(report["evidence_paths"]["rebased"], 0)

        db = sqlite3.connect(elsewhere / "corpus.sqlite")
        try:
            rows = db.execute("SELECT id, storage_uri FROM artifacts").fetchall()
        finally:
            db.close()
        absolute = [(i, u) for i, u in rows if str(u).startswith("/")]
        self.assertTrue(absolute, "the fixture must have absolute URIs or this proves nothing")
        for identifier, uri in absolute:
            with self.subTest(artifact=identifier):
                self.assertTrue(Path(uri).is_file(), f"{identifier} -> {uri} does not exist")
                self.assertTrue(str(uri).startswith(str(elsewhere)),
                                f"{identifier} still points at the original directory")

    def test_the_rebase_is_a_no_op_when_nothing_moved(self) -> None:
        """A rule that rewrites rows it did not need to is a rule that makes a
        clean restore look like a recovery."""
        archive = self.make("inplace.tar.gz")
        report = backup_evidence.restore(archive, self.data)
        self.assertEqual(0, report["evidence_paths"]["rebased"])
        self.assertGreater(report["evidence_paths"]["already_correct"], 0)
        self.assertEqual(0, report["evidence_paths"]["unresolved_count"])

    def test_a_moved_directory_can_be_rebased_with_no_archive_at_all(self) -> None:
        """`--rebase`: the same rule over what is on disk. A directory somebody
        copied to a new box has no archive to recover from and the same broken
        absolute paths."""
        import shutil

        moved = self.root / "moved"
        shutil.copytree(self.data, moved)
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "backup_evidence.py"),
             "--rebase", "--data-dir", str(moved)],
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(0, report["unresolved_count"])
        db = sqlite3.connect(moved / "corpus.sqlite")
        try:
            uris = [u for (u,) in db.execute(
                "SELECT storage_uri FROM artifacts").fetchall() if str(u).startswith("/")]
        finally:
            db.close()
        self.assertTrue(uris)
        for uri in uris:
            self.assertTrue(str(uri).startswith(str(moved)), uri)
            self.assertTrue(Path(uri).is_file(), uri)

    def test_an_unresolved_row_is_counted_exactly_and_named_boundedly(self) -> None:
        """A partial relocation that reported only its successes would read
        exactly like a complete one."""
        sys.path.insert(0, str(ROOT / "src"))
        from mobile_observatory import evidence_paths

        self.add_artifact("art-gone", Path("/nowhere/at/all/x.bin"), b"never written")
        db = sqlite3.connect(self.data / "corpus.sqlite")
        try:
            report = evidence_paths.rebase(db, self.data, [])
        finally:
            db.close()
        self.assertEqual(1, report["unresolved_count"])
        self.assertIn("art-gone", report["unresolved"][0])

    def test_bytes_that_do_not_match_their_digest_are_refused_not_repointed(self) -> None:
        """The digest is the whole key. Repointing a corpus at a file that is
        not the file it recorded would make the corpus cite the wrong bytes --
        and do it under the name of a recovery, which is worse than leaving the
        broken path visible."""
        sys.path.insert(0, str(ROOT / "src"))
        from mobile_observatory import evidence_paths

        target = self.data / "evidence" / "artifacts" / "a.bin"
        before = target.read_bytes()
        target.write_bytes(b"different bytes entirely")
        db = sqlite3.connect(self.data / "corpus.sqlite")
        try:
            stored_before = db.execute(
                "SELECT storage_uri FROM artifacts WHERE id='art-0'").fetchone()[0]
            report = evidence_paths.rebase(
                db, self.data,
                [("evidence/artifacts/a.bin", hashlib.sha256(before).hexdigest())])
            stored_after = db.execute(
                "SELECT storage_uri FROM artifacts WHERE id='art-0'").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(0, report["rebased"])
        self.assertEqual(1, len(report["mismatched"]))
        self.assertIn("does not match", report["mismatched"][0])
        self.assertEqual(stored_before, stored_after,
                         "the corpus was repointed at bytes that are not the bytes it "
                         "recorded")

    def test_an_entry_resolving_outside_the_data_directory_is_refused(self) -> None:
        """A `..` in somebody else's manifest is not a path into our corpus."""
        sys.path.insert(0, str(ROOT / "src"))
        from mobile_observatory import evidence_paths

        db = sqlite3.connect(self.data / "corpus.sqlite")
        try:
            report = evidence_paths.rebase(db, self.data, [("../outside.bin", None)])
        finally:
            db.close()
        self.assertEqual(0, report["rebased"])
        self.assertTrue(any("outside the data directory" in m
                            for m in report["mismatched"] + report["missing"]))

    def test_a_pseudo_uri_is_not_reported_as_a_broken_path(self) -> None:
        """The demonstration seed ships one `fixture:` row. Counting it as
        unresolved would make every fresh corpus look damaged."""
        sys.path.insert(0, str(ROOT / "src"))
        from mobile_observatory import evidence_paths

        db = sqlite3.connect(self.data / "corpus.sqlite")
        try:
            db.execute("UPDATE artifacts SET storage_uri='fixture:seeded' WHERE id='art-0'")
            db.commit()
            report = evidence_paths.rebase(db, self.data, [])
        finally:
            db.close()
        self.assertEqual(0, report["unresolved_count"])

    def test_an_empty_corpus_refuses_rather_than_writing_a_useless_archive(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        Database.migrated(empty / "corpus.sqlite").close()
        with self.assertRaises(SystemExit):
            backup_evidence.build(empty, self.root / "empty.tar.gz", stamp=NOW)


if __name__ == "__main__":
    unittest.main()
