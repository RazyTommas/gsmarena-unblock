"""Every command this project tells an operator to type must work.

MEASURED ON A FRESH CLONE, with the command copied verbatim out of HANDOFF.md:

    $ python3 -m mobile_observatory.batch
    ModuleNotFoundError: No module named 'mobile_observatory'       EXIT=1

Six documented sites omitted `PYTHONPATH=src`, including the production recipe in
docs/ACCESS_CONTROL.md and the ops command in
docs/AUTOMATED_IDENTITY_ENRICHMENT.md -- and `server.py` printed the broken form
to the operator inside an error message telling them how to recover.

WHY 677 TESTS NEVER CAUGHT IT, which is the part that matters more than the
typo: every test file begins `sys.path.insert(0, ROOT / "src")`. The suite
manufactures the one condition the operator does not have, so it exercises a
path no deployment ever takes. That is the vacuous-guard shape this codebase
keeps paying for -- a check that cannot fail is decoration.

So this file does two different things, and needs both:

  1. a SOURCE SCAN over every doc and every string that a human might copy,
     which catches the seventh site nobody has written yet;
  2. a SUBPROCESS RUN with a clean environment and no sys.path help, which is
     the only thing that proves the scan is guarding something true. It runs
     the documented form (must succeed) AND the bare form (must fail), because
     a test that only asserts the good case would still pass if `-m` worked
     either way and the whole rule were pointless.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# `python3 -m mobile_observatory.<something>` with no PYTHONPATH in front of it.
# `mobile_observatory.*` is excluded: a glob is a reference to the family of
# modules, not a command anybody can paste.
BARE = re.compile(r"(?<!PYTHONPATH=src )(?<!PYTHONPATH=<repo>/src )"
                  r"(?<!PYTHONPATH=<repo>/mobile-observatory/src )"
                  r"python3? -m mobile_observatory\.(?!\*)")

#: A line that is DEMONSTRATING the failure, rather than instructing anyone to
#: run it, says so. Narrow on purpose and counted below: an exemption nobody
#: bounds becomes the way the rule is avoided.
SHOWING_THE_FAILURE = "[FAILS]"
MAX_EXEMPTIONS = 4

# Files a human reads and copies from. Test modules are included: their
# docstrings are read by the next person to touch them.
def documents():
    for pattern in ("*.md", "docs/*.md", "*.py", "src/**/*.py", "tools/*.py",
                    "tests/*.py", "scheduling/*", "apps/web/*.js"):
        for path in ROOT.glob(pattern):
            if path.is_file() and "work-sessions" not in str(path):
                yield path


class EveryDocumentedModuleCommandCarriesItsPath(unittest.TestCase):
    def test_no_document_tells_an_operator_to_run_the_form_that_fails(self) -> None:
        offenders = []
        for path in documents():
            if path.name == Path(__file__).name:
                continue                      # this file quotes the broken form on purpose
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if BARE.search(line) and SHOWING_THE_FAILURE not in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(
            [], offenders,
            "these tell an operator to run a command that exits 1 with "
            "ModuleNotFoundError on a fresh clone:\n  " + "\n  ".join(offenders))

    def test_the_exemption_stays_rare_enough_to_be_an_exception(self) -> None:
        """`[FAILS]` marks a line that is showing the broken form on purpose. If
        it spreads, the rule above has been worked around rather than kept."""
        marked = []
        for path in documents():
            if path.name == Path(__file__).name:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            marked += [f"{path.relative_to(ROOT)}:{n}"
                       for n, line in enumerate(text.splitlines(), 1)
                       if SHOWING_THE_FAILURE in line]
        self.assertLessEqual(len(marked), MAX_EXEMPTIONS,
                             f"{len(marked)} lines are exempt: {marked}")

    def test_nothing_tells_an_operator_to_run_bare_python(self) -> None:
        """`python` does not exist on the deploy box. `python3` does."""
        offenders = []
        for path in documents():
            if path.suffix != ".md":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if re.match(r"^\$?\s*python\s+-m\s", stripped) or stripped.startswith("python -m "):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {stripped}")
        self.assertEqual([], offenders,
                         "`python` is not on the deploy box:\n  " + "\n  ".join(offenders))


class TheDocumentedFormIsRunHere(unittest.TestCase):
    """A clean environment, the project directory, and no sys.path help.

    `--help` rather than a real run: what is under test is whether the INTERPRETER
    can find the package, which fails before argparse either way. A real batch
    would take ten minutes to answer the same question.
    """

    def run_module(self, *argv, pythonpath=None):
        environ = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        if pythonpath is not None:
            environ["PYTHONPATH"] = pythonpath
        return subprocess.run([sys.executable, "-m", *argv], cwd=ROOT, env=environ,
                              capture_output=True, text=True, timeout=120)

    # Every module a document names as a command.
    MODULES = ("mobile_observatory.batch", "mobile_observatory.server",
               "mobile_observatory.integrity", "mobile_observatory.corpus_identity",
               "mobile_observatory.snapshots", "mobile_observatory.current_firmware")

    def test_the_documented_form_works_for_every_documented_module(self) -> None:
        for module in self.MODULES:
            with self.subTest(module=module):
                result = self.run_module(module, "--help", pythonpath="src")
                self.assertEqual(
                    0, result.returncode,
                    f"`PYTHONPATH=src python3 -m {module} --help` failed:\n"
                    f"{result.stderr[-1500:]}")
                self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_the_bare_form_still_fails_so_the_rule_above_is_not_decoration(self) -> None:
        """If this ever passes, `PYTHONPATH=src` has stopped being necessary --
        and the scan above has become a style rule guarding nothing. Delete it
        then, deliberately, rather than letting it quietly mean nothing."""
        result = self.run_module("mobile_observatory.batch", "--help")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("No module named 'mobile_observatory'", result.stderr)

    def test_the_test_suite_itself_runs_the_way_the_readme_says(self) -> None:
        """The README's own command, on one module, with a clean environment.

        The suite adds `src` to sys.path from inside each test file, which is
        exactly why it could not see this defect -- so this asserts the entry
        point a reader types, not the one the fixtures build.
        """
        environ = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_suite_is_discoverable", "-v"],
            cwd=ROOT, env=environ, capture_output=True, text=True, timeout=600)
        self.assertEqual(0, result.returncode, result.stderr[-2000:])


class TheErrorMessagesTellTheTruthToo(unittest.TestCase):
    """A recovery instruction inside an error message is a documented command.

    server.py printed `Run \\`python3 -m mobile_observatory.batch\\`` to an
    operator whose catalogue would not serve -- a person already having a bad
    day, handed a command that exits 1.
    """

    def test_every_recovery_instruction_in_the_source_is_runnable(self) -> None:
        offenders = []
        for path in sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tools").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                if BARE.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual([], offenders,
                         "these print a failing command at an operator: " + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
