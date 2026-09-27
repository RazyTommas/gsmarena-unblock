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

    def test_detects_a_run_that_accepted_records_but_holds_none(self) -> None:
        """A run whose records exist nowhere in the corpus."""
        self.assertNotIn("run_accepted_but_holds_nothing", self._checks())
        self.con.execute(
            "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,parser_version,"
            "accepted_count,rejected_count) SELECT 'phantom-run',id,'2026-01-01T00:00:00Z','succeeded',"
            "'parser-with-no-other-run','1',999,0 FROM sources LIMIT 1")
        found = self._checks()
        self.assertIn("run_accepted_but_holds_nothing", found)
        self.assertEqual(found["run_accepted_but_holds_nothing"], 1)

    def test_tolerates_an_adapter_that_fans_one_record_into_several(self) -> None:
        """The check this replaced got this exact case wrong.

        The TECNO feed accepts 828 source records and emits 862 observations.
        An invariant demanding accepted_count == count(observations) called that
        corruption for two runs. Asserting the tolerance so it is not
        reintroduced.
        """
        source = self.con.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        self.con.execute(
            "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,parser_version,"
            "accepted_count,rejected_count) VALUES('fanout-run',?,'2026-01-01T00:00:00Z','succeeded',"
            "'fanout','1',2,0)", (source,))
        self.con.execute(
            "INSERT INTO artifacts VALUES('fanout-art',?,'fanout-run',?,'text/csv',NULL,?,'x',1)",
            (source, "f" * 64, "2026-01-01T00:00:00Z"))
        for index in range(3):  # 2 accepted records -> 3 observations
            self.con.execute(
                "INSERT INTO observations VALUES(?,?,'fanout-run','fanout-art','firmware_release',"
                "?,'2026-01-01T00:00:00Z','{}',?,'valid',NULL)",
                (f"fan-{index}", source, f"fank-{index}", f"{index:064d}"))
        self.assertNotIn("run_accepted_but_holds_nothing", self._checks())

    def test_tolerates_a_superseded_run_left_empty_by_a_reingest(self) -> None:
        """Re-ingesting under a new run id empties the old one. Not a fault."""
        source = self.con.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        self.con.execute(
            "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,parser_version,"
            "accepted_count,rejected_count) VALUES('old-run',?,'2026-01-01T00:00:00Z','succeeded',"
            "'shared-parser','1',5,0)", (source,))
        self.con.execute(
            "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,parser_version,"
            "accepted_count,rejected_count) VALUES('new-run',?,'2026-02-01T00:00:00Z','succeeded',"
            "'shared-parser','1',5,0)", (source,))
        self.con.execute(
            "INSERT INTO artifacts VALUES('sup-art',?,'new-run',?,'text/csv',NULL,?,'x',1)",
            (source, "e" * 64, "2026-02-01T00:00:00Z"))
        self.con.execute(
            "INSERT INTO observations VALUES('sup-obs',?,'new-run','sup-art','firmware_release',"
            "'supk','2026-02-01T00:00:00Z','{}',?,'valid',NULL)", (source, "d" * 64))
        # old-run holds nothing, but its records live under new-run.
        self.assertNotIn("run_accepted_but_holds_nothing", self._checks())

    def test_detects_an_approved_product_naming_an_unplaceable_vendor(self) -> None:
        """An approved product mints its vendor at promotion, so a typo sticks."""
        self.con.execute(
            "INSERT INTO source_products VALUES('p-ghost','Nokia Of Nowhere','Ghost','ghost',"
            "'approved',NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        found = self._checks()
        self.assertIn("approved_product_unplaceable_vendor", found)
        self.assertEqual(found["approved_product_unplaceable_vendor"], 1)

    def test_an_unapproved_product_naming_an_unknown_vendor_is_not_a_fault(self) -> None:
        """The evidence layer exists to hold identities the corpus has not vetted.

        Apple sits here on the live corpus: 66 proposed products, no
        manufacturer row and no brand row, and nothing wrong. An earlier version
        of this check flagged all of them, plus all 425 Samsung products,
        because it resolved against manufacturers only.
        """
        self.con.execute(
            "INSERT INTO source_products VALUES('p-pending','Apple','iPhone 4','iphone 4',"
            "'proposed',NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        self.assertNotIn("approved_product_unplaceable_vendor", self._checks())

    def test_a_vendor_named_as_a_brand_resolves(self) -> None:
        """Sources say "Samsung"; the manufacturer is "Samsung Electronics"."""
        brand = self.con.execute(
            "SELECT b.canonical_name, m.canonical_name FROM brands b "
            "JOIN manufacturers m ON m.id=b.manufacturer_id LIMIT 1").fetchone()
        self.assertNotEqual(brand[0], brand[1],
                            "fixture must have a brand whose name differs from its manufacturer, "
                            "or this test cannot tell the two lookups apart")
        self.con.execute(
            "INSERT INTO source_products VALUES('p-brandnamed',?,'Galaxy Z','galaxy z',"
            "'approved',NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')", (brand[0],))
        self.assertNotIn("approved_product_unplaceable_vendor", self._checks())

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
