from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mobile_observatory.dedupe import dedupe_reingested_observations, retire_observations

MIGRATION_0001 = (Path(__file__).parents[1] / "migrations" / "0001_canonical_core.sql").read_text()


def _payload(data: dict, identity_hints: dict, evidence: dict) -> str:
    return json.dumps({"data": data, "identity_hints": identity_hints, "evidence": evidence},
                      sort_keys=True, separators=(",", ":"))


class DedupeReingestedObservationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(MIGRATION_0001)
        self.db.execute("INSERT INTO sources(id,name,authority_scope,created_at) VALUES('samsung.fota','samsung.fota','secondary','2026-01-01T00:00:00Z')")
        self.db.execute(
            "INSERT INTO ingestion_runs(id,source_id,started_at,finished_at,outcome,parser_name,parser_version,fetched_count,accepted_count,rejected_count) "
            "VALUES('r1','samsung.fota','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','succeeded','p','1.1.0',1,1,0)")
        self.db.execute(
            "INSERT INTO artifacts(id,source_id,run_id,sha256,media_type,retrieved_at,storage_uri,byte_length) "
            "VALUES('art1','samsung.fota','r1',?,'text/csv','2026-01-01T00:00:00Z','/tmp/x',10)",
            ("a" * 64,))
        now = "2026-01-01T00:00:00Z"
        self.db.execute("INSERT INTO manufacturers(id,canonical_name,created_at,updated_at) VALUES('man1','Samsung',?,?)", (now, now))
        self.db.execute("INSERT INTO brands(id,manufacturer_id,canonical_name,created_at,updated_at) VALUES('brand1','man1','Samsung',?,?)", (now, now))
        self.db.execute("INSERT INTO device_families(id,brand_id,canonical_name,created_at,updated_at) VALUES('fam1','brand1','Galaxy A01',?,?)", (now, now))
        self.db.execute("INSERT INTO device_variants(id,family_id,canonical_name,created_at,updated_at) VALUES('var1','fam1','Galaxy A01',?,?)", (now, now))
        self.db.execute("INSERT INTO hardware_models(id,variant_id,model_code,model_code_normalized,created_at,updated_at) VALUES('hm1','var1','SM-A015F','sma015f',?,?)", (now, now))
        self.db.commit()

    def _insert_observation(self, obs_id: str, data: dict, identity_hints=None, evidence=None,
                            source_key="SM-A015F:EGY:BUILD1") -> None:
        identity_hints = identity_hints or {"manufacturer": "Samsung", "model_code": "SM-A015F"}
        evidence = evidence or {"artifact_pointer": "CSV line 26", "authority": "vendor-fota-capture"}
        payload = _payload(data, identity_hints, evidence)
        digest = format(abs(hash((obs_id, payload))), "064x")[:64]
        self.db.execute(
            "INSERT INTO observations(id,source_id,run_id,artifact_id,record_type,source_key,observed_at,payload_json,content_sha256,validation_state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (obs_id, "samsung.fota", "r1", "art1", "firmware_release", source_key, "2026-01-01T00:00:00Z", payload, digest, "valid"))

    def _insert_evidence(self, evidence_id: str, observation_id: str, locator=None) -> None:
        self.db.execute(
            "INSERT INTO evidence(id,artifact_id,observation_id,locator,created_at) VALUES(?,?,?,?,?)",
            (evidence_id, "art1", observation_id, locator, "2026-01-01T00:00:00Z"))

    def _insert_firmware_release(self, fr_id: str) -> None:
        self.db.execute(
            "INSERT INTO firmware_releases(id,hardware_model_id,build_id,channel,first_observed_at,last_observed_at,created_at) "
            "VALUES(?,'hm1','BUILD1','stable','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')", (fr_id,))

    def test_collapses_reshaped_duplicate_and_repoints_dependents(self) -> None:
        old_data = {"model_code": "SM-A015F", "build": "BUILD1", "baseband": "BB1",
                    "region_code": "EGY", "manifest_position": "upgrade", "channel": "stable",
                    "source_device_name": "Samsung Galaxy A01", "release_time": "2021-12-01"}
        new_data = {**old_data, "release_time": None, "build_derived_month": "2021-12",
                    "date_basis": "build_identifier_month_not_vendor_release"}
        self._insert_observation("obs-old", old_data)
        self._insert_observation("obs-new", new_data)
        # A real, non-null locator matters here: SQL UNIQUE treats NULLs as distinct
        # from each other, so this only reproduces the production collision -- both
        # copies citing the identical CSV line -- when the locator is the same string.
        self._insert_evidence("ev-old", "obs-old", locator="CSV line 26")
        self._insert_evidence("ev-new", "obs-new", locator="CSV line 26")
        self._insert_firmware_release("fr1")
        for role in ("availability", "baseband"):
            self.db.execute(
                "INSERT INTO firmware_release_evidence(firmware_release_id,evidence_id,role) VALUES('fr1',?,?)",
                ("ev-old", role))
            self.db.execute(
                "INSERT INTO firmware_release_evidence(firmware_release_id,evidence_id,role) VALUES('fr1',?,?)",
                ("ev-new", role))
        self.db.execute(
            "INSERT INTO domain_events(id,event_type,subject_type,subject_id,dedupe_key,occurred_at,recorded_at,evidence_id) "
            "VALUES('de1','firmware_first_observed','firmware_release','fr1','dk1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','ev-old')")
        self.db.commit()

        result = dedupe_reingested_observations(self.db, source_id="samsung.fota", run_id="r1")

        self.assertEqual(result["groups_collapsed"], 1)
        self.assertEqual(result["groups_skipped_unsafe"], 0)
        self.assertEqual(result["observations_deleted"], 1)

        remaining = self.db.execute("SELECT id, payload_json FROM observations").fetchall()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0][0], "obs-new")
        self.assertIn("date_basis", remaining[0][1])

        # domain_events is append-only (a BEFORE UPDATE/DELETE trigger raises), so its
        # citation of ev-old can never be repointed to ev-new: ev-old survives, detached
        # from the (now-deleted) observation rather than dropped or silently orphaned.
        evidence_rows = self.db.execute("SELECT id, observation_id FROM evidence ORDER BY id").fetchall()
        self.assertEqual(evidence_rows, [("ev-new", "obs-new"), ("ev-old", None)])
        self.assertEqual(result.get("evidence_detached_immutable_citation"), 1)

        # ev-old itself was never deleted (only detached), so its firmware_release_evidence
        # rows are still valid FKs too -- fr1 simply keeps both citations rather than
        # orphaning or fabricating a repoint that immutability forbids.
        fre_rows = self.db.execute("SELECT evidence_id, role FROM firmware_release_evidence ORDER BY evidence_id, role").fetchall()
        self.assertEqual(set(fre_rows), {("ev-new", "availability"), ("ev-new", "baseband"),
                                         ("ev-old", "availability"), ("ev-old", "baseband")})

        event_row = self.db.execute("SELECT evidence_id FROM domain_events WHERE id='de1'").fetchone()
        self.assertEqual(event_row[0], "ev-old")

        # No evidence row points at a retired (now-deleted) observation -- ev-old's
        # detachment (observation_id=NULL) is exactly what keeps this true for it too.
        no_orphan_evidence = self.db.execute(
            "SELECT COUNT(*) FROM evidence WHERE observation_id IS NOT NULL "
            "AND observation_id NOT IN (SELECT id FROM observations)").fetchone()[0]
        self.assertEqual(no_orphan_evidence, 0)
        no_orphan_fre = self.db.execute(
            "SELECT COUNT(*) FROM firmware_release_evidence WHERE evidence_id NOT IN (SELECT id FROM evidence)").fetchone()[0]
        self.assertEqual(no_orphan_fre, 0)
        no_orphan_events = self.db.execute(
            "SELECT COUNT(*) FROM domain_events WHERE evidence_id IS NOT NULL "
            "AND evidence_id NOT IN (SELECT id FROM evidence)").fetchone()[0]
        self.assertEqual(no_orphan_events, 0)

        second = dedupe_reingested_observations(self.db, source_id="samsung.fota", run_id="r1")
        self.assertEqual(second["groups_collapsed"], 0)
        self.assertEqual(second.get("observations_deleted", 0), 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 1)

    def test_refuses_to_collapse_genuinely_different_facts_sharing_a_key(self) -> None:
        base = {"model_code": "SM-A015F", "build": "BUILD1", "region_code": "EGY",
                "manifest_position": "upgrade", "channel": "stable"}
        self._insert_observation("obs-a", {**base, "baseband": "BB1"})
        self._insert_observation("obs-b", {**base, "baseband": "BB2"})
        self.db.commit()

        result = dedupe_reingested_observations(self.db, source_id="samsung.fota", run_id="r1")

        self.assertEqual(result["groups_collapsed"], 0)
        self.assertEqual(result["groups_skipped_unsafe"], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 2)

    def test_retire_observations_raises_rather_than_silently_orphan(self) -> None:
        self._insert_observation("obs-old", {"a": 1})
        self._insert_observation("obs-new", {"a": 1, "b": 2})
        self._insert_evidence("ev-old", "obs-old", locator="only-on-old")
        self.db.commit()
        # keep side has no evidence at all for this artifact+locator, so remapping is
        # impossible to prove safe by deletion; it must succeed as a plain repoint
        # instead of being dropped -- assert that happens, not silent data loss.
        retire_observations(self.db, {"obs-old": "obs-new"})
        row = self.db.execute("SELECT observation_id FROM evidence WHERE id='ev-old'").fetchone()
        self.assertEqual(row[0], "obs-new")


if __name__ == "__main__":
    unittest.main()
