"""The projection must cover both fact layers, and must publish all-or-nothing.

The bug this guards against is not a slow query, it is a false sentence. Before
the projection existed, /api/v1/devices reported "Catalogued; firmware not
observed" for 156 of 303 canonical devices whose firmware the corpus had
observed -- every Xiaomi, itel and Infinix device and 40 TECNOs -- because the
only latest-firmware view read firmware_releases, which holds Samsung and
nothing else. A device with 22 observed builds was told it had none.

Each test here fails if that regresses. The evidence-layer test is the important
one: reverting the projection to read firmware_releases alone leaves it the only
failure in the suite.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory import current_firmware as cf  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService  # noqa: E402

NOW = "2026-09-01T00:00:00Z"


def _evidence_layer_device(db, *, product="p-evi", model="EVI-1",
                           builds=(("GLOBAL", "EVI.BUILD.002", "2026-03-04"),
                                   ("GLOBAL", "EVI.BUILD.001", "2025-01-02"))):
    """A canonical device whose firmware exists ONLY in product_firmware_releases.

    This is the shape the old read path could not see: a promoted device, linked
    firmware, and not one row in firmware_releases.
    """
    from mobile_observatory.repository import CanonicalRepository
    connection = db.connection
    hardware = CanonicalRepository(db).create_device(
        manufacturer="Xiaomi", brand="Xiaomi", family="Evidence fixture",
        variant="Evidence fixture", model_code=model)
    src = "xiaomi.community.firmware_tracker"
    connection.execute("INSERT OR IGNORE INTO sources(id,name,base_url,authority_scope,enabled,created_at) VALUES(?,'x',NULL,'primary',1,?)", (src, NOW))
    connection.execute("INSERT OR IGNORE INTO ingestion_runs VALUES('run-evi',?,?,?,'succeeded','p','1',0,0,0,NULL)",
                       (src, NOW, NOW))
    connection.execute("INSERT OR IGNORE INTO artifacts VALUES('art-evi',?,'run-evi',?,'text/csv',NULL,?,'x',1)",
                       (src, "c" * 64, NOW))
    connection.execute("INSERT INTO source_products VALUES(?,'Xiaomi','Evidence fixture','evidence fixture','approved',NULL,?,?)",
                       (product, NOW, NOW))
    connection.execute(
        """INSERT INTO source_identity_registry VALUES
           ('ident-evi',?,'codename','evifix','evifix',?,'approved','test','1','high',?,?)""",
        (src, product, NOW, NOW))
    connection.execute("INSERT INTO product_hardware_links VALUES(?,?,'test',NULL,?)",
                       (product, hardware, NOW))
    for index, (region, build, released) in enumerate(builds):
        observation = f"obs-evi-{index}"
        connection.execute(
            "INSERT INTO observations VALUES(?,?,'run-evi','art-evi','firmware_release',?,?,'{}',?,'valid',NULL)",
            (observation, src, f"key-{index}", NOW, f"{index:064d}"))
        connection.execute(
            """INSERT INTO product_firmware_releases
               (id,product_id,identity_id,observation_id,source_id,region_code,build_id,channel,
                android_version,android_major,vendor_released_at,delivery_method,created_at,hardware_model_id)
               VALUES(?,?,'ident-evi',?,?,?,?,'Stable','14',14,?,NULL,?,?)""",
            (f"rel-evi-{index}", product, observation, src, region, build, released, NOW, hardware))
    return hardware


class ProjectionCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.hardware = _evidence_layer_device(self.db)
        cf.build(self.db)
        self.service = ObservatoryService(
            self.db, Path(self.temp.name) / "local.sqlite", demonstration=True)
        self.addCleanup(self.service.local.close)

    def test_evidence_layer_firmware_reaches_the_device_grid(self) -> None:
        """The 156-device falsehood, as one assertion."""
        row = self.service.devices_page({"model": ["EVI-1"]}).items[0]
        self.assertGreater(row["firmware_count"], 0,
                           "a device with observed firmware must not report a count of 0")
        self.assertNotEqual(row["region"], "Catalogued; firmware not observed",
                            "firmware WAS observed for this device; saying otherwise is false")
        self.assertEqual(row["build"], "EVI.BUILD.002", "must pick the newest stated release date")
        self.assertEqual(row["fact_layer"], "evidence")
        self.assertEqual(row["software_state_basis"], "vendor_release_date")

    def test_no_device_is_told_firmware_was_not_observed_while_it_was(self) -> None:
        """Whole-catalogue invariant, not one sampled row."""
        liars = []
        for row in self.service.devices_page({"limit": ["500"]}).items:
            observed = self.db.connection.execute(
                """SELECT (SELECT count(*) FROM firmware_releases WHERE hardware_model_id=?)
                        + (SELECT count(*) FROM product_firmware_releases WHERE hardware_model_id=?)""",
                (row["id"], row["id"])).fetchone()[0]
            if observed and row["firmwareCoverage"] == "not_observed":
                liars.append(f"{row['maker']} {row['name']}: {observed} releases observed, UI says not_observed")
        self.assertEqual([], liars, "\n".join(liars))

    def test_the_detail_panel_shows_the_same_history_the_grid_counts(self) -> None:
        """Two views of one device must not disagree.

        The detail panel's ROM history read a view fed by a single source, which
        covers Samsung only. So an evidence-layer device -- every TECNO, itel,
        Infinix and Xiaomi -- opened to "CAPTURED ROM HISTORY · 0" and "No
        captured ROMs match this selection" while the grid row behind it said
        "6 builds · 4 regions". Caught by watching a screen recording of the
        real app, not by a test.
        """
        row = self.service.devices_page({"model": ["EVI-1"]}).items[0]
        self.assertGreater(row["firmware_count"], 0, "precondition: the grid counts builds")
        detail = self.service.device_detail("EVI-1")
        self.assertEqual(row["firmware_count"], detail["firmware"]["meta"]["page"]["total"],
                         "the detail panel must account for every build the grid counts")
        self.assertTrue(detail["firmware"]["items"], "and it must actually list them")
        listed = {item["build"] for item in detail["firmware"]["items"]}
        self.assertIn("EVI.BUILD.002", listed)

    def test_both_layers_are_present_in_the_projection(self) -> None:
        layers = {r[0] for r in self.db.connection.execute(
            "SELECT DISTINCT fact_layer FROM device_current_firmware")}
        self.assertEqual({"canonical", "evidence"}, layers)


class DeviceHeadlineRowTest(unittest.TestCase):
    """The row the grid shows must not be chosen by comparing publishers' dates."""

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.con = self.db.connection

    def _source(self, source_id: str, rank: int) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
            " VALUES(?,?,NULL,'secondary',1,?)", (source_id, source_id, NOW))
        self.con.execute("UPDATE sources SET currency_rank=? WHERE id=?", (rank, source_id))
        self.con.execute(
            "INSERT OR IGNORE INTO ingestion_runs VALUES(?,?,?,?,'succeeded','p','1',0,0,0,NULL)",
            (f"run-{source_id}", source_id, NOW, NOW))
        self.con.execute(
            "INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,'text/csv',NULL,?,'x',1)",
            (f"art-{source_id}", source_id, f"run-{source_id}", source_id.ljust(64, "0")[:64], NOW))

    def test_the_most_current_publisher_wins_regardless_of_date(self) -> None:
        """An archive with a NEWER date must not outrank a live check-in.

        google.ota.checkin says what the vendor's servers would hand the device
        today; an archive row says a build once existed. Ordering their dates
        against each other treats two different measurements as one, which is
        the comparison this corpus forbids. Before the fix, 218 of 274
        multi-publisher devices had their headline row decided exactly that way.
        """
        from mobile_observatory.repository import CanonicalRepository

        hardware = CanonicalRepository(self.db).create_device(
            manufacturer="TECNO", brand="TECNO", family="Two publishers",
            variant="Two publishers", model_code="TP-1")
        self.con.execute("INSERT INTO source_products VALUES('p-tp','TECNO','TP','tp','approved',NULL,?,?)",
                         (NOW, NOW))
        self._source("archive.community", 50)
        self._source("google.ota.checkin", 10)
        self.con.execute(
            """INSERT INTO source_identity_registry VALUES
               ('id-tp','archive.community','codename','tp','tp','p-tp','approved','t','1','high',?,?)""",
            (NOW, NOW))
        rows = [("archive.community", "GLOBAL", "ARCHIVE.NEWER", "2026-09-30"),
                ("google.ota.checkin", "EU", "OTA.OLDER", "2026-01-01")]
        for index, (source, region, build, released) in enumerate(rows):
            observation = f"obs-tp-{index}"
            self.con.execute(
                "INSERT INTO observations VALUES(?,?,?,?,'firmware_release',?,?,'{}',?,'valid',NULL)",
                (observation, source, f"run-{source}", f"art-{source}", f"k{index}", NOW,
                 f"{index:064d}"))
            self.con.execute(
                """INSERT INTO product_firmware_releases
                   (id,product_id,identity_id,observation_id,source_id,region_code,build_id,channel,
                    android_version,android_major,vendor_released_at,delivery_method,created_at,hardware_model_id)
                   VALUES(?,'p-tp','id-tp',?,?,?,?,'Stable','14',14,?,NULL,?,?)""",
                (f"rel-tp-{index}", observation, source, region, build, released, NOW, hardware))
        cf.build(self.db)

        primary = self.con.execute(
            "SELECT build_id, source_id FROM device_current_firmware "
            "WHERE hardware_model_id=? AND is_device_primary=1", (hardware,)).fetchone()
        self.assertEqual("OTA.OLDER", primary["build_id"],
                         "the more current publisher must win even with an older date; "
                         "picking ARCHIVE.NEWER means the two dates were compared")
        self.assertEqual("google.ota.checkin", primary["source_id"])

    def test_the_region_count_matches_the_region_list_beside_it(self) -> None:
        """The grid prints both. They came from different expressions.

        device_target_total counted (target, channel) partitions while
        device_target_codes listed DISTINCT codes, so a device with the same
        region on two channels read "11 regions" above a list of 6. A number and
        the list it summarises disagreeing in the same cell is a number nobody
        can trust.
        """
        from mobile_observatory.repository import CanonicalRepository

        hardware = CanonicalRepository(self.db).create_device(
            manufacturer="TECNO", brand="TECNO", family="Channels",
            variant="Channels", model_code="CH-1")
        self.con.execute("INSERT INTO source_products VALUES('p-ch','TECNO','CH','ch','approved',NULL,?,?)",
                         (NOW, NOW))
        self._source("archive.community", 50)
        self.con.execute(
            """INSERT INTO source_identity_registry VALUES
               ('id-ch','archive.community','codename','ch','ch','p-ch','approved','t','1','high',?,?)""",
            (NOW, NOW))
        # One region, two channels -> one REGION, two partitions.
        for index, channel in enumerate(("Stable", "Beta")):
            observation = f"obs-ch-{index}"
            self.con.execute(
                "INSERT INTO observations VALUES(?,'archive.community','run-archive.community',"
                "'art-archive.community','firmware_release',?,?,'{}',?,'valid',NULL)",
                (observation, f"ck{index}", NOW, f"{index:064d}"))
            self.con.execute(
                """INSERT INTO product_firmware_releases
                   (id,product_id,identity_id,observation_id,source_id,region_code,build_id,channel,
                    android_version,android_major,vendor_released_at,delivery_method,created_at,hardware_model_id)
                   VALUES(?,'p-ch','id-ch',?,'archive.community','GLOBAL',?,?,'14',14,'2026-01-01',NULL,?,?)""",
                (f"rel-ch-{index}", observation, f"B{index}", channel, NOW, hardware))
        cf.build(self.db)

        row = self.con.execute(
            "SELECT device_target_total, device_target_codes FROM device_current_firmware "
            "WHERE hardware_model_id=? AND is_device_primary=1", (hardware,)).fetchone()
        listed = len([code for code in (row["device_target_codes"] or "").split(",") if code])
        self.assertEqual(listed, row["device_target_total"],
                         f"the count says {row['device_target_total']} but the list holds "
                         f"{listed}: {row['device_target_codes']}")
        self.assertEqual(1, row["device_target_total"], "one region on two channels is one region")

    def test_a_sibling_models_build_never_becomes_the_headline(self) -> None:
        """"i3Pro-..." on the device "i3" is another phone's ROM.

        Five devices had exactly such a build as their current firmware --
        TECNO i3, W3, i5, W5 and itel S11 each told a user their phone was
        running a different model's release. The row stays in the corpus and in
        the ROM history, attributed to the source that filed it; it is only
        barred from being the answer.
        """
        from mobile_observatory.repository import CanonicalRepository

        hardware = CanonicalRepository(self.db).create_device(
            manufacturer="TECNO", brand="TECNO", family="Sibling",
            variant="Sibling", model_code="TECNO i3")
        self.con.execute("INSERT INTO source_products VALUES('p-sb','TECNO','SB','sb','approved',NULL,?,?)",
                         (NOW, NOW))
        self._source("archive.community", 50)
        self.con.execute(
            """INSERT INTO source_identity_registry VALUES
               ('id-sb','archive.community','codename','sb','sb','p-sb','approved','t','1','high',?,?)""",
            (NOW, NOW))
        # The sibling build is NEWER, so only the exclusion can keep it out.
        rows = [("i3Pro-H375D1-N-IN-190416V304", "2026-09-01"),
                ("i3-H375A1-N-IN-180101V100", "2020-01-01")]
        for index, (build, released) in enumerate(rows):
            observation = f"obs-sb-{index}"
            self.con.execute(
                "INSERT INTO observations VALUES(?,'archive.community','run-archive.community',"
                "'art-archive.community','firmware_release',?,?,'{}',?,'valid',NULL)",
                (observation, f"sk{index}", NOW, f"{index:064d}"))
            self.con.execute(
                """INSERT INTO product_firmware_releases
                   (id,product_id,identity_id,observation_id,source_id,region_code,build_id,channel,
                    android_version,android_major,vendor_released_at,delivery_method,created_at,hardware_model_id)
                   VALUES(?,'p-sb','id-sb',?,'archive.community','GLOBAL',?,'Stable','14',14,?,NULL,?,?)""",
                (f"rel-sb-{index}", observation, build, released, NOW, hardware))
        cf.build(self.db)

        headline = self.con.execute(
            "SELECT build_id FROM device_current_firmware "
            "WHERE hardware_model_id=? AND is_device_primary=1", (hardware,)).fetchone()
        self.assertIsNotNone(headline, "the genuine build must still be served")
        self.assertEqual("i3-H375A1-N-IN-180101V100", headline["build_id"],
                         "a sibling model's ROM must never be this device's current firmware")
        self.assertEqual(2, self.con.execute(
            "SELECT count(*) FROM product_firmware_releases WHERE hardware_model_id=?",
            (hardware,)).fetchone()[0],
            "the row is excluded from SELECTION, not deleted from the corpus")

    def test_the_headline_row_says_how_many_publishers_describe_the_device(self) -> None:
        """One publisher's account must not read as the only account.

        Four community archives share a currency rank, so when several describe
        one device the choice falls to a deterministic string sort. That is a
        display choice among rows that do not contradict each other -- but a
        reader shown one build has no way to tell it from an uncontested one
        unless the count travels with it.
        """
        from mobile_observatory.repository import CanonicalRepository

        hardware = CanonicalRepository(self.db).create_device(
            manufacturer="TECNO", brand="TECNO", family="Many",
            variant="Many", model_code="MANY-1")
        self.con.execute("INSERT INTO source_products VALUES('p-mp','TECNO','MP','mp','approved',NULL,?,?)",
                         (NOW, NOW))
        for source in ("archive.one", "archive.two"):
            self._source(source, 50)
        self.con.execute(
            """INSERT INTO source_identity_registry VALUES
               ('id-mp','archive.one','codename','mp','mp','p-mp','approved','t','1','high',?,?)""",
            (NOW, NOW))
        for index, source in enumerate(("archive.one", "archive.two")):
            observation = f"obs-mp-{index}"
            self.con.execute(
                "INSERT INTO observations VALUES(?,?,?,?,'firmware_release',?,?,'{}',?,'valid',NULL)",
                (observation, source, f"run-{source}", f"art-{source}", f"mk{index}", NOW,
                 f"{index:064d}"))
            self.con.execute(
                """INSERT INTO product_firmware_releases
                   (id,product_id,identity_id,observation_id,source_id,region_code,build_id,channel,
                    android_version,android_major,vendor_released_at,delivery_method,created_at,hardware_model_id)
                   VALUES(?,'p-mp','id-mp',?,?,?,?,'Stable','14',14,'2026-01-01',NULL,?,?)""",
                (f"rel-mp-{index}", observation, source, f"R{index}", f"B{index}", NOW, hardware))
        cf.build(self.db)
        row = self.con.execute(
            "SELECT device_source_count FROM device_current_firmware "
            "WHERE hardware_model_id=? AND is_device_primary=1", (hardware,)).fetchone()
        self.assertEqual(2, row["device_source_count"])

    def test_exactly_one_primary_row_per_device(self) -> None:
        cf.build(self.db)
        self.assertEqual(0, self.con.execute(
            """SELECT count(*) FROM (SELECT hardware_model_id FROM device_current_firmware
                                      GROUP BY hardware_model_id HAVING sum(is_device_primary)<>1)"""
        ).fetchone()[0])


class ProjectionPublicationTest(unittest.TestCase):
    """Publication is all-or-nothing, and a bad build never replaces a good one."""

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")

    def test_generation_advances_and_state_is_recorded(self) -> None:
        first = cf.build(self.db)
        second = cf.build(self.db)
        self.assertEqual(first.generation + 1, second.generation)
        self.assertEqual(first.digest, second.digest, "same inputs must publish the same digest")
        self.assertEqual(cf.state(self.db.connection)["generation"], second.generation)

    def test_a_failed_validation_leaves_the_previous_generation_serving(self) -> None:
        good = cf.build(self.db)
        served = self.db.connection.execute("SELECT count(*) FROM device_current_firmware").fetchone()[0]
        self.assertGreater(served, 0)

        # Plant a defect the validator is supposed to catch: a staged row naming
        # a device that does not exist.
        original = cf.CANONICAL_SQL
        try:
            cf.CANONICAL_SQL = (
                "INSERT INTO device_current_firmware_staging (hardware_model_id,target_key,channel,"
                "fact_layer,firmware_release_id,product_firmware_release_id,source_id,build_id,"
                "android_version,android_major,security_patch_level,security_patch_level_source_id,"
                "effective_at,effective_at_basis,latest_basis,release_count) "
                "VALUES('no-such-device','X','stable','canonical','no-such-release',NULL,NULL,'B',"
                "NULL,NULL,NULL,NULL,NULL,'not_captured','observation_order_only',1)")
            with self.assertRaises(cf.ProjectionError):
                cf.build(self.db)
        finally:
            cf.CANONICAL_SQL = original

        self.assertEqual(
            self.db.connection.execute("SELECT count(*) FROM device_current_firmware").fetchone()[0],
            served, "a rejected build must not have emptied or altered the served table")
        self.assertEqual(cf.state(self.db.connection)["generation"], good.generation,
                         "a rejected build must not advance the published generation")

    def test_validation_rejects_a_patch_level_with_no_publisher(self) -> None:
        """The attribution check must be able to fail, or it proves nothing."""
        cf.build(self.db)
        self.db.connection.execute(
            """INSERT INTO device_current_firmware_staging
               (hardware_model_id,target_key,channel,fact_layer,firmware_release_id,
                product_firmware_release_id,source_id,build_id,android_version,android_major,
                security_patch_level,security_patch_level_source_id,effective_at,
                effective_at_basis,latest_basis,release_count)
               VALUES('x','X','stable','canonical','r',NULL,NULL,'B',NULL,NULL,
                      NULL,'samsung.doc.aspl',NULL,'not_captured','observation_order_only',1)""")
        with self.assertRaises(cf.ProjectionError) as caught:
            cf._validate(self.db.connection)
        self.assertIn("unattributed", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
