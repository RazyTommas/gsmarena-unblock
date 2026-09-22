from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import CanonicalRepository, Database  # noqa: E402
from mobile_observatory.collectors import device_promotion as dp  # noqa: E402
from mobile_observatory.server import ObservatoryService  # noqa: E402


def _insert_product(connection, *, product_id, manufacturer, canonical_name,
                     review_state="approved", now="2026-09-01T00:00:00Z"):
    normalized_name = canonical_name.casefold()
    connection.execute(
        "INSERT INTO source_products VALUES(?,?,?,?,?,NULL,?,?)",
        (product_id, manufacturer, canonical_name, normalized_name, review_state, now, now),
    )


def _insert_conclusion(connection, *, product_id, evidence, method="exact_unique_google_play_name",
                        conclusion="auto_approved", now="2026-09-01T00:00:00Z"):
    connection.execute(
        """INSERT INTO identity_conclusions
           (product_id, conclusion, confidence, method, rule_version, rationale,
            candidates_json, evidence_json, concluded_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (product_id, conclusion, "high", method, "1", "test fixture",
         "[]", json.dumps(evidence), now),
    )


def _google_play_evidence(model_codes, identity=None):
    entry = {"source": "google_play_supported_devices", "model_codes": list(model_codes)}
    return [entry] if identity is None else [
        {"source": "xiaomi_devices_yml", "identity": identity}, entry,
    ]


class DevicePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database.migrated()
        self.con = self.db.connection

    def tearDown(self) -> None:
        self.db.close()

    def _device_count(self) -> int:
        return self.con.execute("SELECT count(*) FROM hardware_models").fetchone()[0]

    # -- requirement 3: the review gate is never widened -----------------------
    def test_proposed_product_is_never_promoted(self) -> None:
        _insert_product(self.con, product_id="p1", manufacturer="TECNO",
                         canonical_name="TECNO SPARK 40", review_state="proposed")
        _insert_conclusion(self.con, product_id="p1", evidence=_google_play_evidence(["TECNO ZZ1"]),
                            conclusion="ambiguous")
        result = dp.promote_approved_products_to_devices(self.con)
        self.assertEqual(result.skipped_not_approved, 1)
        self.assertEqual(result.promoted, 0)
        self.assertEqual(self._device_count(), 0)

    # -- requirement 4: no fabricated model codes -------------------------------
    def test_approved_product_without_an_observed_model_code_is_not_promoted(self) -> None:
        _insert_product(self.con, product_id="p1", manufacturer="TECNO", canonical_name="TECNO SPARK 40")
        _insert_conclusion(self.con, product_id="p1", evidence=[
            {"source": "gsmarena_captured_specs", "slug": "tecno_spark_40-99999.php"}
        ], method="exact_unique_spec_name")
        result = dp.promote_approved_products_to_devices(self.con)
        self.assertEqual(result.skipped_no_model_code, 1)
        self.assertEqual(result.promoted, 0)
        self.assertEqual(self._device_count(), 0)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM product_hardware_links").fetchone()[0], 0
        )

    # -- root cause: any manufacturer, not just a hardcoded Samsung Electronics -
    def test_promotes_a_non_samsung_manufacturer_from_a_genuine_observed_model_code(self) -> None:
        _insert_product(self.con, product_id="p1", manufacturer="TECNO", canonical_name="TECNO SPARK 40")
        _insert_conclusion(self.con, product_id="p1", evidence=_google_play_evidence(["TECNO LH7n"]),
                            method="tecno_vendor_device_scope")
        result = dp.promote_approved_products_to_devices(self.con)
        self.assertEqual(result.promoted, 1)
        self.assertEqual(self._device_count(), 1)
        row = self.con.execute("SELECT * FROM v_device_catalog").fetchone()
        self.assertEqual(row["manufacturer"], "TECNO")
        self.assertEqual(row["brand"], "TECNO")
        self.assertEqual(row["model_code"], "TECNO LH7n")
        self.assertIsNone(row["codename"])  # never fabricated

    # -- requirement 1: idempotent replay ---------------------------------------
    def test_rerunning_promotion_creates_no_second_device(self) -> None:
        _insert_product(self.con, product_id="p1", manufacturer="Xiaomi", canonical_name="Xiaomi Pad 8")
        _insert_conclusion(self.con, product_id="p1",
                            evidence=_google_play_evidence(["25097RP43C"], identity="yupei"))
        first = dp.promote_approved_products_to_devices(self.con)
        self.assertEqual(first.promoted, 1)
        self.assertEqual(self._device_count(), 1)
        second = dp.promote_approved_products_to_devices(self.con)
        self.assertEqual(second.promoted, 0)
        self.assertEqual(second.already_linked, 1)
        self.assertEqual(self._device_count(), 1)

    # -- requirement 2: no duplicate devices, PLANTED violation -----------------
    def test_two_products_sharing_one_observed_model_code_do_not_produce_two_devices(self) -> None:
        """Plants exactly the corpus-observed failure mode: two different, legitimately
        approved products (different marketing names) whose only observed identifier --
        Google Play's Model field -- happens to be the same string. Without the
        cross-product collision guard this creates two hardware_models rows for one
        real code; this test proves it creates only one, and that the second product
        is refused rather than silently merged or silently duplicated.
        """
        _insert_product(self.con, product_id="p-base", manufacturer="TECNO", canonical_name="TECNO CAMON 40 5G")
        _insert_product(self.con, product_id="p-pro", manufacturer="TECNO", canonical_name="TECNO CAMON 40 Pro 5G")
        _insert_conclusion(self.con, product_id="p-base", evidence=_google_play_evidence(["TECNO CM7"]),
                            method="tecno_vendor_device_scope")
        _insert_conclusion(self.con, product_id="p-pro", evidence=_google_play_evidence(["TECNO CM7"]),
                            method="tecno_vendor_device_scope")
        result = dp.promote_approved_products_to_devices(self.con)
        self.assertEqual(result.promoted, 1)
        self.assertEqual(result.skipped_collision, 1)
        self.assertEqual(self._device_count(), 1)
        normalized = self.con.execute(
            "SELECT model_code_normalized, count(*) c FROM hardware_models GROUP BY 1"
        ).fetchall()
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["c"], 1)
        # Exactly one of the two products ended up linked; the other stayed unlinked.
        linked_products = {
            r["product_id"] for r in self.con.execute("SELECT product_id FROM product_hardware_links")
        }
        self.assertEqual(len(linked_products), 1)
        self.assertTrue(linked_products <= {"p-base", "p-pro"})

    # -- must not collide two different products onto one EXISTING device too --
    def test_two_products_cannot_both_link_to_the_same_existing_device(self) -> None:
        hardware_id = CanonicalRepository(self.db).create_device(
            manufacturer="Samsung", brand="Samsung", family="Galaxy A",
            variant="Galaxy A01", model_code="SM-A015F",
        )
        _insert_product(self.con, product_id="p1", manufacturer="Samsung", canonical_name="Samsung Galaxy A01")
        _insert_conclusion(self.con, product_id="p1", evidence=[], method="exact_unique_spec_name")
        result = dp.promote_approved_products_to_devices(self.con)
        self.assertEqual(result.linked_existing, 1)
        self.assertEqual(self._device_count(), 1)
        link = self.con.execute("SELECT hardware_model_id FROM product_hardware_links WHERE product_id='p1'").fetchone()
        self.assertEqual(link["hardware_model_id"], hardware_id)
        # A second, different product must never be allowed to claim the same device.
        try:
            self.con.execute(
                "INSERT INTO product_hardware_links VALUES(?,?,?,NULL,?)",
                ("p2", hardware_id, "manual_test_attempt", "2026-09-01T00:00:00Z"),
            )
            self.fail("a second product was allowed to link to an already-claimed device")
        except Exception as error:
            self.assertIn("UNIQUE", str(error).upper())

    # -- requirement 5: firmware stays reachable from the promoted device -------
    def test_firmware_is_reachable_from_the_promoted_device(self) -> None:
        now = "2026-09-01T00:00:00Z"
        self.con.execute("INSERT OR IGNORE INTO sources VALUES('xiaomi.community.firmware_tracker','x',NULL,'primary',1,?)", (now,))
        self.con.execute("INSERT OR IGNORE INTO ingestion_runs VALUES('run-1','xiaomi.community.firmware_tracker',?,?,'succeeded','p','1',0,0,0,NULL)", (now, now))
        self.con.execute("INSERT OR IGNORE INTO artifacts VALUES('art-1','xiaomi.community.firmware_tracker','run-1',?,'text/csv',NULL,?,'x',1)", ("a" * 64, now))
        self.con.execute(
            "INSERT INTO observations VALUES('obs-1','xiaomi.community.firmware_tracker','run-1','art-1','firmware_release','k1',?,'{}',?,'valid',NULL)",
            (now, "b" * 64),
        )
        _insert_product(self.con, product_id="p1", manufacturer="Xiaomi", canonical_name="Xiaomi Pad 8")
        _insert_conclusion(self.con, product_id="p1",
                            evidence=_google_play_evidence(["25097RP43C"], identity="yupei"))
        self.con.execute(
            """INSERT INTO source_identity_registry VALUES
               ('ident-1','xiaomi.community.firmware_tracker','codename','yupei','yupei','p1','approved',
                'test','1','high',?,?)""",
            (now, now),
        )
        self.con.execute(
            """INSERT INTO product_firmware_releases
               (id,product_id,identity_id,observation_id,source_id,region_code,build_id,channel,
                android_version,android_major,vendor_released_at,delivery_method,created_at)
               VALUES('rel-1','p1','ident-1','obs-1','xiaomi.community.firmware_tracker','GLOBAL',
                      'BUILD.001','stable','15',15,NULL,NULL,?)""",
            (now,),
        )
        self.assertIsNone(
            self.con.execute("SELECT hardware_model_id FROM product_firmware_releases WHERE id='rel-1'").fetchone()[0]
        )
        result = dp.promote_approved_products_to_devices(self.con)
        self.assertEqual(result.promoted, 1)
        self.assertEqual(result.firmware_links_backfilled, 1)
        hardware_id = self.con.execute("SELECT hardware_model_id FROM product_hardware_links WHERE product_id='p1'").fetchone()[0]
        linked = self.con.execute("SELECT hardware_model_id FROM product_firmware_releases WHERE id='rel-1'").fetchone()[0]
        self.assertEqual(linked, hardware_id)

    # -- API surface actually reflects the promotion, not just the database -----
    def test_product_detail_reports_established_hardware_coverage_once_promoted(self) -> None:
        _insert_product(self.con, product_id="p1", manufacturer="TECNO", canonical_name="TECNO SPARK 40")
        _insert_conclusion(self.con, product_id="p1", evidence=_google_play_evidence(["TECNO LH7n"]),
                            method="tecno_vendor_device_scope")
        with tempfile.TemporaryDirectory() as directory:
            service = ObservatoryService(self.db, Path(directory) / "local.sqlite", demonstration=False)
            before = service.product_detail("p1")
            self.assertEqual(before["coverage"]["hardware"], "not_established")
            self.assertIsNone(before["hardware"])
            dp.promote_approved_products_to_devices(self.con)
            after = service.product_detail("p1")
            self.assertEqual(after["coverage"]["hardware"], "established")
            self.assertEqual(after["hardware"]["model_code"], "TECNO LH7n")
            service.local.close()


if __name__ == "__main__":
    unittest.main()
