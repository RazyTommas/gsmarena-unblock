"""`python3 run.py` must not hand a deployer a tenth of the corpus.

The README led with `python3 run.py` and run.py extracted a hardcoded
`portable/mobile-observatory-2026-09-17.zip`. Measured: **83 devices / 26,961
observations** against 854 / 94,969 from the batch
over the captured inputs that are in the same repository -- and nothing
downstream said so. Every page, count and export was internally consistent and
answered from a corpus missing 771 phones.

It could not be repointed at a correct path: the only zip in `portable/` IS the
09-17 one, the 09-22 manifest has no zip beside it, and even that one states 236
devices. So the default is a refusal that names the batch, with the snapshot kept
behind an explicit `--bundle`.

These tests run the real script as a subprocess, because what shipped wrong was
the DEFAULT -- and a test that imports `main()` and calls it with the flag it
needs proves nothing about what a deployer typing `python3 run.py` gets.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("portable_launcher", ROOT / "run.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class TheDefaultInvocationRefuses(unittest.TestCase):
    """What a deployer following the old README actually gets, measured by
    running it."""

    def run_it(self, *args, bare: bool = False):
        """Run run.py and come back.

        `--restore-only` and a throwaway `--data-dir` are added unless `bare`,
        and both are there because of what happened when the refusal was REMOVED
        to check these tests can fail: run.py extracted 34 MB into the
        repository and then served forever, so the test hung instead of failing
        and left a 170 MB directory behind. Neither flag can mask the refusal --
        it is decided before either is read -- but together they mean a
        regression fails fast and writes only into a temporary directory.

        The `bare` case runs the literal `python3 run.py` a deployer types, with
        a short timeout: the refusal is immediate, so a timeout IS the failure.
        """
        argv = list(args)
        temporary = None
        if not bare:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            argv += ["--restore-only", "--data-dir", str(Path(temporary.name) / "data")]
        try:
            proc = subprocess.run([sys.executable, "run.py", *argv], cwd=ROOT,
                                  capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as expired:
            self.fail(f"`run.py {' '.join(argv)}` did not return in 120s. A refusal is "
                      f"immediate; a run that blocks is one that started serving the "
                      f"snapshot. stderr so far: {expired.stderr!r}")
        return proc.returncode, proc.stdout, proc.stderr

    def test_bare_run_py_exits_nonzero_and_restores_nothing(self) -> None:
        """Literally what the old README said to type, with no flags at all.

        The bare form has no `--data-dir` to redirect, so run.py's default --
        `<repo>/.observatory-data` -- is what a regression would write 34 MB
        into. The cleanup below removes that directory ONLY when this test found
        it absent, so it can never touch a corpus that was already there, and a
        regression does not leave a 170 MB 83-device restore in the checkout for
        somebody to find later and mistake for the real one. (Measured: the
        first planted-defect run did exactly that.)
        """
        default = ROOT / ".observatory-data"
        if not default.exists():
            self.addCleanup(lambda: shutil.rmtree(default, ignore_errors=True)
                            if default.exists() else None)
        code, out, err = self.run_it(bare=True)
        self.assertNotEqual(0, code,
                            "a script piping this into a deployment must see a failure")
        self.assertEqual("", out, "the refusal belongs on stderr, not in a pipeline's stdout")
        self.assertIn("PACKAGED HISTORICAL SNAPSHOT", err)
        self.assertFalse(default.exists(),
                         "the refusal created a data directory; it must restore nothing")

    def test_the_refusal_names_the_command_to_run_instead(self) -> None:
        """A refusal that does not say what to do instead is a worse README."""
        _, _, err = self.run_it()
        # With PYTHONPATH, because that is the form that actually runs: see
        # tests/test_documented_commands_actually_run.py.
        self.assertIn("PYTHONPATH=src python3 -m mobile_observatory.batch", err)
        self.assertIn("mobile_observatory.server", err)

    def test_the_refusal_states_what_the_snapshot_actually_holds(self) -> None:
        """Read from the bundle's own manifest, never hardcoded here -- a number
        typed into a refusal goes stale the first time the bundle moves."""
        manifest = json.loads(
            (ROOT / "portable" / "mobile-observatory-2026-09-17.json").read_text())
        devices = manifest["corpus_row_counts"]["hardware_models"]
        _, _, err = self.run_it()
        self.assertIn(f"{devices:,} devices", err)

    def test_the_refusal_names_the_manifest_that_has_no_zip(self) -> None:
        """The 09-22 bundle's absence lived only in a handoff's open-items list.
        A declared snapshot that cannot be served has to say so where somebody
        reaching for it will read it."""
        _, _, err = self.run_it()
        self.assertIn("mobile-observatory-2026-09-22", err)
        self.assertIn("missing", err)

    def test_list_bundles_reports_every_declared_snapshot_and_its_state(self) -> None:
        code, out, _ = self.run_it("--list-bundles")
        self.assertEqual(0, code)
        self.assertIn("ZIP MISSING", out)
        self.assertIn("present", out)
        self.assertEqual(2, len(out.strip().splitlines()),
                         "both declared manifests, not only the usable one")


class TheBundlePathIsStillThereAndSaysWhatItIs(unittest.TestCase):
    """--bundle is an opt-in, not a deprecation: an air-gapped box with no
    captured inputs has no other route, and --restore-only is documented."""

    def test_restoring_the_bundle_yields_exactly_what_the_refusal_claimed(self) -> None:
        """The proof that the number in the refusal is a measurement: extract the
        bundle and count the rows."""
        import sqlite3
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "data"
            proc = subprocess.run(
                [sys.executable, "run.py", "--bundle", "--restore-only",
                 "--data-dir", str(target)],
                cwd=ROOT, capture_output=True, text=True, timeout=900)
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertIn("mobile-observatory-2026-09-17.zip", proc.stdout)
            self.assertIn("is NOT the corpus", proc.stdout,
                          "even the opt-in path must say what it is not")
            connection = sqlite3.connect(target / "corpus.sqlite")
            try:
                counts = {t: connection.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                          for t in ("hardware_models", "observations")}
            finally:
                connection.close()
        manifest = json.loads(
            (ROOT / "portable" / "mobile-observatory-2026-09-17.json").read_text())
        self.assertEqual(manifest["corpus_row_counts"]["hardware_models"],
                         counts["hardware_models"])
        self.assertEqual(manifest["corpus_row_counts"]["observations"],
                         counts["observations"])
        self.assertEqual(83, counts["hardware_models"],
                         "the measured tenth-of-the-corpus figure from the handoff")


class SelectingABundleIsNotADateTypedIntoTheSource(unittest.TestCase):
    """The hardcode is how 09-17 stayed the default after 09-22 was packaged."""

    def inventory_dir(self, bundles):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        for name, counts, with_zip in bundles:
            archive = f"{name}.zip"
            (directory / f"{name}.json").write_text(json.dumps(
                {"archive": archive, "sha256": "x",
                 **({"corpus_row_counts": counts} if counts else {})}))
            if with_zip:
                with zipfile.ZipFile(directory / archive, "w") as handle:
                    handle.writestr("manifest.json", "{}")
        return directory

    def test_the_newest_manifest_with_a_zip_wins(self) -> None:
        directory = self.inventory_dir([
            ("mobile-observatory-2026-09-17", {"hardware_models": 83}, True),
            ("mobile-observatory-2026-11-01", {"hardware_models": 900}, True)])
        inventory = launcher.bundle_inventory(directory)
        self.assertEqual("mobile-observatory-2026-11-01.zip",
                         launcher.select_bundle(inventory)["archive"].name)

    def test_a_newer_manifest_with_no_zip_does_not_win_and_is_reported(self) -> None:
        """Today's real state. The newest DECLARED bundle must not be chosen just
        because it is newest, and its absence must not be silent."""
        directory = self.inventory_dir([
            ("mobile-observatory-2026-09-17", {"hardware_models": 83}, True),
            ("mobile-observatory-2026-09-22", {"hardware_models": 236}, False)])
        inventory = launcher.bundle_inventory(directory)
        self.assertEqual("mobile-observatory-2026-09-17.zip",
                         launcher.select_bundle(inventory)["archive"].name)
        refusal = launcher.bundle_refusal(inventory)
        self.assertIn("mobile-observatory-2026-09-22", refusal)
        self.assertIn("236 devices", refusal)

    def test_no_zip_at_all_raises_rather_than_restoring_nothing(self) -> None:
        directory = self.inventory_dir([
            ("mobile-observatory-2026-09-22", {"hardware_models": 236}, False)])
        with self.assertRaisesRegex(ValueError, "no packaged snapshot"):
            launcher.select_bundle(launcher.bundle_inventory(directory))

    def test_an_unreadable_manifest_is_listed_rather_than_skipped(self) -> None:
        directory = self.inventory_dir([
            ("mobile-observatory-2026-09-17", {"hardware_models": 83}, True)])
        (directory / "broken.json").write_text("{ not json")
        inventory = launcher.bundle_inventory(directory)
        broken = [b for b in inventory if b["manifest"].name == "broken.json"]
        self.assertEqual(1, len(broken))
        self.assertIsNotNone(broken[0]["error"])
        self.assertFalse(broken[0]["present"])


class ABundleThatStatesNoCountsIsNotReportedAsZero(unittest.TestCase):
    """Absence recorded as absence. A manifest predating corpus_row_counts says
    nothing, and printing "0 devices" would be inventing a value."""

    def test_missing_counts_say_so(self) -> None:
        self.assertIn("states no row counts", launcher.describe_counts({}))
        self.assertIn("states no row counts", launcher.describe_counts(None))

    def test_a_partial_count_reports_only_what_is_stated(self) -> None:
        described = launcher.describe_counts({"hardware_models": 83})
        self.assertEqual("83 devices", described)
        self.assertNotIn("observations", described)


class TheReadmeNoLongerLeadsWithIt(unittest.TestCase):
    """The trap had two halves and fixing one leaves the other. A source scan,
    because the README is the thing a deployer reads tomorrow."""

    def setUp(self) -> None:
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_the_first_command_in_the_readme_is_the_batch(self) -> None:
        fences = [block.strip() for block in self.readme.split("```")[1::2]]
        self.assertTrue(fences, "the README has no code block at all")
        first = fences[0]
        self.assertIn("mobile_observatory.batch", first)
        self.assertNotIn("run.py", first)

    def test_the_readme_says_what_run_py_costs_if_it_mentions_it(self) -> None:
        if "run.py" not in self.readme:
            return
        self.assertIn("83 devices", self.readme,
                      "mentioning run.py without the measured shortfall is the trap again")
        self.assertIn("refuses", self.readme)

    def test_the_rebuild_is_not_a_restore_caveat_reaches_the_readme(self) -> None:
        """It was in the handoff and in docs/BACKUP.md, and absent from the one
        document a new deployer actually opens."""
        self.assertIn("rebuild is not a restore", self.readme)


if __name__ == "__main__":
    unittest.main()
