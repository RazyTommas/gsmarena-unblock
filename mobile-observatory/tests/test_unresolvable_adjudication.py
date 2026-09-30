"""A terminal state is a published conclusion, so the tests that matter are the
ones that prove it is not a cover-up.

`unresolvable_on_captured_evidence` takes 626 products out of a review queue. Three
things have to be true for that to be honest rather than convenient, and each is
asserted here by a case built to defeat exactly it:

  * it must not be an approval in disguise -- nothing may be asserted, promoted,
    approved or deleted, so the cases check `source_identity_registry`,
    `observation_product_links`, `identity_conclusions` and
    `product_firmware_releases` are byte-for-byte what they were;
  * it must not close a product nobody has looked at. A product with NO recorded
    conclusion is the residue an agent has not reached, and adjudicating it would be
    the same lie one state further on;
  * it must be REVERSIBLE, three ways: a new captured identity, a bump of the
    adjudication's own rule version, and a human. Each is exercised, and the last
    two would both pass if reopening were a no-op, so the first also asserts the
    frozen conclusion is dropped and the ordinary rule re-decides.

And the 465/161 split is not decoration: `reason` and the review_queue columns are
asserted to keep "no independent identifier exists" apart from "several candidates
exist and none discriminates", because only the first says the sources are silent.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import adjudication  # noqa: E402
from mobile_observatory.adjudication import (  # noqa: E402
    ADJUDICATION_RULE, UNRESOLVABLE,
    adjudicate_unresolvable_products, reopen_stale_adjudications)
from mobile_observatory.database import Database  # noqa: E402
from mobile_observatory.enrichment import (  # noqa: E402
    automate_identity_review, promote_approved_product_observations,
    write_agent_review_bundle)
from mobile_observatory.integrity import check_corpus, review_queue  # noqa: E402

NOW = "2026-01-01T00:00:00Z"


class AdjudicationTest(unittest.TestCase):

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

    def _product(self, pid: str, name: str, *, maker: str = "Xiaomi",
                 review_state: str = "proposed", conclusion: str | None = "insufficient_evidence",
                 method: str = "no_independent_identifier", candidates: str = "[]") -> None:
        with self.con:
            self.con.execute("INSERT INTO source_products VALUES(?,?,?,?,?,NULL,?,?)",
                             (pid, maker, name, name.casefold(), review_state, NOW, NOW))
            if conclusion:
                self.con.execute(
                    "INSERT INTO identity_conclusions VALUES(?,?,'low',?,'2','recorded earlier',"
                    "?,'[]',?)", (pid, conclusion, method, candidates, NOW))

    def _identity(self, iid: str, value: str, pid: str, *, with_observation: bool = True) -> None:
        with self.con:
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES(?,'archive','codename',?,?,?,"
                "'proposed','deterministic_source_catalog','1','medium',?,?)",
                (iid, value, value.casefold(), pid, NOW, NOW))
            if with_observation:
                self.con.execute(
                    "INSERT INTO observations VALUES(?,'archive','r','a','firmware_release',?,?,?,?,"
                    "'valid',NULL)",
                    ("o-" + iid, "o-" + iid, NOW,
                     '{"data":{"build":"V1.0","region_code":"GLOBAL","branch":"Stable",'
                     '"release_date":"2025-01-01"}}',
                     ("%064d" % abs(hash(iid)))[:64]))
                self.con.execute("INSERT INTO observation_product_links VALUES(?,?,?,'proposed',?)",
                                 ("o-" + iid, pid, iid, NOW))

    def _rationales(self, pid: str) -> list[dict]:
        return [dict(row) for row in self.con.execute(
            """SELECT r.* FROM identity_resolution_rationales r
                 JOIN source_identity_registry sir ON sir.id = r.identity_id
                WHERE sir.product_id=? ORDER BY r.identity_id""", (pid,))]

    def _state(self, pid: str) -> str:
        return self.con.execute(
            "SELECT review_state FROM source_products WHERE id=?", (pid,)).fetchone()[0]

    # -- the state itself ----------------------------------------------------

    def test_the_check_constraint_admits_the_new_state_and_still_refuses_nonsense(self) -> None:
        self._product("p1", "Redmi 1")
        with self.con:
            self.con.execute("UPDATE source_products SET review_state=? WHERE id='p1'",
                             (UNRESOLVABLE,))
        self.assertEqual(UNRESOLVABLE, self._state("p1"))
        # The CHECK is the thing being tested, so a value it must reject is the proof
        # the rebuild in migration 0030 did not simply drop the constraint.
        with self.assertRaises(sqlite3.IntegrityError):
            with self.con:
                self.con.execute("UPDATE source_products SET review_state='unresolvable' "
                                 "WHERE id='p1'")

    def test_the_new_state_is_neither_approved_nor_rejected(self) -> None:
        self.assertNotIn(UNRESOLVABLE, ("approved", "rejected", "proposed"))

    # -- what it closes, and what it refuses to close ------------------------

    def test_closes_both_sub_populations_and_keeps_them_distinguishable(self) -> None:
        self._product("p-silent", "Redmi 1", conclusion="insufficient_evidence",
                      method="no_independent_identifier")
        self._identity("i-silent", "cactus", "p-silent")
        self._product("p-several", "Zero 5", maker="Infinix", conclusion="ambiguous",
                      method="google_play_model_code_multiple_names",
                      candidates='["Zero 5","Zero 5 Pro"]')
        self._identity("i-several", "X603", "p-several")

        totals = adjudicate_unresolvable_products(self.con)
        self.assertEqual(2, totals["adjudicated"])
        self.assertEqual(1, totals["adjudicated_no_independent_identifier"])
        self.assertEqual(1, totals["adjudicated_several_candidates_none_discriminating"])
        self.assertEqual(UNRESOLVABLE, self._state("p-silent"))
        self.assertEqual(UNRESOLVABLE, self._state("p-several"))

        # ONE state, TWO reasons. Collapsing them would make "the sources are
        # silent" and "the sources disagree" read as the same claim.
        self.assertEqual("no_independent_identifier", self._rationales("p-silent")[0]["reason"])
        self.assertEqual("several_candidates_none_discriminating",
                         self._rationales("p-several")[0]["reason"])
        several = json.loads(self._rationales("p-several")[0]["evidence_json"])
        self.assertEqual(["Zero 5", "Zero 5 Pro"], several["conclusion"]["candidates"])
        self.assertEqual([], json.loads(
            self._rationales("p-silent")[0]["evidence_json"])["conclusion"]["candidates"])

    def test_refuses_to_close_a_product_nobody_has_looked_at(self) -> None:
        """No conclusion means the rules have not run. Closing it would be the same
        dishonesty one state further on -- 'adjudicated' without an adjudication."""
        self._product("p-unseen", "Never Concluded", conclusion=None)
        self._identity("i-unseen", "unseen", "p-unseen")
        totals = adjudicate_unresolvable_products(self.con)
        self.assertEqual(1, totals["skipped_not_concluded"])
        self.assertNotIn("adjudicated", totals)
        self.assertEqual("proposed", self._state("p-unseen"))

    def test_refuses_to_close_a_conclusion_that_is_not_terminal(self) -> None:
        self._product("p-ok", "Auto", review_state="proposed", conclusion="auto_approved",
                      method="xiaomi_vendor_codename_catalog")
        totals = adjudicate_unresolvable_products(self.con)
        self.assertEqual(1, totals["skipped_conclusion_not_terminal"])
        self.assertEqual("proposed", self._state("p-ok"))

    # -- it must not be an approval in disguise ------------------------------

    def test_asserts_nothing_promotes_nothing_and_deletes_nothing(self) -> None:
        self._product("p1", "Redmi 1")
        self._identity("i1", "cactus", "p1")
        before = self._fingerprint()
        adjudicate_unresolvable_products(self.con)
        self.assertEqual(before, self._fingerprint())
        # And the gate promotion depends on is still shut, so no firmware serves.
        promote_approved_product_observations(self.con)
        self.assertEqual(0, self.con.execute(
            "SELECT count(*) FROM product_firmware_releases").fetchone()[0])

    def _fingerprint(self) -> tuple:
        """Everything the adjudication must not touch."""
        return (
            tuple(tuple(r) for r in self.con.execute(
                "SELECT id,resolution_state,resolution_method,confidence"
                "  FROM source_identity_registry ORDER BY id")),
            tuple(tuple(r) for r in self.con.execute(
                "SELECT observation_id,link_state FROM observation_product_links"
                " ORDER BY observation_id")),
            tuple(tuple(r) for r in self.con.execute(
                "SELECT product_id,conclusion,method,rule_version FROM identity_conclusions"
                " ORDER BY product_id")),
            self.con.execute("SELECT count(*) FROM observations").fetchone()[0],
            self.con.execute("SELECT count(*) FROM product_firmware_releases").fetchone()[0],
        )

    # -- provenance ----------------------------------------------------------

    def test_records_who_when_and_on_what_basis_per_identity(self) -> None:
        self._product("p1", "Redmi 1")
        self._identity("i1", "cactus", "p1")
        self._identity("i2", "cactus_global", "p1")
        adjudicate_unresolvable_products(self.con)
        rows = self._rationales("p1")
        self.assertEqual(2, len(rows), "one row per identity, which is the basis")
        for row in rows:
            self.assertEqual(ADJUDICATION_RULE, row["rule"])
            # 'adjudicated_unresolvable' is used by no other writer, which is what
            # makes an agent adjudication distinguishable from a human decision
            # ('manual_product_review') and from a source having proved the
            # identity (outcome 'approved').
            self.assertEqual("adjudicated_unresolvable", row["outcome"])
            self.assertTrue(row["decided_at"])
            evidence = json.loads(row["evidence_json"])
            self.assertEqual("agent:unresolvable_adjudication", evidence["decided_by"])
            self.assertEqual({"cactus", "cactus_global"},
                             {i["source_value"] for i in evidence["identities"]})
            self.assertIn("basis_fingerprint", evidence)

    def test_a_product_with_no_identity_is_recorded_product_scoped_in_the_same_store(self) -> None:
        """Two of the 626 carry no registry identity at all, so there is no identity to
        key a rationale on. They get a row with identity_id NULL in the SAME table --
        not a second provenance store -- and are counted separately so a reader can
        see how many decisions could not be keyed on an identity."""
        self._product("p-bare", "Redmi 1")
        totals = adjudicate_unresolvable_products(self.con)
        self.assertEqual(1, totals["adjudicated_without_identity"])
        self.assertEqual(1, totals["rationales_recorded"])
        row = self.con.execute(
            "SELECT * FROM identity_resolution_rationales WHERE product_id='p-bare'").fetchone()
        self.assertIsNone(row["identity_id"])
        self.assertEqual("adjudicated_unresolvable", row["outcome"])
        self.assertEqual("agent:unresolvable_adjudication",
                         json.loads(row["evidence_json"])["decided_by"])
        # And the product-scoped row is rewritten, never appended to, so a corpus
        # reopened and re-adjudicated a hundred times still holds one current row.
        for _ in range(3):
            reopen_stale_adjudications(self.con)
            adjudicate_unresolvable_products(self.con)
        self.assertEqual(1, self.con.execute(
            "SELECT count(*) FROM identity_resolution_rationales "
            "WHERE product_id='p-bare'").fetchone()[0])

    def test_integrity_catches_a_terminal_state_with_no_recorded_basis(self) -> None:
        """The planted defect for the provenance guard: the state without its reason.

        Both shapes of basis are planted, the identity-keyed one and the
        product-scoped one, because a guard that only ever sees the common shape
        would pass a corpus where the rare shape is missing -- and the rare shape is
        exactly the one nobody thinks about."""
        self._product("p1", "Redmi 1")
        self._identity("i1", "cactus", "p1")
        self._product("p-bare", "ACE2N", maker="itel")
        adjudicate_unresolvable_products(self.con)
        self.assertEqual([], self._failed("adjudicated_product_without_recorded_basis"))

        # Defect 1: the identity-keyed basis is gone.
        with self.con:
            self.con.execute(
                "DELETE FROM identity_resolution_rationales WHERE identity_id IS NOT NULL")
        found = self._failed("adjudicated_product_without_recorded_basis")
        self.assertEqual([1], [f.count for f in found])
        self.assertEqual("error", found[0].severity)

        # Defect 2: and the product-scoped one.
        with self.con:
            self.con.execute(
                "DELETE FROM identity_resolution_rationales WHERE identity_id IS NULL")
        self.assertEqual([2], [f.count for f in self._failed(
            "adjudicated_product_without_recorded_basis")])

    def _failed(self, check: str) -> list:
        return [f for f in check_corpus(self.con, deep=False) if f.check == check]

    # -- reversibility, which is the whole reason this is honest -------------

    def test_a_new_captured_identity_reopens_it_and_the_rules_decide_again(self) -> None:
        self._product("p1", "Redmi 1")
        self._identity("i1", "cactus", "p1")
        adjudicate_unresolvable_products(self.con)
        self.assertEqual({"examined": 1, "unchanged": 1}, reopen_stale_adjudications(self.con))

        # A later capture contributes a second source value for the same product.
        self._identity("i2", "cactus_global", "p1")
        self.assertEqual(1, reopen_stale_adjudications(self.con)["reopened"])
        self.assertEqual("proposed", self._state("p1"))
        # The frozen conclusion goes too, or `_conclude` would return the remembered
        # answer and the reopen would be a no-op dressed as a reversal.
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM identity_conclusions WHERE product_id='p1'").fetchone())
        # And the withdrawal is recorded, not silent.
        reopened = self._rationales("p1")
        self.assertEqual({"reopened"}, {r["outcome"] for r in reopened})
        self.assertEqual({"captured_basis_changed"}, {r["reason"] for r in reopened})

        # The ordinary rule now runs on it again from scratch.
        catalog = Path(self.tmp.name) / "devices.yml"
        catalog.write_text("cactus:\n- Redmi 1\n", encoding="utf-8")
        specs = Path(self.tmp.name) / "specs.csv"
        specs.write_text("slug,device_name\n", encoding="utf-8")
        automate_identity_review(self.con, devices_yml=catalog, specs_csv=specs)
        self.assertEqual("approved", self._state("p1"),
                         "the reopened product was decided again, not left in limbo")

    def test_a_new_adjudication_rule_version_reopens_every_adjudication(self) -> None:
        self._product("p1", "Redmi 1")
        self._identity("i1", "cactus", "p1")
        adjudicate_unresolvable_products(self.con)
        original = adjudication.ADJUDICATION_RULE_VERSION
        try:
            adjudication.ADJUDICATION_RULE_VERSION = original + "-next"
            self.assertEqual(1, reopen_stale_adjudications(self.con)["reopened"])
        finally:
            adjudication.ADJUDICATION_RULE_VERSION = original
        self.assertEqual("proposed", self._state("p1"))

    def test_an_unchanged_corpus_reopens_nothing(self) -> None:
        """The counterweight to the two above: reopening must be driven by a measured
        change in the corpus and never by the clock, or the corpus would depend on
        when it last ran -- which is the reason conclusions are frozen at all."""
        self._product("p1", "Redmi 1")
        self._identity("i1", "cactus", "p1")
        adjudicate_unresolvable_products(self.con)
        for _ in range(3):
            self.assertEqual({"examined": 1, "unchanged": 1}, reopen_stale_adjudications(self.con))
            self.assertEqual({}, adjudicate_unresolvable_products(self.con))
        self.assertEqual(UNRESOLVABLE, self._state("p1"))

    def test_a_torn_provenance_record_reopens_rather_than_resting_on_it(self) -> None:
        self._product("p1", "Redmi 1")
        self._identity("i1", "cactus", "p1")
        adjudicate_unresolvable_products(self.con)
        with self.con:
            self.con.execute("DELETE FROM identity_resolution_rationales")
        self.assertEqual(1, reopen_stale_adjudications(self.con)["reopened"])

    # -- the metric ----------------------------------------------------------

    def test_the_queue_stops_counting_them_as_pending_without_hiding_them(self) -> None:
        self._product("p-silent", "Redmi 1", conclusion="insufficient_evidence")
        self._identity("i-silent", "cactus", "p-silent")
        self._product("p-several", "Redmi 2", conclusion="ambiguous",
                      method="ranked_candidates", candidates='["a","b"]')
        self._identity("i-several", "kenzo", "p-several")

        before = {row["vendor"]: row for row in review_queue(self.con)}["Xiaomi"]
        self.assertEqual(2, before["observations_awaiting_review"])
        self.assertEqual(2, before["observations_pending_review"])
        self.assertEqual(0, before["observations_adjudicated_unresolvable"])

        adjudicate_unresolvable_products(self.con)
        after = {row["vendor"]: row for row in review_queue(self.con)}["Xiaomi"]
        # Not pending...
        self.assertEqual(0, after["observations_awaiting_review"])
        self.assertEqual(0, after["observations_pending_review"])
        # ...and not hidden: the same two observations, under their own name.
        self.assertEqual(2, after["observations_adjudicated_unresolvable"])
        self.assertEqual(2, after["observations_not_serving"])
        # The three answers the module is built on stay three answers.
        self.assertEqual(2, after["unresolvable"])
        self.assertEqual(1, after["unresolvable_no_identifier"])
        self.assertEqual(1, after["unresolvable_several_candidates"])
        self.assertEqual(0, after["unresolvable_without_identity"])

    def test_awaiting_review_no_longer_counts_a_rejected_product(self) -> None:
        """It used to be `review_state <> 'approved'`, so a product a reviewer had
        already looked at and REJECTED still read as awaiting review."""
        self._product("p-no", "Rejected", review_state="rejected")
        self._identity("i-no", "nope", "p-no")
        row = {r["vendor"]: r for r in review_queue(self.con)}["Xiaomi"]
        self.assertEqual(0, row["observations_awaiting_review"])
        self.assertEqual(1, row["observations_not_serving"])

    def test_a_human_reopen_survives_the_next_batch(self) -> None:
        """Through the REAL review endpoint, because the bug this covers lived in the
        seam between it and the batch: Reopen writes 'proposed', and a rule that only
        asks for 'proposed' plus a terminal conclusion would re-close the product on
        the next nightly run -- the reviewer's decision reverted within a day, with
        nothing in the UI to say so. Found by reasoning about the batch order, so the
        test drives the endpoint rather than the helper."""
        import tempfile as _tempfile
        from mobile_observatory.server import ObservatoryService

        self._product("p1", "Redmi 1")
        self._identity("i1", "cactus", "p1")
        adjudicate_unresolvable_products(self.con)
        self.assertEqual(UNRESOLVABLE, self._state("p1"))

        local_dir = _tempfile.TemporaryDirectory()
        self.addCleanup(local_dir.cleanup)
        service = ObservatoryService(self.db, Path(local_dir.name) / "local.sqlite",
                                     demonstration=False)
        self.addCleanup(service.local.close)
        service.review_source_product("p1", "proposed")
        self.assertEqual("proposed", self._state("p1"))

        decisions = [dict(r) for r in service.local.execute("SELECT * FROM identity_decisions")]
        self.assertEqual(1, len(decisions))
        totals = adjudicate_unresolvable_products(self.con, decisions=decisions)
        self.assertEqual(1, totals["skipped_human_decision"])
        self.assertEqual("proposed", self._state("p1"),
                         "an agent rule must not overrule a person's explicit reopen")

    def test_the_agent_handoff_stops_offering_work_nobody_can_do(self) -> None:
        self._product("p1", "Redmi 1")
        self._identity("i1", "cactus", "p1")
        out = Path(self.tmp.name) / "bundle"
        write_agent_review_bundle(self.con, out)
        self.assertEqual(1, len(json.loads((out / "identity-candidates.json").read_text())))
        adjudicate_unresolvable_products(self.con)
        write_agent_review_bundle(self.con, out)
        self.assertEqual([], json.loads((out / "identity-candidates.json").read_text()))


if __name__ == "__main__":
    unittest.main()
