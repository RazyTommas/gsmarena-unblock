"""link_state is a MIRROR of the identity's state, and only for its own product.

THE DEFECT THESE TESTS PIN. `observation_product_links.link_state` had four
writers -- the bridge's insert, `_conclude`'s per-product UPDATE,
`identity_backfill`'s bulk refresh, and the server's manual-review path -- and
three of them decided it from `identity_id` alone. `source_identity_registry.id`
is uuid5(source_id, namespace, normalized_value) with no product component, and
its upsert conflicts on (source_id, namespace, normalized_value), so the row keeps
whichever product_id inserted it FIRST. One identity id is therefore reachable
from observations that resolve to different products, and the approval granted to
one of them licensed the others.

Measured on the live corpus: 9 links on the still-proposed product "Redmi 1" were
`link_state='approved'`, holding an approval granted to "Redmi 1 W" -- the name
Xiaomi's own catalogue gives model code HM2013023, which the tracker also
publishes as "Redmi 1 China / Global / Taiwan". Four more on "MI 3" held
"MI 3 / Mi 4"'s approval. 13 in total.

Every test below is written so that reverting the rule to match on identity_id
alone makes it FAIL. The fixture deliberately reproduces the collision rather than
asserting on a clean corpus, because a clean corpus cannot tell the two rules
apart.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory.database import Database  # noqa: E402
from mobile_observatory.enrichment import (automate_identity_review,  # noqa: E402
                                           promote_approved_product_observations,
                                           write_agent_review_bundle)
from mobile_observatory.identity_bridge import refresh_observation_link_states  # noqa: E402
from mobile_observatory.integrity import check_corpus, review_queue  # noqa: E402

NOW = "2026-01-01T00:00:00Z"


def _source(connection) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
        " VALUES('s','S',NULL,'primary',1,?)", (NOW,))
    connection.execute(
        "INSERT OR IGNORE INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,"
        "parser_version,accepted_count) VALUES('r','s',?,'succeeded','p','1',0)", (NOW,))
    connection.execute(
        "INSERT OR IGNORE INTO artifacts VALUES('a','s','r',?,'text/csv',NULL,?,'x',1)",
        ("a" * 64, NOW))


def _product(connection, pid: str, name: str, review_state: str, maker: str = "Xiaomi") -> None:
    connection.execute("INSERT INTO source_products VALUES(?,?,?,?,?,NULL,?,?)",
                       (pid, maker, name, name.casefold(), review_state, NOW, NOW))


def _identity(connection, iid: str, value: str, product_id: str, state: str) -> None:
    connection.execute("INSERT INTO source_identity_registry VALUES(?,'s','codename',?,?,?,?,'test','1','high',?,?)",
                       (iid, value, value.casefold(), product_id, state, NOW, NOW))


def _observation(connection, oid: str, build: str) -> None:
    connection.execute(
        "INSERT INTO observations VALUES(?,'s','r','a','firmware_release',?,?,?,?,'valid',NULL)",
        (oid, oid, NOW, '{"data":{"build":"%s","region_code":"GLOBAL","branch":"Stable"}}' % build,
         f"{abs(hash(oid)):064d}"[:64]))


def _link(connection, oid: str, product_id: str, identity_id: str, state: str) -> None:
    connection.execute("INSERT INTO observation_product_links VALUES(?,?,?,?,?)",
                       (oid, product_id, identity_id, state, NOW))


class LinkLicenceRuleTest(unittest.TestCase):
    """The Redmi 1 collision, reproduced small enough to reason about."""

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        self.con = self.db.connection
        with self.con:
            _source(self.con)
            # The approved product the catalogue confirms, and the identity it owns.
            _product(self.con, "p-w", "Redmi 1 W", "approved")
            _identity(self.con, "i-code", "HM2013023", "p-w", "approved")
            # The SEPARATE product the same source value also resolves to. Its own
            # review never happened; it holds no identity of its own.
            _product(self.con, "p-plain", "Redmi 1", "proposed")
            _observation(self.con, "o-w", "W-1")
            _link(self.con, "o-w", "p-w", "i-code", "approved")
            _observation(self.con, "o-plain", "PLAIN-1")
            # The state the live corpus was found in: approved, on a proposed
            # product, licensed by an identity that belongs to a different one.
            _link(self.con, "o-plain", "p-plain", "i-code", "approved")

    def _states(self) -> dict[str, str]:
        return {row[0]: row[1] for row in self.con.execute(
            "SELECT observation_id, link_state FROM observation_product_links")}

    def test_an_approval_granted_to_one_product_does_not_license_another(self) -> None:
        counts = refresh_observation_link_states(self.con)
        self.assertEqual(1, counts["links_demoted"])
        self.assertEqual({"o-w": "approved", "o-plain": "proposed"}, self._states(),
                         "the identity is approved for 'Redmi 1 W' only; matching on "
                         "identity_id alone approves 'Redmi 1' too")

    def test_a_link_whose_own_identity_is_approved_is_approved(self) -> None:
        """The rule must still open the gate it exists to open."""
        with self.con:
            self.con.execute("UPDATE observation_product_links SET link_state='proposed'")
        counts = refresh_observation_link_states(self.con)
        self.assertEqual(1, counts["links_approved"])
        self.assertEqual("approved", self._states()["o-w"])

    def test_demotion_is_scoped_when_a_product_is_named(self) -> None:
        """The manual-review path reconciles ONE product, not the corpus."""
        counts = refresh_observation_link_states(self.con, product_id="p-w")
        self.assertEqual({"links_approved": 0, "links_demoted": 0}, counts)
        self.assertEqual("approved", self._states()["o-plain"],
                         "reviewing 'Redmi 1 W' must not touch another product's links")

    def test_an_unlicensed_approval_never_promotes(self) -> None:
        """End to end: the observation must not reach the served read model."""
        with self.con:
            refresh_observation_link_states(self.con)
            # Approving the product is the OTHER gate. With the link demoted, the
            # observation still must not promote -- that is what makes the demotion
            # a real refusal and not bookkeeping.
            self.con.execute("UPDATE source_products SET review_state='approved' WHERE id='p-plain'")
        promote_approved_product_observations(self.con)
        promoted = [row[0] for row in self.con.execute(
            "SELECT observation_id FROM product_firmware_releases")]
        self.assertEqual(["o-w"], promoted)

    def test_integrity_reports_the_unlicensed_approval(self) -> None:
        found = {f.check: f for f in check_corpus(self.con, deep=False)}
        self.assertIn("observation_link_approved_without_an_approved_identity", found)
        self.assertEqual(1, found["observation_link_approved_without_an_approved_identity"].count)
        with self.con:
            refresh_observation_link_states(self.con)
        self.assertNotIn("observation_link_approved_without_an_approved_identity",
                         {f.check for f in check_corpus(self.con, deep=False)})

    def test_integrity_reports_the_collision_behind_it(self) -> None:
        """The outstanding identity decision, named rather than silently taken.

        Survives the repair: demoting the link refuses to infer that the two
        products are one device, it does not answer the question.
        """
        with self.con:
            refresh_observation_link_states(self.con)
        found = {f.check: f for f in check_corpus(self.con, deep=False)}
        self.assertIn("observation_link_identity_owned_by_another_product", found)
        finding = found["observation_link_identity_owned_by_another_product"]
        self.assertEqual(1, finding.count)
        self.assertIn("HM2013023", finding.detail)
        self.assertIn("Redmi 1", finding.detail)

    def test_a_corpus_with_no_collision_reports_neither_check(self) -> None:
        """Both checks must be absent when nothing is wrong, or they say nothing."""
        with self.con:
            self.con.execute("DELETE FROM observation_product_links WHERE observation_id='o-plain'")
        checks = {f.check for f in check_corpus(self.con, deep=False)}
        self.assertNotIn("observation_link_approved_without_an_approved_identity", checks)
        self.assertNotIn("observation_link_identity_owned_by_another_product", checks)


class TheBridgeCreatesLinksLicensedForTheirOwnProductTest(unittest.TestCase):
    """Where the unlicensed state was actually minted, driven through the bridge.

    Every other test here reconciles links that already exist. The bridge is the
    only thing that CREATES them, and it read the registry with `WHERE id=?` --
    so a corpus repaired by hand grew the same 13 links back on the next ingest.

    The fixture is the real collision, not an invented one: the Xiaomi tracker
    publishes model code HM2013023 under two device names whose region strip lands
    on two different products. identity_id is uuid5(source_id, namespace,
    normalized_value), so both observations name ONE registry row.
    """

    def test_a_second_product_reached_by_the_same_identity_is_not_approved(self) -> None:
        from mobile_observatory import identity_bridge as bridge

        db = Database.migrated()
        self.addCleanup(db.close)
        con = db.connection
        # Ids exactly as the bridge derives them, so the seeded registry row is the
        # one the rebuild will find rather than a lookalike.
        p_w = bridge._id("product", "Xiaomi", bridge._norm("Redmi 1 W", "Xiaomi"))
        p_plain = bridge._id("product", "Xiaomi", bridge._norm("Redmi 1", "Xiaomi"))
        identity = bridge._id("identity", "xiaomi.community.firmware_tracker", "codename",
                              bridge._norm("HM2013023"))
        self.assertNotEqual(p_w, p_plain, "the fixture needs two products, or it proves nothing")
        tracker = "xiaomi.community.firmware_tracker"   # the bridge dispatches on source id
        with con:
            con.execute("INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
                        " VALUES(?,'Tracker',NULL,'community',1,?)", (tracker, NOW))
            con.execute("INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,"
                        "parser_version,accepted_count) VALUES('r',?,?,'succeeded','p','1',0)",
                        (tracker, NOW))
            con.execute("INSERT INTO artifacts VALUES('a',?,'r',?,'text/csv',NULL,?,'x',1)",
                        (tracker, "a" * 64, NOW))
            _product(con, p_w, "Redmi 1 W", "approved")
            # Approved for "Redmi 1 W" only -- the state the vendor catalogue
            # actually produced on the live corpus.
            con.execute(
                "INSERT INTO source_identity_registry VALUES(?,?,'codename','HM2013023','hm2013023',?,"
                "'approved','xiaomi_vendor_codename_catalog','1','authoritative',?,?)",
                (identity, tracker, p_w, NOW, NOW))
            for oid, device in (("o-w", "Redmi 1 W Global"), ("o-plain", "Redmi 1 Global")):
                con.execute(
                    "INSERT INTO observations VALUES(?,?,'r','a','firmware_release',?,?,?,?,'valid',NULL)",
                    (oid, tracker, oid, NOW,
                     '{"data":{"source_device_name":"%s","model_code":"HM2013023",'
                     '"build":"%s","branch":"Stable"}}' % (device, oid),
                     f"{abs(hash(oid)):064d}"[:64]))

        bridge.rebuild_identity_registry(con)

        states = dict(con.execute(
            """SELECT sp.canonical_name, opl.link_state FROM observation_product_links opl
                 JOIN source_products sp ON sp.id = opl.product_id"""))
        self.assertEqual({"Redmi 1 W": "approved", "Redmi 1": "proposed"}, states,
                         "the bridge must ask whether the identity is approved FOR THIS "
                         "product; asking by identity_id alone re-mints the unlicensed state "
                         "on every ingest")
        self.assertEqual(
            identity,
            con.execute("SELECT identity_id FROM observation_product_links WHERE product_id=?",
                        (p_plain,)).fetchone()[0],
            "precondition: both links really do share one identity row")


class IdentityReviewDoesNotApproveByProductAloneTest(unittest.TestCase):
    """The loosest of the four copies, driven through the real entry point.

    `_conclude` ended with `UPDATE observation_product_links SET
    link_state='approved' WHERE product_id=?`. Every link on a concluded product
    was approved, including one whose identity belongs to a different product --
    so the corpus could mint the unlicensed state on any run, and repairing the
    data without this would have been undone by the next batch.
    """

    def test_a_concluded_product_approves_only_its_own_identities_links(self) -> None:
        import csv
        import tempfile

        db = Database.migrated()
        self.addCleanup(db.close)
        con = db.connection
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Xiaomi's own catalogue confirms the codename, so "Redmi 1 W"
            # auto-approves exactly as it does on the live corpus.
            (root / "devices.yml").write_text("HM2013023:\n- Redmi 1 W Global\n- HMWGlobal\n",
                                              encoding="utf-8")
            specs = root / "specs.csv"
            with specs.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["device_name", "slug", "chipset", "fetched_at"])
                writer.writeheader()
            with con:
                _source(con)
                _product(con, "p-w", "Redmi 1 W", "proposed")
                _identity(con, "i-code", "HM2013023", "p-w", "proposed")
                _product(con, "p-plain", "Redmi 1", "proposed")
                _observation(con, "o-w", "W-1")
                _link(con, "o-w", "p-w", "i-code", "proposed")
                _observation(con, "o-plain", "PLAIN-1")
                # Same identity id, other product: what the uuid5(source, namespace,
                # value) key plus ON CONFLICT actually produces.
                _link(con, "o-plain", "p-plain", "i-code", "proposed")

            result = automate_identity_review(con, devices_yml=root / "devices.yml", specs_csv=specs)
            self.assertEqual(1, result["auto_approved"])
            self.assertEqual("approved",
                             con.execute("SELECT review_state FROM source_products WHERE id='p-w'").fetchone()[0])
            states = dict(con.execute(
                "SELECT observation_id, link_state FROM observation_product_links"))
            self.assertEqual({"o-w": "approved", "o-plain": "proposed"}, states,
                             "approving 'Redmi 1 W' must not license a link naming 'Redmi 1'")


class QueueSeesBothGatesTest(unittest.TestCase):
    """8,332 observations were held by the second gate and reported as zero."""

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        self.con = self.db.connection
        with self.con:
            _source(self.con)
            # Gate one: the product itself is unreviewed.
            _product(self.con, "p-unreviewed", "Redmi 99", "proposed")
            _identity(self.con, "i-unreviewed", "unreviewed", "p-unreviewed", "proposed")
            _observation(self.con, "o-unreviewed", "U-1")
            _link(self.con, "o-unreviewed", "p-unreviewed", "i-unreviewed", "proposed")
            # Gate two: the product IS approved, and a LATER source contributed an
            # identity that was never evaluated because the conclusion was frozen.
            _product(self.con, "p-approved", "Xiaomi 11T Pro", "approved")
            _identity(self.con, "i-regional", "vili_global", "p-approved", "approved")
            _identity(self.con, "i-bare", "vili", "p-approved", "proposed")
            _observation(self.con, "o-serving", "S-1")
            _link(self.con, "o-serving", "p-approved", "i-regional", "approved")
            _observation(self.con, "o-stalled", "T-1")
            _link(self.con, "o-stalled", "p-approved", "i-bare", "proposed")

    def test_both_gates_are_counted_and_stay_distinguishable(self) -> None:
        row = next(r for r in review_queue(self.con) if r["vendor"] == "Xiaomi")
        self.assertEqual(1, row["observations_awaiting_review"],
                         "the product-gate number keeps the meaning the UI's tooltip states")
        self.assertEqual(1, row["observations_held_by_link_review"],
                         "an observation on an APPROVED product with a proposed link is held, "
                         "and was previously invisible in every column")
        self.assertEqual(2, row["observations_not_serving"])
        served = self.con.execute(
            """SELECT count(*) FROM observation_product_links opl
                 JOIN source_products sp ON sp.id=opl.product_id
                WHERE opl.link_state='approved' AND sp.review_state='approved'""").fetchone()[0]
        self.assertEqual(3 - row["observations_not_serving"], served,
                         "not-serving and serving must partition the links, or one of the "
                         "two numbers is measuring something else")

    def test_the_stall_clears_when_the_identity_is_approved(self) -> None:
        """The metric must go to zero for the right reason, not by redefinition."""
        with self.con:
            self.con.execute(
                "UPDATE source_identity_registry SET resolution_state='approved' WHERE id='i-bare'")
            refresh_observation_link_states(self.con)
        row = next(r for r in review_queue(self.con) if r["vendor"] == "Xiaomi")
        self.assertEqual(0, row["observations_held_by_link_review"])
        self.assertEqual(1, row["observations_not_serving"], "the unreviewed product is still held")


class AgentBundleAsksOnlyOpenQuestionsTest(unittest.TestCase):
    """628 candidates where 626 were actually open."""

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        self.con = self.db.connection
        with self.con:
            _source(self.con)
            for pid, name, review_state in (("p-open", "Redmi Note 5", "proposed"),
                                            ("p-settled", "Redmi Note 5 Pro", "approved")):
                _product(self.con, pid, name, review_state)
                _identity(self.con, "i-" + pid, name, pid,
                          "approved" if review_state == "approved" else "proposed")
                self.con.execute(
                    "INSERT INTO identity_conclusions VALUES(?,'ambiguous','medium','ranked_candidates',"
                    "'1','several candidates','[]','[]',?)", (pid, NOW))

    def test_an_already_approved_product_is_not_an_open_question(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bundle = write_agent_review_bundle(self.con, Path(tmp) / "b")
            self.assertEqual(1, bundle["candidate_count"],
                             "a product whose review_state is already 'approved' is settled; "
                             "handing it to a reviewer invites them to contradict the corpus")
            import json
            candidates = json.loads((Path(tmp) / "b" / "identity-candidates.json").read_text())
            self.assertEqual(["Redmi Note 5"], [c["canonical_name"] for c in candidates])


if __name__ == "__main__":
    unittest.main()
