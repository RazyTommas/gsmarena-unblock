"""A rebuild is not a restore, and something must finally say so.

HANDOFF.md's single most important caveat, measured: rebuilding from the same
inputs gives **759 identical devices, 106 only in the live corpus, 95 only in a
rebuild** -- 201 resolving differently -- "and nothing detects this". The
changesets added before this are a NARROWER thing and must not be confused with
it: they make a WRITE reversible; they never compare a rebuild against the
corpus it claims to be.

Both halves are tested here and the second half is the one that matters:

  * the check FIRES when subjects the baseline recorded are gone or re-decided;
  * the check STAYS SILENT when the corpus has only grown, which is what every
    nightly batch does to it. An equality test over a digest would pass the
    first and fail the second, fire every single night, and be switched off
    inside a week -- a check that always fires is as useless as one that never
    does.

And a third state, because PASS/FAIL alone reports an absent measurement as a
negative one: a corpus with no recorded baseline is REPORTED as having none,
never as having been checked and found consistent.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory import corpus_identity as ci  # noqa: E402
from mobile_observatory.integrity import check_corpus  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402

NOW = "2026-01-01T00:00:00Z"


class CorpusUnderTest(unittest.TestCase):
    """A real corpus file, because the baseline is a sidecar beside it and
    `baseline_path` derives its location from the connection's own database."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.db = Database.migrated(self.data / "corpus.sqlite")
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")
        self._concluded = 0
        self.variant = self.db.connection.execute(
            "SELECT variant_id FROM hardware_models LIMIT 1").fetchone()[0]

    # -- fixture helpers ---------------------------------------------------

    def conclude(self, product_id: str, *, method: str = "exact_model_code") -> None:
        self.db.connection.execute(
            "INSERT INTO source_products(id,manufacturer,canonical_name,normalized_name,"
            "review_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO NOTHING",
            (product_id, "Acme", product_id, product_id, "approved", NOW, NOW))
        self.db.connection.execute(
            "INSERT INTO identity_conclusions(product_id,conclusion,confidence,method,"
            "rule_version,rationale,candidates_json,evidence_json,concluded_at)"
            " VALUES(?,'auto_approved','high',?,'1','because','[]','[]',?)",
            (product_id, method, NOW))
        self.db.connection.commit()

    def add_device(self, code: str) -> None:
        self.db.connection.execute(
            "INSERT INTO hardware_models(id,variant_id,model_code,model_code_normalized,"
            "codename,created_at,updated_at) VALUES(?,?,?,?,NULL,?,?)",
            (f"hm-{code}", self.variant, code, code.lower(), NOW, NOW))
        self.db.connection.commit()

    def record(self) -> dict:
        return ci.write_baseline(self.db.connection, self.data / ci.BASELINE_FILENAME)

    def identity_findings(self):
        return [f for f in check_corpus(self.db.connection, deep=True)
                if f.check.startswith("corpus_identity")
                or f.check == "corpus_no_longer_matches_its_recorded_identity"]


class ItStaysSilentOnACorpusThatOnlyGrew(CorpusUnderTest):
    """The half that decides whether anybody keeps this check."""

    def test_an_unchanged_corpus_produces_no_finding(self) -> None:
        self.conclude("p-1")
        self.record()
        self.assertEqual([], self.identity_findings())

    def test_a_corpus_that_only_added_subjects_produces_no_finding(self) -> None:
        """What a nightly batch does. New conclusions, new devices, nothing
        withdrawn."""
        self.conclude("p-1")
        self.record()
        self.conclude("p-2")
        self.conclude("p-3")
        self.add_device("NEW-001")
        self.assertEqual([], self.identity_findings(),
                         "growth is not divergence; reporting it would make this fire "
                         "every night and get it disabled")

    def test_a_merged_product_is_not_mass_amnesia(self) -> None:
        """The defect this file was nearly shipped with.

        `merge_confirmed_duplicates` runs on EVERY batch and repoints duplicate
        products at a survivor, so a conclusion about the same device moves to a
        different row id. Keyed by `product_id`, two consecutive batches over one
        fresh corpus reported **464 conclusions forgotten and 464 added** with
        the total unchanged at 2,335 -- an ordinary night reading as mass
        amnesia, on a check whose whole premise is that it stays quiet on
        ordinary nights. Keyed by `(manufacturer, normalized_name)` -- what the
        product IS, and `source_products`' own UNIQUE constraint -- a merge is
        invisible and a withdrawn conclusion is not.
        """
        self.conclude("p-old")
        baseline = self.record()
        # The same product, same manufacturer and name, a new row id.
        self.db.connection.execute("DELETE FROM identity_conclusions WHERE product_id='p-old'")
        self.db.connection.execute("DELETE FROM source_products WHERE id='p-old'")
        self.db.connection.commit()
        self.conclude("p-old")        # canonical_name/normalized_name are the id here
        report = ci.compare(baseline, ci.fingerprint(self.db.connection))
        self.assertEqual(0, report["forgotten"])
        self.assertEqual(0, report["changed"])
        self.assertEqual([], self.identity_findings())

    def test_a_conclusion_about_the_same_product_that_CHANGED_still_fires(self) -> None:
        """The companion assertion: keying by identity must not also hide a real
        re-decision of the same device."""
        self.conclude("p-1", method="exact_model_code")
        self.record()
        self.db.connection.execute(
            "UPDATE identity_conclusions SET method='rebuilt_in_a_different_order'")
        self.db.connection.commit()
        found = self.identity_findings()
        self.assertEqual(1, len(found))
        self.assertIn("changed:Acme|p-1", found[0].detail)

    def test_growth_is_still_counted_as_growth(self) -> None:
        """Silent is not the same as blind: the comparison reports what arrived
        even when it reports no fault."""
        self.conclude("p-1")
        baseline = self.record()
        self.conclude("p-2")
        report = ci.compare(baseline, ci.fingerprint(self.db.connection))
        self.assertEqual(0, report["forgotten"])
        self.assertEqual(0, report["changed"])
        self.assertEqual(1, report["added"])
        self.assertFalse(report["identical"])


class ItFiresWhenTheCorpusForgetsSomethingItConcluded(CorpusUnderTest):
    def test_a_withdrawn_identity_conclusion_is_an_error(self) -> None:
        self.conclude("p-1")
        self.conclude("p-2")
        self.record()
        self.db.connection.execute("DELETE FROM identity_conclusions WHERE product_id='p-2'")
        self.db.connection.commit()
        found = self.identity_findings()
        self.assertEqual(1, len(found))
        self.assertEqual("corpus_no_longer_matches_its_recorded_identity", found[0].check)
        self.assertEqual("error", found[0].severity)
        self.assertIn("forgotten:Acme|p-2", found[0].detail,
                      "the finding has to name the subject; a bare count is a number "
                      "somebody then has to reproduce by hand")

    def test_a_re_decided_conclusion_is_an_error_even_though_the_count_is_equal(self) -> None:
        """The case a row COUNT cannot see, and the one the frozen RULE_VERSION
        exists to prevent: same product, same number of conclusions, different
        decision."""
        self.conclude("p-1")
        self.record()
        before = self.db.connection.execute(
            "SELECT count(*) FROM identity_conclusions").fetchone()[0]
        self.db.connection.execute("UPDATE identity_conclusions SET method='rebuilt_in_"
                                   "a_different_order' WHERE product_id='p-1'")
        self.db.connection.commit()
        after = self.db.connection.execute(
            "SELECT count(*) FROM identity_conclusions").fetchone()[0]
        self.assertEqual(before, after, "the counts are equal; only the decision moved")
        found = self.identity_findings()
        self.assertEqual("error", found[0].severity)
        self.assertIn("changed:Acme|p-1", found[0].detail)

    def test_a_device_the_baseline_published_and_this_corpus_does_not(self) -> None:
        """The 106-only-in-the-live-corpus half of the measured divergence, in
        the table a reader actually sees."""
        self.add_device("GONE-001")
        self.record()
        self.db.connection.execute("DELETE FROM hardware_models WHERE model_code='GONE-001'")
        self.db.connection.commit()
        found = self.identity_findings()
        self.assertEqual("error", found[0].severity)
        self.assertIn("hardware_models", found[0].detail)
        self.assertIn("forgotten:gone-001", found[0].detail)

    def test_a_captured_input_version_the_corpus_no_longer_rests_on(self) -> None:
        """"1,316 observations rest on an input version no longer on disk. Six
        artifact digests exist only in the live corpus.\""""
        baseline = self.record()
        artifacts = baseline["components"]["artifacts"]["count"]
        if not artifacts:
            self.skipTest("the demonstration fixture cites no artifacts")
        self.db.connection.execute("PRAGMA foreign_keys=OFF")
        self.db.connection.execute("DELETE FROM artifacts WHERE id=("
                                   "SELECT id FROM artifacts LIMIT 1)")
        self.db.connection.commit()
        self.assertEqual("error", self.identity_findings()[0].severity)

    def test_both_directions_at_once_report_separately(self) -> None:
        """A rebuild forgets AND adds. The two must not net off against each
        other, which is exactly what a single row count does."""
        self.conclude("p-keep")
        self.conclude("p-lose")
        baseline = self.record()
        self.db.connection.execute("DELETE FROM identity_conclusions WHERE product_id='p-lose'")
        self.db.connection.commit()
        self.conclude("p-new")
        report = ci.compare(baseline, ci.fingerprint(self.db.connection))
        self.assertEqual(1, report["forgotten"])
        self.assertEqual(1, report["added"])
        self.assertEqual(
            baseline["components"]["identity_conclusions"]["count"],
            report["components"]["identity_conclusions"]["current_count"],
            "the counts are equal in both corpora and 2 subjects differ")
        self.assertEqual(1, len(self.identity_findings()))


class ItReportsTheMechanismAndNotOnlyTheSize(CorpusUnderTest):
    """A count sends an operator looking for damage. The mechanism tells them
    whether this is the divergence they already know about.

    Measured on the live corpus against a full cold rebuild: 1,209 conclusions
    frozen at RULE_VERSION 1 against a rebuild that is 100% v2, and v2 strips the
    brand prefix -- so 87 of the 106 model codes only the live corpus has are
    literally `<BRAND> <a code the rebuild does have>`. A report that said only
    "106 devices missing" would describe that as loss.
    """

    def test_a_rule_version_shift_is_named_as_the_mechanism(self) -> None:
        self.conclude("p-1", method="exact_model_code")
        self.db.connection.execute("UPDATE identity_conclusions SET rule_version='1'")
        self.db.connection.commit()
        baseline = self.record()
        self.db.connection.execute("UPDATE identity_conclusions SET rule_version='2'")
        self.db.connection.commit()
        report = ci.compare(baseline, ci.fingerprint(self.db.connection))
        self.assertTrue(report["mechanism"])
        self.assertIn("rule versions", report["mechanism"][0])
        self.assertIn("v1x1", report["mechanism"][0])
        self.assertIn("v2x1", report["mechanism"][0])

    def test_a_brand_prefixed_code_is_named_as_the_same_device(self) -> None:
        self.add_device("ITEL A21")
        baseline = self.record()
        self.db.connection.execute(
            "DELETE FROM hardware_models WHERE model_code='ITEL A21'")
        self.db.connection.commit()
        self.add_device("A21")
        report = ci.compare(baseline, ci.fingerprint(self.db.connection))
        joined = " ".join(report["mechanism"])
        self.assertIn("END WITH a code this corpus DID gain", joined)
        self.assertIn("itel a21", joined)

    def test_a_rebuild_is_not_reported_as_only_loss(self) -> None:
        """docs/BACKUP.md framed the divergence purely as loss. Measured, a
        rebuild also GAINED 4,941 product_firmware_releases."""
        self.add_device("GONE-1")
        baseline = self.record()
        self.db.connection.execute("DELETE FROM hardware_models WHERE model_code='GONE-1'")
        self.db.connection.commit()
        self.add_device("NEW-1")
        joined = " ".join(ci.compare(baseline, ci.fingerprint(self.db.connection))["mechanism"])
        self.assertIn("not only loss", joined)

    def test_the_mechanism_reaches_the_finding_an_operator_reads(self) -> None:
        """It is no use in a dict nobody prints."""
        self.conclude("p-1")
        self.db.connection.execute("UPDATE identity_conclusions SET rule_version='1'")
        self.db.connection.commit()
        self.record()
        self.db.connection.execute("UPDATE identity_conclusions SET rule_version='2'")
        self.db.connection.commit()
        found = self.identity_findings()
        self.assertEqual(1, len(found))
        self.assertIn("MECHANISM:", found[0].detail)
        self.assertIn("rule versions", found[0].detail)

    def test_an_unchanged_corpus_has_no_mechanism_to_report(self) -> None:
        """It speaks only when it has something to say; a sentence attached to
        every comparison is a sentence nobody reads."""
        self.conclude("p-1")
        baseline = self.record()
        self.assertEqual([], ci.compare(baseline,
                                        ci.fingerprint(self.db.connection))["mechanism"])


class AnAbsentBaselineIsReportedAndNeverTreatedAsAPass(CorpusUnderTest):
    def test_no_baseline_is_a_warning_that_says_how_to_record_one(self) -> None:
        found = self.identity_findings()
        self.assertEqual(1, len(found))
        self.assertEqual("corpus_identity_has_no_recorded_baseline", found[0].check)
        self.assertEqual("warning", found[0].severity)
        self.assertIn("corpus_identity record", found[0].detail)

    def test_an_unreadable_baseline_is_a_warning_and_not_a_divergence(self) -> None:
        """Reporting "diverged" over a parse error would send an operator
        hunting for a data problem that is a file problem."""
        (self.data / ci.BASELINE_FILENAME).write_text("{ not json")
        found = self.identity_findings()
        self.assertEqual("warning", found[0].severity)
        self.assertIn("could not be read", found[0].detail)

    def test_a_baseline_from_a_future_format_refuses_to_pretend(self) -> None:
        self.record()
        path = self.data / ci.BASELINE_FILENAME
        stored = json.loads(path.read_text())
        stored["format"] = ci.FORMAT + 1
        path.write_text(json.dumps(stored))
        found = self.identity_findings()
        self.assertEqual("corpus_identity_baseline_is_not_comparable", found[0].check)
        self.assertEqual("warning", found[0].severity)

    def test_an_in_memory_corpus_says_there_is_nowhere_to_record_one(self) -> None:
        memory = Database.migrated()
        try:
            found = [f for f in check_corpus(memory.connection, deep=True)
                     if f.check.startswith("corpus_identity")]
            self.assertEqual(["corpus_identity_cannot_be_located"],
                             [f.check for f in found])
        finally:
            memory.close()


class TheFingerprintItself(CorpusUnderTest):
    def test_the_key_expressions_are_unique_or_it_refuses(self) -> None:
        """A non-unique key would collapse rows, leaving the COUNT honest while
        the comparison went blind over fewer subjects than exist. Caught on the
        live corpus: artifacts keyed by sha256 alone put 21 rows into 20 keys."""
        connection = sqlite3.connect(":memory:")
        with unittest.mock.patch.object(
                ci, "_COMPONENTS",
                (("artifacts", "pins", "SELECT 'same-key', 1 UNION ALL SELECT 'same-key', 2"),)):
            with self.assertRaisesRegex(ValueError, "not unique"):
                ci.fingerprint(connection)
        connection.close()

    def test_a_clock_is_not_part_of_the_identity(self) -> None:
        """`concluded_at` legitimately differs between two corpora holding the
        same conclusion. Including it would make every component differ always
        -- the always-fires failure, arriving through the fingerprint instead of
        through the comparison."""
        self.conclude("p-1")
        first = ci.fingerprint(self.db.connection)
        self.db.connection.execute(
            "UPDATE identity_conclusions SET concluded_at='2030-12-31T00:00:00Z'")
        self.db.connection.commit()
        self.assertEqual(first["digest"], ci.fingerprint(self.db.connection)["digest"])

    def test_the_decision_itself_is_part_of_it(self) -> None:
        """The companion assertion: without this the test above would pass over
        a fingerprint that ignored everything."""
        self.conclude("p-1")
        first = ci.fingerprint(self.db.connection)
        self.db.connection.execute(
            "UPDATE identity_conclusions SET confidence='low'")
        self.db.connection.commit()
        self.assertNotEqual(first["digest"], ci.fingerprint(self.db.connection)["digest"])

    def test_the_baseline_keeps_what_the_previous_one_said(self) -> None:
        self.conclude("p-1")
        first = self.record()
        self.conclude("p-2")
        second = self.record()
        self.assertEqual([first["digest"]], [e["digest"] for e in second["supersedes"]])

    def test_a_countless_baseline_reports_an_unknown_shape_not_a_zero(self) -> None:
        """`--no-entries` can say THAT a corpus diverged and not which subjects
        moved. Reporting 0 forgotten would read as "nothing missing"."""
        self.conclude("p-1")
        compact = ci.fingerprint(self.db.connection, entries=False)
        self.conclude("p-2")
        report = ci.compare(compact, ci.fingerprint(self.db.connection))
        part = report["components"]["identity_conclusions"]
        self.assertIsNone(part["forgotten"], "unknown must not be reported as zero")
        self.assertFalse(part["detail_available"])
        self.assertIn("not derivable", part["examples"][0])


class TheBatchRecordsItAndChecksBeforeRecording(unittest.TestCase):
    """Order is the whole thing: recording before checking would compare this
    run against itself, which is a check that cannot fail."""

    def test_the_batch_records_the_baseline_after_check_corpus(self) -> None:
        import inspect

        from mobile_observatory import batch
        source = inspect.getsource(batch._ingest)
        self.assertIn("corpus_identity.write_baseline", source,
                      "nothing records a baseline, so nothing can ever be compared")
        self.assertLess(source.index("check_corpus(db.connection)"),
                        source.index("corpus_identity.write_baseline"),
                        "the baseline is written before the check, so the check compares "
                        "this run against itself and can never fail")


if __name__ == "__main__":
    unittest.main()
