"""Is this server exposed in a way nobody meant -- and would anybody know?

docs/ACCESS_CONTROL.md names two deploy-time exposures and calls them
un-closeable in code:

  * a loopback-bound server behind a reverse proxy or an SSH tunnel is remotely
    reachable while still counting as "open" -- no token, every route allowed;
  * plain HTTP exposes a permanent bearer token to everything on the path.

Neither is closeable from inside this process: the proxy is somebody else's
config file and the TLS terminator is somebody else's daemon. But "cannot be
fixed here" and "cannot be noticed here" are different claims, and only the
second one was true. Both exposures leave evidence IN THE REQUEST:

  * something in front rewrote the request, so it arrives carrying
    X-Forwarded-For / X-Forwarded-Proto / X-Forwarded-Host / Forwarded;
  * or the peer is not loopback, so the bytes crossed a network to get here;
  * and nothing on the request says the hop in front spoke TLS (no
    `proto=https`), so any credential it carried travelled in clear.

This module therefore does not try to close the gaps. It DETECTS them, from
real traffic, and makes the unsafe state loud -- on stderr for a human, and in
/api/v1/admin/health for a monitor that nobody has to remember to tail.

WHY IT WARNS AND NEVER REFUSES
------------------------------
A decision, not an omission:

 1. **Neither condition is knowable at startup.** Both are properties of a
    REQUEST. A startup refusal cannot see them at all, so "refuse" could only
    mean refusing mid-run -- on the thousandth request, having already served
    the first 999 under the exposure it is refusing over.
 2. **The trigger is attacker-controlled.** `X-Forwarded-For: 1.2.3.4` is one
    header any client can send. A server that exits on it hands every stranger
    an off switch: a detector turned into a denial of service, which is a
    strictly worse security property than the one it was added to fix.
 3. **A reverse proxy in front is a legitimate deployment.** What is wrong is
    the COMBINATION (reachable + no credential), and the repair is a token in
    the unit file, applied by a human in daylight. Killing the service at 3am
    does not apply it; it only guarantees the next operator disables the check.

Refusing is right where the state is known before the first byte is served and
is unambiguous -- and `access.token_for_binding` already holds that ground: a
non-loopback bind with no token mints a credential rather than serving open.
This module is the other half, the half only traffic can reveal.

SAYING "I KNOW", ONCE
---------------------
`MOBILE_OBSERVATORY_INSECURE_OK` is the opt-out, and it takes ALARM NAMES
rather than a blanket boolean, so acknowledging the cleartext-token reality of
an air-gapped LAN box does not also silence "this open server is reachable from
the internet". `1`/`true`/`yes`/`all` still waive everything, for the operator
who means exactly that.

The environment variable is the authority and the file
`<data-dir>/insecure-acknowledged.json` is the RECORD, append-only: every
startup that asserts a waiver adds an entry (when, which alarms, what bind,
which host, which pid). Deliberately that way round. A waiver that lived only
in the file would survive the operator who set it, the box it was reasoned
about and the exposure it was reasoned about -- and a waiver you cannot see is
how this gap got shipped in the first place. Removing the variable re-arms the
alarm; the file still says it was once waived, and by whom, and when.

An acknowledged condition is still DETECTED and still counted. It stops being
an ALARM and becomes a reported, dated, waived fact in
/api/v1/admin/health -- which is what makes the alarm actionable instead of
something people learn to scroll past.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .access import ENV_VAR as TOKEN_ENV_VAR
from .access import TOKEN_FILENAME, is_loopback

INSECURE_OK_ENV_VAR = "MOBILE_OBSERVATORY_INSECURE_OK"
ACKNOWLEDGEMENT_FILENAME = "insecure-acknowledged.json"

# Headers that only something IN FRONT of us writes. Any one of them means this
# request was relayed, so our own socket's peer address is not the client's and
# a loopback bind is not a loopback exposure.
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host", "Forwarded")

OPEN_AND_REACHABLE = "open_server_is_reachable_from_outside"
TOKEN_IN_CLEARTEXT = "access_token_travelled_in_cleartext"
ALARMS = (OPEN_AND_REACHABLE, TOKEN_IN_CLEARTEXT)

ALARM_DETAIL = {
    OPEN_AND_REACHABLE: (
        "no access token is configured, so every route including the fifteen that write "
        "the corpus is allowed -- and this request did not originate on this machine. A "
        "loopback bind is not a loopback exposure once something relays to it. Set "
        f"{TOKEN_ENV_VAR} and restart"),
    TOKEN_IN_CLEARTEXT: (
        "a request presented an access token over a connection nothing says was "
        "TLS-protected (no X-Forwarded-Proto: https, no Forwarded proto=https) and that "
        "did not originate on this machine. This token never rotates, so one capture of "
        "one request is permanent access. Terminate TLS in front and have it set "
        "X-Forwarded-Proto"),
}

# How often a still-firing alarm is re-stated on stderr. Loud has to mean
# PERSISTENT -- a line printed once, at the top of a log that then gets ten
# thousand access lines, has been printed and not communicated. 300s is quiet
# enough that it cannot be mistaken for per-request noise and frequent enough
# that any window of the log contains it.
REALARM_SECONDS = 300.0

_UNSET = object()


# ---------------------------------------------------------------------------
# reading the evidence off one request
# ---------------------------------------------------------------------------

def _header(headers, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    return getter(name) if getter is not None else None


def proxy_headers_present(headers) -> tuple[str, ...]:
    """Which relay headers this request carries. Empty means nobody relayed it."""
    return tuple(name for name in PROXY_HEADERS if (_header(headers, name) or "").strip())


def _first_element(value: str) -> str:
    # X-Forwarded-Proto and Forwarded are comma-separated LISTS when several
    # proxies each append their hop, oldest first. The CLIENT's hop is the first
    # element and it is the only one that says anything about the leg the
    # credential crossed in public. Reading "any element says https" would read
    # an internal https hop behind a plain-http front door as safe -- which is
    # the exposure, not its absence.
    return value.split(",")[0].strip()


def tls_terminated_in_front(headers) -> bool:
    """True when the hop in front says it spoke TLS to the client.

    Absence is not proof of cleartext -- a proxy can be misconfigured to not
    forward this -- which is why the alarm it feeds says "nothing says this was
    TLS-protected" rather than "this was plaintext". An unprovable claim of
    safety is treated as unproven, because the cost of the two mistakes is not
    symmetric: a false alarm costs one line of config, a missed one costs a
    permanent credential.
    """
    proto = _first_element((_header(headers, "X-Forwarded-Proto") or "").strip().lower())
    if proto == "https":
        return True
    forwarded = (_header(headers, "Forwarded") or "").strip()
    if forwarded:
        for part in _first_element(forwarded).split(";"):
            key, _, value = part.strip().partition("=")
            if key.strip().lower() == "proto":
                return value.strip().strip('"').lower() == "https"
    return False


def peer_is_loopback(client_address) -> bool:
    """True when the socket's peer is this machine.

    Unknown counts as NOT loopback. Being wrong in the other direction silently
    disables the whole detector, which is the same reasoning as
    access.is_loopback's hostname branch.
    """
    if not client_address:
        return False
    host = client_address[0] if isinstance(client_address, (tuple, list)) else client_address
    try:
        return ipaddress.ip_address(str(host).strip().strip("[]")).is_loopback
    except ValueError:
        return False


def request_came_from_outside(*, headers, client_address) -> tuple[bool, str]:
    """Did this request reach us from beyond this machine? With the reason.

    Two independent signals, OR-ed, because they catch different deployments:
    a relay header catches the proxy/tunnel on localhost (where the peer IS
    loopback and the client is not), and a non-loopback peer catches the direct
    LAN or internet client (which sends no relay header at all). Either alone
    misses half of what this module exists to see.
    """
    relayed = proxy_headers_present(headers)
    if relayed:
        return True, f"relayed by something in front ({', '.join(relayed)} present)"
    if not peer_is_loopback(client_address):
        host = client_address[0] if isinstance(client_address, (tuple, list)) else client_address
        return True, f"the connection's peer {host} is not loopback"
    return False, "loopback peer, no relay headers: this request never left the machine"


# ---------------------------------------------------------------------------
# the opt-out
# ---------------------------------------------------------------------------

def parse_acknowledgement(value: str | None) -> tuple[frozenset[str], tuple[str, ...]]:
    """Split MOBILE_OBSERVATORY_INSECURE_OK into (acknowledged, unrecognised).

    Unrecognised values are RETURNED rather than raised on. A typo waives
    nothing, so the alarm stays armed and the failure is already in the safe
    direction -- but it is reported by name beside the valid ones, because an
    operator who believes they acknowledged and did not would otherwise learn it
    only from an alarm that gives no hint their waiver was misspelt. Refusing to
    start over a typo in a WAIVER would take a working server down for the one
    kind of misconfiguration that cannot make it less safe.
    """
    text = (value or "").strip()
    if not text:
        return frozenset(), ()
    tokens = [t for t in text.replace(",", " ").split() if t]
    if any(t.lower() in {"1", "true", "yes", "on", "all"} for t in tokens):
        return frozenset(ALARMS), tuple(
            t for t in tokens if t not in ALARMS and t.lower() not in {"1", "true", "yes", "on", "all"})
    return frozenset(t for t in tokens if t in ALARMS), tuple(t for t in tokens if t not in ALARMS)


def record_acknowledgement(data_dir: Path, names, *, bind: str, now=None) -> dict:
    """Append this startup's waiver to the append-only record. Returns a report.

    Never raises into startup. A corrupt or unwritable record is reported (and
    the waiver still takes effect, because the ENVIRONMENT is the authority) --
    and the existing bytes are left exactly where they are rather than
    overwritten, because an audit trail that a parse error can delete is not one.
    """
    path = Path(data_dir) / ACKNOWLEDGEMENT_FILENAME
    report: dict = {"record": str(path), "recorded": False, "first_acknowledged_at": None,
                    "error": None}
    stamp = (now or (lambda: datetime.now(timezone.utc)))().strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {"at": stamp, "acknowledged": sorted(names), "bind": bind,
             "hostname": socket.gethostname(), "pid": os.getpid()}
    existing: list = []
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            existing = list(loaded.get("acknowledgements") or [])
        except (ValueError, OSError, AttributeError) as exc:
            report["error"] = (f"{path} exists but could not be read as an acknowledgement "
                               f"record ({type(exc).__name__}: {exc}); it was left untouched "
                               f"and this waiver is NOT recorded")
            return report
    earlier = [e for e in existing
               if isinstance(e, dict) and set(names) & set(e.get("acknowledged") or [])]
    report["first_acknowledged_at"] = (earlier[0].get("at") if earlier else stamp)
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"acknowledgements": existing + [entry]}, indent=2) + "\n",
                        encoding="utf-8")
        report["recorded"] = True
    except OSError as exc:
        report["error"] = f"could not write {path}: {exc}"
    return report


def token_provenance(data_dir: Path, environ: dict | None = None) -> str:
    """Where a token would come from -- 'environment', 'file' or 'none'.

    Read BEFORE token_for_binding, which is what mints one: 'none' before and a
    token after means this startup minted it, and that is a materially different
    posture from a token a deployment chose.
    """
    environ = os.environ if environ is None else environ
    if (environ.get(TOKEN_ENV_VAR) or "").strip():
        return "environment"
    return "file" if (Path(data_dir) / TOKEN_FILENAME).is_file() else "none"


# ---------------------------------------------------------------------------
# the posture, as one line
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Posture:
    """Everything about this server's exposure that is knowable at startup."""

    bind_host: str
    port: int | None
    token_source: str                 # environment | file | minted file | none
    open: bool                        # no credential required of anybody
    acknowledged: frozenset
    unrecognised_acknowledgements: tuple = ()
    acknowledgement: dict | None = None
    # What this startup did to somebody's schema. Here rather than only on
    # stdout because "the server quietly carried my corpus forward 25 versions"
    # is a question asked AFTER the fact, when the printed line has scrolled
    # away -- and because a monitor can then see it.
    schema_version: int | None = None
    migrations_applied: tuple = ()

    @property
    def exposure(self) -> str:
        host = (self.bind_host or "").strip()
        if host.strip("[]").lower() in {"", "*", "0.0.0.0", "::", "::0"}:
            return "all-interfaces"
        return "loopback-only" if is_loopback(host) else f"bound-to-{host}"

    @property
    def origin_check(self) -> str:
        # The CSRF control only runs when a token is configured: see
        # AccessPolicy.decide, which returns ALLOWED before reaching it when
        # open. So "open" and "no origin check" are one state, not two.
        return "inactive-because-open" if self.open else "active"

    def banner(self) -> str:
        """The effective posture, one line, no secrets.

        `tls-in-front` is honestly unknown here and says so rather than
        defaulting to a word a reader would act on. It is a property of a
        REQUEST -- nothing in this process can see a terminator that has not
        relayed anything yet -- and /api/v1/admin/health reports it as measured
        once traffic exists. A banner that guessed "no" would cry wolf on every
        correctly proxied box; one that guessed "yes" would bless every
        cleartext one.
        """
        auth = "open(no token)" if self.open else f"token({self.token_source})"
        if self.acknowledged:
            waived = f"acknowledged[{','.join(sorted(self.acknowledged))}]"
        else:
            waived = "unset"
        schema = ("schema=unknown" if self.schema_version is None
                  else f"schema={self.schema_version}"
                       + (f"(migrated+{len(self.migrations_applied)}-this-startup)"
                          if self.migrations_applied else ""))
        return (f"posture: bind={self.bind_host}:{self.port if self.port is not None else '?'}"
                f" exposure={self.exposure}"
                f" auth={auth}"
                f" origin-check={self.origin_check}"
                f" tls-in-front=unknown-until-a-request-arrives"
                f" insecure-ok={waived}"
                f" {schema}")


def posture_at_startup(*, data_dir: Path, host: str, port: int | None, token: str | None,
                       token_source: str, environ: dict | None = None,
                       write_record: bool = True, schema_version: int | None = None,
                       migrations_applied=()) -> Posture:
    """Resolve the startup posture and record any waiver it asserts."""
    environ = os.environ if environ is None else environ
    acknowledged, unrecognised = parse_acknowledgement(environ.get(INSECURE_OK_ENV_VAR))
    record = None
    if acknowledged and write_record:
        record = record_acknowledgement(data_dir, acknowledged, bind=f"{host}:{port}")
    return Posture(bind_host=host, port=port, token_source=token_source, open=token is None,
                   acknowledged=acknowledged, unrecognised_acknowledgements=unrecognised,
                   acknowledgement=record, schema_version=schema_version,
                   migrations_applied=tuple(migrations_applied or ()))


# ---------------------------------------------------------------------------
# the runtime detector
# ---------------------------------------------------------------------------

class ExposureMonitor:
    """Watches real requests for the two exposures, and reports them.

    Holds counters behind a lock because ThreadingHTTPServer calls observe()
    from every connection's thread at once. Holds no per-request state, so one
    instance serves the whole process.
    """

    def __init__(self, posture: Posture, *, emit=None, clock=time.monotonic,
                 wall_clock=None, realarm_seconds: float = REALARM_SECONDS) -> None:
        self.posture = posture
        self._emit = emit if emit is not None else _emit_to_stderr
        self._clock = clock
        self._wall = wall_clock or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self._realarm = realarm_seconds
        self._lock = threading.Lock()
        self._requests = 0
        self._relayed = 0
        self._tls = 0
        self._state = {name: {"count": 0, "first": None, "last": None, "example": None,
                              "emitted_at": None, "noted": False} for name in ALARMS}

    # -- observation ------------------------------------------------------

    def observe(self, *, headers, client_address, credential_presented: bool) -> tuple[str, ...]:
        """Record one request. Returns the alarm names it triggered.

        Called from the handler's single guarded chokepoint, so a verb added
        without touching it is a verb this cannot see -- which is why it lives
        beside the `_guarded` comment that says the same thing about answering.
        """
        outside, why = request_came_from_outside(headers=headers, client_address=client_address)
        secure = tls_terminated_in_front(headers)
        relayed = bool(proxy_headers_present(headers))
        triggered: list[str] = []
        if outside and self.posture.open:
            triggered.append(OPEN_AND_REACHABLE)
        if outside and credential_presented and not secure:
            triggered.append(TOKEN_IN_CLEARTEXT)
        with self._lock:
            self._requests += 1
            self._relayed += 1 if relayed else 0
            self._tls += 1 if secure else 0
            pending = [self._tick(name, why) for name in triggered]
        for name, detail, count, since, acknowledged in pending:
            if detail is None:
                continue
            if acknowledged:
                self._emit(f"posture note: {name} occurred and is acknowledged via "
                           f"{INSECURE_OK_ENV_VAR}: {detail}. {self._ack_provenance()}")
            else:
                self._emit(f"ALARM posture {name}: {ALARM_DETAIL[name]}. Evidence: {detail}. "
                           f"Seen {count} time(s) since {since}. If this deployment is "
                           f"intended, acknowledge it deliberately and once with "
                           f"{INSECURE_OK_ENV_VAR}={name} -- see docs/ACCESS_CONTROL.md")
        return tuple(triggered)

    def _tick(self, name: str, why: str):
        """Count one occurrence; decide whether it is this one's turn to speak.

        Under the lock. Returns (name, detail-or-None, count, since, acknowledged)
        and the caller emits OUTSIDE the lock, so a slow or blocking stderr
        cannot serialise every request in the process behind one write.
        """
        state = self._state[name]
        now = self._clock()
        stamp = self._wall()
        state["count"] += 1
        state["last"] = stamp
        if state["first"] is None:
            state["first"] = stamp
        state["example"] = why
        acknowledged = name in self.posture.acknowledged
        if acknowledged:
            # Once, ever. The waiver was deliberate; repeating it would train the
            # reader to skip the lines around it, including the unwaived one.
            if state["noted"]:
                return (name, None, state["count"], state["first"], True)
            state["noted"] = True
            return (name, why, state["count"], state["first"], True)
        last = state["emitted_at"]
        if last is not None and (now - last) < self._realarm:
            return (name, None, state["count"], state["first"], False)
        state["emitted_at"] = now
        return (name, why, state["count"], state["first"], False)

    def _ack_provenance(self) -> str:
        record = self.posture.acknowledgement or {}
        if record.get("error"):
            return f"The acknowledgement could not be recorded: {record['error']}"
        if record.get("recorded"):
            return (f"First acknowledged {record.get('first_acknowledged_at')}, recorded in "
                    f"{record.get('record')}")
        return "No acknowledgement record was written."

    # -- reporting --------------------------------------------------------

    def tls_in_front(self) -> str:
        """What traffic has said about a terminator in front, as measured."""
        with self._lock:
            requests, tls = self._requests, self._tls
        if not requests:
            return "unknown-until-a-request-arrives"
        if tls == requests:
            return "terminated-in-front"
        if tls == 0:
            return "not-terminated-in-front"
        return f"mixed({tls}-of-{requests}-requests-claimed-https)"

    def report(self) -> dict:
        """The posture as JSON, for /api/v1/admin/health.

        `alarming` is the one boolean a monitoring check can be pointed at. It
        counts only UNACKNOWLEDGED alarms that have actually fired: an
        acknowledged one is still listed, still counted and still dated, so a
        waiver reads as a waiver rather than as the absence of a problem.

        `status` and `waived` are two fields because they are two facts.
        `status` describes what the traffic DID -- clear, firing, or occurring
        under a waiver -- and `waived` describes what the operator SAID. Folding
        them into one value made a waiver for a condition that has never once
        happened read identically to a condition happening and being ignored.
        """
        with self._lock:
            snapshot = {name: dict(state) for name, state in self._state.items()}
            requests, relayed, tls = self._requests, self._relayed, self._tls
        alarms = []
        for name in ALARMS:
            state = snapshot[name]
            acknowledged = name in self.posture.acknowledged
            alarms.append({
                "name": name,
                "status": ("clear" if not state["count"]
                           else ("acknowledged" if acknowledged else "firing")),
                "waived": acknowledged,
                "count": state["count"],
                "first_seen_at": state["first"],
                "last_seen_at": state["last"],
                "evidence": state["example"],
                "detail": ALARM_DETAIL[name],
            })
        record = self.posture.acknowledgement or {}
        return {
            "banner": self.posture.banner(),
            "bind": f"{self.posture.bind_host}:{self.posture.port}",
            "exposure": self.posture.exposure,
            "auth": "open" if self.posture.open else "token",
            "token_source": self.posture.token_source,
            "origin_check": self.posture.origin_check,
            "schema_version": self.posture.schema_version,
            "migrations_applied_at_startup": list(self.posture.migrations_applied),
            "tls_in_front": self.tls_in_front(),
            "requests_observed": requests,
            "requests_relayed_by_a_proxy": relayed,
            "requests_claiming_tls_in_front": tls,
            "insecure_ok": {
                "env_var": INSECURE_OK_ENV_VAR,
                "acknowledged": sorted(self.posture.acknowledged),
                "unrecognised": list(self.posture.unrecognised_acknowledgements),
                "valid_values": list(ALARMS),
                "record": record.get("record"),
                "recorded": record.get("recorded", False),
                "first_acknowledged_at": record.get("first_acknowledged_at"),
                "record_error": record.get("error"),
            },
            "alarms": alarms,
            "alarming": any(a["status"] == "firing" for a in alarms),
        }

    # -- startup ----------------------------------------------------------

    def announce(self) -> None:
        """Print the banner, and anything about the waiver a reader must know."""
        self._emit(self.posture.banner())
        unrecognised = self.posture.unrecognised_acknowledgements
        if unrecognised:
            self._emit(f"ALARM posture {INSECURE_OK_ENV_VAR} names "
                       f"{', '.join(unrecognised)}, which is not an alarm name. Nothing was "
                       f"waived by it and the alarms below remain armed. Valid values: "
                       f"{', '.join(ALARMS)} (or 1/all for every one)")
        record = self.posture.acknowledgement
        if record and record.get("error"):
            self._emit(f"ALARM posture the insecure-ok acknowledgement could not be recorded: "
                       f"{record['error']}")
        elif record and record.get("recorded"):
            self._emit(f"  posture: waiver recorded in {record['record']} "
                       f"(first acknowledged {record['first_acknowledged_at']})")


def _emit_to_stderr(line: str) -> None:
    # stderr, and flushed. stdout here is block-buffered under nohup/systemd --
    # measured: the whole startup banner was absent from a backgrounded run's log
    # (see server.main's note) -- and an alarm that reaches the log only when the
    # process exits is an alarm about a server that is no longer exposed.
    print(line, file=sys.stderr, flush=True)
