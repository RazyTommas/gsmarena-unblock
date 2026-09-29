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
MOBILE_OBSERVATORY_TOKEN=... python3 -m mobile_observatory.server --host 0.0.0.0
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
