"""The corpus stores the four-character string 'null' where a date belongs.

Six observations from `xiaomi.community.firmware_tracker` carry it in
`$.data.release_date` -- `typeof` is `text`, and it is the only non-date value in
that field across all 96,319 observations. `json_extract` returned it faithfully,
`coalesce` treated it as present, so `observations.effective_at` never fell through
to `observed_at` and the word `null` was published as the effective date of six
builds. It propagated into `product_firmware_releases.vendor_released_at`, where
`releases_page`'s `released` column printed it with no guard at all.

WHAT THESE TESTS HAVE TO PROVE, beyond "the six rows are fixed":

  * the payload is NOT rewritten. The archive still says what the source said.
  * the rule is ONE rule. It had already been noticed twice and patched locally
    both times -- `nullif(pfr.vendor_released_at,'null')` in
    current_firmware.EVIDENCE_SQL and four more copies in server.py -- while the
    two places that DERIVE the value went on storing the sentinel. So one case
    asserts the sentinel guard has not come back as a sixth reader-side patch, and
    another asserts the SQL in migration 0031 is the literal text
    `source_dates.stated_date_sql` generates rather than a hand-copied lookalike.
  * the Python and SQL halves agree, checked by running both over the same values
    instead of by reading them.
  * it is a SHAPE test and not a sentinel list, so a seventh spelling of nothing is
    already covered while a real partial date is not silently widened into one.
"""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory.database import Database  # noqa: E402
from mobile_observatory.enrichment import promote_approved_product_observations  # noqa: E402
from mobile_observatory.integrity import check_corpus  # noqa: E402
from mobile_observatory.source_dates import (  # noqa: E402
    ISO_DATE_GLOB, STATED_DATE_FIELDS, stated_date, stated_date_sql)

NOW = "2026-01-01T00:00:00Z"

# Values chosen so each one distinguishes the shape rule from something weaker.
CASES = [
    ("2025-01-31", "2025-01-31"),                        # a date
    ("2021-08-17 03:53:18", "2021-08-17 03:53:18"),      # publish_date really looks like this
    ("2025-01-31T00:00:00Z", "2025-01-31T00:00:00Z"),    # and observed_at like this
    ("null", None),                                      # THE observed defect
    ("NULL", None),
    ("", None),
    ("N/A", None),                                       # covered without being listed
    ("unknown", None),
    ("2025-01", None),                                   # a PARTIAL date is not a date
    ("2025", None),
    (" 2025-01-31", None),                               # leading space: report, do not repair
    ("20250131", None),                                  # not widened into a date
]


class SharedRuleTest(unittest.TestCase):

    def test_python_and_sql_agree_on_every_case(self) -> None:
        """Run both, rather than reading both. Two halves of one rule that disagree is
        the failure this module exists to prevent, one layer up."""
        con = sqlite3.connect(":memory:")
        # The generated SQL names its operand twice, once in the test and once in the
        # result, so the value is bound twice. Counted from the emitted SQL rather
        # than hardcoded, so the test does not break the day the rule is reshaped.
        sql = stated_date_sql("?")
        bindings = sql.count("?")
        for value, expected in CASES:
            with self.subTest(value=value):
                self.assertEqual(expected, stated_date(value))
                self.assertEqual(expected, con.execute(
                    f"SELECT {sql}", (value,) * bindings).fetchone()[0])

    def test_a_non_string_in_a_date_field_is_absence(self) -> None:
        self.assertIsNone(stated_date(None))
        self.assertIsNone(stated_date(20250131))
        self.assertIsNone(stated_date(True))

    def test_the_migration_uses_the_generated_sql_and_not_a_copy_of_it(self) -> None:
        """One definition, verified. The migration cannot import Python, so the check is
        that its text CONTAINS what the Python function emits -- which is what stops
        the .sql becoming a second copy of the rule that drifts from this one."""
        sql = (ROOT / "migrations" / "0031_source_stated_date_rule.sql").read_text(encoding="utf-8")
        for field in STATED_DATE_FIELDS:
            expected = stated_date_sql(f"json_extract(payload_json,'{field}')")
            self.assertIn(expected, sql, f"migration 0031 does not apply the shared rule to {field}")

    def test_no_reader_side_sentinel_guard_has_come_back(self) -> None:
        """The pattern, not the instance. Five `nullif(<column>,'null')` guards had
        accumulated on READERS while the writers kept storing the sentinel; if one
        reappears, the rule has stopped being one rule again."""
        offenders = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            # Prose is allowed to NAME the pattern -- the docstrings above and in
            # source_dates.py explain the defect and would otherwise trip the guard
            # that exists to prevent it, which is a check failing on its own
            # explanation. So docstrings are skipped and comments are truncated; what
            # is scanned is code and the SQL inside it.
            docstring_lines = set()
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    doc = node.body[0] if node.body else None
                    if (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
                            and isinstance(doc.value.value, str)):
                        docstring_lines.update(range(doc.lineno, doc.end_lineno + 1))
            for number, line in enumerate(text.splitlines(), 1):
                if number in docstring_lines:
                    continue
                code = re.split(r"#|--", line, maxsplit=1)[0]
                if re.search(r"nullif\s*\([^)]*,\s*'null'\s*\)", code, re.I):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual([], offenders,
                         "read a date through source_dates, or fix it where it is written")


class GeneratedColumnTest(unittest.TestCase):

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        self.con = self.db.connection
        with self.con:
            self.con.execute(
                "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
                " VALUES('xiaomi.community.firmware_tracker','Tracker',NULL,'community',1,?)", (NOW,))
            self.con.execute(
                "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,"
                "parser_version,accepted_count) VALUES('r','xiaomi.community.firmware_tracker',"
                "?,'succeeded','p','1',0)", (NOW,))
            self.con.execute(
                "INSERT INTO artifacts VALUES('a','xiaomi.community.firmware_tracker','r',?,"
                "'application/json',NULL,?,'x',1)", ("a" * 64, NOW))

    def _observe(self, key: str, payload: dict, observed_at: str = NOW) -> str:
        with self.con:
            self.con.execute(
                "INSERT INTO observations VALUES(?,'xiaomi.community.firmware_tracker','r','a',"
                "'firmware_release',?,?,?,?,'valid',NULL)",
                (key, key, observed_at, json.dumps(payload),
                 ("%064d" % abs(hash(key)))[:64]))
        return key

    def test_the_sentinel_reads_as_absence_and_falls_through_to_the_capture_time(self) -> None:
        # The exact shape of the six live rows, source_key and all.
        key = self._observe(
            "bixi:Stable Beta:OS2.0.104.0.VOHCNXM:Recovery:CN:mix-flip-2-china",
            {"data": {"build": "OS2.0.104.0.VOHCNXM", "release_date": "null"}},
            observed_at="2026-09-13T10:55:00Z")
        self.assertEqual("2026-09-13T10:55:00Z", self.con.execute(
            "SELECT effective_at FROM observations WHERE id=?", (key,)).fetchone()[0])

    def test_the_captured_payload_is_not_rewritten(self) -> None:
        key = self._observe("k", {"data": {"release_date": "null"}})
        payload = self.con.execute(
            "SELECT payload_json FROM observations WHERE id=?", (key,)).fetchone()[0]
        self.assertEqual('{"data": {"release_date": "null"}}', payload)
        self.assertEqual("null", self.con.execute(
            "SELECT json_extract(payload_json,'$.data.release_date') FROM observations"
            " WHERE id=?", (key,)).fetchone()[0],
            "the archive must still say exactly what the source said")

    def test_a_real_stated_date_still_wins_over_the_capture_time(self) -> None:
        """The counterweight: a fix that discarded every stated date would pass every
        test above and lose 9,224 real dates."""
        key = self._observe("k", {"data": {"release_date": "2025-03-04"}})
        self.assertEqual("2025-03-04", self.con.execute(
            "SELECT effective_at FROM observations WHERE id=?", (key,)).fetchone()[0])

    def test_the_coalesce_precedence_survives_the_redefinition(self) -> None:
        cases = [
            ({"release_date": "null", "publish_date": "2024-02-02"}, "2024-02-02"),
            ({"release_date": "2024-01-01", "publish_date": "2024-02-02"}, "2024-01-01"),
            ({"release_date": None, "publish_date": None, "release_time": "2023-06-06"},
             "2023-06-06"),
            ({"release_date": "null", "publish_date": "null", "release_time": "null"}, NOW),
        ]
        for index, (data, expected) in enumerate(cases):
            with self.subTest(data=data):
                key = self._observe(f"k{index}", {"data": data})
                self.assertEqual(expected, self.con.execute(
                    "SELECT effective_at FROM observations WHERE id=?", (key,)).fetchone()[0])

    def test_the_index_still_covers_the_redefined_column(self) -> None:
        """Migration 0031 has to drop the index to drop the column. If it forgot to put
        it back, the source-records sort silently returns to a 96,319-row scan."""
        plan = [dict(row) for row in self.con.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM observations ORDER BY effective_at DESC")]
        self.assertTrue(any("observations_effective_at_idx" in str(row) for row in plan), plan)

    # -- the derived read model ---------------------------------------------

    def _approved_product(self) -> None:
        with self.con:
            self.con.execute("INSERT INTO source_products VALUES('p','Xiaomi','Redmi Pad 2',"
                             "'redmi pad 2','approved',NULL,?,?)", (NOW, NOW))
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES('i',"
                "'xiaomi.community.firmware_tracker','codename','taiko','taiko','p','approved',"
                "'x','1','high',?,?)", (NOW, NOW))

    def test_promotion_records_the_sentinel_as_absence(self) -> None:
        self._approved_product()
        key = self._observe("k", {"data": {"build": "OS2.0.102.0.VOVEUXM", "release_date": "null",
                                           "region_code": "EEA", "branch": "Stable",
                                           "model_code": "taiko_eea_global",
                                           "source_device_name": "Redmi Pad 2 EEA"}})
        with self.con:
            self.con.execute("INSERT INTO observation_product_links VALUES(?,'p','i','approved',?)",
                             (key, NOW))
        promote_approved_product_observations(self.con)
        self.assertEqual([(None,)], [tuple(r) for r in self.con.execute(
            "SELECT vendor_released_at FROM product_firmware_releases")])

    def test_promotion_keeps_a_real_stated_date(self) -> None:
        self._approved_product()
        key = self._observe("k", {"data": {"build": "B1", "release_date": "2025-05-06",
                                           "region_code": "EEA", "branch": "Stable",
                                           "model_code": "taiko_eea_global",
                                           "source_device_name": "Redmi Pad 2 EEA"}})
        with self.con:
            self.con.execute("INSERT INTO observation_product_links VALUES(?,'p','i','approved',?)",
                             (key, NOW))
        promote_approved_product_observations(self.con)
        self.assertEqual([("2025-05-06",)], [tuple(r) for r in self.con.execute(
            "SELECT vendor_released_at FROM product_firmware_releases")])

    # -- the integrity checks ------------------------------------------------

    def _finding(self, check: str):
        return next((f for f in check_corpus(self.con, deep=False) if f.check == check), None)

    def test_the_source_defect_is_reported_as_a_warning_and_attributed(self) -> None:
        self.assertIsNone(self._finding("source_states_a_non_date_where_a_date_belongs"))
        for index in range(6):
            self._observe(f"k{index}", {"data": {"release_date": "null"}})
        finding = self._finding("source_states_a_non_date_where_a_date_belongs")
        self.assertIsNotNone(finding)
        self.assertEqual(6, finding.count)
        # WARNING and not error: since the derived columns read it as absence, the
        # corpus no longer states anything false. It is reported because a source that
        # has started spelling absence is a change in that source worth seeing.
        self.assertEqual("warning", finding.severity)

    def test_a_json_null_is_not_reported_as_a_source_defect(self) -> None:
        """87,089 rows say nothing by saying nothing. Counting those would bury the 6
        that say nothing by writing a word, which is the only interesting case."""
        self._observe("k", {"data": {"release_date": None, "publish_date": None}})
        self._observe("k2", {"data": {"build": "B"}})
        self.assertIsNone(self._finding("source_states_a_non_date_where_a_date_belongs"))

    def test_a_derived_column_holding_a_non_date_is_an_error(self) -> None:
        """The planted defect for the rule itself: a write that bypassed source_dates.
        This is what the check exists to catch, because a source cannot cause it."""
        self.assertIsNone(self._finding("derived_date_column_holds_a_non_date"))
        self._approved_product()
        key = self._observe("k", {"data": {"build": "B1", "release_date": "2025-05-06",
                                           "region_code": "EEA", "branch": "Stable",
                                           "model_code": "taiko_eea_global",
                                           "source_device_name": "Redmi Pad 2 EEA"}})
        with self.con:
            self.con.execute("INSERT INTO observation_product_links VALUES(?,'p','i','approved',?)",
                             (key, NOW))
        promote_approved_product_observations(self.con)
        self.assertIsNone(self._finding("derived_date_column_holds_a_non_date"))
        with self.con:
            self.con.execute("UPDATE product_firmware_releases SET vendor_released_at='null'")
        finding = self._finding("derived_date_column_holds_a_non_date")
        self.assertIsNotNone(finding)
        self.assertEqual(1, finding.count)
        self.assertEqual("error", finding.severity)

    def test_the_derived_check_also_watches_the_generated_column(self) -> None:
        """Planted by reverting migration 0031's redefinition -- the one defect that
        would otherwise reintroduce the original bug silently, because reverting it
        leaves every Python-side guard intact and still publishes `null` as a date."""
        with self.con:
            self.con.execute("DROP INDEX observations_effective_at_idx")
            self.con.execute("ALTER TABLE observations DROP COLUMN effective_at")
            self.con.execute(
                "ALTER TABLE observations ADD COLUMN effective_at TEXT GENERATED ALWAYS AS ("
                "coalesce(json_extract(payload_json,'$.data.release_date'),"
                "json_extract(payload_json,'$.data.publish_date'),"
                "json_extract(payload_json,'$.data.release_time'),observed_at)) VIRTUAL")
            self.con.execute("CREATE INDEX observations_effective_at_idx"
                             " ON observations(effective_at DESC, observed_at DESC)")
        self._observe("k", {"data": {"release_date": "null"}})
        self.assertEqual("null", self.con.execute(
            "SELECT effective_at FROM observations WHERE id='k'").fetchone()[0],
            "precondition: the reverted column republishes the sentinel")
        finding = self._finding("derived_date_column_holds_a_non_date")
        self.assertIsNotNone(finding)
        self.assertEqual("error", finding.severity)

    def test_the_iso_glob_is_what_both_halves_are_built_from(self) -> None:
        self.assertEqual("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]", ISO_DATE_GLOB)


if __name__ == "__main__":
    unittest.main()
