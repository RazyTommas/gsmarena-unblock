"""5,643 of 5,804 published "this build came after that one" claims were decided by a hash.

THE DEFECT. `promote_approved_product_observations` read `$.data.release_date` and
`$.data.branch` -- the xiaomi tracker's vocabulary.
`mifirm.community.firmware_archive` publishes the same two facts as
`$.data.vendor_released_at` and `$.data.channel`, so all 21,845 of its promoted
releases stored `vendor_released_at IS NULL` and `channel='unknown'` with a real
date sitting in the payload beside them. Nothing errored.
`android_version_changed` then orders its before/after pair by
`ORDER BY ... vendor_released_at, id`, and with every date NULL the sort collapsed
onto `id` -- a uuid5 digest.

Measured on a read-only copy of the live corpus, 2026-10-04:

    5,804  android_version_changed events published
    5,643  rest on a pair where BOTH releases are undated
       56  rest on a pair where both carry a stated date
      105  cite release rows the corpus no longer holds
    5,588  of the 5,690 whose inputs survive would NOT exist with the dates applied
    1,064  that the dates would produce were never published
      102  are the same claim either way

WHAT THESE CASES HAVE TO PROVE, beyond "mifirm's date lands now":

  * BOTH spellings work. A fix that read only `vendor_released_at` would pass any
    test about mifirm and silently lose all 4,880 xiaomi dates.
  * the rule is ONE rule, and the NEXT adapter is caught rather than the last one.
    `test_every_adapter_date_and_channel_field_is_declared` scans every adapter
    for a key that is date-ish or channel-ish and appears in none of
    source_dates' declared lists. That is the part that would have caught this
    defect on the day the mifirm adapter was written.
  * a date that is not a vendor release date is still NOT read as one. frbox's
    `build_date` comes out of a version string and naijarom's `date_token` is
    declared opaque by its own payload; reading either would put a date parsed
    from a filename next to a date a vendor published and then sort the two.
  * the basis is FROZEN. Migration 0034 backfills 21,845 release dates, so a basis
    derived at read time would report every one of those 5,643 hash-ordered events
    as `vendor_release_date` the moment the backfill landed. The re-dating case
    below is the one that fails if the record is ever re-derived.
  * migration 0034's two halves run IN ORDER -- record, then repair. Reversed, it
    labels 5,643 hash-ordered events as date-ordered, and every other case here
    still passes.
  * nothing is RETRACTED. domain_events is append-only by trigger and the event
    count does not move.
"""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory.database import Database  # noqa: E402
from mobile_observatory.enrichment import promote_approved_product_observations  # noqa: E402
from mobile_observatory.firmware_order import ORDERING_BASES, ordering_basis  # noqa: E402
from mobile_observatory.integrity import check_corpus  # noqa: E402
from mobile_observatory.source_dates import (  # noqa: E402
    DATE_FIELDS_NOT_A_RELEASE_DATE, PUBLICATION_DATE_FIELDS,
    SECURITY_PATCH_LEVEL_FIELDS, STATED_CHANNEL_FIELDS, VENDOR_RELEASE_DATE_FIELDS,
    stated_channel, vendor_release_date)

NOW = "2026-01-01T00:00:00Z"
MIGRATION = ROOT / "migrations" / "0034_firmware_order_basis.sql"


class TheFieldNameRuleIsDeclaredOnceTest(unittest.TestCase):
    """The spellings live in source_dates and the adapters are scanned against them."""

    # TOKENS that make a payload key a date or a channel. Matched per
    # underscore-separated word and not as a substring: `date` as a substring also
    # matches `update_title`, and a check that fires on an obvious non-date is a
    # check someone widens the exemption list for. Deliberately broad otherwise --
    # it has to fire on a name nobody thought of, which is the whole point.
    DATEISH = ("date", "dates", "released", "month", "time", "at", "day", "timestamp",
               "published", "publish")
    CHANNELISH = ("channel", "branch", "track")

    def _adapter_payload_keys(self) -> dict[str, set[str]]:
        """Every string key any adapter writes into an observation's `data` dict.

        Read out of the SOURCE with ast rather than by running the adapters: an
        adapter that needs a captured input file would otherwise be skipped, and a
        skipped adapter is exactly where the next unread spelling hides.
        """
        found: dict[str, set[str]] = {}
        for path in sorted((ROOT / "src" / "mobile_observatory" / "collectors" / "adapters")
                           .glob("*.py")):
            if path.name == "__init__.py":
                continue
            keys: set[str] = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Dict):
                    for key in node.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            keys.add(key.value)
            found[path.name] = keys
        return found

    def test_the_adapter_scan_actually_sees_the_adapters(self) -> None:
        """A scan over an empty set reports no offenders and proves nothing. This is the
        case that fails if the adapter directory moves or the ast walk stops matching."""
        keys = self._adapter_payload_keys()
        self.assertGreaterEqual(len(keys), 9, keys)
        self.assertIn("mifirm_archive.py", keys)
        self.assertIn("vendor_released_at", keys["mifirm_archive.py"],
                      "the scan cannot see the key whose absence from the read path "
                      "caused this defect")
        self.assertIn("release_date", keys["xiaomi_tracker.py"])

    def test_every_adapter_date_and_channel_field_is_declared(self) -> None:
        """The pattern, not the instance.

        A date-ish or channel-ish key an adapter publishes must appear in one of
        source_dates' lists: read as a release date, read as a bulletin date, read
        by observations.effective_at, or explicitly NOT read with the adapter's own
        reason recorded. A key in none of them is a fact the read path cannot see,
        which is what `vendor_released_at` was for 21,845 releases.
        """
        declared = (set(VENDOR_RELEASE_DATE_FIELDS) | set(STATED_CHANNEL_FIELDS)
                    | set(PUBLICATION_DATE_FIELDS) | set(SECURITY_PATCH_LEVEL_FIELDS)
                    | set(DATE_FIELDS_NOT_A_RELEASE_DATE))
        undeclared = []
        for adapter, keys in self._adapter_payload_keys().items():
            for key in sorted(keys):
                if key in declared or key.endswith("_basis") or key.endswith("_state"):
                    continue
                tokens = set(key.lower().split("_"))
                if tokens & set(self.DATEISH + self.CHANNELISH):
                    undeclared.append(f"{adapter}: {key}")
        self.assertEqual(
            [], undeclared,
            "declare each of these in source_dates -- as a spelling that IS read, or "
            "in DATE_FIELDS_NOT_A_RELEASE_DATE with the reason it is not")

    def test_the_fields_that_are_not_release_dates_each_record_a_reason(self) -> None:
        """A list of names with no reasons goes stale invisibly. Each entry names the
        `date_basis` its own adapter publishes, so the claim is checkable."""
        for field, reason in DATE_FIELDS_NOT_A_RELEASE_DATE.items():
            with self.subTest(field=field):
                self.assertTrue(reason and "not" in reason,
                                f"{field} must record WHY it is not a release date")

    def test_both_publishers_spellings_are_read(self) -> None:
        self.assertEqual("2025-05-06", vendor_release_date({"release_date": "2025-05-06"}))
        self.assertEqual("2018-09-26", vendor_release_date({"vendor_released_at": "2018-09-26"}))
        self.assertEqual("Stable", stated_channel({"branch": "Stable"}))
        self.assertEqual("developer", stated_channel({"channel": "developer"}))

    def test_the_shape_rule_still_applies_to_every_spelling(self) -> None:
        """The field-name half must not become a way around the shape half: the string
        'null' in `vendor_released_at` has to read as absence exactly as it does in
        `release_date`."""
        for field in VENDOR_RELEASE_DATE_FIELDS:
            with self.subTest(field=field):
                self.assertIsNone(vendor_release_date({field: "null"}))
                self.assertIsNone(vendor_release_date({field: "2025-01"}))
                self.assertIsNone(vendor_release_date({field: 20250131}))

    def test_an_absent_channel_is_still_absent(self) -> None:
        """3,190 promoted releases come from publishers that name no channel at all.
        They must keep saying so rather than borrowing another source's default."""
        self.assertEqual("unknown", stated_channel({}))
        self.assertEqual("unknown", stated_channel({"channel": "   "}))
        self.assertEqual("unknown", stated_channel({"branch": None, "channel": None}))

    def test_no_second_spelling_survives_in_the_read_path(self) -> None:
        """One rule. `data.get("release_date")` / `data.get("branch")` reaching a reader
        again means the rule has stopped being one rule, which is the shape this
        codebase has paid for three times."""
        offenders = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            if path.name in ("source_dates.py", "firmware_order.py"):
                continue
            if path.parent.name == "adapters":
                continue  # an adapter WRITES its own vocabulary; that is its job
            text = path.read_text(encoding="utf-8")
            docstrings = set()
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    doc = node.body[0] if node.body else None
                    if (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
                            and isinstance(doc.value.value, str)):
                        docstrings.update(range(doc.lineno, doc.end_lineno + 1))
            for number, line in enumerate(text.splitlines(), 1):
                if number in docstrings:
                    continue
                code = re.split(r"#", line, maxsplit=1)[0]
                for field in (*VENDOR_RELEASE_DATE_FIELDS, *STATED_CHANNEL_FIELDS):
                    # Keyed on the RECEIVER being the payload's `data` dict. These
                    # names are also column names (`vendor_released_at` is one), so
                    # matching `.get("vendor_released_at")` on anything would report
                    # every read of a database row as a second spelling, and a check
                    # that cries wolf gets the exemption widened until it is silent.
                    if re.search(r"""\b(?:data|payload)\s*(?:\.get\(\s*|\[\s*)['"]%s['"]"""
                                 % re.escape(field), code):
                        offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual([], offenders,
                         "read these through source_dates.vendor_release_date / "
                         "stated_channel, so a third spelling cannot be missed again")


class TheMigrationAndTheCodeAgreeTest(unittest.TestCase):

    def test_the_basis_vocabulary_is_not_two_lists(self) -> None:
        """The CHECK constraint and ORDERING_BASES must hold the same words. A migration
        cannot import Python, so the check is that the .sql names every one of them
        and no others."""
        sql = MIGRATION.read_text(encoding="utf-8")
        check = re.search(r"ordering_basis\s+TEXT NOT NULL CHECK \(ordering_basis IN \((.*?)\)\)",
                          sql, re.S)
        self.assertIsNotNone(check, "the CHECK constraint is not where this test looks")
        in_sql = set(re.findall(r"'([a-z_]+)'", check.group(1)))
        self.assertEqual(set(ORDERING_BASES), in_sql)

    def test_the_basis_vocabulary_reuses_the_words_that_already_exist(self) -> None:
        """`software_state_basis` (migration 0019) already distinguishes these states. A
        second set of words for one concept is how the first drifts."""
        existing = (ROOT / "migrations" / "0019_device_current_firmware.sql").read_text(
            encoding="utf-8")
        for word in ("vendor_release_date", "observation_order_only"):
            self.assertIn(f"'{word}'", existing,
                          f"{word} is supposed to be the EXISTING vocabulary")

    def test_the_backfill_cannot_run_before_the_basis_is_recorded(self) -> None:
        """The one ordering in this migration that, reversed, is silently wrong: every
        other case in this file still passes while 5,643 hash-ordered events get
        labelled `vendor_release_date`."""
        sql = MIGRATION.read_text(encoding="utf-8")
        record = sql.index("INSERT OR IGNORE INTO domain_event_ordering")
        repair = sql.index("UPDATE product_firmware_releases SET vendor_released_at")
        self.assertLess(record, repair,
                        "the ordering basis of every existing event must be measured on the "
                        "UNDATED corpus, before the dates are repaired")

    def test_the_migration_retracts_nothing(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        statements = re.findall(r"\b(DELETE FROM|DROP TABLE|DROP TRIGGER)\s+(\w+)", sql, re.I)
        self.assertEqual([], statements, "this migration withdraws no published fact")
        self.assertNotIn("UPDATE domain_events", sql)


class OrderBasisIsRecordedWithTheEventTest(unittest.TestCase):
    """A tiny corpus shaped exactly like the mifirm rows that caused this."""

    def setUp(self) -> None:
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        self.con = self.db.connection
        with self.con:
            self.con.execute(
                "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
                " VALUES('mifirm.community.firmware_archive','MiFirm',NULL,'community',1,?)", (NOW,))
            self.con.execute(
                "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,"
                "parser_version,accepted_count) VALUES('r','mifirm.community.firmware_archive',"
                "?,'succeeded','p','1',0)", (NOW,))
            self.con.execute("INSERT INTO artifacts VALUES('a','mifirm.community.firmware_archive',"
                             "'r',?,'application/json',NULL,?,'x',1)", ("a" * 64, NOW))
            self.con.execute("INSERT INTO source_products VALUES('p','Xiaomi','Redmi 1 W',"
                             "'redmi 1 w','approved',NULL,?,?)", (NOW, NOW))
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES('i',"
                "'mifirm.community.firmware_archive','codename','HM2013023','hm2013023','p',"
                "'approved','x','1','high',?,?)", (NOW, NOW))

    def _release(self, key: str, *, build: str, android: str, date: str | None,
                 channel: str | None = "stable", field: str = "vendor_released_at",
                 branch: str | None = None) -> str:
        data = {"build": build, "android": android, "region_code": "CN",
                "model_code": "HM2013023", "source_device_name": "Redmi 1 W"}
        if date is not None:
            data[field] = date
        if branch is not None:
            data["branch"] = branch
        if channel is not None:
            data["channel"] = channel
        with self.con:
            self.con.execute(
                "INSERT INTO observations VALUES(?,'mifirm.community.firmware_archive','r','a',"
                "'firmware_release',?,?,?,?,'valid',NULL)",
                (key, key, NOW, json.dumps({"data": data}), ("%064d" % abs(hash(key)))[:64]))
            self.con.execute("INSERT INTO observation_product_links VALUES(?,'p','i','approved',?)",
                             (key, NOW))
        return key

    def _bases(self) -> list[tuple]:
        return [tuple(r) for r in self.con.execute(
            "SELECT ordering_basis,before_released_at,after_released_at"
            " FROM domain_event_ordering")]

    def test_the_promotion_reads_mifirms_spelling(self) -> None:
        """The defect itself, at its root."""
        self._release("k", build="7.11.16", android="4.4", date="2018-09-26")
        promote_approved_product_observations(self.con)
        self.assertEqual([("2018-09-26", "stable")], [tuple(r) for r in self.con.execute(
            "SELECT vendor_released_at,channel FROM product_firmware_releases")])

    def test_the_promotion_still_reads_the_other_spelling(self) -> None:
        """The counterweight. A fix that only learned mifirm's name would pass the case
        above and drop all 4,880 dates the xiaomi tracker states."""
        self._release("k", build="B1", android="12", date="2025-05-06", field="release_date",
                      channel=None, branch="Stable")
        promote_approved_product_observations(self.con)
        self.assertEqual([("2025-05-06", "Stable")], [tuple(r) for r in self.con.execute(
            "SELECT vendor_released_at,channel FROM product_firmware_releases")])

    def test_two_dated_releases_record_a_date_basis(self) -> None:
        self._release("a", build="B1", android="11", date="2021-01-01")
        self._release("b", build="B2", android="12", date="2022-02-02")
        result = promote_approved_product_observations(self.con)
        self.assertEqual(1, result["android_upgrade_events"])
        self.assertEqual(1, result["android_upgrade_events_vendor_release_date"])
        self.assertEqual(0, result["android_upgrade_events_observation_order_only"])
        self.assertEqual([("vendor_release_date", "2021-01-01", "2022-02-02")], self._bases())

    def test_two_undated_releases_record_observation_order_only(self) -> None:
        """The 5,643. Note the event is still PUBLISHED -- what changes is that it says
        what decided it."""
        self._release("a", build="B1", android="11", date=None)
        self._release("b", build="B2", android="12", date=None)
        result = promote_approved_product_observations(self.con)
        self.assertEqual(1, result["android_upgrade_events"])
        self.assertEqual(1, result["android_upgrade_events_observation_order_only"])
        self.assertEqual(0, result["android_upgrade_events_vendor_release_date"])
        self.assertEqual([("observation_order_only", None, None)], self._bases())
        self.assertEqual(1, self.con.execute(
            "SELECT count(*) FROM domain_events WHERE event_type='android_version_changed'"
        ).fetchone()[0], "the claim is not withdrawn")

    def test_one_dated_and_one_undated_is_its_own_state(self) -> None:
        """SQLite sorts NULL first, so the undated row is treated as the earlier one --
        a conclusion no date supports. It must not be filed under either of the two
        clean answers."""
        self._release("a", build="B1", android="11", date=None)
        self._release("b", build="B2", android="12", date="2022-02-02")
        promote_approved_product_observations(self.con)
        self.assertEqual([("mixed_dated_and_undated", None, "2022-02-02")], self._bases())

    def _re_date_the_releases(self) -> None:
        """Exactly what migration 0034's second half does to the live corpus."""
        with self.con:
            self.con.execute("UPDATE product_firmware_releases SET vendor_released_at="
                             "'2020-01-01' WHERE android_major=11")
            self.con.execute("UPDATE product_firmware_releases SET vendor_released_at="
                             "'2020-06-01' WHERE android_major=12")

    def test_a_recorded_basis_is_frozen_against_a_later_re_dating(self) -> None:
        """THE case that fails if the basis is ever derived at read time instead of
        recorded at decision time. Migration 0034 re-dates 21,845 releases, so a
        derived basis would relabel 5,643 hash-ordered events as date-ordered -- an
        old claim wearing new evidence.

        Two independent mechanisms hold this, and this case passes while EITHER one
        does, which makes it useless for telling them apart -- proven by planting
        each in turn and watching it stay green. So it is kept as the end-to-end
        statement and the two cases below isolate the halves.
        """
        self._release("a", build="B1", android="11", date=None)
        self._release("b", build="B2", android="12", date=None)
        promote_approved_product_observations(self.con)
        self.assertEqual([("observation_order_only", None, None)], self._bases())
        self._re_date_the_releases()
        promote_approved_product_observations(self.con)
        self.assertEqual([("observation_order_only", None, None)], self._bases(),
                         "the basis describes the moment the order was decided and "
                         "must not move when the releases are re-dated")

    def test_recording_a_basis_twice_keeps_the_FIRST_answer(self) -> None:
        """Half one, isolated: INSERT OR IGNORE and never OR REPLACE. Called directly,
        because the promotion will not reach this function twice for one event --
        which is half two, and why a test going through the promotion cannot see
        this one."""
        from mobile_observatory.firmware_order import record_ordering_basis
        with self.con:
            self.con.execute("INSERT INTO domain_events VALUES('e',"
                             "'android_version_changed','source_product','p','dk',?,?,"
                             "'{}','{}',NULL,NULL)", (NOW, NOW))
            first = record_ordering_basis(self.con, event_id="e", before_release_id="b",
                                          after_release_id="a", before_released_at=None,
                                          after_released_at=None, recorded_at=NOW)
            record_ordering_basis(self.con, event_id="e", before_release_id="b",
                                  after_release_id="a", before_released_at="2020-01-01",
                                  after_released_at="2021-01-01", recorded_at=NOW)
        self.assertEqual("observation_order_only", first)
        self.assertEqual([("observation_order_only", None, None)], self._bases(),
                         "a second call with re-dated releases must not overwrite the "
                         "answer that was true when the order was decided")

    def test_an_existing_event_is_never_given_a_basis_after_the_fact(self) -> None:
        """Half two, isolated: `record_ordering_basis` is called only on the branch
        where the domain_events INSERT actually inserted.

        The row is deleted first, so OR IGNORE cannot be what holds the line. If the
        promotion filled the gap on a later run it would fill it from TODAY's
        releases -- which migration 0034 has re-dated -- and label a hash-ordered
        claim `vendor_release_date`. The honest answer is to leave the gap and let
        check_corpus report it as an error, which is what this asserts.
        """
        self._release("a", build="B1", android="11", date=None)
        self._release("b", build="B2", android="12", date=None)
        promote_approved_product_observations(self.con)
        self.assertEqual(1, len(self._bases()))
        with self.con:
            self.con.execute("DELETE FROM domain_event_ordering")
        self._re_date_the_releases()
        promote_approved_product_observations(self.con)
        self.assertEqual([], self._bases(),
                         "a basis cannot honestly be filled in later: by then the "
                         "releases carry dates they did not have when the order was "
                         "decided. check_corpus reports the absence as an error")
        finding = self._finding("event_order_basis_not_recorded")
        self.assertIsNotNone(finding)
        self.assertEqual("error", finding.severity)

    def test_the_channel_partitions_the_history(self) -> None:
        """mifirm publishes `stable` and `developer`, and both were recorded as the one
        value 'unknown' -- so two separate branches were walked as one sequence."""
        self._release("a", build="B1", android="11", date="2021-01-01", channel="stable")
        self._release("b", build="B2", android="12", date="2022-02-02", channel="developer")
        promote_approved_product_observations(self.con)
        self.assertEqual({"stable", "developer"}, {r[0] for r in self.con.execute(
            "SELECT channel FROM product_firmware_releases")})
        self.assertEqual(0, self.con.execute(
            "SELECT count(*) FROM domain_events WHERE event_type='android_version_changed'"
        ).fetchone()[0], "an upgrade is not evidence across two different branches")

    def test_an_event_with_no_basis_row_is_an_error_not_a_guess(self) -> None:
        self._release("a", build="B1", android="11", date=None)
        self._release("b", build="B2", android="12", date=None)
        promote_approved_product_observations(self.con)
        self.assertIsNone(self._finding("event_order_basis_not_recorded"))
        with self.con:
            self.con.execute("DELETE FROM domain_event_ordering")
        finding = self._finding("event_order_basis_not_recorded")
        self.assertIsNotNone(finding)
        self.assertEqual("error", finding.severity)
        self.assertEqual(1, finding.count)

    def test_row_id_ordering_is_reported_as_a_warning_with_its_count(self) -> None:
        self._release("a", build="B1", android="11", date=None)
        self._release("b", build="B2", android="12", date=None)
        promote_approved_product_observations(self.con)
        finding = self._finding("firmware_order_decided_by_row_id_not_a_date")
        self.assertIsNotNone(finding)
        self.assertEqual("warning", finding.severity)
        self.assertEqual(1, finding.count)

    def test_a_fully_dated_corpus_raises_no_order_warning(self) -> None:
        """The counterweight: a warning that always fires is a warning nobody reads."""
        self._release("a", build="B1", android="11", date="2021-01-01")
        self._release("b", build="B2", android="12", date="2022-02-02")
        promote_approved_product_observations(self.con)
        self.assertIsNone(self._finding("firmware_order_decided_by_row_id_not_a_date"))

    def _finding(self, check: str):
        return next((f for f in check_corpus(self.con, deep=False) if f.check == check), None)


class ThePureFunctionTest(unittest.TestCase):

    def test_every_state_is_reachable_from_the_two_dates(self) -> None:
        self.assertEqual("vendor_release_date", ordering_basis("2020-01-01", "2021-01-01"))
        self.assertEqual("observation_order_only", ordering_basis(None, None))
        self.assertEqual("mixed_dated_and_undated", ordering_basis(None, "2021-01-01"))
        self.assertEqual("mixed_dated_and_undated", ordering_basis("2020-01-01", None))

    def test_cited_releases_absent_is_not_reachable_from_two_dates(self) -> None:
        """It is the migration's answer for 105 events whose release rows are gone, and
        must never be produced by a live decision -- a live decision always has both
        rows in hand."""
        for before in (None, "2020-01-01"):
            for after in (None, "2021-01-01"):
                self.assertNotEqual("cited_releases_absent", ordering_basis(before, after))


class TheMigrationRunsOnACorpusThatAlreadyHasTheDefectTest(unittest.TestCase):
    """Migration 0034 against the state `.observatory-data` is actually in.

    Every case above builds a corpus at head schema and promotes into it, so none
    of them exercises the migration's own backfill. This one builds the schema-33
    situation -- undated mifirm releases with events already published on top of
    them -- applies 0034, and asserts the two halves happened in the right order.
    """

    def setUp(self) -> None:
        import sqlite3 as _sqlite3
        self.con = _sqlite3.connect(":memory:")
        self.con.row_factory = _sqlite3.Row
        self.addCleanup(self.con.close)
        migrations = sorted((ROOT / "migrations").glob("*.sql"))
        self.before_0034 = [p for p in migrations if not p.name.startswith("0034")]
        self.only_0034 = next(p for p in migrations if p.name.startswith("0034"))

    def _apply(self, paths) -> None:
        for path in paths:
            self.con.executescript(path.read_text(encoding="utf-8"))

    def _defective_corpus(self) -> None:
        """Two undated mifirm releases and the event that a hash ordered."""
        self._apply(self.before_0034)
        with self.con:
            self.con.execute(
                "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
                " VALUES('mifirm.community.firmware_archive','MiFirm',NULL,'community',1,?)", (NOW,))
            self.con.execute(
                "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,"
                "parser_version,accepted_count) VALUES('r','mifirm.community.firmware_archive',"
                "?,'succeeded','p','1',0)", (NOW,))
            self.con.execute("INSERT INTO artifacts VALUES('a','mifirm.community.firmware_archive',"
                             "'r',?,'application/json',NULL,?,'x',1)", ("a" * 64, NOW))
            self.con.execute("INSERT INTO source_products VALUES('p','Xiaomi','Redmi 1 W',"
                             "'redmi 1 w','approved',NULL,?,?)", (NOW, NOW))
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES('i',"
                "'mifirm.community.firmware_archive','codename','HM2013023','hm2013023','p',"
                "'approved','x','1','high',?,?)", (NOW, NOW))
            for obs, build, android in (("o1", "B1", "11"), ("o2", "B2", "12")):
                self.con.execute(
                    "INSERT INTO observations VALUES(?,'mifirm.community.firmware_archive','r','a',"
                    "'firmware_release',?,?,?,?,'valid',NULL)",
                    (obs, obs, NOW, json.dumps({"data": {
                        "build": build, "android": android, "region_code": "CN",
                        "channel": "stable", "vendor_released_at": "2020-0%s-01" % android[-1],
                        "model_code": "HM2013023", "source_device_name": "Redmi 1 W"}}),
                     ("%064d" % (1 if obs == "o1" else 2))[:64]))
            # The releases AS THE DEFECT LEFT THEM: no date, channel 'unknown'.
            # The uuid5 ids below are literal so the dedupe_key slices are exact.
            self.before_id = "11111111-1111-5111-8111-111111111111"
            self.after_id = "22222222-2222-5222-8222-222222222222"
            for rid, obs, build, android, major in (
                    (self.before_id, "o1", "B1", "11", 11), (self.after_id, "o2", "B2", "12", 12)):
                self.con.execute(
                    "INSERT INTO product_firmware_releases VALUES(?,'p','i',?,"
                    "'mifirm.community.firmware_archive','CN',?,'unknown',?,?,NULL,NULL,?,NULL)",
                    (rid, obs, build, android, major, NOW))
            self.dedupe = "product-android:p:CN:unknown:%s:%s" % (self.before_id, self.after_id)
            self.con.execute(
                "INSERT INTO domain_events VALUES('e','android_version_changed','source_product',"
                "'p',?,?,?,'{}','{}',NULL,NULL)", (self.dedupe, NOW, NOW))

    def test_the_event_is_recorded_as_hash_ordered_and_the_releases_are_then_dated(self) -> None:
        self._defective_corpus()
        self._apply([self.only_0034])
        self.assertEqual(
            [("observation_order_only", self.before_id, self.after_id, None, None)],
            [tuple(r) for r in self.con.execute(
                "SELECT ordering_basis,before_release_id,after_release_id,"
                "before_released_at,after_released_at FROM domain_event_ordering")],
            "the basis must describe the UNDATED corpus the order was decided on")
        self.assertEqual([("2020-01-01", "stable"), ("2020-02-01", "stable")],
                         sorted(tuple(r) for r in self.con.execute(
                             "SELECT vendor_released_at,channel FROM product_firmware_releases")),
                         "and the releases must carry the date the source stated all along")

    def test_the_dedupe_key_slices_find_the_right_releases(self) -> None:
        """The migration reads the pair out of `dedupe_key` by fixed-width substr. If
        the slice is off by one, every event gets `cited_releases_absent` -- which
        still produces a row and a basis, so only a case that checks the IDS catches
        it."""
        self._defective_corpus()
        self._apply([self.only_0034])
        row = self.con.execute("SELECT * FROM domain_event_ordering").fetchone()
        self.assertEqual(self.before_id, row["before_release_id"])
        self.assertEqual(self.after_id, row["after_release_id"])
        self.assertNotEqual("cited_releases_absent", row["ordering_basis"])

    def test_an_event_whose_releases_are_gone_is_not_called_row_id_ordered(self) -> None:
        """105 live events cite releases the corpus no longer holds. 'we cannot
        establish this' and 'this was decided by a row id' are different statements."""
        self._defective_corpus()
        with self.con:
            self.con.execute(
                "INSERT INTO domain_events VALUES('e2','android_version_changed',"
                "'source_product','p',?,?,?,'{}','{}',NULL,NULL)",
                ("product-android:p:CN:unknown:33333333-3333-5333-8333-333333333333:"
                 "44444444-4444-5444-8444-444444444444", NOW, NOW))
        self._apply([self.only_0034])
        self.assertEqual("cited_releases_absent", self.con.execute(
            "SELECT ordering_basis FROM domain_event_ordering WHERE event_id='e2'").fetchone()[0])

    def test_the_backfill_records_what_it_changed(self) -> None:
        """A derived read model may be repaired; a repair that leaves no record of the
        previous value may not. Migration 0031 set this precedent for the same column."""
        self._defective_corpus()
        self._apply([self.only_0034])
        rows = [dict(r) for r in self.con.execute(
            "SELECT * FROM source_data_corrections WHERE reason LIKE '%field_names%'")]
        self.assertEqual(2, len(rows))
        before = json.loads(rows[0]["before_json"])
        self.assertIsNone(before["vendor_released_at"])
        self.assertEqual("unknown", before["channel"])
        self.assertIsNotNone(json.loads(rows[0]["after_json"])["vendor_released_at"])

    def test_the_published_event_itself_is_untouched(self) -> None:
        self._defective_corpus()
        before = dict(self.con.execute("SELECT * FROM domain_events WHERE id='e'").fetchone())
        self._apply([self.only_0034])
        after = dict(self.con.execute("SELECT * FROM domain_events WHERE id='e'").fetchone())
        self.assertEqual(before, after)
        self.assertEqual(1, self.con.execute("SELECT count(*) FROM domain_events").fetchone()[0])

    def test_a_release_the_source_gave_no_date_keeps_none(self) -> None:
        """The counterweight. 3,190 releases are honestly channel-less and the sources
        behind 3,196 of them publish no date at all; a backfill that invented
        something for those would pass every case above."""
        self._defective_corpus()
        with self.con:
            self.con.execute(
                "INSERT INTO observations VALUES('o3','mifirm.community.firmware_archive','r','a',"
                "'firmware_release','o3',?,?,?,'valid',NULL)",
                (NOW, json.dumps({"data": {"build": "B3", "region_code": "CN"}}),
                 ("%064d" % 3)[:64]))
            self.con.execute(
                "INSERT INTO product_firmware_releases VALUES("
                "'33333333-3333-5333-8333-333333333333','p','i','o3',"
                "'mifirm.community.firmware_archive','CN','B3','unknown',NULL,NULL,NULL,NULL,?,NULL)",
                (NOW,))
        self._apply([self.only_0034])
        self.assertEqual((None, "unknown"), tuple(self.con.execute(
            "SELECT vendor_released_at,channel FROM product_firmware_releases"
            " WHERE observation_id='o3'").fetchone()))

    def test_the_constraints_refuse_a_mislabelled_basis(self) -> None:
        """The one failure mode that would make this table worse than no table: a basis
        that names dates it does not have. Enforced by the database, not the writer.

        REAL event ids, and a well-labelled row inserted first for each one. The
        first version of this case used invented ids, so every insert raised
        IntegrityError from the `REFERENCES domain_events(id)` foreign key and the
        case passed with the CHECK constraints removed entirely -- a test that could
        not fail, found by planting exactly that.
        """
        self._defective_corpus()
        self._apply([self.only_0034])
        cases = [("vendor_release_date", None, None),
                 ("vendor_release_date", "2020-01-01", None),
                 ("vendor_release_date", None, "2021-01-01"),
                 ("observation_order_only", "2020-01-01", "2021-01-01"),
                 ("observation_order_only", "2020-01-01", None)]
        with self.con:
            for index in range(len(cases)):
                self.con.execute(
                    "INSERT INTO domain_events VALUES(?,'android_version_changed',"
                    "'source_product','p',?,?,?,'{}','{}',NULL,NULL)",
                    (f"real{index}", f"dedupe-{index}", NOW, NOW))
        for index, (basis, before, after) in enumerate(cases):
            with self.subTest(basis=basis, before=before, after=after):
                # The foreign key and every other constraint are satisfied by this
                # id: proven by inserting a well-labelled row for it and rolling
                # back, so the refusal below can only be the CHECK.
                with self.con:
                    self.con.execute(
                        "INSERT INTO domain_event_ordering VALUES(?,?,NULL,NULL,NULL,NULL,?)",
                        (f"real{index}", "observation_order_only", NOW))
                with self.con:
                    self.con.execute("DELETE FROM domain_event_ordering WHERE event_id=?",
                                     (f"real{index}",))
                with self.assertRaises(sqlite3.IntegrityError), self.con:
                    self.con.execute(
                        "INSERT INTO domain_event_ordering VALUES(?,?,NULL,NULL,?,?,?)",
                        (f"real{index}", basis, before, after, NOW))

    def test_check_corpus_reports_the_table_being_absent_rather_than_crashing(self) -> None:
        """check_corpus is pointed at restored backups and at corpora a tool opened
        read-only. A `no such table` turns every other finding into "the check
        crashed", which is how one missing table becomes a health endpoint that
        answers nothing."""
        self._defective_corpus()
        findings = check_corpus(self.con, deep=False)
        absent = next(f for f in findings if f.check == "event_order_basis_table_absent")
        # A warning: the feed degrades to a null basis and the client prints "Order
        # basis not recorded", so nothing false is stated. The error-severity half
        # is `event_order_basis_not_recorded` -- rows missing while the table
        # EXISTS, which can only be a writer bypassing firmware_order.
        self.assertEqual("warning", absent.severity)
        self.assertEqual(1, absent.count)
        self._apply([self.only_0034])
        self.assertIsNone(next((f for f in check_corpus(self.con, deep=False)
                                if f.check == "event_order_basis_table_absent"), None))

    def test_the_generated_column_gap_is_reported_only_on_the_deep_scan(self) -> None:
        """222ms of 96,319-row scan, and check_corpus(deep=False) runs on every page
        load. Measured, not assumed -- see the comment in integrity.py."""
        self._defective_corpus()
        self._apply([self.only_0034])
        check = "source_stated_release_date_not_read_by_the_generated_column"
        self.assertIsNone(next((f for f in check_corpus(self.con, deep=False)
                                if f.check == check), None))
        deep = next((f for f in check_corpus(self.con, deep=True) if f.check == check), None)
        self.assertIsNotNone(deep, "the gap must still be REPORTED, just not per request")
        self.assertEqual("warning", deep.severity)
        self.assertEqual(2, deep.count)


class TheBasisReachesTheReaderTest(unittest.TestCase):
    """A basis recorded in a table nobody renders is not legibility.

    The whole point of this change is that a reader can tell a dated claim from a
    hash-ordered one, so the feed that shows the claims has to carry it and the
    client has to print it.
    """

    def setUp(self) -> None:
        import tempfile
        from mobile_observatory.server import ObservatoryService
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.db = Database.migrated(Path(self._temp.name) / "corpus.sqlite")
        self.addCleanup(self.db.close)
        self.con = self.db.connection
        with self.con:
            self.con.execute(
                "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
                " VALUES('mifirm.community.firmware_archive','MiFirm',NULL,'community',1,?)", (NOW,))
            self.con.execute(
                "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,"
                "parser_version,accepted_count) VALUES('r','mifirm.community.firmware_archive',"
                "?,'succeeded','p','1',0)", (NOW,))
            self.con.execute("INSERT INTO artifacts VALUES('a','mifirm.community.firmware_archive',"
                             "'r',?,'application/json',NULL,?,'x',1)", ("a" * 64, NOW))
            self.con.execute("INSERT INTO source_products VALUES('p','Xiaomi','Redmi 1 W',"
                             "'redmi 1 w','approved',NULL,?,?)", (NOW, NOW))
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES('i',"
                "'mifirm.community.firmware_archive','codename','HM2013023','hm2013023','p',"
                "'approved','x','1','high',?,?)", (NOW, NOW))
            # DATED, so the pair's order is the dates and this fixture is
            # deterministic. Undated, it is not: the walk reads the pair in uuid5
            # order, and with these two observation ids the hash puts Android 12
            # BEFORE Android 11, so the loop sees a downgrade and publishes no
            # event at all. That is the other half of the damage -- the hash order
            # did not only invent 5,588 claims, it also suppressed the 1,064 real
            # ones -- and it is measured in the migration's own notes rather than
            # left to a fixture whose outcome depends on a digest.
            for obs, build, android, date in (("o1", "B1", "11", "2020-01-01"),
                                              ("o2", "B2", "12", "2021-01-01")):
                self.con.execute(
                    "INSERT INTO observations VALUES(?,'mifirm.community.firmware_archive','r','a',"
                    "'firmware_release',?,?,?,?,'valid',NULL)",
                    (obs, obs, NOW, json.dumps({"data": {
                        "build": build, "android": android, "region_code": "CN",
                        "channel": "stable", "vendor_released_at": date,
                        "model_code": "HM2013023",
                        "source_device_name": "Redmi 1 W"}}), ("%064d" % int(obs[1]))[:64]))
                self.con.execute(
                    "INSERT INTO observation_product_links VALUES(?,'p','i','approved',?)",
                    (obs, NOW))
        self.assertEqual(1, promote_approved_product_observations(
            self.con)["android_upgrade_events_vendor_release_date"])
        self.service = ObservatoryService(self.db, Path(self._temp.name) / "local.sqlite",
                                          demonstration=True)
        self.addCleanup(self.service.local.close)

    def _restate_as_row_id_ordered(self) -> None:
        """Replace the recorded basis with the state 5,643 live events are in. The
        CHECK constraints forbid `observation_order_only` beside two dates, so this
        is a delete and an insert -- which is also the proof that the constraint is
        doing its job."""
        with self.con:
            event = self.con.execute("SELECT event_id,before_release_id,after_release_id"
                                     " FROM domain_event_ordering").fetchone()
            self.con.execute("DELETE FROM domain_event_ordering")
            self.con.execute("INSERT INTO domain_event_ordering VALUES(?,?,?,?,NULL,NULL,?)",
                             (event["event_id"], "observation_order_only",
                              event["before_release_id"], event["after_release_id"], NOW))

    def _upgrades(self) -> list[dict]:
        rows = self.service.updates_page({"tab": ["history"], "limit": ["50"]}).items
        return [r for r in rows if r["change"] == "Android upgrade"]

    def test_the_radar_feed_states_the_basis_of_every_came_after_claim(self) -> None:
        upgrades = self._upgrades()
        self.assertTrue(upgrades)
        for row in upgrades:
            self.assertEqual("vendor_release_date", row["orderingBasis"])
            self.assertEqual("2020-01-01", row["orderedFrom"])
            self.assertEqual("2021-01-01", row["orderedTo"])

    def test_the_feed_distinguishes_a_row_id_ordered_claim(self) -> None:
        """The distinction is the whole deliverable: the same event, the same
        before/after builds, and a reader can tell which kind of claim it is."""
        dated = self._upgrades()
        self._restate_as_row_id_ordered()
        hashed = self._upgrades()
        self.assertEqual([r["id"] for r in dated], [r["id"] for r in hashed])
        self.assertEqual([r["buildTo"] for r in dated], [r["buildTo"] for r in hashed])
        self.assertNotEqual([r["orderingBasis"] for r in dated],
                            [r["orderingBasis"] for r in hashed])
        self.assertEqual(["observation_order_only"], [r["orderingBasis"] for r in hashed])
        self.assertIsNone(hashed[0]["orderedFrom"])

    def test_the_product_detail_states_it_too(self) -> None:
        """One device must not describe itself two ways -- the same reason
        _firmware_holdings became the single answer behind the grid and the detail."""
        payload = self.service.product_detail("p")
        self.assertTrue(payload["androidUpgrades"])
        for upgrade in payload["androidUpgrades"]:
            self.assertEqual("vendor_release_date", upgrade["orderingBasis"])
        self._restate_as_row_id_ordered()
        self.assertEqual(["observation_order_only"],
                         [u["orderingBasis"] for u in
                          self.service.product_detail("p")["androidUpgrades"]])

    def test_a_missing_basis_is_reported_as_null_and_never_defaulted(self) -> None:
        """If the feed defaulted it, `event_order_basis_not_recorded` would be an error
        nobody could ever see on screen."""
        with self.con:
            self.con.execute("DELETE FROM domain_event_ordering")
        upgrades = self._upgrades()
        self.assertTrue(upgrades)
        for row in upgrades:
            self.assertIsNone(row["orderingBasis"])
        self.assertEqual([None], [u["orderingBasis"] for u in
                                  self.service.product_detail("p")["androidUpgrades"]])

    def test_the_feed_survives_a_corpus_that_predates_the_table(self) -> None:
        """A hard join against a table that is not there turns the whole Update Radar
        into a 500, and the server, the batch and tools/ are all pointed at corpora
        that may be behind -- a restored backup, a bundle extracted at schema 8, the
        live directory in the moment before the startup migration runs. Losing the
        feed is much worse than losing one column of it.

        DROPped rather than mocked, so the degraded path is the one that executes.
        """
        with self.con:
            self.con.execute("DROP TABLE domain_event_ordering")
        upgrades = self._upgrades()
        self.assertTrue(upgrades, "the feed must still answer")
        self.assertIsNone(upgrades[0]["orderingBasis"])
        self.assertEqual([None], [u["orderingBasis"] for u in
                                  self.service.product_detail("p")["androidUpgrades"]])
        finding = next((f for f in check_corpus(self.con, deep=False)
                        if f.check == "event_order_basis_table_absent"), None)
        self.assertIsNotNone(finding)
        self.assertEqual("warning", finding.severity,
                         "a corpus that cannot attribute its claims and SAYS SO states "
                         "nothing false -- that is this module's own line between a "
                         "warning and an error")

    def test_the_client_renders_every_basis_and_the_missing_one(self) -> None:
        """The vocabulary has to exist on both sides. A state the server can emit and
        the client has no label for renders as nothing at all, which is the silence
        this change exists to remove."""
        app = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
        for basis in ORDERING_BASES:
            self.assertIn(f"{basis}:", app, f"app.js has no label for {basis}")
        self.assertIn("Order basis not recorded", app)
        self.assertIn("orderBasisLine(x)", app)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
