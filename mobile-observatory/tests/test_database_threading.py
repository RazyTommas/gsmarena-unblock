"""The corpus is read by many request threads at once; prove that is safe.

ThreadingHTTPServer serves every connection on its own thread, and the service
reads `self.corpus.connection` from ~60 places. When that was one connection
opened with check_same_thread=False and no lock, two threads interleaving on it
corrupted its statement state and SQLite raised

    InterfaceError: bad parameter or other API misuse

from whichever query happened to be running -- blaming a query that was correct.
It reached a user as a crashed global search against a real corpus.

The bug needed concurrency to appear, so the whole existing suite passed while it
shipped. These tests supply the concurrency.

They were written as bare module-level functions taking pytest's `tmp_path`
fixture, which `python -m unittest discover` -- the runner README.md documents,
and the only one present on an air-gapped box -- does not collect. Seven tests
guarding a race that already reached a user ran under pytest and nowhere else.
They are a TestCase with its own temporary directory now, so both runners see
them.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory.database import Database  # noqa: E402


def _schema(db: Database) -> None:
    db.connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.connection.executemany(
        "INSERT INTO t (id, v) VALUES (?, ?)", [(i, f"row-{i}") for i in range(400)]
    )


def _hammer(db: Database, threads: int = 12, rounds: int = 25) -> list[BaseException]:
    """Read from `db` on `threads` threads at once; collect what goes wrong.

    Iterates the cursor rather than calling fetchall(), because iteration is when
    a shared connection actually breaks: rows are fetched lazily as the loop runs,
    so any lock that is released when execute() returns leaves the fetching
    unprotected. A test that only called fetchall() could pass against a fix that
    does not hold for the code the server really runs.
    """
    errors: list[BaseException] = []
    guard = threading.Lock()
    start = threading.Barrier(threads)

    def work() -> None:
        try:
            start.wait(timeout=30)
            for _ in range(rounds):
                seen = 0
                for row in db.connection.execute("SELECT id, v FROM t ORDER BY id"):
                    assert row["v"] == f"row-{row['id']}"
                    seen += 1
                assert seen == 400, f"partial read: {seen} of 400 rows"
        except BaseException as exc:                      # noqa: BLE001
            with guard:
                errors.append(exc)

    workers = [threading.Thread(target=work) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    return errors


class DatabaseThreadingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp_path = Path(self._temp.name)

    def test_concurrent_reads_on_a_file_corpus_do_not_raise(self) -> None:
        """The regression: this is what the crashed search endpoint was doing."""
        db = Database.migrated(self.tmp_path / "corpus.sqlite", check_same_thread=False)
        self.addCleanup(db.close)
        _schema(db)
        errors = _hammer(db)
        self.assertEqual([], errors, f"concurrent reads failed: {errors[:3]}")

    def test_each_thread_gets_its_own_connection(self) -> None:
        """The mechanism behind the fix, asserted directly.

        Without this, the test above could pass by luck on a quiet machine.
        """
        db = Database.migrated(self.tmp_path / "corpus.sqlite", check_same_thread=False)
        self.addCleanup(db.close)
        seen: dict[str, int] = {}
        guard = threading.Lock()

        def record(name: str) -> None:
            with guard:
                seen[name] = id(db.connection)

        threads = [threading.Thread(target=record, args=(f"t{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(4, len(set(seen.values())), f"threads shared a connection: {seen}")

    def test_every_connection_carries_the_read_settings(self) -> None:
        """cache_size and mmap_size are PER-CONNECTION state.

        This server hands each request thread its own connection, so settings
        applied anywhere other than _connect reach the first connection and no
        other -- and the ones that matter here are exactly the ones a thread uses:
        the corpus reads that made /api/v1/admin/health cost 773-892ms all ran on
        a per-thread handle. Asserted on a connection opened BY A THREAD, not on
        the one the constructor made, because those are the two different code
        paths and only one of them was ever exercised by hand.
        """
        db = Database.migrated(self.tmp_path / "corpus.sqlite", check_same_thread=False)
        self.addCleanup(db.close)
        wanted = {"cache_size": -16384, "mmap_size": 268435456}
        seen: dict[str, dict[str, int]] = {}
        guard = threading.Lock()

        def record(name: str) -> None:
            values = {p: db.connection.execute(f"PRAGMA {p}").fetchone()[0] for p in wanted}
            with guard:
                seen[name] = values

        threads = [threading.Thread(target=record, args=(f"t{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(4, len(seen), "a recording thread did not run")
        for name, values in seen.items():
            self.assertEqual(wanted["cache_size"], values["cache_size"],
                             f"thread {name} got SQLite's default page cache, not the "
                             f"corpus-sized one")
            # mmap_size reads back as the size actually mapped, which the build caps
            # at SQLITE_MAX_MMAP_SIZE. Zero is the thing being guarded against: it
            # means no mapping at all, which is what shipped.
            self.assertGreater(values["mmap_size"], 0,
                               f"thread {name} has no memory mapping; every page read "
                               f"is a syscall")

    def test_one_thread_keeps_the_same_connection(self) -> None:
        """Per-thread must not mean per-access.

        transaction() runs BEGIN, yields, then COMMIT through separate reads of the
        property; a fresh connection per access would commit a transaction that was
        never begun on it, and leave the real one open.
        """
        db = Database.migrated(self.tmp_path / "corpus.sqlite", check_same_thread=False)
        self.addCleanup(db.close)
        self.assertIs(db.connection, db.connection)

        _schema(db)
        with db.transaction() as connection:
            connection.execute("INSERT INTO t (id, v) VALUES (9001, 'in-transaction')")
        self.assertEqual(
            "in-transaction",
            db.connection.execute("SELECT v FROM t WHERE id=9001").fetchone()["v"])

    def test_memory_database_shares_one_connection(self) -> None:
        """An in-memory database must NOT go per-thread, even when asked to thread.

        Each connection to ":memory:" is a separate empty database, not another handle
        on the same one, so this is the one case that keeps sharing. Going thread-local
        here would fail far worse than the race it replaced: every query would succeed
        against an empty schema and quietly measure nothing.

        check_same_thread=False is the point of the test -- it is the request that
        would trigger the per-thread path for a file, and must not for :memory:. The
        default (guard on) is deliberately not tested cross-thread, because there the
        right answer IS to refuse.
        """
        db = Database.migrated(":memory:", check_same_thread=False)
        self.addCleanup(db.close)
        _schema(db)
        found: list[int] = []
        guard = threading.Lock()

        def count() -> None:
            with guard:
                found.append(db.connection.execute("SELECT count(*) FROM t").fetchone()[0])

        threads = [threading.Thread(target=count) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([400] * 4, found, f"in-memory rows not visible across threads: {found}")

    def test_release_thread_frees_the_handle_and_the_next_use_still_works(self) -> None:
        """Per-request release is what keeps open handles bounded.

        Relying on the thread's locals being collected was measured at 145 open corpus
        handles behind ONE live thread after 144 requests -- the crash traded for a
        slower one. Releasing must also leave the database usable: the same thread
        serves the next request.
        """
        db = Database.migrated(self.tmp_path / "corpus.sqlite", check_same_thread=False)
        self.addCleanup(db.close)
        _schema(db)
        first = db.connection
        self.assertEqual(400, first.execute("SELECT count(*) FROM t").fetchone()[0])

        db.release_thread()
        with self.assertRaises(sqlite3.ProgrammingError,
                               msg="release_thread() did not close the connection"):
            first.execute("SELECT 1")

        self.assertIsNot(db.connection, first)
        self.assertEqual(400, db.connection.execute("SELECT count(*) FROM t").fetchone()[0])

    def test_release_thread_is_a_noop_for_a_shared_connection(self) -> None:
        """A shared connection is not the calling thread's to close.

        If release_thread() closed it, the first request to an in-memory or
        single-threaded corpus would tear down the database for everyone after it.
        """
        db = Database.migrated(":memory:")
        self.addCleanup(db.close)
        _schema(db)
        db.release_thread()
        self.assertEqual(400, db.connection.execute("SELECT count(*) FROM t").fetchone()[0])

    def test_use_after_close_raises_instead_of_reopening(self) -> None:
        """Closing must not silently hand back a new, empty database.

        The property has an open-a-new-one path; close() has to be visible to it, or
        a closed corpus reopens on next access and reads as an empty one.
        """
        db = Database.migrated(self.tmp_path / "corpus.sqlite", check_same_thread=False)
        _schema(db)
        db.close()
        with self.assertRaises(sqlite3.ProgrammingError,
                               msg="closed Database reopened instead of raising"):
            db.connection.execute("SELECT count(*) FROM t")


if __name__ == "__main__":
    unittest.main()
