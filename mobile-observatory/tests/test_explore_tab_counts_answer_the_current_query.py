"""An Explore tab must not state a total for a query nobody asked.

THE DEFECT. Only the ACTIVE tab is re-fetched when a reader filters, so after
typing `S26` the strip read

    Devices (3) | Silicon (452) | Canonical ROMs (21,186) | Source records (...)

-- one right number and two wrong ones, in identical type, with nothing to tell
a reader which was which. `Silicon (452)` is the total for a query that is no
longer on screen.

THE FIX IS NOT TO FETCH ALL FOUR. That is 4x the requests per keystroke for
three numbers the reader is not looking at; the releases query alone was 53ms
before the trigram index. Instead each page records the SCOPE it was fetched
under, and a count is shown only while that scope is the one in force.

WHY NO NUMBER RATHER THAN A NUMBER LABELLED "unfiltered": the stored total is
not the unfiltered total, it is the total for whatever query was last sent to
THAT tab. Refine `S2` to `S26` and Silicon holds the total for `S2`; calling it
"unfiltered" states something false. Omitting it cannot state a wrong number.

Driven in a REAL browser, because the thing under test is what a reader sees on
the chrome, and the previous round's lesson in this file's neighbours is that
every serious defect here was found by executing the path and none by reading
it. The fixture is 121 devices for the same reason
test_search_hit_opens_a_loaded_table.py uses 121: with four, the boot's first
page already holds the match and a broken build passes.
"""
from __future__ import annotations

import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.access import AccessPolicy  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService, make_handler  # noqa: E402

FILLERS = 120
TARGET_NAME = "Galaxy S26"
NOW = "2026-01-01T00:00:00Z"


def _browser():
    """Playwright, or a skip -- never an import error at module load.

    tests/test_suite_is_discoverable.py refuses a module that raises on import,
    and an air-gapped box has no Chromium. A skip says "not measured here".
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


class ExploreTabCounts(unittest.TestCase):
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
            ("127.0.0.1", 0),
            make_handler(cls.service, ROOT / "apps" / "web", AccessPolicy(None)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        # Measured, not FILLERS+1: the demonstration fixture seeds devices of its
        # own, so the catalogue is larger than what _populate added. A test that
        # asserts a number it derived by arithmetic over a fixture it did not
        # count is asserting its own guess.
        cls.devices_total = cls.corpus.connection.execute(
            "SELECT count(*) FROM device_catalog_flat").fetchone()[0]
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
        variant = connection.execute(
            "SELECT variant_id FROM hardware_models LIMIT 1").fetchone()[0]
        rows = [(f"hm-filler-{i:03d}", "Aaa", f"Filler {i:03d}", f"AAA-{i:03d}")
                for i in range(FILLERS)]
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

    # -- harness -----------------------------------------------------------

    def page(self):
        page = self.browser.new_page(viewport={"width": 1500, "height": 1000})
        self.calls, self.answered, self.errors = [], [], []
        page.on("request",
                lambda r: self.calls.append(r.url) if "/api/v1/" in r.url else None)
        page.on("response",
                lambda r: self.answered.append(r.url) if "/api/v1/" in r.url else None)
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(self.base, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        # networkidle is a quiet-period heuristic and under load it returns while
        # the boot's phase 2 is still in flight -- and phase 2 is what STAMPS the
        # three unfiltered scopes onto the three totals. Opening Explore before
        # it lands would measure the boot, not the rule. Same wait as
        # test_search_hit_opens_a_loaded_table.py, for the same reason: the last
        # call of phase 2 must have been ANSWERED.
        deadline = time.monotonic() + 60
        while (time.monotonic() < deadline
               and not any("identity/history" in u for u in self.answered)):
            page.wait_for_timeout(100)
        page.wait_for_timeout(400)
        page.click('[data-route="explore"]')
        page.wait_for_selector("#exploreTabs button", timeout=30000)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(200)
        self.addCleanup(page.close)
        return page

    def tabs(self, page) -> dict:
        """{label-without-count: full-button-text} for the three canonical tabs."""
        out = {}
        for button in page.query_selector_all("#exploreTabs button"):
            text = (button.inner_text() or "").strip()
            key = button.get_attribute("data-value")
            out[key] = text
        return out

    def count_in(self, text):
        """The number in a tab label, or None when there is none."""
        found = re.search(r"\(([\d,]+)\)\s*$", text)
        return int(found.group(1).replace(",", "")) if found else None

    def filter_for(self, page, text):
        page.fill("#globalSearch", text)
        page.dispatch_event("#globalSearch", "input")
        page.wait_for_timeout(400)          # the box debounces at 180ms
        page.wait_for_load_state("networkidle")

    # -- with no filter, every count is honest and every count is shown ----

    def test_unfiltered_all_three_tabs_carry_their_total(self) -> None:
        page = self.page()
        tabs = self.tabs(page)
        self.assertEqual(self.devices_total, self.count_in(tabs["devices"]),
                         "the devices total must still be there when nothing is filtered")
        for kind in ("silicon", "releases"):
            self.assertIsNotNone(self.count_in(tabs[kind]),
                                 f"{kind} lost its count with no filter in force; the fix "
                                 f"must not simply delete the numbers")
        self.assertEqual([], self.errors)

    # -- the defect itself -------------------------------------------------

    def test_a_filtered_load_leaves_no_stale_number_on_the_other_tabs(self) -> None:
        page = self.page()
        before = self.tabs(page)
        unfiltered_silicon = self.count_in(before["silicon"])
        self.assertIsNotNone(unfiltered_silicon)

        self.filter_for(page, "S26")
        after = self.tabs(page)
        self.assertEqual(1, self.count_in(after["devices"]),
                         "the active tab must show the query's own total")
        for kind in ("silicon", "releases"):
            with self.subTest(tab=kind):
                self.assertIsNone(
                    self.count_in(after[kind]),
                    f"{kind} is still stating a total: {after[kind]!r}. That number "
                    f"answers the previous query, not this one.")
                self.assertNotRegex(after[kind], r"\d",
                                    "no digit at all, so there is nothing to misread")

    def test_the_hidden_count_says_why_it_is_hidden(self) -> None:
        """A number that vanishes with no explanation reads as zero. The title
        has to be on the button, not in a release note."""
        page = self.page()
        self.filter_for(page, "S26")
        for kind in ("silicon", "releases"):
            title = page.get_attribute(f'#exploreTabs button[data-value="{kind}"]', "title")
            self.assertTrue(title, f"{kind} hides its count and explains nothing")
            self.assertIn("filter", title.lower())
        self.assertFalse(
            page.get_attribute('#exploreTabs button[data-value="devices"]', "title"),
            "the tab whose number IS current must not carry a caveat about it")

    def test_filtering_does_not_fetch_the_tabs_nobody_opened(self) -> None:
        """The cheap wrong fix. Four requests per keystroke for three numbers
        the reader is not looking at."""
        page = self.page()
        self.calls.clear()
        self.filter_for(page, "S26")
        chips = [c for c in self.calls if "/api/v1/chips" in c]
        releases = [c for c in self.calls if "/api/v1/releases" in c]
        self.assertEqual([], chips, f"filtering fetched the silicon tab: {chips}")
        self.assertEqual([], releases, f"filtering fetched the releases tab: {releases}")
        self.assertTrue([c for c in self.calls if "/api/v1/devices" in c],
                        "the ACTIVE tab must still be re-fetched")

    def test_opening_the_tab_counts_it_for_the_filter_in_force(self) -> None:
        """The count is not gone, it is deferred to the act that pays for it."""
        page = self.page()
        self.filter_for(page, "S26")
        self.assertIsNone(self.count_in(self.tabs(page)["silicon"]))
        page.click('#exploreTabs button[data-value="silicon"]')
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(200)
        tabs = self.tabs(page)
        self.assertIsNotNone(self.count_in(tabs["silicon"]),
                             "opening the tab fetched its total and it must now show")
        # Devices keeps its count, and that is correct rather than a leftover:
        # the filter did not change, so the total it was fetched under is still
        # the total being asked for. Hiding it here would be the opposite error
        # -- withholding a number that is true.
        self.assertEqual(1, self.count_in(tabs["devices"]))
        self.assertIsNone(self.count_in(tabs["releases"]),
                          "the tab nobody opened is still uncounted")

    def test_clearing_the_filter_brings_the_boot_totals_back(self) -> None:
        """They were correct for the unfiltered query all along; the rule has to
        recognise that rather than hiding them forever."""
        page = self.page()
        self.filter_for(page, "S26")
        self.assertIsNone(self.count_in(self.tabs(page)["silicon"]))
        self.filter_for(page, "")
        tabs = self.tabs(page)
        self.assertEqual(self.devices_total, self.count_in(tabs["devices"]))
        self.assertIsNotNone(self.count_in(tabs["silicon"]))
        self.assertIsNotNone(self.count_in(tabs["releases"]))

    def test_a_filter_that_does_not_apply_to_a_tab_does_not_hide_its_count(self) -> None:
        """The scope is per TAB, not one global "is anything filtered" flag.

        `maker` is sent for devices and releases and is NOT sent for silicon --
        the silicon total genuinely does not depend on it. A single global flag
        would hide the silicon count here, which is a true number withheld for
        no reason, and would teach a reader that the numbers come and go at
        random.
        """
        page = self.page()
        # "Aaa", not the one-device "Zzz": the dropdown's options come from the
        # first PAGE of devices, and 120 fillers named Aaa sort ahead of it. A
        # test selecting an option the control does not offer fails on the
        # control, not on the behaviour.
        page.select_option("#makerFilter", "Aaa")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(200)
        tabs = self.tabs(page)
        self.assertEqual(FILLERS, self.count_in(tabs["devices"]))
        self.assertIsNotNone(self.count_in(tabs["silicon"]),
                             "the manufacturer filter is not part of the silicon query, "
                             "so the silicon total is still the answer")
        self.assertIsNone(self.count_in(tabs["releases"]),
                          "...and it IS part of the releases query, so that one is stale")

    def test_no_page_errors_through_any_of_it(self) -> None:
        page = self.page()
        self.filter_for(page, "S26")
        page.click('#exploreTabs button[data-value="silicon"]')
        page.wait_for_load_state("networkidle")
        self.filter_for(page, "")
        self.assertEqual([], self.errors)


if __name__ == "__main__":
    unittest.main()
