"""The backfill must REFUSE, and the live corpus cannot prove that it does.

`approve_catalog_confirmed_identities` was written on 2026-09-22 to close one
specific stall -- an identity contributed by a later source to a product whose
conclusion was already frozen -- and then had no caller and no test anywhere in
the repo until it was wired into run_batch.

WHY THESE TESTS USE A FIXTURE CATALOG AND NOT THE LIVE ONE. Measured 2026-09-30:
against the captured `crawler/relay/results/xiaomi-tracker/devices.yml` this
function approves **0** of the 58 identities it targets -- 49 of their codenames
are absent from the catalog and 9 mismatch on name -- because the catalog is keyed
by regional codename variants (`vili_global`, `vili_eea_global`) while the mifirm
archive publishes the bare stem (`vili`). So a test asserting "it approves things"
against the live corpus is vacuously green: it would pass with the body deleted,
and it would pass with the name comparison deleted too.

Every test below therefore drives it from a small catalog written in the test, and
the ones that matter assert what it REFUSES: a codename the catalog does not
carry, a catalog entry naming a DIFFERENT phone, and a product that was never
approved itself. The wrong-catalog-entry case is the one that decides whether this
function is a rule or a rubber stamp -- `jasmine` really is a Xiaomi codename, so
nothing about the identity looks suspicious; only the name comparison catches that
the catalog is calling it an Mi A3 while the product is an Mi A2.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory.database import Database  # noqa: E402
from mobile_observatory.enrichment import promote_approved_product_observations  # noqa: E402
from mobile_observatory.identity_backfill import approve_catalog_confirmed_identities  # noqa: E402

NOW = "2026-01-01T00:00:00Z"


class CatalogBackfillTest(unittest.TestCase):
    """One product per case, each already concluded and approved."""

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
                "parser_version,accepted_count) VALUES('r','archive',?,'succeeded','p','1',0)", (NOW,))
            self.con.execute("INSERT INTO artifacts VALUES('a','archive','r',?,'text/csv',NULL,?,'x',1)",
                             ("a" * 64, NOW))

    def _catalog(self, body: str) -> Path:
        path = Path(self.tmp.name) / "devices.yml"
        path.write_text(body, encoding="utf-8")
        return path

    def _product(self, pid: str, name: str, *, maker: str = "Xiaomi",
                 review_state: str = "approved", conclusion: str | None = "auto_approved") -> None:
        with self.con:
            self.con.execute("INSERT INTO source_products VALUES(?,?,?,?,?,NULL,?,?)",
                             (pid, maker, name, name.casefold(), review_state, NOW, NOW))
            if conclusion:
                self.con.execute(
                    "INSERT INTO identity_conclusions VALUES(?,?,'authoritative',"
                    "'xiaomi_vendor_codename_catalog','1','settled earlier','[]','[]',?)",
                    (pid, conclusion, NOW))

    def _stranded_identity(self, iid: str, codename: str, pid: str) -> None:
        """An identity a LATER source contributed, with one observation behind it."""
        with self.con:
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES(?,'archive','codename',?,?,?,"
                "'proposed','deterministic_source_catalog','1','medium',?,?)",
                (iid, codename, codename.casefold(), pid, NOW, NOW))
            self.con.execute(
                "INSERT INTO observations VALUES(?,'archive','r','a','firmware_release',?,?,?,?,'valid',NULL)",
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
                WHERE sir.id = ?""", (iid,)).fetchone())

    # -- the one case it exists for ------------------------------------------

    def test_approves_a_codename_the_vendor_catalog_confirms_and_it_starts_serving(self) -> None:
        self._product("p-a2", "Mi A2")
        self._stranded_identity("i-jasmine", "jasmine", "p-a2")
        totals = approve_catalog_confirmed_identities(
            self.con, devices_yml=self._catalog("jasmine:\n- Mi A2 Global\n- JASMINEGlobal\n"))
        self.assertEqual(1, totals["approved"])
        self.assertEqual(("approved", "approved"), self._state("i-jasmine"))
        # The point of the approval is the evidence reaching the surface, so assert
        # promotion, not the state columns. 8,332 observations were in exactly this
        # position with 0 rows in product_firmware_releases.
        promote_approved_product_observations(self.con)
        self.assertEqual(1, self.con.execute(
            "SELECT count(*) FROM product_firmware_releases").fetchone()[0])

    # -- the refusals, which are what make it a rule -------------------------

    def test_refuses_a_catalog_entry_naming_a_DIFFERENT_phone(self) -> None:
        """`jasmine` is a real Xiaomi codename. The catalog calls it an Mi A3.

        Nothing about the identity looks wrong -- same vendor, same source, a
        codename that exists -- so only the name comparison can catch it. If this
        test passes with the comparison removed, the function is a rubber stamp
        that approves any codename the catalog happens to mention.
        """
        self._product("p-a2", "Mi A2")
        self._stranded_identity("i-jasmine", "jasmine", "p-a2")
        totals = approve_catalog_confirmed_identities(
            self.con, devices_yml=self._catalog("jasmine:\n- Mi A3 China\n- JASMINECN\n"))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["skipped_catalog_name_mismatch"])
        self.assertEqual(("proposed", "proposed"), self._state("i-jasmine"))
        promote_approved_product_observations(self.con)
        self.assertEqual(0, self.con.execute(
            "SELECT count(*) FROM product_firmware_releases").fetchone()[0])

    def test_refuses_a_codename_the_catalog_does_not_carry(self) -> None:
        """The live case: the catalog is keyed by `vili_global`, mifirm says `vili`.

        49 of the 58 stranded identities are exactly this, and inferring the stem
        relationship is an identity decision this function does not make.
        """
        self._product("p-11tpro", "Xiaomi 11T Pro")
        self._stranded_identity("i-vili", "vili", "p-11tpro")
        totals = approve_catalog_confirmed_identities(
            self.con, devices_yml=self._catalog(
                "vili_global:\n- Xiaomi 11T Pro Global\n- VILIGlobal\n"))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["skipped_codename_not_in_catalog"])
        self.assertEqual(("proposed", "proposed"), self._state("i-vili"))

    def test_refuses_an_identity_on_a_product_that_was_never_approved(self) -> None:
        """An identity can never be more certain than the product it points at."""
        self._product("p-pending", "Redmi 1", review_state="proposed",
                      conclusion="insufficient_evidence")
        self._stranded_identity("i-pending", "armani", "p-pending")
        totals = approve_catalog_confirmed_identities(
            self.con, devices_yml=self._catalog("armani:\n- Redmi 1 Global\n"))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["skipped_product_not_approved"])
        self.assertEqual(("proposed", "proposed"), self._state("i-pending"))

    def test_refuses_a_vendor_the_catalog_has_no_authority_over(self) -> None:
        """devices.yml is Xiaomi's publication; it says nothing about TECNO."""
        self._product("p-tecno", "CAMON 20", maker="TECNO")
        self._stranded_identity("i-tecno", "camon 20", "p-tecno")
        totals = approve_catalog_confirmed_identities(
            self.con, devices_yml=self._catalog("camon 20:\n- CAMON 20\n"))
        self.assertEqual(0, totals.get("approved", 0))
        self.assertEqual(1, totals["skipped_not_xiaomi"])
        self.assertEqual(("proposed", "proposed"), self._state("i-tecno"))

    def test_an_empty_catalog_approves_nothing_rather_than_everything(self) -> None:
        """An unreadable or absent artifact must not read as "no objection"."""
        self._product("p-a2", "Mi A2")
        self._stranded_identity("i-jasmine", "jasmine", "p-a2")
        totals = approve_catalog_confirmed_identities(
            self.con, devices_yml=self._catalog("# nothing captured\n"))
        self.assertEqual({"catalog_empty": 1}, totals)
        self.assertEqual(("proposed", "proposed"), self._state("i-jasmine"))

    def test_does_not_license_a_link_on_another_product(self) -> None:
        """The approval is for one product; the link mirror must not spread it.

        The same source value can resolve to two products (model code HM2013023 is
        published both as "Redmi 1 W" and as "Redmi 1"), and this function's own
        link refresh used to match on identity_id alone.
        """
        self._product("p-a2", "Mi A2")
        self._product("p-other", "Mi A2 Lite")
        self._stranded_identity("i-jasmine", "jasmine", "p-a2")
        with self.con:
            self.con.execute(
                "INSERT INTO observations VALUES('o-other','archive','r','a','firmware_release',"
                "'o-other',?,'{\"data\":{\"build\":\"OTHER\"}}',?,'valid',NULL)", (NOW, "b" * 64))
            self.con.execute("INSERT INTO observation_product_links VALUES"
                             "('o-other','p-other','i-jasmine','proposed',?)", (NOW,))
        approve_catalog_confirmed_identities(
            self.con, devices_yml=self._catalog("jasmine:\n- Mi A2 Global\n"))
        states = dict(self.con.execute(
            "SELECT observation_id, link_state FROM observation_product_links"))
        self.assertEqual({"o-i-jasmine": "approved", "o-other": "proposed"}, states)


class TheRuleIsInThePipelineTest(unittest.TestCase):
    """A rule with no caller is not a rule, it is a file.

    This function was correct and unreachable for eight days. Nothing in the suite
    noticed, because every test that could have noticed would have had to run the
    pipeline, and nothing asserted the pipeline's SHAPE. Reading the compiled code
    object's global references is cheap, exact, and fails the moment a call is
    dropped -- which is the failure that actually happened.
    """

    def test_run_batch_calls_the_backfill_and_the_link_reconciliation(self) -> None:
        from mobile_observatory.batch import run_batch

        names = set(run_batch.__code__.co_names)
        self.assertIn("approve_catalog_confirmed_identities", names,
                      "the backfill closes the stall that leaves captured evidence unable to "
                      "reach the surface; it has to run in the batch, not exist beside it")
        self.assertIn("refresh_observation_link_states", names,
                      "dedupe repoints link.product_id and registry.product_id separately, so "
                      "link_state has to be reconciled before promotion reads it")

    def test_identity_review_derives_link_state_rather_than_writing_it(self) -> None:
        """`_conclude` must not carry its own copy of the mirror rule.

        It used to end with `UPDATE observation_product_links SET
        link_state='approved' WHERE product_id=?` -- the loosest of the four
        copies, approving every link on the product whether or not the link's own
        identity belonged to it. Asserted on the compiled code object rather than
        the source text, so a comment mentioning the old statement cannot pass or
        fail it.
        """
        from mobile_observatory import enrichment

        self.assertIn("refresh_observation_link_states",
                      set(enrichment.automate_identity_review.__code__.co_names),
                      "the reconciliation runs once, over the whole corpus, after every "
                      "conclusion is in")


if __name__ == "__main__":
    unittest.main()
