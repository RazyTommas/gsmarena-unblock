from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Database:
    """SQLite lifecycle with mandatory integrity settings."""

    def __init__(self, path: str | Path = ":memory:", *, check_same_thread: bool = True) -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=check_same_thread
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")

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
        self.connection.close()
