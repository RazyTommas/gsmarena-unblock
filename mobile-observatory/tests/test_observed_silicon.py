"""Silicon a device's own firmware evidence states.

787 of 865 devices showed an em dash for silicon. The only source was a 586-row
specification capture -- 470 Samsung, 87 Xiaomi, 29 TECNO, and no Infinix or
itel at all -- while 4,634 of those devices' own firmware observations named the
chipset in $.data.chipset, with $.data.chipset_basis recording where the source
got it. Nothing read the field. Observed, and not served.

The rule is the one used for identity: take what the evidence states
unambiguously and refuse the rest. A device whose observations name one chipset
gets it; a device whose observations disagree gets nothing, because which chip
it is is exactly what is unknown.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.enrichment import (_vendor_by_part_prefix,  # noqa: E402
                                           enrich_observed_hardware_silicon)
from mobile_observatory.seed import seed_demonstration  # noqa: E402

NOW = "2026-01-01T00:00:00Z"


class ObservedSiliconTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.con = self.db.connection
        self.con.execute(
            "INSERT OR IGNORE INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
            " VALUES('archive.test','t',NULL,'secondary',1,?)", (NOW,))
        self.con.execute(
            "INSERT OR IGNORE INTO ingestion_runs VALUES('run-sil','archive.test',?,?,"
            "'succeeded','p','1',0,0,0,NULL)", (NOW, NOW))
        self.con.execute(
            "INSERT OR IGNORE INTO artifacts VALUES('art-sil','archive.test','run-sil',?,"
            "'text/csv',NULL,?,'x',1)", ("s" * 64, NOW))

    def device_with_chipsets(self, *, model: str, chipsets: list[str], product: str) -> str:
        """A canonical device whose firmware observations state these chipsets."""
        from mobile_observatory.repository import CanonicalRepository

        hardware = CanonicalRepository(self.db).create_device(
            manufacturer="TECNO", brand="TECNO", family=model, variant=model, model_code=model)
        self.con.execute(
            "INSERT INTO source_products VALUES(?,'TECNO',?,?,'approved',NULL,?,?)",
            (product, model, model.lower(), NOW, NOW))
        self.con.execute(
            """INSERT INTO source_identity_registry VALUES
               (?,'archive.test','codename',?,?,?,'approved','t','1','high',?,?)""",
            (f"id-{product}", model, model, product, NOW, NOW))
        for index, chipset in enumerate(chipsets):
            observation = f"obs-{product}-{index}"
            self.con.execute(
                "INSERT INTO observations VALUES(?,'archive.test','run-sil','art-sil',"
                "'firmware_release',?,?,?,?,'valid',NULL)",
                (observation, f"k-{product}-{index}", NOW,
                 json.dumps({"data": {"chipset": chipset, "build": f"B{index}"}}),
                 f"{abs(hash((product, index))) % 10**60:064d}"))
            self.con.execute(
                """INSERT INTO product_firmware_releases
                   (id,product_id,identity_id,observation_id,source_id,region_code,build_id,channel,
                    android_version,android_major,vendor_released_at,delivery_method,created_at,hardware_model_id)
                   VALUES(?,?,?,?,'archive.test','GL',?,'Stable','14',14,NULL,NULL,?,?)""",
                (f"rel-{product}-{index}", product, f"id-{product}", observation,
                 f"B{index}", NOW, hardware))
        return hardware

    def chip_of(self, hardware_id: str):
        row = self.con.execute(
            """SELECT sp.part_number, sv.canonical_name vendor
                 FROM hardware_silicon hs
                 JOIN silicon_parts sp ON sp.id = hs.part_id
                 JOIN silicon_families sf ON sf.id = sp.family_id
                 JOIN silicon_vendors sv ON sv.id = sf.vendor_id
                WHERE hs.hardware_model_id = ?""", (hardware_id,)).fetchone()
        return (row["part_number"], row["vendor"]) if row else None

    # -- the vendor map is derived, not declared ------------------------------
    def test_the_prefix_map_comes_from_parts_the_corpus_already_attributed(self) -> None:
        """No hardcoded table of vendor numbering schemes."""
        self.con.execute("INSERT INTO silicon_vendors VALUES('v-mtk','MediaTek',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_families VALUES('f-mtk','v-mtk',NULL,'MediaTek',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_parts VALUES('p-1','f-mtk','MT6768','MT6768',NULL,NULL,?)", (NOW,))
        self.assertEqual("MediaTek", _vendor_by_part_prefix(self.con).get("MT"))

    def test_a_prefix_two_vendors_claim_is_dropped_not_guessed(self) -> None:
        self.con.execute("INSERT INTO silicon_vendors VALUES('v-a','AlphaCo',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_vendors VALUES('v-b','BetaCo',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_families VALUES('f-a','v-a',NULL,'AlphaCo',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_families VALUES('f-b','v-b',NULL,'BetaCo',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_parts VALUES('p-a','f-a','ZZ1000','ZZ1000',NULL,NULL,?)", (NOW,))
        self.con.execute("INSERT INTO silicon_parts VALUES('p-b','f-b','ZZ2000','ZZ2000',NULL,NULL,?)", (NOW,))
        self.assertNotIn("ZZ", _vendor_by_part_prefix(self.con))

    # -- attaching -------------------------------------------------------------
    def test_one_stated_chipset_is_attached_with_its_vendor(self) -> None:
        self.con.execute("INSERT INTO silicon_vendors VALUES('v-mtk','MediaTek',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_families VALUES('f-mtk','v-mtk',NULL,'MediaTek',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_parts VALUES('p-seed','f-mtk','MT6768','MT6768',NULL,NULL,?)", (NOW,))
        hardware = self.device_with_chipsets(model="AGREE", chipsets=["MT6761", "MT6761"], product="p-agree")
        result = enrich_observed_hardware_silicon(self.con)
        self.assertEqual(1, result["hardware_silicon_attached"])
        self.assertEqual(("MT6761", "MediaTek"), self.chip_of(hardware))

    def test_disagreeing_observations_attach_nothing(self) -> None:
        """Which chip it is, is exactly what is unknown. 178 devices are here."""
        self.con.execute("INSERT INTO silicon_vendors VALUES('v-mtk','MediaTek',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_families VALUES('f-mtk','v-mtk',NULL,'MediaTek',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_parts VALUES('p-seed','f-mtk','MT6768','MT6768',NULL,NULL,?)", (NOW,))
        hardware = self.device_with_chipsets(model="DISAGREE", chipsets=["MT6761", "MT6768"], product="p-dis")
        result = enrich_observed_hardware_silicon(self.con)
        self.assertEqual(0, result["hardware_silicon_attached"])
        self.assertEqual(1, result["skipped_conflicting_chipset"])
        self.assertIsNone(self.chip_of(hardware))

    def test_an_unrecognised_vendor_attaches_nothing(self) -> None:
        hardware = self.device_with_chipsets(model="UNKNOWN", chipsets=["QQ9999"], product="p-unk")
        result = enrich_observed_hardware_silicon(self.con)
        self.assertEqual(0, result["hardware_silicon_attached"])
        self.assertEqual(1, result["skipped_unrecognized_vendor"])
        self.assertIsNone(self.chip_of(hardware))

    def test_existing_silicon_is_never_overwritten(self) -> None:
        """Higher-authority captures win; this only fills gaps."""
        self.con.execute("INSERT INTO silicon_vendors VALUES('v-mtk','MediaTek',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_families VALUES('f-mtk','v-mtk',NULL,'MediaTek',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_parts VALUES('p-seed','f-mtk','MT6768','MT6768',NULL,NULL,?)", (NOW,))
        hardware = self.device_with_chipsets(model="HELD", chipsets=["MT6761"], product="p-held")
        self.con.execute(
            "INSERT INTO hardware_silicon VALUES(?,'p-seed',NULL,'primary_soc',NULL,?,NULL)",
            (hardware, NOW))
        result = enrich_observed_hardware_silicon(self.con)
        self.assertEqual(0, result["hardware_silicon_attached"])
        self.assertEqual(1, result["skipped_existing_silicon"])
        self.assertEqual(("MT6768", "MediaTek"), self.chip_of(hardware))

    def test_the_attachment_is_traceable_to_the_observation_that_said_it(self) -> None:
        """A claim the corpus cannot source is a claim it should not make."""
        self.con.execute("INSERT INTO silicon_vendors VALUES('v-mtk','MediaTek',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_families VALUES('f-mtk','v-mtk',NULL,'MediaTek',?)", (NOW,))
        self.con.execute("INSERT INTO silicon_parts VALUES('p-seed','f-mtk','MT6768','MT6768',NULL,NULL,?)", (NOW,))
        hardware = self.device_with_chipsets(model="TRACE", chipsets=["MT6761"], product="p-trace")
        enrich_observed_hardware_silicon(self.con)
        row = self.con.execute(
            """SELECT e.observation_id, e.locator, o.payload_json
                 FROM hardware_silicon hs
                 JOIN evidence e ON e.id = hs.evidence_id
                 JOIN observations o ON o.id = e.observation_id
                WHERE hs.hardware_model_id = ?""", (hardware,)).fetchone()
        self.assertIsNotNone(row, "the attachment must carry evidence")
        self.assertEqual("$.data.chipset", row["locator"])
        self.assertEqual("MT6761", json.loads(row["payload_json"])["data"]["chipset"])


if __name__ == "__main__":
    unittest.main()
