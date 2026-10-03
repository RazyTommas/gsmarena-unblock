"""The ROM-branch rule must REFUSE, and must never mint a phone out of a branch.

WHY THESE TESTS DRIVE A FIXTURE CATALOG AND NOT THE LIVE ONE. Measured 2026-10-04
on a copy of the live corpus: the rule examines 154 Xiaomi codename registry rows
and approves 55, so a test asserting only "it approves things" against the live
corpus would be green with every refusal deleted. Each refusal below is therefore
driven from a catalog written in the test, and the shapes were taken from the real
corpus rather than invented:

  * `marble` -- the case the rule exists for. Xiaomi's catalog names the China
    phone under the bare key and the global phone under the regional keys; the
    mifirm archive names both, joined by a slash. Nothing matches by exact key.
  * `emerald` -- the catalog names a phone the archive does not (`emerald_r_*` is
    a different branch the stem prefix over-collects). Must refuse.
  * `platina` -- "Mi 8 Lite/Youth": the archive names a phone the catalog does
    not, because the slash here is two names for ONE phone. Must refuse, or the
    rule invents a device.
  * the tracker publishing a bare stem -- `devices.yml` is captured from the same
    export, so this is one publisher agreeing with itself. Must refuse.

THE TEST THAT DECIDES WHETHER THIS IS A RULE OR A RUBBER STAMP is
`test_an_approved_branch_creates_no_device`: the whole claim of the ROM-branch
model is that a branch spanning three phones becomes neither one device nor three,
and that is enforced by what this rule declines to write
(`identity_conclusions`), not by a promise in a docstring. Measured on the live
corpus: 20 products approved, 8,168 releases promoted, **0** new
`hardware_models`, `device_variants` or `product_hardware_links`.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory.collectors.device_promotion import (  # noqa: E402
    promote_approved_products_to_devices)
from mobile_observatory.database import Database  # noqa: E402
from mobile_observatory.enrichment import promote_approved_product_observations  # noqa: E402
from mobile_observatory.integrity import check_corpus  # noqa: E402
from mobile_observatory.rom_branch import (BRANCH_RULE, BRANCH_RULE_VERSION,  # noqa: E402
                                           approve_branch_corroborated_identities,
                                           branch_members, name_set)

NOW = "2026-01-01T00:00:00Z"
ARCHIVE = "mifirm.community.firmware_archive"
TRACKER = "xiaomi.community.firmware_tracker"
#: A second, hypothetical archive. `source_identity_registry` is
#: UNIQUE(source_id,namespace,normalized_value), so one publisher can file a
#: codename only once -- the two-products-one-branch case is unreachable with the
#: single archive the corpus has today, which is exactly why it is tested from a
#: fixture rather than asserted against the corpus.
OTHER_ARCHIVE = "other.community.firmware_archive"

MARBLE_CATALOG = (
    "marble:\n- Redmi Note 12 Turbo China\n- MARBLE\n"
    "marble_global:\n- POCO F5 Global\n- MARBLEGlobal\n"
    "marble_eea_global:\n- POCO F5 EEA\n- MARBLEEEAGlobal\n")


class BranchRuleTest(unittest.TestCase):

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        self.con = self.db.connection
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with self.con:
            for source in (ARCHIVE, TRACKER, OTHER_ARCHIVE):
                self.con.execute(
                    "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
                    " VALUES(?,?,NULL,'primary',1,?)", (source, source, NOW))
            self.con.execute(
                "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,"
                "parser_version,accepted_count) VALUES('r',?,?,'succeeded','p','1',0)",
                (ARCHIVE, NOW))
            self.con.execute("INSERT INTO artifacts VALUES('a',?,'r',?,'text/csv',NULL,?,'x',1)",
                             (ARCHIVE, "a" * 64, NOW))

    # -- fixture builders ----------------------------------------------------

    def _catalog(self, body: str) -> Path:
        path = Path(self.tmp.name) / "devices.yml"
        path.write_text(body, encoding="utf-8")
        return path

    def _product(self, pid: str, name: str, *, maker: str = "Xiaomi",
                 review_state: str = "unresolvable_on_captured_evidence",
                 conclusion: str | None = "ambiguous") -> None:
        with self.con:
            self.con.execute("INSERT INTO source_products VALUES(?,?,?,?,?,NULL,?,?)",
                             (pid, maker, name, name.casefold(), review_state, NOW, NOW))
            if conclusion:
                self.con.execute(
                    "INSERT INTO identity_conclusions VALUES(?,?,'medium','ranked_candidates',"
                    "'2','several candidates','[]','[]',?)", (pid, conclusion, NOW))

    def _identity(self, iid: str, codename: str, pid: str, *, source: str = ARCHIVE,
                  observations: int = 1) -> None:
        with self.con:
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES(?,?,'codename',?,?,?,"
                "'proposed','deterministic_source_catalog','1','medium',?,?)",
                (iid, source, codename, codename.casefold(), pid, NOW, NOW))
            for index in range(observations):
                oid = f"o-{iid}-{index}"
                self.con.execute(
                    "INSERT INTO observations VALUES(?,?,'r','a','firmware_release',?,?,?,?,"
                    "'valid',NULL)",
                    (oid, source, oid, NOW,
                     json.dumps({"data": {"build": f"V1.0.{index}", "region_code": "GLOBAL",
                                          "branch": "Stable"}}),
                     f"{abs(hash(oid)):064d}"[:64]))
                self.con.execute("INSERT INTO observation_product_links VALUES(?,?,?,'proposed',?)",
                                 (oid, pid, iid, NOW))

    def _link_to(self, iid: str, other_pid: str) -> None:
        """A second product reached from the SAME identity row -- the real collision."""
        with self.con:
            oid = f"o-{iid}-cross"
            self.con.execute(
                "INSERT INTO observations VALUES(?,?,'r','a','firmware_release',?,?,?,?,'valid',NULL)",
                (oid, ARCHIVE, oid, NOW, '{"data":{"build":"V9"}}', f"{abs(hash(oid)):064d}"[:64]))
            self.con.execute("INSERT INTO observation_product_links VALUES(?,?,?,'proposed',?)",
                             (oid, other_pid, iid, NOW))

    def _state(self, iid: str) -> tuple[str, str]:
        return tuple(self.con.execute(
            """SELECT sir.resolution_state, sp.review_state
                 FROM source_identity_registry sir
                 JOIN source_products sp ON sp.id = sir.product_id
                WHERE sir.id = ?""", (iid,)).fetchone())

    def _rationale(self, iid: str) -> tuple[str, str] | None:
        row = self.con.execute(
            "SELECT outcome, reason FROM identity_resolution_rationales"
            " WHERE identity_id=? AND rule=?", (iid, BRANCH_RULE)).fetchone()
        return tuple(row) if row else None

    def _run(self, catalog: str, **kwargs) -> dict[str, int]:
        return approve_branch_corroborated_identities(
            self.con, devices_yml=self._catalog(catalog), **kwargs)

    # -- the case the rule exists for ----------------------------------------

    def test_approves_a_branch_both_publishers_name_the_same_way(self) -> None:
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-marble", "marble", "p-marble", observations=3)
        totals = self._run(MARBLE_CATALOG)
        self.assertEqual(1, totals["approved"])
        self.assertEqual(1, totals["products_approved"])
        self.assertEqual(("approved", "approved"), self._state("i-marble"))
        self.assertEqual(("approved", "branch_corroborated_by_two_publishers"),
                         self._rationale("i-marble"))
        # The point of an approval is the evidence reaching the surface, so assert
        # promotion rather than the state columns.
        promote_approved_product_observations(self.con)
        self.assertEqual(3, self.con.execute(
            "SELECT count(*) FROM product_firmware_releases").fetchone()[0])

    def test_the_approved_branch_records_its_members_and_they_read_back(self) -> None:
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-marble", "marble", "p-marble")
        self._run(MARBLE_CATALOG)
        self.assertEqual({"p-marble": ["poco f5", "redmi note 12 turbo"]},
                         branch_members(self.con))

    def test_an_approved_branch_creates_no_device(self) -> None:
        """A branch spanning two phones becomes neither one device nor two.

        This is the test the ROM-branch model lives or dies on. The guarantee is
        structural: the rule writes no `auto_approved` conclusion, so
        `_observed_model_code` finds no model code and device promotion refuses.
        """
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-marble", "marble", "p-marble")
        self._run(MARBLE_CATALOG)
        promote_approved_product_observations(self.con)
        result = promote_approved_products_to_devices(self.con)
        self.assertEqual(0, result.promoted)
        self.assertEqual(1, result.skipped_no_model_code)
        for table in ("hardware_models", "device_variants", "device_families",
                      "product_hardware_links"):
            self.assertEqual(0, self.con.execute(
                f"SELECT count(*) FROM {table}").fetchone()[0], table)

    def test_the_no_device_assertion_is_not_vacuous(self) -> None:
        """The positive control for the test above.

        `promote_approved_products_to_devices` is given the SAME fixture, plus the
        one thing the branch rule withholds: a conclusion carrying a Google Play
        model code. A device appears. So the assertion above is a measurement of
        this rule's behaviour and not a property of a fixture that could never
        promote anything -- which is how a guard comes to assert a condition only
        true where nothing happens.
        """
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-marble", "marble", "p-marble")
        self._run(MARBLE_CATALOG)
        with self.con:
            self.con.execute(
                "UPDATE identity_conclusions SET conclusion='auto_approved', evidence_json=?"
                " WHERE product_id='p-marble'",
                (json.dumps([{"source": "google_play_supported_devices",
                              "matched_on": "model_code", "model_codes": ["FAKECODE"],
                              "marketing_names": ["Branch Phone"],
                              "device_name": "Branch Phone"}]),))
        promote_approved_product_observations(self.con)
        result = promote_approved_products_to_devices(self.con)
        self.assertEqual(1, result.promoted)
        self.assertEqual(1, self.con.execute(
            "SELECT count(*) FROM hardware_models").fetchone()[0])

    def test_the_rule_does_not_rewrite_the_conclusion(self) -> None:
        """"Which phone is this" stays ambiguous, because it is.

        Writing `auto_approved` here would be both a false statement and the thing
        that lets device promotion key a phone off a branch name.
        """
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-marble", "marble", "p-marble")
        before = self.con.execute("SELECT * FROM identity_conclusions").fetchall()
        self._run(MARBLE_CATALOG)
        after = self.con.execute("SELECT * FROM identity_conclusions").fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
        self.assertEqual("ambiguous", self.con.execute(
            "SELECT conclusion FROM identity_conclusions WHERE product_id='p-marble'").fetchone()[0])

    # -- the refusals, which are what make it a rule -------------------------

    def test_refuses_when_the_vendor_catalog_names_a_phone_the_archive_does_not(self) -> None:
        """`emerald`: the stem prefix also collects `emerald_r_*`, a second branch.

        The archive names two phones and the catalog names three. Approving on a
        subset would say the branch is smaller than the vendor says it is.
        """
        self._product("p-emerald", "Redmi Note 13 Pro 4G / POCO M6 Pro")
        self._identity("i-emerald", "emerald", "p-emerald")
        totals = self._run(
            "emerald_global:\n- Redmi Note 13 Pro 4G / POCO M6 Pro Global\n- EMERALDGlobal\n"
            "emerald_r_global:\n- Redmi Note 14S Global\n- EMERALDRGlobal\n")
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_vendor_catalog_names_a_phone_the_archive_does_not"])
        self.assertEqual(("proposed", "unresolvable_on_captured_evidence"),
                         self._state("i-emerald"))
        self.assertEqual(("refused", "vendor_catalog_names_a_phone_the_archive_does_not"),
                         self._rationale("i-emerald"))

    def test_refuses_when_the_archive_names_a_phone_the_catalog_does_not(self) -> None:
        """"Mi 8 Lite/Youth" is ONE phone with two names, and nothing says so.

        Five products on the live corpus are this shape. Reading the slash as
        "several phones" here would invent a phone called "Youth".
        """
        self._product("p-platina", "Mi 8 Lite/Youth")
        self._identity("i-platina", "platina", "p-platina")
        totals = self._run("platina:\n- Mi 8 Lite China\n- PLATINA\n")
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_archive_names_a_phone_the_vendor_catalog_does_not"])
        self.assertEqual(("refused", "archive_names_a_phone_the_vendor_catalog_does_not"),
                         self._rationale("i-platina"))

    def test_refuses_a_set_that_differs_in_both_directions(self) -> None:
        self._product("p-ingres", "Redmi K50 Gaming / Poco F4 GT")
        self._identity("i-ingres", "ingres", "p-ingres")
        totals = self._run("ingres:\n- Redmi K50G China\n- INGRES\n"
                           "ingres_global:\n- POCO F4 GT Global\n- INGRESGlobal\n")
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_branch_name_sets_disagree"])

    def test_refuses_the_catalogs_own_publisher_as_its_own_corroboration(self) -> None:
        """devices.yml ships in the tracker's export, so this is one publisher.

        Exactly the condition that makes the 66 Apple products unresolvable, where
        every piece of evidence is ipsw.me.
        """
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-marble", "marble", "p-marble", source=TRACKER)
        totals = self._run(MARBLE_CATALOG)
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_corroborating_source_is_the_same_publisher"])
        self.assertEqual(("refused", "corroborating_source_is_the_same_publisher"),
                         self._rationale("i-marble"))

    def test_refuses_a_stem_the_catalog_does_not_carry(self) -> None:
        self._product("p-unknown", "Redmi Phantom / POCO Ghost")
        self._identity("i-unknown", "nosuchstem", "p-unknown")
        totals = self._run(MARBLE_CATALOG)
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_stem_absent_from_vendor_catalog"])

    def test_refuses_a_catalog_entry_whose_shape_is_not_name_then_code(self) -> None:
        """Entry 0 is the marketing name in all 1,422 captured keys. If that stops
        being true, reading entry 0 is comparing a build code to a phone."""
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-marble", "marble", "p-marble")
        totals = self._run("marble:\n- Redmi Note 12 Turbo China\n"
                           "marble_global:\n- POCO F5 Global\n- MARBLEGlobal\n")
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_catalog_entry_shape_unexpected"])

    def test_refuses_an_identity_row_reached_from_another_product(self) -> None:
        """The real collision: ONE registry row, two products.

        `source_identity_registry.id` is uuid5(source_id,namespace,normalized_value)
        with no product component, so approving such a row hands one product's
        approval to another product's links.
        """
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._product("p-other", "Something Else")
        self._identity("i-marble", "marble", "p-marble")
        self._link_to("i-marble", "p-other")
        totals = self._run(MARBLE_CATALOG)
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["refused_identity_linked_from_another_product"])

    def test_refuses_when_two_products_carry_the_same_branch_name_set(self) -> None:
        """Two candidates is not a match, and the answer must not depend on order."""
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._product("p-twin", "POCO F5 / Redmi Note 12 Turbo")
        self._identity("i-marble", "marble", "p-marble")
        self._identity("i-marble-2", "marble", "p-twin", source=OTHER_ARCHIVE)
        totals = self._run(MARBLE_CATALOG)
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(2, totals["refused_several_products_share_this_branch"])
        self.assertEqual(("refused", "several_products_share_this_branch"),
                         self._rationale("i-marble"))
        self.assertEqual(("refused", "several_products_share_this_branch"),
                         self._rationale("i-marble-2"))

    def test_refuses_a_product_a_human_has_deferred(self) -> None:
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-marble", "marble", "p-marble")
        totals = self._run(MARBLE_CATALOG,
                           decisions=[{"decision": "defer", "canonical_id": "p-marble"}])
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["skipped_human_decision"])
        self.assertIsNone(self._rationale("i-marble"))

    def test_an_absent_catalog_is_not_no_objection(self) -> None:
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-marble", "marble", "p-marble")
        totals = approve_branch_corroborated_identities(
            self.con, devices_yml=Path(self.tmp.name) / "missing.yml")
        self.assertEqual({"catalog_absent": 1}, totals)
        self.assertEqual(("proposed", "unresolvable_on_captured_evidence"),
                         self._state("i-marble"))

    # -- properties of the whole pass ----------------------------------------

    def test_every_judged_row_records_why(self) -> None:
        """A refusal nobody can read is a number in a log. Three different
        outcomes, three rationale rows, zero silent decisions."""
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-marble", "marble", "p-marble")
        self._product("p-platina", "Mi 8 Lite/Youth")
        self._identity("i-platina", "platina", "p-platina")
        self._product("p-unknown", "Redmi Phantom")
        self._identity("i-unknown", "nosuchstem", "p-unknown")
        self._run(MARBLE_CATALOG + "platina:\n- Mi 8 Lite China\n- PLATINA\n")
        rows = self.con.execute(
            "SELECT identity_id, outcome, rule_version FROM identity_resolution_rationales"
            " WHERE rule=? ORDER BY identity_id", (BRANCH_RULE,)).fetchall()
        self.assertEqual([("i-marble", "approved", BRANCH_RULE_VERSION),
                          ("i-platina", "refused", BRANCH_RULE_VERSION),
                          ("i-unknown", "refused", BRANCH_RULE_VERSION)],
                         [tuple(r) for r in rows])

    def test_a_second_pass_over_an_unchanged_corpus_changes_nothing(self) -> None:
        self._product("p-marble", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-marble", "marble", "p-marble")
        self._product("p-platina", "Mi 8 Lite/Youth")
        self._identity("i-platina", "platina", "p-platina")
        catalog = MARBLE_CATALOG + "platina:\n- Mi 8 Lite China\n- PLATINA\n"
        self._run(catalog)
        first = tuple(self.con.execute(
            "SELECT count(*), sum(resolution_state='approved') FROM source_identity_registry"
        ).fetchone())
        rationales = self.con.execute(
            "SELECT count(*) FROM identity_resolution_rationales").fetchone()[0]
        second = self._run(catalog)
        self.assertEqual(0, second.get("approved", 0))
        self.assertEqual(first, tuple(self.con.execute(
            "SELECT count(*), sum(resolution_state='approved') FROM source_identity_registry"
        ).fetchone()))
        self.assertEqual(rationales, self.con.execute(
            "SELECT count(*) FROM identity_resolution_rationales").fetchone()[0])

    def test_it_says_nothing_about_another_manufacturer(self) -> None:
        self._product("p-tecno", "SPARK 30 / SPARK 30C", maker="TECNO")
        self._identity("i-tecno", "marble", "p-tecno")
        totals = self._run(MARBLE_CATALOG)
        self.assertEqual(1, totals["skipped_not_xiaomi"])
        self.assertIsNone(self._rationale("i-tecno"))


class BranchNameFoldingTest(unittest.TestCase):
    """`+` is a phone, not punctuation.

    `enrichment._norm` strips every non-alphanumeric character, so "Pro+" and
    "Pro" fold to the same string. Both sides of the rule would fold identically
    and the sets would still match, which is precisely the problem: the rule would
    be unable to tell two phones apart.
    """

    def test_plus_survives_the_fold(self) -> None:
        self.assertNotEqual(name_set("Redmi Note 12 Pro"), name_set("Redmi Note 12 Pro+"))

    def test_a_slash_list_becomes_one_member_per_phone(self) -> None:
        self.assertEqual({"redmi note 12 pro", "pro plus", "discovery"},
                         set(name_set("Redmi Note 12 Pro/Pro+/Discovery")))

    def test_the_region_suffix_is_stripped_on_every_member(self) -> None:
        self.assertEqual(name_set("Redmi Note 12 Turbo / POCO F5 Global"),
                         name_set("Redmi Note 12 Turbo / POCO F5"))

    def test_order_is_not_a_fact_about_the_phones(self) -> None:
        self.assertEqual(name_set("POCO F5 / Redmi Note 12 Turbo"),
                         name_set("Redmi Note 12 Turbo / POCO F5"))


class TwinnedBranchFindingTest(unittest.TestCase):
    """One codename under two products is reported, never repaired here."""

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        self.con = self.db.connection
        with self.con:
            for source in (ARCHIVE, TRACKER):
                self.con.execute(
                    "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
                    " VALUES(?,?,NULL,'primary',1,?)", (source, source, NOW))

    def _product(self, pid: str, name: str) -> None:
        with self.con:
            self.con.execute("INSERT INTO source_products VALUES(?,'Xiaomi',?,?,'approved',NULL,?,?)",
                             (pid, name, name.casefold(), NOW, NOW))

    def _identity(self, iid: str, value: str, pid: str, source: str) -> None:
        with self.con:
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES(?,?,'codename',?,?,?,"
                "'proposed','deterministic_source_catalog','1','medium',?,?)",
                (iid, source, value, value.casefold(), pid, NOW, NOW))

    def _finding(self):
        return {f.check: f for f in check_corpus(self.con, deep=False)}

    def test_one_codename_under_two_products_is_reported(self) -> None:
        self._product("p-a", "Redmi Note 12 Turbo")
        self._product("p-b", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-a", "marble", "p-a", TRACKER)
        self._identity("i-b", "marble", "p-b", ARCHIVE)
        found = self._finding()
        self.assertIn("rom_branch_held_as_two_source_products", found)
        finding = found["rom_branch_held_as_two_source_products"]
        self.assertEqual(1, finding.count)
        self.assertEqual("warning", finding.severity)
        self.assertIn("marble", finding.detail)

    def test_one_codename_under_one_product_is_not_reported(self) -> None:
        self._product("p-b", "Redmi Note 12 Turbo / POCO F5")
        self._identity("i-b", "marble", "p-b", ARCHIVE)
        self.assertNotIn("rom_branch_held_as_two_source_products", self._finding())


class TheBatchRunsTheRuleTest(unittest.TestCase):
    """A rule nothing calls approves nothing, and that failure is silent.

    `approve_catalog_confirmed_identities` sat in this repo unreferenced from
    2026-09-22, which is the precedent for checking the wiring rather than
    assuming it.
    """

    def test_run_batch_calls_the_branch_rule_as_its_own_phase(self) -> None:
        source = (ROOT / "src" / "mobile_observatory" / "batch.py").read_text(encoding="utf-8")
        self.assertIn("approve_branch_corroborated_identities(", source)
        self.assertIn("identity:rom-branch-name-set", source)
        # After the stem rule and BEFORE adjudication: a product must not be
        # called unresolvable while a rule that could resolve it has not run.
        self.assertLess(source.index("identity:stem-corroborated"),
                        source.index("identity:rom-branch-name-set"))
        self.assertLess(source.index("identity:rom-branch-name-set"),
                        source.index("identity:adjudicate-unresolvable"))


if __name__ == "__main__":
    unittest.main()
