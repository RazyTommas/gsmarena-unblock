"""Resolving a product that is NAMED by its hardware code.

Transsion products reach the corpus from identity_bridge named by the bare
vendor code -- "X6962", "CN7c" -- never by a commercial name. The identity
review only ever looked the Google Play catalogue up BY MARKETING NAME, so a
code-named product could not match, concluded insufficient_evidence, and stayed
unpromotable forever. On the live corpus that was 1,049 of 1,060 proposed
Transsion products, carrying 4,710 observations that no canonical device could
reach.

The rule added here asks the catalogue the other question: what is this CODE
called? It approves only when the catalogue answers with exactly one name, and
the tests below pin both halves of that -- the resolution AND the refusal --
because a rule that resolves ambiguous codes would silently merge two devices
into one.
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.collectors.device_promotion import promote_approved_products_to_devices  # noqa: E402
from mobile_observatory.enrichment import automate_identity_review  # noqa: E402
from mobile_observatory.integrity import check_corpus  # noqa: E402

NOW = "2026-01-01T00:00:00Z"


class CodeKeyedIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        self.con = self.db.connection
        self.con.execute("INSERT INTO manufacturers VALUES('m-t','TECNO',NULL,?,?)", (NOW, NOW))
        self.con.execute("INSERT INTO brands VALUES('b-t','m-t','TECNO',?,?)", (NOW, NOW))
        self.devices_yml = self.root / "devices.yml"
        self.devices_yml.write_text("{}\n", encoding="utf-8")
        self.specs = self.root / "specs.csv"
        self.specs.write_text("slug,device_name,chipset,maker,fetched_at\n", encoding="utf-8")
        self.play = self.root / "play.csv"

    def write_play(self, rows: list[tuple[str, str, str, str]]) -> None:
        with self.play.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Retail Branding", "Marketing Name", "Device", "Model"])
            writer.writerows(rows)

    def add_product(self, product_id: str, code: str, manufacturer: str = "TECNO") -> None:
        """A product named by its hardware code, the way identity_bridge makes them."""
        self.con.execute(
            "INSERT INTO source_products VALUES(?,?,?,?,'proposed',NULL,?,?)",
            (product_id, manufacturer, code, code.lower(), NOW, NOW))
        self.con.execute(
            "INSERT OR IGNORE INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
            " VALUES('frbox.community.transsion_catalog','f',NULL,'secondary',1,?)", (NOW,))
        self.con.execute(
            """INSERT INTO source_identity_registry VALUES
               (?,'frbox.community.transsion_catalog','model_code',?,?,?,'proposed',
                'test','1','medium',?,?)""",
            (f"id-{product_id}", code, code.upper(), product_id, NOW, NOW))

    def review(self) -> dict:
        return automate_identity_review(
            self.con, devices_yml=self.devices_yml, specs_csv=self.specs,
            google_play_csv=self.play, decisions=[])

    def conclusion(self, product_id: str) -> tuple[str, str]:
        row = self.con.execute(
            "SELECT conclusion, method FROM identity_conclusions WHERE product_id=?",
            (product_id,)).fetchone()
        return (row["conclusion"], row["method"]) if row else ("<none>", "<none>")

    # -- resolution -----------------------------------------------------------
    def test_a_code_the_catalogue_names_once_is_approved(self) -> None:
        self.write_play([("Tecno", "CAMON 50 Ultra", "TECNO-CN7c", "TECNO CN7c")])
        self.add_product("p1", "CN7c")
        self.review()
        self.assertEqual(("auto_approved", "exact_unique_google_play_model_code"),
                         self.conclusion("p1"))
        self.assertEqual("approved", self.con.execute(
            "SELECT review_state FROM source_products WHERE id='p1'").fetchone()[0])

    def test_the_promoted_device_is_named_commercially_not_by_its_code(self) -> None:
        """The whole point: a user searches for the name, not for CN7c."""
        self.write_play([("Tecno", "CAMON 50 Ultra", "TECNO-CN7c", "TECNO CN7c")])
        self.add_product("p1", "CN7c")
        self.review()
        promote_approved_products_to_devices(self.con)
        row = self.con.execute("SELECT variant, model_code FROM v_device_catalog").fetchone()
        self.assertEqual("CAMON 50 Ultra", row["variant"])
        self.assertEqual("CN7c", row["model_code"],
                         "the code must survive as the model code; it is what OTA reports")

    def test_a_brand_prefix_on_either_side_does_not_prevent_the_match(self) -> None:
        """Play writes "TECNO CN7c"; the OTA feed and the corpus write "CN7c"."""
        self.write_play([("Tecno Mobile", "SPARK 20", "TECNO-KJ5", "KJ5")])
        self.add_product("p1", "TECNO KJ5")
        self.review()
        self.assertEqual("auto_approved", self.conclusion("p1")[0])

    # -- refusal --------------------------------------------------------------
    def test_a_code_listed_under_two_names_is_refused(self) -> None:
        """Infinix X603 is both Zero 5 and Zero 5 Pro. Merging them invents a device."""
        self.write_play([("Tecno", "Zero 5", "TECNO-X603", "TECNO X603"),
                         ("Tecno", "Zero 5 Pro", "TECNO-X603b", "TECNO X603")])
        self.add_product("p1", "X603")
        self.review()
        self.assertEqual(("ambiguous", "google_play_model_code_multiple_names"),
                         self.conclusion("p1"))
        self.assertEqual("proposed", self.con.execute(
            "SELECT review_state FROM source_products WHERE id='p1'").fetchone()[0],
            "an ambiguous code must not be approved")

    def test_a_code_the_catalogue_does_not_list_stays_unresolved(self) -> None:
        self.write_play([("Tecno", "SPARK 20", "TECNO-KJ5", "TECNO KJ5")])
        self.add_product("p1", "NOTINCATALOGUE")
        self.review()
        self.assertEqual("insufficient_evidence", self.conclusion("p1")[0])

    # -- name quality ---------------------------------------------------------
    def test_a_name_that_is_just_the_brand_does_not_become_the_device_name(self) -> None:
        """Play's marketing name for TECNO BF7 is literally "TECNO"."""
        self.write_play([("Tecno", "TECNO", "TECNO-BF7", "TECNO BF7")])
        self.add_product("p1", "BF7")
        self.review()
        self.assertEqual("auto_approved", self.conclusion("p1")[0],
                         "the CODE is still corroborated even when the name is useless")
        promote_approved_products_to_devices(self.con)
        row = self.con.execute("SELECT variant FROM v_device_catalog").fetchone()
        self.assertEqual("BF7", row["variant"],
                         "a device must not be called by its own brand")

    def test_a_name_that_is_just_the_code_does_not_become_the_device_name(self) -> None:
        self.write_play([("Tecno", "X5010", "TECNO-X5010", "TECNO X5010")])
        self.add_product("p1", "X5010")
        self.review()
        promote_approved_products_to_devices(self.con)
        self.assertEqual("X5010", self.con.execute(
            "SELECT variant FROM v_device_catalog").fetchone()["variant"])

    def test_a_name_covering_two_devices_is_refused(self) -> None:
        """Infinix X6517 is published as "SMART 7  or SMART 7 PLUS"."""
        self.write_play([("Tecno", "SMART 7  or SMART 7 PLUS", "TECNO-X6517", "TECNO X6517")])
        self.add_product("p1", "X6517")
        self.review()
        self.assertEqual(("ambiguous", "google_play_model_code_names_several_devices"),
                         self.conclusion("p1"))

    def test_spelling_variants_of_one_name_are_not_an_ambiguity(self) -> None:
        """Play writes both "Infinix NOTE 5" and "Infinix Note 5" for X604."""
        self.write_play([("Tecno", "TECNO NOTE 5", "TECNO-X604", "TECNO X604"),
                         ("Tecno", "TECNO Note 5", "TECNO-X604b", "TECNO X604")])
        self.add_product("p1", "X604")
        self.review()
        self.assertEqual("auto_approved", self.conclusion("p1")[0],
                         "one device spelled two ways is one device")

    def test_non_breaking_and_doubled_spaces_are_normalised(self) -> None:
        """"S5\u00a0Pro" cannot be typed; a user searching "S5 Pro" must find it."""
        self.write_play([("Tecno", "S5\u00a0Pro", "TECNO-S5P", "TECNO S5P")])
        self.add_product("p1", "S5P")
        self.review()
        promote_approved_products_to_devices(self.con)
        self.assertEqual("S5 Pro", self.con.execute(
            "SELECT variant FROM v_device_catalog").fetchone()["variant"])

    def test_a_vendor_qualifier_after_the_brand_is_stripped_too(self) -> None:
        """Play lists the vendor as both "Tecno" and "Tecno Mobile".

        Stripping only "TECNO" from "TECNO Mobile SPARK 30 Pro" left
        "Mobile SPARK 30 Pro", which no longer matched the device already named
        "SPARK 30 Pro" -- so the product was refused and its 7 firmware rows
        became unreachable.
        """
        self.write_play([("Tecno Mobile", "TECNO Mobile SPARK 30 Pro", "TECNO-KL7", "TECNO KL7")])
        self.add_product("p1", "KL7")
        self.review()
        promote_approved_products_to_devices(self.con)
        self.assertEqual("SPARK 30 Pro", self.con.execute(
            "SELECT variant FROM v_device_catalog").fetchone()["variant"])

    # -- distinct SKUs sharing one marketing name -----------------------------
    def test_two_codes_sharing_a_name_become_two_findable_devices(self) -> None:
        """Play lists eleven codes as "SPARK 7". They are not one phone.

        Before this, the first code alphabetically took the name and every other
        code was dropped along with its firmware -- so SPARK 7 answered for all
        of them with one SKU's builds, and a user with a KF6n found nothing.
        """
        self.write_play([("Tecno", "SPARK 7", "TECNO-KF6", "TECNO KF6"),
                         ("Tecno", "SPARK 7", "TECNO-KF6n", "TECNO KF6n")])
        self.add_product("p1", "KF6")
        self.add_product("p2", "KF6n")
        self.review()
        result = promote_approved_products_to_devices(self.con)
        self.assertEqual(0, result.skipped_collision, "no product may be silently dropped")
        rows = {r["model_code"]: r["variant"] for r in
                self.con.execute("SELECT model_code, variant FROM v_device_catalog")}
        self.assertEqual({"KF6", "KF6n"}, set(rows),
                         "both codes must be findable by the code the phone reports")
        self.assertIn("SPARK 7", rows["KF6"])
        self.assertIn("SPARK 7", rows["KF6n"])
        self.assertNotEqual(rows["KF6"], rows["KF6n"], "the two devices need distinct names")

    def test_the_same_code_spelled_with_a_brand_prefix_links_instead_of_duplicating(self) -> None:
        """"TECNO CM5" and "CM5" are one code. The mismatch stranded 1,287 rows."""
        self.write_play([("Tecno", "CAMON 40", "TECNO-CM5", "TECNO CM5")])
        self.add_product("p1", "TECNO CM5")
        self.review()
        promote_approved_products_to_devices(self.con)
        self.assertEqual(1, self.con.execute("SELECT count(*) FROM hardware_models").fetchone()[0])
        self.add_product("p2", "CM5")
        self.review()
        result = promote_approved_products_to_devices(self.con)
        self.assertEqual(1, self.con.execute("SELECT count(*) FROM hardware_models").fetchone()[0],
                         "the bare code must link to the prefixed device, not duplicate it")
        self.assertEqual(0, result.skipped_collision)
        self.assertEqual(2, self.con.execute(
            "SELECT count(*) FROM product_hardware_links").fetchone()[0],
            "both products must reach the device so both carry their firmware there")

    # -- the re-evaluation gate ----------------------------------------------
    def test_an_unresolved_conclusion_is_reconsidered_under_a_new_rule_version(self) -> None:
        """Otherwise the new rule would never run: every product already has one."""
        self.write_play([])
        self.add_product("p1", "CN7c")
        self.review()
        self.assertEqual("insufficient_evidence", self.conclusion("p1")[0])
        self.con.execute("UPDATE identity_conclusions SET rule_version='0' WHERE product_id='p1'")
        self.write_play([("Tecno", "CAMON 50 Ultra", "TECNO-CN7c", "TECNO CN7c")])
        self.review()
        self.assertEqual("auto_approved", self.conclusion("p1")[0])

    def test_an_approved_conclusion_is_never_reopened(self) -> None:
        """A settled identity must not change because the catalogue moved."""
        self.write_play([("Tecno", "CAMON 50 Ultra", "TECNO-CN7c", "TECNO CN7c")])
        self.add_product("p1", "CN7c")
        self.review()
        self.assertEqual("auto_approved", self.conclusion("p1")[0])
        self.con.execute("UPDATE identity_conclusions SET rule_version='0' WHERE product_id='p1'")
        self.write_play([])  # catalogue no longer carries it
        self.review()
        self.assertEqual("auto_approved", self.conclusion("p1")[0],
                         "a resolved identity must survive the evidence going away")

    # -- withdrawing an identity ----------------------------------------------
    def test_rejecting_a_product_withdraws_its_device_claim(self) -> None:
        """A reviewer saying "wrong" must stop the device serving that firmware.

        Rejection updated review_state and the link states but left
        product_hardware_links and the hardware_model_id promotion had
        backfilled onto the firmware -- so the device kept answering with builds
        attributed to an identity that had just been rejected.
        """
        import tempfile as _tempfile
        from mobile_observatory.server import ObservatoryService

        self.write_play([("Tecno", "CAMON 50 Ultra", "TECNO-CN7c", "TECNO CN7c")])
        self.add_product("p1", "CN7c")
        self.review()
        promote_approved_products_to_devices(self.con)
        device = self.con.execute("SELECT hardware_model_id FROM product_hardware_links").fetchone()
        self.assertIsNotNone(device, "precondition: the product was promoted")

        local = Path(self.enterContext(_tempfile.TemporaryDirectory())) / "local.sqlite"
        service = ObservatoryService(self.db, local, demonstration=False)
        self.addCleanup(service.local.close)
        service.review_source_product("p1", "rejected")

        self.assertIsNone(
            self.con.execute("SELECT 1 FROM product_hardware_links WHERE product_id='p1'").fetchone(),
            "a rejected identity must not keep claiming a device")
        self.assertEqual(0, self.con.execute(
            """SELECT count(*) FROM product_firmware_releases
                WHERE product_id='p1' AND hardware_model_id IS NOT NULL""").fetchone()[0],
            "its firmware must stop being attributed to that device")
        recorded = self.con.execute(
            """SELECT reason FROM source_data_corrections
                WHERE entity_id='p1' AND reason='identity_rejected_link_withdrawn'""").fetchone()
        self.assertIsNotNone(recorded, "the withdrawal must be auditable")

    # -- vendor spelling ------------------------------------------------------
    def test_a_spec_only_product_adopts_the_corpus_spelling_of_its_vendor(self) -> None:
        """The path that actually produced the split, not a hand-inserted row.

        enrich_product_specs creates a product for a captured specification that
        no existing product claims, using the SOURCE's brand spelling. GSMArena
        writes "Tecno"; the corpus has 618 products under "TECNO" and a
        manufacturer to match. Promotion looks manufacturers up by exact name,
        so the unnormalised spelling mints a second vendor.
        """
        self.write_play([])
        self.specs.write_text(
            "brand,device,slug,device_name,chipset,chipset-hl,fetched_at\n"
            "Tecno,camon-50,tecno_camon_50-1,Tecno Camon 50,Helio G99,Helio G99,%s\n" % NOW,
            encoding="utf-8")
        self.review()
        makers = {row[0] for row in self.con.execute("SELECT DISTINCT manufacturer FROM source_products")}
        self.assertTrue(makers, "the spec should have produced a product")
        self.assertIn("TECNO", makers)
        self.assertNotIn("Tecno", makers,
                         "a spec-only product must adopt the corpus spelling, not GSMArena's")
        self.assertNotIn("vendor_spelled_several_ways", {f.check for f in check_corpus(self.con)})

    def test_one_vendor_spelled_two_ways_is_caught(self) -> None:
        """Play says "Tecno", the feeds say "TECNO"; promotion matches exactly."""
        self.assertNotIn("vendor_spelled_several_ways",
                         {f.check for f in check_corpus(self.con)})
        self.add_product("p1", "CN7c", manufacturer="TECNO")
        self.add_product("p2", "KJ5", manufacturer="Tecno")
        self.assertIn("vendor_spelled_several_ways", {f.check for f in check_corpus(self.con)})


if __name__ == "__main__":
    unittest.main()
