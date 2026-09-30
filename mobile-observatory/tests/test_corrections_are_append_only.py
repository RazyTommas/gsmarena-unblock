"""`source_data_corrections` says it is immutable. It has to actually be.

The table carries two BEFORE triggers that RAISE(ABORT, 'source corrections are
immutable'). `INSERT OR REPLACE` walked straight past them: SQLite performs a
REPLACE's implicit DELETE **without firing delete triggers** unless
`PRAGMA recursive_triggers` is on, and nothing here turns it on.

Measured on a copy of the live corpus before the fix: 6 of 7 stored refusals were
deleted and re-inserted by every batch, each time with a new random primary key
and a new `recorded_at`, for no change in what they said.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory.collectors import device_promotion
from mobile_observatory.database import Database

SCHEMA_SOURCES = (ROOT / "src" / "mobile_observatory", ROOT / "migrations", ROOT / "tools")


class TheTriggersAreNotBypassable(unittest.TestCase):
    def setUp(self):
        self.db = Database.migrated(":memory:")
        self.connection = self.db.connection
        self.addCleanup(self.db.close)

    def _record(self, when: str) -> None:
        device_promotion._record_unresolved(
            self.connection, product_id="p1", owner_id="d1", model_code="TECNO KN3",
            owner_name="SPARK Go 3", claimed_name="Pop X", now=when)

    def test_recording_the_same_refusal_twice_changes_nothing(self):
        self._record("2026-09-01T00:00:00Z")
        first = self.connection.execute(
            "SELECT id, recorded_at FROM source_data_corrections").fetchall()
        self.assertEqual(1, len(first))
        self._record("2026-09-30T23:59:59Z")
        second = self.connection.execute(
            "SELECT id, recorded_at FROM source_data_corrections").fetchall()
        self.assertEqual([tuple(row) for row in first], [tuple(row) for row in second],
                         "the refusal was rewritten: a new id or a moved recorded_at")

    def test_recorded_at_keeps_saying_when_it_was_FIRST_reached(self):
        """The reader's question is 'when did we conclude this', not 'when did the
        last batch run'."""
        self._record("2026-09-01T00:00:00Z")
        self._record("2026-09-30T23:59:59Z")
        self.assertEqual(
            "2026-09-01T00:00:00Z",
            self.connection.execute(
                "SELECT recorded_at FROM source_data_corrections").fetchone()[0])

    def test_the_id_is_derived_and_not_minted(self):
        """A random id makes the second recording a different row, which is how a
        REPLACE got past a table that forbids DELETE."""
        self._record("2026-09-01T00:00:00Z")
        stored = self.connection.execute("SELECT id FROM source_data_corrections").fetchone()[0]
        self.assertEqual(
            device_promotion._correction_id(
                "source_product", "p1", "model_code_claimed_by_a_differently_named_device"),
            stored)

    def test_the_trigger_really_does_forbid_a_plain_delete(self):
        """Otherwise the tests above guard a table with no guarantee to protect."""
        self._record("2026-09-01T00:00:00Z")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM source_data_corrections")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE source_data_corrections SET reason='x'")

    def test_and_that_INSERT_OR_REPLACE_would_have_slipped_past_it(self):
        """The measurement behind this whole file, asserted rather than asserted-in-prose.

        If a future SQLite fires delete triggers for a REPLACE, this fails and the
        source scan below becomes unnecessary -- which is worth being told.
        """
        self._record("2026-09-01T00:00:00Z")
        self.connection.execute(
            """INSERT OR REPLACE INTO source_data_corrections
                 (id, entity_type, entity_id, reason, before_json, after_json,
                  evidence_id, recorded_at)
               VALUES ('a-fresh-id','source_product','p1',
                       'model_code_claimed_by_a_differently_named_device',
                       '{}','{}',NULL,'2026-12-25T00:00:00Z')""")
        self.assertEqual(
            ("a-fresh-id", "2026-12-25T00:00:00Z"),
            tuple(self.connection.execute(
                "SELECT id, recorded_at FROM source_data_corrections").fetchone()),
            "REPLACE no longer bypasses the immutability trigger")


class NoWriterMayUseReplaceOnAnAppendOnlyTable(unittest.TestCase):
    """Fix the pattern, not the instance.

    This codebase has already paid for one bug living at five call sites. The
    immutability of these two tables is enforced by triggers that REPLACE does
    not fire, so the only durable guard is that no writer spells it that way.
    """

    APPEND_ONLY = ("source_data_corrections", "domain_events")

    def test_no_source_file_writes_an_append_only_table_with_replace(self):
        offenders = []
        for root in SCHEMA_SOURCES:
            for path in sorted(root.rglob("*.py")) + sorted(root.rglob("*.sql")):
                text = path.read_text(encoding="utf-8")
                collapsed = " ".join(text.split()).upper()
                for table in self.APPEND_ONLY:
                    for phrase in (f"INSERT OR REPLACE INTO {table.upper()}",
                                   f"REPLACE INTO {table.upper()}"):
                        if phrase in collapsed:
                            offenders.append(f"{path.relative_to(ROOT)}: {phrase}")
        self.assertEqual([], sorted(set(offenders)),
                         "REPLACE deletes the stored row without firing the delete trigger that "
                         "makes these tables append-only; use ON CONFLICT ... DO NOTHING")

    def test_the_tables_it_names_really_are_trigger_protected(self):
        """A guard over a list of tables that turns out not to be append-only
        would pass forever while protecting nothing."""
        db = Database.migrated(":memory:")
        self.addCleanup(db.close)
        for table in self.APPEND_ONLY:
            with self.subTest(table=table):
                triggers = [row[0] for row in db.connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name=?",
                    (table,))]
                self.assertTrue(any("no_delete" in name for name in triggers),
                                f"{table} has no delete-forbidding trigger: {triggers}")


if __name__ == "__main__":
    unittest.main()
