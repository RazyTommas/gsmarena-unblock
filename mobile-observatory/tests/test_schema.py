from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402


class SchemaTests(unittest.TestCase):
    def test_migration_and_foreign_keys(self) -> None:
        db = Database.migrated()
        try:
            self.assertEqual(db.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                db.connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0],
            max(int(path.name.split("_", 1)[0]) for path in (ROOT / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")),
            )
            problems = db.connection.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(problems, [])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
