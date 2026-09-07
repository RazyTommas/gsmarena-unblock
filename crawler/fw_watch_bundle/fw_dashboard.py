#!/usr/bin/env python3
"""
fw_dashboard.py — a standalone, self-refreshing firmware dashboard.

Drop this file (plus baseline_latest.csv) on any machine with Python 3 and run:

    python3 fw_dashboard.py                 # http://localhost:8900, checks every 6h
    python3 fw_dashboard.py --interval 120  # check every 120 min
    python3 fw_dashboard.py --host 0.0.0.0 --port 8900   # view from the LAN
    python3 fw_dashboard.py --once          # one check, print, exit (no server)

What it does, on a routine timer:
  * queries Samsung's PUBLIC firmware version manifest (fota-cloud, no login) for
    every device/CSC in baseline_latest.csv,
  * compares to our last-ingested build to find what's "new in line",
  * tracks the single newest release across everything and POPS it — a hero banner,
    a browser toast, and (best-effort) a desktop notification when a build appears
    that we hadn't seen on the previous check.

Zero third-party dependencies (stdlib only). State persists in fw_state.json so
"new since last check" survives restarts. This is a separate system — it needs
nothing from the crawler DB or any lab infra, just outbound internet.
"""
from __future__ import annotations
import argparse, csv, json, os, re, subprocess, sys, threading, time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline_latest.csv"
HISTORY = HERE / "history.csv"
STATE = HERE / "fw_state.json"
MANIFEST = "https://fota-cloud-dn.ospserver.net/firmware/{csc}/{model}/version.xml"
ZONE_NAME = {"ILO": "Israel", "MID": "Iraq / Lebanon"}
MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_lock = threading.Lock()
_state: dict = {"status": "starting", "updated_at": None}


# ---- Samsung PDA date decode (calibrated vs our own dated builds) ----
def decode(pda):
    m = re.search(r"([A-Z0-9]{3})$", pda or "")
    if not m:
        return None
    y, mo, _ = m.group(1)
    if not (y.isalpha() and mo.isalpha()):
        return None
    year = 2024 + (ord(y) - ord("X"))
    month = ord(mo) - ord("A") + 1
    return (year, month) if 1 <= month <= 12 else None


def approx(pda):
    d = decode(pda)
    return f"{MON[d[1]]} {d[0]}" if d else ""


def load_baseline():
    if not BASELINE.exists():
        sys.exit(f"missing {BASELINE} — copy it next to this script")
    rows = []
    with open(BASELINE, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({"csc": r["csc"], "model": r["model"],
                         "device": r.get("device", r["model"]),
                         "ours": r["build"], "our_date": r.get("released", "")})
    return rows


def load_history():
    """Full per-device build history we already collected (from history.csv)."""
    hist = {}
    if not HISTORY.exists():
        return hist
    with open(HISTORY, newline="") as f:
        for r in csv.DictReader(f):
            key = f'{r["csc"]}/{r["model"]}'
            hist.setdefault(key, []).append(
                {"build": r["build"], "android": r.get("android", ""),
                 "date": r.get("released", "")})
    for k in hist:
        hist[k].sort(key=lambda x: x["date"], reverse=True)
    return hist


_history = load_history()


def upstream(csc, model):
    url = MANIFEST.format(csc=csc, model=model)
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            xml = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            m = re.search(r"<latest[^>]*>([^<]+)</latest>", xml)
            return m.group(1).split("/")[0] if m else None
        except Exception:
            time.sleep(0.6)
    return None


def notify(title, body):
    """Best-effort desktop notification; silently no-ops where unavailable."""
    for cmd in (["notify-send", title, body],
                ["osascript", "-e", f'display notification "{body}" with title "{title}"']):
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=5)
            return
        except Exception:
            continue


def run_check():
    baseline = load_baseline()
    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except Exception:
            prev = {}
    prev_seen = set((prev.get("seen") or []))  # upstream builds seen on prior checks

    zones = {z: {"name": ZONE_NAME.get(z, z), "rows": []} for z in ZONE_NAME}
    pending, seen_now = [], set()
    current = unchecked = 0
    newest = None  # (year, month, build) tuple for ordering
    newest_rec = None
    fresh = []  # builds newly seen this check (not in prev_seen)

    for b in baseline:
        up = upstream(b["csc"], b["model"])
        time.sleep(0.12)
        cur_build = up or b["ours"]
        seen_now.add(cur_build)
        is_new = bool(up and up != b["ours"] and (decode(up) or (0,)) > (decode(b["ours"]) or (0,)))
        if up is None:
            unchecked += 1
        elif is_new:
            pending.append({"csc": b["csc"], "device": b["device"], "model": b["model"],
                            "ours": b["ours"], "upstream": up, "approx": approx(up)})
        else:
            current += 1
        rec = {"csc": b["csc"], "device": b["device"], "model": b["model"],
               "build": cur_build, "date_approx": approx(cur_build),
               "new": is_new, "ingested": not is_new}
        zones.setdefault(b["csc"], {"name": b["csc"], "rows": []})["rows"].append(rec)
        d = decode(cur_build)
        if d and (newest is None or (d[0], d[1]) > (newest[0], newest[1])):
            newest = (d[0], d[1]); newest_rec = rec
        if up and up not in prev_seen:
            fr = decode(up)
            if fr:
                fresh.append({**rec, "_ord": fr})

    for z in zones.values():
        z["rows"].sort(key=lambda r: (decode(r["build"]) or (0, 0)), reverse=True)

    just = None
    if fresh and prev_seen:  # only "pop" once we have a prior baseline of seen builds
        fresh.sort(key=lambda r: r["_ord"], reverse=True)
        just = {k: v for k, v in fresh[0].items() if k != "_ord"}

    st = {
        "status": "ok",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checked": len(baseline), "current": current,
        "new_in_line": len(pending), "unchecked": unchecked,
        "latest_release": newest_rec,
        "just_released": just,
        "pending": sorted(pending, key=lambda r: (r["csc"], r["device"])),
        "zones": zones,
        "seen": sorted(seen_now),
    }
    STATE.write_text(json.dumps(st, indent=1))
    if just:
        notify("New Samsung firmware in line",
               f'{just["device"]} ({just["csc"]}): {just["build"]} · {just["date_approx"]}')
    return st


def poller(interval_min):
    global _state
    while True:
        try:
            st = run_check()
            with _lock:
                _state = st
            print(f"[{st['updated_at']}] checked {st['checked']} | "
                  f"new {st['new_in_line']} | current {st['current']} | "
                  f"unchecked {st['unchecked']}"
                  + (f" | POP {st['just_released']['device']}" if st['just_released'] else ""))
        except Exception as e:
            print("check error:", e)
        time.sleep(max(60, interval_min * 60))


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Firmware Watch</title><style>
:root{--bg:#0d1017;--surf:#161b26;--surf2:#1d2431;--ink:#eef2f8;--mut:#9aa4b5;--line:#273040;
--up:#f5b13d;--ok:#38d39f;--acc:#4c9be8}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(1200px 600px at 20% -10%,#1a2740 0%,var(--bg) 55%);
color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;padding:26px 26px 60px}
h1{font-size:23px;margin:0 0 2px}.sub{color:var(--mut);font-size:13px;margin:0 0 18px}
.hero{position:relative;overflow:hidden;background:linear-gradient(100deg,#20304d,#161b26);
border:1px solid var(--line);border-left:4px solid var(--acc);border-radius:16px;padding:18px 22px;margin-bottom:14px}
.hero .k{color:var(--mut);font-size:11px;letter-spacing:.08em;text-transform:uppercase}
.hero .dev{font-size:22px;font-weight:700;margin:2px 0}
.hero .mono{font-family:ui-monospace,Menlo,Consolas,monospace}
.hero .meta{color:#cdd7ff;font-size:13.5px;margin-top:3px}
.kpis{display:flex;gap:12px;margin:0 0 18px;flex-wrap:wrap}
.kpi{background:var(--surf);border:1px solid var(--line);border-radius:13px;padding:12px 18px;min-width:118px}
.kpi .n{font-size:26px;font-weight:700}.kpi.up .n{color:var(--up)}.kpi.ok .n{color:var(--ok)}
.kpi .l{color:var(--mut);font-size:12px}
.grid{display:grid;grid-template-columns:1fr;gap:16px;max-width:1200px}
.card{background:var(--surf);border:1px solid var(--line);border-radius:15px;overflow:hidden}
.card h2{font-size:16px;margin:0;padding:14px 18px 10px}
.card h2 .csc{font-size:11px;color:var(--acc);border:1px solid var(--acc);border-radius:999px;padding:1px 7px;margin-left:6px}
.card.up{border-left:3px solid var(--up)}
.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--mut);font-weight:600;font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;
padding:9px 16px;border-bottom:1px solid var(--line);background:var(--surf2)}
td{padding:8px 16px;border-bottom:1px solid #ffffff08}tr:hover td{background:#ffffff06}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}.dim{color:var(--mut)}
.dev{font-weight:600}.badge{font-size:9.5px;font-weight:700;border-radius:5px;padding:1px 5px;margin-left:7px}
.b-new{color:var(--up);border:1px solid var(--up)}.arrow{color:var(--up)}
.toast{position:fixed;right:20px;bottom:20px;max-width:340px;background:#1c2436;border:1px solid var(--up);
border-left:4px solid var(--up);border-radius:12px;padding:14px 16px;box-shadow:0 20px 40px -18px #000;
transform:translateY(140%);transition:transform .4s;z-index:9}
.toast.show{transform:translateY(0)}
.toast .t{font-weight:700;color:var(--up)}.toast .m{font-size:13px;margin-top:3px}
.foot{color:var(--mut);font-size:11.5px;margin-top:18px;max-width:1200px}
.live{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px;
animation:pulse 1.8s infinite}@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.hero.pop{animation:flash 1.6s ease-out}@keyframes flash{0%{border-left-color:var(--up);background:linear-gradient(100deg,#4a3a1a,#161b26)}100%{}}
.drow{cursor:pointer}.drow:hover td{background:#ffffff0a}.chev{color:var(--mut);width:20px;text-align:center}
.histrow td{background:#0c1016;padding:0 16px}
.histhdr{color:var(--acc);font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;padding:10px 0 6px}
.histtbl{width:auto;margin-bottom:12px}.histtbl td{border-bottom:1px solid #ffffff06;padding:4px 22px 4px 0}
</style></head><body>
<h1>Firmware Watch <span class="dim" style="font-size:14px;font-weight:400">— Israel &amp; Iraq/Lebanon</span></h1>
<p class="sub"><span class="live"></span><span id="live">connecting…</span></p>
<div id="hero" class="hero"><div class="k">Latest release</div><div class="dev">—</div></div>
<div class="kpis" id="kpis"></div>
<div class="grid" id="grid"></div>
<div class="foot" id="foot"></div>
<div class="toast" id="toast"><div class="t">New firmware in line</div><div class="m" id="toastmsg"></div></div>
<script>
let lastReleaseBuild=null;
function h(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function fmt(iso){if(!iso)return '';const d=new Date(iso);return d.toLocaleString();}
async function tick(){
  try{
    const s=await (await fetch('/api/state',{cache:'no-store'})).json();
    if(s.status!=='ok'){document.getElementById('live').textContent='first check running…';return;}
    document.getElementById('live').textContent='updated '+fmt(s.updated_at)+' · auto-refresh 30s';
    const lr=s.latest_release||{};
    const hero=document.getElementById('hero');
    hero.innerHTML=`<div class="k">Latest release across tracked devices</div>
      <div class="dev">${h(lr.device||'—')} <span class="dim" style="font-size:14px">${h(lr.csc||'')}</span></div>
      <div class="meta"><span class="mono">${h(lr.build||'')}</span> · ${h(lr.date_approx||'')} · model ${h(lr.model||'')}</div>`;
    if(lr.build&&lr.build!==lastReleaseBuild){hero.classList.remove('pop');void hero.offsetWidth;hero.classList.add('pop');lastReleaseBuild=lr.build;}
    document.getElementById('kpis').innerHTML=
      `<div class="kpi up"><div class="n">${s.new_in_line}</div><div class="l">new in line</div></div>
       <div class="kpi ok"><div class="n">${s.current}</div><div class="l">up to date</div></div>
       <div class="kpi"><div class="n">${s.checked}</div><div class="l">devices tracked</div></div>
       <div class="kpi"><div class="n">${s.unchecked}</div><div class="l">unchecked</div></div>`;
    let html='';
    if(s.pending&&s.pending.length){
      html+=`<div class="card up"><h2>New versions in line (${s.pending.length})</h2><div class="tablewrap"><table>
        <thead><tr><th>Zone</th><th>Device</th><th>Model</th><th>Our build</th><th>Upstream (new)</th></tr></thead><tbody>`;
      for(const p of s.pending){html+=`<tr><td class="dim mono">${h(p.csc)}</td><td class="dev">${h(p.device)}</td>
        <td class="mono">${h(p.model)}</td><td class="mono dim">${h(p.ours)}</td>
        <td class="mono"><span class="arrow">→</span> ${h(p.upstream)} <span class="dim">· ${h(p.approx)}</span></td></tr>`;}
      html+='</tbody></table></div></div>';
    }
    for(const z of Object.keys(s.zones)){
      const zn=s.zones[z];
      html+=`<div class="card"><h2>${h(zn.name)}<span class="csc">${h(z)}</span>
        <span class="dim" style="font-size:11px;font-weight:400;margin-left:8px">click a device for full history</span></h2>
        <div class="tablewrap"><table>
        <thead><tr><th></th><th>Device</th><th>Model</th><th>Current build</th><th>Released</th></tr></thead><tbody>`;
      for(const r of zn.rows){
        const rid=(z+'_'+r.model).replace(/[^A-Za-z0-9_]/g,'');
        html+=`<tr class="drow" onclick="toggleHist('${h(z)}','${h(r.model)}','${rid}')">
          <td class="chev" id="cv_${rid}">▸</td>
          <td class="dev">${h(r.device)}${r.new?'<span class="badge b-new">NEW</span>':''}</td>
          <td class="mono dim">${h(r.model)}</td><td class="mono">${h(r.build)}</td><td class="dim">${h(r.date_approx)}</td></tr>
          <tr id="hr_${rid}" class="histrow" style="display:none"><td></td><td colspan="4" id="hc_${rid}"></td></tr>`;
      }
      html+='</tbody></table></div></div>';
    }
    document.getElementById('grid').innerHTML=html;
    document.getElementById('foot').textContent='Source: Samsung public firmware version manifest (fota-cloud, no login). '
      +'This is a standalone dashboard — no login, no lab infra. Dates decoded from the PDA build code.';
    if(s.just_released){
      document.getElementById('toastmsg').textContent=
        `${s.just_released.device} (${s.just_released.csc}): ${s.just_released.build} · ${s.just_released.date_approx}`;
      const t=document.getElementById('toast');t.classList.add('show');setTimeout(()=>t.classList.remove('show'),12000);
    }
  }catch(e){document.getElementById('live').textContent='dashboard offline?';}
}
async function toggleHist(csc,model,rid){
  const row=document.getElementById('hr_'+rid), cell=document.getElementById('hc_'+rid), cv=document.getElementById('cv_'+rid);
  if(row.style.display!=='none'){row.style.display='none';cv.textContent='▸';return;}
  cv.textContent='▾';row.style.display='';
  cell.innerHTML='<span class="dim">loading history…</span>';
  try{
    const d=await (await fetch('/api/history?csc='+encodeURIComponent(csc)+'&model='+encodeURIComponent(model),{cache:'no-store'})).json();
    const b=d.builds||[];
    if(!b.length){cell.innerHTML='<span class="dim">no history on file for this device</span>';return;}
    let t='<div class="histhdr">'+b.length+' builds on file — full version history</div><table class="histtbl"><tbody>';
    for(const r of b){t+=`<tr><td class="dim">${h(r.date)}</td><td class="mono">${h(r.build)}</td><td class="dim">${r.android?'A'+h(r.android):''}</td></tr>`;}
    t+='</tbody></table>';cell.innerHTML=t;
  }catch(e){cell.innerHTML='<span class="dim">could not load history</span>';}
}
tick();setInterval(tick,30000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            with _lock:
                body = json.dumps(_state).encode()
            self._send(200, body, "application/json")
        elif self.path.startswith("/api/history"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            key = f'{q.get("csc",[""])[0]}/{q.get("model",[""])[0]}'
            body = json.dumps({"key": key, "builds": _history.get(key, [])}).encode()
            self._send(200, body, "application/json")
        elif self.path.startswith("/api/refresh"):
            threading.Thread(target=self._refresh, daemon=True).start()
            self._send(200, b'{"ok":true}', "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def _refresh(self):
        global _state
        st = run_check()
        with _lock:
            _state = st

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description="Standalone self-refreshing firmware dashboard.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--interval", type=int, default=360, help="check interval, minutes (default 360 = 6h)")
    ap.add_argument("--once", action="store_true", help="run one check, print JSON, exit")
    args = ap.parse_args()

    if args.once:
        print(json.dumps(run_check(), indent=1))
        return

    global _state
    if STATE.exists():
        try:
            _state = json.loads(STATE.read_text())  # instant paint from last run
        except Exception:
            pass
    threading.Thread(target=poller, args=(args.interval,), daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host if args.host!='0.0.0.0' else 'localhost'}:{args.port}"
    print(f"Firmware Watch dashboard → {url}  (checking every {args.interval} min)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
