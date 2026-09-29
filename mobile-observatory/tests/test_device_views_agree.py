"""One device must not describe itself two ways.

Reproduced on the live 865-device corpus:

    GET /api/v1/devices?model=TECNO%20i3
        firmware_count 0, firmwareCoverage "not_observed",
        region "Catalogued; firmware not observed"

    GET /api/v1/devices/TECNO%20i3
        "Captured ROM history · 1", build i3Pro-H375D1-N-IN-190416V304

Neither number was invented. The grid read device_current_firmware -- the
projection of what is CURRENT -- and the detail view read
product_firmware_releases, what was CAPTURED. They differ for this device because
the projection deliberately refuses that build: `i3Pro-...` names a longer model
than the device it is filed under, so it belongs to a sibling phone, and
current_firmware.EVIDENCE_SQL excludes it from SELECTION while leaving the row in
place. Refusing to call another model's ROM this phone's current firmware is
right. Telling the reader "firmware not observed" on one screen and listing a
build on the next is not.

Two things this file deliberately does NOT do:

  * It does not decide whether that build really belongs to the TECNO i3. That is
    an identity judgement about a source's claim, it is not the read path's to
    make, and nothing here reattaches, deletes or re-reviews the row. The corpus
    already reports the class through the
    `firmware_build_names_a_sibling_model` invariant, which counts 8 such builds
    across 6 devices on the live corpus -- the i3Pro row among them.
  * It does not pin the live corpus's numbers. The fixture below rebuilds the
    exact shape from scratch, so the guard holds on any corpus.

The scope is the CLASS, not the instance: 7 of 865 devices had the grid
undercounting the detail view, all 7 carrying one of those 8 builds. TECNO i3 is
only the one where the gap crosses zero and the two views contradict each other
in words. Measured on the live corpus after the fix: 0 of 865 disagree.
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
from mobile_observatory.integrity import check_corpus  # noqa: E402
from mobile_observatory.repository import CanonicalRepository  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService  # noqa: E402

NOW = "2026-09-01T00:00:00Z"
SOURCE = "frbox.community.transsion_catalog"


def _device_with_only_a_sibling_build(db, *, model="EVI1", product="p-sib",
                                      build="EVI1Pro-H375D1-N-IN-190416V304"):
    """A promoted device whose ONLY captured release names a longer model.

    The TECNO i3 shape exactly: the product is genuinely `EVI1`, the source filed
    a build for `EVI1Pro` under it, the projection refuses to call that current,
    and the row stays in product_firmware_releases where the ROM-history panel
    reads it. The model code carries no dash, because the projection extracts the
    build's model prefix at the first dash.
    """
    connection = db.connection
    hardware = CanonicalRepository(db).create_device(
        manufacturer="TECNO", brand="TECNO", family="Sibling fixture",
        variant="Sibling fixture", model_code=model)
    connection.execute(
        "INSERT OR IGNORE INTO sources(id,name,base_url,authority_scope,enabled,created_at) "
        "VALUES(?,'frbox',NULL,'community',1,?)", (SOURCE, NOW))
    connection.execute(
        "INSERT OR IGNORE INTO ingestion_runs VALUES('run-sib',?,?,?,'succeeded','p','1',0,0,0,NULL)",
        (SOURCE, NOW, NOW))
    connection.execute(
        "INSERT OR IGNORE INTO artifacts VALUES('art-sib',?,'run-sib',?,'text/html',NULL,?,'x',1)",
        (SOURCE, "d" * 64, NOW))
    connection.execute(
        "INSERT INTO source_products VALUES(?,'TECNO','Sibling fixture','sibling fixture',"
        "'approved',NULL,?,?)", (product, NOW, NOW))
    connection.execute(
        "INSERT INTO source_identity_registry VALUES"
        "('ident-sib',?,'codename','sibfix','sibfix',?,'approved','test','1','high',?,?)",
        (SOURCE, product, NOW, NOW))
    connection.execute("INSERT INTO product_hardware_links VALUES(?,?,'test',NULL,?)",
                       (product, hardware, NOW))
    connection.execute(
        "INSERT INTO observations VALUES('obs-sib',?,'run-sib','art-sib','firmware_release',"
        "'key-sib',?,'{}',?,'valid',NULL)", (SOURCE, NOW, "1" * 64))
    connection.execute(
        """INSERT INTO product_firmware_releases
           (id,product_id,identity_id,observation_id,source_id,region_code,build_id,channel,
            android_version,android_major,vendor_released_at,delivery_method,created_at,
            hardware_model_id)
           VALUES('rel-sib',?,'ident-sib','obs-sib',?,'IN',?,'unknown',NULL,NULL,NULL,NULL,?,?)""",
        (product, SOURCE, build, NOW, hardware))
    return hardware


class TwoViewsOfOneDeviceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.hardware = _device_with_only_a_sibling_build(self.db)
        cf.build(self.db)
        self.service = ObservatoryService(
            self.db, Path(self.temp.name) / "local.sqlite", demonstration=True)
        self.addCleanup(self.service.local.close)

    def grid(self, model="EVI1") -> dict:
        rows = self.service.devices_page({"model_exact": [model], "model": [model]}).items
        matching = [row for row in rows if row["model"] == model]
        self.assertEqual(len(matching), 1, f"expected exactly one {model} row, got {rows}")
        return matching[0]

    def test_the_fixture_really_is_the_shape_under_test(self) -> None:
        """Precondition. Without this the tests below could pass because the
        projection accepted the build after all, i.e. by not reproducing anything."""
        rows = self.db.connection.execute(
            "SELECT count(*) FROM device_current_firmware WHERE hardware_model_id=?",
            (self.hardware,)).fetchone()[0]
        self.assertEqual(rows, 0, "the projection must refuse this build; if it does not, "
                                  "this fixture is not the TECNO i3 case")
        held = self.db.connection.execute(
            "SELECT count(*) FROM product_firmware_releases WHERE hardware_model_id=?",
            (self.hardware,)).fetchone()[0]
        self.assertEqual(held, 1, "and the row must still be in the corpus")

    def test_the_grid_count_equals_the_detail_rom_history_total(self) -> None:
        """The defect, as one assertion: 0 on the grid, 1 in the detail view."""
        detail = self.service.device_detail("EVI1")
        self.assertEqual(self.grid()["firmware_count"],
                         detail["firmware"]["meta"]["page"]["total"],
                         "the grid and the device detail view report different numbers of "
                         "builds for the same device")

    def test_the_two_views_agree_for_every_device_in_the_corpus(self) -> None:
        """The class, not the instance. On the live corpus 7 of 865 diverged; a
        guard that only checked the fixture would have passed on all 7 before the
        fix, since only one of them crossed zero."""
        disagree = []
        for row in self.service.devices_page({"limit": ["500"]}).items:
            detail = self.service.device_detail(row["model"])
            if row["firmware_count"] != detail["firmware"]["meta"]["page"]["total"]:
                disagree.append((row["model"], row["firmware_count"],
                                 detail["firmware"]["meta"]["page"]["total"]))
        self.assertEqual([], disagree,
                         "these devices report one build count on the grid and another in "
                         "their own detail view: " + repr(disagree))

    def test_a_device_holding_builds_is_not_called_unobserved(self) -> None:
        """The sentence, not just the number. "Catalogued; firmware not observed"
        beside a listed build is the falsehood a reader actually saw."""
        row = self.grid()
        self.assertEqual(row["firmwareCoverage"], self.service.COVERAGE_HELD_NOT_CURRENT)
        self.assertNotEqual(row["firmwareCoverage"], self.service.COVERAGE_NOT_OBSERVED)
        self.assertNotIn("not observed", row["region"].lower())

    def test_a_device_with_nothing_captured_is_still_called_unobserved(self) -> None:
        """The new state must not have swallowed the old one: absence is still
        recorded as absence."""
        bare = CanonicalRepository(self.db).create_device(
            manufacturer="TECNO", brand="TECNO", family="Bare", variant="Bare",
            model_code="BARE1")
        cf.build(self.db)
        row = self.grid("BARE1")
        self.assertEqual(row["firmware_count"], 0)
        self.assertEqual(row["firmwareCoverage"], self.service.COVERAGE_NOT_OBSERVED)
        self.assertIn("not observed", row["region"].lower())
        self.assertEqual(
            self.service.device_detail("BARE1")["firmware"]["meta"]["page"]["total"], 0)
        self.assertTrue(bare)

    def test_a_device_with_current_firmware_reads_as_observed(self) -> None:
        """And the ordinary case is untouched."""
        row = self.grid("SM-S931B")
        self.assertEqual(row["firmwareCoverage"], self.service.COVERAGE_OBSERVED)
        self.assertGreater(row["firmware_count"], 0)

    def test_the_detail_view_publishes_the_same_coverage_and_a_note(self) -> None:
        """Both views quote one sentence, so neither can be updated alone."""
        detail = self.service.device_detail("EVI1")
        self.assertEqual(detail["firmwareCoverage"], self.grid()["firmwareCoverage"])
        self.assertEqual(detail["firmwareCoverageNote"], self.grid()["region"])

    def test_no_current_firmware_is_claimed_for_the_sibling_build(self) -> None:
        """The presentation fix must not have promoted the refused build. The
        projection's refusal is the correct behaviour and stays."""
        detail = self.service.device_detail("EVI1")
        self.assertEqual(detail["latestFirmware"], [],
                         "a sibling model's ROM must never be stated as this device's "
                         "current firmware")
        self.assertIsNone(self.grid()["build"])


class SiblingModelInvariantTest(unittest.TestCase):
    """What the corpus already reports about this class of source defect.

    Recorded here because the investigation's answer was counterintuitive: the
    check was suspected of UNDER-reporting the i3Pro case and it does not. On the
    live corpus it counts 8, and querying the same SQL with the rows listed
    rather than counted shows i3Pro-H375D1-N-IN-190416V304 on TECNO i3 as one of
    the 8 -- alongside L9Plus (twice), W3Pro, i5Pro, S11Plus, W5Lite and
    DP10APro. It is 8 and not 9 because the i3Pro row was never missing from it.

    This test asserts the property that matters -- a device whose only build
    names a sibling model IS counted -- rather than the number 8, which belongs
    to one corpus.
    """

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")

    def _count(self) -> int:
        findings = {f.check: f.count for f in check_corpus(self.db.connection, deep=False)}
        return findings.get("firmware_build_names_a_sibling_model", 0)

    def test_a_sibling_named_build_is_reported(self) -> None:
        cf.build(self.db)
        before = self._count()
        _device_with_only_a_sibling_build(self.db)
        cf.build(self.db)
        self.assertEqual(self._count(), before + 1,
                         "a build whose identifier names a longer model than the device it "
                         "is filed under must be reported; this is the source's mistake and "
                         "it must not be invisible")

    def test_a_build_naming_its_own_device_is_not_reported(self) -> None:
        """The check must stay narrow. It fires only when the build's model prefix
        starts with the device's own code AND is longer, so an ordinary build
        never reaches it."""
        cf.build(self.db)
        before = self._count()
        _device_with_only_a_sibling_build(self.db, model="OWN1", product="p-own",
                                          build="OWN1-H375D1-N-IN-190416V304")
        cf.build(self.db)
        self.assertEqual(self._count(), before)


if __name__ == "__main__":
    unittest.main()
