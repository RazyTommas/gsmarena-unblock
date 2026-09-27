"""Every invariant must be able to fail.

tests/test_schema.py asserts PRAGMA foreign_key_check == [] against
Database.migrated(), which defaults to ":memory:" -- a freshly migrated,
zero-row database. There is nothing there to violate, so the assertion cannot
fail, and the one test in the repo that looked like a corpus integrity check
had never examined a row of the corpus. tools/validate_product_batch.py and
tools/validate_security_batch.py share the shape.

So this file does not test that a clean corpus is clean. For each check it
plants the specific violation that check exists to find, and asserts the check
finds it -- and, separately, that a clean corpus reports it absent. A check
that only ever passes is indistinguishable from one that is not wired up.

The last test runs the whole invariant set against the REAL corpus when one is
present, and skips when it is not, so a developer without the 224MB file is not
blocked while CI on a machine that has it still exercises the thing that
matters.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.integrity import check_corpus, summarise  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402

LIVE_CORPUS = ROOT / ".observatory-data" / "corpus.sqlite"


class InvariantsCanFailTest(unittest.TestCase):
    """One planted violation per check."""

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.con = self.db.connection

    def _checks(self) -> dict[str, int]:
        return {f.check: f.count for f in check_corpus(self.con)}

    def test_a_clean_seeded_corpus_reports_no_errors(self) -> None:
        """The baseline the other tests move away from."""
        errors = [f for f in check_corpus(self.con) if f.severity == "error"]
        self.assertEqual([], [f.check for f in errors], summarise(check_corpus(self.con)))

    def test_detects_firmware_observed_but_not_served(self) -> None:
        """The 156-device falsehood: evidence exists, the read path cannot see it."""
        self.assertNotIn("firmware_observed_but_not_served", self._checks())
        # Emptying the projection is exactly the state of a corpus whose
        # nightly rebuild failed, or was never run after a restore.
        self.con.execute("DELETE FROM device_current_firmware")
        found = self._checks()
        self.assertIn("firmware_observed_but_not_served", found)
        self.assertGreater(found["firmware_observed_but_not_served"], 0)

    def test_detects_a_device_served_from_both_layers(self) -> None:
        self.assertNotIn("device_served_from_both_layers", self._checks())
        row = self.con.execute(
            "SELECT hardware_model_id,target_key,channel FROM device_current_firmware LIMIT 1").fetchone()
        # A real evidence-layer release, so the row passes the table's own CHECK
        # that exactly one release pointer is set -- the violation under test is
        # the cross-layer overlap, not a malformed row.
        from test_current_firmware_projection import _evidence_layer_device
        _evidence_layer_device(self.db, product="p-overlap", model="OVERLAP-1")
        release = self.con.execute("SELECT id FROM product_firmware_releases LIMIT 1").fetchone()[0]
        self.con.execute(
            """INSERT INTO device_current_firmware
               (hardware_model_id,target_key,channel,fact_layer,firmware_release_id,
                product_firmware_release_id,source_id,build_id,android_version,android_major,
                security_patch_level,security_patch_level_source_id,effective_at,
                effective_at_basis,latest_basis,release_count)
               VALUES(?,?,?,'evidence',NULL,?,NULL,'B',NULL,NULL,NULL,NULL,NULL,
                      'not_captured','observation_order_only',1)""",
            (row["hardware_model_id"], row["target_key"] + "-ALT", row["channel"], release))
        self.assertIn("device_served_from_both_layers", self._checks())

    def test_detects_a_malformed_or_unattributed_patch_level(self) -> None:
        self.assertNotIn("malformed_or_unattributed_patch_level", self._checks())
        self.con.execute(
            "UPDATE device_current_firmware SET security_patch_level='last Tuesday' "
            "WHERE rowid=(SELECT rowid FROM device_current_firmware LIMIT 1)")
        self.assertIn("malformed_or_unattributed_patch_level", self._checks())

    def test_detects_run_accounting_mismatch(self) -> None:
        self.con.execute(
            "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,parser_version,"
            "accepted_count,rejected_count) SELECT 'phantom-run',id,'2026-01-01T00:00:00Z','succeeded',"
            "'p','1',999,0 FROM sources LIMIT 1")
        found = self._checks()
        self.assertIn("run_accounting_mismatch", found)
        self.assertGreaterEqual(found["run_accounting_mismatch"], 1)

    def test_detects_a_source_product_naming_an_unknown_manufacturer(self) -> None:
        self.con.execute(
            "INSERT INTO source_products VALUES('p-ghost','Nokia Of Nowhere','Ghost','ghost',"
            "'proposed',NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        found = self._checks()
        self.assertIn("source_product_unknown_manufacturer", found)
        self.assertGreaterEqual(found["source_product_unknown_manufacturer"], 1)

    def test_detects_foreign_key_orphans(self) -> None:
        self.assertNotIn("foreign_key_orphans", self._checks())
        # FKs are enforced on this connection, so an orphan has to be made the
        # way the ingest path actually makes one: with enforcement off.
        self.con.execute("PRAGMA foreign_keys=OFF")
        try:
            self.con.execute(
                "INSERT INTO firmware_releases(id,hardware_model_id,build_id,first_observed_at,"
                "last_observed_at,created_at) VALUES('orphan','no-such-device','B',"
                "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        finally:
            self.con.execute("PRAGMA foreign_keys=ON")
        self.assertIn("foreign_key_orphans", self._checks())


class LiveCorpusInvariantsTest(unittest.TestCase):
    """The checks, against the real corpus, rather than an empty schema."""

    @unittest.skipUnless(LIVE_CORPUS.is_file(), "no live corpus on this machine")
    def test_live_corpus_has_no_error_severity_findings(self) -> None:
        import sqlite3

        # Read-only, so a test run can never be the thing that changes the
        # corpus it is checking.
        connection = sqlite3.connect(f"file:{LIVE_CORPUS}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            findings = check_corpus(connection)
            errors = [f for f in findings if f.severity == "error"]
            self.assertEqual(
                [], [f"{f.check}: {f.count} — {f.detail}" for f in errors],
                "the live corpus would make the UI state something false")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
