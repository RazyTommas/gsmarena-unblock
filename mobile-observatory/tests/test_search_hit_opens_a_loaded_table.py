"""Clicking a search result must leave the table holding the rows the server has.

This drives the REAL path in a REAL browser -- type, wait for the dropdown, click
one hit, and then touch nothing -- because the defect it guards was invisible to
every other kind of test. `/api/v1/search` answered correctly, `/api/v1/devices?q=`
answered correctly, and the page still showed an empty catalogue: the click set
state.route and state.exploreMode and called render(), and render() paints
state.data without ever fetching. Measured against the live corpus before the fix:
the single click made ZERO API calls and rendered ZERO rows while the server held
three, and only a SECOND click, on the Devices tab, fetched anything.

The fixture is deliberately larger than one page. With four devices the bug cannot
reproduce -- the boot's unfiltered first page already contains the match, so the
grid's own client-side filtering happens to leave the right row on screen and a
test would pass against the broken code. 120 filler devices sort ahead of the
target, so the row can only appear if the query actually reached the server. That
is the same shape as the live corpus, where the match sat past row 100 of 865.

Also guards the two things the search box now says about itself (the matching rule
on focus, and the literal-punctuation note in a zero result), because they are
rendered by the same handler and a browser is the only place their visibility is
real rather than asserted.
"""
from __future__ import annotations

import sys
import tempfile
import time
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.access import AccessPolicy  # noqa: E402
from mobile_observatory.server import ObservatoryService, make_handler  # noqa: E402

# One page is 100 rows, so the target has to sit past it to prove the query
# travelled. 120 is a page and a bit, not an arbitrary large number.
FILLERS = 120
TARGET_NAME = "Galaxy S26"
NOW = "2026-01-01T00:00:00Z"


def _browser():
    """Playwright, or a reason to skip -- never an import error at module load.

    tests/test_suite_is_discoverable.py refuses a module that raises on import,
    and an air-gapped box has no Chromium. A skip says "not measured here"; an
    import error would take the whole suite with it.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                                  # pragma: no cover
        raise unittest.SkipTest(f"playwright is not installed: {exc}")
    try:
        manager = sync_playwright().start()
        return manager, manager.chromium.launch()
    except Exception as exc:                                  # pragma: no cover
        raise unittest.SkipTest(f"no usable chromium: {exc}")


class SearchHitOpensALoadedTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.corpus = Database.migrated(check_same_thread=False)
        seed_demonstration(cls.corpus, ROOT / "fixtures" / "supported_catalog.sample.json")
        cls._populate(cls.corpus.connection)
        cls.service = ObservatoryService(
            cls.corpus, Path(cls.temp.name) / "local.sqlite", demonstration=True,
            sample_path=ROOT / "fixtures" / "real_source_sample.json")
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(cls.service, ROOT / "apps" / "web", AccessPolicy(None)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.manager, cls.browser = _browser()

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "browser", None):
            cls.browser.close()
            cls.manager.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.service.local.close()
        cls.corpus.close()
        cls.temp.cleanup()

    @staticmethod
    def _populate(connection) -> None:
        """Enough devices that one page of the catalogue cannot contain the match."""
        variant = connection.execute("SELECT variant_id FROM hardware_models LIMIT 1").fetchone()[0]
        rows = [(f"hm-filler-{i:03d}", "Aaa", f"Filler {i:03d}", f"AAA-{i:03d}") for i in range(FILLERS)]
        rows.append(("hm-target", "Zzz", TARGET_NAME, "ZZZ-S26"))
        for hid, brand, name, code in rows:
            connection.execute(
                "INSERT INTO hardware_models(id,variant_id,model_code,model_code_normalized,"
                "codename,created_at,updated_at) VALUES(?,?,?,?,NULL,?,?)",
                (hid, variant, code, code.lower().replace("-", ""), NOW, NOW))
            connection.execute(
                "INSERT INTO device_catalog_flat(hardware_model_id,manufacturer,brand,family,"
                "variant,model_code,codename,silicon_part_count) VALUES(?,?,?,?,?,?,NULL,0)",
                (hid, brand, brand, "Fixture", name, code))
        connection.commit()

    def page(self):
        page = self.browser.new_page(viewport={"width": 1400, "height": 1000})
        self.calls, self.errors, self.answered = [], [], []
        page.on("request", lambda r: self.calls.append(r.url) if "/api/v1/" in r.url else None)
        page.on("response", lambda r: self.answered.append(r.url) if "/api/v1/" in r.url else None)
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(self.base, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        # networkidle is a quiet-period heuristic and under load it returns while
        # the boot's second phase is still in flight -- which then overwrites
        # state.data with the UNFILTERED tables it asked for before the test typed.
        # The app handles that race now; the test must not depend on it to, or it
        # would be measuring the recovery instead of the thing it names. So wait
        # for the last call of the boot's phase 2 to have been ANSWERED.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not any("identity/history" in u for u in self.answered):
            page.wait_for_timeout(100)
        page.wait_for_timeout(400)
        self.addCleanup(page.close)
        return page

    # -- the defect itself ---------------------------------------------------

    def test_one_click_on_a_search_hit_leaves_the_rows_on_screen(self) -> None:
        page = self.page()
        page.fill("#globalSearch", TARGET_NAME)
        page.wait_for_selector(f".search-results.open .search-hit[data-label='{TARGET_NAME}']", timeout=20000)

        self.calls.clear()
        page.click(f".search-hit[data-label='{TARGET_NAME}']")
        # No second click. Only time, and more of it than the request needs.
        page.wait_for_timeout(2500)

        rows = page.eval_on_selector_all("#app .data-table tbody tr", "els=>els.map(e=>e.innerText)")
        self.assertEqual(
            1, len(self.calls),
            "clicking a search hit must make exactly one request -- its own. "
            f"Zero means the view moved without loading; more than one means the boot "
            f"was still running and this test measured a race, not the fix: {self.calls}")
        self.assertIn("q=Galaxy+S26", self.calls[0])
        self.assertEqual(
            1, len(rows),
            "one device matches this query on the server; the grid shows "
            f"{len(rows)}. Requests made by the click: {self.calls}")
        self.assertIn(TARGET_NAME, rows[0])
        self.assertEqual([], self.errors)

    def test_the_same_rule_covers_the_rail_the_hash_and_enter(self) -> None:
        """The hit was one of five call sites; a fix that repaired only it is the wrong fix."""
        for label, move in (
            ("rail button", lambda page: page.click(".rail button[data-route='explore']")),
            ("#explore hash", lambda page: page.evaluate("location.hash='#explore'")),
        ):
            with self.subTest(path=label):
                page = self.page()
                page.fill("#globalSearch", TARGET_NAME)
                page.wait_for_timeout(900)
                self.calls.clear()
                move(page)
                page.wait_for_timeout(2500)
                rows = page.eval_on_selector_all("#app .data-table tbody tr", "els=>els.length")
                self.assertTrue(self.calls, f"{label} moved the view without loading it")
                self.assertEqual(1, rows, f"{label} left {rows} rows on screen; requests: {self.calls}")

        with self.subTest(path="Enter in the search box"):
            page = self.page()
            page.click(".rail button[data-route='explore']")
            page.wait_for_timeout(1500)
            # Typed, not filled: the dropdown's own footer tells the reader to press
            # Enter to filter the table, and before this there was no keydown handler
            # on the search box at all.
            page.fill("#globalSearch", TARGET_NAME)
            page.wait_for_timeout(900)
            self.calls.clear()
            page.focus("#globalSearch")
            page.keyboard.press("Enter")
            page.wait_for_timeout(2500)
            rows = page.eval_on_selector_all("#app .data-table tbody tr", "els=>els.length")
            self.assertTrue(self.calls, "Enter did nothing, while the dropdown says it filters the table")
            self.assertEqual(1, rows)

    def test_a_match_only_the_server_can_see_is_not_filtered_back_out(self) -> None:
        """The grid used to re-run the query over the row's own values.

        The server matches a device's codename; the row it returns carries no
        codename column, so the client filter discarded rows the server had just
        said match. Measured on the live corpus: 88 devices, e.g. q=lisa_tw_global
        -> 1 device from the server, 0 on screen.
        """
        page = self.page()
        self.corpus.connection.execute(
            "UPDATE device_catalog_flat SET codename='fixture_codename_xyz' WHERE hardware_model_id='hm-target'")
        self.corpus.connection.commit()
        page.click(".rail button[data-route='explore']")
        page.wait_for_timeout(1500)
        page.fill("#globalSearch", "fixture_codename_xyz")
        page.wait_for_timeout(2500)
        rows = page.eval_on_selector_all("#app .data-table tbody tr", "els=>els.map(e=>e.innerText)")
        self.corpus.connection.execute(
            "UPDATE device_catalog_flat SET codename=NULL WHERE hardware_model_id='hm-target'")
        self.corpus.connection.commit()
        self.assertEqual(1, len(rows), "the server matches on codename; the grid must not drop the row")
        self.assertIn(TARGET_NAME, rows[0])

    # -- what the search box says about itself -------------------------------

    def test_the_matching_rule_is_reachable_but_never_persistent(self) -> None:
        page = self.page()
        visible = lambda: page.eval_on_selector(
            "#searchRule", "e=>getComputedStyle(e).display!=='none' && e.getBoundingClientRect().height>0")
        self.assertFalse(visible(), "the rule must not be sitting in the chrome before it is asked for")
        self.assertIn("no wildcards", page.eval_on_selector("#globalSearch", "e=>e.title"),
                      "hover has no keyboard route, so title= alone is not enough -- but it is still required")
        self.assertEqual("searchRule", page.eval_on_selector("#globalSearch", "e=>e.getAttribute('aria-describedby')"))
        page.keyboard.press("/")            # the keyboard route, no mouse involved
        page.wait_for_timeout(300)
        self.assertTrue(visible(), "focus must reveal it, or keyboard users cannot reach it at all")
        text = page.eval_on_selector("#searchRule", "e=>e.textContent")
        for claim in ("Case-insensitive", "anywhere in the name", "no wildcards"):
            self.assertIn(claim, text)
        page.evaluate("document.querySelector('#globalSearch').blur()")
        page.wait_for_timeout(300)
        self.assertFalse(visible(), "it must leave on blur; nothing persistent is added to the chrome")

    def test_a_zero_result_says_when_the_punctuation_was_taken_literally(self) -> None:
        page = self.page()
        page.fill("#globalSearch", "s26*")
        page.wait_for_selector(".search-results.open", timeout=20000)
        page.wait_for_timeout(700)
        self.assertEqual(0, page.locator(".search-hit").count(), "precondition: s26* matches nothing")
        note = page.eval_on_selector(".search-results .empty", "e=>e.innerText")
        self.assertIn("searched for literally", note)
        self.assertIn("no wildcards", note)
        self.assertFalse(
            page.eval_on_selector("#searchRule", "e=>getComputedStyle(e).display!=='none'"),
            "the focus hint must yield to the dropdown; they occupy the same space")

        page.click(".rail button[data-route='explore']")
        page.wait_for_timeout(2000)
        table = page.eval_on_selector("#app .empty", "e=>e.innerText")
        self.assertIn("searched for literally", table)

        # and a zero result with no such character says nothing extra.
        page.fill("#globalSearch", "zzqqxxnothing")
        page.wait_for_timeout(2000)
        plain = page.eval_on_selector("#app .empty", "e=>e.innerText")
        self.assertNotIn("literally", plain)
        self.assertNotIn("wildcard", plain)


if __name__ == "__main__":
    unittest.main()
