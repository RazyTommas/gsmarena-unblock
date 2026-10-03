# Access control

The server had none until 2026-09-28: no authentication, no authorisation, no
origin checking. Fifteen POST routes change state, and one of them --
`/api/v1/identity/products/<id>/review` -- runs `UPDATE
product_firmware_releases SET hardware_model_id=NULL` and `DELETE FROM
product_hardware_links` against the corpus, then replays that decision at every
subsequent startup. The only thing in front of them was the default `--host` of
`127.0.0.1`.

This was demonstrated against the running server, with a real device id:

```
POST /api/v1/watches
Content-Type: text/plain
Origin: https://evil.example
-> HTTP 200, row written
```

Both headers mattered. `text/plain` makes it a CORS-*simple* request, so a
browser will send it cross-site with no preflight to block. (The UI's own
`application/json` would have been preflighted -- but an attacker does not use
the UI's client.) The hostile `Origin` was ignored outright. That is a working
cross-site request forgery against any operator with the page open.

## The two controls

They stop different attacks, and either alone leaves a hole.

| Control | Proves | Stops |
| --- | --- | --- |
| Access token | *who* you are | an unauthenticated client on the network |
| Origin check | *where the request was composed* | another site driving the operator's already-authenticated browser |

A token alone is not enough for writes, because the browser sends it
automatically as a cookie -- which is exactly what CSRF abuses.

## Running it

**On loopback with no token configured, everything is allowed.** That is the
development machine, and making it demand a credential would only train people
to paste one.

**Binding anywhere else is not a quiet downgrade.** If `--host` is not
loopback (`0.0.0.0`, `::`, a LAN address, a hostname) and no token is
configured, the server mints one into `<data-dir>/auth-token` at mode 0600 and
prints the *path* at startup. It never prints the value: that would put a live
credential into terminal scrollback and journald.

Configure one explicitly instead with the environment, which keeps the secret
out of the data directory entirely:

```sh
MOBILE_OBSERVATORY_TOKEN=... PYTHONPATH=src python3 -m mobile_observatory.server --host 0.0.0.0
```

The environment wins over the file. A blank value (`MOBILE_OBSERVATORY_TOKEN=`)
means *absent*, not a token of zero length -- that is someone clearing it, not
someone choosing the empty password.

## Presenting the token

Three channels, checked in this order:

1. `Authorization: Bearer <token>` -- scripts, the batch, monitoring.
2. Cookie `mo_token` -- what the browser uses after step 3.
3. `?token=<token>` on a GET -- the one-time operator link.

The third exists because an operator has a token in a file and a browser that
cannot send a header by typing a URL. Opening

```
http://your-box:8000/?token=<the value in auth-token>
```

stores it as an `HttpOnly; SameSite=Strict` cookie and **redirects to the same
path without the parameter** -- which keeps the credential out of the address
bar, the history, and any `Referer` the page later sends.

## What is gated

Everything, including static files, when a token is configured. A page served
to an unauthenticated browser is a page that can then be scripted; on a
reachable interface this is not a public read-only mirror.

Writes are additionally origin-checked. `Origin` is compared whole --
scheme, host *and* port -- because `http://box` and `https://box` are different
origins to a browser, and so are two ports. `Origin: null` (sandboxed iframe,
`data:` document) is refused. If `Origin` is absent, `Referer` is used. If
neither is present the write is allowed, because a browser *always* sends
`Origin` on a cross-origin request -- its absence means the caller is not a
cross-site browser request, and it has already presented a token.

Authentication is checked **before** origin, so `401` vs `403` cannot tell a
cross-site probe whether it already holds a valid session.

## The two deploy-time exposures, and how you now find out about them

Two exposures cannot be closed from inside this process, and the earlier version
of this document stopped there. That was half an answer: *un-closeable* and
*un-noticeable* are different problems, and only the second one was true.

| Exposure | Why code cannot close it | What now detects it |
| --- | --- | --- |
| A loopback-bound server behind a reverse proxy or an SSH tunnel is remotely reachable while still counting as "open" | The proxy is somebody else's config file; this process only sees `--host 127.0.0.1` | A request arriving with `X-Forwarded-For` / `X-Forwarded-Proto` / `X-Forwarded-Host` / `Forwarded`, or from a non-loopback peer, while no token is configured |
| Plain HTTP exposes a permanent bearer token | The TLS terminator is somebody else's daemon | A request presenting a credential, from beyond this machine, with nothing saying the hop in front spoke TLS |

Both are reported as named alarms: `open_server_is_reachable_from_outside` and
`access_token_travelled_in_cleartext`. They go to **stderr** with an `ALARM`
prefix, re-stated at most every five minutes while still true, and to
**`/api/v1/admin/health`** under `posture`, where `posture.alarming` is the one
boolean a monitoring check can be pointed at. `src/mobile_observatory/posture.py`
has the argument; `tests/test_posture_alarms.py` drives a real server on a
non-loopback bind to prove each one fires and each opt-out silences exactly one
thing.

### It warns and never refuses, deliberately

1. **Neither condition is knowable at startup.** Both are properties of a
   REQUEST. A startup refusal cannot see them, so "refuse" could only mean
   refusing mid-run -- on the thousandth request, having served 999 under the
   exposure it is refusing over.
2. **The trigger is attacker-controlled.** `X-Forwarded-For: 1.2.3.4` is one
   header any client can send. A server that exits on it hands every stranger an
   off switch: a detector turned into a denial of service, which is strictly
   worse than what it was added to fix.
3. **A proxy in front is a legitimate deployment.** What is wrong is the
   combination, and the repair is a token in the unit file, applied by a human in
   daylight. Killing the service at 3am does not apply it.

Refusing is right where the state is known before the first byte and is
unambiguous -- which `token_for_binding` already covers: a non-loopback bind with
no token mints a credential rather than serving open.

### The startup banner

One line, on stderr, every start. It never carries the token's value.

```
posture: bind=0.0.0.0:8000 exposure=all-interfaces auth=token(environment) origin-check=active tls-in-front=unknown-until-a-request-arrives insecure-ok=unset
```

`tls-in-front` is honestly unknown at startup and says so rather than defaulting
to a word a reader would act on: nothing in this process can see a terminator
that has not relayed anything yet. `/api/v1/admin/health` reports it as measured
once traffic exists -- `terminated-in-front`, `not-terminated-in-front`, or
`mixed(N-of-M-requests-claimed-https)`.

`auth` names where the credential came from and never what it is:
`token(environment)`, `token(file)`, `token(minted file)` (this process generated
it because nobody had), or `open(no token)`.

### Saying "I know", once

`MOBILE_OBSERVATORY_INSECURE_OK` takes **alarm names**, not a boolean, so
acknowledging the cleartext reality of an air-gapped LAN box does not also
silence "this open server is reachable from the internet":

```sh
MOBILE_OBSERVATORY_INSECURE_OK=access_token_travelled_in_cleartext
MOBILE_OBSERVATORY_INSECURE_OK=open_server_is_reachable_from_outside,access_token_travelled_in_cleartext
MOBILE_OBSERVATORY_INSECURE_OK=1        # or true/yes/on/all -- every alarm
```

The **environment is the authority** and `<data-dir>/insecure-acknowledged.json`
is the **record**, append-only: every startup that asserts a waiver adds an entry
with the time, the alarm names, the bind, the hostname and the pid, and the
startup line says where it was written and when the waiver was first asserted.
That way round on purpose -- a waiver that lived only in the file would outlive
the operator who reasoned about it, the box, and the exposure, and a waiver
nobody can see is how this gap shipped. Removing the variable re-arms the alarm;
the file still says it was once waived.

An acknowledged condition is still detected and still counted. In the health
payload it reads `status: acknowledged`, with its count and first-seen date, and
`waived: true` -- two fields because they are two facts: what the traffic did,
and what the operator said. A misspelt value waives nothing (fail-safe) and the
startup line names it beside the valid ones.

### What fires in which posture

Measured against a real server on a copy of the live corpus, 2026-10-03.

| bind | token | the request | `open_…reachable` | `…token…cleartext` |
| --- | --- | --- | --- | --- |
| `127.0.0.1` | none | loopback peer, no relay headers | clear | clear |
| `127.0.0.1` | none | `X-Forwarded-For` present | **FIRING** | clear |
| `127.0.0.1` | none | `X-Forwarded-For` + `X-Forwarded-Proto: https` | **FIRING** | clear |
| `0.0.0.0` | minted | LAN peer, no credential | clear | clear |
| `0.0.0.0` | env | loopback peer + Bearer | clear | clear |
| `0.0.0.0` | env | LAN peer + Bearer, no TLS evidence | clear | **FIRING** |
| `0.0.0.0` | env | LAN peer + Bearer + `X-Forwarded-Proto: https` | clear | clear |
| `127.0.0.1` | env | Bearer + `X-Forwarded-*: …, http` | clear | **FIRING** |

The dev posture -- row one, loopback with no token and nothing in front -- stays
silent. If it did not, nobody would read the alarm on the box that matters.

Two things that follow from the table and are worth stating:

- **TLS does not make an open server safe.** `https` to the proxy still reaches
  an unauthenticated corpus, so row three still fires.
- **A token over loopback is not a leak.** Those bytes never left the machine,
  and reporting them would make the air-gapped posture permanently alarmed.

## Ordering note

`GET /api/<anything-unrouted>` now returns `404`. It used to fall through to
the SPA shell and return `200` with HTML, so a monitoring probe pointed at a
typo'd or renamed route would pass forever -- reporting health it had never
checked.

## Tests

`tests/test_access_control.py`, in two layers. The policy layer
(`src/mobile_observatory/access.py`) is exhaustive and fast. The server layer
runs a real `ThreadingHTTPServer` on a real socket, because every hole here so
far was in the *wiring* rather than the rule: a policy that returns the right
answer while `do_POST` never calls it is exactly the bug that must not survive.

Each control was proven against a planted defect -- removing the gate from
`do_POST`, disabling the origin comparison, and restoring the `/api/`
fallthrough each fail the tests that guard them.
