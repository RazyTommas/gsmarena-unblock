#!/usr/bin/env python3
"""
watches.py — the watch engine.

A watch is a stored PREDICATE, not just a device id. That matters: with 163
devices but 36 regions, 9 vendors and an OS axis, the interesting subscription is
usually a slice ("any Samsung in ILO") or a criterion ("anything on Android 17+",
"anything that ships a security patch"). A device-only watchlist can't express
those, so the primitive is a predicate over `roms`.

Predicate JSON (all keys optional, AND-ed):
    {"device": "Galaxy S25 Ultra 5G",     exact device name
     "vendor": "Samsung",
     "region": "ILO",
     "chip":   "Snapdragon 8 Elite",      substring of the joined chipset
     "android_min": 17,                   OS major >= n
     "security": true}                    only builds carrying a security advisory

notify: any | security | os_major   — what is worth interrupting for.

Nothing here fabricates a "first seen" time: matches are ranked by ingested_at
when we have it and updated_at otherwise, and rows with neither are never
reported as new.
"""
from __future__ import annotations
import json, sqlite3, time
from common import DB_PATH


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def list_watches():
    con = _conn()
    out = []
    for r in con.execute("SELECT * FROM watch ORDER BY id DESC"):
        d = dict(r)
        try:
            d["predicate"] = json.loads(d.get("predicate") or "{}")
        except Exception:
            d["predicate"] = {}
        out.append(d)
    con.close()
    return out


def add_watch(label, kind, predicate: dict, notify="any"):
    con = _conn()
    cur = con.execute(
        "INSERT INTO watch(label,kind,predicate,notify,created_at) VALUES(?,?,?,?,?)",
        (label, kind, json.dumps(predicate or {}), notify,
         time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())))
    con.commit()
    wid = cur.lastrowid
    con.close()
    return wid


def del_watch(wid):
    con = _conn()
    con.execute("DELETE FROM watch WHERE id=?", (wid,))
    con.commit(); con.close()


def _where(pred):
    """Predicate dict -> (sql, args) over roms r LEFT JOIN device_specs ds."""
    w, a = [], []
    if pred.get("device"):
        w.append("r.device = ?"); a.append(pred["device"])
    if pred.get("vendor"):
        w.append("r.vendor = ?"); a.append(pred["vendor"])
    if pred.get("region"):
        w.append("r.region = ?"); a.append(pred["region"])
    if pred.get("chip"):
        w.append("LOWER(IFNULL(r.chipset,'')) LIKE ?"); a.append(f"%{pred['chip'].lower()}%")
    if pred.get("android_min") not in (None, ""):
        # android_num is NULL for unparseable values, so they are excluded EXPLICITLY
        # rather than silently comparing as 0. A bad input is ignored, never a 500.
        try:
            a_min = float(pred["android_min"])
            w.append("r.android_num IS NOT NULL AND r.android_num >= ?"); a.append(a_min)
        except (TypeError, ValueError):
            pass
    if pred.get("security"):
        w.append("(r.security_url IS NOT NULL AND r.security_url!='') OR (r.security_level IS NOT NULL AND r.security_level!='')")
    return (" AND ".join(w) if w else "1=1"), a


def matches(pred, limit=25, since=None):
    """Newest builds matching a predicate. `since` filters on first-seen/release."""
    sql, args = _where(pred)
    q = (f"SELECT r.source,r.vendor,r.device,r.model,r.region,r.version,r.android,"
         f"r.updated_at,r.security_patch,r.security_level,r.security_url,r.link_kind,r.download_url,r.ingested_at,r.chipset "
         f"FROM roms r WHERE {sql}")
    if since:
        q += " AND IFNULL(r.ingested_at, r.updated_at) > ?"; args = args + [since]
    q += " ORDER BY IFNULL(r.ingested_at, r.updated_at) DESC LIMIT ?"
    con = _conn()
    rows = [dict(x) for x in con.execute(q, args + [limit])]
    con.close()
    return rows


def count(pred):
    sql, args = _where(pred)
    con = _conn()
    n = con.execute(f"SELECT COUNT(*) FROM roms r WHERE {sql}", args).fetchone()[0]
    con.close()
    return n


def watermark():
    con = _conn()
    r = con.execute("SELECT v FROM user_state WHERE k='last_seen_at'").fetchone()
    con.close()
    return r["v"] if r else None


def mark_seen():
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    con = _conn()
    con.execute("INSERT OR REPLACE INTO user_state(k,v) VALUES('last_seen_at',?)", (ts,))
    con.commit(); con.close()
    return ts


def inbox(limit_per_watch=12):
    """What each watch has seen arrive since the watermark, rolled up per device
    (one refresh can land 18 regional builds of one version — that's 1 item)."""
    wm = watermark()
    out, total, sec_total = [], 0, 0
    for w in list_watches():
        pred, notify = w["predicate"], (w.get("notify") or "any")
        rows = matches(pred, limit=200, since=wm)
        if notify == "security":
            rows = [r for r in rows if r.get("security_patch")]
        elif notify == "os_major":
            seen_os = {}
            keep = []
            for r in rows:                        # only the first build of each new OS major
                k = (r["device"], (r.get("android") or "").split(".")[0])
                if k not in seen_os:
                    seen_os[k] = 1; keep.append(r)
            rows = keep
        # roll up: one entry per device, newest first, regions collected
        byd = {}
        for r in rows:
            d = byd.setdefault(r["device"], {**r, "regions": set(), "n": 0})
            if r.get("region"):
                d["regions"].add(r["region"])
            d["n"] += 1
        items = sorted(byd.values(), key=lambda x: x.get("ingested_at") or x.get("updated_at") or "",
                       reverse=True)[:limit_per_watch]
        for it in items:
            it["regions"] = sorted(it["regions"])
        total += sum(i["n"] for i in items)
        sec_total += sum(1 for i in items if i.get("security_patch"))
        out.append({"watch": {k: w[k] for k in ("id", "label", "kind", "notify")},
                    "predicate": pred, "items": items,
                    "new": sum(i["n"] for i in items), "devices": len(items),
                    "total_matching": count(pred)})
    return {"watermark": wm, "watches": out, "new_total": total,
            "security_total": sec_total}


def security_feed(days=120, limit=40):
    """Recent builds carrying a security advisory — the 'pop security patches' feed."""
    con = _conn()
    rows = [dict(r) for r in con.execute(
        "SELECT vendor,device,model,region,version,android,updated_at,security_patch,download_url "
        "FROM roms WHERE security_patch IS NOT NULL AND security_patch!='' "
        "AND updated_at!='' ORDER BY updated_at DESC LIMIT ?", (limit,))]
    con.close()
    return rows
