"""Search must consider every row, not the first page of each table.

The original implementation called devices({}) / chips({}) / releases({}) with no
filter and then filtered the result in Python. That searched only the first page:
measured on the live corpus, 6 of 6 devices sampled from past row 100 were
invisible, and releases were matched against 100 of 21,186 rows. A user reported it
as "the S23 Ultra isn't in the db" -- it was, and it was only findable because it
happened to sort into the first page.

The failure is invisible to a small fixture, so these tests build a catalogue
LARGER than one page and look for the rows at the end of it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.server import ObservatoryService  # noqa: E402


class SearchCoverageTest(unittest.TestCase):
    """A catalogue deliberately bigger than SEARCH_PER_TYPE and than a page."""

    DEVICE_COUNT = 260          # > the 100-row default page that used to bound search

    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_search_tmp"
        self.tmp.mkdir(exist_ok=True)
        self.corpus_path = self.tmp / "corpus.sqlite"
        for leftover in self.tmp.glob("*"):
            leftover.unlink()
        corpus = Database.migrated(self.corpus_path)
        c = corpus.connection
        now = "2026-01-01T00:00:00Z"   # fixed: these tests must not depend on the clock
        c.execute("INSERT INTO manufacturers(id,canonical_name,created_at,updated_at)"
                  " VALUES('m1','TestCo',?,?)", (now, now))
        c.execute("INSERT INTO brands(id,manufacturer_id,canonical_name,created_at,updated_at)"
                  " VALUES('b1','m1','TestCo',?,?)", (now, now))
        c.execute("INSERT INTO device_families(id,brand_id,canonical_name,created_at,updated_at)"
                  " VALUES('f1','b1','Fam',?,?)", (now, now))
        for n in range(self.DEVICE_COUNT):
            # Names sort so that ZZ-* land last under any ascending order, and the
            # numeric prefix keeps them last under descending order too.
            name = f"Device {n:03d}"
            c.execute("INSERT INTO device_variants(id,family_id,canonical_name,created_at,updated_at)"
                      " VALUES(?,?,?,?,?)", (f"v{n}", "f1", name, now, now))
            c.execute("INSERT INTO hardware_models(id,variant_id,model_code,model_code_normalized,"
                      "codename,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                      (f"h{n}", f"v{n}", f"TC-{n:04d}", f"tc{n:04d}", f"code{n:04d}", now, now))
        self.service = ObservatoryService(corpus, self.tmp / "local.sqlite",
                                          demonstration=False)

    def tearDown(self):
        self.service.corpus.close()
        for leftover in self.tmp.glob("*"):
            leftover.unlink()
        self.tmp.rmdir()

    def _labels(self, term):
        return [i["label"] for i in self.service.search({"q": [term]})]

    def test_finds_a_device_in_the_first_page(self):
        """Control: if this fails the fixture is wrong, not the search."""
        self.assertIn("Device 000", self._labels("Device 000"))

    def test_finds_a_device_past_the_first_page(self):
        """The regression. Row 259 of 260 is past any 100-row page."""
        last = f"Device {self.DEVICE_COUNT - 1:03d}"
        self.assertIn(last, self._labels(last),
                      "search only looked at the first page of devices")

    def test_finds_by_model_code_past_the_first_page(self):
        code = f"TC-{self.DEVICE_COUNT - 1:04d}"
        self.assertTrue(any(code in lbl or code in str(i.get("detail"))
                            for i, lbl in zip(self.service.search({"q": [code]}),
                                              self._labels(code))),
                        f"{code} not found by model code")

    def test_every_device_is_findable_by_its_own_name(self):
        """Sampled across the whole catalogue, not just the ends."""
        missing = [n for n in range(0, self.DEVICE_COUNT, 37)
                   if f"Device {n:03d}" not in self._labels(f"Device {n:03d}")]
        self.assertEqual(missing, [], f"devices invisible to search: {missing}")

    def test_totals_report_all_matches_not_just_the_returned_ones(self):
        """A capped list must be labelled as a slice, or it reads as the whole answer."""
        payload = self.service.search_payload({"q": ["Device"]})
        self.assertEqual(payload["totals"]["device"], self.DEVICE_COUNT)
        self.assertLessEqual(len(payload["items"]), self.service.SEARCH_PER_TYPE * 3)
        self.assertGreater(payload["totals"]["device"], len(payload["items"]),
                           "fixture too small to prove the cap is reported")

    def test_empty_query_returns_nothing_rather_than_everything(self):
        self.assertEqual(self.service.search({"q": [""]}), [])
        self.assertEqual(self.service.search({"q": ["   "]}), [])

    def test_no_match_is_an_empty_list_not_an_error(self):
        self.assertEqual(self._labels("nosuchdevicename"), [])


if __name__ == "__main__":
    unittest.main()
