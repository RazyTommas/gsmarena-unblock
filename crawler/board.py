#!/usr/bin/env python3
"""
board.py — the personal dashboard + refresh control.

Dashboard: everything scoped to WHAT YOU WATCH, not the whole corpus.
  * your picked devices and the latest ROM each is on
  * firmware that appeared for them since your watermark
  * security patches for them, filterable by chipset / vendor / region

Refresh control: the schedule and the run button live in the app, not only in cron,
so "update every X hours" is configurable from the web and can be fired by hand.
A run is a subprocess of refresh.sh with its log streamed to a file the UI can tail.
"""
from __future__ import annotations
import json, os, signal, sqlite3, subprocess, sys, threading, time
from pathlib import Path
from common import DB_PATH
import watches as W

HERE = Path(__file__).resolve().parent
RUN_LOG = HERE / "refresh_run.log"
_state = {"running": False, "started": None, "finished": None, "rc": None, "pid": None}
_lock = threading.Lock()

DEFAULTS = {"interval_hours": 4, "auto": "0",
            "sources": "ios,ios_security,samsung,chipsets,chipset_cves,patch_levels,exposure,audit",
            "notify_security_only": "0"}


# ── config ──────────────────────────────────────────────────────────────────
def _con():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS config(k TEXT PRIMARY KEY, v TEXT)")
    c.commit()
    return c


def get_config():
    con = _con()
    cfg = dict(DEFAULTS)
    for r in con.execute("SELECT k,v FROM config"):
        cfg[r["k"]] = r["v"]
    con.close()
    cfg["interval_hours"] = int(cfg.get("interval_hours") or 4)
    return cfg


def set_config(d: dict):
    con = _con()
    for k, v in (d or {}).items():
        if k in DEFAULTS:
            con.execute("INSERT OR REPLACE INTO config(k,v) VALUES(?,?)", (k, str(v)))
    con.commit(); con.close()
    return get_config()


# ── the personal board ──────────────────────────────────────────────────────
def board(chip: str = "", vendor: str = "", region: str = ""):
    """Everything below is scoped to the user's watches, then optionally narrowed
    by chipset / vendor / region — the 'only the ones I care about' view."""
    con = _con()
    wm = W.watermark()
    extra, eargs = [], []
    if chip:
        extra.append("LOWER(IFNULL(r.chipset,'')) LIKE ?"); eargs.append(f"%{chip.lower()}%")
    if vendor:
        extra.append("r.vendor = ?"); eargs.append(vendor)
    if region:
        extra.append("r.region = ?"); eargs.append(region)
    narrow = (" AND " + " AND ".join(extra)) if extra else ""

    devices, security, fresh = [], [], []
    seen_dev = set()
    for w in W.list_watches():
        sql, args = W._where(w["predicate"])
        # --- latest ROM per device inside this watch ------------------------
        q = (f"SELECT r.device, r.vendor, r.model, r.region, r.version, r.android, r.chipset, "
             f"r.updated_at, r.security_level, r.security_url, r.download_url, r.link_kind "
             f"FROM roms r WHERE {sql}{narrow} AND r.updated_at!='' "
             f"ORDER BY r.device, r.updated_at DESC, r.version DESC")
        best = {}
        for row in con.execute(q, args + eargs):
            k = row["device"]
            if k not in best:
                best[k] = dict(row); best[k]["watch"] = w["label"]
        for k, v in best.items():
            if k not in seen_dev:
                seen_dev.add(k); devices.append(v)
        # --- security for those devices -------------------------------------
        qs = (f"SELECT r.device,r.vendor,r.model,r.region,r.version,r.chipset,r.updated_at,"
              f"r.security_level,r.security_url FROM roms r WHERE {sql}{narrow} "
              f"AND ((r.security_level IS NOT NULL AND r.security_level!='') "
              f"  OR (r.security_url IS NOT NULL AND r.security_url!='')) "
              f"ORDER BY r.updated_at DESC LIMIT 60")
        for row in con.execute(qs, args + eargs):
            security.append({**dict(row), "watch": w["label"]})
        # --- new since watermark --------------------------------------------
        if wm:
            qn = (f"SELECT r.device,r.vendor,r.model,r.region,r.version,r.chipset,r.updated_at,"
                  f"r.ingested_at FROM roms r WHERE {sql}{narrow} "
                  f"AND IFNULL(r.ingested_at,'') > ? ORDER BY r.ingested_at DESC LIMIT 40")
            for row in con.execute(qn, args + eargs + [wm]):
                fresh.append({**dict(row), "watch": w["label"]})

    devices.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    # de-dup security by device+level, newest first
    seen, sec = set(), []
    for s in security:
        k = (s["device"], s.get("security_level") or s.get("security_url"))
        if k not in seen:
            seen.add(k); sec.append(s)
    sec.sort(key=lambda x: x.get("updated_at") or "", reverse=True)

    # chipset options present *within* the watched set — so the filter can't offer
    # a chip that would return nothing
    chips = sorted({d["chipset"] for d in devices if d.get("chipset")})
    con.close()
    return {"watermark": wm, "devices": devices[:200], "security": sec[:60],
            "fresh": fresh[:40], "chips": chips,
            "counts": {"devices": len(devices), "security": len(sec), "fresh": len(fresh)},
            "filter": {"chip": chip, "vendor": vendor, "region": region}}


# ── refresh control ─────────────────────────────────────────────────────────
def _runner(steps):
    scripts = {"ios": ("ios.py", []), "ios_security": ("ios_security.py", ["--days", "90"]),
               "iphone_specs": ("add_iphones.py", []), "samsung": ("samsung.py", []),
               "chipsets": ("derive.py", []), "fix": ("fix_data.py", ["--all"]),
               "chipset_cves": ("chipset_cves.py", ["--since", "2024-01-01"]),
               "patch_levels": ("osv_spl.py", []),
               "exposure": ("vuln.py", ["--build"]),
               "audit": ("audit.py", [])}
    with open(RUN_LOG, "w") as lg:
        lg.write(f"=== refresh started {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} ===\n")
        lg.write(f"steps: {', '.join(steps)}\n\n"); lg.flush()
        rc = 0
        # derived columns are wiped by any DELETE+re-INSERT ingest, so always
        # re-derive at the end regardless of which steps were picked.
        if any(s in ("ios", "samsung", "ios_security", "iphone_specs") for s in steps) \
                and "chipsets" not in steps:
            steps = list(steps) + ["chipsets"]
        # device_vuln is derived from roms.chipset + roms.security_level + the CVE
        # tables. Any ingest invalidates it, so re-join last, always.
        if any(s in ("ios", "samsung", "chipsets", "chipset_cves", "patch_levels",
                     "iphone_specs", "fix") for s in steps) and "exposure" not in steps:
            steps = list(steps) + ["exposure"]
        for s in steps:
            if s not in scripts:
                continue
            name, args = scripts[s]
            lg.write(f"\n--- {s} ({name}) ---\n"); lg.flush()
            try:
                p = subprocess.Popen([sys.executable, str(HERE / name)] + args,
                                     cwd=str(HERE), stdout=lg, stderr=subprocess.STDOUT)
                with _lock:
                    _state["pid"] = p.pid
                p.wait()
                lg.write(f"[{s}] exit={p.returncode}\n"); lg.flush()
                rc = rc or (p.returncode if p.returncode not in (0,) else 0)
            except Exception as e:
                lg.write(f"[{s}] FAILED {type(e).__name__}: {e}\n"); lg.flush()
        lg.write(f"\n=== finished {time.strftime('%H:%M:%S UTC', time.gmtime())} ===\n")
    with _lock:
        _state.update(running=False, finished=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                      rc=rc, pid=None)


def start_refresh(steps=None):
    with _lock:
        if _state["running"]:
            return {"ok": False, "error": "a refresh is already running"}
        cfg = get_config()
        steps = steps or [s for s in (cfg.get("sources") or "").split(",") if s]
        _state.update(running=True, started=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                      finished=None, rc=None)
    threading.Thread(target=_runner, args=(steps,), daemon=True).start()
    return {"ok": True, "steps": steps}


def refresh_status(tail=120):
    with _lock:
        st = dict(_state)
    st["log"] = ""
    if RUN_LOG.exists():
        try:
            lines = RUN_LOG.read_text(errors="replace").splitlines()
            st["log"] = "\n".join(lines[-tail:])
        except Exception:
            pass
    st["config"] = get_config()
    con = _con()
    try:
        st["crawl"] = [dict(r) for r in con.execute(
            "SELECT source, ran_at, rows FROM crawl_log ORDER BY ran_at DESC")]
    except sqlite3.OperationalError:
        st["crawl"] = []
    con.close()
    return st


# ── the in-app scheduler (so "every X hours" works without cron) ────────────
def _scheduler():
    while True:
        try:
            cfg = get_config()
            if cfg.get("auto") == "1" and not _state["running"]:
                hrs = max(1, int(cfg.get("interval_hours") or 4))
                last = _state.get("finished") or _state.get("started")
                due = True
                if last:
                    try:
                        t = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S UTC"))
                        due = (time.time() - t) >= hrs * 3600
                    except Exception:
                        due = True
                if due:
                    start_refresh()
        except Exception:
            pass
        time.sleep(60)


def start_scheduler():
    threading.Thread(target=_scheduler, daemon=True).start()
