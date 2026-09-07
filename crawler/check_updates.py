#!/usr/bin/env python3
"""
check_updates.py — is there a newer firmware "in line" than what we have?

For every Samsung device we track in a set of carrier zones (Samsung CSC codes),
this compares OUR latest captured build against Samsung's *current* published build
— taken from Samsung's own public firmware version manifest:

    https://fota-cloud-dn.ospserver.net/firmware/<CSC>/<MODEL>/version.xml

That endpoint needs no login and no evasion; it just returns the latest PDA/CSC/CP
build string for a model in a region. If the upstream build differs from ours and
decodes to a newer date, a new version is "in line" (shipped upstream, not yet
ingested into our corpus).

Two ways to supply "ours":
  * from the corpus DB (default): data/devices.db, latest build per device per zone.
  * from a baseline CSV (--baseline file.csv): columns model,csc,build[,device].
    Use this where the DB isn't available (e.g. a cloud run off the git repo).

Usage:
  python check_updates.py                          # zones ILO,MID from the DB
  python check_updates.py --regions ILO MID INS    # pick zones
  python check_updates.py --only-updates           # print just the ones in line
  python check_updates.py --csv out.csv            # also write a CSV
  python check_updates.py --json out.json          # machine-readable
  python check_updates.py --baseline baseline.csv  # compare vs a committed snapshot

Exit code is the number of new-in-line devices (0 = everything current), so it
composes in cron/CI: `python check_updates.py --only-updates || echo "updates!"`.
"""
from __future__ import annotations
import argparse, csv, json, re, sqlite3, sys, time, urllib.request
from pathlib import Path

DB = Path(__file__).with_name("data") / "devices.db"
MANIFEST = "https://fota-cloud-dn.ospserver.net/firmware/{csc}/{model}/version.xml"
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def decode_date(pda: str):
    """Decode the trailing 3 chars of a Samsung PDA build into (year, month, minor).

    Calibrated against our own dated builds: year letter runs ...X=2024, Y=2025,
    Z=2026; month letter A..L = Jan..Dec; last char is a minor build counter.
    Returns None when the tail isn't a decodable date code.
    """
    m = re.search(r"([A-Z0-9]{3})$", pda or "")
    if not m:
        return None
    y, mo, minor = m.group(1)
    if not (y.isalpha() and mo.isalpha()):
        return None
    year = 2024 + (ord(y) - ord("X"))
    month = ord(mo) - ord("A") + 1
    if not (1 <= month <= 12):
        return None
    return (year, month, minor)


def approx(pda: str) -> str:
    d = decode_date(pda)
    return f"{MONTHS[d[1]]} {d[0]}" if d else ""


def ours_from_db(regions):
    """Latest build per (region, model) from the corpus DB."""
    if not DB.exists():
        sys.exit(f"no corpus DB at {DB} — pass --baseline instead")
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    out = {}
    for reg in regions:
        for r in con.execute(
            "SELECT device,model,version,updated_at FROM roms "
            "WHERE region=? AND updated_at!='' ORDER BY device, updated_at DESC",
            (reg,)):
            key = (reg, r["model"])
            if key not in out:  # rows are newest-first, so first wins
                out[key] = {"device": r["device"], "build": r["version"],
                            "date": r["updated_at"]}
    con.close()
    return out


def ours_from_csv(path):
    out = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            reg = (row.get("csc") or row.get("region") or "").strip()
            model = (row.get("model") or "").strip()
            build = (row.get("build") or row.get("exact_build")
                     or row.get("version") or "").strip()
            if reg and model and build:
                out[(reg, model)] = {"device": row.get("device", model),
                                     "build": build, "date": row.get("released", "")}
    return out


def upstream(csc, model, retries=2):
    url = MANIFEST.format(csc=csc, model=model)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            xml = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            m = re.search(r"<latest[^>]*>([^<]+)</latest>", xml)
            return m.group(1).split("/")[0] if m else None
        except Exception:
            if attempt < retries:
                time.sleep(0.5)
    return None


def classify(ours, up):
    if not up:
        return "?"
    if up == ours:
        return "current"
    du, do = decode_date(up), decode_date(ours)
    if du and do and du > do:
        return "UPDATE"
    if du and do and du < do:
        return "ours-newer"
    return "differs"


def main():
    ap = argparse.ArgumentParser(description="Detect firmware newer upstream than our corpus.")
    ap.add_argument("--regions", nargs="+", default=["ILO", "MID"],
                    help="Samsung CSC codes to check (default: ILO MID)")
    ap.add_argument("--baseline", help="CSV of our builds instead of the DB "
                                       "(cols: model,csc,build[,device,released])")
    ap.add_argument("--csv", help="write full results to this CSV")
    ap.add_argument("--json", help="write full results to this JSON")
    ap.add_argument("--only-updates", action="store_true",
                    help="print only devices with a newer build in line")
    ap.add_argument("--delay", type=float, default=0.15, help="seconds between requests")
    args = ap.parse_args()

    ours = ours_from_csv(args.baseline) if args.baseline else ours_from_db(args.regions)
    if args.baseline and args.regions != ["ILO", "MID"]:
        ours = {k: v for k, v in ours.items() if k[0] in args.regions}

    rows = []
    for (reg, model), info in sorted(ours.items()):
        up = upstream(reg, model)
        time.sleep(args.delay)
        status = classify(info["build"], up)
        rows.append({"status": status, "csc": reg, "device": info["device"],
                     "model": model, "our_build": info["build"],
                     "our_date": info.get("date", ""), "upstream": up or "",
                     "upstream_approx": approx(up) if status in ("UPDATE", "differs") else ""})

    updates = [r for r in rows if r["status"] == "UPDATE"]
    show = updates if args.only_updates else rows
    print(f"{len(rows)} devices checked · {len(updates)} new in line · "
          f"{sum(1 for r in rows if r['status']=='current')} current · "
          f"{sum(1 for r in rows if r['status']=='?')} unchecked\n")
    for r in sorted(show, key=lambda x: (x["status"] != "UPDATE", x["csc"], x["device"])):
        tag = {"UPDATE": "NEW IN LINE", "current": "current",
               "ours-newer": "ours-newer", "differs": "differs", "?": "unchecked"}[r["status"]]
        line = f"[{tag:<11}] {r['csc']} {r['device'][:30]:31} {r['model']:11} {r['our_build']}"
        if r["status"] == "UPDATE":
            line += f"  ->  {r['upstream']} ({r['upstream_approx']})"
        print(line)

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        print(f"\nwrote {args.csv}")
    if args.json:
        json.dump(rows, open(args.json, "w"), indent=1)
        print(f"wrote {args.json}")

    sys.exit(len(updates))


if __name__ == "__main__":
    main()
