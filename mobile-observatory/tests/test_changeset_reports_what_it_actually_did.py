"""A run that produced no rollback artifact must not say it did.

MEASURED on a cold first build, in the batch's own JSON summary:

    "changeset": {"error": "sqlite3session_changeset failed (rc=17)",
                  "reason": "sqlite3session via ctypes",
                  "recorded": true}

and no `changesets/` directory on disk at all. Two separate defects in one line:

  * **The honesty bug.** `recorded` was a hardcoded `True` sitting beside
    `**stored`, so it described the code path taken rather than the artifact
    produced. A scheduler reading that JSON was told it had an undo for the
    FIRST batch -- the run with the most to undo and the only one that had none.

  * **rc=17 is SQLITE_SCHEMA, and the migrations were NOT the cause.** That was
    the obvious reading and it is wrong: a session survives `apply_migrations()`
    intact (measured: rc=0, 1,930 bytes). Bisected phase by phase against a real
    cold ingest, the session breaks at the FIRST captured source; bisected again
    by attaching one table at a time, it is `observations`:

        attach=ALL              -> rc=17
        attach=observations     -> rc=17
        attach=ingestion_runs   -> ok bytes=209
        attach=artifacts        -> ok bytes=586
        attach=sources          -> ok bytes=141

    `observations.effective_at` is a VIRTUAL GENERATED column, and the session
    extension cannot produce a changeset for such a table -- attaching it fails
    the WHOLE session, not just that table.

    **This was never only a cold-build problem.** The ingest uses
    `INSERT OR IGNORE`, so a steady-state night writes no new observation and the
    session never touches `observations`; the changeset therefore worked on
    exactly the runs with nothing to undo and failed on every run that captured
    something new. The 285 KB nightly changesets were the no-op runs.

Each half is guarded by REPRODUCING the hazard first: a test that only asserts
the fix works would pass just as well if the hazard had never existed.
"""
from __future__ import annotations

import inspect
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database, batch, changesets  # noqa: E402


def _session_or_skip():
    support = changesets.session_support()
    if not support.available:
        raise unittest.SkipTest(f"no session extension here: {support.reason}")


class RecordedMeansAnArtifactExists(unittest.TestCase):
    """`recorded` is derived from what the store returned."""

    def test_a_failed_collection_is_not_reported_as_recorded(self) -> None:
        result = batch.changeset_result(
            "sqlite3session via ctypes", False,
            {"error": "sqlite3session_changeset failed (rc=17)"})
        self.assertFalse(result["recorded"])
        self.assertIn("rc=17", result["error"])

    def test_a_written_changeset_is_reported_as_recorded(self) -> None:
        result = batch.changeset_result(
            "sqlite3session via ctypes", False,
            {"path": "/data/changesets/x.changeset", "error": "", "bytes": 285184})
        self.assertTrue(result["recorded"])

    def test_a_store_that_returned_no_path_is_not_recorded_either(self) -> None:
        """`_store_changeset` returns `{"error": "the session was never created"}`
        when the recording is None. No path, no artifact, no claim."""
        self.assertFalse(batch.changeset_result("r", False, {"error": "", "bytes": 0})["recorded"])

    def test_run_batch_has_no_second_copy_of_the_derivation(self) -> None:
        """One rule. The defect was a hardcoded True at the one site that
        mattered, and a second copy is how the first one survives a fix."""
        source = inspect.getsource(batch.run_batch)
        self.assertIn("changeset_result(", source)
        self.assertNotIn('"recorded": True', source)


class AGeneratedColumnCostsTheWholeChangeset(unittest.TestCase):
    """Reproduce the hazard, then prove the fix keeps the rest of the diff."""

    def setUp(self) -> None:
        _session_or_skip()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = sqlite3.connect(Path(self.temp.name) / "x.sqlite")
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE plain(id INTEGER PRIMARY KEY, v TEXT)")
        self.connection.execute(
            "CREATE TABLE derived(id INTEGER PRIMARY KEY, v TEXT, "
            "shouty TEXT GENERATED ALWAYS AS (upper(v)) VIRTUAL)")
        self.connection.commit()

    def test_a_generated_column_table_is_identified(self) -> None:
        self.assertEqual(["derived"],
                         changesets.tables_with_a_generated_column(self.connection))

    def test_attaching_it_returns_schema_for_the_whole_session(self) -> None:
        """The measured hazard, reproduced with the raw API. If this ever stops
        failing, the exclusion below is guarding nothing and should be removed
        deliberately rather than kept out of superstition."""
        import ctypes

        library = changesets._library
        session = ctypes.c_void_p()
        self.assertEqual(0, library.sqlite3session_create(
            changesets._handle(self.connection), b"main", ctypes.byref(session)))
        try:
            self.assertEqual(0, library.sqlite3session_attach(session, None))
            self.connection.execute("INSERT INTO plain VALUES(1,'a')")
            self.connection.execute("INSERT INTO derived(id,v) VALUES(1,'b')")
            self.connection.commit()
            size, buffer = ctypes.c_int(), ctypes.c_void_p()
            rc = library.sqlite3session_changeset(
                session, ctypes.byref(size), ctypes.byref(buffer))
            if rc == 0:
                library.sqlite3_free(buffer)
            self.assertEqual(17, rc,
                             "a generated column no longer poisons the session; the "
                             "exclusion in record_changes is now unnecessary")
        finally:
            library.sqlite3session_delete(session)

    def test_record_changes_keeps_the_rest_of_the_diff_and_names_what_it_lost(self) -> None:
        with changesets.record_changes(self.connection) as recording:
            self.connection.execute("INSERT INTO plain VALUES(1,'a')")
            self.connection.execute("INSERT INTO derived(id,v) VALUES(1,'b')")
            self.connection.commit()
        self.assertEqual("", recording.error)
        self.assertEqual({"plain"}, set(changesets.summarise(recording.changeset)))
        self.assertEqual(("derived",), recording.tables_with_generated_columns)
        self.assertIn("derived", recording.tables_untracked,
                      "the lost table must be named, or the revert's gap is invisible")

    def test_the_lost_table_reaches_check_corpus(self) -> None:
        """A blind spot nobody is told about is the defect, not the exclusion.

        Against the REAL schema, not the two-table fixture above: the table this
        actually costs is `observations`, which is 57% of the corpus, and the
        finding has to name it where an operator looks.
        """
        from mobile_observatory.integrity import check_corpus

        corpus = Database.migrated(Path(self.temp.name) / "corpus.sqlite")
        self.addCleanup(corpus.close)
        self.assertIn("observations",
                      changesets.tables_with_a_generated_column(corpus.connection))
        checks = {f.check: f for f in check_corpus(corpus.connection, deep=False)}
        finding = checks.get("table_cannot_be_in_a_changeset_generated_column")
        self.assertIsNotNone(finding, "the gap is not reported anywhere an operator looks")
        self.assertIn("observations", finding.detail)
        self.assertIn("in NO changeset", finding.detail)

    def test_a_table_created_inside_the_block_is_reported_as_unrecorded(self) -> None:
        """The cost of attaching by name instead of with NULL, stated rather than
        discovered during a revert."""
        with changesets.record_changes(self.connection) as recording:
            self.connection.execute("CREATE TABLE later(id INTEGER PRIMARY KEY, v TEXT)")
            self.connection.execute("INSERT INTO later VALUES(1,'a')")
            self.connection.commit()
        self.assertEqual("", recording.error)
        self.assertIn("later", recording.tables_created_after_attach)
        self.assertIn("later", recording.tables_untracked)

    def test_migrating_first_still_reports_the_schema_move(self) -> None:
        """The reordering is hardening, not the rc=17 fix -- but `schema_changed`
        must stay true through it, or a revert stops refusing a run it cannot
        undo."""
        before = changesets.schema_digest(self.connection)
        self.connection.execute("CREATE TABLE added(id INTEGER PRIMARY KEY, v TEXT)")
        self.connection.commit()
        with changesets.record_changes(
                self.connection, schema_digest_before=before) as recording:
            self.connection.execute("INSERT INTO plain VALUES(1,'a')")
            self.connection.commit()
        self.assertEqual("", recording.error)
        self.assertTrue(recording.schema_changed)

    def test_the_batch_migrates_outside_the_session(self) -> None:
        source = inspect.getsource(batch.run_batch)
        self.assertLess(source.index("db.apply_migrations()"),
                        source.index("changesets.record_changes("))
        self.assertIn("schema_digest_before=schema_before", source)


class AFirstBuildSaysItRecordedNothing(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def test_an_empty_corpus_is_a_first_build(self) -> None:
        db = Database(self.data / "corpus.sqlite")
        self.addCleanup(db.close)
        self.assertTrue(batch.first_build(db.connection),
                        "a corpus with no schema at all is a first build")
        db.apply_migrations()
        self.assertTrue(batch.first_build(db.connection),
                        "a migrated but empty corpus is still a first build: "
                        "Database() CREATES the file, so 'the file exists' proves nothing")

    def test_a_corpus_with_sources_is_not_a_first_build(self) -> None:
        from mobile_observatory.seed import seed_demonstration

        db = Database.migrated(self.data / "corpus.sqlite")
        self.addCleanup(db.close)
        seed_demonstration(db, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.assertFalse(batch.first_build(db.connection))

    def test_the_skip_is_reported_with_its_reason_and_never_as_recorded(self) -> None:
        result = batch.changeset_result("sqlite3session via ctypes", True,
                                        {"error": "", "bytes": 0})
        self.assertFalse(result["recorded"])
        self.assertTrue(result["first_build"])


if __name__ == "__main__":
    unittest.main()
