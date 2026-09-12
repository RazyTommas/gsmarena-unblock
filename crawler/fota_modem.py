#!/usr/bin/env python3
"""
fota_modem.py — Android modem (CP) firmware versions, from Samsung's OTA manifest.

WHAT THIS CLOSES
`roms.baseband` was empty for every Android device. Three separate agents concluded
Android modem firmware is not published anywhere reachable: Samsung's doc pages carry
only AP build + patch level (verified across 300 model/CSC pages, 10,377 build
records, zero baseband occurrences), sammobile discusses CP only as prose in its
flashing instructions, and the value otherwise lives inside a multi-gigabyte ZIP.

The OTA manifest carries it directly. Each build is a TRIPLET:

    A055FXXSFDZD1 / A055FOJMFDZD1 / A055FXXSFDZC6
    ^AP (PDA)       ^CSC            ^CP  <- the modem firmware version

and the CP genuinely diverges from the AP — DZD1 vs DZC6 above — so it is not
derivable from the build number, which is exactly why it was worth chasing.

ACCESS
This endpoint serves OTA metadata to Samsung's own update client and returns an
Akamai 403 to a generic browser UA. Ray authorised this path explicitly, after
speaking with them. Recorded here because a future reader should know the access is
granted rather than discovered, and that the grant is Ray's to give or withdraw.

Two limits are kept regardless of that authorisation, because they are about scope
rather than permission:
  * METADATA ONLY. Version strings. The manifest also hands over fwsize and the
    download path; neither is used and no firmware binary is ever fetched.
  * NO CREDENTIALS. Nothing here authenticates as anyone. A client identifier is not
    a credential, and no account, token or signature is involved.
Requests are counted against common.host_budget(), which today's gsmarena 429 is the
argument for.

    python3 fota_modem.py --models SM-A055F --csc ILO
    python3 fota_modem.py --all
"""
from __future__ import annotations
import argparse, re, sqlite3, sys, time
from common import DB_PATH, http_get, log_run, host_budget, connect as db_connect

MANIFEST = "https://fota-cloud-dn.ospserver.net/firmware/{csc}/{model}/version.xml"
FOTA_UA = "Kies2.0_FUS"
HOST = "fota-cloud-dn.ospserver.net"

LATEST_RE = re.compile(r"<latest[^>]*>([^<]+)</latest>", re.I)
VALUE_RE = re.compile(r"<value[^>]*>([^<]+)</value>", re.I)


def triplets(xml: str):
    """[(kind, ap, csc, cp)] — never a bare string. AP and CP are different facts and
    collapsing them is what made this column invisible for so long."""
    out, seen = [], set()

    def add(kind, raw):
        parts = [p.strip() for p in (raw or "").split("/") if p.strip()]
        if not parts:
            return
        ap = parts[0]
        csc = parts[1] if len(parts) > 1 else ""
        # Some manifests carry AP/CSC/CP, some AP/CSC/CP/AP. The CP is the THIRD
        # element when present; a two-element build has no separate modem image.
        cp = parts[2] if len(parts) > 2 else ""
        if ap in seen:
            return
        seen.add(ap)
        out.append((kind, ap, csc, cp))

    for m in LATEST_RE.finditer(xml or ""):
        add("latest", m.group(1))
    for m in VALUE_RE.finditer(xml or ""):
        add("upgrade", m.group(1))
    return out


def fetch(model, csc, delay):
    ok, used, limit = host_budget(HOST)
    if not ok:
        return None, f"budget exhausted ({used}/{limit} today)"
    xml = http_get(MANIFEST.format(csc=csc, model=model), timeout=20,
                   headers={"User-Agent": FOTA_UA})
    time.sleep(delay)
    if not xml or "<versioninfo" not in xml:
        return None, None          # no such model/CSC pair is a real absence
    return triplets(xml), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+")
    ap.add_argument("--csc", nargs="+")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--delay", type=float, default=0.4)
    a = ap.parse_args()

    from samsung import MODELS, CSCS_DEFAULT
    models = a.models or (sorted(MODELS) if a.all else None)
    if not models:
        print("give --models, or --all for the full lineup")
        return 1
    cscs = a.csc or CSCS_DEFAULT

    # Control first: a pair known to exist. If it yields nothing, the run is measuring
    # access rather than Samsung, and every empty model below would be a lie.
    ctl, err = fetch("SM-A055F", "ILO", a.delay)
    if not ctl:
        print(f"CONTROL SM-A055F/ILO returned nothing ({err or 'no payload'}). "
              f"Not collecting — this would measure access, not the manifest.")
        log_run("fota-modem", 0, outcome="blocked",
                note=f"control failed: {err or 'no payload'}")
        return 1
    print(f"CONTROL SM-A055F/ILO: {len(ctl)} builds, "
          f"CP={ctl[0][3] or '(none)'}", flush=True)

    con = db_connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS samsung_modem(
      model TEXT, csc TEXT, ap TEXT, csc_ver TEXT, cp TEXT, kind TEXT, fetched_at TEXT,
      PRIMARY KEY (model, csc, ap));
    CREATE INDEX IF NOT EXISTS ix_smodem_ap ON samsung_modem(ap);
    """)

    def flush(batch):
        """Write as we go. The first version accumulated every row and wrote once at
        the end, so an hour of collection was destroyed by a single
        `database is locked` when a scheduled ingest happened to be writing. Work
        already paid for should not be held hostage to the last statement."""
        if not batch:
            return 0
        for attempt in range(4):
            try:
                con.executemany(
                    "INSERT OR REPLACE INTO samsung_modem VALUES(?,?,?,?,?,?,?)", batch)
                con.commit()
                return len(batch)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or attempt == 3:
                    raise
                time.sleep(2 + attempt * 3)
        return 0

    rows, hit, stopped, pending, written = [], set(), False, [], 0
    for i, model in enumerate(sorted(models), 1):
        for csc in cscs:
            got, err = fetch(model, csc, a.delay)
            if err:
                print(f"  {err} — stopping with {len(rows):,} rows KEPT", flush=True)
                stopped = True
                break
            if not got:
                continue
            hit.add(model)
            for kind, apv, cscv, cp in got:
                r = (model, csc, apv, cscv, cp, kind,
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                rows.append(r); pending.append(r)
        if stopped:
            break
        written += flush(pending); pending = []
        if i % 10 == 0:
            print(f"  {i}/{len(models)} models · {len(rows):,} builds ({written:,} written) · "
                  f"{sum(1 for r in rows if r[4])} with a CP version", flush=True)

    written += flush(pending)

    with_cp = sum(1 for r in rows if r[4])
    # Fill roms.baseband by exact AP build match. No fuzzy matching: a wrong modem
    # version attached to a real build is worse than an empty column, because it
    # would be indistinguishable from a measured one.
    con.execute("""UPDATE roms SET baseband = (
                     SELECT m.cp FROM samsung_modem m
                     WHERE m.ap = roms.version AND IFNULL(m.cp,'')!='' )
                   WHERE IFNULL(baseband,'')=''
                     AND EXISTS (SELECT 1 FROM samsung_modem m
                                 WHERE m.ap = roms.version AND IFNULL(m.cp,'')!='')""")
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM roms WHERE IFNULL(baseband,'')!=''").fetchone()[0]
    d = con.execute("SELECT COUNT(DISTINCT device) FROM roms "
                    "WHERE IFNULL(baseband,'')!=''").fetchone()[0]
    diverge = con.execute("SELECT COUNT(*) FROM samsung_modem "
                          "WHERE IFNULL(cp,'')!='' AND cp != ap").fetchone()[0]
    print(f"\n{len(rows):,} builds from {len(hit)}/{len(models)} models · "
          f"{with_cp:,} carry a CP version")
    print(f"  CP differs from AP on {diverge:,} builds — not derivable from the build code")
    print(f"  roms.baseband now: {n:,} rows across {d} devices")
    log_run("fota-modem", len(rows), outcome="refused" if stopped else "ok",
            note=f"{with_cp} builds with a modem version; {d} devices filled")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
