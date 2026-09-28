"""Nothing may change the corpus without proving who and where it is.

The server shipped with no authentication, no authorisation and no origin
checking. Fourteen POST routes change state; one of them NULLs
hardware_model_id across product_firmware_releases, deletes rows from
product_hardware_links, and replays that decision at every future startup.

This was demonstrated against the running server before the fix, with a valid
payload and a real device id:

    POST /api/v1/watches  Content-Type: text/plain  Origin: https://evil.example
    -> HTTP 200, row written

Both headers are load-bearing. `text/plain` makes it a CORS-*simple* request,
so a browser sends it cross-site with no preflight to block -- the UI's own
application/json would have been preflighted, but an attacker does not use the
UI's client. And the hostile Origin was ignored outright.

The tests below are in two layers, deliberately. The AccessPolicy layer is
exhaustive and fast. The server layer is small but runs a REAL
ThreadingHTTPServer on a REAL socket, because every previous hole here was in
the wiring rather than the rule: a policy object that returns the right answer
while do_POST never calls it is exactly the shape of bug this must catch.
"""
from __future__ import annotations

import json
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
from mobile_observatory.access import (AccessPolicy, is_loopback,  # noqa: E402
                                       read_token, token_for_binding, write_token)
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService, make_handler  # noqa: E402

# A fixture value, never a deployed credential: the tests construct their own
# AccessPolicy around it. Named so no secret scanner or reader mistakes it.
TOKEN = "fixture-token-not-a-real-credential"


class PolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = AccessPolicy(TOKEN)

    # -- the open (development) case ------------------------------------------
    def test_no_token_configured_allows_everything(self) -> None:
        """Loopback development must not demand a credential; making it do so
        only teaches people to paste one."""
        policy = AccessPolicy(None)
        self.assertTrue(policy.open)
        self.assertTrue(policy.decide(method="POST", origin="https://evil.example").allowed)

    # -- authentication --------------------------------------------------------
    def test_a_write_with_no_token_is_401(self) -> None:
        decision = self.policy.decide(method="POST")
        self.assertFalse(decision.allowed)
        self.assertEqual(401, decision.status)

    def test_a_read_with_no_token_is_also_401(self) -> None:
        """A page served to an unauthenticated browser is a page that can then
        be scripted. On a reachable interface this is not a public mirror."""
        self.assertEqual(401, self.policy.decide(method="GET").status)

    def test_the_token_is_accepted_from_a_bearer_header(self) -> None:
        self.assertTrue(self.policy.decide(
            method="POST", authorization=f"Bearer {TOKEN}").allowed)

    def test_the_token_is_accepted_from_the_cookie(self) -> None:
        self.assertTrue(self.policy.decide(
            method="POST", cookie=f"other=1; mo_token={TOKEN}; x=2").allowed)

    def test_a_wrong_token_is_refused(self) -> None:
        self.assertEqual(401, self.policy.decide(
            method="POST", authorization="Bearer not-the-token").status)

    def test_an_empty_token_never_authenticates(self) -> None:
        """A blank credential must not compare equal to anything."""
        for supplied in ("", "   "):
            with self.subTest(supplied=repr(supplied)):
                self.assertFalse(self.policy.token_matches(supplied))

    def test_authentication_is_checked_before_origin(self) -> None:
        """Otherwise 401-vs-403 tells a cross-site probe whether it already
        holds a valid session."""
        decision = self.policy.decide(method="POST", origin="https://evil.example")
        self.assertEqual(401, decision.status, "an unauthenticated write must not reveal more")

    # -- the CSRF control ------------------------------------------------------
    def test_an_authenticated_write_from_another_origin_is_403(self) -> None:
        """The whole point: the browser sends the cookie automatically, so the
        token alone does NOT prove the request was composed on our page."""
        decision = self.policy.decide(
            method="POST", cookie=f"mo_token={TOKEN}",
            origin="https://evil.example", host="box:8000")
        self.assertFalse(decision.allowed)
        self.assertEqual(403, decision.status)

    def test_a_write_from_our_own_origin_is_allowed(self) -> None:
        self.assertTrue(self.policy.decide(
            method="POST", cookie=f"mo_token={TOKEN}",
            origin="http://box:8000", host="box:8000").allowed)

    def test_a_different_port_is_a_different_origin(self) -> None:
        self.assertEqual(403, self.policy.decide(
            method="POST", authorization=f"Bearer {TOKEN}",
            origin="http://box:9999", host="box:8000").status)

    def test_a_different_scheme_is_a_different_origin(self) -> None:
        self.assertEqual(403, self.policy.decide(
            method="POST", authorization=f"Bearer {TOKEN}",
            origin="https://box:8000", host="box:8000").status)

    def test_a_null_origin_is_refused(self) -> None:
        """Sandboxed iframes and data: documents send Origin: null. That is not
        our page."""
        self.assertEqual(403, self.policy.decide(
            method="POST", authorization=f"Bearer {TOKEN}",
            origin="null", host="box:8000").status)

    def test_referer_is_used_when_origin_is_absent(self) -> None:
        self.assertEqual(403, self.policy.decide(
            method="POST", authorization=f"Bearer {TOKEN}",
            referer="https://evil.example/page", host="box:8000").status)

    def test_no_origin_and_no_referer_is_allowed_for_an_authenticated_client(self) -> None:
        """A browser ALWAYS sends Origin cross-origin, so absence means this is
        not a cross-site browser request -- curl, a script, the batch. Those
        already had to present a token."""
        self.assertTrue(self.policy.decide(
            method="POST", authorization=f"Bearer {TOKEN}", host="box:8000").allowed)

    def test_a_read_is_not_origin_checked(self) -> None:
        """Reads change nothing, and refusing them cross-origin would break
        nothing an attacker could not already do by fetching the URL."""
        self.assertTrue(self.policy.decide(
            method="GET", authorization=f"Bearer {TOKEN}",
            origin="https://evil.example", host="box:8000").allowed)

    def test_every_mutating_verb_is_covered_not_just_post(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(403, self.policy.decide(
                    method=method, authorization=f"Bearer {TOKEN}",
                    origin="https://evil.example", host="box:8000").status)


class BindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.data = Path(self._temp.name)

    def test_the_wildcard_bind_is_not_loopback(self) -> None:
        """0.0.0.0 is the case most likely to be typed and the one that
        exposes everything. Calling it loopback would disable the guard."""
        for host in ("0.0.0.0", "::", "", "192.168.1.5", "example.internal"):
            with self.subTest(host=host):
                self.assertFalse(is_loopback(host))
        for host in ("127.0.0.1", "::1", "localhost", "127.0.0.53"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback(host))

    def test_loopback_with_nothing_configured_runs_open(self) -> None:
        token, note = token_for_binding(self.data, "127.0.0.1", environ={})
        self.assertIsNone(token)
        self.assertFalse((self.data / "auth-token").exists(),
                         "no credential should be minted for a loopback dev run")
        self.assertIn("open", note)

    def test_a_reachable_bind_with_nothing_configured_mints_a_token(self) -> None:
        """The defect this guards: starting open on an interface other people
        can reach."""
        token, note = token_for_binding(self.data, "0.0.0.0", environ={})
        self.assertTrue(token)
        path = self.data / "auth-token"
        self.assertTrue(path.is_file())
        self.assertEqual(0o600, path.stat().st_mode & 0o777,
                         "a world-readable token has already been disclosed")
        self.assertIn(str(path), note)
        self.assertNotIn(token, note,
                         "the note is printed at startup; it must never carry the value")

    def test_the_environment_wins_over_the_file(self) -> None:
        write_token(self.data)
        self.assertEqual("from-env", read_token(self.data, {"MOBILE_OBSERVATORY_TOKEN": "from-env"}))

    def test_a_blank_environment_value_is_absent_not_an_empty_password(self) -> None:
        stored = write_token(self.data)
        self.assertEqual(stored, read_token(self.data, {"MOBILE_OBSERVATORY_TOKEN": "  "}))


class LiveServerTest(unittest.TestCase):
    """A real socket. Every hole here so far was in the wiring, not the rule."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        data = Path(self._temp.name)
        corpus = Database.migrated(data / "corpus.sqlite", check_same_thread=False)
        seed_demonstration(corpus, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.service = ObservatoryService(corpus, data / "local.sqlite", demonstration=True)
        self.addCleanup(self.service.local.close)
        self.addCleanup(corpus.close)

        handler = make_handler(self.service, ROOT / "apps" / "web", AccessPolicy(TOKEN))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)

        self.device = corpus.connection.execute(
            "SELECT id FROM hardware_models LIMIT 1").fetchone()["id"]

    def call(self, path, *, method="GET", headers=None, body=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=body.encode() if body else None, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def watch_body(self, enabled=True):
        return json.dumps({"subjectType": "hardware_model",
                           "subjectId": self.device, "enabled": enabled})

    def watch_count(self):
        return self.service.local.execute("SELECT count(*) FROM watches").fetchone()[0]

    def test_the_exact_attack_that_worked_before_is_now_refused(self) -> None:
        """text/plain + hostile Origin: a CORS-simple cross-site write."""
        before = self.watch_count()
        status, _ = self.call("/api/v1/watches", method="POST",
                              headers={"Content-Type": "text/plain",
                                       "Origin": "https://evil.example",
                                       "Authorization": f"Bearer {TOKEN}"},
                              body=self.watch_body())
        self.assertEqual(403, status)
        self.assertEqual(before, self.watch_count(), "the refused write must not have landed")

    def test_an_unauthenticated_write_is_refused_and_changes_nothing(self) -> None:
        before = self.watch_count()
        status, _ = self.call("/api/v1/watches", method="POST",
                              headers={"Content-Type": "application/json"},
                              body=self.watch_body())
        self.assertEqual(401, status)
        self.assertEqual(before, self.watch_count())

    def test_an_authenticated_same_origin_write_still_works(self) -> None:
        """The control must not simply break the product."""
        status, _ = self.call("/api/v1/watches", method="POST",
                              headers={"Content-Type": "application/json",
                                       "Origin": f"http://127.0.0.1:{self.port}",
                                       "Host": f"127.0.0.1:{self.port}",
                                       "Authorization": f"Bearer {TOKEN}"},
                              body=self.watch_body())
        self.assertEqual(200, status)
        self.assertEqual(1, self.watch_count())

    def test_reads_require_the_token_too(self) -> None:
        self.assertEqual(401, self.call("/api/v1/devices")[0])
        status, _ = self.call("/api/v1/devices",
                              headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(200, status)

    def test_every_mutating_route_is_gated(self) -> None:
        """Enumerated rather than sampled: the gate is one call in do_POST, and
        a route added outside it would be silently open."""
        routes = ["/api/v1/updates/acknowledge-bulk", "/api/v1/admin/config",
                  "/api/v1/identity/agent-proposals", "/api/v1/watches",
                  "/api/v1/identity/decisions", "/api/v1/admin/collection-requests",
                  "/api/v1/admin/collection-requests/process-next",
                  "/api/v1/admin/collection-requests/recover",
                  f"/api/v1/identity/products/{self.device}/review"]
        open_routes = []
        for path in routes:
            status, _ = self.call(path, method="POST",
                                  headers={"Content-Type": "application/json"}, body="{}")
            if status != 401:
                open_routes.append(f"{path} -> {status}")
        self.assertEqual([], open_routes,
                         "these mutating routes answered without a token: " + "; ".join(open_routes))

    def test_a_token_in_the_query_sets_a_cookie_and_redirects_without_it(self) -> None:
        """The operator has a token in a file and a browser. The redirect is
        what keeps the credential out of history and out of any Referer."""
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/?token={TOKEN}")
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=20) as response:
                status, headers = response.status, response.headers
        except urllib.error.HTTPError as error:
            status, headers = error.code, error.headers
        self.assertEqual(303, status)
        self.assertEqual("/", headers.get("Location"))
        cookie = headers.get("Set-Cookie") or ""
        self.assertIn(f"mo_token={TOKEN}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_a_wrong_query_token_does_not_set_a_cookie(self) -> None:
        status, _ = self.call("/?token=wrong")
        self.assertEqual(401, status)

    def test_the_token_redirect_cannot_be_pointed_off_site(self) -> None:
        """An open redirect, found by probing this flow rather than reading it.

        urlparse leaves a backslash alone, but browsers normalise it to "/", so
        `/\\evil.example?token=<valid>` produced `Location: /\\evil.example`,
        which a browser reads as `//evil.example` -- a protocol-relative URL
        pointing off-site. It only ever fired for a caller already holding a
        valid token, so it was not a way in; a redirector that can be aimed
        elsewhere is still a building block worth removing.
        """
        opener = urllib.request.build_opener(_NoRedirect())
        for hostile in ("/\\evil.example", "//evil.example/", "/\\\\evil.example",
                        "///evil.example"):
            with self.subTest(path=hostile):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}{hostile}?token={TOKEN}")
                try:
                    with opener.open(request, timeout=20) as response:
                        location = response.headers.get("Location")
                except urllib.error.HTTPError as error:
                    location = error.headers.get("Location")
                self.assertIsNotNone(location, "precondition: this path redirects")
                # Exactly one leading slash, and no backslash a browser could
                # fold into a second one.
                self.assertTrue(location.startswith("/"), location)
                self.assertFalse(location.startswith("//"), f"protocol-relative: {location}")
                self.assertNotIn("\\", location, f"a browser reads \\ as /: {location}")

    def test_an_unknown_api_path_is_404_not_the_html_shell(self) -> None:
        """A probe on a typo'd route used to get 200 + index.html back, so a
        health check could pass forever without checking anything."""
        status, body = self.call("/api/definitely-not-a-route",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(404, status)
        self.assertNotIn(b"<!doctype html", body.lower())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
