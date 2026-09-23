from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Database:
    """SQLite lifecycle with mandatory integrity settings.

    A sqlite3 connection is NOT safe to drive from two threads at once. Passing
    check_same_thread=False only removes Python's guard against it; it does not
    make the sharing safe. Two threads interleaving on one connection corrupt its
    statement state, which surfaces as `InterfaceError: bad parameter or other API
    misuse` raised from a query that is perfectly valid on its own -- a confusing
    failure precisely because the query it blames is innocent.

    So check_same_thread=False here means "hand each thread its own connection",
    not "share one unguarded connection". Under WAL that is the cheap option:
    concurrent readers do not block each other, so per-thread connections need no
    lock and serialise nothing.

    The exception is an in-memory database, where each connection would be a
    SEPARATE EMPTY database rather than another handle on the same one. Those keep
    a single shared connection. That is safe for their actual use -- tests and
    one-shot tooling, both single-threaded -- and a thread-local :memory: would
    fail far worse than the race it replaced: every query would succeed against an
    empty schema and quietly measure nothing.
    """

    def __init__(self, path: str | Path = ":memory:", *, check_same_thread: bool = True) -> None:
        self.path = str(path)
        self._check_same_thread = check_same_thread
        self._shared_only = check_same_thread or self.path == ":memory:" or not self.path
        self._local = threading.local()
        self._closed = False
        self._shared = self._connect() if self._shared_only else None

    def _connect(self) -> sqlite3.Connection:
        """Open one connection with the settings every connection must carry.

        Called once per thread when threading is enabled, so the PRAGMAs live here
        rather than in __init__ -- they are per-connection state, and a connection
        opened without them is not the same database contract.
        """
        # A per-thread connection keeps the guard ON: it belongs to exactly one
        # thread, so a tripped guard is a real bug worth hearing about. Only the
        # shared connection honours the caller's flag, and the one case that still
        # relies on sharing across threads is an in-memory database, which cannot
        # be made thread-local at all.
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=self._check_same_thread if self._shared_only else True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        # Deliberately NOT recorded in a list for close() to walk. A thread serving
        # one HTTP request is one thread, and a registry of every connection ever
        # opened is a strong reference that outlives the thread that owns it: the
        # thread dies, its thread-local storage is cleared, and the connection still
        # cannot be collected because the registry holds it. That leaks one file
        # descriptor per request -- measured at 719 open corpus handles behind a
        # single live thread, which is a slower version of the crash this change
        # exists to remove. sqlite3.Connection cannot be weak-referenced, so the
        # only way not to pin it is not to hold it: with no registry, the last
        # reference dies with the thread and CPython closes the handle there.
        return connection

    @property
    def connection(self) -> sqlite3.Connection:
        """This thread's connection. Same object every time within a thread, so a
        cursor stays valid while it is being iterated and transaction() still spans
        the statements a caller runs inside it."""
        if self._closed:
            # Checked before the open-a-new-one path below, which would otherwise
            # resurrect a closed database instead of reporting the mistake.
            raise sqlite3.ProgrammingError(f"Database({self.path!r}) is closed")
        if self._shared is not None:
            return self._shared
        existing = getattr(self._local, "connection", None)
        if existing is None:
            existing = self._connect()
            self._local.connection = existing
        return existing

    def release_thread(self) -> None:
        """Close and forget this thread's connection, if it owns one.

        Called when a unit of work that got its own thread is finished with the
        database -- one HTTP request, in this server. Without it the connection
        lives until the thread object and its locals are collected, which is not
        prompt: measured at 145 open corpus handles behind a single live thread
        after 144 requests, freed only whenever a GC pass happened to run. Closing
        here caps open handles at the number of requests actually in flight.

        A no-op for a shared connection, which is not this thread's to close.
        """
        if self._shared is not None:
            return
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            self._local.connection = None
            connection.close()

    @classmethod
    def migrated(
        cls, path: str | Path = ":memory:", *, check_same_thread: bool = True
    ) -> "Database":
        db = cls(path, check_same_thread=check_same_thread)
        db.apply_migrations()
        return db

    def apply_migrations(self) -> None:
        """Apply every numbered migration not already recorded.

        Corpus databases are durable artifacts, so opening an existing corpus must
        not leave its read-model contract behind the application version.
        """
        root = Path(__file__).resolve().parents[2] / "migrations"
        applied: set[int] = set()
        if self.connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_migrations'"
        ).fetchone():
            applied = {int(row[0]) for row in self.connection.execute("SELECT version FROM schema_migrations")}
        for migration in sorted(root.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = int(migration.name.split("_", 1)[0])
            if version not in applied:
                self.connection.executescript(migration.read_text(encoding="utf-8"))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """Close the shared connection, or this thread's own.

        Other threads' connections are not reachable from here and must not be:
        sqlite3 refuses close() from a thread that does not own the connection, and
        holding them somewhere closable is exactly the leak described in _connect.
        Each is closed when its own thread ends, and any still live at interpreter
        exit are closed by the process teardown.
        """
        # Deliberately does NOT drop the shared handle or reset the thread-locals.
        # Clearing them would send the `connection` property down its open-a-new-one
        # path and silently REOPEN a database the caller just closed -- for :memory:
        # a fresh empty schema in which every later query succeeds against nothing.
        # The _closed flag makes use-after-close raise instead.
        self._closed = True
        mine = self._shared if self._shared is not None else getattr(self._local, "connection", None)
        if mine is not None:
            mine.close()
