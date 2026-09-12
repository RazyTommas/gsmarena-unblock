#!/usr/bin/env python3
"""
samsung_aspl.py — Samsung Android Security Patch Levels, from Samsung.

THE CONSTRAINT THIS REMOVES
Of 1,207 devices in the corpus, 39 carried a patch level and all 39 came from one
source reached through a browser. That single column is what every exposure verdict
is adjudicated against, so its coverage was the ceiling on the entire project.

doc.samsungmobile.com is Samsung's own Notify Update publisher: first-party firmware
distribution metadata, no WAF, no auth, no challenge, reachable from an ordinary
datacenter IP. Two hops:

    /{MODEL}/{CSC}/doc.html          -> 200, carries a numeric document id
    /{MODEL}/{ID}/eng.html           -> the full build history for that model+CSC

Found by collector@field after samfw (Cloudflare browser challenge) and fota-cloud
(Akamai ASN deny) both refused every network we tried. The answer was never a better
way through a WAF — it was the same class of data from a host nobody put one in
front of.

WHY THIS IS A REAL PATCH LEVEL AND NOT A REPURPOSED DATE
Verified before use, on SM-S928B/EUX: 38 records, and the patch-level values land
exclusively on day 01 (30) or 05 (8) — Google's patch-level convention. A column of
upload or release timestamps scatters across all 28-31 days. That test matters: the
same check applied to mifirm.net found a date column that greps beautifully and is
entirely upload timestamps, which would have written 1,207 confident wrong patch
levels into the corpus. `--verify` re-runs it, and ingestion REFUSES a model whose
distribution fails.

WHAT THIS COLUMN MEANS, EXACTLY
It is the patch level of a build Samsung PUBLISHED for that model and CSC. It is not
proof that any handset installed it. Our verdict vocabulary already carries that
distinction — `claimed-fixed` is a vendor compliance claim, never a measurement — and
this data does not upgrade it. It also varies by CSC: carrier variants legitimately
lag, so one model has many answers and the region column is not decorative.

PACING
doc.samsungmobile.com serves no robots.txt (403). RFC 9309 treats a 4xx as "crawling
permitted", but that is the absence of a prohibition, not a grant — so this defaults
to 1 request/second and is meant to be re-run monthly, never in a loop.

    python3 samsung_aspl.py --verify                    # prove the column is real, fetch nothing else
    python3 samsung_aspl.py --models SM-S928B --csc EUX
    python3 samsung_aspl.py --all                       # the full lineup, slow on purpose
"""
from __future__ import annotations
import argparse, collections, re, sqlite3, sys, time
from common import DB_PATH, http_get, log_run, replace_rows, connect as db_connect

DOC = "https://doc.samsungmobile.com/{model}/{csc}/doc.html"
ENG = "https://doc.samsungmobile.com/{model}/{doc_id}/eng.html"

# Israel-and-around plus global, matching the corpus' regional scope.
CSCS = ["ILO", "MID", "EGY", "TUR", "XSG", "AFG", "INS", "EUX", "XEU", "SEE", "XSA", "BTU"]

SPL_RE = re.compile(r"Security patch level\s*:\s*(?:<[^>]*>\s*)*(\d{4}-\d{2}-\d{2})")
DOCID_RE = re.compile(r"/?(\d{8,})/eng\.html")


def doc_id(model: str, csc: str, delay: float):
    html = http_get(DOC.format(model=model, csc=csc), timeout=25)
    time.sleep(delay)
    if not html:
        return None
    m = DOCID_RE.search(html)
    return m.group(1) if m else None


def history(model: str, csc: str, delay: float):
    """[(build_version, spl)] newest first, or [] — never a guess."""
    did = doc_id(model, csc, delay)
    if not did:
        return []
    html = http_get(ENG.format(model=model, doc_id=did), timeout=30)
    time.sleep(delay)
    if not html:
        return []
    # Pair each build code with the patch level that follows it. Both appear once per
    # record block, so walking the document in order keeps them aligned; a global
    # findall on each separately would silently mis-pair when one is missing.
    stem = model.split("-")[-1]
    pat = re.compile(rf"({re.escape(stem)}[A-Z0-9]{{6,}})")
    out, pos = [], 0
    for bm in pat.finditer(html):
        sm = SPL_RE.search(html, bm.end())
        if not sm:
            continue
        build, spl = bm.group(1), sm.group(1)
        if not any(b == build for b, _ in out):
            out.append((build, spl))
    return out


def distribution_ok(spls):
    """Google publishes patch levels on the 1st or the 5th. Anything else in bulk is
    a different column wearing this one's name."""
    if not spls:
        return False, {}
    dom = collections.Counter(s[-2:] for s in spls)
    return set(dom) <= {"01", "05"}, dict(dom)


def cmd_verify(a):
    model, csc = a.models[0] if a.models else "SM-S928B", (a.csc or ["EUX"])[0]
    print(f"verifying {model}/{csc} …")
    h = history(model, csc, a.delay)
    if not h:
        print("  NOTHING RETURNED — cannot verify. This is not evidence the column is bad;")
        print("  it is evidence the fetch failed. Check reachability before concluding.")
        return 1
    ok, dom = distribution_ok([s for _, s in h])
    print(f"  {len(h)} builds · patch levels {min(s for _,s in h)} .. {max(s for _,s in h)}")
    print(f"  day-of-month distribution: {dom}")
    print(f"  {'PASS' if ok else 'FAIL'} — {'a real ASPL column' if ok else 'this is NOT a patch-level column; refusing it'}")
    for b, s in h[:4]:
        print(f"    {b}  {s}")
    return 0 if ok else 2


def collect(models, cscs, delay, verbose=True):
    rows, refused, tried = [], [], 0
    for i, model in enumerate(models, 1):
        got = 0
        for csc in cscs:
            tried += 1
            h = history(model, csc, delay)
            if not h:
                continue
            ok, dom = distribution_ok([s for _, s in h])
            if not ok:
                # Refuse the model rather than import a column that is not what it says.
                refused.append((model, csc, dom))
                continue
            for build, spl in h:
                rows.append((model, csc, build, spl,
                             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                got += 1
        if verbose and (i % 5 == 0 or i == len(models)):
            print(f"  {i}/{len(models)} models · {len(rows):,} build-patch rows", flush=True)
    return rows, refused, tried


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--models", nargs="+")
    ap.add_argument("--csc", nargs="+")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests (default 1.0 — deliberately polite)")
    a = ap.parse_args()

    if a.verify:
        return cmd_verify(a)

    from samsung import MODELS
    models = a.models or (sorted(MODELS) if a.all else None)
    if not models:
        print("give --models, or --all for the full lineup (slow on purpose)")
        return 1
    unknown = [m for m in models if m not in MODELS]
    if unknown and not a.models:
        print(f"unknown models {unknown}")
        return 2
    cscs = a.csc or CSCS

    print(f"{len(models)} model(s) x {len(cscs)} CSC(s) at {a.delay}s/request "
          f"≈ {len(models)*len(cscs)*2*a.delay/60:.0f} min", flush=True)
    rows, refused, tried = collect(models, cscs, a.delay)

    con = db_connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS samsung_aspl(
      model TEXT, csc TEXT, build TEXT, spl TEXT, fetched_at TEXT,
      PRIMARY KEY (model, csc, build));
    CREATE INDEX IF NOT EXISTS ix_saspl_build ON samsung_aspl(build);
    CREATE INDEX IF NOT EXISTS ix_saspl_model ON samsung_aspl(model, csc);
    """)
    # Scope the replace to the models this run actually collected. The first version
    # used "1=1" — a wholesale swap — so collecting two models proposed replacing the
    # entire table with two models' worth of rows, and the plausibility guard refused
    # it (90 incoming vs 207 stored). The guard was right and the operation was wrong:
    # this is an INCREMENTAL collector, so each run owns only the rows for the models
    # it fetched and must not speak for the ones it did not.
    got_models = sorted({r[0] for r in rows})
    if not got_models:
        print("nothing collected — leaving the table untouched")
        log_run("samsung-aspl", 0, outcome="empty")
        con.close()
        return 1
    ph = ",".join("?" * len(got_models))
    n, outcome = replace_rows(
        con, "samsung_aspl", f"model IN ({ph})", tuple(got_models), rows,
        "INSERT OR REPLACE INTO samsung_aspl VALUES(?,?,?,?,?)", source="samsung-aspl")

    print(f"\n{len(rows):,} build-patch rows from {tried} model/CSC pairs → {outcome}")
    if refused:
        print(f"  {len(refused)} pair(s) REFUSED for a bad day-of-month distribution "
              f"(not a patch-level column): {refused[:3]}")
    if outcome in ("blocked", "refused"):
        print("  kept the existing rows rather than replacing them with a worse set.")
        log_run("samsung-aspl", n, outcome=outcome)
        con.close()
        return 1

    # Fill roms.security_level where a build matches exactly. Exact join on the build
    # code — no fuzzy matching, because a wrong patch level flips verdicts silently.
    before = con.execute("SELECT COUNT(DISTINCT device) FROM roms "
                         "WHERE IFNULL(security_level,'')!=''").fetchone()[0]
    con.execute("""UPDATE roms SET security_level = (
                     SELECT a.spl FROM samsung_aspl a WHERE a.build = roms.version)
                   WHERE IFNULL(security_level,'')=''
                     AND EXISTS (SELECT 1 FROM samsung_aspl a WHERE a.build = roms.version)""")
    con.commit()
    filled = con.execute("SELECT changes()").fetchone()[0]

    # Most of the lineup is not in `roms` at all — 45 of 83 models had no row, so
    # there was nothing to fill. Those builds ARE firmware records: model, CSC, build
    # code and patch level, first-party. Insert the ones we do not already hold.
    # source is its own name so the provenance of every row stays legible, and the
    # insert is additive: it can never remove a build another source contributed.
    from samsung import MODELS as _M
    new = [(f"Samsung {_M.get(m, m)}", m, m, c, "stock", "", b, spl,
            f"https://doc.samsungmobile.com/{m}/{c}/doc.html", fetched)
           for (m, c, b, spl, fetched) in con.execute(
               """SELECT a.model, a.csc, a.build, a.spl, a.fetched_at FROM samsung_aspl a
                  WHERE NOT EXISTS (SELECT 1 FROM roms r WHERE r.version = a.build)""")]
    if new:
        con.executemany(
            "INSERT INTO roms(device,model,codename,region,type,branch,version,"
            "security_level,model_url,ingested_at) VALUES('__d'||'',?,?,?,?,?,?,?,?,?)"
            .replace("'__d'||''", "?"), new)
        con.execute("UPDATE roms SET source='samsung-doc' WHERE IFNULL(source,'')=''")
        con.commit()
    after = con.execute("SELECT COUNT(DISTINCT device) FROM roms "
                        "WHERE IFNULL(security_level,'')!=''").fetchone()[0]
    print(f"  {filled:,} existing rows filled · {len(new):,} new builds added")
    print(f"  devices with a patch level: {before} → {after}")
    log_run("samsung-aspl", len(rows), outcome="ok",
            note=f"{after-before} more devices adjudicable")
    con.close()
    print("\nre-join:  python3 vuln.py --build && python3 platform_vuln.py --build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
