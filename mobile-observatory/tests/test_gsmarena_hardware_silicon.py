from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import CanonicalRepository, Database  # noqa: E402
from mobile_observatory.enrichment import enrich_gsmarena_hardware_silicon  # noqa: E402
from mobile_observatory.server import ObservatoryService  # noqa: E402

NOW = "2026-01-01T00:00:00Z"
FIELDS = ["brand", "device", "slug", "device_name", "chipset", "fetched_at"]


class GsmarenaHardwareSiliconTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database.migrated()
        self.c = self.db.connection
        self.repo = CanonicalRepository(self.db)
        self.specs = self.root / "gsm_specs.csv"

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def write_specs(self, rows: list[dict]) -> None:
        with self.specs.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({**{k: "" for k in FIELDS}, **row})

    def device(self, variant: str, model_code: str, brand: str = "Samsung") -> str:
        return self.repo.create_device(manufacturer=brand + " Electronics", brand=brand,
                                        family=variant.split()[0], variant=variant, model_code=model_code)

    def explore_silicon_column(self) -> dict[str, str | None]:
        """Read the chipset exactly the way server.py::devices_page does, not a
        hand-rolled query -- this is the real Explore 'Silicon' column consumer."""
        svc = ObservatoryService(self.db, self.root / "local.sqlite", demonstration=False)
        items = svc.devices_page({"limit": ["200"]}).items
        return {i["model"]: i.get("chip") for i in items}

    def test_exact_unique_match_attaches_chip_with_evidence(self) -> None:
        self.device("Galaxy Exact", "SM-E100F")
        self.write_specs([{"brand": "Samsung", "device": "Galaxy Exact", "slug": "samsung_galaxy_exact-1.php",
                            "device_name": "Samsung Galaxy Exact", "chipset": "Qualcomm SM1234 Snapdragon 999 (4 nm)",
                            "fetched_at": NOW}])
        result = enrich_gsmarena_hardware_silicon(self.c, self.specs)
        self.assertEqual(result["hardware_silicon_attached"], 1)
        row = self.c.execute("""SELECT sp.marketing_name,sp.part_number,sv.canonical_name vendor,hs.evidence_id
            FROM hardware_silicon hs JOIN silicon_parts sp ON sp.id=hs.part_id
            JOIN silicon_families sf ON sf.id=sp.family_id JOIN silicon_vendors sv ON sv.id=sf.vendor_id""").fetchone()
        self.assertEqual(row["vendor"], "Qualcomm")
        self.assertIsNotNone(row["evidence_id"])
        evidence = self.c.execute("""SELECT e.excerpt,a.source_url FROM evidence e
            JOIN artifacts a ON a.id=e.artifact_id WHERE e.id=?""", (row["evidence_id"],)).fetchone()
        self.assertEqual(evidence["source_url"], "https://www.gsmarena.com/samsung_galaxy_exact-1.php")
        self.assertIn("Qualcomm SM1234 Snapdragon 999", evidence["excerpt"])
        self.assertEqual(self.explore_silicon_column()["SM-E100F"], row["marketing_name"])

    def test_device_without_observed_silicon_stays_empty(self) -> None:
        """The core invariant: no GSMArena page names this device at all, so its
        Explore 'Silicon' cell must stay empty (an em dash), never a guess."""
        self.device("Galaxy Unmatched", "SM-U100F")
        self.write_specs([{"brand": "Samsung", "device": "Some Other Phone", "slug": "other.php",
                            "device_name": "Samsung Some Other Phone", "chipset": "Qualcomm SM1 (4 nm)",
                            "fetched_at": NOW}])
        result = enrich_gsmarena_hardware_silicon(self.c, self.specs)
        self.assertEqual(result["hardware_silicon_attached"], 0)
        self.assertIsNone(self.c.execute(
            "SELECT 1 FROM hardware_silicon WHERE hardware_model_id=?", (self.device_id("SM-U100F"),)).fetchone())
        self.assertIsNone(self.explore_silicon_column()["SM-U100F"])

    def device_id(self, model_code: str) -> str:
        return self.c.execute("SELECT id FROM hardware_models WHERE model_code=?", (model_code,)).fetchone()[0]

    def test_planted_violation_is_caught(self) -> None:
        """Proves the previous test is not vacuous: if the code guessed a chipset
        for an unmatched device (the exact defect the task forbids), inserting
        that violation directly makes the invariant assertion fail."""
        self.device("Galaxy Unmatched", "SM-U200F")
        self.write_specs([])
        enrich_gsmarena_hardware_silicon(self.c, self.specs)
        self.assertIsNone(self.explore_silicon_column()["SM-U200F"])
        # Simulate the forbidden behaviour: invent a chip for the unmatched device.
        self.c.execute("INSERT INTO silicon_vendors VALUES('v','Invented',?)", (NOW,))
        self.c.execute("INSERT INTO silicon_families VALUES('f','v',NULL,'Invented',?)", (NOW,))
        self.c.execute("INSERT INTO silicon_parts VALUES('p','f','GUESS1','Guessed Chip',NULL,NULL,?)", (NOW,))
        self.c.execute("INSERT INTO hardware_silicon VALUES(?,?,NULL,'primary_soc',NULL,?,NULL)",
                       (self.device_id("SM-U200F"), "p", NOW))
        with self.assertRaises(AssertionError):
            self.assertIsNone(self.explore_silicon_column()["SM-U200F"])

    def test_packed_multi_region_chipset_string_is_not_attached(self) -> None:
        self.device("Galaxy S24", "SM-S921B")
        self.write_specs([{"brand": "Samsung", "device": "Galaxy S24", "slug": "s24.php",
                            "device_name": "Samsung Galaxy S24",
                            "chipset": "Qualcomm SM8650-AC Snapdragon 8 Gen 3 (4 nm) - USA/Canada/China"
                                       "Exynos 2400 (4 nm) - International",
                            "fetched_at": NOW}])
        result = enrich_gsmarena_hardware_silicon(self.c, self.specs)
        self.assertEqual(result["hardware_silicon_attached"], 0)
        self.assertEqual(result["skipped_conflicting_chipset"], 1)
        self.assertIsNone(self.explore_silicon_column()["SM-S921B"])

    def test_conflicting_duplicate_rows_for_same_name_are_not_attached(self) -> None:
        self.device("Galaxy Dup", "SM-D100F")
        self.write_specs([
            {"brand": "Samsung", "device": "Galaxy Dup", "slug": "dup-1.php", "device_name": "Samsung Galaxy Dup",
             "chipset": "Qualcomm SM1 (4 nm)", "fetched_at": NOW},
            {"brand": "Samsung", "device": "Galaxy Dup", "slug": "dup-2.php", "device_name": "Samsung Galaxy Dup",
             "chipset": "Mediatek MT9 (4 nm)", "fetched_at": NOW},
        ])
        result = enrich_gsmarena_hardware_silicon(self.c, self.specs)
        self.assertEqual(result["hardware_silicon_attached"], 0)
        self.assertEqual(result["skipped_conflicting_chipset"], 1)
        self.assertIsNone(self.explore_silicon_column()["SM-D100F"])

    def test_existing_higher_authority_silicon_is_never_overwritten_or_duplicated(self) -> None:
        model_id = self.device("Galaxy Existing", "SM-X100F")
        self.c.execute("INSERT INTO silicon_vendors VALUES('vend','Qualcomm',?)", (NOW,))
        self.c.execute("INSERT INTO silicon_families VALUES('fam','vend',NULL,'Qualcomm',?)", (NOW,))
        self.c.execute("INSERT INTO silicon_parts VALUES('part','fam','EXIST1','Existing Truth',NULL,NULL,?)", (NOW,))
        self.c.execute("INSERT INTO hardware_silicon VALUES(?,?,NULL,'primary_soc',NULL,?,NULL)",
                       (model_id, "part", NOW))
        self.write_specs([{"brand": "Samsung", "device": "Galaxy Existing", "slug": "existing.php",
                            "device_name": "Samsung Galaxy Existing", "chipset": "Mediatek MT1 (4 nm)",
                            "fetched_at": NOW}])
        result = enrich_gsmarena_hardware_silicon(self.c, self.specs)
        self.assertEqual(result["hardware_silicon_attached"], 0)
        self.assertEqual(result["skipped_existing_silicon"], 1)
        rows = self.c.execute("SELECT part_id FROM hardware_silicon WHERE hardware_model_id=?", (model_id,)).fetchall()
        self.assertEqual([r["part_id"] for r in rows], ["part"])
        self.assertEqual(self.explore_silicon_column()["SM-X100F"], "Existing Truth")

    def test_rerun_is_idempotent(self) -> None:
        self.device("Galaxy Idem", "SM-I100F")
        self.write_specs([{"brand": "Samsung", "device": "Galaxy Idem", "slug": "idem.php",
                            "device_name": "Samsung Galaxy Idem", "chipset": "Unisoc T999 (4 nm)",
                            "fetched_at": NOW}])
        enrich_gsmarena_hardware_silicon(self.c, self.specs)
        second = enrich_gsmarena_hardware_silicon(self.c, self.specs)
        self.assertEqual(second["hardware_silicon_attached"], 0)
        self.assertEqual(second["skipped_existing_silicon"], 1)
        self.assertEqual(self.c.execute("SELECT count(*) FROM hardware_silicon").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
