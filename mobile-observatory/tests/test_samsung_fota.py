from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mobile_observatory.collectors.adapters.samsung_fota import SamsungFotaArtifactAdapter, parse_version_triplets
from mobile_observatory.collectors.importer import IngestionImporter
from mobile_observatory.collectors.pipeline import CollectorPipeline
from mobile_observatory.collectors.promotion import SamsungFirmwarePromoter
from mobile_observatory.database import Database
from mobile_observatory.repository import CanonicalRepository

FIXTURE = Path(__file__).parents[1] / "fixtures" / "samsung" / "fota_sm-s938b_ilo.xml"


class SamsungFotaTest(unittest.TestCase):
    def test_triplet_parser_keeps_ap_csc_and_cp_separate(self):
        rows = parse_version_triplets(FIXTURE.read_text())
        self.assertEqual(rows[0], ("latest", "S938BXXSCCZH1", "S938BOXMCCZH1", "S938BXXSCCZG3"))
        self.assertNotEqual(rows[0][1], rows[0][3])

    def test_artifact_adapter_is_replayable_and_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            result = CollectorPipeline(Path(directory)).run(
                SamsungFotaArtifactAdapter(FIXTURE, model_code="sm-s938b", csc="ilo", observed_at="2026-09-16T06:00:00Z"),
                "samsung-offline-test",
            )
            self.assertEqual(result.run.state, "healthy")
            self.assertEqual(len(result.valid), 2)
            latest = next(row for row in result.valid if row.data["manifest_position"] == "latest")
            self.assertEqual(latest.data["baseband"], "S938BXXSCCZG3")

    def test_exact_identity_promotes_and_emits_immutable_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); db = Database.migrated()
            hardware_id = CanonicalRepository(db).create_device(
                manufacturer="Samsung Electronics", brand="Samsung", family="Galaxy S25",
                variant="Galaxy S25 Ultra 5G", model_code="SM-S938B")
            CollectorPipeline(root).run(
                SamsungFotaArtifactAdapter(FIXTURE, model_code="SM-S938B", csc="ILO", observed_at="2026-09-16T06:00:00Z"), "promote-run")
            IngestionImporter(root, db.connection).import_run("samsung.fota", "promote-run")
            result = SamsungFirmwarePromoter(db.connection).promote_pending()
            self.assertEqual((result.promoted, result.skipped, result.events), (2, 0, 1))
            releases = db.connection.execute("SELECT * FROM firmware_releases WHERE hardware_model_id=?", (hardware_id,)).fetchall()
            self.assertEqual(len(releases), 2)
            self.assertTrue(all(row["baseband_version"] for row in releases))
            self.assertEqual(db.connection.execute("SELECT count(*) FROM domain_events").fetchone()[0], 1)
            self.assertEqual(SamsungFirmwarePromoter(db.connection).promote_pending().promoted, 0)

    def test_unknown_model_stays_staged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); db = Database.migrated()
            CollectorPipeline(root).run(
                SamsungFotaArtifactAdapter(FIXTURE, model_code="SM-S938B", csc="ILO", observed_at="2026-09-16T06:00:00Z"), "unknown-run")
            IngestionImporter(root, db.connection).import_run("samsung.fota", "unknown-run")
            result = SamsungFirmwarePromoter(db.connection).promote_pending()
            self.assertEqual((result.promoted, result.skipped), (0, 2))
            self.assertEqual(db.connection.execute("SELECT count(*) FROM firmware_releases").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
