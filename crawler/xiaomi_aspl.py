#!/usr/bin/env python3
"""
xiaomi_aspl.py — Xiaomi's published patch months, stored as a STALE FLOOR, not as current state.

    https://trust.mi.com/bff/security-update-detail/synctime/{YYYYMM}
    {"heavy":"CVE-…", "high":"CVE-…;CVE-…", "models":"Xiaomi 11T;  Redmi 13C 5G;  …"}

THE FEED IS DEAD AND THAT CHANGES WHAT THE DATA MEANS
50 real months, 2021-01 through 2025-02. Every month after that returns HTTP 200
with a body of literally `null` — verified on 2026-08 and 2026-09. Meanwhile the
end-of-support feed on the same host stamped 2026-09-10, so Xiaomi is alive and has
simply stopped publishing security bulletins. 19 months and counting.

WHY THIS IS NOT WRITTEN INTO roms.security_level, EVEN THOUGH IT WOULD FILL 331 DEVICES

A patch level drives an asymmetric verdict. `claimed-fixed` needs the device's level
to be >= the fix; `open` is what you get otherwise. Writing 2025-02 onto a Xiaomi
device would therefore assert OPEN against every Android CVE fixed in the 19 months
since — hundreds per device — when the truthful statement is "Xiaomi stopped saying,
and these phones have almost certainly been patched".

That is not a small inaccuracy. It is thousands of confident wrong verdicts, in the
alarming direction, indistinguishable from measured ones. Xiaomi is 331 of our
patch-level gaps, so it would also be the single largest block of numbers on the
dashboard — and all of it wrong.

So it is stored as what it actually is: the last patch level Xiaomi PUBLISHED for a
device, with the date it was published, and an explicit note that the feed is dead.
device_state surfaces it as `spl_last_known` / `spl_as_of`, never as `spl`, and the
platform lane does not read it at all.

A stale floor is evidence about the past. Treating it as current state is the
difference between "we do not know" and "we say we know, wrongly".

    python3 xiaomi_aspl.py --backfill        # 2021-01 .. 2025-02, once
    python3 xiaomi_aspl.py --check           # is the feed alive again?
"""
from __future__ import annotations
import argparse, json, re, sys, time
from common import connect as db_connect, http_get, log_run, host_budget

SRC = "https://trust.mi.com/bff/security-update-detail/synctime/{ym}"
HOST = "trust.mi.com"
FIRST, LAST_KNOWN_GOOD = "2021-01", "2025-02"


def months(start, end):
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}", f"{y:04d}{m:02d}"
        m += 1
        if m > 12:
            y, m = y + 1, 1


def fetch_month(ym_compact):
    ok, used, limit = host_budget(HOST)
    if not ok:
        return None, "budget"
    raw = http_get(SRC.format(ym=ym_compact), timeout=30)
    if raw is None:
        return None, "no-response"
    raw = raw.strip()
    if raw in ("null", ""):
        return None, "null"          # published nothing that month — a real absence
    try:
        return json.loads(raw), None
    except Exception:
        return None, "unparseable"


def models_of(doc):
    """Device names from the semicolon-delimited free-text field."""
    out = []
    for name in re.split(r"[;\n]+", (doc.get("models") or "")):
        name = re.sub(r"\s+", " ", name).strip()
        if len(name) > 2:
            out.append(name)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--delay", type=float, default=0.5)
    a = ap.parse_args()

    if a.check:
        # Is the feed alive again? Cheap, and the answer changes what this module is.
        alive = []
        for ym, comp in list(months("2025-03", time.strftime("%Y-%m")))[-6:]:
            doc, why = fetch_month(comp)
            time.sleep(a.delay)
            print(f"  {ym}: {'ALIVE — ' + str(len(models_of(doc))) + ' models' if doc else why}")
            if doc:
                alive.append(ym)
        print(f"\n  {'FEED IS BACK for ' + ', '.join(alive) if alive else 'still dead — '
              'nothing published since ' + LAST_KNOWN_GOOD}")
        return 0

    if not a.backfill:
        print("give --backfill (2021-01..2025-02) or --check (is the feed alive again?)")
        return 1

    con = db_connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS xiaomi_aspl(
      model_name TEXT PRIMARY KEY, spl_month TEXT, as_of TEXT, fetched_at TEXT);
    """)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    latest, seen_months, empty = {}, 0, 0
    for ym, comp in months(FIRST, LAST_KNOWN_GOOD):
        doc, why = fetch_month(comp)
        time.sleep(a.delay)
        if why == "budget":
            print(f"  budget exhausted at {ym} — stopping with what we have")
            break
        if not doc:
            empty += 1
            continue
        seen_months += 1
        for name in models_of(doc):
            if name not in latest or ym > latest[name]:
                latest[name] = ym
        if seen_months % 12 == 0:
            print(f"  {ym} · {len(latest)} models so far", flush=True)

    if not latest:
        raise SystemExit("0 models across the whole window — parser failure, not an "
                         "empty feed")
    print(f"\n  {len(latest)} models · {seen_months} months with content · {empty} empty")
    con.executemany("INSERT OR REPLACE INTO xiaomi_aspl VALUES(?,?,?,?)",
                    [(n, m, LAST_KNOWN_GOOD, now) for n, m in latest.items()])
    con.commit()

    # Report the overlap WITHOUT writing security_level. This is deliberately not an
    # ingest into the verdict path — see the module docstring.
    def norm(s):
        s = re.sub(r"\s+", " ", (s or "").upper().strip())
        return re.sub(r"[^A-Z0-9 ]", " ", s).strip()

    corpus = {}
    for dev, in con.execute("SELECT DISTINCT device FROM roms WHERE vendor='Xiaomi'"):
        corpus.setdefault(norm(dev), []).append(dev)
    hits = sum(len(corpus.get(norm(n), [])) for n in latest)
    print(f"  {hits} corpus devices have a last-known published patch month")
    print(f"  stored as a FLOOR as of {LAST_KNOWN_GOOD}; NOT written to "
          f"roms.security_level, because asserting a 19-month-old level as current "
          f"would generate thousands of wrong 'open' verdicts.")
    log_run("xiaomi-aspl", len(latest), outcome="ok",
            note=f"floor only, as_of {LAST_KNOWN_GOOD}; feed dead since then")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
