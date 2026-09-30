"""A recorded write must be reversible, and the reversal must be verifiable.

Every test here asserts against a real SQLite database driven through the same
`changesets` module the batch uses. None of them assert "the function returned
something"; each one changes rows and then checks the rows.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mobile_observatory import changesets


def _corpus(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE device(id TEXT PRIMARY KEY, variant TEXT NOT NULL, codename TEXT);
        CREATE TABLE release(id INTEGER PRIMARY KEY,
                             device_id TEXT NOT NULL REFERENCES device(id),
                             build TEXT NOT NULL);
        CREATE TABLE rationale(product TEXT, reason TEXT);  -- no primary key, on purpose
        INSERT INTO device VALUES ('d1','Galaxy S25','pa1q'),('d2','CAMON 50','TECNO-KJ5');
        INSERT INTO release VALUES (1,'d1','S931BXXU1AYA1'),(2,'d2','KJ5-H694ABCD');
        INSERT INTO rationale VALUES ('p1','stem corroborated');
    """)
    return connection


class SessionReachable(unittest.TestCase):
    def test_the_session_api_is_reachable_and_says_why_when_it_is_not(self):
        support = changesets.session_support()
        self.assertTrue(support.reason, "an unavailable capability must carry its reason")
        if not support.available:
            self.skipTest(f"session extension unreachable here: {support.reason}")

    def test_python_still_has_no_session_binding(self):
        """The reason this module uses ctypes at all.

        If CPython ever grows `Connection.create_session`, this fails and whoever
        sees it should delete the ctypes layer rather than keep it.
        """
        self.assertFalse(hasattr(sqlite3.Connection, "create_session"),
                         "CPython now binds the session API: use it instead of ctypes")


class Reversibility(unittest.TestCase):
    def setUp(self):
        if not changesets.session_support().available:
            self.skipTest(changesets.session_support().reason)
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.connection = _corpus(Path(self._tmp.name) / "corpus.sqlite")
        self.addCleanup(self.connection.close)

    def test_inverting_a_changeset_restores_every_tracked_table(self):
        before = changesets.table_digests(self.connection)
        with changesets.record_changes(self.connection) as recording:
            self.connection.execute("BEGIN")
            self.connection.execute("INSERT INTO device VALUES ('d3','itel A80','itel-A80')")
            self.connection.execute("UPDATE device SET variant='Galaxy S25 Ultra' WHERE id='d1'")
            self.connection.execute("DELETE FROM release WHERE id=2")
            self.connection.execute("INSERT INTO release VALUES (3,'d3','A80-XYZ')")
            self.connection.execute("COMMIT")

        # the write really happened
        during = changesets.table_digests(self.connection)
        self.assertNotEqual(before["device"], during["device"])
        self.assertNotEqual(before["release"], during["release"])

        changesets.apply_changeset(self.connection, changesets.invert(recording.changeset))
        after = changesets.table_digests(self.connection)
        self.assertEqual(before["device"], after["device"])
        self.assertEqual(before["release"], after["release"])
        self.assertEqual(
            [("d1", "Galaxy S25", "pa1q"), ("d2", "CAMON 50", "TECNO-KJ5")],
            self.connection.execute("SELECT * FROM device ORDER BY id").fetchall())

    def test_the_changeset_is_a_small_fraction_of_the_database(self):
        """The whole argument for this over a directory copy.

        Asserted as a RATIO against the file, not an absolute byte count, so it
        keeps meaning as the corpus grows.
        """
        with changesets.record_changes(self.connection) as recording:
            self.connection.execute("UPDATE device SET codename='changed' WHERE id='d1'")
        page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = self.connection.execute("PRAGMA page_count").fetchone()[0]
        self.assertLess(recording.bytes, page_size * page_count)
        self.assertGreater(recording.bytes, 0, "a real change must produce a real changeset")

    def test_a_failed_run_still_yields_its_changeset(self):
        """A batch that dies half way is the run somebody needs to undo."""
        before = changesets.table_digests(self.connection)
        recording = None
        with self.assertRaises(ZeroDivisionError):
            with changesets.record_changes(self.connection) as recording:
                self.connection.execute("INSERT INTO device VALUES ('d9','half','h9')")
                raise ZeroDivisionError("the ingest fell over")
        self.assertIsNotNone(recording)
        self.assertGreater(recording.bytes, 0, "the committed part of a failed run was not recorded")
        changesets.apply_changeset(self.connection, changesets.invert(recording.changeset))
        self.assertEqual(before["device"], changesets.table_digests(self.connection)["device"])

    def test_applying_over_a_moved_corpus_refuses_and_changes_nothing(self):
        with changesets.record_changes(self.connection) as recording:
            self.connection.execute("INSERT INTO device VALUES ('d4','Redmi 14','ruby')")
        changesets.apply_changeset(self.connection, changesets.invert(recording.changeset))
        settled = changesets.table_digests(self.connection)
        with self.assertRaises(changesets.ChangesetConflict):
            changesets.apply_changeset(self.connection, changesets.invert(recording.changeset))
        self.assertEqual(settled, changesets.table_digests(self.connection),
                         "a refused revert must leave the corpus exactly as it was")

    def test_a_refusal_names_the_table_and_the_KIND_of_conflict(self):
        """A wrong label on an honest error is worse than no label.

        The first version of CONFLICT_NAMES was shifted by one and reported a
        UNIQUE-constraint refusal as "FOREIGN_KEY (would leave an orphan)",
        which sent a real investigation after an orphan that did not exist.
        Pinned here against a conflict whose kind is known.
        """
        self.connection.execute(
            "CREATE TABLE unique_keyed(id TEXT PRIMARY KEY, k TEXT, UNIQUE(k))")
        self.connection.execute("INSERT INTO unique_keyed VALUES ('first','shared')")
        with changesets.record_changes(self.connection) as recording:
            self.connection.execute("INSERT INTO unique_keyed VALUES ('second','other')")
        # Make the recorded insert collide on the UNIQUE key rather than the PK.
        self.connection.execute("DELETE FROM unique_keyed WHERE id='second'")
        self.connection.execute("INSERT INTO unique_keyed VALUES ('third','other')")
        with self.assertRaises(changesets.ChangesetConflict) as caught:
            changesets.apply_changeset(self.connection, recording.changeset)
        message = str(caught.exception)
        self.assertIn("unique_keyed", message, message)
        self.assertIn("CONSTRAINT", message, message)
        self.assertNotIn("FOREIGN_KEY", message, message)

    def test_the_conflict_names_start_at_one(self):
        """The off-by-one this file exists to stop coming back."""
        self.assertEqual(1, min(changesets.CONFLICT_NAMES))
        self.assertIn("FOREIGN_KEY", changesets.CONFLICT_NAMES[5])
        self.assertEqual(5, changesets.SQLITE_CHANGESET_FOREIGN_KEY)

    def test_summary_names_the_tables_and_counts_the_operations(self):
        with changesets.record_changes(self.connection) as recording:
            self.connection.execute("INSERT INTO device VALUES ('d5','Pixel 10','frankel')")
            self.connection.execute("DELETE FROM release WHERE id=1")
        summary = changesets.summarise(recording.changeset)
        self.assertEqual(1, summary["device"]["insert"])
        self.assertEqual(1, summary["release"]["delete"])


class TheBlindSpotIsMeasured(unittest.TestCase):
    """A table with no primary key is untracked SILENTLY. That is the danger."""

    def setUp(self):
        if not changesets.session_support().available:
            self.skipTest(changesets.session_support().reason)
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.connection = _corpus(Path(self._tmp.name) / "corpus.sqlite")
        self.addCleanup(self.connection.close)

    def test_a_table_without_a_primary_key_is_named(self):
        self.assertEqual(["rationale"],
                         changesets.tables_invisible_to_a_changeset(self.connection))

    def test_and_its_rows_really_are_missing_from_the_changeset(self):
        """Proves the claim in the docstring instead of restating it."""
        with changesets.record_changes(self.connection) as recording:
            self.connection.execute("INSERT INTO rationale VALUES ('p2','catalog confirmed')")
            self.connection.execute("INSERT INTO device VALUES ('d6','A','a')")
        self.assertNotIn("rationale", changesets.summarise(recording.changeset))
        changesets.apply_changeset(self.connection, changesets.invert(recording.changeset))
        self.assertEqual(2, self.connection.execute("SELECT count(*) FROM rationale").fetchone()[0],
                         "the un-keyed row survived the revert, which is the documented limit")

    def test_a_virtual_table_is_not_reported_as_a_blind_spot(self):
        """Its shadow tables have primary keys, so its content IS recorded.

        Asserted by changing the virtual table and finding the change in the
        changeset -- the only way to know the exclusion is correct rather than
        convenient.
        """
        self.connection.execute(
            "CREATE VIRTUAL TABLE fts USING fts5(body, tokenize='trigram')")
        self.assertNotIn("fts", changesets.tables_invisible_to_a_changeset(self.connection))
        with changesets.record_changes(self.connection) as recording:
            self.connection.execute("INSERT INTO fts(body) VALUES ('S931BXXU1AYA1')")
        touched = set(changesets.summarise(recording.changeset))
        self.assertTrue([name for name in touched if name.startswith("fts_")],
                        f"no fts5 shadow table in the changeset; only {sorted(touched)}")

    def test_schema_changes_are_reported_because_they_cannot_be_inverted(self):
        with changesets.record_changes(self.connection) as recording:
            self.connection.execute("CREATE TABLE late(id INTEGER PRIMARY KEY)")
        self.assertTrue(recording.schema_changed,
                        "a run that created a table must report its schema moved")
        with changesets.record_changes(self.connection) as quiet:
            self.connection.execute("UPDATE device SET codename='x' WHERE id='d1'")
        self.assertFalse(quiet.schema_changed)


class TheStore(unittest.TestCase):
    def setUp(self):
        if not changesets.session_support().available:
            self.skipTest(changesets.session_support().reason)
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.connection = _corpus(Path(self._tmp.name) / "corpus.sqlite")
        self.addCleanup(self.connection.close)

    def test_a_recording_is_written_with_metadata_and_reads_back_byte_exact(self):
        with changesets.record_changes(self.connection) as recording:
            self.connection.execute("INSERT INTO device VALUES ('d7','Nothing 3','wiz')")
        store = changesets.ChangesetStore(Path(self._tmp.name) / "changesets")
        path = store.write(recording, run="batch-test")
        self.assertEqual(recording.changeset, path.read_bytes())
        self.assertEqual(recording.changeset, store.read(path.name))
        entries = store.entries()
        self.assertEqual(1, len(entries))
        self.assertEqual(len(recording.changeset), entries[0]["bytes"])
        self.assertEqual("batch-test", entries[0]["run"])
        self.assertIn("device", entries[0]["changes"])


class DegradedPath(unittest.TestCase):
    """The unavailable branch must be exercised, not merely reasoned about."""

    def test_disabling_the_session_reports_a_reason_and_raises_on_use(self):
        previous = changesets._support
        os.environ[changesets.DISABLE_ENV] = "1"
        changesets._support = None
        try:
            support = changesets.session_support()
            self.assertFalse(support.available)
            self.assertIn(changesets.DISABLE_ENV, support.reason)
            with TemporaryDirectory() as tmp:
                connection = _corpus(Path(tmp) / "corpus.sqlite")
                try:
                    with self.assertRaises(changesets.ChangesetUnavailable):
                        with changesets.record_changes(connection):
                            pass
                finally:
                    connection.close()
        finally:
            os.environ.pop(changesets.DISABLE_ENV, None)
            changesets._support = previous


if __name__ == "__main__":
    unittest.main()
