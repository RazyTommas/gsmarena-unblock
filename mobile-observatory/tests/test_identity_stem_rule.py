"""The stem rule writes FINAL approvals, so the tests that matter are the refusals.

`approve_stem_corroborated_identities` approves a bare Xiaomi codename stem
(`vili`) against a catalog that only ever carries its regional variants
(`vili_global`, `vili_eea_global`, ...). An approval licenses observations to
serve and `identity_conclusions` is final by design, so a rule that approves one
stem it should not have approved is not a bug that gets noticed later -- it is a
published falsehood with a vendor's name on it.

WHY A FIXTURE CATALOG AND NOT THE LIVE ONE, for the same reason
test_identity_backfill.py gives: against today's captured `devices.yml` the
older exact-key rule approves 0, so any test written against the live corpus that
asserts "it approves things" would be vacuously green -- green with the body
deleted. Every case below writes its own three-line catalog and its own Google
Play evidence, so each guard is exercised by a case built to defeat exactly that
guard.

The four that decide whether this is a rule or a rubber stamp:

  * a stem in NO captured `device_codes` list -- delete the corroboration check
    and this one passes, which is the whole difference between reading a source
    field and guessing from a string prefix;
  * one stem, two candidate products (`lisa` is this on the live corpus: Google
    Play lists it for both "Xiaomi 11 Lite 5G NE" and "Mi 11 LE");
  * a catalog variant naming a genuinely DIFFERENT phone -- the
    `jasmine -> "Mi A3 China"` shape borrowed from test_identity_backfill.py,
    where nothing about the identity looks suspicious because `jasmine` really is
    a Xiaomi codename;
  * no regional variant at all, so the stem/variant relationship the rule claims
    to read is simply not in the artifact.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory.database import Database  # noqa: E402
from mobile_observatory.enrichment import promote_approved_product_observations  # noqa: E402
from mobile_observatory.identity_backfill import (  # noqa: E402
    STEM_RULE, STEM_RULE_VERSION, approve_stem_corroborated_identities)

NOW = "2026-01-01T00:00:00Z"


def play_evidence(*device_codes: str, model_codes: tuple[str, ...] = ("M1",)) -> str:
    """An `identity_conclusions.evidence_json` shaped exactly as `_conclude` writes it."""
    return json.dumps([{"source": "google_play_supported_devices",
                        "marketing_name": "whatever",
                        "model_codes": list(model_codes),
                        "device_codes": list(device_codes)}], sort_keys=True)


class StemRuleTest(unittest.TestCase):

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        self.con = self.db.connection
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with self.con:
            self.con.execute(
                "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
                " VALUES('archive','Archive',NULL,'primary',1,?)", (NOW,))
            self.con.execute(
                "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,"
                "parser_version,accepted_count) VALUES('r','archive',?,'succeeded','p','1',0)",
                (NOW,))
            self.con.execute(
                "INSERT INTO artifacts VALUES('a','archive','r',?,'text/csv',NULL,?,'x',1)",
                ("a" * 64, NOW))

    # -- fixtures ------------------------------------------------------------

    def _catalog(self, body: str) -> Path:
        path = Path(self.tmp.name) / "devices.yml"
        path.write_text(body, encoding="utf-8")
        return path

    def _product(self, pid: str, name: str, *, evidence: str = "[]",
                 maker: str = "Xiaomi", review_state: str = "approved",
                 conclusion: str | None = "auto_approved") -> None:
        with self.con:
            self.con.execute("INSERT INTO source_products VALUES(?,?,?,?,?,NULL,?,?)",
                             (pid, maker, name, name.casefold(), review_state, NOW, NOW))
            if conclusion:
                self.con.execute(
                    "INSERT INTO identity_conclusions VALUES(?,?,'authoritative',"
                    "'xiaomi_vendor_codename_catalog','2','settled earlier','[]',?,?)",
                    (pid, conclusion, evidence, NOW))

    def _stranded_identity(self, iid: str, codename: str, pid: str) -> None:
        """An identity a LATER source contributed, with one firmware observation."""
        with self.con:
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES(?,'archive','codename',?,?,?,"
                "'proposed','deterministic_source_catalog','1','medium',?,?)",
                (iid, codename, codename.casefold(), pid, NOW, NOW))
            self.con.execute(
                "INSERT INTO observations VALUES(?,'archive','r','a','firmware_release',?,?,?,?,"
                "'valid',NULL)",
                ("o-" + iid, "o-" + iid, NOW,
                 '{"data":{"build":"V1.0.%s","region_code":"GLOBAL","branch":"Stable"}}' % iid,
                 f"{abs(hash(iid)):064d}"[:64]))
            self.con.execute("INSERT INTO observation_product_links VALUES(?,?,?,'proposed',?)",
                             ("o-" + iid, pid, iid, NOW))

    def _state(self, iid: str) -> tuple[str, str]:
        return tuple(self.con.execute(
            """SELECT sir.resolution_state, opl.link_state
                 FROM source_identity_registry sir
                 JOIN observation_product_links opl ON opl.identity_id = sir.id
                                                   AND opl.product_id = sir.product_id
                WHERE sir.id = ?""", (iid,)).fetchone())

    def _rationale(self, iid: str) -> dict:
        row = self.con.execute(
            "SELECT * FROM identity_resolution_rationales WHERE identity_id=?", (iid,)).fetchone()
        return dict(row) if row else {}

    VILI = "vili_eea_global:\n- Xiaomi 11T Pro EEA\n- VILIEEAGlobal\nvili_global:\n- Xiaomi 11T Pro Global\n- VILIGlobal\n"

    # -- the one case it exists for ------------------------------------------

    def test_approves_a_stem_the_captured_device_codes_corroborate_and_it_serves(self) -> None:
        self._product("p-11tpro", "Xiaomi 11T Pro", evidence=play_evidence("vili"))
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        totals = approve_stem_corroborated_identities(
            self.con, devices_yml=self._catalog(self.VILI))
        self.assertEqual(1, totals["approved"])
        self.assertEqual(("approved", "approved"), self._state("i-vili"))
        # The point of the approval is evidence reaching the surface, so assert
        # promotion rather than the state columns.
        promote_approved_product_observations(self.con)
        self.assertEqual(1, self.con.execute(
            "SELECT count(*) FROM product_firmware_releases").fetchone()[0])

    def test_the_approval_records_why_naming_the_stem_the_variants_and_the_play_evidence(self) -> None:
        """A decision whose basis is not recorded cannot be re-checked later."""
        self._product("p-11tpro", "Xiaomi 11T Pro", evidence=play_evidence("vili"))
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        approve_stem_corroborated_identities(self.con, devices_yml=self._catalog(self.VILI))
        note = self._rationale("i-vili")
        self.assertEqual(("approved", STEM_RULE, STEM_RULE_VERSION),
                         (note["outcome"], note["rule"], note["rule_version"]))
        self.assertIn("vili", note["rationale"])
        self.assertIn("vili_eea_global", note["rationale"])
        self.assertIn("Xiaomi 11T Pro", note["rationale"])
        evidence = json.loads(note["evidence_json"])
        play = next(e for e in evidence if e["source"] == "google_play_supported_devices")
        self.assertEqual(["vili"], play["device_codes"])
        # The field the rule is checkable against has to be NAMED in the record,
        # or a later reader has to guess where the corroboration came from.
        self.assertEqual("identity_conclusions.evidence_json[*].device_codes",
                         play["read_from"])
        catalog = next(e for e in evidence if e["source"] == "xiaomi_devices_yml")
        self.assertEqual(["vili_eea_global", "vili_global"],
                         sorted(catalog["catalog_variants"]))

    def test_it_creates_no_hardware_model_and_fabricates_no_model_code(self) -> None:
        """Approval means the identity belongs to the product. Nothing more."""
        self._product("p-11tpro", "Xiaomi 11T Pro", evidence=play_evidence("vili"))
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        approve_stem_corroborated_identities(self.con, devices_yml=self._catalog(self.VILI))
        self.assertEqual(0, self.con.execute(
            "SELECT count(*) FROM hardware_models").fetchone()[0])
        self.assertEqual(0, self.con.execute(
            "SELECT count(*) FROM product_hardware_links").fetchone()[0])
        self.assertEqual(0, self.con.execute(
            "SELECT count(*) FROM aliases").fetchone()[0])

    def test_it_does_not_reopen_or_rewrite_a_settled_product_conclusion(self) -> None:
        """The stem rule decides a REGISTRY row; conclusions are not its business.

        This is the measured argument against bumping enrichment.RULE_VERSION to
        announce this rule: the products it acts on are already auto_approved, so
        their conclusions are never reopened, and a bump would only reopen
        conclusions this rule cannot resolve.
        """
        self._product("p-11tpro", "Xiaomi 11T Pro", evidence=play_evidence("vili"))
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        before = self.con.execute("SELECT * FROM identity_conclusions").fetchall()
        approve_stem_corroborated_identities(self.con, devices_yml=self._catalog(self.VILI))
        after = self.con.execute("SELECT * FROM identity_conclusions").fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])

    def test_running_it_twice_changes_nothing(self) -> None:
        self._product("p-11tpro", "Xiaomi 11T Pro", evidence=play_evidence("vili"))
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        self._product("p-a2", "Mi A2", evidence=play_evidence("jasmine_sprout"))
        self._stranded_identity("i-jasmine", "jasmine", "p-a2")
        catalog = self._catalog(self.VILI + "jasmine_global:\n- Mi A2 Global\n")

        def state() -> list[tuple]:
            return ([tuple(r) for r in self.con.execute(
                        "SELECT * FROM source_identity_registry ORDER BY id")]
                    + [tuple(r) for r in self.con.execute(
                        "SELECT * FROM identity_resolution_rationales ORDER BY identity_id")]
                    + [tuple(r) for r in self.con.execute(
                        "SELECT * FROM observation_product_links ORDER BY observation_id")])

        first = approve_stem_corroborated_identities(self.con, devices_yml=catalog)
        snapshot = state()
        second = approve_stem_corroborated_identities(self.con, devices_yml=catalog)
        self.assertEqual(snapshot, state())
        self.assertEqual(1, first["approved"])
        # The approved row is no longer a candidate, so the second pass must not
        # report it again -- and must not have re-approved anything either.
        self.assertNotIn("approved", second)

    # -- the refusals, which are what make it a rule -------------------------

    def test_refuses_a_stem_absent_from_every_captured_device_codes_list(self) -> None:
        """Delete the corroboration check and this test passes. That is the point.

        The catalog carries `vili_global`/`vili_eea_global`, both naming this exact
        product, so every other signal agrees. The ONLY thing standing between the
        stem and an approval is that no captured Google Play row names it.
        """
        self._product("p-11tpro", "Xiaomi 11T Pro", evidence=play_evidence("xig01"))
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        totals = approve_stem_corroborated_identities(
            self.con, devices_yml=self._catalog(self.VILI))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_stem_not_in_captured_device_codes"])
        self.assertEqual(("proposed", "proposed"), self._state("i-vili"))
        promote_approved_product_observations(self.con)
        self.assertEqual(0, self.con.execute(
            "SELECT count(*) FROM product_firmware_releases").fetchone()[0])
        note = self._rationale("i-vili")
        self.assertEqual("refused", note["outcome"])
        self.assertIn("xig01", note["rationale"])

    def test_refuses_a_product_with_no_play_evidence_at_all(self) -> None:
        """46 of the 58 live stems are this: nothing recorded, so nothing to read.

        An absent list must read as absence, never as "no objection".
        """
        self._product("p-11tpro", "Xiaomi 11T Pro", evidence="[]")
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        totals = approve_stem_corroborated_identities(
            self.con, devices_yml=self._catalog(self.VILI))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_stem_not_in_captured_device_codes"])
        self.assertEqual(("proposed", "proposed"), self._state("i-vili"))

    def test_refuses_an_ambiguous_stem_two_products_name_the_same_device_code(self) -> None:
        """`lisa` on the live corpus: Xiaomi 11 Lite 5G NE and Mi 11 LE both claim it.

        The second claimant is a product this rule never examines -- it is already
        approved and carries no stranded identity -- so a lookup scoped to the
        candidates would not see it. Two candidates is not a match.
        """
        self._product("p-ne", "Xiaomi 11 Lite 5G NE", evidence=play_evidence("lisa"))
        self._product("p-le", "Mi 11 LE", evidence=play_evidence("lisa"))
        self._stranded_identity("i-lisa", "lisa", "p-ne")
        totals = approve_stem_corroborated_identities(self.con, devices_yml=self._catalog(
            "lisa_global:\n- Xiaomi 11 Lite 5G NE Global\n"
            "lisa_eea_global:\n- Xiaomi 11 Lite 5G NE EEA\n"))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_stem_names_several_products"])
        self.assertEqual(("proposed", "proposed"), self._state("i-lisa"))
        self.assertIn("2 different source products", self._rationale("i-lisa")["rationale"])

    def test_refuses_a_stem_two_products_registered_under_one_source_value(self) -> None:
        """One captured value, two products: approving it licenses the other's links.

        This is the HM2013023 / "Redmi 1" vs "Redmi 1 W" shape that
        identity_bridge.LINK_LICENCE_CONDITION exists for, seen one level up --
        here the approval itself is refused rather than only its spread.
        """
        self._product("p-11tpro", "Xiaomi 11T Pro", evidence=play_evidence("vili"))
        self._product("p-11t", "Xiaomi 11T", evidence=play_evidence("agate"))
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        with self.con:
            # A second source publishing the same value against the other product.
            self.con.execute(
                "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
                " VALUES('other','Other',NULL,'primary',1,?)", (NOW,))
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES('i-vili-2','other','codename',"
                "'vili','vili','p-11t','proposed','deterministic_source_catalog','1','medium',?,?)",
                (NOW, NOW))
        totals = approve_stem_corroborated_identities(
            self.con, devices_yml=self._catalog(self.VILI))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_stem_registered_on_several_products"])
        self.assertEqual(("proposed", "proposed"), self._state("i-vili"))

    def test_refuses_a_catalog_variant_naming_a_DIFFERENT_phone(self) -> None:
        """`jasmine` is a real Xiaomi codename. One variant calls it an Mi A3.

        Borrowed from tests/test_identity_backfill.py, one level harder: here the
        OTHER variant does name the product, and Google Play does corroborate the
        stem, so every other signal says approve. Only the requirement that the
        catalog agree with ITSELF catches that one variant is a different phone.
        Live analogue: the bare key `lisa` names "Mi 11 LE China" while
        `lisa_global` names "Xiaomi 11 Lite 5G NE Global".
        """
        self._product("p-a2", "Mi A2", evidence=play_evidence("jasmine"))
        self._stranded_identity("i-jasmine", "jasmine", "p-a2")
        totals = approve_stem_corroborated_identities(self.con, devices_yml=self._catalog(
            "jasmine_global:\n- Mi A2 Global\n- JASMINEGlobal\n"
            "jasmine_cn:\n- Mi A3 China\n- JASMINECN\n"))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_catalog_variants_disagree"])
        self.assertEqual(("proposed", "proposed"), self._state("i-jasmine"))
        note = self._rationale("i-jasmine")
        self.assertIn("Mi A3 China", note["rationale"])
        promote_approved_product_observations(self.con)
        self.assertEqual(0, self.con.execute(
            "SELECT count(*) FROM product_firmware_releases").fetchone()[0])

    def test_refuses_a_bare_catalog_key_naming_a_different_phone(self) -> None:
        """The live `lisa` shape exactly: the BARE key disagrees with the variants."""
        self._product("p-ne", "Xiaomi 11 Lite 5G NE", evidence=play_evidence("lisa"))
        self._stranded_identity("i-lisa", "lisa", "p-ne")
        totals = approve_stem_corroborated_identities(self.con, devices_yml=self._catalog(
            "lisa:\n- Mi 11 LE China\n- LISA\n"
            "lisa_global:\n- Xiaomi 11 Lite 5G NE Global\n"))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_catalog_variants_disagree"])
        self.assertEqual(("proposed", "proposed"), self._state("i-lisa"))

    def test_refuses_a_stem_with_no_regional_variant_in_the_catalog(self) -> None:
        """The relationship the rule reads is stem<->regional variant.

        With only a bare key, the vendor has said nothing about a stem standing for
        a family of regional codenames, and Google Play's device code alone is not
        this rule's licence to approve -- it is only half of it.
        """
        self._product("p-11tpro", "Xiaomi 11T Pro", evidence=play_evidence("vili"))
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        totals = approve_stem_corroborated_identities(self.con, devices_yml=self._catalog(
            "vili:\n- Xiaomi 11T Pro\n"))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_stem_has_no_regional_catalog_variants"])
        self.assertEqual(("proposed", "proposed"), self._state("i-vili"))

    def test_a_short_stem_does_not_swallow_an_unrelated_codename(self) -> None:
        """`lisa` must not collect `lisandra`: the suffix separator is required."""
        self._product("p-ne", "Xiaomi 11 Lite 5G NE", evidence=play_evidence("lisa"))
        self._stranded_identity("i-lisa", "lisa", "p-ne")
        totals = approve_stem_corroborated_identities(self.con, devices_yml=self._catalog(
            "lisandra_global:\n- Redmi Note 99 Global\n"))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_stem_has_no_regional_catalog_variants"])

    def test_refuses_an_identity_on_a_product_that_was_never_approved(self) -> None:
        self._product("p-pending", "Redmi 1", evidence=play_evidence("armani"),
                      review_state="proposed", conclusion="insufficient_evidence")
        self._stranded_identity("i-pending", "armani", "p-pending")
        totals = approve_stem_corroborated_identities(self.con, devices_yml=self._catalog(
            "armani_global:\n- Redmi 1 Global\n"))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["skipped_product_not_approved"])
        self.assertEqual(("proposed", "proposed"), self._state("i-pending"))
        self.assertEqual({}, self._rationale("i-pending"))

    def test_refuses_a_vendor_the_xiaomi_catalog_has_no_authority_over(self) -> None:
        self._product("p-tecno", "CAMON 20", evidence=play_evidence("ck6n"), maker="TECNO")
        self._stranded_identity("i-tecno", "ck6n", "p-tecno")
        totals = approve_stem_corroborated_identities(self.con, devices_yml=self._catalog(
            "ck6n_global:\n- CAMON 20\n"))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["skipped_not_xiaomi"])
        self.assertEqual(("proposed", "proposed"), self._state("i-tecno"))

    def test_an_empty_catalog_approves_nothing_rather_than_everything(self) -> None:
        self._product("p-11tpro", "Xiaomi 11T Pro", evidence=play_evidence("vili"))
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        totals = approve_stem_corroborated_identities(
            self.con, devices_yml=self._catalog("# nothing captured\n"))
        self.assertEqual({"catalog_empty": 1}, totals)
        self.assertEqual(("proposed", "proposed"), self._state("i-vili"))

    def test_malformed_play_evidence_refuses_rather_than_approves(self) -> None:
        """evidence_json is written elsewhere; an unexpected shape must not approve."""
        for evidence in ('{"source":"google_play_supported_devices"}',
                         '[{"source":"google_play_supported_devices","device_codes":"vili"}]',
                         '[["google_play_supported_devices",["vili"]]]',
                         '[]'):
            with self.subTest(evidence=evidence):
                self.setUp()
                self._product("p-11tpro", "Xiaomi 11T Pro", evidence=evidence)
                self._stranded_identity("i-vili", "vili", "p-11tpro")
                totals = approve_stem_corroborated_identities(
                    self.con, devices_yml=self._catalog(self.VILI))
                self.assertEqual(0, totals.get("approved", 0))
                self.assertEqual(("proposed", "proposed"), self._state("i-vili"))

    def test_does_not_license_a_link_on_another_product(self) -> None:
        """The approval is for one product; the link mirror must not spread it."""
        self._product("p-11tpro", "Xiaomi 11T Pro", evidence=play_evidence("vili"))
        self._product("p-other", "Xiaomi 11T Pro Lite")
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        with self.con:
            self.con.execute(
                "INSERT INTO observations VALUES('o-other','archive','r','a','firmware_release',"
                "'o-other',?,'{\"data\":{\"build\":\"OTHER\"}}',?,'valid',NULL)", (NOW, "b" * 64))
            self.con.execute("INSERT INTO observation_product_links VALUES"
                             "('o-other','p-other','i-vili','proposed',?)", (NOW,))
        approve_stem_corroborated_identities(self.con, devices_yml=self._catalog(self.VILI))
        self.assertEqual({"o-i-vili": "approved", "o-other": "proposed"}, dict(self.con.execute(
            "SELECT observation_id, link_state FROM observation_product_links")))


class TheStemRuleIsInThePipelineTest(unittest.TestCase):
    """A rule with no caller is not a rule, it is a file.

    `approve_catalog_confirmed_identities` was correct and unreachable for eight
    days and nothing in the suite noticed. Asserted on the compiled code object,
    so a comment mentioning the call cannot pass it.
    """

    def test_run_batch_calls_the_stem_rule(self) -> None:
        from mobile_observatory import batch

        self.assertIn("approve_stem_corroborated_identities",
                      set(batch._ingest.__code__.co_names),
                      "the stem rule releases captured evidence that otherwise cannot reach "
                      "the surface; it has to run in the batch, not exist beside it")

    def test_the_stem_rule_is_versioned_apart_from_the_conclusion_rule(self) -> None:
        """Bumping enrichment.RULE_VERSION reopens conclusions; this rule must not.

        Measured on the live corpus 2026-09-30: a bump from "2" to "3" reopens the
        465 conclusions that resolved nothing under version 2, of which 0 carry a
        Google Play device code -- so the bump re-decides 465 products to announce
        a rule that can resolve none of them. The two versions are therefore
        separate constants, and this test fails if they are ever merged.
        """
        from mobile_observatory import enrichment
        from mobile_observatory import identity_backfill

        self.assertNotEqual(enrichment.RULE_VERSION, identity_backfill.STEM_RULE_VERSION)


if __name__ == "__main__":
    unittest.main()
