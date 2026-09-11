#!/usr/bin/env python3
"""
collect_samsung.py — harvest Samsung firmware manifests and return them as DATA.

RUN THIS ON THE REMOTE BOX. It exists because of one measured fact: of 1,207 devices
in the corpus, 39 carry an Android Security Patch Level, all 39 came from Samsung,
and that single column is the binding limit on every exposure verdict the project
produces. Samsung's CDN WAF-blocks the other box with 403. This box is not blocked.

WHY IT WRITES A CSV AND NOT A DATABASE
The remote box is a COLLECTOR, not a second source of truth. It has no corpus, and
it must never grow one: two databases drifting apart, merged later by hand, is how a
corpus acquires contradictions nobody can date. So this returns flat, timestamped,
self-describing rows. The asking box ingests them, derives from them, and owns every
verdict. Nothing here is ever a conclusion.

WHAT IT WILL NOT DO
It will not delete anything, it will not write to devices.db, and it will not report
success on an empty harvest. A run that fetched nothing reports `blocked` when the
control proves the network was fine, and `empty` only when the upstream genuinely
had no builds — those are different facts and a row count cannot tell them apart.

    python3 collect_samsung.py --out <dir>
    python3 collect_samsung.py --out <dir> --csc ILO MID GLB --models SM-A055F
"""
from __future__ import annotations
import argparse, csv, json, re, socket, ssl, sys, time, urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from samsung import MODELS, CSCS_DEFAULT          # single source of truth
    from common import pda_month
except Exception:                                      # standalone fallback
    MODELS, CSCS_DEFAULT, pda_month = {}, ["ILO", "MID", "XSG", "EGY", "INS"], lambda x: None

MANIFEST = "https://fota-cloud-dn.ospserver.net/firmware/{csc}/{model}/version.xml"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

# A model+CSC pair that is known to exist, used as the control. If THIS returns
# nothing, the harvest measured the network, not Samsung.
CONTROL = ("ILO", "SM-A055F")


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError):
        return None, ""


def parse(xml):
    """The manifest carries the current build and, on most models, the one before it.
    Returns a list of (kind, pda, csc_ver, build) — never a bare string, because
    'latest' and 'previous' are different rows and collapsing them loses history."""
    out = []
    for tag, kind in (("latest", "latest"), ("upgrade", "upgrade")):
        for m in re.finditer(rf"<{tag}[^>]*>([^<]+)</{tag}>", xml):
            v = m.group(1).strip()
            if v:
                out.append((kind, v))
    for m in re.finditer(r"<value>([^<]+)</value>", xml):
        v = m.group(1).strip()
        if v and v not in [x[1] for x in out]:
            out.append(("value", v))
    return out


def split_ver(v):
    """'A055FXXU5CYA1/A055FOXM5CYA1/A055FXXU5CYA1' -> (pda, csc, phone)."""
    parts = [p for p in v.split("/") if p]
    return (parts + ["", "", ""])[:3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--csc", nargs="+", default=CSCS_DEFAULT)
    ap.add_argument("--models", nargs="+")
    ap.add_argument("--sleep", type=float, default=0.15)
    a = ap.parse_args()
    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)

    models = {k: v for k, v in MODELS.items() if (not a.models or k in a.models)}
    if a.models and not models:
        # A selection that matches nothing is an unanswerable question, not a
        # negative answer. Refuse rather than report an empty harvest.
        bad = [m for m in a.models if m not in MODELS]
        msg = f"unknown model codes {bad}; {len(MODELS)} are known"
        print(f"REFUSED: {msg}")
        (outdir / "result.json").write_text(json.dumps(
            {"outcome": "refused", "note": msg, "control_ok": None,
             "counts": {}, "files": []}, indent=2))
        return 2

    # ---- control FIRST, so a blocked network is known before the loop ----------
    cs, cm = CONTROL
    st, xml = fetch(MANIFEST.format(csc=cs, model=cm))
    control_ok = bool(xml and parse(xml))
    print(f"CONTROL {cm}/{cs}: HTTP {st}, {'PARSED' if control_ok else 'NOTHING'}")
    if not control_ok:
        note = (f"control {cm}/{cs} returned HTTP {st} with no parseable build — this "
                f"network cannot reach fota-cloud either. Nothing below is a fact "
                f"about Samsung.")
        print(f"BLOCKED: {note}")
        (outdir / "result.json").write_text(json.dumps(
            {"outcome": "blocked", "note": note, "control_ok": False,
             "counts": {}, "files": []}, indent=2))
        return 1

    rows, hit_models, http = [], set(), {}
    t0 = time.time()
    total = len(models) * len(a.csc)
    for i, (code, name) in enumerate(sorted(models.items()), 1):
        for csc in a.csc:
            st, xml = fetch(MANIFEST.format(csc=csc, model=code))
            http[str(st)] = http.get(str(st), 0) + 1
            time.sleep(a.sleep)
            if not xml:
                continue
            for kind, v in parse(xml):
                pda, csc_ver, phone = split_ver(v)
                rows.append({
                    "model": code, "device": f"Samsung {name}", "csc": csc,
                    "kind": kind, "version": v, "pda": pda, "csc_ver": csc_ver,
                    "phone_ver": phone,
                    "pda_month": (pda_month(pda) or "") if pda else "",
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                hit_models.add(code)
        if i % 10 == 0:
            el = time.time() - t0
            print(f"  {i}/{len(models)} models · {len(rows)} builds · "
                  f"{el:.0f}s · {len(hit_models)} models answered", flush=True)

    csv_path = outdir / "samsung_fota.csv"
    if rows:
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)

    # `empty` is only honest because the control passed. Without it this would be
    # indistinguishable from the 403 the other box gets.
    outcome = "ok" if rows else "empty"
    note = (f"{len(rows)} builds across {len(hit_models)}/{len(models)} models and "
            f"{len(a.csc)} CSCs in {time.time()-t0:.0f}s. HTTP mix: {http}. "
            f"Control passed, so an empty model is a real absence, not a block.")
    print(f"\n{note}")
    (outdir / "result.json").write_text(json.dumps(
        {"outcome": outcome, "note": note, "control_ok": True,
         "counts": {"builds": len(rows), "models_answered": len(hit_models),
                    "models_tried": len(models), "cscs": len(a.csc),
                    "pairs_tried": total},
         "files": ["samsung_fota.csv"] if rows else []}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
