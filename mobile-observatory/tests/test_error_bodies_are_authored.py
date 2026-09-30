"""A refusal must name the right problem, with the right status, in our own words.

Two defects, both about a request being described back to the caller wrongly.

    POST /api/v1/updates/acknowledge-bulk  {"ids": "abcdef"}
      -> 404 {"error": "update_not_found"}

    A str is iterable, so `for item in event_ids` walked the SIX CHARACTERS of
    "abcdef", looked up "a" as an update id, found nothing, and reported a fact
    about the corpus in answer to a type error. `{"ids": 7}` reached the correct
    400 only because an int happens not to be iterable -- so the right answer
    depended on which wrong type you sent. No duck-typed guard catches this: a
    str satisfies `iter()`, `hasattr(x, '__iter__')` and every "is it a
    sequence" test there is. Only an explicit type check refuses it.

    POST /api/v1/admin/collection-requests/abc/retry
      -> 409 {"error": "retry_conflict",
              "detail": "invalid literal for int() with base 10: 'abc'"}

    Two faults in one body. 409 asserts a conflict with some real state and
    there was none -- every other malformed-id path in this server answers 404.
    And the `detail` is a raw interpreter message: it describes the server's
    implementation, not the caller's request, and this codebase does not state
    values it cannot source.

The sweep at the bottom is the pattern guard. Fixing the two reported routes
would leave the next one to be found the same way, so instead every POST route
is driven with a matrix of malformed payloads and every response body is checked
for interpreter and filesystem fingerprints.
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.access import AccessPolicy  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService, make_handler  # noqa: E402

# Text that can only have come from the interpreter, the OS or this machine.
# Checked as substrings of the response body, so a new route leaking one of them
# fails here rather than in a bug report.
FINGERPRINTS = (
    "invalid literal for int()",
    "Traceback (most recent call last)",
    "object has no attribute",
    "is not subscriptable",
    "unsupported operand type",
    "is not iterable",
    "not supported between instances",
    "takes no arguments",
    "argument of type",
    "No such file or directory",
    "Permission denied",
    str(ROOT),          # any absolute path from this checkout
    "/home/",
    "sqlite3.",
    "site-packages",
)

# Every POST route, with a body for the shape each expects. The point is breadth:
# a route missing from here is a route this guard does not cover.
POST_ROUTES = (
    "/api/v1/updates/acknowledge-bulk",
    "/api/v1/admin/config",
    "/api/v1/identity/agent-proposals",
    "/api/v1/identity/agent-proposals/abc/review",
    "/api/v1/watches",
    "/api/v1/identity/decisions",
    "/api/v1/admin/collection-requests",
    "/api/v1/admin/collection-requests/process-next",
    "/api/v1/admin/collection-requests/recover",
    "/api/v1/admin/collection-requests/abc/retry",
    "/api/v1/admin/collection-requests/abc/process",
    "/api/v1/admin/collection-requests/-1/retry",
    "/api/v1/admin/collection-requests/99999999999999999999/retry",
    "/api/v1/identity/products/abc/review",
    "/api/v1/updates/abc/acknowledge",
)

# Payloads chosen for the type confusions that actually reached a client: a str
# where a list was expected, a scalar where an object was, a JSON array as the
# whole body, and a nested str in a list-of-list position.
MALFORMED_BODIES = (
    b"",
    b"{}",
    b"not json at all",
    b'"abcdef"',
    b"[]",
    b"7",
    b"null",
    b'{"ids": "abcdef"}',
    b'{"ids": 7}',
    b'{"ids": {"a": 1}}',
    b'{"ids": null}',
    b'{"preferredRegions": "ILO"}',
    b'{"enabledSources": "samsung"}',
    b'{"decision": ["approved"]}',
    b'{"subjectType": "hardware_model", "subjectId": ["x"], "enabled": true}',
)


class ServerFixture(unittest.TestCase):
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
        self.port = self.server.server_port

    def post(self, path: str, body: bytes = b"{}"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("POST", path, body=body,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", "replace")
        finally:
            connection.close()


class StrIsIterableTest(ServerFixture):
    def test_a_string_of_ids_is_a_bad_request_not_a_missing_update(self) -> None:
        status, body = self.post("/api/v1/updates/acknowledge-bulk", b'{"ids": "abcdef"}')
        self.assertEqual(status, 400,
                         "a str was iterated character by character and the first character "
                         "reported as an unknown update id")
        self.assertEqual(json.loads(body), {"error": "invalid_update_ids"})

    def test_the_wrong_type_gets_the_same_answer_whichever_wrong_type_it_is(self) -> None:
        """`{"ids": 7}` was already right, by accident. The answer must not depend
        on whether the wrong type happens to be iterable."""
        answers = {}
        for body in (b'{"ids": "abcdef"}', b'{"ids": 7}', b'{"ids": {"a": 1}}',
                     b'{"ids": null}', b'{"ids": 1.5}', b'{"ids": true}'):
            answers[body] = self.post("/api/v1/updates/acknowledge-bulk", body)
        self.assertEqual({status for status, _ in answers.values()}, {400}, answers)

    def test_a_real_list_of_ids_still_works(self) -> None:
        """The guard must refuse the type, not the endpoint."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request("GET", "/api/v1/updates")
        ids = [item["id"] for item in json.load(connection.getresponse())["items"][:2]]
        connection.close()
        self.assertTrue(ids, "precondition: there are updates to acknowledge")
        status, body = self.post("/api/v1/updates/acknowledge-bulk",
                                 json.dumps({"ids": ids}).encode())
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["acknowledged"], len(ids))

    def test_an_empty_list_is_still_accepted(self) -> None:
        status, body = self.post("/api/v1/updates/acknowledge-bulk", b'{"ids": []}')
        self.assertEqual(status, 200, body)

    def test_config_list_fields_refuse_a_string_too(self) -> None:
        """Same assumption, a second endpoint. It happened to reach 400 anyway,
        because no region key is one character long -- a property of the option
        table, not of the code."""
        for body in (b'{"preferredRegions": "ILO"}', b'{"enabledSources": "samsung"}'):
            with self.subTest(body=body):
                status, _ = self.post("/api/v1/admin/config", body)
                self.assertEqual(status, 400)

    def test_no_list_taking_entry_point_iterates_a_string(self) -> None:
        """Directly, at the service layer, so the reason is visible rather than
        inferred from a status code."""
        with self.assertRaises(TypeError):
            self.service.acknowledge_many("abcdef")
        with self.assertRaises(TypeError):
            self.service.save_config({"preferredRegions": "ILO"})


class MalformedIdStatusTest(ServerFixture):
    def test_a_non_numeric_request_id_is_not_found_not_a_conflict(self) -> None:
        for suffix in ("retry", "process"):
            with self.subTest(suffix=suffix):
                status, body = self.post(f"/api/v1/admin/collection-requests/abc/{suffix}")
                self.assertEqual(status, 404,
                                 "409 asserts a conflict with real state; there is no "
                                 "request named 'abc' for anything to conflict with")
                self.assertEqual(json.loads(body),
                                 {"error": "collection_request_not_found"})

    def test_the_status_matches_every_other_malformed_id_path(self) -> None:
        """The consistency claim, checked rather than asserted: an unknown device,
        product, CVE and update all answer 404, so an unknown request must too."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        for path in ("/api/v1/devices/abc", "/api/v1/products/abc",
                     "/api/v1/security/cves/abc"):
            connection.request("GET", path)
            response = connection.getresponse()
            self.assertEqual(response.status, 404, path)
            response.read()
        connection.close()
        self.assertEqual(self.post("/api/v1/updates/abc/acknowledge")[0], 404)
        self.assertEqual(self.post("/api/v1/admin/collection-requests/abc/retry")[0], 404)

    def test_an_id_too_large_for_sqlite_is_not_found_rather_than_a_fault(self) -> None:
        """The same integer family as the query-string defect, on a path segment."""
        status, _ = self.post("/api/v1/admin/collection-requests/"
                              f"{2 ** 63}/retry")
        self.assertIn(status, (404, 409))


class NoResponseBodyQuotesTheInterpreterTest(ServerFixture):
    def test_the_reported_route_no_longer_leaks_int_s_message(self) -> None:
        _, body = self.post("/api/v1/admin/collection-requests/abc/retry")
        self.assertNotIn("invalid literal for int()", body)

    def test_no_post_route_leaks_a_fingerprint_for_any_malformed_body(self) -> None:
        """The pattern. Fixing the one reported route would leave the next one to
        be found the same way it was: by someone reading an error message."""
        for path in POST_ROUTES:
            for body in MALFORMED_BODIES:
                with self.subTest(route=path, body=body):
                    status, text = self.post(path, body)
                    self.assertLess(status, 500, f"{path} answered {status}: {text}")
                    for fingerprint in FINGERPRINTS:
                        self.assertNotIn(
                            fingerprint, text,
                            f"{path} returned {fingerprint!r} to the caller. A refusal "
                            f"must describe the request in our own words; an interpreter "
                            f"message describes the server, and a path leaks it.")

    def test_a_refusal_still_says_something_useful(self) -> None:
        """Filtering must not have turned every refusal into a blank. The `detail`
        on a proposal import is authored prose and is meant to be read."""
        status, body = self.post("/api/v1/identity/agent-proposals", b'[{"nope": 1}]')
        self.assertEqual(status, 400)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "invalid_agent_proposals")
        self.assertIn("proposal", payload["detail"].lower())

    def test_an_os_error_does_not_reach_the_caller(self) -> None:
        """An OSError carries strerror plus the path it failed on. On a box reached
        through a tunnel -- which the handoff says still counts as loopback --
        that is a filesystem detail handed to a stranger."""
        def explode():
            raise OSError(2, "No such file or directory", "/home/secret/ledger/raw")
        self.service.recover_collection_requests = explode
        status, body = self.post("/api/v1/admin/collection-requests/recover")
        self.assertEqual(status, 409)
        self.assertNotIn("/home/secret", body)
        self.assertNotIn("No such file", body)
        self.assertEqual(json.loads(body)["error"], "worker_active")


if __name__ == "__main__":
    unittest.main()
