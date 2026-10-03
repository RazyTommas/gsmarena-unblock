"""`identity_resolution_rationales` was the one table a revert could not see.

The session extension identifies a row by its declared PRIMARY KEY, so a table
without one is not recorded AND NOT COMPLAINED ABOUT -- its changes are simply
absent from the diff. This table had no key (migration 0030 explains at length
why its uniqueness had to be an EXPRESSION index), so reverting a batch rolled
everything back except the 721 rows recording WHY each automated identity
conclusion was reached, and `check_corpus` carried
`table_absent_from_every_changeset` as a standing warning.

Migration 0033 materialises 0030's expression into a stored `subject` column and
makes `(subject, rule)` the primary key. Everything that could quietly go wrong
in that rebuild is asserted here against a real database rather than reasoned
about:

  * the table is tracked now, and its rows really do travel in the changeset --
    an insert, an update and a delete, because a key that works for one row shape
    and not another is the shape nobody notices;
  * the collision set did not move. The old expression keyed a product-scoped row
    on `product:<id>`, and a naive `UNIQUE(identity_id, rule)` would have let those
    rows append one copy per run, which is the regression 0030 was written to
    prevent;
  * STRICT, the FK + CASCADE, the `outcome` CHECK and the `json_valid` CHECK all
    survived a rebuild that recreated the table from scratch;
  * `subject` is derived by the DATABASE's CHECK and not trusted from the writer,
    and `identity_rationales.record_rationale` is the only writer -- scanned for,
    in the shape of tests/test_corrections_are_append_only.py, because this
    codebase has already paid for one rule living at five call sites.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import changesets  # noqa: E402
from mobile_observatory import identity_rationales  # noqa: E402
from mobile_observatory.database import Database  # noqa: E402
from mobile_observatory.identity_rationales import record_rationale, subject_key  # noqa: E402

TABLE = "identity_resolution_rationales"
NOW = "2026-01-01T00:00:00Z"
RULE = "xiaomi_codename_stem_google_play_corroborated"

# The same three roots tests/test_corrections_are_append_only.py scans, for the
# same reason: a writer can be added in application code, in a migration or in a
# tool, and all three reach this table.
SCHEMA_SOURCES = (ROOT / "src" / "mobile_observatory", ROOT / "migrations", ROOT / "tools")

# `INSERT [OR <verb>] INTO identity_resolution_rationales`, with a word boundary so
# migration 0030's and 0033's `..._rebuilt` rebuild targets are not matched -- they
# write a table that does not exist yet by this name, which is not a second writer.
WRITE_PATTERN = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+" + TABLE.upper() + r"\b")

# The only file allowed to contain it, relative to the repository root.
THE_ONLY_WRITER = Path("src") / "mobile_observatory" / "identity_rationales.py"


class RationaleFixture(unittest.TestCase):
    """A migrated corpus with one product and one identity to hang rows on."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database.migrated(Path(self._tmp.name) / "corpus.sqlite")
        self.addCleanup(self.db.close)
        self.con = self.db.connection
        with self.con:
            self.con.execute(
                "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
                " VALUES('archive','Archive',NULL,'primary',1,?)", (NOW,))
            self.con.execute("INSERT INTO source_products VALUES(?,?,?,?,?,NULL,?,?)",
                             ("p1", "Xiaomi", "Redmi 1", "redmi 1", "proposed", NOW, NOW))
            self.con.execute("INSERT INTO source_products VALUES(?,?,?,?,?,NULL,?,?)",
                             ("p-bare", "itel", "ACE2N", "ace2n", "proposed", NOW, NOW))
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES(?,'archive','codename',?,?,?,"
                "'proposed','deterministic_source_catalog','1','medium',?,?)",
                ("i1", "cactus", "cactus", "p1", NOW, NOW))

    def _record(self, *, identity_id: str | None, product_id: str, rule: str = RULE,
                rationale: str = "because the captured catalogue said so",
                outcome: str = "approved", decided_at: str = NOW) -> None:
        with self.con:
            record_rationale(self.con, identity_id=identity_id, rule=rule, rule_version="1",
                             product_id=product_id, outcome=outcome, reason="a_reason",
                             rationale=rationale, evidence_json=json.dumps([{"source": "x"}]),
                             decided_at=decided_at)

    def _count(self, **where) -> int:
        clause = " AND ".join(
            f"{column} IS ?" for column in where) or "1"
        return self.con.execute(
            f"SELECT count(*) FROM {TABLE} WHERE {clause}", tuple(where.values())).fetchone()[0]


class TheTableIsVisibleToAChangesetNow(RationaleFixture):
    def setUp(self) -> None:
        support = changesets.session_support()
        if not support.available:
            self.skipTest(support.reason)
        super().setUp()

    def test_it_is_no_longer_named_as_a_blind_spot(self) -> None:
        self.assertNotIn(TABLE, changesets.tables_invisible_to_a_changeset(self.con),
                         "the PRIMARY KEY migration 0033 adds is missing or was not applied")

    def test_the_primary_key_is_subject_then_rule(self) -> None:
        """Order matters: it is the index a lookup by subject would use, and
        `pk` here is what the session extension reads to identify a row."""
        self.assertEqual(
            {"subject": 1, "rule": 2},
            {row["name"]: row["pk"] for row in self.con.execute(
                f'PRAGMA table_info("{TABLE}")') if row["pk"]})

    def test_an_insert_an_update_and_a_delete_all_round_trip_through_invert(self) -> None:
        """All three row shapes, because a key that handles one is not proof.

        The digest compares CONTENT: two SQLite files holding the same rows differ
        byte for byte, so a file hash would report a correct revert as a failure.
        """
        self._record(identity_id="i1", product_id="p1")
        self._record(identity_id=None, product_id="p-bare",
                     rule="unresolvable_on_captured_evidence",
                     outcome="adjudicated_unresolvable")
        before = changesets.table_digests(self.con, tables=[TABLE])[TABLE]

        with changesets.record_changes(self.con) as recording:
            # an INSERT, through the only writer there is
            self._record(identity_id=None, product_id="p1", rule="a_third_rule",
                         outcome="refused")
            with self.con:
                # an UPDATE of an existing row
                self.con.execute(f"UPDATE {TABLE} SET rationale='edited' WHERE subject='i1'")
                # and a DELETE of the product-scoped shape
                self.con.execute(
                    f"DELETE FROM {TABLE} WHERE subject='product:p-bare'")

        during = changesets.table_digests(self.con, tables=[TABLE])[TABLE]
        self.assertNotEqual(before, during, "the write did not actually change anything")
        self.assertEqual({"insert": 1, "update": 1, "delete": 1},
                         changesets.summarise(recording.changeset).get(TABLE),
                         "the rows did not travel in the changeset")

        changesets.apply_changeset(self.con, changesets.invert(recording.changeset))
        self.assertEqual(before, changesets.table_digests(self.con, tables=[TABLE])[TABLE],
                         "inverting did not restore the table")


class TheCollisionSetDidNotMove(RationaleFixture):
    """0030's uniqueness semantics, re-asserted against the new key.

    A key of `(identity_id, rule)` would pass every test that only ever uses an
    identity-keyed row, and would silently append a row per run for the two
    products that have no registry identity at all.
    """

    def test_an_identity_keyed_row_is_rewritten_in_place(self) -> None:
        for n in range(4):
            self._record(identity_id="i1", product_id="p1", rationale=f"pass {n}")
        self.assertEqual(1, self._count(subject="i1", rule=RULE))
        self.assertEqual("pass 3", self.con.execute(
            f"SELECT rationale FROM {TABLE} WHERE subject='i1'").fetchone()[0])

    def test_a_product_scoped_row_is_rewritten_in_place_and_not_appended(self) -> None:
        """The measurement tests/test_unresolvable_adjudication.py makes for the
        two keyless products, made again against the primary key that replaced the
        index it relied on."""
        for n in range(100):
            self._record(identity_id=None, product_id="p-bare",
                         rule="unresolvable_on_captured_evidence",
                         outcome="adjudicated_unresolvable", rationale=f"pass {n}")
        self.assertEqual(1, self._count(product_id="p-bare"))
        self.assertEqual("product:p-bare", self.con.execute(
            f"SELECT subject FROM {TABLE} WHERE product_id='p-bare'").fetchone()[0])

    def test_two_rules_about_one_subject_are_two_rows(self) -> None:
        """`rule` is in the key, so this must NOT collapse."""
        self._record(identity_id="i1", product_id="p1", rule="rule_a")
        self._record(identity_id="i1", product_id="p1", rule="rule_b")
        self.assertEqual(2, self._count(subject="i1"))

    def test_a_plain_insert_of_a_duplicate_subject_and_rule_is_refused(self) -> None:
        """Without OR REPLACE the key must still refuse the second row, including
        for the NULL-identity shape a UNIQUE index treats as distinct."""
        self._record(identity_id=None, product_id="p-bare")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.con:
                self.con.execute(
                    f"INSERT INTO {TABLE} (identity_id,rule,rule_version,product_id,outcome,"
                    f" reason,rationale,evidence_json,decided_at,subject)"
                    f" VALUES(NULL,?, '1','p-bare','refused','r','r','[]',?,?)",
                    (RULE, NOW, "product:p-bare"))

    def test_the_redundant_expression_index_is_gone(self) -> None:
        """The primary key IS a unique index over (subject, rule); keeping
        0030's expression index would be the same constraint twice, and a revert
        colliding on it would be reported as CONSTRAINT rather than CONFLICT."""
        indexes = {row["name"]: row["unique"] for row in self.con.execute(
            f"SELECT name, \"unique\" FROM pragma_index_list('{TABLE}')")}
        self.assertNotIn("identity_rationale_subject_idx", indexes)
        unique = [name for name, is_unique in indexes.items() if is_unique]
        self.assertEqual(1, len(unique), f"expected exactly one unique index, got {unique}")
        self.assertEqual(
            ["subject", "rule"],
            [row["name"] for row in self.con.execute(
                f"SELECT name FROM pragma_index_info('{unique[0]}') ORDER BY seqno")])

    def test_the_two_lookup_indexes_survived_the_rebuild(self) -> None:
        self.assertEqual(
            {"idx_identity_rationale_outcome", "idx_identity_rationale_product"},
            {row["name"] for row in self.con.execute(
                f"SELECT name FROM pragma_index_list('{TABLE}') WHERE origin='c'")})


class TheConstraintsSurvivedTheRebuild(RationaleFixture):
    """A rebuild recreates the table from scratch, so every constraint is a line
    somebody could have dropped. Each is proven by a write it must refuse."""

    def _insert(self, **values) -> None:
        row = {"identity_id": "i1", "rule": RULE, "rule_version": "1", "product_id": "p1",
               "outcome": "approved", "reason": "r", "rationale": "r",
               "evidence_json": "[]", "decided_at": NOW, "subject": "i1"}
        row.update(values)
        columns = ",".join(row)
        marks = ",".join("?" * len(row))
        with self.con:
            self.con.execute(f"INSERT INTO {TABLE} ({columns}) VALUES({marks})",
                             tuple(row.values()))

    def test_the_subject_check_refuses_a_subject_the_row_does_not_derive(self) -> None:
        """The whole reason the key is safe to add: a future writer cannot store a
        row under a subject that is not its subject."""
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self._insert(subject="product:p1")  # right shape, wrong row: i1 has an identity
        self.assertIn("CHECK", str(caught.exception))
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(identity_id=None, subject="p1")  # missing the 'product:' prefix
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(identity_id=None, product_id="p-bare", subject="i1")

    def test_the_derivation_the_helper_computes_is_the_one_the_database_checks(self) -> None:
        """If these ever disagree every write refuses, which is safe but opaque.
        Asserted here so the failure names the divergence instead."""
        for identity_id, product_id in ((None, "p1"), ("i1", "p1"), ("", "p1"),
                                        (None, "product:p1")):
            with self.subTest(identity_id=identity_id, product_id=product_id):
                self.assertEqual(
                    self.con.execute("SELECT ifnull(?,'product:'||?)",
                                     (identity_id, product_id)).fetchone()[0],
                    subject_key(identity_id, product_id))

    def test_the_outcome_check_still_refuses_an_unknown_outcome(self) -> None:
        self._insert(outcome="adjudicated_unresolvable", rule="rule_ok")  # a known one works
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(outcome="probably_fine", rule="rule_bad")

    def test_the_json_valid_check_still_refuses_evidence_that_is_not_json(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(evidence_json="{not json")

    def test_the_table_is_still_strict(self) -> None:
        """A BLOB and not an integer, which is a measurement rather than a choice.

        STRICT is weaker over a TEXT column than the word suggests: measured on
        SQLite 3.45.1, `rule_version=1` is ACCEPTED and stored as the text '1',
        and 1.5 as '1.5' -- the lossless-conversion rule. A BLOB is the value it
        actually refuses, so a BLOB is what proves the declaration survived. An
        earlier version of this test asserted the integer and failed, which is the
        only reason this is known.
        """
        self.assertIn("STRICT", self.con.execute(
            "SELECT sql FROM sqlite_schema WHERE name=?", (TABLE,)).fetchone()[0].upper())
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self._insert(rule_version=b"\x00binary")
        self.assertIn("cannot store BLOB value in TEXT column", str(caught.exception))

    def test_the_foreign_key_still_cascades_from_the_identity_registry(self) -> None:
        self._record(identity_id="i1", product_id="p1")
        self.assertEqual(1, self._count(subject="i1"))
        with self.con:
            self.con.execute("DELETE FROM source_identity_registry WHERE id='i1'")
        self.assertEqual(0, self._count(subject="i1"),
                         "ON DELETE CASCADE did not survive the rebuild")

    def test_a_rationale_naming_no_real_identity_is_refused(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(identity_id="i-does-not-exist", subject="i-does-not-exist")


class OnlyOneWriterOfThisTable(unittest.TestCase):
    """Fix the pattern, not the instance.

    The INSERT was spelled byte-identically in identity_backfill's stem rule and
    adjudication's `_record` before 0033, and the new `subject` column is a value
    both would have had to derive separately -- the exact shape of the bug that
    lived at five call sites in this repository's history.
    """

    def _offenders(self) -> list[str]:
        offenders = []
        for root in SCHEMA_SOURCES:
            for path in sorted(root.rglob("*.py")) + sorted(root.rglob("*.sql")):
                relative = path.relative_to(ROOT)
                if relative == THE_ONLY_WRITER:
                    continue
                collapsed = " ".join(path.read_text(encoding="utf-8").split()).upper()
                if WRITE_PATTERN.search(collapsed):
                    offenders.append(str(relative))
        return sorted(set(offenders))

    def test_no_other_source_file_writes_the_table_directly(self) -> None:
        self.assertEqual(
            [], self._offenders(),
            f"{TABLE} carries a derived PRIMARY KEY column (subject); every writer must go "
            f"through identity_rationales.record_rationale so the derivation lives in one place")

    def test_the_file_it_exempts_really_is_the_writer(self) -> None:
        """Otherwise the guard above passes forever while protecting nothing --
        including on the day somebody renames the helper."""
        collapsed = " ".join((ROOT / THE_ONLY_WRITER).read_text(encoding="utf-8").split()).upper()
        self.assertTrue(WRITE_PATTERN.search(collapsed),
                        f"{THE_ONLY_WRITER} no longer contains the INSERT this guard exempts")

    def test_both_rules_reach_the_table_through_the_helper(self) -> None:
        """The scan proves nobody spells the INSERT. This proves the two writers
        that used to spell it now call the helper, rather than having stopped
        recording anything at all."""
        for module in ("identity_backfill", "adjudication"):
            with self.subTest(module=module):
                text = (ROOT / "src" / "mobile_observatory" / f"{module}.py").read_text(
                    encoding="utf-8")
                self.assertIn("record_rationale", text)
        self.assertIs(
            identity_rationales.record_rationale,
            __import__("mobile_observatory.adjudication", fromlist=["x"]).record_rationale)


if __name__ == "__main__":
    unittest.main()
