"""Every test file must be visible to the runner the README tells you to run.

This suite was reported as 223 tests. `python3 -m unittest discover -s tests` --
the command in README.md, and the only runner an air-gapped deployment has, since
pytest is a development convenience that is not installed there -- collected 210.
The missing 13 lived in two files written as bare module-level `def test_*`
functions, which unittest does not collect at all. They guarded a threading race
that had already reached a user as a crashed search, and a name-normalisation bug
that had already stored seven devices twice. Both files ran green under pytest and
had never once run under the documented command.

The failure is silent in the worst direction: the count goes DOWN and nothing
reports it, so a file can stop being a test while still looking like one. This
checks the property directly -- every tests/test_*.py contributes at least one
test case to unittest's loader -- rather than pinning a total, which would have to
be edited every time a test is legitimately added.
"""
from __future__ import annotations

import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _case_count(suite: unittest.TestSuite | unittest.TestCase) -> int:
    if isinstance(suite, unittest.TestCase):
        return 1
    return sum(_case_count(child) for child in suite)


class SuiteIsDiscoverableTest(unittest.TestCase):
    def test_every_test_module_is_collectable_by_unittest(self) -> None:
        loader = unittest.TestLoader()
        modules = sorted(path.stem for path in TESTS.glob("test_*.py"))
        self.assertGreater(len(modules), 1, "precondition: there are test modules to check")

        empty = []
        for name in modules:
            if name == Path(__file__).stem:
                continue
            suite = loader.loadTestsFromName(f"tests.{name}")
            if _case_count(suite) == 0:
                empty.append(name)

        self.assertEqual(
            [], empty,
            "these modules contribute no tests to `python3 -m unittest discover -s tests`, "
            "so they do not run on a box without pytest. unittest only collects "
            "unittest.TestCase subclasses -- a bare `def test_*` at module level is "
            "invisible to it: " + ", ".join(empty))

    def test_no_module_fails_to_import(self) -> None:
        """A module that raises on import is reported by the loader as a single
        synthetic _FailedTest, which passes the count check above while running
        none of its real tests."""
        loader = unittest.TestLoader()
        broken = []
        for path in sorted(TESTS.glob("test_*.py")):
            suite = loader.loadTestsFromName(f"tests.{path.stem}")
            for case in _flatten(suite):
                if type(case).__name__ == "_FailedTest":
                    broken.append(f"{path.stem}: {getattr(case, '_exception', 'import failed')}")
        self.assertEqual([], broken, "test modules that do not import: " + "; ".join(broken))


def _flatten(suite):
    if isinstance(suite, unittest.TestCase):
        yield suite
        return
    for child in suite:
        yield from _flatten(child)


if __name__ == "__main__":
    unittest.main()
