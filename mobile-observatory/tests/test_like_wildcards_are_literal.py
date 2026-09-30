"""A search for `_` must search for `_`, and the reported total must be a count.

`%` and `_` are LIKE metacharacters. Every filter clause interpolated the user's
value into `%...%` and bound it without an ESCAPE, so no substring search ever
searched for itself. Measured on the live 865-device corpus, before the fix:

    GET /api/v1/search?q=_   ->  {"device": 865, "chip": 452, "release": 3443}
    GET /api/v1/search?q=%   ->  {"device": 865, "chip": 452, "release": 3443}

i.e. every device, every chip and every release reported as "matching". Nothing
was injected -- the values were bound parameters throughout -- and nothing
crashed. The defect is that the number beside the results was not a count of
anything the reader asked about, in a system whose entire product is counts it
can source. It also made a literal search impossible: `TECNO_W4` and `Tcard_CL8`
are real model codes and real build strings in this corpus.

After the fix, on the same corpus: `q=_` -> 60 devices / 4 chips / 0 releases
(the ones that really do contain an underscore) and `q=%` -> 0 / 0 / 0, while
`q=5G` stayed at 99 / 28 / 1357 and `q=i3` at 1 / 0 / 39 -- unchanged, which is
the other half of the claim.

The clause is built in exactly one place (database.like_clause) and the pattern
in exactly one place (database.like_contains), because the previous arrangement
had eighteen copies of the same clause text across two modules; fixing `q` on
/search alone would have left /devices, /releases, /source-records,
/identity/products, /product-releases, /product-security and /security/findings
each answering a different question from the same input.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.database import like_clause, like_contains  # noqa: E402
from mobile_observatory.repository import CanonicalRepository  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService  # noqa: E402

SOURCES = tuple((ROOT / "src" / "mobile_observatory").glob("*.py"))


class LikeHelperTest(unittest.TestCase):
    def test_a_pattern_matches_its_own_text_and_nothing_else(self) -> None:
        import sqlite3
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE t(v TEXT)")
        rows = ["TECNO_W4", "TECNOxW4", "TECNO i3", "50%off", "50off",
                "plain", "back\\slash", "a_b_c"]
        connection.executemany("INSERT INTO t VALUES(?)", [(r,) for r in rows])
        sql = f"SELECT v FROM t WHERE {like_clause('v')}"

        def found(query: str) -> list[str]:
            return sorted(r[0] for r in connection.execute(sql, (like_contains(query),)))

        # The underscore is a character, not "any character".
        self.assertEqual(found("TECNO_W4"), ["TECNO_W4"])
        self.assertNotIn("TECNOxW4", found("TECNO_W4"))
        # The percent is a character, not "anything".
        self.assertEqual(found("50%"), ["50%off"])
        self.assertEqual(found("%"), ["50%off"])
        # A bare wildcard no longer matches the whole table.
        self.assertNotEqual(len(found("_")), len(rows))
        # The escape character itself survives being searched for.
        self.assertEqual(found("back\\slash"), ["back\\slash"])
        # Case-insensitivity, which the clause must not have lost.
        self.assertEqual(found("tecno_w4"), ["TECNO_W4"])
        # And ordinary text still matches ordinarily.
        self.assertEqual(found("plain"), ["plain"])
        connection.close()

    def test_no_module_builds_a_like_clause_by_hand(self) -> None:
        """The pattern, not the instance. Eighteen hand-written `LIKE ?` clauses
        across server.py and security_read.py were what made this one defect
        eighteen defects; a nineteenth added later must not be invisible."""
        offenders = []
        for path in SOURCES:
            if path.name == "database.py":
                continue  # the one legitimate definition site
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"LIKE\s+\?", line) and "ESCAPE" not in line:
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual([], offenders,
                         "these lines bind a LIKE pattern without an ESCAPE clause; build "
                         "them with database.like_clause/like_contains instead: "
                         + ", ".join(offenders))

    def test_no_module_builds_a_contains_pattern_by_hand(self) -> None:
        offenders = []
        for path in SOURCES:
            if path.name == "database.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r'f["\']%\{', line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual([], offenders,
                         "these lines interpolate a value into a %...% LIKE pattern without "
                         "escaping its wildcards; use database.like_contains: "
                         + ", ".join(offenders))


class SearchTotalsAreCountsTest(unittest.TestCase):
    """End to end, through the service, on a corpus containing a literal `_`."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")
        # One device whose model code really does contain an underscore, and one
        # that differs from it only at that position. Without both, "matches the
        # literal" and "matches any character" are indistinguishable.
        repository = CanonicalRepository(self.db)
        repository.create_device(manufacturer="TECNO", brand="TECNO", family="W",
                                variant="W4 underscore", model_code="TECNO_W4")
        repository.create_device(manufacturer="TECNO", brand="TECNO", family="W",
                                variant="W4 letter", model_code="TECNOxW4")
        from mobile_observatory import current_firmware as cf
        cf.build(self.db)
        self.service = ObservatoryService(
            self.db, Path(self.temp.name) / "local.sqlite", demonstration=True)
        self.addCleanup(self.service.local.close)

    def totals(self, query: str) -> dict:
        return self.service.search_payload({"q": [query]})["totals"]

    def catalogue_size(self) -> int:
        return self.service.devices_page({"limit": ["500"]}).total

    def test_a_lone_underscore_does_not_match_the_whole_catalogue(self) -> None:
        """The headline measurement: 865 of 865 devices reported as matching `_`."""
        self.assertLess(self.totals("_")["device"], self.catalogue_size())

    def test_a_lone_percent_matches_only_a_literal_percent(self) -> None:
        self.assertEqual(self.totals("%")["device"], 0)
        self.assertEqual(self.totals("%")["chip"], 0)
        self.assertEqual(self.totals("%")["release"], 0)

    def test_an_underscore_in_a_model_code_is_findable(self) -> None:
        """The reader-facing half: before the fix, searching for a model code that
        contains `_` could not distinguish it from any other character."""
        self.assertEqual(self.totals("TECNO_W4")["device"], 1)
        rows = self.service.devices_page({"q": ["TECNO_W4"]}).items
        self.assertEqual([row["model"] for row in rows], ["TECNO_W4"])

    def test_the_wildcard_sibling_is_excluded(self) -> None:
        """TECNOxW4 differs from TECNO_W4 only where the wildcard was."""
        rows = self.service.devices_page({"q": ["TECNO_W4"]}).items
        self.assertNotIn("TECNOxW4", [row["model"] for row in rows])

    def test_ordinary_search_is_unaffected(self) -> None:
        """Escaping must narrow only the wildcard cases. A fix that also changed
        normal matching would be a different defect wearing this one's fix."""
        rows = self.service.devices_page({"q": ["SM-S931B"]}).items
        self.assertEqual([row["model"] for row in rows], ["SM-S931B"])
        self.assertEqual(self.totals("SM-S931B")["device"], 1)

    def test_every_filter_key_escapes_too_not_only_q(self) -> None:
        """`q` was the reported symptom; `maker`, `model` and the rest build their
        clauses from the same helper and had the same flaw."""
        self.assertEqual(self.service.devices_page({"model": ["%"]}).total, 0)
        self.assertEqual(self.service.devices_page({"maker": ["%"]}).total, 0)
        self.assertEqual([row["model"] for row in
                          self.service.devices_page({"model": ["TECNO_W4"]}).items],
                         ["TECNO_W4"])

    def test_other_paginated_routes_escape_as_well(self) -> None:
        """Routes whose LIKE clauses were written out separately from _sql_filters."""
        for name in ("releases_page", "source_records_page", "source_products_page",
                     "product_releases_page", "product_security_page", "security_page",
                     "chips_page"):
            with self.subTest(route=name):
                unfiltered = getattr(self.service, name)({}).total
                wildcard = getattr(self.service, name)({"q": ["%"]}).total
                self.assertLessEqual(wildcard, unfiltered)
                if unfiltered:
                    self.assertNotEqual(
                        wildcard, unfiltered,
                        f"{name} still reports every row as matching a bare '%'")


if __name__ == "__main__":
    unittest.main()
