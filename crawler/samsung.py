#!/usr/bin/env python3
"""
samsung.py — ingest the Samsung A-series + S-series lineup via the public
firmware manifest (fota-cloud). RUN THIS ON YOUR OWN MACHINE.

NOTE ON ENVIRONMENT: Samsung's CDN (fota-cloud) WAF-blocks datacenter IPs with
403 (the build sandbox is blocked), and samfw is Cloudflare-gated. From a normal
home/office connection both work — so this script is meant to run on your machine
(and via refresh.sh cron there), where it is not blocked.

What it does: for each model × CSC it asks fota-cloud for the current build, and
writes a `roms` row (source 'fota-cloud') with version, region, decoded month, and
a samfw.com page link. This is the *latest build per model per region* — for full
version history + direct downloads use the samfw browser-ingest instead.

  python samsung.py                 # all models below, common CSCs
  python samsung.py --csc ILO MID   # limit regions
"""
from __future__ import annotations
import argparse, sqlite3, re, time
from common import replace_rows, DB_PATH as DB, http_get, pda_month, log_run

MANIFEST = "https://fota-cloud-dn.ospserver.net/firmware/{csc}/{model}/version.xml"

# Samsung A-series + S-series (2020-2026). code -> marketing name.
MODELS = {
    # --- A-series ---
    "SM-A015F": "Galaxy A01", "SM-A025F": "Galaxy A02s", "SM-A032F": "Galaxy A03 Core",
    "SM-A035F": "Galaxy A03", "SM-A037F": "Galaxy A03s", "SM-A042F": "Galaxy A04e",
    "SM-A045F": "Galaxy A04", "SM-A047F": "Galaxy A04s", "SM-A055F": "Galaxy A05",
    "SM-A057F": "Galaxy A05s", "SM-A065F": "Galaxy A06", "SM-A066B": "Galaxy A06 5G",
    "SM-A105F": "Galaxy A10", "SM-A115F": "Galaxy A11", "SM-A125F": "Galaxy A12",
    "SM-A135F": "Galaxy A13", "SM-A137F": "Galaxy A13 (Exynos)", "SM-A155F": "Galaxy A15",
    "SM-A156B": "Galaxy A15 5G", "SM-A166B": "Galaxy A16 5G", "SM-A176B": "Galaxy A17 5G",
    "SM-A205F": "Galaxy A20", "SM-A215U": "Galaxy A21", "SM-A217F": "Galaxy A21s",
    "SM-A225F": "Galaxy A22", "SM-A226B": "Galaxy A22 5G", "SM-A235F": "Galaxy A23",
    "SM-A236B": "Galaxy A23 5G", "SM-A245F": "Galaxy A24", "SM-A256B": "Galaxy A25 5G",
    "SM-A266B": "Galaxy A26 5G", "SM-A276B": "Galaxy A27 5G", "SM-A305F": "Galaxy A30",
    "SM-A315F": "Galaxy A31", "SM-A325F": "Galaxy A32", "SM-A326B": "Galaxy A32 5G",
    "SM-A336B": "Galaxy A33 5G", "SM-A346B": "Galaxy A34 5G", "SM-A356B": "Galaxy A35 5G",
    "SM-A366B": "Galaxy A36 5G", "SM-A376B": "Galaxy A37 5G", "SM-A505F": "Galaxy A50",
    "SM-A515F": "Galaxy A51", "SM-A525F": "Galaxy A52", "SM-A526B": "Galaxy A52 5G",
    "SM-A528B": "Galaxy A52s 5G", "SM-A536B": "Galaxy A53 5G", "SM-A546B": "Galaxy A54 5G",
    "SM-A556B": "Galaxy A55 5G", "SM-A566B": "Galaxy A56 5G", "SM-A576B": "Galaxy A57 5G",
    "SM-A705F": "Galaxy A70", "SM-A715F": "Galaxy A71", "SM-A725F": "Galaxy A72",
    "SM-A736B": "Galaxy A73 5G",
    # --- S-series ---
    "SM-G980F": "Galaxy S20", "SM-G981B": "Galaxy S20 5G", "SM-G985F": "Galaxy S20+",
    "SM-G988B": "Galaxy S20 Ultra", "SM-G780F": "Galaxy S20 FE", "SM-G781B": "Galaxy S20 FE 5G",
    "SM-G991B": "Galaxy S21 5G", "SM-G996B": "Galaxy S21+ 5G", "SM-G998B": "Galaxy S21 Ultra 5G",
    "SM-G990B": "Galaxy S21 FE 5G", "SM-S901B": "Galaxy S22", "SM-S906B": "Galaxy S22+",
    "SM-S908B": "Galaxy S22 Ultra", "SM-S711B": "Galaxy S23 FE", "SM-S911B": "Galaxy S23",
    "SM-S916B": "Galaxy S23+", "SM-S918B": "Galaxy S23 Ultra", "SM-S921B": "Galaxy S24",
    "SM-S926B": "Galaxy S24+", "SM-S928B": "Galaxy S24 Ultra", "SM-S721B": "Galaxy S24 FE",
    "SM-S931B": "Galaxy S25", "SM-S936B": "Galaxy S25+", "SM-S937B": "Galaxy S25 Edge",
    "SM-S938B": "Galaxy S25 Ultra", "SM-S942B": "Galaxy S26", "SM-S947B": "Galaxy S26+",
    "SM-S948B": "Galaxy S26 Ultra",
}

# broad common CSC set (global + major regions); a model returns builds only for CSCs it ships in
CSCS_DEFAULT = ["XAA", "BTU", "DBT", "XEU", "EUX", "INS", "INU", "XSG", "XFA", "EGY",
                "ILO", "MID", "THL", "TGY", "XME", "SEK", "ITV", "PHN", "XSA", "AUT"]


def latest(csc, model):
    xml = http_get(MANIFEST.format(csc=csc, model=model), timeout=15)
    if not xml:
        return None
    m = re.search(r"<latest[^>]*>([^<]+)</latest>", xml)
    return m.group(1).split("/")[0] if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csc", nargs="+", default=CSCS_DEFAULT)
    ap.add_argument("--models", nargs="+", help="limit to these model codes")
    ap.add_argument("--force", action="store_true",
                    help="replace even if the fetch returned far fewer rows than are stored "
                         "(use only when the upstream shrink is real)")
    args = ap.parse_args()
    models = {k: MODELS[k] for k in (args.models or MODELS) if k in MODELS}
    if args.models and not models:
        # A --models filter that matches nothing used to run to completion and print
        # DONE: zero fetched, zero stored, exit 0. An empty selection is an unanswerable
        # question, not a negative answer — refuse it so it can never be read as "the
        # upstream had nothing".
        unknown = [m for m in args.models if m not in MODELS]
        print(f"REFUSED: none of {unknown} is a known model code. "
              f"{len(MODELS)} are known, e.g. {list(MODELS)[:4]}", flush=True)
        log_run("fota-cloud",
                sqlite3.connect(DB).execute(
                    "SELECT COUNT(*) FROM roms WHERE source='fota-cloud'").fetchone()[0],
                outcome="refused", note=f"unknown model codes: {unknown}")
        return 2

    con = sqlite3.connect(DB)
    # Fetch FIRST, into memory. The previous version deleted and committed here, before
    # a single request went out — so on any box where the WAF blocks us (this build
    # sandbox does) every scheduled refresh destroyed the corpus and then logged a
    # successful run with 0 rows. Collect, then replace atomically, then only if the
    # result is plausible.
    rows = []
    found = 0
    for i, (code, name) in enumerate(sorted(models.items()), 1):
        got = 0
        for csc in args.csc:
            b = latest(csc, code)
            time.sleep(0.15)
            if b is None:
                continue
            got += 1
            rows.append((f"Samsung {name}", code, code, csc, "stock", "", b, None, None,
                         pda_month(b), None, f"https://www.samfw.com/firmware/{code}",
                         f"https://www.samfw.com/firmware/{code}", ""))
        if got:
            found += 1
        if i % 10 == 0:
            print(f"  {i}/{len(models)} models, {len(rows)} builds", flush=True)

    ins_sql = ("INSERT INTO roms(source,device,model,codename,region,type,branch,version,"
               "android,size,updated_at,downloads,download_url,model_url,matched_devices)"
               " VALUES('fota-cloud',?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    n, outcome = replace_rows(con, "roms", "source='fota-cloud'", (), rows, ins_sql,
                              source="fota-cloud", force=args.force)
    tot = con.execute("SELECT COUNT(*) FROM roms WHERE source='fota-cloud'").fetchone()[0]

    if outcome == "ok":
        print(f"DONE: {found}/{len(models)} models had builds; {tot} region-builds ingested",
              flush=True)
        log_run("fota-cloud", tot, outcome="ok")
    elif outcome == "blocked":
        note = (f"fetched 0 builds; kept the {tot} existing rows rather than destroying them. "
                f"This is the 403 WAF block — run on your own machine.")
        print(f"REFUSED TO REPLACE: {note}", flush=True)
        log_run("fota-cloud", tot, outcome="blocked", note=note)
    elif outcome == "refused":
        note = (f"fetched only {len(rows)} builds against {tot} stored (below the 50% floor); "
                f"kept the existing rows. Upstream layout change or a partial block. "
                f"Re-run with --force if the shrink is real.")
        print(f"REFUSED TO REPLACE: {note}", flush=True)
        log_run("fota-cloud", tot, outcome="refused", note=note)
    elif found == 0 and models:
        # Nothing stored AND nothing fetched. replace_rows cannot tell these apart —
        # with an empty corpus there is nothing to protect, so it reports 'empty'. But
        # zero models responding across every CSC is the signature of the block, not of
        # an empty upstream, and a source that is permanently blocked must not read as
        # "upstream has nothing" forever.
        note = (f"0 of {len(models)} models returned a build across {len(args.csc)} CSCs — "
                f"that is the 403 WAF block, not an empty upstream. Run on your own machine.")
        print(f"BLOCKED: {note}", flush=True)
        log_run("fota-cloud", tot, outcome="blocked", note=note)
    else:
        print(f"DONE: {tot} region-builds (corpus was empty before this run)", flush=True)
        log_run("fota-cloud", tot, outcome=outcome)
    con.close()


if __name__ == "__main__":
    main()
