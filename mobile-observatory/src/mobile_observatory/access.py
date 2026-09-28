"""Who is allowed to change the corpus, and from where.

The server had no authentication, no authorisation, and no origin checking of
any kind. Fourteen POST routes change state, and one of them --
/api/v1/identity/products/<id>/review -- runs UPDATE ... SET
hardware_model_id=NULL and DELETE FROM product_hardware_links against the
corpus, then replays that decision at every subsequent startup. The only thing
standing in front of them was the default --host of 127.0.0.1, which the
deployment is about to change: this ships to an online box as well as an
air-gapped one.

Measured against the running server before this existed, with a real payload:

    POST /api/v1/watches
    Content-Type: text/plain
    Origin: https://evil.example
    -> HTTP 200, row written

Both headers matter. `text/plain` makes it a CORS-*simple* request, so a
browser sends it cross-site with no preflight to block -- the UI's own
Content-Type of application/json would have been preflighted, but an attacker
does not use the UI's client. And the hostile Origin was simply ignored. That
is a working cross-site request forgery against any operator with the page
open, not a theoretical one.

Two separate controls, because they stop different attacks and either alone
leaves a hole:

  * A token proves *who* you are. It stops an unauthenticated client on the
    network reaching the box at all.
  * An origin check proves *where the request was composed*. It stops a page
    on another site driving the operator's already-authenticated browser --
    which a token, sent automatically by the browser as a cookie, would not.

The open case is deliberate and stays: bound to loopback with no token
configured, everything is allowed. That is the development machine, and making
it demand a credential would only train people to paste one. The guard is that
binding anywhere else without a token is not a quiet downgrade -- see
`token_for_binding`.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

TOKEN_FILENAME = "auth-token"
COOKIE_NAME = "mo_token"
ENV_VAR = "MOBILE_OBSERVATORY_TOKEN"

# Methods that can change state. Everything else is a read.
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_loopback(host: str) -> bool:
    """True when binding `host` exposes nothing beyond this machine.

    The empty string and 0.0.0.0/:: are the all-interfaces wildcards and are
    emphatically NOT loopback -- they are the exact case this module exists to
    catch, and the one an operator is most likely to type.
    """
    if host is None:
        return False
    name = host.strip().strip("[]").lower()
    if name in {"localhost", "localhost.localdomain"}:
        return True
    if name in {"", "*", "0.0.0.0", "::", "::0"}:
        return False
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        # A hostname we cannot resolve here. Refuse to call it loopback: being
        # wrong in that direction silently disables the whole guard.
        return False


def read_token(data_dir: Path, environ: dict | None = None) -> str | None:
    """The configured token, or None when there is none.

    Environment wins over the file so a deployment can hold the secret outside
    the data directory entirely. A blank or whitespace-only value is treated as
    absent rather than as a token of zero length -- `TOKEN=` in a unit file is
    someone clearing it, not someone choosing the empty password.
    """
    environ = os.environ if environ is None else environ
    from_env = (environ.get(ENV_VAR) or "").strip()
    if from_env:
        return from_env
    path = Path(data_dir) / TOKEN_FILENAME
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def write_token(data_dir: Path) -> str:
    """Generate a token, store it readable only by this user, return it.

    0600 before the write, not after: a token that exists world-readable for
    even a moment has been disclosed, and on a shared box that moment is
    enough.
    """
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / TOKEN_FILENAME
    token = secrets.token_urlsafe(32)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")
    os.chmod(path, 0o600)
    return token


def token_for_binding(data_dir: Path, host: str, environ: dict | None = None) -> tuple[str | None, str]:
    """Resolve the token for a server about to bind `host`.

    Returns (token, note). On loopback with nothing configured the answer is
    (None, ...) and the server runs open -- that is the development case.

    Binding anywhere else with nothing configured MINTS one rather than
    starting open. Refusing to start would be the other defensible choice, but
    this ships to an air-gapped box where a refusal at 3am is expensive and a
    generated credential is not; and the failure mode of starting open on a
    reachable interface is the whole reason this module exists. The note names
    the FILE, never the value -- returning it for printing would put a live
    credential into terminal scrollback, journald and any CI log that captured
    the startup.
    """
    existing = read_token(data_dir, environ)
    if existing:
        return existing, "using the configured access token"
    if is_loopback(host):
        return None, "loopback with no token: running open, no credential required"
    token = write_token(data_dir)
    return token, (
        f"bound to {host}, which is reachable beyond this machine, and no access token "
        f"was configured. Generated one at {Path(data_dir) / TOKEN_FILENAME} (mode 0600). "
        f"Open the UI once as  http://{host}:<port>/?token=<the value in that file>  "
        f"to store it, or send it as  Authorization: Bearer <value>."
    )


def _host_of(url: str) -> str | None:
    """The scheme://host:port of an Origin or Referer, lowercased.

    Compared as a whole rather than by hostname alone: http://box and
    https://box are different origins to a browser, and so are two ports.
    """
    if not url:
        return None
    value = url.strip()
    if value == "null":          # sandboxed iframe, data: document -- not our page
        return "null"
    if "://" not in value:
        return None
    scheme, _, rest = value.partition("://")
    authority = rest.split("/", 1)[0]
    if not authority:
        return None
    return f"{scheme.lower()}://{authority.lower()}"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    status: int = 200
    reason: str = ""


ALLOWED = Decision(True)


class AccessPolicy:
    """Decides one request. Holds no per-request state, so it is shared safely
    across the server's threads."""

    def __init__(self, token: str | None) -> None:
        self._token = token

    @property
    def open(self) -> bool:
        """True when no credential is configured and everything is permitted."""
        return self._token is None

    def token_matches(self, candidate: str | None) -> bool:
        """Constant-time compare. A plain == leaks the token's prefix through
        timing, which matters precisely because this value never rotates."""
        if self._token is None or not candidate:
            return False
        return hmac.compare_digest(candidate, self._token)

    def presented(self, *, authorization: str | None, cookie: str | None,
                  query_token: str | None) -> str | None:
        """The token this request carries, from whichever channel supplied it."""
        if authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() == "bearer" and value.strip():
                return value.strip()
        if cookie:
            for part in cookie.split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE_NAME and value:
                    return value
        if query_token:
            return query_token
        return None

    def decide(self, *, method: str, authorization: str | None = None,
               cookie: str | None = None, query_token: str | None = None,
               origin: str | None = None, referer: str | None = None,
               host: str | None = None, secure: bool = False) -> Decision:
        """Allow or refuse, and say which control refused.

        Order matters: authentication first, then origin. A caller with no
        token gets 401 whatever its Origin says, so a cross-site probe cannot
        use the difference between 401 and 403 to learn whether it holds a
        valid session.
        """
        if self.open:
            return ALLOWED

        if not self.token_matches(self.presented(
                authorization=authorization, cookie=cookie, query_token=query_token)):
            return Decision(False, 401, "no valid access token")

        if method.upper() not in MUTATING:
            return ALLOWED

        # The CSRF control. A browser always sends Origin on a cross-origin
        # request, so its ABSENCE means this is not a cross-site browser
        # request -- curl, a script, the batch. Those already had to present a
        # token above. Its PRESENCE and mismatch is the attack.
        stated = _host_of(origin) or _host_of(referer)
        if stated is None:
            return ALLOWED
        expected = f"{'https' if secure else 'http'}://{(host or '').lower()}"
        if stated != expected:
            return Decision(False, 403,
                            "cross-origin write refused: this request was composed on "
                            "another site")
        return ALLOWED
