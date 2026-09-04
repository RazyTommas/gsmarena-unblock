#!/usr/bin/env python3
"""
app.py — Firmware Atlas: a zero-dependency local app to explore device specs (gsmarena)
and firmware/ROMs (mifirm), with the two joined together.

  * Devices ⇄ ROMs views, search ANY field, choose columns, click-to-sort.
  * Click a device -> a detail drawer with its full spec sheet AND its matched ROMs
    (region / version / size / date + direct download links). That drawer is the join.

Run:  python app.py            -> http://localhost:8765
      python app.py --port 9000 --data data
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DB_PATH = Path("data/devices.db")
INGEST_DIR = Path("output")     # browser-ingested models land here for export.py
LINK_COLS = {"url", "image", "download_url", "model_url", "rom_url"}
DEFAULTS = {
    "devices": ["name", "rom_count", "Launch — Announced", "Body — Dimensions",
                "Body — Weight", "Platform — Chipset", "Platform — OS",
                "Misc — Price", "url"],
    "roms": ["source", "device", "model", "region", "type", "branch", "version",
             "android", "size", "updated_at", "download_url", "model_url"],
}


def _read(table: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(f'SELECT * FROM "{table}"')
    except sqlite3.OperationalError:
        return {"columns": [], "rows": []}
    cols = [d[0] for d in cur.description]
    rows = [list(r) for r in cur.fetchall()]
    conn.close()
    return {"columns": cols, "rows": rows,
            "defaults": [c for c in DEFAULTS.get(table, []) if c in cols] or cols[:9]}


def read_all() -> dict:
    d, r = _read("devices"), _read("roms")
    regions = sorted({row[r["columns"].index("region")]
                      for row in r["rows"]} - {None, ""}) if r["columns"] else []
    linked = sum(1 for row in d["rows"]
                 if str(row[d["columns"].index("rom_count")] or "0") not in ("0", "", "None")) \
        if "rom_count" in d["columns"] else 0
    return {"devices": d, "roms": r,
            "stats": {"devices": len(d["rows"]), "roms": len(r["rows"]),
                      "linked": linked, "regions": len(regions)},
            "link_cols": list(LINK_COLS)}


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Firmware Atlas</title>
<style>
:root{
  --bg:#0a0e17; --bg2:#0e1420; --panel:#121a2b; --elev:#16203400; --card:#141d30;
  --line:#233150; --line2:#2c3d61; --fg:#eaf1fb; --mut:#93a6c9; --dim:#63769a;
  --acc:#5b8cff; --acc2:#8b5cff; --good:#38d39f; --warn:#f5b13d; --bad:#ff6b6b;
  --grad:linear-gradient(120deg,#5b8cff,#8b5cff 55%,#c46bff);
  --shadow:0 18px 50px -12px #000a; --r:12px;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:radial-gradient(1200px 600px at 80% -10%,#17264a55,transparent),
  radial-gradient(900px 500px at 0% 0%,#141d3a55,transparent),var(--bg);
  color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,Segoe UI,Roboto,Inter,sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.mut{color:var(--mut)}.dim{color:var(--dim)}

/* top bar */
.bar{position:sticky;top:0;z-index:20;background:linear-gradient(180deg,#0b1120f2,#0b112099);
  backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.bar .in{display:flex;align-items:center;gap:18px;padding:12px 20px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:11px;font-weight:750;font-size:17px;letter-spacing:.2px}
.logo{width:30px;height:30px;border-radius:9px;background:var(--grad);display:grid;place-items:center;
  box-shadow:0 6px 18px -4px #5b8cff88}
.logo svg{width:17px;height:17px;color:#fff}
.brand .sub{font-weight:500;font-size:12px;color:var(--mut);margin-left:2px}
.stats{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap}
.stat{padding:6px 12px;border:1px solid var(--line);border-radius:20px;background:#0e1626;
  display:flex;gap:7px;align-items:baseline}
.stat b{font-size:15px;font-variant-numeric:tabular-nums}
.stat span{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.6px}

/* toolbar */
.tools{display:flex;align-items:center;gap:12px;padding:12px 20px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);background:#0b111d99}
.seg{display:flex;background:#0d1524;border:1px solid var(--line);border-radius:10px;padding:3px}
.seg button{border:0;background:transparent;color:var(--mut);padding:8px 16px;border-radius:8px;
  cursor:pointer;font-weight:600;font-size:13px;display:flex;gap:7px;align-items:center;transition:.15s}
.seg button.on{color:#fff;background:var(--grad);box-shadow:0 6px 16px -6px #5b8cffaa}
.seg .n{opacity:.75;font-variant-numeric:tabular-nums}
.search{position:relative;flex:1;min-width:260px}
.search svg{position:absolute;left:13px;top:50%;transform:translateY(-50%);width:16px;height:16px;color:var(--dim)}
.search input{width:100%;padding:11px 44px 11px 40px;border:1px solid var(--line);border-radius:11px;
  background:#0c1424;color:var(--fg);font-size:14px;outline:none;transition:.15s}
.search input:focus{border-color:var(--acc);box-shadow:0 0 0 3px #5b8cff33;background:#0d1730}
.search .kbd{position:absolute;right:11px;top:50%;transform:translateY(-50%);color:var(--dim);
  border:1px solid var(--line);border-radius:6px;padding:1px 7px;font-size:11px}
.btn{padding:10px 14px;border:1px solid var(--line);border-radius:11px;background:#0d1524;color:var(--fg);
  cursor:pointer;font-weight:600;font-size:13px;display:flex;gap:7px;align-items:center;transition:.15s}
.btn:hover{border-color:var(--line2);background:#111a2d}
.btn svg{width:15px;height:15px}
.count{color:var(--mut);font-size:13px;white-space:nowrap;font-variant-numeric:tabular-nums}

/* column panel */
.pop{position:absolute;right:20px;top:132px;z-index:30;width:min(560px,92vw);max-height:60vh;overflow:auto;
  background:var(--panel);border:1px solid var(--line2);border-radius:14px;box-shadow:var(--shadow);
  padding:14px;display:none}
.pop.show{display:block}
.pop h4{margin:0 0 4px;font-size:13px;color:var(--mut);display:flex;justify-content:space-between;align-items:center}
.pop .grid{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chk{display:inline-flex;gap:7px;align-items:center;padding:6px 10px;border:1px solid var(--line);
  border-radius:9px;cursor:pointer;font-size:12.5px;color:var(--mut);user-select:none;transition:.12s}
.chk:hover{border-color:var(--line2);color:var(--fg)}
.chk.on{background:#13203a;border-color:var(--acc);color:#fff}
.linkbtn{background:none;border:0;color:var(--acc);cursor:pointer;font-size:12px;font-weight:600}

/* table */
.wrap{overflow:auto;height:calc(100vh - 132px)}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:13.5px}
thead th{position:sticky;top:0;z-index:5;background:#0f1728;color:var(--mut);text-align:left;
  padding:11px 14px;white-space:nowrap;cursor:pointer;user-select:none;font-weight:650;
  border-bottom:1px solid var(--line2);letter-spacing:.2px}
thead th:hover{color:var(--fg)}
tbody td{padding:11px 14px;border-bottom:1px solid #1a2540;vertical-align:top;max-width:440px;color:#d7e0f0}
tbody tr{transition:background .1s}
tbody tr:hover td{background:#0f1830}
tbody tr.clk{cursor:pointer}
tbody tr.clk:hover td{background:#132043}
.thumb{height:38px;border-radius:6px;background:#fff1}
.name{font-weight:650;color:#fff}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11.5px;font-weight:600;
  border:1px solid transparent;white-space:nowrap}
.romcount{display:inline-flex;gap:6px;align-items:center;padding:3px 10px;border-radius:20px;font-weight:700;
  background:linear-gradient(120deg,#5b8cff22,#8b5cff22);border:1px solid #5b8cff55;color:#bcd0ff}
.romcount.zero{background:#0e1626;border-color:var(--line);color:var(--dim);font-weight:500}
.dl{display:inline-flex;gap:6px;align-items:center;padding:5px 11px;border-radius:8px;font-weight:600;
  font-size:12px;background:var(--grad);color:#fff!important;text-decoration:none!important;
  box-shadow:0 6px 14px -8px #5b8cff}
.dl:hover{filter:brightness(1.08)}
.ext{display:inline-flex;gap:5px;align-items:center;color:var(--acc);font-size:12.5px;font-weight:600}
.ico{width:13px;height:13px;vertical-align:-2px}

/* drawer */
.scrim{position:fixed;inset:0;background:#04070cbb;backdrop-filter:blur(2px);opacity:0;pointer-events:none;
  transition:.2s;z-index:40}
.scrim.show{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;height:100%;width:min(560px,96vw);background:var(--bg2);
  border-left:1px solid var(--line2);box-shadow:-30px 0 60px -20px #000c;z-index:50;
  transform:translateX(100%);transition:transform .25s cubic-bezier(.4,0,.2,1);overflow:auto}
.drawer.show{transform:translateX(0)}
.dhead{position:sticky;top:0;background:linear-gradient(180deg,#101a2e,#0e1626);padding:20px;
  border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:flex-start}
.dhead img{height:74px;border-radius:9px;background:#fff1}
.dhead h2{margin:0 0 4px;font-size:19px}
.close{margin-left:auto;background:#0d1524;border:1px solid var(--line);color:var(--fg);width:34px;height:34px;
  border-radius:9px;cursor:pointer;display:grid;place-items:center;flex:none}
.close:hover{background:#152b47}
.dbody{padding:18px 20px}
.sect{margin-bottom:18px}
.sect .st{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--acc);font-weight:700;
  margin:0 0 8px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.kv{display:grid;grid-template-columns:120px 1fr;gap:5px 12px;font-size:13px}
.kv .k{color:var(--mut)}.kv .v{color:#e2e9f6}
.romgroup{border:1px solid var(--line);border-radius:11px;margin-bottom:10px;overflow:hidden}
.romgroup summary{padding:11px 14px;cursor:pointer;display:flex;gap:10px;align-items:center;
  background:#101a2c;font-weight:650;list-style:none}
.romgroup summary::-webkit-details-marker{display:none}
.romrow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:9px 14px;border-top:1px solid #1a2540;font-size:12.5px}
.romrow .v{font-family:ui-monospace,monospace;color:#cdd8ee}
.romrow .meta{color:var(--mut);margin-left:auto;display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.romrow .dl{flex:none}
.empty{text-align:center;color:var(--dim);padding:60px 20px}
.empty svg{width:42px;height:42px;opacity:.4;margin-bottom:10px}

/* analytics */
#analytics{display:none;height:calc(100vh - 132px);overflow:auto}
#analytics.show{display:block}
.filterbar{position:sticky;top:0;z-index:6;display:flex;gap:9px;flex-wrap:wrap;align-items:center;
  padding:12px 20px;background:#0b111df2;backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.filterbar input{padding:8px 11px;border:1px solid var(--line);border-radius:9px;background:#0c1424;
  color:var(--fg);font-size:12.5px;outline:none;width:148px}
.filterbar input:focus{border-color:var(--acc);box-shadow:0 0 0 3px #5b8cff33}
.facet{position:relative}
.facet>button{padding:8px 12px;border:1px solid var(--line);border-radius:9px;background:#0d1524;color:var(--mut);
  cursor:pointer;font-size:12.5px;font-weight:600;display:flex;gap:7px;align-items:center;white-space:nowrap}
.facet>button:hover{border-color:var(--line2);color:var(--fg)}
.facet>button.active{color:#fff;border-color:var(--acc);background:#13203a}
.facet .badge{background:var(--acc);color:#fff;border-radius:10px;padding:0 6px;font-size:11px;font-weight:700}
.facet .menu{display:none;position:absolute;top:calc(100% + 6px);left:0;z-index:20;min-width:180px;max-height:340px;
  overflow:auto;background:var(--panel);border:1px solid var(--line2);border-radius:12px;box-shadow:var(--shadow);padding:7px}
.facet.open .menu{display:block}
.facet .opt{display:flex;gap:8px;align-items:center;padding:6px 9px;border-radius:8px;cursor:pointer;font-size:12.5px;color:var(--mut)}
.facet .opt:hover{background:#13203a;color:var(--fg)}
.facet .opt.on{color:#fff}
.facet .opt .dot{width:9px;height:9px;border-radius:3px;flex:none}
.facet .opt .box{width:15px;height:15px;border:1px solid var(--line2);border-radius:4px;flex:none;display:grid;place-items:center;font-size:11px;color:#fff}
.facet .opt.on .box{background:var(--acc);border-color:var(--acc)}
.fb-clear{margin-left:auto;color:var(--acc);background:none;border:0;cursor:pointer;font-weight:600;font-size:12.5px}
.fb-count{color:var(--mut);font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.chartgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:16px;padding:18px 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 16px 10px;overflow:hidden}
.card.wide{grid-column:1/-1}
.card h3{margin:0 0 2px;font-size:14px;font-weight:650}
.card .sub{margin:0 0 12px;font-size:12px;color:var(--mut)}
.card svg{display:block;width:100%;overflow:visible}
.card .legend{display:flex;flex-wrap:wrap;gap:5px 14px;margin-top:11px;font-size:11.5px}
.card .legend span{display:inline-flex;gap:6px;align-items:center;color:var(--mut)}
.card .legend i{width:10px;height:10px;border-radius:3px;display:inline-block}
.axlabel{fill:var(--mut);font-size:10px}.axtick{fill:var(--dim);font-size:9.5px}
.grid-l{stroke:#1b2740;stroke-width:1}.baseline{stroke:#2c3d61;stroke-width:1}
.barlabel{fill:#cdd8ee;font-size:10.5px;font-variant-numeric:tabular-nums}
.viz-tip{position:fixed;z-index:80;background:#0b1220f2;border:1px solid var(--line2);border-radius:9px;
  padding:7px 10px;font-size:12px;color:var(--fg);pointer-events:none;box-shadow:var(--shadow);display:none;max-width:300px}
.viz-tip b{color:#fff}.viz-tip .k{color:var(--mut)}
.emptychart{fill:var(--dim);font-size:12px}
.card.explore{grid-column:1/-1;background:linear-gradient(160deg,#16203a,#131b2e)}
.pivot-ctl{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:2px 0 14px}
.pivot-ctl label{color:var(--mut);font-size:12px;display:flex;gap:6px;align-items:center}
.pivot-ctl select{padding:7px 10px;border:1px solid var(--line2);border-radius:9px;background:#0c1424;color:#fff;
  font-size:12.5px;font-weight:600;cursor:pointer;outline:none}
.pivot-ctl select:focus{border-color:var(--acc);box-shadow:0 0 0 3px #5b8cff33}
.pivot-ctl .arrow{color:var(--dim);font-size:14px;margin:0 -3px}
</style></head><body>

<div class="bar"><div class="in">
  <div class="brand">
    <span class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="6" y="2" width="12" height="20" rx="3"/><line x1="10" y1="18" x2="14" y2="18"/></svg></span>
    <span>Firmware&nbsp;Atlas<span class="sub">device specs × firmware ROMs</span></span>
  </div>
  <div class="stats" id="stats"></div>
</div></div>

<div class="tools">
  <div class="seg" id="seg"></div>
  <div class="search">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/></svg>
    <input id="q" placeholder="Search any field — codename, chipset, region, Android version…">
    <span class="kbd">/</span>
  </div>
  <button class="btn" id="colBtn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="4" x2="8" y2="20"/><line x1="16" y1="4" x2="16" y2="20"/><rect x="3" y="4" width="18" height="16" rx="2"/></svg>Columns</button>
  <span class="count" id="count"></span>
</div>
<div class="pop" id="pop"></div>

<div class="wrap"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<div id="analytics"><div class="filterbar" id="filterbar"></div><div class="chartgrid" id="chartgrid"></div></div>
<div class="viz-tip" id="viztip"></div>

<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer"></aside>

<script>
const $=s=>document.querySelector(s);
let ALL=null, VIEW="devices", VIS=new Set(), SORT={i:-1,d:1};

const REGION_COL={China:"#ff6b6b",Global:"#5b8cff",EEA:"#38d39f",Russian:"#8b5cff",
  Indo:"#4dd0e1",India:"#f5993d",Taiwan:"#ff8ac0",Japan:"#ff5c7a",Turkey:"#f5b13d",EU:"#59d0ff"};
function pill(col,val){
  if(col==="region"){const c=REGION_COL[val]||"#7c8db0";
    return `<span class="pill" style="color:${c};border-color:${c}55;background:${c}18">${esc(val)}</span>`;}
  if(col==="type"){const c=val==="fastboot"?"#f5b13d":"#38d39f";
    return `<span class="pill" style="color:${c};border-color:${c}55;background:${c}18">${esc(val)}</span>`;}
  if(col==="branch"){const c=val==="developer"?"#8b5cff":"#5b8cff";
    return `<span class="pill" style="color:${c};border-color:${c}55;background:${c}18">${esc(val)}</span>`;}
  return esc(val);
}
function esc(s){return String(s??"").replace(/[&<>"]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m]));}
const ICON={dl:'<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 21h16"/></svg>',
  ext:'<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/></svg>'};

function COL(){return ALL[VIEW].columns;} function ROWS(){return ALL[VIEW].rows;}
function idx(c){return COL().indexOf(c);}

function cell(col,val,row){
  if(val==null||val==="") return '<span class="dim">—</span>';
  if(col==="image") return `<img class="thumb" src="${esc(val)}" loading="lazy">`;
  if(col==="rom_count"){const n=+val||0;
    return `<span class="romcount ${n?"":"zero"}">${n?ICON.dl:""}${n} ROM${n===1?"":"s"}</span>`;}
  if(/^https?:\/\//.test(val)){
    const lbl=col==="download_url"?`${ICON.dl}download`:(col==="model_url"||col==="rom_url")?`${ICON.ext}ROM page`:`${ICON.ext}gsmarena`;
    const cls=col==="download_url"?"dl":"ext";
    return `<a class="${cls}" href="${esc(val)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${lbl}</a>`;}
  if(col==="name"||col==="device") return `<span class="name">${esc(val)}</span>`;
  if(col==="source"){const c={"mifirm.net":"#5b8cff","firmwarefile.com":"#38d39f","samfw.com":"#f5b13d","givemerom.com":"#ff8ac0","romprovider.com":"#c46bff","needrom.com":"#38d39f"}[val]||"#8b5cff";
    return `<span class="pill" style="color:${c};border-color:${c}55;background:${c}18">${esc(val)}</span>`;}
  if(["region","type","branch"].includes(col)) return pill(col,val);
  if(col==="codename") return `<span class="v" style="font-family:ui-monospace,monospace;color:#9fb3d9">${esc(val)}</span>`;
  return esc(val);
}

function renderStats(){
  const s=ALL.stats;
  $("#stats").innerHTML=[["devices",s.devices],["ROM builds",s.roms],
    ["linked",s.linked],["regions",s.regions]]
    .map(([l,v])=>`<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
  $("#seg").innerHTML=[["devices","Devices",s.devices],["roms","ROMs",s.roms],["analytics","Analytics","📊"]]
    .map(([k,l,n])=>`<button data-v="${k}" class="${k===VIEW?"on":""}">${l}<span class="n">${n}</span></button>`).join("");
  $("#seg").querySelectorAll("button").forEach(b=>b.onclick=()=>setView(b.dataset.v));
}
function setView(v){VIEW=v;SORT={i:-1,d:1};
  const isTable=(v==="devices"||v==="roms");
  document.querySelector(".wrap").style.display=isTable?"":"none";
  $("#analytics").classList.toggle("show",v==="analytics");
  $("#q").style.display=isTable?"":"none";
  $("#colBtn").style.display=isTable?"":"none";
  $("#count").style.display=isTable?"":"none";
  if(isTable){VIS=new Set(ALL[v].defaults);buildPop();}
  renderStats();
  if(v==="analytics"){renderAnalytics();} else {render();}
}

function buildPop(){
  const p=$("#pop");
  p.innerHTML=`<h4><span>Show columns · ${VIEW}</span><span>
    <button class="linkbtn" id="cAll">All</button> ·
    <button class="linkbtn" id="cDef">Reset</button> ·
    <button class="linkbtn" id="cNone">None</button></span></h4>
    <div class="grid">${COL().map(c=>`<label class="chk ${VIS.has(c)?"on":""}" data-c="${esc(c)}">
      <input type="checkbox" ${VIS.has(c)?"checked":""} style="display:none">${esc(c)}</label>`).join("")}</div>`;
  p.querySelectorAll(".chk").forEach(l=>l.onclick=()=>{const c=l.dataset.c;
    VIS.has(c)?VIS.delete(c):VIS.add(c);buildPop();render();});
  $("#cAll").onclick=()=>{VIS=new Set(COL());buildPop();render();};
  $("#cNone").onclick=()=>{VIS=new Set();buildPop();render();};
  $("#cDef").onclick=()=>{VIS=new Set(ALL[VIEW].defaults);buildPop();render();};
}

function anyFilter(){return ["vendor","source","region","android","type"].some(k=>F[k]&&F[k].size)||F.name||F.chipset||F.minBatt;}
function filtered(){
  const q=$("#q").value.trim().toLowerCase();
  const t=q?q.split(/\s+/):[];
  const A=(VIEW==="devices")?ADEV:(VIEW==="roms")?AROM:null;
  const useF=anyFilter()&&A;
  let rows=ROWS().filter((r,i)=>{
    if(t.length){const h=r.join(" ").toLowerCase();if(!t.every(x=>h.includes(x)))return false;}
    if(useF&&A[i]){if(VIEW==="devices"&&!filtDev(A[i]))return false;if(VIEW==="roms"&&!filtRom(A[i]))return false;}
    return true;});
  if(SORT.i>=0){rows=rows.slice().sort((a,b)=>{let x=a[SORT.i]??"",y=b[SORT.i]??"";
    const nx=parseFloat(String(x).replace(/[^\d.]/g,"")),ny=parseFloat(String(y).replace(/[^\d.]/g,""));
    if(!isNaN(nx)&&!isNaN(ny)&&/\d/.test(x)&&/\d/.test(y)){x=nx;y=ny;}
    return (x>y?1:x<y?-1:0)*SORT.d;});}
  return rows;
}
const RENDER_CAP=1200;   // keep the DOM snappy with 40k+ rows; search narrows the full set
function render(){
  const cols=COL().map((c,i)=>({c,i})).filter(o=>VIS.has(o.c));
  const all=filtered();
  const rows=all.slice(0,RENDER_CAP);
  $("#head").innerHTML=cols.map(o=>`<th data-i="${o.i}">${esc(o.c)}${SORT.i===o.i?(SORT.d>0?" ▲":" ▼"):""}</th>`).join("");
  $("#head").querySelectorAll("th").forEach(th=>th.onclick=()=>{const i=+th.dataset.i;
    SORT.d=SORT.i===i?-SORT.d:1;SORT.i=i;render();});
  const clickable=VIEW==="devices";
  $("#body").innerHTML = rows.length ? rows.map((r,ri)=>
    `<tr class="${clickable?"clk":""}" data-ri="${ROWS().indexOf(r)}">`+
    cols.map(o=>`<td>${cell(o.c,r[o.i],r)}</td>`).join("")+"</tr>").join("")
    : `<tr><td colspan="${cols.length||1}"><div class="empty">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/></svg>
        <div>No rows match “${esc($("#q").value)}”.</div></div></td></tr>`;
  if(clickable)$("#body").querySelectorAll("tr.clk").forEach(tr=>tr.onclick=()=>openDrawer(+tr.dataset.ri));
  const capped=all.length>RENDER_CAP?`  ·  showing ${RENDER_CAP} — refine search`:"";
  $("#count").textContent=`${all.length} of ${ROWS().length}${capped}`;
}

/* ---- device drawer = the join ---- */
function openDrawer(ri){
  const D=ALL.devices, cols=D.columns, row=D.rows[ri];
  const g=c=>{const i=cols.indexOf(c);return i<0?null:row[i];};
  const id=g("device_id"), name=g("name"), img=g("image");
  // group spec columns by "Section — Field"
  const sections={};
  cols.forEach((c,i)=>{const m=c.match(/^(.*?) — (.*)$/);if(m&&row[i]){(sections[m[1]]??=[]).push([m[2],row[i]]);}});
  // matched roms
  const R=ALL.roms, rc=R.columns, mi=rc.indexOf("matched_devices");
  const mine=R.rows.filter(r=>String(r[mi]||"").split(",").includes(id));
  const byRegion={};mine.forEach(r=>{const rg=r[rc.indexOf("region")]||"—";(byRegion[rg]??=[]).push(r);});
  const rg=c=>rc.indexOf(c);

  const src=c=>rc.indexOf("source");
  let romHtml=mine.length? Object.entries(byRegion).map(([reg,list])=>`
    <details class="romgroup" open><summary>${pill("region",reg)}<span class="dim" style="font-weight:500">${list.length} build${list.length===1?"":"s"}</span></summary>
      ${list.map(r=>{const meta=[r[rg("android")]?"Android "+r[rg("android")]:"",r[rg("size")]||"",
        (r[rg("updated_at")]||"").slice(0,10)].filter(Boolean).join(" · ");
        return `<div class="romrow">${pill("source",r[rg("source")])}
        <span class="v">${esc(r[rg("version")]||r[rg("model")]||"firmware")}</span>
        ${r[rg("type")]?pill("type",r[rg("type")]):""}${r[rg("branch")]?pill("branch",r[rg("branch")]):""}
        <span class="meta">${esc(meta)}
        ${r[rg("download_url")]?`<a class="dl" href="${esc(r[rg("download_url")])}" target="_blank" rel="noopener">${ICON.dl}get</a>`:""}</span></div>`;}).join("")}
    </details>`).join("")
    : `<div class="dim" style="padding:6px 2px">No firmware matched yet for this device.</div>`;

  const specHtml=Object.entries(sections).map(([s,kvs])=>`<div class="sect"><div class="st">${esc(s)}</div>
    <div class="kv">${kvs.map(([k,v])=>`<div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>`).join("")}</div></div>`).join("");

  $("#drawer").innerHTML=`<div class="dhead">${img?`<img src="${esc(img)}">`:""}
    <div><h2>${esc(name)}</h2>
      <div class="dim" style="font-size:12.5px">${g("codenames")?"codename: "+esc(g("codenames")):"no matched codename"}</div>
      <div style="margin-top:9px;display:flex;gap:10px">
        ${g("url")?`<a class="ext" href="${esc(g("url"))}" target="_blank" rel="noopener">${ICON.ext}gsmarena</a>`:""}
        ${g("rom_url")?`<a class="ext" href="${esc(g("rom_url"))}" target="_blank" rel="noopener">${ICON.ext}firmware</a>`:""}</div>
    </div><button class="close" id="dClose">✕</button></div>
    <div class="dbody">
      <div class="sect"><div class="st">Firmware / ROMs · ${mine.length}</div>${romHtml}</div>
      ${specHtml}</div>`;
  $("#dClose").onclick=closeDrawer;
  $("#drawer").classList.add("show");$("#scrim").classList.add("show");
}
function closeDrawer(){$("#drawer").classList.remove("show");$("#scrim").classList.remove("show");}

$("#scrim").onclick=closeDrawer;
$("#q").addEventListener("input",render);
$("#colBtn").onclick=e=>{e.stopPropagation();$("#pop").classList.toggle("show");};
document.addEventListener("click",e=>{if(!$("#pop").contains(e.target)&&e.target!==$("#colBtn"))$("#pop").classList.remove("show");});
document.addEventListener("keydown",e=>{
  if(e.key==="/"&&document.activeElement!==$("#q")){e.preventDefault();$("#q").focus();}
  if(e.key==="Escape"){closeDrawer();$("#pop").classList.remove("show");}});

/* ===================== ANALYTICS ===================== */
const SERIES=["#3987e5","#d95926","#199e70","#c98500","#d55181","#008300","#9085e9","#e66767"];
const SRC_COL={"mifirm.net":"#5b8cff","firmwarefile.com":"#38d39f","samfw.com":"#f5b13d","givemerom.com":"#ff8ac0","romprovider.com":"#c46bff"};
const OTHER="#7c8db0";
const MONTHS={january:0,february:1,march:2,april:3,may:4,june:5,july:6,august:7,september:8,october:9,november:10,december:11,
  jan:0,feb:1,mar:2,apr:3,jun:5,jul:6,aug:7,sep:8,sept:8,oct:9,nov:10,dec:11};
let ADEV=null, AROM=null, VENDOR_COL={}, ANDROID_COL={};
const F={vendor:new Set(),source:new Set(),region:new Set(),android:new Set(),type:new Set(),name:"",chipset:"",minBatt:0};

const VENDOR_ALIAS={mi:"Xiaomi",mix:"Xiaomi",redmi:"Redmi",poco:"Poco",pocophone:"Poco"};
function vendorOf(n){n=(n||"").trim();const w=(n.split(/\s+/)[0]||"").toLowerCase();
  return VENDOR_ALIAS[w]||(w?w[0].toUpperCase()+w.slice(1):"?");}
function parseYM(s){if(!s)return null;const m=String(s).match(/(\d{4})(?:[,\s]+([A-Za-z]+))?(?:\s+(\d{1,2}))?/);
  if(!m)return null;const y=+m[1];const mo=m[2]?MONTHS[m[2].toLowerCase()]:0;
  return {t:Date.UTC(y,mo==null?0:mo,+(m[3]||1)),y,mo:mo==null?0:mo};}
function parseDate(s){if(!s)return null;const m=String(s).match(/(\d{4})-(\d{2})-(\d{2})/);
  return m?{t:Date.UTC(+m[1],+m[2]-1,+m[3]),y:+m[1],mo:+m[2]-1}:null;}
function battOf(s){const m=String(s||"").match(/(\d{3,5})\s*mAh/i);return m?+m[1]:0;}
function mapColors(keys){const c={};keys.slice(0,8).forEach((k,i)=>c[k]=SERIES[i]);keys.slice(8).forEach(k=>c[k]=OTHER);return c;}
function normAndroid(a){if(a==null||a==="")return null;const m=String(a).match(/(\d{1,2})/);return m?m[1]:null;}
function andrOf(os){if(!os)return null;const m=String(os).match(/Android\s*(\d{1,2})/i);return m?m[1]:null;}
function chipShort(s){s=String(s||"");
  const pats=[/Snapdragon\s+[\w\s]+?(?:Gen\s*\d+|Elite(?:\s+Gen\s*\d+)?|\d[\w+]*)/i,/Dimensity\s+\d+\w*/i,/Helio\s+\w+/i,/Exynos\s+\w+/i,/Tensor\s*\w*/i,/Unisoc\s+\w+/i,/Kirin\s+\w+/i];
  for(const p of pats){const m=s.match(p);if(m)return m[0].replace(/\s+/g," ").trim();}
  const w=s.replace(/\(.*?\)/g,"").trim().split(/\s+/).slice(0,3).join(" ");return w||null;}
function ramOf(s){const ms=[...String(s||"").matchAll(/(\d+)\s*GB\s*RAM/gi)].map(m=>+m[1]);return ms.length?Math.max(...ms):0;}

function enrich(){
  if(ADEV)return;
  const dc=ALL.devices.columns, gi=c=>dc.indexOf(c);
  ADEV=ALL.devices.rows.map(r=>{const g=c=>{const i=gi(c);return i<0?null:r[i];};const name=g("name")||"";
    return {id:g("device_id"),name,vendor:vendorOf(name),announced:parseYM(g("Launch — Announced")),
      chipset:(g("Platform — Chipset")||""),chip:chipShort(g("Platform — Chipset")),
      battery:battOf(g("Battery — Type")||g("Battery — Charging")),ram:ramOf(g("Memory — Internal")),
      android:andrOf(g("Platform — OS")),romCount:+(g("rom_count")||0)};});
  const rc=ALL.roms.columns, ri=c=>rc.indexOf(c);
  AROM=ALL.roms.rows.map(r=>{const g=c=>{const i=ri(c);return i<0?null:r[i];};const dev=g("device")||"";
    return {source:g("source"),device:dev,vendor:vendorOf(dev),model:g("model"),region:g("region"),
      type:g("type"),branch:g("branch"),android:normAndroid(g("android")),date:parseDate(g("updated_at"))};});
  const vcount={};[...ADEV,...AROM].forEach(x=>{if(x.vendor)vcount[x.vendor]=(vcount[x.vendor]||0)+1;});
  const vend=Object.keys(vcount).sort((a,b)=>vcount[b]-vcount[a]);  // frequent vendors get the distinct hues
  VENDOR_COL=mapColors(vend);
  const andr=[...new Set([...AROM,...ADEV].map(r=>r.android).filter(Boolean))].sort((a,b)=>parseFloat(b)-parseFloat(a));
  ANDROID_COL=mapColors(andr);
}
const uniq=(arr,f)=>[...new Set(arr.map(f).filter(v=>v!=null&&v!==""))];
function filtDev(d){return (!F.vendor.size||F.vendor.has(d.vendor))&&(!F.name||d.name.toLowerCase().includes(F.name))
  &&(!F.chipset||d.chipset.toLowerCase().includes(F.chipset))&&(!F.minBatt||d.battery>=F.minBatt)
  &&(!F.android.size||F.android.has(d.android));}
function filtRom(r){return (!F.vendor.size||F.vendor.has(r.vendor))&&(!F.name||r.device.toLowerCase().includes(F.name))
  &&(!F.source.size||F.source.has(r.source))&&(!F.region.size||F.region.has(r.region))
  &&(!F.android.size||F.android.has(r.android))&&(!F.type.size||F.type.has(r.type))
  &&(!F.chipset||true);}

/* ---- filter bar ---- */
function buildFilterBar(){
  const facets=[["vendor","Vendor",uniq([...ADEV,...AROM],x=>x.vendor).sort(),k=>VENDOR_COL[k]||OTHER],
    ["source","Source",uniq(AROM,x=>x.source).sort(),k=>SRC_COL[k]||OTHER],
    ["region","Region",uniq(AROM,x=>x.region).sort(),k=>REGION_COL[k]||OTHER],
    ["android","Android",uniq([...AROM,...ADEV],x=>x.android).sort((a,b)=>parseFloat(b)-parseFloat(a)),k=>ANDROID_COL[k]||OTHER],
    ["type","FW type",uniq(AROM,x=>x.type).sort(),null]];
  const fb=$("#filterbar");
  fb.innerHTML=facets.map(([key,label,opts])=>`
    <div class="facet" data-key="${key}"><button><span>${label}</span><span class="cnt"></span></button>
      <div class="menu">${opts.map(o=>`<div class="opt" data-v="${esc(o)}"><span class="box"></span><span class="dot"></span><span>${esc(o)}</span></div>`).join("")||'<div class="opt">—</div>'}</div></div>`).join("")
    + `<input id="fName" placeholder="device name…" style="width:150px">
       <input id="fChip" placeholder="chipset…" style="width:130px">
       <input id="fBatt" type="number" placeholder="min mAh" style="width:100px">
       <button class="fb-clear" id="fClear">Reset filters</button>
       <span class="fb-count" id="fCount"></span>`;
  facets.forEach(([key,label,opts,colf])=>{
    const el=fb.querySelector(`.facet[data-key="${key}"]`);
    el.querySelector("button").onclick=e=>{e.stopPropagation();document.querySelectorAll(".facet").forEach(x=>{if(x!==el)x.classList.remove("open")});el.classList.toggle("open");};
    el.querySelectorAll(".opt[data-v]").forEach(op=>{
      const v=op.dataset.v; if(colf){const d=op.querySelector(".dot");d.style.background=colf(v);} else op.querySelector(".dot").remove();
      op.onclick=()=>{const S=F[key];S.has(v)?S.delete(v):S.add(v);op.classList.toggle("on");op.querySelector(".box").textContent=S.has(v)?"✓":"";updateFacetCounts();renderAnalytics();};
    });
  });
  $("#fName").oninput=e=>{F.name=e.target.value.trim().toLowerCase();renderAnalytics();};
  $("#fChip").oninput=e=>{F.chipset=e.target.value.trim().toLowerCase();renderAnalytics();};
  $("#fBatt").oninput=e=>{F.minBatt=+e.target.value||0;renderAnalytics();};
  $("#fClear").onclick=()=>{["vendor","source","region","android","type"].forEach(k=>F[k].clear());F.name=F.chipset="";F.minBatt=0;
    $("#fName").value=$("#fChip").value=$("#fBatt").value="";fb.querySelectorAll(".opt.on").forEach(o=>{o.classList.remove("on");o.querySelector(".box").textContent="";});updateFacetCounts();renderAnalytics();};
  document.addEventListener("click",()=>document.querySelectorAll(".facet.open").forEach(x=>x.classList.remove("open")));
  updateFacetCounts();
}
function updateFacetCounts(){["vendor","source","region","android","type"].forEach(k=>{
  const el=$("#filterbar").querySelector(`.facet[data-key="${k}"]`);if(!el)return;
  const n=F[k].size;const b=el.querySelector("button");b.classList.toggle("active",n>0);
  el.querySelector(".cnt").innerHTML=n?`<span class="badge">${n}</span>`:"";});}

/* ---- svg helpers ---- */
const NS="http://www.w3.org/2000/svg";
function svg(w,h){const s=document.createElementNS(NS,"svg");s.setAttribute("viewBox",`0 0 ${w} ${h}`);s.setAttribute("preserveAspectRatio","xMidYMid meet");return s;}
function el(t,a,parent){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(parent)parent.appendChild(e);return e;}
function tipOn(node,html){node.addEventListener("mousemove",e=>{const t=$("#viztip");t.innerHTML=html;t.style.display="block";
  t.style.left=Math.min(e.clientX+14,innerWidth-t.offsetWidth-8)+"px";t.style.top=(e.clientY+14)+"px";});
  node.addEventListener("mouseleave",()=>{$("#viztip").style.display="none";});}
function card(title,sub,wide){const c=document.createElement("div");c.className="card"+(wide?" wide":"");
  c.innerHTML=`<h3>${esc(title)}</h3><p class="sub">${esc(sub)}</p>`;$("#chartgrid").appendChild(c);return c;}
function legend(c,items){c.insertAdjacentHTML("beforeend",`<div class="legend">${items.map(([l,col])=>`<span><i style="background:${col}"></i>${esc(l)}</span>`).join("")}</div>`);}
function empty(c,W){const s=svg(W,80);c.appendChild(s);el("text",{x:W/2,y:44,"text-anchor":"middle",class:"emptychart"},s).textContent="No data for the current filters";}

/* ---- horizontal bar ---- */
function hbar(c,items,colf,W){W=W||440;const rowH=26,padT=6,padB=18,left=Math.min(150,Math.max(...items.map(i=>i.label.length))*6.2+10),right=44;
  const H=padT+padB+items.length*rowH;const s=svg(W,H);c.appendChild(s);
  const max=Math.max(1,...items.map(i=>i.value));const plotW=W-left-right;
  items.forEach((it,i)=>{const y=padT+i*rowH;const bw=Math.max(2,it.value/max*plotW);
    el("text",{x:left-8,y:y+rowH/2+4,"text-anchor":"end",class:"axtick"},s).textContent=it.label.length>24?it.label.slice(0,23)+"…":it.label;
    const r=el("rect",{x:left,y:y+4,width:bw,height:rowH-10,rx:4,fill:colf(it.label,i)},s);
    el("text",{x:left+bw+6,y:y+rowH/2+4,class:"barlabel"},s).textContent=it.value;
    tipOn(r,`<b>${esc(it.label)}</b><br>${it.value} ${it.unit||""}`);});
}
/* ---- stacked bars over months ---- */
function stackedMonths(c,rows,catKey,colf,W){W=W||1000;const H=260,left=40,right=14,top=12,bot=54;
  const withD=rows.filter(r=>r.date);if(!withD.length){empty(c,W);return[];}
  const keyOf=r=>`${r.date.y}-${String(r.date.mo+1).padStart(2,"0")}`;
  const cats=uniq(withD,r=>r[catKey]);const months=[...new Set(withD.map(keyOf))].sort();
  const idx={};months.forEach((m,i)=>idx[m]=i);
  const stack=months.map(()=>({}));let max=0;
  withD.forEach(r=>{const m=stack[idx[keyOf(r)]];const k=r[catKey]||"?";m[k]=(m[k]||0)+1;});
  stack.forEach(m=>{max=Math.max(max,Object.values(m).reduce((a,b)=>a+b,0));});max=Math.max(1,max);
  const s=svg(W,H);c.appendChild(s);const plotW=W-left-right,plotH=H-top-bot;
  for(let g=0;g<=4;g++){const y=top+plotH*g/4;el("line",{x1:left,y1:y,x2:W-right,y2:y,class:"grid-l"},s);
    el("text",{x:left-6,y:y+3,"text-anchor":"end",class:"axtick"},s).textContent=Math.round(max*(4-g)/4);}
  const bw=Math.max(3,plotW/months.length-3);
  months.forEach((m,i)=>{const x=left+i*plotW/months.length+1.5;let acc=0;
    cats.forEach(cat=>{const v=stack[i][cat]||0;if(!v)return;const h=v/max*plotH;const y=top+plotH-acc-h;
      const r=el("rect",{x,y,width:bw,height:Math.max(1,h-2),rx:2,fill:colf(cat)},s);
      tipOn(r,`<b>${esc(m)}</b><br><span class="k">${esc(cat)}:</span> ${v}`);acc+=h;});
    if(months.length<=24||i%Math.ceil(months.length/12)===0)
      el("text",{x:x+bw/2,y:H-bot+16,"text-anchor":"end",class:"axtick",transform:`rotate(-45 ${x+bw/2} ${H-bot+16})`},s).textContent=m;});
  return cats.map(cat=>[cat,colf(cat)]);
}
/* ---- per-device dot timeline ---- */
function dotLanes(c,rows,W){W=W||1000;const withD=rows.filter(r=>r.date&&r.device);if(!withD.length){empty(c,W);return[];}
  const byDev={};withD.forEach(r=>{(byDev[r.device]=byDev[r.device]||[]).push(r);});
  let devs=Object.entries(byDev).sort((a,b)=>b[1].length-a[1].length).slice(0,14);
  const left=190,right=16,top=10,rowH=24,bot=40;const H=top+bot+devs.length*rowH;
  const ts=withD.map(r=>r.date.t);const min=Math.min(...ts),max=Math.max(...ts)||min+1;const span=Math.max(1,max-min);
  const s=svg(W,H);c.appendChild(s);const plotW=W-left-right;
  const X=t=>left+(t-min)/span*plotW;
  const years=[];for(let y=new Date(min).getUTCFullYear();y<=new Date(max).getUTCFullYear();y++)years.push(y);
  years.forEach(y=>{const x=X(Date.UTC(y,0,1));if(x>=left&&x<=W-right){el("line",{x1:x,y1:top,x2:x,y2:H-bot,class:"grid-l"},s);
    el("text",{x,y:H-bot+16,"text-anchor":"middle",class:"axtick"},s).textContent=y;}});
  devs.forEach(([dev,list],i)=>{const y=top+i*rowH+rowH/2;
    el("text",{x:left-10,y:y+4,"text-anchor":"end",class:"axtick"},s).textContent=dev.length>28?dev.slice(0,27)+"…":dev;
    el("line",{x1:left,y1:y,x2:W-right,y2:y,stroke:"#141d30","stroke-width":1},s);
    list.forEach(r=>{const cx=X(r.date.t);const dt=new Date(r.date.t).toISOString().slice(0,10);
      const cc=el("circle",{cx,cy:y,r:4.2,fill:SRC_COL[r.source]||OTHER,stroke:"#0a0e17","stroke-width":1},s);
      tipOn(cc,`<b>${esc(dev)}</b><br><span class="k">${esc(r.source)}</span> · ${esc(r.version||r.model||"")}<br><span class="k">${dt}</span> · ${esc(r.region||"")}`);});
  });
  return uniq(withD,r=>r.source).map(sc=>[sc,SRC_COL[sc]||OTHER]);
}
/* ---- launch scatter by vendor lane ---- */
function launchScatter(c,devs,W){W=W||1000;const withD=devs.filter(d=>d.announced);if(!withD.length){empty(c,W);return[];}
  const vendors=uniq(withD,d=>d.vendor).sort();const left=110,right=16,top=12,laneH=30,bot=40;
  const H=top+bot+vendors.length*laneH;const ts=withD.map(d=>d.announced.t);const min=Math.min(...ts),max=Math.max(...ts)||min+1;const span=Math.max(1,max-min);
  const s=svg(W,H);c.appendChild(s);const plotW=W-left-right;const X=t=>left+(t-min)/span*plotW;
  const laneY={};vendors.forEach((v,i)=>laneY[v]=top+i*laneH+laneH/2);
  const y0=new Date(min).getUTCFullYear(),y1=new Date(max).getUTCFullYear();
  for(let y=y0;y<=y1;y++){const x=X(Date.UTC(y,0,1));el("line",{x1:x,y1:top,x2:x,y2:H-bot,class:"grid-l"},s);
    el("text",{x,y:H-bot+16,"text-anchor":"middle",class:"axtick"},s).textContent=y;}
  vendors.forEach(v=>{el("text",{x:left-10,y:laneY[v]+4,"text-anchor":"end",class:"axtick"},s).textContent=v;
    el("line",{x1:left,y1:laneY[v],x2:W-right,y2:laneY[v],stroke:"#141d30","stroke-width":1},s);});
  withD.forEach(d=>{const cx=X(d.announced.t),cy=laneY[d.vendor]+(Math.random?0:0);
    const cc=el("circle",{cx,cy,r:4.5,fill:VENDOR_COL[d.vendor]||OTHER,stroke:"#0a0e17","stroke-width":1,opacity:.92},s);
    tipOn(cc,`<b>${esc(d.name)}</b><br><span class="k">announced</span> ${d.announced.y}-${String(d.announced.mo+1).padStart(2,"0")}<br>${esc(d.chipset||"")}`);});
  return vendors.map(v=>[v,VENDOR_COL[v]||OTHER]);
}
/* ---- histogram ---- */
function histogram(c,vals,W,unit){W=W||440;vals=vals.filter(v=>v>0);if(!vals.length){empty(c,W);return;}
  const H=210,left=34,right=12,top=10,bot=42;const min=Math.min(...vals),max=Math.max(...vals);
  const nb=8,bw=(max-min)/nb||1;const bins=new Array(nb).fill(0);vals.forEach(v=>{let b=Math.floor((v-min)/bw);if(b>=nb)b=nb-1;bins[b]++;});
  const mx=Math.max(1,...bins);const s=svg(W,H);c.appendChild(s);const plotW=W-left-right,plotH=H-top-bot;
  for(let g=0;g<=3;g++){const y=top+plotH*g/3;el("line",{x1:left,y1:y,x2:W-right,y2:y,class:"grid-l"},s);
    el("text",{x:left-6,y:y+3,"text-anchor":"end",class:"axtick"},s).textContent=Math.round(mx*(3-g)/3);}
  const cw=plotW/nb;bins.forEach((v,i)=>{const h=v/mx*plotH;const x=left+i*cw+2;const y=top+plotH-h;
    const r=el("rect",{x,y,width:cw-4,height:Math.max(1,h),rx:3,fill:SERIES[0]},s);
    tipOn(r,`<b>${Math.round(min+i*bw)}–${Math.round(min+(i+1)*bw)} ${unit}</b><br>${v} devices`);});
  el("text",{x:left,y:H-bot+16,class:"axtick"},s).textContent=Math.round(min)+unit;
  el("text",{x:W-right,y:H-bot+16,"text-anchor":"end",class:"axtick"},s).textContent=Math.round(max)+unit;
}
function count(arr,f){const m={};arr.forEach(x=>{const k=f(x);if(k==null||k==="")return;m[k]=(m[k]||0)+1;});
  return Object.entries(m).map(([label,value])=>({label,value})).sort((a,b)=>b.value-a.value);}

/* ---- dynamic pivot (Explore) ---- */
const DIMS_DEV={vendor:d=>d.vendor,chipset:d=>d.chip,"Android version":d=>d.android?("Android "+d.android):null,
  RAM:d=>d.ram?d.ram+" GB":null,"launch year":d=>d.announced?String(d.announced.y):null,
  "battery band":d=>d.battery?(Math.floor(d.battery/1000)+"–"+(Math.floor(d.battery/1000)+1)+"k mAh"):null};
const DIMS_ROM={source:r=>r.source,region:r=>r.region,"Android version":r=>r.android?("Android "+r.android):null,
  "firmware type":r=>r.type,branch:r=>r.branch,vendor:r=>r.vendor,"release year":r=>r.date?String(r.date.y):null};
const pivotState={dataset:"devices",group:"chipset",split:"vendor"};
function splitColorFn(dim){
  if(dim==="vendor")return k=>VENDOR_COL[k]||OTHER;
  if(dim==="source")return k=>SRC_COL[k]||OTHER;
  if(dim==="region")return k=>REGION_COL[k]||OTHER;
  if(dim==="Android version")return k=>ANDROID_COL[String(k).replace("Android ","")]||OTHER;
  return (k,i)=>SERIES[i%8];
}
/* stacked horizontal bars: groups=[{label,parts,total}], cats ordered, colf(cat,i) */
function stackedHBar(host,groups,cats,colf,W){W=W||1000;
  if(!groups.length){empty(host,W);return[];}
  const rowH=26,padT=6,padB=8,left=Math.min(210,Math.max(60,...groups.map(g=>g.label.length))*6.4+10),right=52;
  const H=padT+padB+groups.length*rowH;const s=svg(W,H);host.appendChild(s);
  const max=Math.max(1,...groups.map(g=>g.total));const plotW=W-left-right;
  const cIdx={};cats.forEach((c,i)=>cIdx[c]=i);
  groups.forEach((g,i)=>{const y=padT+i*rowH;
    el("text",{x:left-8,y:y+rowH/2+4,"text-anchor":"end",class:"axtick"},s).textContent=g.label.length>30?g.label.slice(0,29)+"…":g.label;
    let acc=0;cats.forEach(cat=>{const v=g.parts[cat]||0;if(!v)return;const w=v/max*plotW;
      const r=el("rect",{x:left+acc,y:y+4,width:Math.max(1,w-2),height:rowH-10,rx:3,fill:colf(cat,cIdx[cat])},s);
      tipOn(r,`<b>${esc(g.label)}</b><br><span class="k">${esc(cat)}:</span> ${v}`);acc+=w;});
    el("text",{x:left+g.total/max*plotW+6,y:y+rowH/2+4,class:"barlabel"},s).textContent=g.total;});
  return cats.map((c,i)=>[c,colf(c,i)]);
}
function pivotAggregate(limit){
  const ds=pivotState.dataset;
  const rows=ds==="devices"?ADEV.filter(filtDev):AROM.filter(filtRom);
  const dims=ds==="devices"?DIMS_DEV:DIMS_ROM;
  const gf=dims[pivotState.group]||Object.values(dims)[0];
  const sf=(pivotState.split!=="none"&&dims[pivotState.split])?dims[pivotState.split]:null;
  const gmap={},catTot={};
  rows.forEach(r=>{const g=gf(r);if(g==null||g==="")return;const cat=sf?(sf(r)||"—"):"count";
    (gmap[g]=gmap[g]||{})[cat]=(gmap[g][cat]||0)+1;catTot[cat]=(catTot[cat]||0)+1;});
  let groups=Object.entries(gmap).map(([label,parts])=>({label,parts,total:Object.values(parts).reduce((a,b)=>a+b,0)}))
    .sort((a,b)=>b.total-a.total);
  if(limit)groups=groups.slice(0,limit);
  const cats=sf?Object.entries(catTot).sort((a,b)=>b[1]-a[1]).map(e=>e[0]):["count"];
  return {groups,cats,sf,total:rows.length};
}
function renderPivot(){
  const host=$("#pivotChart");if(!host)return;host.innerHTML="";
  const {groups,cats,sf}=pivotAggregate(16);
  const colf=sf?splitColorFn(pivotState.split):(()=>SERIES[0]);
  const leg=stackedHBar(host,groups,cats,colf);
  const legHost=$("#pivotLegend");legHost.innerHTML="";
  if(sf&&leg.length)legHost.innerHTML=`<div class="legend">${leg.slice(0,12).map(([l,col])=>`<span><i style="background:${col}"></i>${esc(l)}</span>`).join("")}${leg.length>12?'<span class="dim">+'+(leg.length-12)+' more</span>':""}</div>`;
}
function toCSV(rows){return rows.map(r=>r.map(c=>{c=String(c==null?"":c);
  return /[",\n]/.test(c)?'"'+c.replace(/"/g,'""')+'"':c;}).join(",")).join("\r\n");}
function downloadCSV(name,text){const b=new Blob([text],{type:"text/csv;charset=utf-8"});const u=URL.createObjectURL(b);
  const a=document.createElement("a");a.href=u;a.download=name;document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(u),1500);}
function exportPivotCSV(){
  const {groups,cats,sf}=pivotAggregate(0);   // 0 = all groups, not just top 16
  const header=sf?[pivotState.group,...cats,"total"]:[pivotState.group,"count"];
  const body=groups.map(g=>sf?[g.label,...cats.map(c=>g.parts[c]||0),g.total]:[g.label,g.total]);
  const fname=`pivot_${pivotState.dataset}_${pivotState.group}${sf?"_by_"+pivotState.split:""}.csv`.replace(/\s+/g,"-");
  downloadCSV(fname,toCSV([header,...body]));
}
function buildPivotControls(host){
  const dims=pivotState.dataset==="devices"?DIMS_DEV:DIMS_ROM;
  const opt=(v,cur)=>`<option ${v===cur?"selected":""}>${esc(v)}</option>`;
  host.innerHTML=`
    <label>Dataset <select id="pvDs">${opt("devices",pivotState.dataset)}${opt("firmware",pivotState.dataset)}</select></label>
    <span class="arrow">·</span>
    <label>Group by <select id="pvGroup">${Object.keys(dims).map(k=>opt(k,pivotState.group)).join("")}</select></label>
    <span class="arrow">split by</span>
    <label><select id="pvSplit"><option ${pivotState.split==="none"?"selected":""}>none</option>${Object.keys(dims).map(k=>opt(k,pivotState.split)).join("")}</select></label>
    <button class="btn" id="pvCsv" style="margin-left:auto;padding:7px 12px">⬇ Export CSV</button>`;
  $("#pvDs").onchange=e=>{pivotState.dataset=e.target.value;const d=pivotState.dataset==="devices"?DIMS_DEV:DIMS_ROM;
    if(!d[pivotState.group])pivotState.group=Object.keys(d)[0];if(pivotState.split!=="none"&&!d[pivotState.split])pivotState.split="none";
    buildPivotControls(host);renderPivot();};
  $("#pvGroup").onchange=e=>{pivotState.group=e.target.value;renderPivot();};
  $("#pvSplit").onchange=e=>{pivotState.split=e.target.value;renderPivot();};
  $("#pvCsv").onclick=exportPivotCSV;
}

/* ---- build all charts ---- */
function renderAnalytics(){
  enrich();
  if(!$("#filterbar").children.length)buildFilterBar();
  const dev=ADEV.filter(filtDev), rom=AROM.filter(filtRom);
  $("#fCount") && ($("#fCount").textContent=`${dev.length} devices · ${rom.length} firmware builds`);
  const g=$("#chartgrid");g.innerHTML="";

  let ex=card("Explore — build your own view","Pivot the corpus by any dimension. Try: group by chipset, split by vendor.",true);
  ex.classList.add("explore");
  ex.insertAdjacentHTML("beforeend",`<div class="pivot-ctl" id="pivotCtl"></div><div id="pivotChart"></div><div id="pivotLegend"></div>`);
  buildPivotControls($("#pivotCtl"));renderPivot();

  let c=card("Firmware releases over time","Monthly build count, stacked by source",true);
  let leg=stackedMonths(c,rom,"source",s=>SRC_COL[s]||OTHER);legend(c,leg);

  c=card("Firmware release timeline per device","Each dot is a firmware build; ×14 most-active devices, colored by source",true);
  leg=dotLanes(c,rom);if(leg.length)legend(c,leg);

  c=card("Model releases over time","New device models per month, stacked by vendor",true);
  leg=stackedMonths(c,dev.filter(d=>d.announced).map(d=>({date:d.announced,vendor:d.vendor})),"vendor",v=>VENDOR_COL[v]||OTHER);
  if(leg.length)legend(c,leg);

  c=card("Device launch timeline","Each dot is a device at its gsmarena announce date, by vendor",true);
  leg=launchScatter(c,dev);if(leg.length)legend(c,leg);

  c=card("Firmware builds by source","How many builds each repository contributes");
  hbar(c,count(rom,r=>r.source),l=>SRC_COL[l]||OTHER);

  c=card("Firmware builds by region","CSC / market region across all sources");
  hbar(c,count(rom,r=>r.region).slice(0,12),l=>REGION_COL[l]||OTHER);

  c=card("Android version spread","Firmware builds per Android major version");
  hbar(c,count(rom,r=>r.android?("Android "+r.android):null),(l,i)=>SERIES[i%8]);

  c=card("Devices by vendor","Device count per manufacturer (gsmarena specs)");
  hbar(c,count(dev,d=>d.vendor),l=>VENDOR_COL[l]||OTHER);

  c=card("Battery capacity distribution","Devices by battery size (mAh)");
  histogram(c,dev.map(d=>d.battery),440," mAh");

  c=card("Top chipsets","Devices per SoC family (gsmarena specs)");
  hbar(c,count(dev,d=>d.chip).slice(0,12),(l,i)=>SERIES[i%8]);

  c=card("Firmware type split","Fastboot / recovery / stock / firmware across builds");
  hbar(c,count(rom,r=>r.type),l=>({fastboot:"#f5b13d",recovery:"#38d39f",stock:"#5b8cff",firmware:"#9085e9"}[l]||OTHER));

  c=card("RAM configurations","Devices by max RAM (GB)");
  hbar(c,count(dev,d=>d.ram?d.ram+" GB":null).sort((a,b)=>parseInt(a.label)-parseInt(b.label)),(l,i)=>SERIES[i%8]);

  c=card("Android version spread — devices","Device count per shipped Android version");
  hbar(c,count(dev,d=>d.android?("Android "+d.android):null).sort((a,b)=>parseInt(b.label.slice(8))-parseInt(a.label.slice(8))),l=>ANDROID_COL[l.slice(8)]||OTHER);
}

fetch("/api/all").then(r=>r.json()).then(d=>{ALL=d;enrich();VIS=new Set(d.devices.defaults);renderStats();buildPop();render();});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif p == "/api/all":
            self._send(json.dumps(read_all()).encode(), "application/json")
        else:
            self.send_error(404)

    def do_POST(self):
        # browser-side ingest for Cloudflare-gated sources (samfw): write raw JSON
        # models to output/<source>/ so export.py can pick them up.
        p = urlparse(self.path).path
        if p != "/ingest":
            self.send_error(404)
            return
        # ingest writes files — only ever from the local machine, never the LAN
        if self.client_address[0] not in ("127.0.0.1", "::1", "localhost"):
            self.send_error(403, "ingest is localhost-only")
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self.send_response(400); self._cors(); self.end_headers(); return
        src = payload.get("source", "samfw.com")
        models = payload.get("models", [])
        out = INGEST_DIR / re.sub(r"[^a-z0-9.]", "_", src)
        out.mkdir(parents=True, exist_ok=True)
        wrote = 0
        for m in models:
            m.setdefault("source", src)
            fn = re.sub(r"[^A-Za-z0-9]+", "_", (m.get("model") or f"m{wrote}")).lower()
            (out / f"{fn}.json").write_text(json.dumps(m, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
            wrote += 1
        body = json.dumps({"ok": True, "wrote": wrote, "dir": str(out)}).encode()
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


def _lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    global DB_PATH
    ap = argparse.ArgumentParser(description="Firmware Atlas — local device & ROM explorer.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default="data")
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address; 0.0.0.0 exposes it on the lab LAN (default), "
                         "127.0.0.1 keeps it local-only")
    args = ap.parse_args()
    DB_PATH = Path(args.data) / "devices.db"
    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} not found — run `python export.py` first.")
    lan = _lan_ip()
    print(f"[app] Firmware Atlas — bound {args.host}:{args.port}  (Ctrl-C to stop)")
    print(f"[app]   local:   http://localhost:{args.port}")
    if args.host == "0.0.0.0":
        print(f"[app]   lab LAN: http://{lan}:{args.port}   <- share this on the lab")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
