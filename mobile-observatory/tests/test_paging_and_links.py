"""Rows-per-page, batched date evidence, and source-record linkability.

All three shipped because a reader hit them, not because a test did: the page size
was hardwired, every source-record identity was inert even when it resolved to a
real device, and a 100-row releases page fanned out into ~200 extra queries.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory.server import _pagination  # noqa: E402
from mobile_observatory.source_corrections import (  # noqa: E402
    firmware_date_evidence, firmware_date_evidence_many)


class PaginationTest(unittest.TestCase):
    def test_default_is_one_hundred(self):
        self.assertEqual(_pagination({})[0], 100)

    def test_reader_choice_is_honoured_up_to_the_cap(self):
        for asked in (50, 100, 250, 500):
            self.assertEqual(_pagination({"limit": [str(asked)]})[0], asked)

    def test_cap_holds_above_five_hundred(self):
        """Uncapped, one mistyped query string renders the whole table."""
        self.assertEqual(_pagination({"limit": ["5000"]})[0], 500)

    def test_nonsense_limit_falls_back_rather_than_raising(self):
        self.assertEqual(_pagination({"limit": ["abc"]})[0], 100)

    def test_negative_and_zero_are_clamped_to_one(self):
        self.assertEqual(_pagination({"limit": ["0"]})[0], 1)
        self.assertEqual(_pagination({"limit": ["-5"]})[0], 1)


class BatchedDateEvidenceTest(unittest.TestCase):
    """The batch must agree with the per-row function it replaced."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
            CREATE TABLE source_data_corrections(entity_type TEXT, entity_id TEXT,
                before_json TEXT, after_json TEXT, evidence_id TEXT, recorded_at TEXT, reason TEXT);
            CREATE TABLE firmware_release_evidence(firmware_release_id TEXT, evidence_id TEXT);
            CREATE TABLE evidence(id TEXT, observation_id TEXT, artifact_id TEXT);
            CREATE TABLE observations(id TEXT, payload_json TEXT);
        """)
        # r1: a recorded correction, which must win.
        self.c.execute("INSERT INTO source_data_corrections VALUES('firmware_release','r1','{}',"
                       "'{\"build_derived_month\":\"2026-03\"}','ev-corr','2026-03-02T00:00:00Z',"
                       "'samsung_build_month_not_release_date')")
        # r2: no correction, month comes from an observation.
        self.c.execute("INSERT INTO firmware_release_evidence VALUES('r2','ev2')")
        self.c.execute("INSERT INTO evidence VALUES('ev2','obs2','art2')")
        self.c.execute("INSERT INTO observations VALUES('obs2','{\"data\":{\"build_derived_month\":\"2026-05\"}}')")
        # r3: evidence exists but states no month -- must stay absent, not become null.
        self.c.execute("INSERT INTO firmware_release_evidence VALUES('r3','ev3')")
        self.c.execute("INSERT INTO evidence VALUES('ev3','obs3','art3')")
        self.c.execute("INSERT INTO observations VALUES('obs3','{\"data\":{}}')")

    def test_batch_matches_per_row(self):
        ids = ["r1", "r2", "r3", "r4-does-not-exist"]
        many = firmware_date_evidence_many(self.c, ids)
        for release_id in ids:
            self.assertEqual(firmware_date_evidence(self.c, release_id),
                             many.get(release_id, {}), release_id)

    def test_correction_wins_over_observation(self):
        self.c.execute("INSERT INTO firmware_release_evidence VALUES('r1','ev1')")
        self.c.execute("INSERT INTO evidence VALUES('ev1','obs1','art1')")
        self.c.execute("INSERT INTO observations VALUES('obs1','{\"data\":{\"build_derived_month\":\"1999-01\"}}')")
        result = firmware_date_evidence_many(self.c, ["r1"])["r1"]
        self.assertEqual(result["build_derived_month"], "2026-03")
        self.assertEqual(result["evidence_id"], "ev-corr")

    def test_a_release_with_no_month_is_absent_not_null(self):
        """An absent key leaves the row's own value alone; a null would overwrite it."""
        self.assertNotIn("r3", firmware_date_evidence_many(self.c, ["r3"]))

    def test_empty_input_runs_no_query(self):
        self.assertEqual(firmware_date_evidence_many(self.c, []), {})

    def test_more_ids_than_one_chunk(self):
        """Chunking must not drop or duplicate rows."""
        ids = [f"bulk{n}" for n in range(1200)]
        self.c.executemany("INSERT INTO firmware_release_evidence VALUES(?,?)",
                           [(i, f"ev-{i}") for i in ids])
        self.c.executemany("INSERT INTO evidence VALUES(?,?,?)",
                           [(f"ev-{i}", f"obs-{i}", "a") for i in ids])
        self.c.executemany("INSERT INTO observations VALUES(?,?)",
                           [(f"obs-{i}", '{"data":{"build_derived_month":"2026-01"}}') for i in ids])
        found = firmware_date_evidence_many(self.c, ids)
        self.assertEqual(len(found), 1200)


if __name__ == "__main__":
    unittest.main()
