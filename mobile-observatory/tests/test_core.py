from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import CanonicalRepository, Database, Event  # noqa: E402
from mobile_observatory.repository import normalize_identifier  # noqa: E402


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database.migrated()
        self.repo = CanonicalRepository(self.db)

    def tearDown(self) -> None:
        self.db.close()

    def test_device_levels_are_distinct_and_shared(self) -> None:
        first = self.repo.create_device(
            manufacturer="Samsung Electronics",
            brand="Samsung",
            family="Galaxy S",
            variant="Galaxy S26",
            model_code="SM-S942B",
            codename="example",
        )
        second = self.repo.create_device(
            manufacturer="Samsung Electronics",
            brand="Samsung",
            family="Galaxy S",
            variant="Galaxy S26",
            model_code="SM-S942U",
        )
        self.assertNotEqual(first, second)
        row = self.db.connection.execute(
            "SELECT count(*) AS n FROM v_device_catalog"
        ).fetchone()
        self.assertEqual(row["n"], 2)
        self.assertEqual(
            self.db.connection.execute("SELECT count(*) FROM device_families").fetchone()[0],
            1,
        )

    def test_identifier_normalization_is_conservative(self) -> None:
        self.assertEqual(normalize_identifier(" sm-s942b "), "SM-S942B")
        self.assertNotEqual(normalize_identifier("SM-S942B"), normalize_identifier("SMS942B"))

    def test_reviewed_alias_resolves_exactly_not_fuzzily(self) -> None:
        hardware_id = self.repo.create_device(
            manufacturer="Example Corp",
            brand="Example",
            family="Phone",
            variant="Phone Pro",
            model_code="EX-1",
        )
        self.repo.add_alias(
            entity_type="hardware_model",
            entity_id=hardware_id,
            namespace="common",
            alias="Example Phone Pro",
        )
        self.assertEqual(
            self.repo.resolve_exact_alias(
                entity_type="hardware_model", namespace="common", value=" example   PHONE pro "
            ),
            hardware_id,
        )
        self.assertIsNone(
            self.repo.resolve_exact_alias(
                entity_type="hardware_model", namespace="common", value="Example Phone"
            )
        )

    def test_events_are_idempotent_and_immutable(self) -> None:
        event = Event(
            event_type="firmware_first_observed",
            subject_type="firmware_release",
            subject_id="release-1",
            dedupe_key="firmware:first:release-1",
            occurred_at="2026-09-16T00:00:00Z",
            after={"build": "A1"},
        )
        first_id, first_created = self.repo.append_event(event)
        second_id, second_created = self.repo.append_event(event)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.db.connection.execute(
                "UPDATE domain_events SET subject_id = 'changed' WHERE id = ?", (first_id,)
            )

    def test_agent_can_only_create_pending_proposal(self) -> None:
        proposal_id = self.repo.propose(
            proposer_type="agent",
            proposer_id="ask-atlas",
            proposal_type="identity_alias",
            patch={"alias": "SM-S942B"},
            rationale="Exact vendor model code in cited artifact",
            citations=[{"evidence_id": "candidate-evidence"}],
        )
        row = self.db.connection.execute(
            "SELECT status, patch_json FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIn("SM-S942B", row["patch_json"])

    def test_unknown_is_not_open_or_not_applicable(self) -> None:
        allowed = {
            row[0]
            for row in self.db.connection.execute(
                "SELECT status FROM ("
                "SELECT 'open' status UNION ALL SELECT 'claimed_fixed' UNION ALL "
                "SELECT 'unknown' UNION ALL SELECT 'not_adjudicable' UNION ALL "
                "SELECT 'not_applicable')"
            )
        }
        self.assertEqual(len(allowed), 5)
        self.assertNotEqual("unknown", "not_applicable")

    def test_new_verdict_supersedes_without_erasing_history(self) -> None:
        self.db.connection.execute(
            "INSERT INTO vulnerabilities(id, cve_id) VALUES ('v1', 'CVE-2026-1234')"
        )
        first = self.repo.record_security_verdict(
            vulnerability_id="v1",
            subject_type="hardware_model",
            subject_id="device-1",
            status="unknown",
            rule_id="chip-applicability",
            rule_version="1",
            inputs={"chip": None},
            evidence_summary={},
        )
        second = self.repo.record_security_verdict(
            vulnerability_id="v1",
            subject_type="hardware_model",
            subject_id="device-1",
            status="open",
            rule_id="chip-applicability",
            rule_version="1",
            inputs={"chip": "part-1"},
            evidence_summary={"applicability": "evidence-1"},
        )
        rows = self.db.connection.execute(
            "SELECT id, supersedes_id, is_current FROM security_verdicts ORDER BY evaluated_at, id"
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(len(rows), 2)
        self.assertEqual(by_id[first]["is_current"], 0)
        self.assertEqual(by_id[second]["is_current"], 1)
        self.assertEqual(by_id[second]["supersedes_id"], first)


if __name__ == "__main__":
    unittest.main()
