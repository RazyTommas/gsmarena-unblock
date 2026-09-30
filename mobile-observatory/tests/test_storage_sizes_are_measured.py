"""Sizes must be MEASURED from dbstat, and the measurement must account for the file.

Two of HANDOFF.md's open items were judged on estimates. An estimate that
happens to be close is indistinguishable from one that is not, so the test that
matters here is not "the numbers look plausible" -- it is that the per-object
bytes add up to the file, which an estimate cannot do and a measurement must.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mobile_observatory import storage
from mobile_observatory.database import Database


class Measured(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls.db = Database.migrated(Path(cls._tmp.name) / "corpus.sqlite")
        connection = cls.db.connection
        # Enough rows that the table needs many pages, so the measurement is over
        # a real b-tree rather than one page where every wrong answer looks right.
        connection.execute("CREATE TABLE bulk(id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("CREATE INDEX bulk_body_idx ON bulk(body)")
        connection.executemany("INSERT INTO bulk(id, body) VALUES (?,?)",
                               [(n, f"build-{n:06d}-" + "x" * 200) for n in range(4000)])
        connection.execute("ANALYZE")

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls._tmp.cleanup()

    def test_dbstat_is_available_here(self):
        self.assertTrue(storage.dbstat_available(self.db.connection),
                        "this build cannot measure sizes; it can only estimate them")

    def test_every_byte_of_the_file_is_accounted_for(self):
        """The instrument check. A partial total looks exactly like a small one."""
        measurement = storage.measure(self.db.connection)
        self.assertTrue(measurement["available"])
        self.assertEqual(
            measurement["file_bytes"],
            measurement["measured_bytes"] + measurement["reclaimable_bytes"]
            + measurement["unaccounted_bytes"])
        self.assertLess(abs(measurement["unaccounted_bytes"]), measurement["page_size"],
                        f"dbstat did not account for the file: {measurement['unaccounted_bytes']} "
                        f"bytes unexplained")

    def test_a_table_and_its_index_are_reported_separately_and_both_are_real(self):
        measurement = storage.measure(self.db.connection)
        objects = {item["name"]: item for item in measurement["objects"]}
        self.assertIn("bulk", objects)
        self.assertIn("bulk_body_idx", objects)
        self.assertEqual("table", objects["bulk"]["kind"])
        self.assertEqual("index", objects["bulk_body_idx"]["kind"])
        self.assertEqual("bulk", objects["bulk_body_idx"]["table"])
        # 4,000 rows of ~220 bytes cannot fit in a handful of pages
        self.assertGreater(objects["bulk"]["pages"], 50)
        self.assertGreater(objects["bulk"]["bytes"], 4000 * 200)

    def test_the_reported_size_tracks_the_data_rather_than_being_a_constant(self):
        """The failure this guards is a 'measurement' that never changes."""
        before = {item["name"]: item["bytes"]
                  for item in storage.measure(self.db.connection)["objects"]}
        self.db.connection.executemany(
            "INSERT INTO bulk(id, body) VALUES (?,?)",
            [(n, f"build-{n:06d}-" + "y" * 200) for n in range(4000, 8000)])
        after = {item["name"]: item["bytes"]
                 for item in storage.measure(self.db.connection)["objects"]}
        self.assertGreater(after["bulk"], before["bulk"])
        self.assertGreater(after["bulk_body_idx"], before["bulk_body_idx"])

    def test_indexes_are_rolled_up_onto_their_table(self):
        measurement = storage.measure(self.db.connection)
        rolled = {item["table"]: item for item in storage.by_table(measurement, limit=50)}
        self.assertIn("bulk", rolled)
        self.assertGreater(rolled["bulk"]["index_bytes"], 0)
        self.assertEqual(rolled["bulk"]["bytes"],
                         rolled["bulk"]["table_bytes"] + rolled["bulk"]["index_bytes"])
        self.assertGreaterEqual(rolled["bulk"]["indexes"], 1)

    def test_vacuum_reclaim_is_the_freelist_and_is_reported_as_a_floor(self):
        connection = self.db.connection
        connection.execute("DELETE FROM bulk WHERE id < 2000")
        measurement = storage.measure(connection)
        self.assertEqual(measurement["reclaimable_bytes"],
                         measurement["freelist_pages"] * measurement["page_size"])
        self.assertGreater(measurement["freelist_pages"], 0,
                           "deleting half a table must leave free pages to reclaim")

    def test_the_top_list_never_presents_itself_as_complete(self):
        measurement = storage.measure(self.db.connection)
        top = storage.largest(measurement, 3)
        self.assertEqual(3, len(top))
        self.assertGreater(len(measurement["objects"]), len(top),
                           "the fixture must have more objects than the truncated list")
        self.assertEqual(sorted((item["bytes"] for item in top), reverse=True),
                         [item["bytes"] for item in top])


class DirectoryBytes(unittest.TestCase):
    def test_the_tree_beside_the_database_is_reported_separately(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ledger" / "raw").mkdir(parents=True)
            (root / "ledger" / "raw" / "a.json").write_bytes(b"x" * 5000)
            (root / "ledger" / "raw" / "b.json").write_bytes(b"y" * 3000)
            (root / "batch.log").write_bytes(b"z" * 100)
            (root / "ledger" / "staging").mkdir()
            (root / "ledger" / "staging" / "big.json").write_bytes(b"s" * 40000)
            sizes = {item["name"]: item for item in storage.directory_sizes(root)}
            self.assertEqual(48000, sizes["ledger"]["bytes"])
            self.assertEqual(3, sizes["ledger"]["files"])
            self.assertEqual(100, sizes["batch.log"]["bytes"])
            self.assertEqual("directory", sizes["ledger"]["kind"])
            self.assertEqual("file", sizes["batch.log"]["kind"])
            # largest first, so "where are the bytes" is answered by reading down
            self.assertEqual("ledger", storage.directory_sizes(root)[0]["name"])

    def test_it_looks_deep_enough_to_name_which_subtree_is_growing(self):
        """A total for `ledger` hides which half of it matters.

        HANDOFF.md's open list names `ledger/raw` as the tree that grows without
        bound; on the live corpus `ledger/staging` beside it is 4.7x larger.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ledger" / "raw").mkdir(parents=True)
            (root / "ledger" / "staging").mkdir()
            (root / "ledger" / "raw" / "a").write_bytes(b"x" * 19000)
            (root / "ledger" / "staging" / "b").write_bytes(b"y" * 90000)
            sizes = {item["name"]: item["bytes"] for item in storage.directory_sizes(root)}
            self.assertEqual(109000, sizes["ledger"])
            self.assertEqual(19000, sizes[str(Path("ledger") / "raw")])
            self.assertEqual(90000, sizes[str(Path("ledger") / "staging")])
            # depth=1 must NOT see them, or the default is doing nothing
            shallow = {item["name"] for item in storage.directory_sizes(root, depth=1)}
            self.assertNotIn(str(Path("ledger") / "staging"), shallow)

    def test_a_missing_directory_reports_nothing_rather_than_guessing(self):
        self.assertEqual([], storage.directory_sizes("/nonexistent/observatory-data"))


if __name__ == "__main__":
    unittest.main()
