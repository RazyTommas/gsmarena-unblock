"""The FTS5 index must not change what search finds. Only how fast it finds it.

A faster search that finds different things is a product change, not an
optimisation. So this compares the indexed path against the plain LIKE scan over
every query below and demands they AGREE -- on the total and on the rows.

The query list deliberately includes every value the wildcard-escaping round
measured (`_`, `%`, `TECNO_W4`, `5G`, `i3`), because those are the ones a
tokenizer is most likely to quietly change: four of the five are shorter than a
trigram, and FTS5 answers a too-short trigram query with NO ROWS rather than an
error.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mobile_observatory import search_index
from mobile_observatory.database import Database, like_clause, like_contains
from mobile_observatory.repository import CanonicalRepository

# Built on the REAL schema and read through the REAL v_device_region_history.
# An earlier draft of this file stood a look-alike view over look-alike tables,
# which would have agreed with itself while the production view did something
# else -- the same mistake as a bundle test whose fixture never reaches the paths
# it claims to cover. `create_device` is the batch's own entry point, so the
# manufacturer/brand/family/variant chain behind the view is the real one.
#
# Model codes and build ids are shaped like the live corpus's, and include the
# LIKE metacharacters that must stay literal plus the sibling-model overlap
# (`i3` / `i3Pro`) that makes a substring match differ from a prefix match.
DEVICES = [
    # manufacturer, brand, family, variant, model_code, codename
    ("Samsung Electronics", "Samsung", "Galaxy S25", "Galaxy S25 Ultra", "SM-S938B", "pa3q"),
    ("Samsung Electronics", "Samsung", "Galaxy S25", "Galaxy S25", "SM-S931B", "pa1q"),
    ("Samsung Electronics", "Samsung", "Galaxy A15", "Galaxy A15 5G", "SM-A155F", "a15x"),
    ("Transsion", "TECNO", "CAMON 50", "CAMON 50 Pro 5G", "TECNO_W4", "TECNO-W4"),
    ("Transsion", "TECNO", "SPARK i3", "SPARK i3", "TECNO_I3", "TECNO-i3"),
    ("Transsion", "TECNO", "SPARK i3", "SPARK i3 Pro", "TECNO_I3PRO", "TECNO-i3Pro"),
    ("Transsion", "itel", "A80", "A80 100%", "ITEL_A80", "itel-A80"),
    ("Xiaomi", "Xiaomi", "Redmi Note", 'Redmi "Note" 14', "MI_RN14", "ruby_x"),
    ("Transsion", "Infinix", "HOT 50", "HOT 50i", "IN_H50I", "X6831"),
]
# (release id, device index into DEVICES, target index, build id)
RELEASES = [
    ("fr1", 0, 0, "S938BXXU1AYA1"),
    ("fr2", 0, 1, "S938BXXU2AYB5"),
    ("fr3", 1, 0, "S931BXXSAFZH3"),
    ("fr4", 2, 2, "A155FXXU5CYA1"),
    ("fr5", 3, 2, "W4-H694ABCD-5G"),
    ("fr6", 4, 1, "i3-H123_QWE"),
    ("fr7", 5, 1, "i3Pro-H124%QWE"),
    ("fr8", 6, 0, "A80-ZZ_01"),
    ("fr9", 7, 2, 'RN14-"beta"-1'),
    ("fr10", 8, 0, "H50I-OTA-9"),
]
TARGETS = [("ILO", "Italy"), ("MID", "Middle East"), ("GLOBAL", "Global")]


def build_corpus(db: Database) -> None:
    """Populate the real tables behind v_device_region_history."""
    connection = db.connection
    repository = CanonicalRepository(db)
    model_ids = []
    for manufacturer, brand, family, variant, model_code, codename in DEVICES:
        model_id = repository.create_device(manufacturer=manufacturer, brand=brand,
                                           family=family, variant=variant, model_code=model_code)
        connection.execute("UPDATE hardware_models SET codename=? WHERE id=?",
                           (codename, model_id))
        model_ids.append(model_id)
    for index, (code, name) in enumerate(TARGETS):
        connection.execute(
            "INSERT INTO firmware_targets(id, vendor_namespace, target_code, target_kind, "
            "display_name, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (f"t{index}", "samsung.csc", code, "csc", name, "2026-01-01T00:00:00Z",
             "2026-01-01T00:00:00Z"))
    for release_id, device, target, build in RELEASES:
        connection.execute(
            "INSERT INTO firmware_releases(id, hardware_model_id, firmware_target_id, build_id, "
            "channel, first_observed_at, last_observed_at, created_at) "
            "VALUES (?,?,?,?,'stable','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',"
            "'2026-01-01T00:00:00Z')",
            (release_id, model_ids[device], f"t{target}", build))

QUERIES = [
    # the five the wildcard round measured
    "_", "%", "TECNO_W4", "5G", "i3",
    # literal metacharacters, which must never widen a match
    "%", "_", "\\", "\\_", "%5G%", "_W4", "100%", "A80-ZZ_01", "i3-H123_QWE", "H124%QWE",
    # quotes, which are FTS5 query syntax and must be read as text
    '"', '"Note"', 'RN14-"beta"', "'", "don't",
    # boundaries of the trigram gate
    "a", "ab", "abc", "i3P", "5G ",
    # substrings from the middle of a token, which is where FTS5 differs from LIKE
    "XXU", "938BXX", "FZH", "155FXXU5", "H694", "OTA", "50i", "ZZ",
    # whole tokens, prefixes and suffixes
    "Galaxy", "SM-", "SM-S938B", "S938BXXU1AYA1", "AYA1", "TECNO", "SPARK", "itel",
    "ILO", "MID", "GLOBAL", "Infinix", "Redmi",
    # case folding must match LIKE's COLLATE NOCASE
    "galaxy", "gAlAxY", "sm-s938b", "I3PRO", "ilo",
    # FTS5 operators, which must be literal text
    "AND", "OR", "NOT", "NEAR", "a OR b", "*", "S938*", "-", "(", "^",
    # absent
    "zzzzz", "Pixel", "nothing here",
]

SEARCHABLE = ("build_id", "model_code", "variant", "target_code")


class SearchIndexAgreesWithScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls.db = Database.migrated(Path(cls._tmp.name) / "corpus.sqlite")
        build_corpus(cls.db)
        search_index.build(cls.db.connection)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls._tmp.cleanup()

    def _scan(self, q):
        like = like_contains(q)
        sql = ("SELECT firmware_release_id FROM v_device_region_history WHERE "
               + " OR ".join(like_clause(column) for column in SEARCHABLE)
               + " ORDER BY firmware_release_id")
        return [row[0] for row in self.db.connection.execute(sql, [like] * 4)]

    def _indexed(self, q):
        narrowing = search_index.plan(self.db.connection, q, "firmware_release_id")
        if narrowing.certainly_empty:
            return [], narrowing
        like = like_contains(q)
        sql = ("SELECT firmware_release_id FROM v_device_region_history WHERE "
               + narrowing.clause + "("
               + " OR ".join(like_clause(column) for column in SEARCHABLE)
               + ") ORDER BY firmware_release_id")
        params = [*narrowing.params, *([like] * 4)]
        return [row[0] for row in self.db.connection.execute(sql, params)], narrowing

    def test_every_query_returns_exactly_what_the_scan_returns(self):
        mismatched = []
        for q in QUERIES:
            expected = self._scan(q)
            actual, narrowing = self._indexed(q)
            if expected != actual:
                mismatched.append((q, narrowing.path, expected, actual))
        self.assertEqual([], mismatched,
                         "the index changed WHICH releases are found, which is a product change")

    def test_the_comparison_is_not_vacuous(self):
        """A test where every query matches nothing proves nothing.

        This is the instrument check: the suite above can only catch a divergence
        if some queries actually match, some do not, and the index path is really
        taken rather than everything falling back to the scan.
        """
        matched = [q for q in QUERIES if self._scan(q)]
        empty = [q for q in QUERIES if not self._scan(q)]
        self.assertGreaterEqual(len(matched), 20, "too few queries match to prove agreement")
        self.assertGreaterEqual(len(empty), 5, "no non-matching queries in the comparison")
        paths = {search_index.plan(self.db.connection, q, "firmware_release_id").path
                 for q in QUERIES}
        self.assertIn("index", paths, "no query actually used the index")
        self.assertIn("scan", paths, "no query exercised the fallback")
        self.assertIn("empty", paths, "the short-circuit was never taken")

    def test_a_query_shorter_than_a_trigram_is_sent_to_the_scan(self):
        """The load-bearing gate. FTS5 answers a too-short query with NO ROWS.

        Without this, `5G` reads as "nothing matches" and is believed.
        """
        for q in ("_", "%", "a", "5G", "i3"):
            with self.subTest(q=q):
                narrowing = search_index.plan(self.db.connection, q, "firmware_release_id")
                self.assertEqual("scan", narrowing.path)
        self.assertTrue(self._scan("5G"), "the fixture must have a 5G match for this to mean anything")
        self.assertEqual(self._scan("5G"), self._indexed("5G")[0])

    def test_like_metacharacters_stay_literal_through_the_index(self):
        """The wildcard bug must not come back by a different door."""
        self.assertEqual(["fr5"], self._indexed("_W4")[0])
        self.assertEqual(["fr7"], self._indexed("H124%QWE")[0])
        self.assertEqual(["fr8"], self._indexed("A80-ZZ_01")[0])
        # `%` and `_` alone must not match everything
        every = [r[0] for r in RELEASES]
        for q in ("%", "_"):
            with self.subTest(q=q):
                self.assertNotEqual(sorted(every), sorted(self._indexed(q)[0]))

    def test_fts5_query_syntax_in_the_query_is_read_as_text(self):
        for q in ('"Note"', "AND", "OR", "NEAR", "S938*", "a OR b", "^"):
            with self.subTest(q=q):
                self.assertEqual(self._scan(q), self._indexed(q)[0])


class StalenessIsDetected(unittest.TestCase):
    """A stale index that looks fresh is worse than a scan."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database.migrated(Path(self._tmp.name) / "corpus.sqlite")
        self.addCleanup(self.db.close)
        build_corpus(self.db)
        search_index.build(self.db.connection)

    def _add_release(self, release_id: str, build: str) -> None:
        model_id = self.db.connection.execute(
            "SELECT id FROM hardware_models WHERE model_code='SM-S938B'").fetchone()[0]
        self.db.connection.execute(
            "INSERT INTO firmware_releases(id, hardware_model_id, firmware_target_id, build_id, "
            "channel, first_observed_at, last_observed_at, created_at) "
            "VALUES (?,?,'t0',?,'beta','2026-02-01T00:00:00Z','2026-02-01T00:00:00Z',"
            "'2026-02-01T00:00:00Z')", (release_id, model_id, build))

    def test_a_fresh_index_reports_fresh(self):
        stale, why = search_index.staleness(self.db.connection)
        self.assertFalse(stale, why)

    def test_a_new_row_makes_the_index_stale_and_the_query_path_falls_back(self):
        connection = self.db.connection
        self._add_release("fr99", "S938BXXU9ZZZ9")
        stale, why = search_index.staleness(connection)
        self.assertTrue(stale, "a row added since the build must read as stale")
        self.assertIn("digest", why)
        # and the request path notices for 0.01ms and scans rather than lying
        narrowing = search_index.plan(connection, "ZZZ9", "firmware_release_id")
        self.assertEqual("scan", narrowing.path)
        self.assertIn("predates", narrowing.reason)
        # the scan still finds the new build, which is the point of falling back
        self.assertEqual(1, connection.execute(
            "SELECT count(*) FROM v_device_region_history WHERE "
            + like_clause("build_id"), (like_contains("ZZZ9"),)).fetchone()[0])

    def test_an_edit_that_keeps_the_row_count_is_still_caught_by_the_digest(self):
        """The case the cheap tripwire CANNOT see, which is why both exist."""
        connection = self.db.connection
        connection.execute("UPDATE hardware_models SET codename='renamed' "
                           "WHERE model_code='SM-S938B'")
        connection.execute("UPDATE firmware_releases SET build_id='S938BXXU1AYA2' WHERE id='fr1'")
        stale, why = search_index.staleness(connection)
        self.assertTrue(stale, "a same-count edit must still be detected")
        # the cheap tripwire is blind to it, and is not described as catching it
        self.assertEqual("index",
                         search_index.plan(connection, "S938", "firmware_release_id").path)

    def test_rebuilding_settles_it(self):
        connection = self.db.connection
        connection.execute("UPDATE firmware_releases SET build_id='S938BXXU1AYA2' WHERE id='fr1'")
        self.assertTrue(search_index.build(connection)["rebuilt"])
        self.assertFalse(search_index.staleness(connection)[0])

    def test_an_unchanged_basis_is_not_rebuilt(self):
        """Rewriting the index would put its whole size into the batch changeset."""
        again = search_index.build(self.db.connection)
        self.assertFalse(again["rebuilt"])
        self.assertEqual("basis unchanged", again["reason"])


if __name__ == "__main__":
    unittest.main()
