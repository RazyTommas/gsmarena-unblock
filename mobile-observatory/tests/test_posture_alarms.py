"""The two exposures docs/ACCESS_CONTROL.md called un-closeable must be LOUD.

They are still un-closeable here -- the proxy is somebody else's config and the
TLS terminator is somebody else's daemon -- but they were also undetectable,
which is a different problem and the one these tests hold closed:

  * a loopback-bound server behind a reverse proxy or an SSH tunnel is remotely
    reachable while still counting as "open";
  * plain HTTP exposes a permanent bearer token.

Everything below drives a REAL ThreadingHTTPServer on a real socket, including
one bound to 0.0.0.0 and reached over this machine's own LAN address, because
both alarms read the connection's PEER and the request's HEADERS -- neither of
which a unit test on the policy object can produce. The policy-shaped version
of these tests passed against a server that never called the detector at all.

Each alarm is proven three ways, because any one of them alone is satisfiable
by a broken instrument:
  1. it FIRES in the posture that is unsafe;
  2. it stays CLEAR in the posture that is safe (a check that always fires is
     as useless as one that never does -- the dev posture, loopback + no token
     + no proxy, must stay silent or nobody will keep this);
  3. the opt-out silences EXACTLY ONE of them, never both.
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.access import AccessPolicy  # noqa: E402
from mobile_observatory.posture import (ALARMS, INSECURE_OK_ENV_VAR,  # noqa: E402
                                        OPEN_AND_REACHABLE, TOKEN_IN_CLEARTEXT,
                                        ExposureMonitor, Posture,
                                        parse_acknowledgement, posture_at_startup,
                                        record_acknowledgement, token_provenance)
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService, make_handler  # noqa: E402

# A fixture value, never a deployed credential.
TOKEN = "fixture-token-not-a-real-credential"


def lan_address() -> str | None:
    """This machine's own non-loopback IPv4, or None.

    Discovered rather than hardcoded, and the tests that need it skip rather
    than fail when there is none: a box with no network is a legitimate place to
    run this suite, and a test that cannot run must say "skipped" and not
    "passed". No packet is sent -- connect() on a UDP socket only picks a route.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))      # TEST-NET-1, RFC 5737: never routed
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


LAN = lan_address()


class LiveServerPosture:
    """One real server in a chosen posture, plus a way to ask it questions."""

    def __init__(self, case: unittest.TestCase, *, bind: str, token: str | None,
                 acknowledge: str | None = None):
        self._temp = tempfile.TemporaryDirectory()
        case.addCleanup(self._temp.cleanup)
        self.data = Path(self._temp.name)
        corpus = Database.migrated(self.data / "corpus.sqlite", check_same_thread=False)
        seed_demonstration(corpus, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.service = ObservatoryService(corpus, self.data / "local.sqlite", demonstration=True)
        case.addCleanup(self.service.local.close)
        case.addCleanup(corpus.close)

        self.lines: list[str] = []
        self.policy = AccessPolicy(token)
        # Bound first, handler attached second, because the posture's banner
        # names the port and the port is only known once the socket exists.
        # BaseServer stores RequestHandlerClass and instantiates it per request,
        # so nothing has been served between these two statements.
        self.server = ThreadingHTTPServer((bind, 0), BaseHTTPRequestHandler)
        case.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        self.posture = posture_at_startup(
            data_dir=self.data, host=bind, port=self.port, token=token,
            token_source="none" if token is None else "environment",
            environ={} if acknowledge is None else {INSECURE_OK_ENV_VAR: acknowledge})
        # realarm_seconds=0 so a re-stated alarm is observable within a test
        # rather than five minutes later. The DEFAULT is 300s and is exercised
        # separately in MonitorTest, so this convenience cannot hide it.
        self.monitor = ExposureMonitor(self.posture, emit=self.lines.append,
                                       realarm_seconds=0)
        self.server.RequestHandlerClass = make_handler(
            self.service, ROOT / "apps" / "web", self.policy, self.monitor)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        case.addCleanup(thread.join, 5)
        case.addCleanup(self.server.shutdown)

    def call(self, path="/api/v1/devices", *, host="127.0.0.1", headers=None, method="GET",
             body=None):
        request = urllib.request.Request(f"http://{host}:{self.port}{path}", method=method,
                                         data=body.encode() if body else None,
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def alarms(self) -> dict:
        return {a["name"]: a for a in self.monitor.report()["alarms"]}

    def firing(self) -> set[str]:
        return {name for name, a in self.alarms().items() if a["status"] == "firing"}


class TheDevelopmentPostureStaysSilent(unittest.TestCase):
    """Loopback + no token + nothing in front is the documented dev machine.

    If this ever becomes noisy the whole mechanism is worthless: an operator who
    sees the alarm on their laptop every day will not read it on the box that
    matters. This is the "it does NOT fire" half, and it is first on purpose.
    """

    def setUp(self) -> None:
        self.box = LiveServerPosture(self, bind="127.0.0.1", token=None)

    def test_a_plain_loopback_request_raises_nothing(self) -> None:
        status, _ = self.box.call()
        self.assertEqual(200, status)
        self.assertEqual(set(), self.box.firing())
        self.assertFalse(self.box.monitor.report()["alarming"])
        self.assertEqual([], [line for line in self.box.lines if "ALARM" in line])

    def test_the_banner_says_open_and_says_why_the_origin_check_is_off(self) -> None:
        self.assertEqual(
            f"posture: bind=127.0.0.1:{self.box.port} exposure=loopback-only "
            f"auth=open(no token) origin-check=inactive-because-open "
            f"tls-in-front=unknown-until-a-request-arrives insecure-ok=unset "
            f"schema=unknown",
            self.box.posture.banner())

    def test_tls_is_reported_as_unknown_before_any_request_and_measured_after(self) -> None:
        """An honest unknown, not a default a reader would act on."""
        self.assertEqual("unknown-until-a-request-arrives", self.box.monitor.tls_in_front())
        self.box.call()
        self.assertEqual("not-terminated-in-front", self.box.monitor.tls_in_front())


class AnOpenServerBehindAProxy(unittest.TestCase):
    """Gap #1: loopback-bound, no token, and something relays to it."""

    def setUp(self) -> None:
        self.box = LiveServerPosture(self, bind="127.0.0.1", token=None)

    def test_a_forwarded_for_header_fires_the_alarm(self) -> None:
        status, _ = self.box.call(headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(200, status, "the request is still served -- this warns, never refuses")
        self.assertIn(OPEN_AND_REACHABLE, self.box.firing())
        self.assertTrue(self.box.monitor.report()["alarming"])
        alarmed = [line for line in self.box.lines if OPEN_AND_REACHABLE in line]
        self.assertTrue(alarmed, "the alarm must reach stderr, not only the health payload")
        self.assertIn("ALARM", alarmed[0])
        self.assertIn("X-Forwarded-For", alarmed[0])

    def test_each_of_the_three_documented_headers_fires_it(self) -> None:
        """Named individually: the handoff names three, and a detector that
        happened to read only the one the first test used would pass that test."""
        for header in ("X-Forwarded-For", "X-Forwarded-Proto", "Forwarded"):
            with self.subTest(header=header):
                box = LiveServerPosture(self, bind="127.0.0.1", token=None)
                value = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "http",
                         "Forwarded": "for=203.0.113.9;proto=http"}[header]
                box.call(headers={header: value})
                self.assertIn(OPEN_AND_REACHABLE, box.firing())

    def test_tls_in_front_does_not_make_an_open_server_safe(self) -> None:
        """https to the proxy still reaches an unauthenticated corpus. The two
        alarms are independent and one must not silence the other."""
        self.box.call(headers={"X-Forwarded-For": "203.0.113.9",
                               "X-Forwarded-Proto": "https"})
        self.assertIn(OPEN_AND_REACHABLE, self.box.firing())
        self.assertNotIn(TOKEN_IN_CLEARTEXT, self.box.firing())

    def test_the_health_endpoint_carries_it_so_a_monitor_can_see_it(self) -> None:
        self.box.call(headers={"X-Forwarded-For": "203.0.113.9"})
        status, body = self.box.call("/api/v1/admin/health")
        self.assertEqual(200, status)
        posture = json.loads(body)["posture"]
        self.assertTrue(posture["alarming"])
        firing = [a for a in posture["alarms"] if a["status"] == "firing"]
        self.assertEqual([OPEN_AND_REACHABLE], [a["name"] for a in firing])
        self.assertEqual(1, firing[0]["count"])
        self.assertIn("X-Forwarded-For", firing[0]["evidence"])
        self.assertEqual(posture["banner"], self.box.posture.banner())

    def test_it_keeps_firing_rather_than_being_printed_once_and_forgotten(self) -> None:
        for _ in range(3):
            self.box.call(headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(3, self.box.alarms()[OPEN_AND_REACHABLE]["count"])
        self.assertGreaterEqual(
            len([line for line in self.box.lines if OPEN_AND_REACHABLE in line]), 2)


@unittest.skipIf(LAN is None, "this box has no non-loopback IPv4 address to bind and reach")
class ABearerTokenOnTheWire(unittest.TestCase):
    """Gap #2: a permanent credential over a connection nothing says was TLS.

    Bound to 0.0.0.0 and reached over this machine's real LAN address, so the
    server's peer genuinely is not loopback. That is the only way to exercise
    the half of the detector that does not depend on a header a test wrote.
    """

    def setUp(self) -> None:
        self.box = LiveServerPosture(self, bind="0.0.0.0", token=TOKEN)

    def test_a_token_from_a_non_loopback_peer_over_http_fires(self) -> None:
        status, _ = self.box.call(host=LAN, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(200, status, "the request is still served -- this warns, never refuses")
        self.assertIn(TOKEN_IN_CLEARTEXT, self.box.firing())
        self.assertNotIn(OPEN_AND_REACHABLE, self.box.firing(),
                         "a token IS configured, so the open-server alarm must stay clear")

    def test_tls_in_front_silences_it_without_any_opt_out(self) -> None:
        """The fix is a terminator, and the alarm must recognise the fix --
        otherwise the only way to quiet it is the waiver, and the waiver becomes
        the normal state."""
        self.box.call(host=LAN, headers={"Authorization": f"Bearer {TOKEN}",
                                         "X-Forwarded-Proto": "https"})
        self.assertEqual(set(), self.box.firing())
        self.assertEqual("terminated-in-front", self.box.monitor.tls_in_front())

    def test_a_loopback_request_with_a_token_is_not_a_leak(self) -> None:
        """Those bytes never left the machine. Reporting them would make the
        air-gapped posture permanently alarmed, which is how an alarm dies."""
        self.box.call(headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(set(), self.box.firing())

    def test_a_request_with_no_credential_is_not_a_leak_either(self) -> None:
        status, _ = self.box.call(host=LAN)
        self.assertEqual(401, status)
        self.assertEqual(set(), self.box.firing())

    def test_an_invalid_token_still_counts_as_a_credential_on_the_wire(self) -> None:
        """It is somebody's secret, in clear, and it may be ours mistyped."""
        self.box.call(host=LAN, headers={"Authorization": "Bearer wrong-value"})
        self.assertIn(TOKEN_IN_CLEARTEXT, self.box.firing())

    def test_a_cookie_and_a_query_token_are_channels_too(self) -> None:
        """Three channels carry the credential (docs/ACCESS_CONTROL.md); a
        detector that watched only the Authorization header would miss the one
        an operator actually types into a browser."""
        for label, kwargs in (("cookie", {"headers": {"Cookie": f"mo_token={TOKEN}"}}),
                              ("query", {"path": f"/api/v1/devices?token={TOKEN}"})):
            with self.subTest(channel=label):
                box = LiveServerPosture(self, bind="0.0.0.0", token=TOKEN)
                box.call(host=LAN, **kwargs)
                self.assertIn(TOKEN_IN_CLEARTEXT, box.firing())


class AProxiedBoxWithATokenRaisesOnlyTheCleartextAlarm(unittest.TestCase):
    """Loopback + token + a plain-http proxy. The common real deployment, and
    the one where the two alarms must be told apart."""

    def setUp(self) -> None:
        self.box = LiveServerPosture(self, bind="127.0.0.1", token=TOKEN)

    def test_only_the_cleartext_alarm_fires(self) -> None:
        self.box.call(headers={"Authorization": f"Bearer {TOKEN}",
                               "X-Forwarded-For": "203.0.113.9",
                               "X-Forwarded-Proto": "http"})
        self.assertEqual({TOKEN_IN_CLEARTEXT}, self.box.firing())

    def test_the_banner_reports_the_origin_check_as_active(self) -> None:
        self.assertIn("origin-check=active", self.box.posture.banner())
        self.assertIn("auth=token(", self.box.posture.banner())
        self.assertNotIn(TOKEN, self.box.posture.banner(),
                         "the banner must never carry the value")


class TheOptOutSilencesExactlyOneThing(unittest.TestCase):
    """An operator says "I know" deliberately, about ONE exposure, once."""

    # Two postures, each of which fires exactly one alarm, crossed with the two
    # single-name waivers. The whole point is the OFF-DIAGONAL: waiving one
    # exposure must leave the other one audible, and a waiver implemented as a
    # boolean would pass the diagonal and fail here.
    #
    #                             waive=open          waive=cleartext
    #   open server, proxied      acknowledged        STILL FIRING
    #   token over http proxy     STILL FIRING        acknowledged

    def proxied_open_server(self, acknowledge):
        box = LiveServerPosture(self, bind="127.0.0.1", token=None, acknowledge=acknowledge)
        box.call(headers={"X-Forwarded-For": "203.0.113.9"})
        return box

    def proxied_token_in_clear(self, acknowledge):
        box = LiveServerPosture(self, bind="127.0.0.1", token=TOKEN, acknowledge=acknowledge)
        box.call(headers={"Authorization": f"Bearer {TOKEN}",
                          "X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "http"})
        return box

    def test_waiving_the_cleartext_token_leaves_the_open_server_alarm_audible(self) -> None:
        box = self.proxied_open_server(TOKEN_IN_CLEARTEXT)
        self.assertEqual({OPEN_AND_REACHABLE}, box.firing())
        self.assertTrue(box.monitor.report()["alarming"])
        self.assertTrue(box.alarms()[TOKEN_IN_CLEARTEXT]["waived"])

    def test_waiving_the_open_server_silences_that_one_and_only_that_one(self) -> None:
        box = self.proxied_open_server(OPEN_AND_REACHABLE)
        self.assertEqual(set(), box.firing())
        self.assertEqual("acknowledged", box.alarms()[OPEN_AND_REACHABLE]["status"])
        self.assertFalse(box.monitor.report()["alarming"])
        self.assertFalse(box.alarms()[TOKEN_IN_CLEARTEXT]["waived"])

    def test_waiving_the_open_server_leaves_the_cleartext_alarm_audible(self) -> None:
        box = self.proxied_token_in_clear(OPEN_AND_REACHABLE)
        self.assertEqual({TOKEN_IN_CLEARTEXT}, box.firing())
        self.assertTrue(box.monitor.report()["alarming"])

    def test_waiving_the_cleartext_token_silences_that_one_and_only_that_one(self) -> None:
        box = self.proxied_token_in_clear(TOKEN_IN_CLEARTEXT)
        self.assertEqual(set(), box.firing())
        self.assertEqual("acknowledged", box.alarms()[TOKEN_IN_CLEARTEXT]["status"])
        self.assertFalse(box.monitor.report()["alarming"])

    def test_the_two_alarms_cannot_fire_on_one_request_through_the_server(self) -> None:
        """Not a coincidence worth leaving unstated: with NO token configured
        there is no credential of ours for a client to leak, so `_guarded` reports
        credential_presented=False and the cleartext alarm cannot be reached from
        the open posture. A Bearer header a stranger invented is not our secret,
        and counting it would make the dev machine alarm on any passer-by's
        header."""
        box = LiveServerPosture(self, bind="127.0.0.1", token=None)
        box.call(headers={"Authorization": "Bearer something-a-client-made-up",
                          "X-Forwarded-For": "203.0.113.9"})
        self.assertEqual({OPEN_AND_REACHABLE}, box.firing())
        self.assertEqual(0, box.alarms()[TOKEN_IN_CLEARTEXT]["count"])

    def test_an_acknowledged_condition_is_still_counted_and_dated(self) -> None:
        """Waived is not absent. A monitor must be able to show "known since
        <date>" rather than nothing at all."""
        box = LiveServerPosture(self, bind="127.0.0.1", token=None,
                                acknowledge=OPEN_AND_REACHABLE)
        box.call(headers={"X-Forwarded-For": "203.0.113.9"})
        box.call(headers={"X-Forwarded-For": "203.0.113.9"})
        alarm = box.alarms()[OPEN_AND_REACHABLE]
        self.assertEqual("acknowledged", alarm["status"])
        self.assertEqual(2, alarm["count"])
        self.assertIsNotNone(alarm["first_seen_at"])
        self.assertFalse(box.monitor.report()["alarming"])
        self.assertEqual([], [line for line in box.lines if "ALARM" in line])
        noted = [line for line in box.lines if "posture note:" in line]
        self.assertEqual(1, len(noted),
                         "an acknowledged condition is noted once, never repeated")

    def test_the_blanket_value_waives_both(self) -> None:
        box = LiveServerPosture(self, bind="127.0.0.1", token=None, acknowledge="1")
        box.call(headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(set(), box.firing())
        alarms = box.alarms()
        self.assertTrue(all(a["waived"] for a in alarms.values()))
        # And the two fields stay distinct: a waiver for something that has never
        # happened must not read like something happening and being ignored.
        self.assertEqual("acknowledged", alarms[OPEN_AND_REACHABLE]["status"])
        self.assertEqual("clear", alarms[TOKEN_IN_CLEARTEXT]["status"])
        self.assertEqual(0, alarms[TOKEN_IN_CLEARTEXT]["count"])

    def test_a_misspelt_waiver_waives_nothing_and_says_so(self) -> None:
        """Fail-safe, and legible. The alarm stays armed, and the startup
        announcement names the unrecognised value beside the valid ones."""
        box = LiveServerPosture(self, bind="127.0.0.1", token=None,
                                acknowledge="MOBILE_OBSERVATORY_INSECURE_OK=yes-please")
        box.monitor.announce()
        box.call(headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertIn(OPEN_AND_REACHABLE, box.firing())
        said = "\n".join(box.lines)
        self.assertIn("is not an alarm name", said)
        self.assertIn(OPEN_AND_REACHABLE, said)

    def test_the_waiver_is_recorded_on_disk_and_the_record_is_append_only(self) -> None:
        box = LiveServerPosture(self, bind="127.0.0.1", token=None,
                                acknowledge=OPEN_AND_REACHABLE)
        record = box.data / "insecure-acknowledged.json"
        self.assertTrue(record.is_file(), "saying 'I know' must leave a record")
        first = json.loads(record.read_text())["acknowledgements"]
        self.assertEqual(1, len(first))
        self.assertEqual([OPEN_AND_REACHABLE], first[0]["acknowledged"])
        self.assertIn("at", first[0])
        self.assertIn("hostname", first[0])
        # A second startup asserting it again appends rather than replaces.
        posture_at_startup(data_dir=box.data, host="127.0.0.1", port=1, token=None,
                           token_source="none",
                           environ={INSECURE_OK_ENV_VAR: TOKEN_IN_CLEARTEXT})
        after = json.loads(record.read_text())["acknowledgements"]
        self.assertEqual(2, len(after))
        self.assertEqual(first, after[:1], "an earlier acknowledgement is never rewritten")

    def test_an_unreadable_record_is_reported_and_left_alone(self) -> None:
        """An audit trail a parse error can delete is not an audit trail."""
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            corrupt = data / "insecure-acknowledged.json"
            corrupt.write_text("{ not json")
            report = record_acknowledgement(data, {OPEN_AND_REACHABLE}, bind="127.0.0.1:1")
            self.assertFalse(report["recorded"])
            self.assertIn("could not be read", report["error"])
            self.assertEqual("{ not json", corrupt.read_text())

    def test_removing_the_variable_rearms_the_alarm(self) -> None:
        """The environment is the authority and the file is only the record. A
        waiver that outlived the operator who reasoned about it would be exactly
        the invisible state this whole mechanism replaces."""
        box = LiveServerPosture(self, bind="127.0.0.1", token=None,
                                acknowledge=OPEN_AND_REACHABLE)
        self.assertTrue((box.data / "insecure-acknowledged.json").is_file())
        rearmed = posture_at_startup(data_dir=box.data, host="127.0.0.1", port=1, token=None,
                                     token_source="none", environ={})
        self.assertEqual(frozenset(), rearmed.acknowledged)
        self.assertIn("insecure-ok=unset", rearmed.banner())


class ParsingTheOptOut(unittest.TestCase):
    def test_every_spelling_of_the_blanket_value(self) -> None:
        for value in ("1", "true", "YES", "on", "all"):
            with self.subTest(value=value):
                self.assertEqual(frozenset(ALARMS), parse_acknowledgement(value)[0])

    def test_blank_and_absent_are_the_same_and_waive_nothing(self) -> None:
        for value in (None, "", "   "):
            self.assertEqual((frozenset(), ()), parse_acknowledgement(value))

    def test_several_names_may_be_given_separated_by_comma_or_space(self) -> None:
        for value in (f"{OPEN_AND_REACHABLE},{TOKEN_IN_CLEARTEXT}",
                      f"{OPEN_AND_REACHABLE} {TOKEN_IN_CLEARTEXT}"):
            self.assertEqual(frozenset(ALARMS), parse_acknowledgement(value)[0])

    def test_an_unknown_name_is_returned_rather_than_raised_on(self) -> None:
        waived, unknown = parse_acknowledgement(f"{OPEN_AND_REACHABLE} typo")
        self.assertEqual(frozenset({OPEN_AND_REACHABLE}), waived)
        self.assertEqual(("typo",), unknown)


class MonitorTest(unittest.TestCase):
    """The parts the live-server cases deliberately configure away."""

    def monitor(self, **kwargs):
        lines: list[str] = []
        posture = Posture(bind_host="127.0.0.1", port=1, token_source="none", open=True,
                          acknowledged=frozenset())
        return ExposureMonitor(posture, emit=lines.append, **kwargs), lines

    def test_the_default_backoff_does_not_restate_an_alarm_per_request(self) -> None:
        """Loud means persistent, not per-request. A detector that printed on
        every request would be indistinguishable from the access log it sits in
        and would be filtered out within a day."""
        clock = [0.0]
        monitor, lines = self.monitor(clock=lambda: clock[0])
        for _ in range(50):
            monitor.observe(headers={"X-Forwarded-For": "203.0.113.9"},
                            client_address=("127.0.0.1", 1), credential_presented=False)
        self.assertEqual(1, len(lines))
        self.assertEqual(50, monitor.report()["alarms"][0]["count"])
        clock[0] = 301.0
        monitor.observe(headers={"X-Forwarded-For": "203.0.113.9"},
                        client_address=("127.0.0.1", 1), credential_presented=False)
        self.assertEqual(2, len(lines), "and it must come back, or it is not persistent")

    def test_a_mixed_tls_picture_is_reported_as_mixed_not_as_either(self) -> None:
        monitor, _ = self.monitor()
        monitor.observe(headers={"X-Forwarded-Proto": "https"},
                        client_address=("127.0.0.1", 1), credential_presented=False)
        monitor.observe(headers={"X-Forwarded-Proto": "http"},
                        client_address=("127.0.0.1", 1), credential_presented=False)
        self.assertEqual("mixed(1-of-2-requests-claimed-https)", monitor.tls_in_front())

    def test_only_the_clients_own_hop_decides_whether_tls_was_used(self) -> None:
        """X-Forwarded-Proto is a list when proxies chain, oldest first. Reading
        "any element says https" would bless a plain-http front door with an
        internal https hop behind it -- which is the exposure, not its absence."""
        monitor, _ = self.monitor()
        for header in ({"X-Forwarded-Proto": "http, https"},
                       {"Forwarded": "for=203.0.113.9;proto=http, for=10.0.0.2;proto=https"}):
            with self.subTest(header=header):
                fired = monitor.observe(headers=header, client_address=("127.0.0.1", 1),
                                        credential_presented=True)
                self.assertIn(TOKEN_IN_CLEARTEXT, fired)

    def test_an_unresolvable_peer_counts_as_outside(self) -> None:
        """Same reasoning as access.is_loopback's hostname branch: being wrong
        in that direction disables the detector silently."""
        monitor, _ = self.monitor()
        self.assertIn(OPEN_AND_REACHABLE,
                      monitor.observe(headers={}, client_address=("not-an-address", 1),
                                      credential_presented=False))


class TokenProvenanceTest(unittest.TestCase):
    """The banner says where the credential came from, never what it is."""

    def test_the_three_sources_are_told_apart(self) -> None:
        from mobile_observatory.access import write_token
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            self.assertEqual("none", token_provenance(data, {}))
            self.assertEqual("environment",
                             token_provenance(data, {"MOBILE_OBSERVATORY_TOKEN": "x"}))
            write_token(data)
            self.assertEqual("file", token_provenance(data, {}))

    def test_the_banner_never_contains_the_token_value(self) -> None:
        posture = Posture(bind_host="0.0.0.0", port=8000, token_source="environment",
                          open=False, acknowledged=frozenset())
        self.assertNotIn(TOKEN, posture.banner())
        self.assertIn("token(environment)", posture.banner())


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


class TheOperationsPageShowsIt(unittest.TestCase):
    """The third place the alarm has to reach: in front of a human, during
    normal use.

    stderr is read after an incident and the health endpoint is read by a
    monitor somebody had to configure. The Operations page is where an operator
    already goes to ask whether a run worked.

    This also guards a crash found by opening that page: `renderAdmin()` called
    a `matches()` deleted three commits earlier, so the WHOLE Operations page --
    collector status, corpus invariants, review inbox -- threw a ReferenceError
    and painted nothing, with 570 tests green over it. A panel that only exists
    in a payload is not surfaced.
    """

    def setUp(self) -> None:
        self.box = LiveServerPosture(self, bind="127.0.0.1", token=None)
        self.manager, self.browser = _browser()
        self.addCleanup(self.manager.stop)
        self.addCleanup(self.browser.close)

    def open_admin(self, headers=None):
        page = self.browser.new_page(viewport={"width": 1500, "height": 1200},
                                     extra_http_headers=headers or {})
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{self.box.port}/", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        page.click('[data-route="admin"]')
        page.wait_for_timeout(2500)
        self.addCleanup(page.close)
        return page

    def test_the_operations_page_paints_at_all(self) -> None:
        page = self.open_admin()
        self.assertEqual([], self.errors,
                         "a throw in renderAdmin blanks the entire Operations page")
        self.assertIn("Collection health", page.inner_text("#app"))
        self.assertTrue(page.query_selector_all(".integrity"),
                        "neither the posture panel nor the invariant panel rendered")

    def test_a_firing_alarm_is_on_the_page_with_its_evidence(self) -> None:
        page = self.open_admin({"X-Forwarded-For": "203.0.113.9"})
        text = page.inner_text("#app")
        self.assertIn("deploy exposure detected", text)
        self.assertIn(OPEN_AND_REACHABLE, text)
        self.assertIn("X-Forwarded-For", text, "the evidence, not just the name")
        self.assertIn("exposure=loopback-only", text, "the banner is on the page too")
        self.assertNotIn(TOKEN, text)
        self.assertEqual([], self.errors)

    def test_a_clear_posture_says_what_it_observed_rather_than_nothing(self) -> None:
        """"No alarms" and "nobody asked" must not look alike -- the panel states
        how many requests it saw, so a silent panel cannot pass as a checked one."""
        page = self.open_admin()
        text = page.inner_text("#app")
        self.assertIn("No deploy exposure detected", text)
        self.assertIn("Requests observed:", text)
        self.assertIn("This is not a claim about requests nobody made", text)


if __name__ == "__main__":
    unittest.main()
