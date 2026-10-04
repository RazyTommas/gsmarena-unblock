"""The headline build was decided alphabetically, and the corpus did not say so.

`_mark_device_primary` ranked `latest_basis` -> `currency_rank` -> `source_id`,
with the date AFTER the publisher. That was correct when written, for the reason
written beside it: `effective_at` means a vendor release date in one row and a
capture time in another, and ordering a naijarom capture date against an frbox
capture date is what this corpus is not allowed to do. Putting the publisher
first kept every date comparison inside one publisher.

What it also did was make `source_id` ASCENDING the tiebreak whenever two
publishers share a `currency_rank`. Repairing the mifirm release dates (migration
0034) moved 39 Xiaomi devices into exactly that state, and measured on a copy of
the live corpus **4 of them ended up showing a build with an EARLIER stated
release date than the one it replaced** -- trading an order decided by a hash for
one decided by the alphabet.

THE DISTINCTION THE OLD RULE MISSED, and the only thing that makes this safe:
inside `latest_basis='vendor_release_date'` every row's `effective_at` IS the
same kind of measurement. Measured on the live corpus, that basis and
`effective_at_basis='vendor_stated_date'` are the same 536 rows (0 disagreements
either way), all 536 carrying a full 10-character ISO date, 0 NULL. So the date
now comes BEFORE the publisher within that basis, and nowhere else.

Measured after, on a copy: 42 headline changes -> **37**, earlier-date cases
**4 -> 0**, `headline_build_decided_by_publisher_name` **243 -> 89** (all 89 in
the capture-order basis, where the dates genuinely are not comparable; zero in
the vendor-stated-date basis).

WHAT THESE CASES HAVE TO PROVE:

  * the later stated date wins ACROSS publishers, and the name is not consulted;
  * the publisher still comes first for every OTHER basis -- a fix that dropped
    `source_id` outright would pass every date case here and silently re-open the
    218-device cross-publisher capture-date comparison the previous round closed;
  * what decided each pick is RECORDED, and the recorded value is the key that
    actually separated the winner from the runner-up rather than a label;
  * `publisher_identity` and `arbitrary_stable_order` are reachable, because a
    confession nobody can reach is not a confession;
  * one publisher's several regions are NOT reported as an unresolvable tie: the
    live counts are 364 and 1 and folding them together states a far worse fact
    than the one it describes.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import current_firmware as cf  # noqa: E402
from mobile_observatory.database import Database  # noqa: E402
from mobile_observatory.integrity import check_corpus  # noqa: E402
from mobile_observatory.repository import CanonicalRepository  # noqa: E402
from mobile_observatory.server import ObservatoryService  # noqa: E402

NOW = "2026-01-01T00:00:00Z"
MIGRATION = ROOT / "migrations" / "0035_device_primary_basis.sql"


class TheRuleAndTheMigrationAgreeTest(unittest.TestCase):

    def test_the_basis_vocabulary_is_not_two_lists(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        check = re.search(r"device_primary_basis IN \((.*?)\)\)", sql, re.S)
        self.assertIsNotNone(check, "the CHECK constraint is not where this test looks")
        self.assertEqual(set(cf.PRIMARY_BASES), set(re.findall(r"'([a-z_]+)'", check.group(1))))

    def test_the_date_comes_before_the_name_and_after_the_declared_rank(self) -> None:
        """The whole change, as an assertion about the rule rather than its output --
        and the one qualification it needs.

        `currency_rank` is a human-recorded statement that two publishers' dates do
        not measure the same event, so it stays AHEAD of the date; `source_id`
        ascending is a statement about the alphabet, so it goes behind it. Putting
        the rank behind the date as well was tried first and
        test_current_firmware_projection.test_the_most_current_publisher_wins_
        regardless_of_date failed at once, which is what that guard is for.
        """
        names = [name for name, _, _ in cf._PRIMARY_KEYS]
        self.assertLess(names.index("latest_stated_date"), names.index("publisher_identity"),
                        "the alphabet must not outrank a stated release date")
        self.assertLess(names.index("publisher_currency_rank"), names.index("latest_stated_date"),
                        "a declared statement that two publishers' dates are "
                        "incomparable must outrank the comparison it forbids")
        self.assertLess(names.index("latest_basis"), names.index("publisher_currency_rank"),
                        "a capture-order guess must still never outrank a declared latest")

    def test_the_date_key_is_inert_outside_the_vendor_stated_basis(self) -> None:
        """And the publisher key is inert INSIDE it. These two gates are the argument;
        without them this change would be comparing a capture time against a vendor
        release date."""
        date_key = dict((n, s) for n, s, _ in cf._PRIMARY_KEYS)["latest_stated_date"]
        name_key = dict((n, s) for n, s, _ in cf._PRIMARY_KEYS)["publisher_identity"]
        self.assertIn("latest_basis='vendor_release_date'", date_key)
        self.assertIn("latest_basis!='vendor_release_date'", name_key)

    def test_is_not_rather_than_not_equal_is_load_bearing(self) -> None:
        """Asked of the GENERATED SQL over a crafted pair, because the projection
        fixtures cannot reach the state that distinguishes the two.

        Four of the seven keys are NULL outside the basis they apply to, and
        `NULL != NULL` is NULL, which a CASE treats as false -- the same outcome
        `IS NOT` gives, so for two rows of the SAME basis the two spellings agree
        and planting `!=` changes nothing visible. They diverge only where exactly
        one side is NULL on the first differing key, which `effective_at` can be:
        the canonical layer publishes `effective_at_basis='not_captured'` rows with
        no date. With `!=` that pair reports the last key instead of
        `observation_order`, i.e. an honest difference recorded as "nothing
        separated them".
        """
        con = sqlite3.connect(":memory:")
        con.execute("""CREATE TABLE t(latest_basis TEXT, effective_at TEXT, source_id TEXT,
                                      android_major INT, target_key TEXT, channel TEXT)""")
        con.execute("CREATE TABLE sources(id TEXT, currency_rank INT)")
        con.execute("INSERT INTO sources VALUES('s',50)")
        con.executemany("INSERT INTO t VALUES('observation_order_only',?,'s',14,'CN','Stable')",
                        [("2026-01-01",), (None,)])
        rows = [r[0] for r in con.execute(
            f"""SELECT {cf._primary_basis_case('w', 'l')} FROM t w, t l
                 WHERE w.effective_at IS NOT NULL AND l.effective_at IS NULL""")]
        self.assertEqual(["observation_order"], rows,
                         "a dated row beating an undated one of the same basis was "
                         "decided by the date, and must say so")

    def test_the_sql_assumptions_this_rests_on_hold_in_sqlite(self) -> None:
        """Four behaviours the generated SQL depends on, asked of SQLite rather than
        assumed: `||` binding tighter than `IS NOT` (or the last key compares the
        wrong things), `NULL IS NOT NULL` being false (or a gated key is never
        inert), `!=` on two NULLs being NULL (which is why `IS NOT` is required at
        all), and DESC putting NULLs last."""
        con = sqlite3.connect(":memory:")
        q = lambda s: con.execute("SELECT " + s).fetchone()[0]
        self.assertEqual(0, q("'a'||'b' IS NOT 'a'||'b'"))
        self.assertEqual(1, q("'a'||'b' IS NOT 'a'||'c'"))
        self.assertEqual(0, q("NULL IS NOT NULL"))
        self.assertIsNone(q("NULL != NULL"))
        con.execute("CREATE TABLE t(a,b)")
        con.executemany("INSERT INTO t VALUES(?,?)", [(None, "x"), (1, "y")])
        self.assertEqual(
            [("y", 1), ("x", None)],
            [(r[0], r[1]) for r in con.execute("SELECT b,a FROM t ORDER BY a DESC")])


def _two_publisher_device(db, *, rows, model="TIE-1"):
    """One canonical device described by two publishers, both vendor-dated.

    `rows` is (source_id, region, build, released, android_major, basis).
    """
    connection = db.connection
    hardware = CanonicalRepository(db).create_device(
        manufacturer="Xiaomi", brand="Xiaomi", family="Tie fixture",
        variant="Tie fixture", model_code=model)
    for index, (src, region, build, released, major, basis) in enumerate(rows):
        connection.execute(
            # `sources.name` is UNIQUE. A literal name here made the second
            # publisher's row a silent OR IGNORE no-op and the fixture then failed
            # a foreign key two statements later -- the failure path, reached while
            # writing this file.
            "INSERT OR IGNORE INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
            " VALUES(?,?,NULL,'community',1,?)", (src, src, NOW))
        connection.execute(
            "INSERT OR IGNORE INTO ingestion_runs VALUES(?,?,?,?,'succeeded','p','1',0,0,0,NULL)",
            (f"run-{index}", src, NOW, NOW))
        connection.execute(
            "INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,'text/csv',NULL,?,'x',1)",
            (f"art-{index}", src, f"run-{index}", f"{index:064d}".replace("0", "c"), NOW))
        product = f"prod-{index}"
        connection.execute(
            "INSERT INTO source_products VALUES(?,'Xiaomi',?,?,'approved',NULL,?,?)",
            (product, f"Tie fixture {index}", f"tie fixture {index}", NOW, NOW))
        connection.execute(
            "INSERT INTO source_identity_registry VALUES(?,?,'codename',?,?,?,'approved',"
            "'test','1','high',?,?)",
            (f"ident-{index}", src, f"tie{index}", f"tie{index}", product, NOW, NOW))
        connection.execute("INSERT INTO product_hardware_links VALUES(?,?,'test',NULL,?)",
                           (product, hardware, NOW))
        connection.execute(
            "INSERT INTO observations VALUES(?,?,?,?,'firmware_release',?,?,'{}',?,'valid',NULL)",
            (f"obs-{index}", src, f"run-{index}", f"art-{index}", f"key-{index}", NOW,
             f"{index:064d}"))
        connection.execute(
            """INSERT INTO product_firmware_releases
               (id,product_id,identity_id,observation_id,source_id,region_code,build_id,channel,
                android_version,android_major,vendor_released_at,delivery_method,created_at,
                hardware_model_id)
               VALUES(?,?,?,?,?,?,?,'Stable',?,?,?,NULL,?,?)""",
            (f"rel-{index}", product, f"ident-{index}", f"obs-{index}", src, region, build,
             str(major), major, released if basis == "dated" else None, NOW, hardware))
    return hardware


class TheLaterStatedDateWinsTest(unittest.TestCase):
    """`mifirm.` sorts before `xiaomi.`; these fixtures keep that true, so a rule
    that still consulted the name would pick the wrong row and be visible."""

    MIFIRM = "mifirm.community.firmware_archive"
    TRACKER = "xiaomi.community.firmware_tracker"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database.migrated(Path(self.temp.name) / "corpus.sqlite")
        self.addCleanup(self.db.close)
        self.assertLess(self.MIFIRM, self.TRACKER,
                        "the premise of every case here: the name order and the date "
                        "order must be able to disagree")

    def _primary(self, hardware):
        return dict(self.db.connection.execute(
            """SELECT build_id,source_id,effective_at,device_primary_basis,latest_basis
                 FROM device_current_firmware
                WHERE hardware_model_id=? AND is_device_primary=1""", (hardware,)).fetchone())

    def test_the_alphabetically_later_publisher_wins_on_a_later_date(self) -> None:
        hardware = _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "MIFIRM.OLD", "2026-08-15", 15, "dated"),
            (self.TRACKER, "RU", "TRACKER.NEW", "2026-09-04", 15, "dated")])
        cf.build(self.db)
        row = self._primary(hardware)
        self.assertEqual("TRACKER.NEW", row["build_id"],
                         "the later stated release date must win even though its "
                         "publisher sorts last")
        self.assertEqual("latest_stated_date", row["device_primary_basis"])

    def test_the_alphabetically_earlier_publisher_wins_on_a_later_date(self) -> None:
        """The counterweight. A rule that simply inverted the name order would pass
        the case above and fail this one."""
        hardware = _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "MIFIRM.NEW", "2026-09-04", 15, "dated"),
            (self.TRACKER, "RU", "TRACKER.OLD", "2026-08-15", 15, "dated")])
        cf.build(self.db)
        row = self._primary(hardware)
        self.assertEqual("MIFIRM.NEW", row["build_id"])
        self.assertEqual("latest_stated_date", row["device_primary_basis"])

    def test_the_headline_date_can_never_go_backwards_against_a_dated_rival(self) -> None:
        """The property the coordinator asked for, as an invariant rather than a count:
        the chosen row's stated date is the MAXIMUM over every vendor-dated candidate."""
        hardware = _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "A", "2026-08-15", 15, "dated"),
            (self.MIFIRM, "EEA", "B", "2024-01-01", 14, "dated"),
            (self.TRACKER, "RU", "C", "2026-09-04", 15, "dated"),
            (self.TRACKER, "TR", "D", "2025-05-05", 14, "dated")])
        cf.build(self.db)
        best = self.db.connection.execute(
            """SELECT max(effective_at) FROM device_current_firmware
                WHERE hardware_model_id=? AND latest_basis='vendor_release_date'""",
            (hardware,)).fetchone()[0]
        self.assertEqual(best, self._primary(hardware)["effective_at"])

    def test_a_declared_latest_still_outranks_a_newer_undated_guess(self) -> None:
        """Key 1 must stay key 1. Ordering by date first would let a confident old row
        lose to an uncertain new one, which is the rule the previous round wrote."""
        hardware = _two_publisher_device(self.db, rows=[
            (self.TRACKER, "RU", "DATED.OLD", "2020-01-01", 11, "dated"),
            (self.MIFIRM, "CN", "UNDATED.NEW", None, 15, "undated")])
        cf.build(self.db)
        row = self._primary(hardware)
        self.assertEqual("DATED.OLD", row["build_id"])
        self.assertEqual("vendor_release_date", row["latest_basis"])
        self.assertEqual("latest_basis", row["device_primary_basis"])

    def test_the_publisher_still_comes_first_where_the_dates_are_not_comparable(self) -> None:
        """THE REGRESSION GUARD. Dropping `source_id` outright would pass every date
        case above and silently re-open the cross-publisher capture-date comparison
        the previous round closed for 218 devices. With both rows undated, nothing
        may be decided by comparing their capture times across publishers -- so the
        pick is the publisher's, and it says so."""
        hardware = _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "MIFIRM.X", None, 15, "undated"),
            (self.TRACKER, "RU", "TRACKER.Y", None, 15, "undated")])
        cf.build(self.db)
        row = self._primary(hardware)
        self.assertEqual("observation_order_only", row["latest_basis"])
        self.assertEqual("publisher_identity", row["device_primary_basis"],
                         "outside the vendor-stated basis the publisher must still "
                         "decide, and the row must confess that it did")
        self.assertEqual(self.MIFIRM, row["source_id"])

    def test_a_declared_rank_still_outranks_a_newer_date_from_an_archive(self) -> None:
        """The invariant the previous round established, restated here so this file
        cannot be read as having replaced it.

        google.ota.checkin (rank 10) says what the vendor's servers would hand the
        device today; an archive row (rank 50) says a build once existed, and
        2026-09-30 from the archive is not "later" than 2026-01-01 from the
        check-in because they answer different questions. 218 of 274
        multi-publisher devices were once decided by exactly that comparison.
        test_current_firmware_projection holds the same property; this one holds it
        against the key order, which is where it would be lost.
        """
        with self.db.connection:
            self.db.connection.execute(
                "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at,"
                "currency_rank) VALUES('google.ota.checkin','g',NULL,'primary',1,?,10)", (NOW,))
        hardware = _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "ARCHIVE.NEWER", "2026-09-30", 15, "dated"),
            ("google.ota.checkin", "EU", "OTA.OLDER", "2026-01-01", 15, "dated")])
        cf.build(self.db)
        row = self._primary(hardware)
        self.assertEqual("OTA.OLDER", row["build_id"],
                         "a declared currency_rank must still beat a newer archive date")
        self.assertEqual("publisher_currency_rank", row["device_primary_basis"])

    def test_a_higher_android_major_breaks_an_identical_date(self) -> None:
        hardware = _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "LOW", "2026-09-04", 14, "dated"),
            (self.TRACKER, "RU", "HIGH", "2026-09-04", 15, "dated")])
        cf.build(self.db)
        row = self._primary(hardware)
        self.assertEqual("HIGH", row["build_id"])
        self.assertEqual("android_version", row["device_primary_basis"])

    def test_a_genuine_cross_publisher_tie_is_recorded_as_arbitrary(self) -> None:
        """Same date, same Android major, two publishers. On the live corpus this is
        exactly 1 device -- `OS1.0.2.0.TKVCNXM` against `V816.0.2.0.TKVCNXM`, one
        build under two of Xiaomi's own naming conventions. The corpus cannot say
        which is current, so it must not present the pick as a decision.

        DIFFERENT regions, deliberately. Two publishers inside ONE (device, target,
        channel) partition are excluded from the projection entirely by
        EVIDENCE_SQL's tie veto and never reach the device-level pick at all --
        found by writing this case with one region and watching the build refuse to
        publish. The live corpus's single instance reaches the device level for the
        same reason: its two rows are `CN|Stable` and `CN|stable`, which differ.
        """
        hardware = _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "ONE", "2026-09-04", 15, "dated"),
            (self.TRACKER, "TW", "TWO", "2026-09-04", 15, "dated")])
        cf.build(self.db)
        self.assertEqual("arbitrary_stable_order", self._primary(hardware)["device_primary_basis"])
        finding = next((f for f in check_corpus(self.db.connection, deep=False)
                        if f.check == "headline_build_tie_broken_arbitrarily"), None)
        self.assertIsNotNone(finding)
        self.assertEqual("warning", finding.severity)
        self.assertEqual(1, finding.count)

    def test_one_publishers_several_regions_is_not_called_an_unresolvable_tie(self) -> None:
        """364 devices against 1 on the live corpus. Both reach the last ordering key,
        and only one of them is a question the corpus cannot answer: the other is
        which region to show, which the grid shows."""
        hardware = _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "ONE", "2026-09-04", 15, "dated"),
            (self.MIFIRM, "EEA", "TWO", "2026-09-04", 15, "dated")])
        cf.build(self.db)
        self.assertEqual("one_publishers_region_choice",
                         self._primary(hardware)["device_primary_basis"])
        self.assertIsNone(next((f for f in check_corpus(self.db.connection, deep=False)
                                if f.check == "headline_build_tie_broken_arbitrarily"), None),
                          "one publisher's region choice must not be reported as a tie "
                          "the evidence cannot settle")

    def test_a_single_candidate_says_so_rather_than_naming_a_tiebreak(self) -> None:
        hardware = _two_publisher_device(self.db, rows=[
            (self.TRACKER, "RU", "ONLY", "2026-09-04", 15, "dated")])
        cf.build(self.db)
        self.assertEqual("sole_candidate", self._primary(hardware)["device_primary_basis"])

    def test_every_primary_row_records_a_basis(self) -> None:
        _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "A", "2026-09-04", 15, "dated"),
            (self.TRACKER, "RU", "B", "2026-08-15", 15, "dated")])
        cf.build(self.db)
        self.assertEqual(
            0, self.db.connection.execute(
                "SELECT count(*) FROM device_current_firmware"
                " WHERE is_device_primary=1 AND device_primary_basis IS NULL").fetchone()[0])
        self.assertEqual(
            0, self.db.connection.execute(
                "SELECT count(*) FROM device_current_firmware"
                " WHERE is_device_primary=0 AND device_primary_basis IS NOT NULL").fetchone()[0],
            "a basis on a row nobody chose describes a choice nobody made")

    def test_publishing_refuses_a_primary_row_with_no_basis(self) -> None:
        """_validate's half. A pick with no recorded reason is the state this
        projection was in before 0035, and it is the state a reader cannot audit."""
        _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "A", "2026-09-04", 15, "dated")])
        original = cf._mark_device_primary

        def without_the_basis(connection):
            original(connection)
            connection.execute("UPDATE device_current_firmware_staging"
                               " SET device_primary_basis=NULL")
        cf._mark_device_primary = without_the_basis
        self.addCleanup(setattr, cf, "_mark_device_primary", original)
        with self.assertRaises(cf.ProjectionError) as caught:
            cf.build(self.db)
        self.assertIn("record no basis", str(caught.exception))

    def test_publishing_refuses_a_basis_on_a_row_nobody_chose(self) -> None:
        """The mirror of the case above, and the reason the `stray` check exists: a
        basis on a non-primary row describes a choice nobody made.

        The `WHERE is_device_primary=1` on the derivation's UPDATE does not prove
        this -- found by planting its removal and watching nothing happen, because
        the correlated subquery already returns NULL for every non-chosen row. So
        the invariant is held by _validate, and this is the case that exercises it.
        """
        _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "A", "2026-09-04", 15, "dated"),
            (self.TRACKER, "RU", "B", "2026-08-15", 15, "dated")])
        original = cf._mark_device_primary

        def onto_every_row(connection):
            original(connection)
            connection.execute("UPDATE device_current_firmware_staging"
                               " SET device_primary_basis='latest_stated_date'")
        cf._mark_device_primary = onto_every_row
        self.addCleanup(setattr, cf, "_mark_device_primary", original)
        with self.assertRaises(cf.ProjectionError) as caught:
            cf.build(self.db)
        self.assertIn("choice that was not made", str(caught.exception))

    def test_the_publisher_name_check_counts_only_the_devices_it_decided(self) -> None:
        """`headline_build_decided_by_publisher_name` must read the RECORDED value and
        not "has a basis at all". Its first version counted the population at risk
        (243 on the live corpus) rather than the population it happened to (89), and
        a check that reads `IS NOT NULL` would report all 845."""
        _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "X", None, 15, "undated"),
            (self.TRACKER, "RU", "Y", None, 15, "undated")], model="UNDATED-1")
        cf.build(self.db)
        finding = next((f for f in check_corpus(self.db.connection, deep=False)
                        if f.check == "headline_build_decided_by_publisher_name"), None)
        self.assertIsNotNone(finding)
        self.assertEqual(1, finding.count)

    def test_a_date_decided_device_is_not_counted_as_name_decided(self) -> None:
        """The counterweight to the case above: a warning that fires on every device
        is a warning nobody reads."""
        _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "A", "2026-08-15", 15, "dated"),
            (self.TRACKER, "RU", "B", "2026-09-04", 15, "dated")], model="DATED-1")
        cf.build(self.db)
        self.assertIsNone(next((f for f in check_corpus(self.db.connection, deep=False)
                                if f.check == "headline_build_decided_by_publisher_name"), None),
                          "the publisher's name decided nothing here -- the dates did")

    def test_the_recorded_basis_is_what_actually_separated_the_two_rows(self) -> None:
        """Not a label. The winner and the runner-up must AGREE on every key before the
        recorded one and DIFFER on it -- re-derived here from _PRIMARY_KEYS against
        the published rows, so a derivation that drifted from the ordering shows up."""
        hardware = _two_publisher_device(self.db, rows=[
            (self.MIFIRM, "CN", "A", "2026-08-15", 15, "dated"),
            (self.TRACKER, "RU", "B", "2026-09-04", 15, "dated")])
        cf.build(self.db)
        order = cf._primary_order_by("device_current_firmware")
        ranked = self.db.connection.execute(f"""
            SELECT rowid AS rid, row_number() OVER (
                     PARTITION BY hardware_model_id ORDER BY {order}) AS rk
              FROM device_current_firmware WHERE hardware_model_id=?""", (hardware,)).fetchall()
        win = next(r["rid"] for r in ranked if r["rk"] == 1)
        lose = next(r["rid"] for r in ranked if r["rk"] == 2)
        recorded = self._primary(hardware)["device_primary_basis"]
        names = [name for name, _, _ in cf._PRIMARY_KEYS]
        self.assertIn(recorded, names)
        for name, sql, _ in cf._PRIMARY_KEYS:
            differs = self.db.connection.execute(
                f"""SELECT {sql.format(t='w')} IS NOT {sql.format(t='l')}
                      FROM device_current_firmware w, device_current_firmware l
                     WHERE w.rowid=? AND l.rowid=?""", (win, lose)).fetchone()[0]
            if names.index(name) < names.index(recorded):
                self.assertEqual(0, differs, f"an earlier key ({name}) already separated them, "
                                             f"so {recorded} is not what decided it")
            elif name == recorded:
                self.assertEqual(1, differs, f"{recorded} does not separate these two rows")


class TheReaderCanSeeWhatDecidedItTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database.migrated(Path(self.temp.name) / "corpus.sqlite")
        self.addCleanup(self.db.close)
        self.hardware = _two_publisher_device(self.db, rows=[
            ("mifirm.community.firmware_archive", "CN", "A", "2026-08-15", 15, "dated"),
            ("xiaomi.community.firmware_tracker", "RU", "B", "2026-09-04", 15, "dated")])
        cf.build(self.db)
        self.service = ObservatoryService(self.db, Path(self.temp.name) / "local.sqlite",
                                          demonstration=True)
        self.addCleanup(self.service.local.close)

    def test_the_devices_api_states_the_basis(self) -> None:
        row = self.service.devices_page({"model": ["TIE-1"]}).items[0]
        self.assertEqual("latest_stated_date", row["build_choice_basis"])

    def test_the_grid_survives_a_corpus_that_predates_the_column(self) -> None:
        """A hard reference to a column that is not there turns the DEVICE GRID -- the
        front page -- into a 500 on any corpus below 0035, and a restored backup or a
        bundle extracted at schema 8 is exactly such a corpus. The column is DROPped
        rather than mocked, so the degraded path is the one that executes."""
        with self.db.connection:
            self.db.connection.execute(
                "ALTER TABLE device_current_firmware DROP COLUMN device_primary_basis")
        row = self.service.devices_page({"model": ["TIE-1"]}).items[0]
        self.assertEqual("B", row["build"], "the grid must still answer")
        self.assertIsNone(row["build_choice_basis"])
        finding = next((f for f in check_corpus(self.db.connection, deep=False)
                        if f.check == "headline_build_basis_not_recorded"), None)
        self.assertIsNotNone(finding, "the absence must be reported, not silent")

    def test_the_client_has_a_label_for_every_basis(self) -> None:
        """A value the server can emit and the client has no label for renders as
        nothing at all, which is the silence this change exists to remove."""
        app = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
        for basis in cf.PRIMARY_BASES:
            self.assertIn(f"{basis}:", app, f"app.js has no label for {basis}")
        self.assertIn("buildChoiceNote(x)", app)
        # Line comments stripped first. The comment above BUILD_CHOICE_NOTE QUOTES
        # the old sentence to explain why it is gone, and a guard that fires on its
        # own explanation is the shape test_source_stated_dates.py already had to
        # fix once.
        code = "\n".join(re.split(r"//", line, maxsplit=1)[0] for line in app.splitlines())
        self.assertNotIn("rather than ranking publishers it has no basis to rank", code,
                         "the old one-size sentence covered every case and was accurate "
                         "only for the case it described; the row states its own basis now")


class NewReleasesArriveDatedWithoutASecondRepairTest(unittest.TestCase):
    """Migration 0034's repair is a one-shot over rows that existed when it ran. Is
    that a defect for a branch landing afterwards with 8,168 more mifirm releases?

    No, and this is the case that proves it rather than asserting it: new rows are
    written by `promote_approved_product_observations`, which reads every declared
    spelling, so they arrive dated. Verified at scale on a copy of the live corpus
    too -- approving 106 held-back products promoted **14,547** new mifirm releases,
    of which **0** were undated and **0** had channel='unknown', and all **858** new
    events were `vendor_release_date` with **0** `observation_order_only`. Against
    the code at the branch point the same simulation produced 14,544 releases, all
    14,544 undated, and **3,733** permanently hash-ordered events.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database.migrated(Path(self.temp.name) / "corpus.sqlite")
        self.addCleanup(self.db.close)
        self.con = self.db.connection

    def test_a_release_promoted_after_the_migration_needs_no_second_repair(self) -> None:
        from mobile_observatory.enrichment import promote_approved_product_observations
        src = "mifirm.community.firmware_archive"
        with self.con:
            self.con.execute(
                "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at)"
                " VALUES(?,'MiFirm',NULL,'community',1,?)", (src, NOW))
            self.con.execute(
                "INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,"
                "parser_version,accepted_count) VALUES('r',?,?,'succeeded','p','1',0)", (src, NOW))
            self.con.execute("INSERT INTO artifacts VALUES('a',?,'r',?,'application/json',"
                             "NULL,?,'x',1)", (src, "a" * 64, NOW))
            self.con.execute("INSERT INTO source_products VALUES('p','Xiaomi','Redmi 1 W',"
                             "'redmi 1 w','approved',NULL,?,?)", (NOW, NOW))
            self.con.execute(
                "INSERT INTO source_identity_registry VALUES('i',?,'codename','HM','hm','p',"
                "'approved','x','1','high',?,?)", (src, NOW, NOW))
            for obs, build, android, date in (("o1", "B1", "11", "2020-01-01"),
                                              ("o2", "B2", "12", "2021-01-01")):
                self.con.execute(
                    "INSERT INTO observations VALUES(?,?,'r','a','firmware_release',?,?,?,?,"
                    "'valid',NULL)",
                    (obs, src, obs, NOW, json.dumps({"data": {
                        "build": build, "android": android, "region_code": "CN",
                        "channel": "developer", "vendor_released_at": date,
                        "model_code": "HM", "source_device_name": "Redmi 1 W"}}),
                     ("%064d" % int(obs[1]))[:64]))
                self.con.execute(
                    "INSERT INTO observation_product_links VALUES(?,'p','i','approved',?)",
                    (obs, NOW))
        result = promote_approved_product_observations(self.con)
        self.assertEqual(2, result["firmware_releases"])
        self.assertEqual(
            0, self.con.execute("SELECT count(*) FROM product_firmware_releases"
                                " WHERE vendor_released_at IS NULL").fetchone()[0],
            "a release promoted after the migration must arrive dated; the migration's "
            "one-shot UPDATE is not what dates it")
        self.assertEqual(
            0, self.con.execute("SELECT count(*) FROM product_firmware_releases"
                                " WHERE channel='unknown'").fetchone()[0])
        self.assertEqual(1, result["android_upgrade_events_vendor_release_date"])
        self.assertEqual(0, result["android_upgrade_events_observation_order_only"])
        # and the migration's repair clause, re-run now, has nothing left to do
        self.assertEqual(
            0, self.con.execute(
                """SELECT count(*) FROM product_firmware_releases pfr
                     JOIN observations o ON o.id=pfr.observation_id
                    WHERE pfr.vendor_released_at IS NULL
                      AND json_extract(o.payload_json,'$.data.vendor_released_at')
                            GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'""").fetchone()[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
