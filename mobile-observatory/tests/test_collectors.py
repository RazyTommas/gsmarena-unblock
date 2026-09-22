from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mobile_observatory.collectors.adapters import (
    AppleIpswFirmwareAdapter,
    FixtureCatalogAdapter,
    TecnoSecurityPatchAdapter,
    XiaomiFirmwareTrackerAdapter,
)
from mobile_observatory.collectors.importer import IngestionImporter
from mobile_observatory.collectors.pipeline import CollectorPipeline
from mobile_observatory.collectors.validation import validate_observation


FIXTURE = Path(__file__).parents[1] / "fixtures" / "supported_catalog.sample.json"
LEGACY_ROOT = Path(__file__).parents[2] / "crawler" / "relay" / "results"


class CollectorPipelineTest(unittest.TestCase):
    def test_seed_is_healthy_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = CollectorPipeline(root)
            first = pipeline.run(FixtureCatalogAdapter(FIXTURE), "fixed-run")
            first_bytes = (root / "staging" / "fixed-run.jsonl").read_bytes()
            second = pipeline.run(FixtureCatalogAdapter(FIXTURE), "fixed-run")
            self.assertEqual(first.run.state, "healthy")
            self.assertEqual(len(first.valid), 7)
            self.assertEqual(first_bytes, (root / "staging" / "fixed-run.jsonl").read_bytes())
            self.assertEqual(first.valid, second.valid)
            hashes = {row.artifact_sha256 for row in first.valid}
            self.assertEqual(len(hashes), 1)
            digest = hashes.pop()
            self.assertTrue((root / "raw" / "fixture.supported_catalog" / digest[:2] / f"{digest}.bin").exists())

    def test_invalid_record_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = FixtureCatalogAdapter(FIXTURE)
            original_parse = adapter.parse

            def broken_parse(artifact, digest):
                rows = list(original_parse(artifact, digest))
                yield replace(rows[0], data={**rows[0].data, "model_code": ""})

            adapter.parse = broken_parse  # type: ignore[method-assign]
            result = CollectorPipeline(root).run(adapter, "bad-run")
            self.assertEqual(result.run.state, "quarantined")
            quarantine = [json.loads(line) for line in (root / "quarantine" / "bad-run.jsonl").read_text().splitlines()]
            self.assertTrue(any(issue["field"] == "data.model_code" for issue in quarantine[0]["issues"]))

    def test_validation_rejects_bad_hash(self) -> None:
        adapter = FixtureCatalogAdapter(FIXTURE)
        artifact = next(iter(adapter.fetch()))
        item = next(iter(adapter.parse(artifact, "bad")))
        self.assertIn("artifact_sha256", {issue.field for issue in validate_observation(item)})

    def test_imports_idempotently_into_ingestion_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            CollectorPipeline(root).run(FixtureCatalogAdapter(FIXTURE), "import-run")
            db = sqlite3.connect(":memory:")
            migration = (Path(__file__).parents[1] / "migrations" / "0001_canonical_core.sql").read_text()
            db.executescript(migration)
            importer = IngestionImporter(root, db)
            self.assertEqual(importer.import_run("fixture.supported_catalog", "import-run"), {"valid": 7, "invalid": 0})
            importer.import_run("fixture.supported_catalog", "import-run")
            self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 7)
            self.assertEqual(db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 1)

    def test_xiaomi_tracker_preserves_codename_as_unresolved_hint(self) -> None:
        fixture = LEGACY_ROOT / "xiaomi-tracker" / "xiaomi-firmware-latest.csv"
        adapter = XiaomiFirmwareTrackerAdapter(fixture)
        artifact = next(iter(adapter.fetch()))
        rows = list(adapter.parse(artifact, "a" * 64))
        # The captured CSV is the source's latest-per-target export; its result
        # metadata counts the larger upstream YAML dataset.
        self.assertGreater(len(rows), 1000)
        target = next(row for row in rows if row.data["build"] == "OS3.0.301.0.WGNMIXM")
        self.assertEqual(target.data["model_code"], "songyuan_global")
        self.assertEqual(target.data["region_code"], "GLOBAL")
        self.assertEqual(target.data["identity_state"], "unresolved_codename")
        self.assertNotIn("canonical_device_id", target.identity_hints)
        self.assertEqual(validate_observation(target), [])

    def test_xiaomi_history_yaml_preserves_multiple_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "history.yml"
            fixture.write_text("""- android: '13.0'
  branch: Stable
  codename: ruby_global
  date: 2024-01-01
  link: https://example.test/one.zip
  md5: abc
  method: Recovery
  name: Redmi Note 12 Turbo Global
  version: V1
- android: '14.0'
  branch: Stable
  codename: ruby_global
  date: 2025-01-01
  method: Recovery
  name: Redmi Note 12 Turbo Global
  version: V2
""", encoding="utf-8")
            adapter = XiaomiFirmwareTrackerAdapter(fixture)
            artifact = next(iter(adapter.fetch()))
            rows = list(adapter.parse(artifact, "a" * 64))
            self.assertEqual([row.data["android"] for row in rows], ["13.0", "14.0"])
            self.assertEqual(rows[0].data["delivery_method"], "Recovery")
            self.assertEqual(rows[0].data["region_code"], "GLOBAL")

    def test_tecno_patch_feed_splits_group_without_inventing_day(self) -> None:
        fixture = LEGACY_ROOT / "tecno-security-comprehensive" / "tecno-security-updates.csv"
        adapter = TecnoSecurityPatchAdapter(fixture)
        artifact = next(iter(adapter.fetch()))
        rows = list(adapter.parse(artifact, "b" * 64))
        self.assertGreater(len(rows), 618)
        target = next(row for row in rows if row.data["device"] == "TECNO CAMON 40 Pro 5G")
        self.assertRegex(target.data["aspl_month"], r"^20\d\d-(0[1-9]|1[0-2])$")
        self.assertEqual(target.data["precision"], "month")
        self.assertEqual(target.data["identity_state"], "unresolved_source_name")
        self.assertEqual(validate_observation(target), [])


    def test_apple_ipsw_is_firmware_release_not_android_patch_level(self) -> None:
        fixture = LEGACY_ROOT / "ipsw-me" / "ipsw-me-firmware.csv"
        adapter = AppleIpswFirmwareAdapter(fixture)
        artifact = next(iter(adapter.fetch()))
        rows = list(adapter.parse(artifact, "c" * 64))
        # Full legacy slice: 66 distinct Apple devices, 4450 iOS builds.
        self.assertEqual(len(rows), 4450)
        self.assertTrue(all(row.kind == "firmware_release" for row in rows))
        target = next(row for row in rows if row.data["source_model_identifier"] == "iPhone1,1")
        self.assertEqual(target.data["model_code"], "iPhone1-1")  # comma -> hyphen, contract format only
        self.assertEqual(target.data["region_code"], "GLOBAL")
        self.assertNotIn("android", target.data)
        self.assertNotIn("android_version", target.data)
        self.assertNotIn("aspl_month", target.data)
        self.assertNotIn("patch_tier", target.data)
        self.assertEqual(validate_observation(target), [])
        # Every row must clear the contract; a previous port quarantined
        # 15,738 rows by skipping this check before writing the parser.
        issues = [issue for row in rows for issue in validate_observation(row)]
        self.assertEqual(issues, [])
        ids = {row.source_record_id for row in rows}
        self.assertEqual(len(ids), len(rows))


if __name__ == "__main__":
    unittest.main()
