"""The corpus must carry query-planner statistics, and the BATCH must be what
puts them there.

The live corpus had 129 indices and no `sqlite_stat1` table at all -- nothing had
ever run ANALYZE -- so every plan was chosen from SQLite's built-in guesses about
index selectivity. It costs 0.1s and measured -56% on /api/v1/releases.

Why this is a test and not a note in a runbook: sqlite_stat1 lives INSIDE the
database file. A hand-run ANALYZE is correct until the next ingest changes the
distribution, and is gone entirely the next time the corpus is rebuilt from
inputs. Only the step that builds the corpus can keep statistics current, so the
assertion is about where the ANALYZE lives, not merely that one ever ran.

run_batch reads 16 real inputs and takes ten minutes, so it is not invoked here.
Two assertions instead: the statement is in run_batch, positioned so a rebuild
cannot skip it; and ANALYZE genuinely populates sqlite_stat1 on a populated
database, so the first assertion is about something that works.
"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory import batch  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402


class TheBatchRefreshesQueryStatisticsTest(unittest.TestCase):
    def test_run_batch_runs_analyze(self) -> None:
        source = inspect.getsource(batch.run_batch)
        self.assertIn('execute("ANALYZE")', source,
                      "run_batch does not ANALYZE, so a rebuilt corpus has no query-planner "
                      "statistics and every plan is guessed")

    def test_analyze_runs_after_the_derivations_and_before_the_checks(self) -> None:
        """Order matters twice over.

        Before check_corpus, so the invariant queries are planned with statistics
        rather than being the one read path that is not. After
        build_current_firmware, so what gets analysed is the corpus this run
        produced rather than the one it started from.
        """
        source = inspect.getsource(batch.run_batch)
        analyze = source.index('execute("ANALYZE")')
        self.assertLess(source.index("build_current_firmware(db)"), analyze,
                        "ANALYZE runs before the projection is rebuilt, so it describes the "
                        "previous generation")
        self.assertLess(analyze, source.index("check_corpus(db.connection)"),
                        "ANALYZE runs after the invariant checks, so those are the one read "
                        "path still planned without statistics")

    def test_analyze_actually_produces_statistics(self) -> None:
        """Otherwise the two assertions above guard a statement that does nothing.

        Asserts the BEFORE state too: a corpus with indices and no sqlite_stat1 is
        exactly the state the live corpus was found in, and a test that only looks
        at the after state cannot tell a working ANALYZE from a table that was
        already there.
        """
        db = Database.migrated()
        self.addCleanup(db.close)
        seed_demonstration(db, ROOT / "fixtures" / "supported_catalog.sample.json")
        c = db.connection
        indices = c.execute("SELECT count(*) FROM sqlite_schema WHERE type='index'").fetchone()[0]
        self.assertGreater(indices, 0, "no indices, so statistics about them mean nothing")
        self.assertIsNone(c.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='sqlite_stat1'").fetchone(),
            "sqlite_stat1 exists before ANALYZE ran; this test cannot tell whether ANALYZE works")
        c.execute("ANALYZE")
        self.assertGreater(c.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0], 0,
                           "ANALYZE left no statistics behind")


if __name__ == "__main__":
    unittest.main()
