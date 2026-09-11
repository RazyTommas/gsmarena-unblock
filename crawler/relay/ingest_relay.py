#!/usr/bin/env python3
"""
ingest_relay.py — take what the remote box collected and fold it into the corpus.

RUN THIS ON THE BOX THAT HOLDS devices.db. The remote collector returns flat CSVs;
this is the only place they become rows, so there is exactly one writer and exactly
one set of rules.

THE RULES IT ENFORCES
  * A result whose `control_ok` is false is DISCARDED, loudly. The control is what
    makes an empty harvest mean "Samsung published nothing" instead of "the network
    said no", and a result that failed its own control is an instrument reading.
  * Rows are replaced through common.replace_rows(), which refuses an empty or
    implausibly-shrunken set. A remote box having a bad day must not be able to
    delete the corpus it exists to fill.
  * Provenance is stamped, not inherited: every row carries source='fota-relay' and
    the collecting agent and timestamp, so a number can always be traced to the box
    and the run that produced it.
  * Nothing here writes a verdict. derive.py and the join modules do that, from the
    corpus, on this box.

    python3 ingest_relay.py --list
    python3 ingest_relay.py --task T002-samsung-fota
    python3 ingest_relay.py --all
"""
from __future__ import annotations
import argparse, csv, json, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import DB_PATH, replace_rows, log_run

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"


def results():
    out = []
    for rp in sorted(RESULTS.glob("*/result.json")):
        try:
            r = json.loads(rp.read_text())
        except Exception:
            continue
        r["_dir"] = rp.parent
        out.append(r)
    return out


def ingest_samsung(con, d: Path, r: dict, force=False):
    csv_path = d / "samsung_fota.csv"
    if not csv_path.exists():
        return 0, "no samsung_fota.csv in this result"
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return 0, "csv present but empty"

    # 'latest' is the build the device is on now; 'upgrade'/'value' entries are the
    # manifest's history and are kept, because a patch-level history is exactly what
    # the platform lane needs to say when a device FELL BEHIND, not just that it is.
    ins = []
    for x in rows:
        ins.append((
            x.get("device"), x.get("model"), x.get("model"), x.get("csc"), "stock", "",
            x.get("version"), None, None, x.get("pda_month") or None, None,
            f"https://www.samfw.com/firmware/{x.get('model')}",
            f"https://www.samfw.com/firmware/{x.get('model')}", "",
            x.get("fetched_at") or time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        ))
    sql = ("INSERT INTO roms(source,device,model,codename,region,type,branch,version,"
           "android,size,updated_at,downloads,download_url,model_url,matched_devices,"
           "ingested_at) VALUES('fota-relay',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    n, outcome = replace_rows(con, "roms", "source='fota-relay'", (), ins, sql,
                              source="fota-relay", force=force)
    if outcome in ("blocked", "refused"):
        return 0, (f"refused to replace: {len(ins)} incoming vs "
                   f"{con.execute('SELECT COUNT(*) FROM roms WHERE source=?', ('fota-relay',)).fetchone()[0]} "
                   f"stored ({outcome}). Re-run with --force if the shrink is real.")
    return n, outcome


HANDLERS = {"T002-samsung-fota": ingest_samsung, "T003-samsung-fota-full": ingest_samsung}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task"); ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true"); ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    rs = results()
    if a.list or not (a.task or a.all):
        if not rs:
            print("no results yet — run `python3 relay.py status` after the other box pushes")
            return 0
        for r in rs:
            ctl = {True: "control ok", False: "CONTROL FAILED", None: "no control"}[r.get("control_ok")]
            print(f"● {r.get('id'):28} {r.get('outcome'):8} {ctl:15} by {r.get('agent')}")
            if r.get("counts"):
                print(f"    {r['counts']}")
            if r.get("note"):
                print(f"    {r['note'][:160]}")
        return 0

    con = sqlite3.connect(DB_PATH)
    todo = [r for r in rs if a.all or r.get("id") == a.task]
    if not todo:
        print(f"no result for {a.task}"); return 1
    for r in todo:
        tid = r.get("id")
        print(f"\n=== {tid} ({r.get('outcome')}) from {r.get('agent')}")
        if r.get("control_ok") is False:
            print("  DISCARDED: this result failed its own control. An empty harvest with a")
            print("  failed control measures the network, not the upstream — ingesting it")
            print("  would write a blocked fetch into the corpus as an absence of firmware.")
            continue
        if r.get("outcome") in ("blocked", "refused", "error"):
            print(f"  skipped: outcome={r.get('outcome')} — {r.get('note','')[:120]}")
            continue
        h = HANDLERS.get(tid)
        if not h:
            print(f"  no handler for {tid}; files: {r.get('files')}")
            continue
        n, outcome = h(con, r["_dir"], r, force=a.force)
        print(f"  -> {n:,} rows ({outcome})")
        if n:
            log_run("fota-relay", n, outcome="ok",
                    note=f"via relay {tid} from {r.get('agent')} at {r.get('finished_at')}")
    con.close()
    print("\nNow re-derive and re-join on this box:")
    print("  python3 derive.py && python3 vuln.py --build && python3 platform_vuln.py --build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
