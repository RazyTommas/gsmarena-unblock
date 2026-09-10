#!/usr/bin/env python3
"""
enrich_local.py — RUN THIS ON YOUR OWN MACHINE. Verbose, resumable enrichment.

Why it exists: the build sandbox is IP-blocked by gsmarena (429/ban), Samsung's
fota-cloud CDN (403) and rate-limited by archive.org, so the spec/chipset backfill
cannot complete there. Your machine is not blocked. This runs the same work with a
live progress readout so you can watch it rather than guess.

    python3 enrich_local.py                 # everything, in order
    python3 enrich_local.py --only chipset  # one stage
    python3 enrich_local.py --limit 200     # cap the slow network stage

Every stage prints: what it is doing, a running counter with rate + ETA, what it
found, and a summary. Progress is committed continuously, so Ctrl-C then re-run
picks up where it stopped — nothing is lost and nothing is redone.
"""
from __future__ import annotations
import argparse, sqlite3, subprocess, sys, time
from common import DB_PATH, log_run

T0 = time.time()
BAR = 34


def hdr(t):
    print(f"\n\033[1m{'─'*72}\n  {t}\n{'─'*72}\033[0m", flush=True)


def log(m, i=0):
    print(f"  [{time.time()-T0:6.1f}s] {'  '*i}{m}", flush=True)


def progress(i, n, extra="", t0=None):
    """One-line live progress with rate + ETA (rewrites in place)."""
    pct = i / n if n else 1
    fill = int(BAR * pct)
    rate = i / max(0.001, time.time() - (t0 or T0))
    eta = (n - i) / rate if rate > 0 else 0
    sys.stdout.write(f"\r  [{'█'*fill}{'·'*(BAR-fill)}] {i:>5}/{n} "
                     f"{pct*100:5.1f}%  {rate:4.1f}/s  ETA {int(eta//60):02d}:{int(eta%60):02d}  {extra[:34]:<34}")
    sys.stdout.flush()
    if i >= n:
        sys.stdout.write("\n")


def run_script(name, args=None):
    """Run one of our own scripts, streaming its output."""
    cmd = [sys.executable, name] + (args or [])
    log(f"$ {' '.join(cmd)}")
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            print("    " + line.rstrip(), flush=True)
        p.wait()
        log(f"exit={p.returncode}", 1)
        return p.returncode == 0
    except FileNotFoundError:
        log(f"{name} not found — skipped", 1)
        return False


def stage_counts(tag):
    con = sqlite3.connect(DB_PATH)
    tot = con.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    chip = con.execute("SELECT COUNT(*) FROM roms WHERE chipset IS NOT NULL AND chipset!=''").fetchone()[0]
    link = con.execute("SELECT COUNT(*) FROM roms WHERE matched_devices IS NOT NULL "
                       "AND matched_devices!=''").fetchone()[0]
    con.close()
    log(f"{tag}: {tot:,} rows · chipset {chip:,} ({100*chip/max(tot,1):.0f}%) · "
        f"linked {link:,} ({100*link/max(tot,1):.0f}%)")
    return tot, chip, link


# ── the slow network stage, with real progress ──────────────────────────────
def stage_specs(limit, vendors):
    hdr("STAGE 3/5 · device specs from archived gsmarena (the slow one)")
    log("Reads ARCHIVED pages via archive.org — no login, no evasion.")
    log("This is network-bound (~2-20s per device) and fully resumable.")
    try:
        import enrich_specs as E
    except Exception as e:
        log(f"cannot import enrich_specs: {e}"); return
    con = sqlite3.connect(DB_PATH)
    E.ensure_table(con)
    done = {r[0] for r in con.execute("SELECT device FROM device_specs")}
    rows = con.execute("SELECT device, COUNT(*) n FROM roms WHERE device!='' "
                       "GROUP BY device ORDER BY n DESC").fetchall()
    todo = [(d, n) for d, n in rows if E.vendor_of(d) in vendors and d not in done]
    if limit:
        todo = todo[:limit]
    log(f"{len(done):,} devices already cached · {len(todo):,} to fetch this run")
    if not todo:
        log("nothing to do — cache is complete for these vendors"); con.close(); return
    t0, hit, miss = time.time(), 0, 0
    for i, (dev, n) in enumerate(todo, 1):
        chip = url = None; status = "no-page"
        try:
            url = E.resolve_url(dev, timeout=12)
            if url:
                html = E.wayback_fallback(url, timeout=30)
                if html:
                    chip, os_, andr = E.extract(html)
                    status = "ok" if chip else "no-chipset"
        except Exception:
            status = "error"
        con.execute("INSERT OR REPLACE INTO device_specs(device,vendor,chipset,gsmarena_url,status,checked_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (dev, E.vendor_of(dev), chip, url, status, time.strftime("%Y-%m-%d %H:%M")))
        con.commit()                      # commit every device: Ctrl-C loses nothing
        hit, miss = (hit + 1, miss) if chip else (hit, miss + 1)
        progress(i, len(todo), f"{'✓' if chip else '·'} {dev[:24]}", t0)
    log(f"resolved {hit:,} chipsets · {miss:,} unresolved (no archived page / no chipset listed)")
    con.close()


def main():
    ap = argparse.ArgumentParser(description="Local enrichment with live progress.")
    ap.add_argument("--only", choices=["fix", "chipset", "specs", "ios", "audit"],
                    help="run a single stage")
    ap.add_argument("--limit", type=int, default=0, help="cap devices in the slow specs stage")
    ap.add_argument("--vendors", nargs="+", default=["samsung", "xiaomi", "tecno"])
    a = ap.parse_args()
    only = a.only

    print(f"\033[1mFirmware Atlas — local enrichment\033[0m   db={DB_PATH}")
    stage_counts("start")

    if only in (None, "fix"):
        hdr("STAGE 1/5 · repair known data defects")
        run_script("fix_data.py", ["--all"])

    if only in (None, "chipset"):
        hdr("STAGE 2/5 · link chipsets onto firmware rows")
        run_script("link_chipsets.py")

    if only in (None, "specs"):
        stage_specs(a.limit, set(a.vendors))
        hdr("STAGE 3b · re-link the newly resolved chipsets")
        run_script("link_chipsets.py")

    if only in (None, "ios"):
        hdr("STAGE 4/5 · Apple: firmware, specs, security advisories")
        run_script("ios.py")
        run_script("add_iphones.py")
        run_script("ios_security.py", ["--days", "90"])

    if only in (None, "audit"):
        hdr("STAGE 5/5 · audit")
        run_script("audit.py")

    hdr("SUMMARY")
    stage_counts("end")
    log(f"total time {int((time.time()-T0)//60)}m {int((time.time()-T0)%60)}s")
    log("apps read the DB live — just reload the browser, no restart needed")
    log_run("enrich-local", None)


if __name__ == "__main__":
    main()
