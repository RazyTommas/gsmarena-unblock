"""A query-string integer must never reach SQLite unbindable, on ANY route.

Two defects of one family, both found by executing a URL and neither by reading
code:

  GET /api/v1/devices?offset=9223372036854775808
      int() succeeds (Python integers are arbitrary precision), max(0, ...)
      succeeds, and the value fails at BIND time with `OverflowError: Python int
      too large to convert to SQLite INTEGER`. OverflowError is not a
      ValueError, so _pagination's except did not catch it: the handler thread
      died, socketserver closed the socket, and the client got NO HTTP RESPONSE
      (curl exit 52). Confirmed at six routes and present at all twelve
      _pagination call sites.

  GET /api/v1/devices?max_android=abc
      devices_page appended a clause containing `?` BEFORE int() ran, so the
      except branch appended a second clause `"0"` with no placeholder and left
      one `?` unbound:
        sqlite3.ProgrammingError: Incorrect number of bindings supplied.
        The current statement uses 3, and there are 2 supplied.
      Same outcome for the client, and the intent the comment there describes
      -- "a non-numeric filter should match nothing" -- had never once run.

This file exists to stop the SECOND one surviving. This codebase has already
shipped one bug that lived at five call sites and was "fixed" three times, each
time at whichever site a crash reached first; a test that covered six of the
twelve paginating routes would reproduce that exactly. So the coverage is
derived, not listed:

  * test_every_pagination_call_site_is_swept reads server.py, finds every
    `_pagination(` call, and fails if one sits in a method this file does not
    exercise. Adding a thirteenth paginating method fails the suite until it is
    covered.
  * test_every_api_route_answers probes EVERY `/api/v1/...` literal in the
    routing code, not a hand-picked subset, so a route added later is swept
    without editing this file.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.access import AccessPolicy  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import (  # noqa: E402
    SQLITE_MAX_INT, ObservatoryService, QueryPage, make_handler)

SERVER_SOURCE = ROOT / "src" / "mobile_observatory" / "server.py"

# Values chosen to sit on the boundaries that actually broke: the first integer
# SQLite cannot bind, one far past it, the negative mirror, a non-number, and an
# empty string.
BEYOND_SQLITE = str(2 ** 63)
FAR_BEYOND = "9" * 20
BELOW_SQLITE = str(-(2 ** 63) - 1)
HOSTILE_VALUES = (BEYOND_SQLITE, FAR_BEYOND, BELOW_SQLITE, "-1", "0", "abc", "", "1e9", "0x10")
HOSTILE_KEYS = ("offset", "cursor", "limit", "max_android")

# Every service method this file drives with HOSTILE_VALUES. Derived coverage is
# checked against the source below -- this list is the claim, not the proof.
SWEPT_PAGE_METHODS = (
    "updates_page", "devices_page", "chips_page", "chip_products_page",
    "product_source_builds_page", "source_records_page", "source_products_page",
    "releases_page", "product_releases_page", "product_security_page", "security_page",
)


def _pagination_callers(source: str) -> set[str]:
    """Every function containing a `_pagination(` call, from the source itself."""
    callers: set[str] = set()
    current = None
    for line in source.splitlines():
        match = re.match(r"\s*def (\w+)\(", line)
        if match:
            current = match.group(1)
        if line.lstrip().startswith("#"):
            continue  # a comment naming the helper is not a call to it
        if "_pagination(" in line and current and current != "_pagination":
            callers.add(current)
    return callers


def _api_route_literals(source: str) -> set[str]:
    """Every /api/v1 path literal in the routing code, turned into a probe path.

    A literal ending in `/` is a prefix the handler matches with startswith, so
    it gets a segment appended; the `/retry`-style suffixes are reconstructed
    from the pairs the source declares. Derived from the source so a route added
    later is probed without editing this file.
    """
    paths: set[str] = set()
    for literal in set(re.findall(r"""["'](/api/v1/[^"']*)["']""", source)):
        if literal.endswith("/"):
            paths.add(literal + "probe")
        else:
            paths.add(literal)
    # Prefix+suffix routes: the handler joins a prefix ending in `/` to a suffix
    # beginning with `/`, and neither half is a probeable path on its own.
    for prefix in ("/api/v1/admin/collection-requests/", "/api/v1/identity/products/",
                   "/api/v1/identity/agent-proposals/", "/api/v1/updates/"):
        for suffix in ("/retry", "/process", "/review", "/acknowledge"):
            paths.add(prefix + "probe" + suffix)
    return paths


class PaginationBoundsTest(unittest.TestCase):
    """_pagination itself, before any route is involved."""

    def test_a_value_sqlite_cannot_bind_is_clamped_not_raised(self) -> None:
        from mobile_observatory.server import _pagination
        for raw in (BEYOND_SQLITE, FAR_BEYOND):
            for key in ("offset", "cursor"):
                limit, offset = _pagination({key: [raw]})
                self.assertLessEqual(offset, SQLITE_MAX_INT, f"{key}={raw}")
                self.assertGreaterEqual(offset, 0)
            limit, _ = _pagination({"limit": [raw]})
            self.assertLessEqual(limit, 500)
            self.assertGreaterEqual(limit, 1)

    def test_every_returned_value_is_bindable(self) -> None:
        """The property that matters is not the number, it is that sqlite3 accepts
        it. Asserted by actually binding it, because the previous code also
        'returned an int' and that was the whole problem."""
        import sqlite3
        from mobile_observatory.server import _pagination
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE t(v INTEGER)")
        for key in HOSTILE_KEYS:
            for raw in HOSTILE_VALUES:
                limit, offset = _pagination({key: [raw]})
                connection.execute("SELECT * FROM t LIMIT ? OFFSET ?", (limit, offset))
        connection.close()

    def test_documented_behaviour_is_unchanged(self) -> None:
        """The clamp must not have moved the defaults the UI depends on."""
        from mobile_observatory.server import _pagination
        self.assertEqual(_pagination({})[0], 100)
        self.assertEqual(_pagination({"limit": ["5000"]})[0], 500)
        self.assertEqual(_pagination({"limit": ["abc"]})[0], 100)
        self.assertEqual(_pagination({"limit": ["0"]})[0], 1)
        self.assertEqual(_pagination({"offset": ["-5"]})[1], 0)
        self.assertEqual(_pagination({"cursor": ["40"]})[1], 40)


class PaginationCoverageIsDerivedTest(unittest.TestCase):
    def test_every_pagination_call_site_is_swept(self) -> None:
        callers = _pagination_callers(SERVER_SOURCE.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(callers), 11,
                                "precondition: the source really does have many callers")
        missing = sorted(callers - set(SWEPT_PAGE_METHODS))
        self.assertEqual(
            [], missing,
            "these methods call _pagination and are not swept by this file, which is "
            "how the last five-call-site bug survived three fixes. Add them to "
            "SWEPT_PAGE_METHODS: " + ", ".join(missing))

    def test_swept_methods_all_exist(self) -> None:
        """A typo in SWEPT_PAGE_METHODS would silently shrink the sweep."""
        for name in SWEPT_PAGE_METHODS:
            self.assertTrue(callable(getattr(ObservatoryService, name, None)), name)


class HostileQueryOverHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.corpus = Database.migrated(check_same_thread=False)
        self.addCleanup(self.corpus.close)
        seed_demonstration(self.corpus, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.service = ObservatoryService(
            self.corpus, Path(self.temp.name) / "local.sqlite", demonstration=True,
            sample_path=ROOT / "fixtures" / "real_source_sample.json")
        self.addCleanup(self.service.local.close)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, ROOT / "apps" / "web", AccessPolicy(None)))
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def status(self, path: str) -> int:
        """The response status, or fail -- a dropped connection is the defect."""
        try:
            with urllib.request.urlopen(self.base + path, timeout=30) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except Exception as error:  # noqa: BLE001
            self.fail(f"{path} produced no HTTP response at all: {error!r}. "
                      f"That is the defect: the handler raised, socketserver closed "
                      f"the socket, and the caller cannot tell a refusal from a crash.")

    def test_every_swept_method_survives_every_hostile_value(self) -> None:
        for name in SWEPT_PAGE_METHODS:
            method = getattr(self.service, name)
            for key in HOSTILE_KEYS:
                for raw in HOSTILE_VALUES:
                    with self.subTest(method=name, key=key, value=raw):
                        page = method({key: [raw]})
                        self.assertIsInstance(page, QueryPage)

    def test_every_api_route_answers(self) -> None:
        """Every /api/v1 literal in the routing code, not a hand-picked six."""
        routes = _api_route_literals(SERVER_SOURCE.read_text(encoding="utf-8"))
        self.assertGreater(len(routes), 20, "precondition: the routes were found")
        query = "&".join(f"{key}={BEYOND_SQLITE}" for key in ("offset", "cursor", "limit"))
        query += f"&max_android={FAR_BEYOND}&q=%25"
        for route in sorted(routes):
            with self.subTest(route=route):
                status = self.status(f"{route}?{query}")
                self.assertLess(status, 500,
                                f"{route} answered {status}; a hostile query string is a "
                                f"bad request or an empty page, never a server fault")

    def test_non_numeric_ceiling_selects_nothing_rather_than_dropping(self) -> None:
        """The intent the comment in devices_page describes, executed for the first
        time: a filter value that is not a version matches no device."""
        with urllib.request.urlopen(self.base + "/api/v1/devices?max_android=abc",
                                    timeout=30) as response:
            payload = json.load(response)
            self.assertEqual(response.status, 200)
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["meta"]["page"]["total"], 0)

    def test_a_numeric_ceiling_still_filters(self) -> None:
        """The clamp must not have turned the filter into a no-op."""
        with urllib.request.urlopen(self.base + "/api/v1/devices?limit=500") as response:
            everything = json.load(response)["meta"]["page"]["total"]
        with urllib.request.urlopen(self.base + "/api/v1/devices?max_android=1") as response:
            ancient = json.load(response)["meta"]["page"]["total"]
        self.assertLess(ancient, everything,
                        "max_android=1 must exclude devices; if it does not, the clause "
                        "is no longer being applied at all")

    def test_an_offset_past_the_end_is_an_empty_page_not_an_error(self) -> None:
        with urllib.request.urlopen(
                self.base + f"/api/v1/devices?offset={BEYOND_SQLITE}", timeout=30) as response:
            payload = json.load(response)
            self.assertEqual(response.status, 200)
        self.assertEqual(payload["items"], [])


if __name__ == "__main__":
    unittest.main()
