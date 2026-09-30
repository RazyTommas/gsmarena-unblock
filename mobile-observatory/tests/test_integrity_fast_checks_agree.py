"""Two integrity checks were rewritten for speed. Prove they answer the same.

`device_targets_share_a_region_code` stopped reading v_latest_firmware (360.3ms ->
3.6ms) and `firmware_observed_but_not_served`'s correlated tie test became one
aggregate (211.3ms -> 22.7ms). Together they were 571ms of the 618ms that
/api/v1/admin/health spent on invariants.

Both rewrites are equal to the originals by an argument about the SQL, not by
inspection of an answer, so the argument is what has to be tested:

* the view emits exactly ONE row per (model, target, channel) partition, so a
  group-and-count keyed on that partition cannot see which row won it;
* a row is tied exactly when its (model, region, channel, currency_rank) group
  holds more than one source, which is a set property, not a per-row one.

The live corpus reports 0 for the first of these. Two queries agreeing on an
empty answer prove nothing -- that is the whole reason this file plants data
where the answers are NON-ZERO and asserts they are, before comparing.

The reference SQL below is the pre-rewrite text. The statement it is compared
against is CAPTURED from the connection while check_corpus runs, so this tests
what actually ships rather than a second copy of it that can drift.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.integrity import check_corpus  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402

LIVE_CORPUS = ROOT / ".observatory-data" / "corpus.sqlite"
FIXTURE = ROOT / "fixtures" / "supported_catalog.sample.json"

REFERENCE_SHARED_CODE = """
    SELECT count(*) FROM (
      SELECT lf.hardware_model_id, coalesce(ft.target_code,'') AS code, lf.channel
        FROM v_latest_firmware lf
        LEFT JOIN firmware_targets ft ON ft.id = lf.firmware_target_id
       GROUP BY 1, 2, 3
      HAVING count(DISTINCT ifnull(lf.firmware_target_id,'')) > 1)"""

REFERENCE_WITH_FIRMWARE = """
    SELECT count(*) FROM (
      SELECT hardware_model_id FROM firmware_releases
       UNION
      SELECT pfr.hardware_model_id FROM product_firmware_releases pfr
       WHERE pfr.hardware_model_id IS NOT NULL
         AND NOT EXISTS (
           SELECT 1 FROM product_firmware_releases tie
             JOIN sources ts ON ts.id = tie.source_id
             JOIN sources ms ON ms.id = pfr.source_id
            WHERE tie.hardware_model_id = pfr.hardware_model_id
              AND tie.region_code = pfr.region_code AND tie.channel = pfr.channel
              AND tie.source_id <> pfr.source_id
              AND ts.currency_rank = ms.currency_rank)
         AND NOT EXISTS (
           SELECT 1 FROM device_catalog_flat d
            WHERE d.hardware_model_id = pfr.hardware_model_id
              AND instr(pfr.build_id,'-') > 1
              AND lower(substr(pfr.build_id, 1, instr(pfr.build_id,'-') - 1)) LIKE
                  lower(replace(replace(replace(d.model_code, d.brand || ' ', ''),
                                        d.brand || '-', ''), ' ', '')) || '_%'))"""

# How to recognise each rewritten statement in the trace. Deliberately the
# distinctive fragment of the NEW text: if a rewrite is reverted or renamed the
# statement is not found and the test fails saying so, rather than silently
# comparing nothing.
SHIPPED_MARKERS = {
    "shared_code": "HAVING count(DISTINCT d.target_id)",
    "with_firmware": "tied_partitions",
}


def shipped_statements(connection) -> dict[str, str]:
    """The SQL check_corpus actually executed, keyed by which rewrite it is."""
    captured: list[str] = []
    connection.set_trace_callback(captured.append)
    try:
        check_corpus(connection, deep=False)
    finally:
        connection.set_trace_callback(None)
    found: dict[str, str] = {}
    for name, marker in SHIPPED_MARKERS.items():
        matches = [s for s in captured if marker in s]
        if matches:
            found[name] = max(matches, key=len)
    return found


class FastChecksAgreeWithTheOriginalsTest(unittest.TestCase):
    """Same answers on planted data, including data where the answer is not 0."""

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, FIXTURE)

    def _compare(self, scenario: str) -> tuple[int, int]:
        c = self.db.connection
        shipped = shipped_statements(c)
        missing = set(SHIPPED_MARKERS) - set(shipped)
        self.assertEqual(set(), missing,
                         f"could not find the rewritten statement(s) {sorted(missing)} among the "
                         f"SQL check_corpus ran; the rewrite was reverted or renamed and this "
                         f"test would otherwise compare nothing")
        results = {}
        for name, reference in (("shared_code", REFERENCE_SHARED_CODE),
                                ("with_firmware", REFERENCE_WITH_FIRMWARE)):
            want = c.execute(reference).fetchone()[0]
            got = c.execute(shipped[name]).fetchone()[0]
            self.assertEqual(want, got,
                             f"[{scenario}] {name}: the rewritten check answers {got} where the "
                             f"original answers {want}. A faster check that reports a different "
                             f"number about the corpus is a regression, not a win.")
            results[name] = want
        return results["shared_code"], results["with_firmware"]

    def _plant_shared_region_code(self) -> None:
        """One region CODE under two vendor namespaces, for one device+channel.

        firmware_targets is UNIQUE(vendor_namespace, target_code), so the same code
        legitimately exists twice; the projection is keyed on the CODE, which is
        what makes it ambiguous. This is the exact shape the check describes.
        """
        c = self.db.connection
        model, channel = c.execute(
            "SELECT hardware_model_id, channel FROM firmware_releases LIMIT 1").fetchone()
        c.execute("INSERT INTO firmware_targets(id,vendor_namespace,target_code,target_kind,"
                  "display_name,created_at,updated_at) VALUES('tgt.ns1','ns-one','SHARED',"
                  "'region','SHARED','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        c.execute("INSERT INTO firmware_targets(id,vendor_namespace,target_code,target_kind,"
                  "display_name,created_at,updated_at) VALUES('tgt.ns2','ns-two','SHARED',"
                  "'region','SHARED','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        for n, target in enumerate(("tgt.ns1", "tgt.ns2")):
            c.execute("INSERT INTO firmware_releases(id,hardware_model_id,firmware_target_id,"
                      "build_id,channel,first_observed_at,last_observed_at,created_at,"
                      "vendor_released_at) VALUES(?,?,?,?,?,'2026-01-01T00:00:00Z',"
                      "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',?)",
                      (f"rel.shared.{n}", model, target, f"SHAREDBUILD{n}", channel,
                       f"2026-0{n + 1}-01"))

    def _plant_tied_publishers(self) -> None:
        """Publishers on one partition, both TIED and NOT tied.

        The tie test only changes an answer for a model with no firmware_releases
        row of its own -- anything already in the UNION's first branch is counted
        whatever the predicate decides, which is why an earlier version of this
        scenario could not tell the rewritten predicate from one with the
        currency_rank comparison deleted. Measured: with the rank dropped, that
        version still passed.

        So two models that have NO releases:

        * `tied` gets two sources at the SAME rank, so no current firmware is
          stated for it and it must NOT be counted;
        * `untied` gets two sources at DIFFERENT ranks, which is not a tie, so it
          MUST be counted.

        The second half is what makes the currency_rank comparison load-bearing:
        drop it and `untied` stops being counted.
        """
        c = self.db.connection
        models = [r[0] for r in c.execute(
            "SELECT hm.id FROM hardware_models hm WHERE NOT EXISTS("
            "SELECT 1 FROM firmware_releases f WHERE f.hardware_model_id=hm.id) ORDER BY hm.id")]
        self.assertGreaterEqual(len(models), 2,
                                "fixture has fewer than two release-free models, so the tie "
                                "predicate cannot change an answer here")
        plan = {"src.tie.a": (models[0], 77), "src.tie.b": (models[0], 77),
                "src.untied.a": (models[1], 11), "src.untied.b": (models[1], 22)}
        for source, (model, rank) in plan.items():
            c.execute("INSERT INTO sources(id,name,authority_scope,enabled,created_at,"
                      "currency_rank) VALUES(?,?,'secondary',1,'2026-01-01T00:00:00Z',?)",
                      (source, f"Tie {source}", rank))
            c.execute("INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,"
                      "parser_version) VALUES(?,?,'2026-01-01T00:00:00Z','succeeded','p','1')",
                      (f"run.{source}", source))
            c.execute("INSERT INTO artifacts(id,source_id,run_id,sha256,media_type,source_url,"
                      "retrieved_at,storage_uri,byte_length) VALUES(?,?,?,?,'text/plain',"
                      "'https://example.invalid/a','2026-01-01T00:00:00Z','file:///a',1)",
                      (f"art.{source}", source, f"run.{source}", source.ljust(64, "0")[:64]))
            c.execute("INSERT INTO source_products(id,manufacturer,canonical_name,"
                      "normalized_name,review_state,created_at,updated_at) VALUES(?,?,?,?,"
                      "'approved','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
                      (f"prod.{source}", "Maker", f"Device {source}", f"device {source}"))
            c.execute("INSERT INTO source_identity_registry(id,source_id,namespace,source_value,"
                      "normalized_value,product_id,resolution_state,resolution_method,"
                      "rule_version,confidence,first_seen_at,last_seen_at) VALUES(?,?,'ns',?,?,?,"
                      "'approved','manual','1','high','2026-01-01T00:00:00Z',"
                      "'2026-01-01T00:00:00Z')",
                      (f"ident.{source}", source, source, source, f"prod.{source}"))
            c.execute("INSERT INTO observations(id,source_id,run_id,artifact_id,record_type,"
                      "source_key,observed_at,payload_json,content_sha256,validation_state)"
                      " VALUES(?,?,?,?,'firmware',?,'2026-01-01T00:00:00Z','{\"data\":{}}',?,"
                      "'promoted')",
                      (f"obs.{source}", source, f"run.{source}", f"art.{source}", source,
                       source.ljust(64, "1")[:64]))
            # Both sources of a pair describe the SAME (model, region, channel).
            # Whether that is a tie is decided by their currency_rank alone.
            # The build id carries no "-", so the sibling-model predicate is false
            # for it and cannot mask what the tie predicate decides.
            c.execute("INSERT INTO product_firmware_releases(id,product_id,identity_id,"
                      "observation_id,source_id,region_code,build_id,channel,android_version,"
                      "android_major,vendor_released_at,delivery_method,created_at,"
                      "hardware_model_id) VALUES(?,?,?,?,?,'TIEREGION',?,'Stable','14',14,"
                      "'2026-05-01','OTA','2026-01-01T00:00:00Z',?)",
                      (f"pfr.{source}", f"prod.{source}", f"ident.{source}", f"obs.{source}",
                       source, "TIEBUILD" + source.replace(".", ""), model))

    def test_they_agree_on_the_shipped_fixture(self) -> None:
        self._compare("shipped fixture")

    def test_they_agree_when_a_region_code_is_genuinely_shared(self) -> None:
        self._plant_shared_region_code()
        shared, _ = self._compare("shared region code planted")
        self.assertGreater(shared, 0,
                           "the planted ambiguity produced no finding, so this scenario "
                           "compared two zeroes and proved nothing")

    def test_they_agree_when_publishers_are_tied_on_rank(self) -> None:
        self._plant_tied_publishers()
        self._compare("tied publishers planted")
        # The tie must actually be a tie, or the rewritten predicate was never
        # exercised by this scenario.
        tied = self.db.connection.execute("""
            SELECT count(*) FROM (
              SELECT 1 FROM product_firmware_releases pfr JOIN sources s ON s.id=pfr.source_id
               WHERE pfr.hardware_model_id IS NOT NULL
               GROUP BY pfr.hardware_model_id, pfr.region_code, pfr.channel, s.currency_rank
              HAVING count(DISTINCT pfr.source_id) > 1)""").fetchone()[0]
        self.assertGreater(tied, 0, "no tied partition exists, so the tie predicate that was "
                                    "rewritten never ran in this scenario")

    def test_they_agree_with_both_plants_at_once(self) -> None:
        self._plant_shared_region_code()
        self._plant_tied_publishers()
        shared, _ = self._compare("both planted")
        self.assertGreater(shared, 0)

    def test_they_agree_when_a_target_id_is_null(self) -> None:
        """A NULL target is reachable and must not change the answer.

        An EMPTY-STRING target is not reachable: firmware_target_id references
        firmware_targets(id), and no row has an empty id, so the foreign key
        refuses it. Verified by test_an_empty_target_id_is_unreachable below.
        """
        c = self.db.connection
        model, channel = c.execute(
            "SELECT hardware_model_id, channel FROM firmware_releases LIMIT 1").fetchone()
        for n in range(2):
            c.execute("INSERT INTO firmware_releases(id,hardware_model_id,firmware_target_id,"
                      "build_id,channel,first_observed_at,last_observed_at,created_at)"
                      " VALUES(?,?,NULL,?,?,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',"
                      "'2026-01-01T00:00:00Z')",
                      (f"rel.null.{n}", model, f"NULLBUILD{n}", channel))
        self._compare("null target id")

    def test_an_empty_target_id_is_unreachable(self) -> None:
        """The precondition the ifnull collapse rests on, asserted not assumed.

        The rewrite joins firmware_targets on the COLLAPSED id where the view
        joined on whichever raw id won the partition. The two can only disagree if
        a firmware_target exists whose id is the empty string -- then `ft.id = ''`
        resolves to a real target code for the rewrite and to none for a partition
        the view happened to award to the NULL row. There is no such target, and
        the foreign key is what keeps it that way.
        """
        c = self.db.connection
        self.assertEqual(0, c.execute(
            "SELECT count(*) FROM firmware_targets WHERE id=''").fetchone()[0],
            "a firmware_target with an empty id makes the ifnull collapse in "
            "device_targets_share_a_region_code disagree with the view it replaced")
        model, channel = c.execute(
            "SELECT hardware_model_id, channel FROM firmware_releases LIMIT 1").fetchone()
        with self.assertRaises(Exception):
            c.execute("INSERT INTO firmware_releases(id,hardware_model_id,firmware_target_id,"
                      "build_id,channel,first_observed_at,last_observed_at,created_at)"
                      " VALUES('rel.empty',?,'',?,?,'2026-01-01T00:00:00Z',"
                      "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
                      (model, "EMPTYBUILD", channel))

    def test_the_divergence_is_real_when_the_precondition_is_violated(self) -> None:
        """Proves the test above guards something, rather than restating a comment.

        With an empty-id target present AND both a NULL and an empty-string target
        in one partition, the two forms give different answers. That is why the
        precondition is asserted instead of assumed.
        """
        c = self.db.connection
        # A target whose id IS the empty string, carrying a real code...
        c.execute("INSERT INTO firmware_targets(id,vendor_namespace,target_code,target_kind,"
                  "display_name,created_at,updated_at) VALUES('','empty','EMPTYCODE','other',"
                  "'EMPTYCODE','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        # ...and a second, ordinary target claiming the SAME code in another
        # namespace, which is exactly the ambiguity this check looks for.
        c.execute("INSERT INTO firmware_targets(id,vendor_namespace,target_code,target_kind,"
                  "display_name,created_at,updated_at) VALUES('tgt.x','other','EMPTYCODE',"
                  "'other','EMPTYCODE','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        model = c.execute("SELECT hardware_model_id FROM firmware_releases LIMIT 1").fetchone()[0]
        # One release with NO target and one with tgt.x, in the beta channel, which the
        # fixture does not use, so they form partitions of their own.
        # The rewrite collapses the NULL to '', which now RESOLVES to EMPTYCODE, so
        # both land in one group and it reports the ambiguity. The view keeps the
        # raw NULL, so its group key is '' and it reports nothing.
        for n, target in enumerate((None, "tgt.x")):
            c.execute("INSERT INTO firmware_releases(id,hardware_model_id,firmware_target_id,"
                      "build_id,channel,first_observed_at,last_observed_at,created_at,"
                      "vendor_released_at) VALUES(?,?,?,?,'beta','2026-01-01T00:00:00Z',"
                      "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',?)",
                      (f"rel.div.{n}", model, target, f"DIVBUILD{n}", f"2026-0{n + 1}-01"))
        shipped = shipped_statements(c)["shared_code"]
        self.assertNotEqual(c.execute(REFERENCE_SHARED_CODE).fetchone()[0],
                            c.execute(shipped).fetchone()[0],
                            "the precondition test guards nothing: violating it did not make "
                            "the two forms disagree, so either the rewrite changed or this "
                            "scenario no longer constructs the divergence")


class TheLiveCorpusAgreesTest(unittest.TestCase):
    """The same comparison on the real 255MB corpus, skipped when it is absent."""

    def setUp(self) -> None:
        if not LIVE_CORPUS.is_file():
            self.skipTest(f"no live corpus at {LIVE_CORPUS}")
        self.db = Database(LIVE_CORPUS)
        self.addCleanup(self.db.close)

    def test_the_rewrites_answer_what_the_originals_answer(self) -> None:
        c = self.db.connection
        shipped = shipped_statements(c)
        self.assertEqual(set(SHIPPED_MARKERS), set(shipped),
                         "a rewritten statement was not found in the SQL check_corpus ran")
        for name, reference in (("shared_code", REFERENCE_SHARED_CODE),
                                ("with_firmware", REFERENCE_WITH_FIRMWARE)):
            self.assertEqual(c.execute(reference).fetchone()[0],
                             c.execute(shipped[name]).fetchone()[0],
                             f"{name} disagrees with the original on the live corpus")

    def test_the_corpus_exercises_the_tie_predicate(self) -> None:
        """Otherwise the live-corpus comparison above says nothing about the tie."""
        self.assertGreater(self.db.connection.execute(
            "SELECT count(*) FROM product_firmware_releases WHERE hardware_model_id IS NOT NULL"
        ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
