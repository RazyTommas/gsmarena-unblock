"""Cost is a property. Assert it, or it regresses silently.

The suite had no assertion about how much work a request does. Two consequences
were measured on this codebase:

* An N+1 could be reintroduced in releases_page and stay green. The existing
  BatchedDateEvidenceTest compares firmware_date_evidence_many against
  firmware_date_evidence directly, so restoring the per-row call at the CALLER
  leaves it passing. Query count went 4 -> 202 at limit=100 and 4 -> 1002 at
  limit=500, while wall time barely moved on a 3-release fixture. Only a counter
  sees that.

* A whole-table window or aggregate inside a paginated query is invisible at
  fixture scale and dominates at real scale. devices_page had two, and they cost
  1,549ms on a corpus scaled to 53,436 devices while costing nothing measurable
  on the 4-device fixture.

Both are asserted here without a stopwatch, because a wall-clock threshold on a
shared machine is a flaky test that gets deleted. Query count and query plan are
deterministic.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory import current_firmware as cf  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService  # noqa: E402


class QueryCounter:
    """Counts statements executed on a connection while the block runs."""

    def __init__(self, connection) -> None:
        self.connection = connection
        self.statements: list[str] = []

    def __enter__(self) -> "QueryCounter":
        self.connection.set_trace_callback(self.statements.append)
        return self

    def __exit__(self, *exc) -> None:
        self.connection.set_trace_callback(None)

    def __len__(self) -> int:
        return len(self.statements)


PAGE_SMALL, PAGE_LARGE = 50, 500
# More rows than the small page, so the two page sizes actually return
# different numbers of rows. The shipped fixture holds 4 devices and 3 firmware
# releases; against it, limit=50 and limit=500 return the same 3 rows and an
# N+1 adds the same 3 queries to both. Verified: with the N+1 planted in
# releases_page, this test PASSED on the bare fixture. A test whose fixture is
# smaller than its page size cannot see a per-row cost at all -- which is the
# same blindness that let the page-one search ship.
FIXTURE_ROWS = 140


def _inflate(db) -> None:
    """Clone the seeded devices and releases past the page size."""
    con = db.connection
    variants = con.execute("SELECT id,family_id,canonical_name,form_factor,announced_on,"
                           "created_at,updated_at FROM device_variants").fetchall()
    models = con.execute("SELECT id,variant_id,model_code,model_code_normalized,codename,"
                         "created_at,updated_at FROM hardware_models").fetchall()
    releases = con.execute("SELECT * FROM firmware_releases").fetchall()
    columns = [d[0] for d in con.execute("SELECT * FROM firmware_releases LIMIT 0").description]
    copies = -(-FIXTURE_ROWS // max(len(models), 1))
    for index in range(1, copies + 1):
        suffix = f"-q{index}"
        for row in variants:
            con.execute("INSERT INTO device_variants VALUES(?,?,?,?,?,?,?)",
                        (row[0] + suffix, row[1], row[2] + suffix, row[3], row[4], row[5], row[6]))
        for row in models:
            con.execute("INSERT INTO hardware_models VALUES(?,?,?,?,?,?,?)",
                        (row[0] + suffix, row[1] + suffix, row[2] + suffix,
                         row[3] + suffix, row[4], row[5], row[6]))
        for row in releases:
            values = dict(zip(columns, row))
            values["id"] = values["id"] + suffix
            values["hardware_model_id"] = values["hardware_model_id"] + suffix
            con.execute(f"INSERT INTO firmware_releases({','.join(columns)}) "
                        f"VALUES({','.join('?' * len(columns))})",
                        [values[name] for name in columns])
    cf.build(db)


class QueryCountDoesNotGrowWithPageSizeTest(unittest.TestCase):
    """The N+1 detector. A page of 500 must not cost 10x a page of 50."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")
        _inflate(self.db)
        self.service = ObservatoryService(
            self.db, Path(self.temp.name) / "local.sqlite", demonstration=True)
        self.addCleanup(self.service.local.close)

    def test_the_fixture_is_larger_than_the_page(self) -> None:
        """Without this the test above is vacuous and would not say so."""
        rows = self.db.connection.execute("SELECT count(*) FROM firmware_releases").fetchone()[0]
        self.assertGreater(rows, PAGE_SMALL,
                           "fixture must exceed the small page size or no per-row cost is visible")

    def _count(self, method, limit: int) -> int:
        with QueryCounter(self.db.connection) as counter:
            method({"limit": [str(limit)]})
        return len(counter)

    def test_paged_endpoints_cost_the_same_at_50_and_500_rows(self) -> None:
        # A few extra statements are fine -- a count query, a pragma. An N+1 is
        # not a few: it is one per row, so the gap scales with the page.
        allowance = 5
        offenders = []
        for name in ("devices_page", "releases_page", "product_releases_page",
                     "source_records_page", "security_page", "chips_page",
                     "source_products_page", "updates_page"):
            method = getattr(self.service, name)
            small = self._count(method, PAGE_SMALL)
            large = self._count(method, PAGE_LARGE)
            if large > small + allowance:
                offenders.append(f"{name}: {small} queries at limit={PAGE_SMALL}, {large} at limit={PAGE_LARGE}")
        self.assertEqual([], offenders,
                         "query count grows with page size, which is an N+1:\n  "
                         + "\n  ".join(offenders))


class HealthEndpointCostTest(unittest.TestCase):
    """The health endpoint must not run whole-database page scans."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.service = ObservatoryService(
            self.db, Path(self.temp.name) / "local.sqlite", demonstration=True)
        self.addCleanup(self.service.local.close)

    def test_health_does_not_run_the_whole_database_scans(self) -> None:
        """PRAGMA integrity_check is 1,233ms on the live corpus.

        Wiring the full invariant set into /api/v1/admin/health -- which the UI
        calls on every load -- took that endpoint from 57ms to 2,066ms. What
        those two pragmas detect is disk corruption, which does not appear
        between two page loads.
        """
        with QueryCounter(self.db.connection) as counter:
            self.service.integrity()
        ran = " ".join(counter.statements).lower()
        self.assertNotIn("integrity_check", ran)
        self.assertNotIn("foreign_key_check", ran)

    def test_the_batch_path_still_runs_them(self) -> None:
        """Gating them on the endpoint must not quietly stop them running at all."""
        from mobile_observatory.integrity import check_corpus
        with QueryCounter(self.db.connection) as counter:
            check_corpus(self.db.connection)
        ran = " ".join(counter.statements).lower()
        self.assertIn("integrity_check", ran)
        self.assertIn("foreign_key_check", ran)


class DeviceGridPlanTest(unittest.TestCase):
    """The device grid must seek the projection, never scan or window all of it.

    This EXPLAINs the statement devices_page actually executes, captured from
    the connection, rather than a copy of it written here. A plan test against a
    hand-written query is a test of the test: the real query can drift into a
    full scan while the transcribed one still plans perfectly.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        seed_demonstration(self.db, ROOT / "fixtures" / "supported_catalog.sample.json")
        _inflate(self.db)
        self.service = ObservatoryService(
            self.db, Path(self.temp.name) / "local.sqlite", demonstration=True)
        self.addCleanup(self.service.local.close)

    def _grid_plan(self) -> tuple[str, str]:
        with QueryCounter(self.db.connection) as counter:
            self.service.devices_page({"limit": ["100"]})
        # Selected by the grid's own join, not by being the longest statement that
        # mentions the projection. "Longest" worked only while the grid was the
        # ONLY query on this page to touch device_current_firmware; when
        # _firmware_holdings was added -- it tests EXISTS against the same table
        # and carries one placeholder per row on the page -- the heuristic started
        # picking that instead, and this test failed while describing a plan it
        # was not about. A discriminator that names what it wants cannot drift
        # that way.
        candidates = [s for s in counter.statements
                      if "LEFT JOIN device_current_firmware lf" in s
                      and s.lstrip().upper().startswith("SELECT")]
        self.assertTrue(candidates, "devices_page never ran its grid query against the "
                                    "projection; the join alias may have been renamed")
        # Two remain: the row-returning query and the bare count over the same
        # FROM clause. The rows one is the long one.
        statement = max(candidates, key=len)
        plan = "\n".join(row[-1] for row in
                         self.db.connection.execute("EXPLAIN QUERY PLAN " + statement))
        return statement, plan

    def test_the_primary_row_is_found_by_index(self) -> None:
        _, plan = self._grid_plan()
        self.assertIn("device_current_firmware_primary_idx", plan,
                      f"the grid must seek the precomputed row, not scan the projection:\n{plan}")

    def test_the_grid_does_not_window_the_whole_projection(self) -> None:
        """Guards the specific 1,549ms shape that was removed.

        A CO-ROUTINE or a materialised subquery over device_current_firmware is
        the window/aggregate-per-request pattern coming back.
        """
        _, plan = self._grid_plan()
        self.assertNotIn("SCAN device_current_firmware", plan,
                         f"projection is being scanned per request:\n{plan}")
        # Scoped to the projection deliberately. The plan does contain a
        # materialised subquery and an AUTOMATIC COVERING INDEX -- both are
        # v_chip_devices, a 67-row view, and a separate (small) cost. Asserting
        # on the whole plan would make this test fail for a reason it is not
        # about, and it would be silenced rather than fixed.
        self.assertNotIn("MATERIALIZE device_current_firmware", plan,
                         f"a subquery over the projection is materialised per request:\n{plan}")


if __name__ == "__main__":
    unittest.main()
