#!/usr/bin/env python3
"""
google_soc.py — chipsets for model codes, from two static files. No crawling.

WHY THIS EXISTS
Chipset coverage was gated on gsmarena, one device page at a time, and this box
earned an HTTP 429 there by pulling too hard. Found by collector@field while that
was blocked: the data is also available as two file downloads, with no per-device
requests and no rate limit to respect beyond fetching each file once.

  1. https://storage.googleapis.com/play_public/supported_devices.csv
     Google's own public bucket. 53,828 rows, UTF-16LE.
     Retail Branding | Marketing Name | Device (codename) | Model (model code)
     This is the model-code <-> codename bridge, and nothing else we had provided it.

  2. https://raw.githubusercontent.com/hossain-khan/android-device-catalog-parser/
     main/catalog-data/android-devices-catalog.csv
     A public mirror of the Play Console device catalog. 25,016 rows, ALL carrying a
     'System on Chip' column. The official export sits behind a Play Console login,
     which we do not touch; this is a committed copy of it.

Joined on CODENAME, they give 29,061 model codes a SoC.

WHY THE CODENAME JOIN MATTERS MORE THAN THE COVERAGE
It resolves the regional silicon split that a marketing-name join physically cannot:

    SM-S921B -> Samsung s5e9945   (Exynos 2400)
    SM-S921U -> QTI SM8650        (Snapdragon 8 Gen 3)

Same phone, same marketing name, different chip. Any mapping keyed on "Galaxy S24"
has to pick one and be wrong about the other. The Samsung suffix folklore that F
means Exynos and U means Snapdragon is also false on the data — the suffix encodes
region and carrier, not silicon — so the pairing has to be STORED, never derived.

WHAT IT DOES AND DOES NOT COVER, measured against this corpus
    Samsung   182 of 190 resolvable   (96%)
    Realme     57 of  59
    Oppo       44 of  49
    Tecno       6 of 321              <- Tecno is essentially absent from the catalog
    Xiaomi      -                     <- our own rows carry no model code to join on
    390 chipset-less devices have no model code at all and are unreachable this way.

So this is a large, free win for some vendors and no help at all for others. It
does not retire the gsmarena path; it removes the urgency from it.

    python3 google_soc.py --dry-run
    python3 google_soc.py
"""
from __future__ import annotations
import argparse, csv, io, sqlite3, sys, urllib.request
from common import DB_PATH, USER_AGENT, log_run, connect as db_connect

SUPPORTED = "https://storage.googleapis.com/play_public/supported_devices.csv"
CATALOG = ("https://raw.githubusercontent.com/hossain-khan/"
           "android-device-catalog-parser/main/catalog-data/android-devices-catalog.csv")


def fetch_csv(url, expect_cols, min_rows):
    """Fetch and parse, asserting on CONTENT. Both files are served from hosts that
    would happily return an error page with HTTP 200, and supported_devices.csv is
    UTF-16LE — decoding it as UTF-8 yields rows of NUL-separated garbage that still
    parse as CSV, so the column check is not decoration."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
    rows = list(csv.DictReader(io.StringIO(raw.decode(enc, "replace"))))
    if not rows:
        raise SystemExit(f"{url} parsed to zero rows ({len(raw)}B) — refusing to continue")
    missing = [c for c in expect_cols if c not in rows[0]]
    if missing:
        raise SystemExit(f"{url} is missing {missing}; columns are {list(rows[0])}. "
                         f"The upstream format changed — refusing rather than "
                         f"importing whatever this is.")
    if len(rows) < min_rows:
        raise SystemExit(f"{url} returned {len(rows)} rows, below the {min_rows} floor. "
                         f"A truncated file would silently shrink chipset coverage.")
    return rows


def build_map():
    sd = fetch_csv(SUPPORTED, ["Device", "Model"], 40_000)
    cat = fetch_csv(CATALOG, ["Device", "System on Chip"], 20_000)
    soc = {}
    for c in cat:
        cn, s = (c.get("Device") or "").strip(), (c.get("System on Chip") or "").strip()
        if cn and s:
            soc.setdefault(cn, set()).add(s)
    m2s = {}
    for r in sd:
        m, cn = (r.get("Model") or "").strip(), (r.get("Device") or "").strip()
        if m and cn in soc:
            m2s.setdefault(m, set()).update(soc[cn])
    print(f"  supported_devices {len(sd):,} rows · catalog {len(cat):,} rows "
          f"-> {len(m2s):,} model codes with a SoC", flush=True)
    return m2s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true",
                    help="also replace chipsets already set from another source")
    a = ap.parse_args()

    m2s = build_map()
    con = db_connect()
    where = "IFNULL(chipset,'')=''" if not a.overwrite else "1=1"
    rows = list(con.execute(
        f"SELECT DISTINCT device, model FROM roms WHERE {where} AND IFNULL(model,'')!=''"))

    single, ambiguous, updates = 0, [], []
    for dev, model in rows:
        s = m2s.get(model)
        if not s:
            continue
        if len(s) > 1:
            # Two SoCs for one model code is a real fact about some models, not noise.
            # Storing one of them would be a coin flip presented as data, so record the
            # conflict and leave the column empty.
            ambiguous.append((dev, model, sorted(s)))
            continue
        updates.append((next(iter(s)), model))
        single += 1

    print(f"  {single:,} device/model pairs resolve to exactly one SoC")
    print(f"  {len(ambiguous)} are ambiguous and are LEFT EMPTY, not guessed")
    for d, m, s in ambiguous[:4]:
        print(f"      {m:12} {d[:26]:28} -> {', '.join(s)}")
    if a.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    # Write to a table we OWN before touching roms. roms is replaced wholesale by
    # every ingest, so a mapping that lives only there is destroyed on the next
    # refresh — which is exactly what happened to 55 devices' chipsets between two
    # runs today. derive.py restores from model_soc afterwards, no refetch.
    con.executescript("""
    CREATE TABLE IF NOT EXISTS model_soc(
      model TEXT PRIMARY KEY, soc TEXT, src TEXT, fetched_at TEXT);
    """)
    import time as _t
    now = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    con.executemany("INSERT OR REPLACE INTO model_soc VALUES(?,?,'google-catalog',?)",
                    [(m, soc, now) for soc, m in updates])
    con.executemany("UPDATE roms SET chipset=? WHERE model=? AND IFNULL(chipset,'')=''",
                    updates)
    con.commit()
    n = con.execute("SELECT COUNT(DISTINCT device) FROM roms "
                    "WHERE IFNULL(chipset,'')!=''").fetchone()[0]
    tot = con.execute("SELECT COUNT(DISTINCT device) FROM roms").fetchone()[0]
    print(f"\n  devices with a chipset: {n:,} / {tot:,}")
    log_run("google-soc", single, outcome="ok",
            note=f"{single} model codes resolved, {len(ambiguous)} ambiguous left empty")
    con.close()
    print("\nre-join:  python3 vuln.py --build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
