"""A POST body must be bounded, and a failure must leave a response and a log line.

Three faults in one area, all of which made the server answer nothing at all.

1. Content-Length was believed. Eight routes each did
   `self.rfile.read(int(self.headers.get("Content-Length", "0")))`, and read()
   blocks until that many bytes arrive. Reproduced:

       curl -m 5 -X POST /api/v1/watches -H 'Content-Length: 5000' \
            --data-binary '{}'

   curl timed out at 5s with no response while the handler thread sat inside
   read(), still holding its per-thread SQLite handles. There was no read
   timeout anywhere in the process, so on any interface a stranger can reach, a
   trickle of these exhausts the thread pool using almost no bandwidth. A
   negative Content-Length is the same defect with a different number:
   `read(-1)` means read to EOF.

2. An unhandled exception dropped the connection. It propagated into
   socketserver, which printed a traceback and closed the socket: curl exit 52,
   no status line. The handoff lists this under "Open, none blocking".

3. `log_message` was a no-op, so (2) left no record either. A fault that
   produces neither a response nor a log line can only be found by reproducing
   it, which is how six of this round's defects were found.

The tests below assert the properties, not the implementations: bounded rather
than "uses read1", answered rather than "returns 400 from _body". The important
one is test_a_lying_content_length_does_not_hold_the_thread, which measures
wall-clock: a guard that only checked the status code would pass against a
version that answered 400 after ten minutes.
"""
from __future__ import annotations

import http.client
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.access import AccessPolicy  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService, make_handler  # noqa: E402

# Every POST route that takes a body, so a fix at one of them is not mistaken for
# a fix at all of them -- the shape that made this one flaw eight flaws.
BODY_ROUTES = (
    "/api/v1/updates/acknowledge-bulk",
    "/api/v1/admin/config",
    "/api/v1/identity/agent-proposals",
    "/api/v1/watches",
    "/api/v1/identity/decisions",
    "/api/v1/admin/collection-requests",
    "/api/v1/identity/agent-proposals/probe/review",
    "/api/v1/identity/products/probe/review",
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
        self.handler = make_handler(self.service, ROOT / "apps" / "web", AccessPolicy(None))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

    def raw_post(self, path: str, *, declared: str, body: bytes, timeout: float = 30.0,
                 half_close: bool = False):
        """POST with a Content-Length we choose, independent of the real body.

        http.client would compute the header for us, which is exactly the bug's
        blind spot, so the request is assembled by hand.

        half_close shuts down the write side after sending, so the server reaches
        a clean EOF mid-body instead of waiting out the idle timeout. Both are
        real client behaviours and both used to hang; the sweep below uses the
        fast one so it can cover every route, and the wall-clock test uses the
        slow one because the bound is what it measures.
        """
        request = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   f"Content-Type: application/json\r\n"
                   f"Content-Length: {declared}\r\nConnection: close\r\n\r\n").encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as sock:
            sock.sendall(request + body)
            if half_close:
                sock.shutdown(socket.SHUT_WR)
            chunks = []
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except (TimeoutError, socket.timeout):
                return None  # nothing arrived within the deadline: the defect
        raw = b"".join(chunks)
        if not raw:
            return None
        status = int(raw.split(b" ")[1])
        _, _, body_bytes = raw.partition(b"\r\n\r\n")
        return status, body_bytes


class LyingContentLengthTest(ServerFixture):
    def test_a_lying_content_length_does_not_hold_the_thread(self) -> None:
        """Wall-clock, not just the status. Unbounded is the defect; a large bound
        is still the defect at a different scale."""
        started = time.monotonic()
        answer = self.raw_post("/api/v1/watches", declared="5000", body=b"{}", timeout=60)
        elapsed = time.monotonic() - started
        self.assertIsNotNone(
            answer, "the server sent no response at all: the handler is still blocked in "
                    "rfile.read() waiting for bytes the client never promised to send")
        status, _ = answer
        self.assertEqual(status, 400)
        self.assertLess(elapsed, 30,
                        f"answered after {elapsed:.1f}s. The read must bound how long the "
                        f"body may go quiet, or one request still occupies a thread for "
                        f"as long as an attacker chooses")

    def test_every_body_route_refuses_a_lying_length(self) -> None:
        """One route fixed is not this defect fixed. It lived at eight."""
        for path in BODY_ROUTES:
            with self.subTest(route=path):
                answer = self.raw_post(path, declared="5000", body=b"{}", timeout=60,
                                       half_close=True)
                self.assertIsNotNone(answer, f"{path} sent no response")
                self.assertIn(answer[0], (400, 404),
                              f"{path} answered {answer[0]} for a body shorter than its "
                              f"declared length")

    def test_a_negative_length_is_refused_not_read_to_eof(self) -> None:
        answer = self.raw_post("/api/v1/watches", declared="-1", body=b"{}", timeout=60,
                               half_close=True)
        self.assertIsNotNone(answer, "read(-1) reads to EOF, i.e. blocks until the client "
                                     "closes -- the same hang with a different header")
        self.assertEqual(answer[0], 400)

    def test_a_length_larger_than_the_cap_is_refused_before_reading(self) -> None:
        """Refused on the declaration, so the bytes are never buffered."""
        started = time.monotonic()
        answer = self.raw_post("/api/v1/watches", declared="999999999", body=b"{}", timeout=60)
        self.assertIsNotNone(answer)
        self.assertEqual(answer[0], 400)
        self.assertLess(time.monotonic() - started, 5,
                        "an oversized declaration must be refused without waiting for it")

    def test_an_unparseable_length_is_refused(self) -> None:
        answer = self.raw_post("/api/v1/watches", declared="abc", body=b"{}", timeout=60)
        self.assertIsNotNone(answer)
        self.assertEqual(answer[0], 400)

    def test_an_honest_body_still_works(self) -> None:
        """The bound must not have broken the routes it protects."""
        device = self.corpus.connection.execute(
            "SELECT id FROM hardware_models LIMIT 1").fetchone()[0]
        payload = json.dumps({"subjectType": "hardware_model", "subjectId": device,
                              "enabled": True}).encode()
        answer = self.raw_post("/api/v1/watches", declared=str(len(payload)), body=payload)
        self.assertEqual(answer[0], 200, answer[1])
        self.assertTrue(json.loads(answer[1])["enabled"])

    def test_a_post_with_no_body_at_all_still_works(self) -> None:
        """Several routes accept an empty POST; absent Content-Length must not be
        read to EOF either."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request("POST", "/api/v1/admin/collection-requests/process-next")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        connection.close()

    def test_a_body_larger_than_a_routes_own_limit_is_refused(self) -> None:
        """The proposal import declares 1 MB; the check must apply to the bytes
        that arrived, not to the client's claim about them."""
        payload = b"[" + b'"x",' * 300_000 + b'"x"]'
        self.assertGreater(len(payload), 1_000_000)
        answer = self.raw_post("/api/v1/identity/agent-proposals",
                               declared=str(len(payload)), body=payload, timeout=60)
        self.assertIsNotNone(answer)
        self.assertEqual(answer[0], 400)


class AnUnhandledFaultIsAnsweredAndLoggedTest(ServerFixture):
    """The handler must answer 500 rather than closing the socket, and say so."""

    def _break_a_route(self) -> None:
        def explode(_query):
            raise RuntimeError("planted fault: a route that raises")
        self.service.devices_page = explode

    def test_a_raising_route_answers_500_rather_than_dropping(self) -> None:
        self._break_a_route()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("GET", "/api/v1/devices")
            response = connection.getresponse()
        except (http.client.RemoteDisconnected, ConnectionResetError) as error:
            self.fail(f"the connection was dropped with no HTTP response ({error!r}). "
                      f"A caller cannot tell a server fault from a network failure, and "
                      f"a monitoring probe reads it as neither.")
        body = response.read()
        self.assertEqual(response.status, 500)
        self.assertEqual(json.loads(body), {"error": "internal_error"})
        connection.close()

    def test_the_fault_is_not_described_to_the_caller(self) -> None:
        """A 500 body must carry a stable code, never the exception text: that is
        how `invalid literal for int() with base 10: 'abc'` became API prose."""
        self._break_a_route()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request("GET", "/api/v1/devices")
        body = connection.getresponse().read().decode()
        connection.close()
        self.assertNotIn("planted fault", body)
        self.assertNotIn("RuntimeError", body)
        self.assertNotIn("Traceback", body)

    def test_the_fault_reaches_the_log(self) -> None:
        """The other half: a fault the operator cannot see is a fault found twice."""
        self._break_a_route()
        captured = io.StringIO()
        with redirect_stderr(captured):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            connection.request("GET", "/api/v1/devices")
            connection.getresponse().read()
            connection.close()
            time.sleep(0.3)  # the handler thread writes the line, not this one
        logged = captured.getvalue()
        self.assertIn("RuntimeError", logged,
                      "the exception type must reach the log even though it must not reach "
                      f"the caller. stderr held: {logged!r}")
        self.assertIn("/api/v1/devices", logged)


class AccessLogTest(ServerFixture):
    def test_a_served_request_leaves_a_line(self) -> None:
        captured = io.StringIO()
        with redirect_stderr(captured):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            connection.request("GET", "/api/v1/devices?limit=1")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            time.sleep(0.3)
        logged = captured.getvalue()
        self.assertIn("GET /api/v1/devices?limit=1", logged,
                      "log_message is a no-op again; there is no HTTP access log")
        self.assertIn("200", logged)

    def test_control_characters_in_a_request_line_cannot_forge_a_line(self) -> None:
        """The request line comes off the wire, so a caller must not be able to
        choose what a terminal renders or to inject a second log entry."""
        captured = io.StringIO()
        with redirect_stderr(captured):
            with socket.create_connection(("127.0.0.1", self.port), timeout=30) as sock:
                sock.sendall(b"GET /api/v1/devices%0d%0afake-line HTTP/1.1\r\n"
                             b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
                while sock.recv(65536):
                    pass
            time.sleep(0.3)
        for line in captured.getvalue().splitlines():
            if line.strip():
                self.assertNotEqual(line.strip(), "fake-line")
        self.assertNotIn("\r", captured.getvalue().replace("\r\n", "\n"))


if __name__ == "__main__":
    unittest.main()
