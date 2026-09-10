"""ui.py — the Firmware Atlas front-end.

Design system synthesised from three independent design specs. The rules that
drive every decision here:

  * Identity is TYPOGRAPHIC, not chromatic. 9 vendors and 36 regions exceed the
    ~8-slot accessible categorical ceiling, so they get monogram chips and mono
    codes — never a hue. Colour is rationed to chart marks + status.
  * Identifiers are monospace with the shared prefix dimmed, so the discriminating
    tail of S918BXXS9AZHL pops out of a column of near-identical strings.
  * Status is ALWAYS glyph + label + colour. The status hexes fail CVD separation
    on hue alone (critical/good ΔE 4.1 deuteranopia), so hue never carries it.
  * Grain ("latest per product") is an AXIS, not a filter — it rolls up rather
    than filtering, so it gets its own control and its own summary grammar.
  * Absent data never impersonates present data: no date renders as "·· no date",
    never as an empty cell or epoch 0.
  * No zebra striping; rhythm comes from month rules, which carry information.
"""

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Firmware Atlas</title>
<style>
:root{
  color-scheme:dark;
  /* neutral ramp — one cool-slate hue, computed */
  --bg-canvas:#080A0D; --bg-surface:#101317; --bg-sunken:#181B1F; --bg-hover:#181B1F;
  --bg-selected:#2C2F34; --bg-overlay:#222529;
  --line-row:#1D2024; --line-subtle:#222529; --line-strong:#2C2F34; --line-group:#383B41;
  --ink-primary:#F0F2F4; --ink-secondary:#B4B8BD; --ink-muted:#989CA2; --ink-faint:#7C8187;
  --accent:#3987E5; --accent-hover:#5598E7; --accent-wash:rgba(57,135,229,.12);
  /* status marks — always paired with glyph + label */
  --st-good:#0CA30C; --st-warn:#FAB219; --st-serious:#EC835A; --st-critical:#E14A45;
  /* validated categorical slots (charts only) */
  --s1:#3987E5; --s2:#D95926; --s3:#199E70; --s4:#C98500; --s5:#D55181;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Mono",Menlo,monospace;
  --row-h:32px;
}
:root[data-theme="light"]{
  color-scheme:light;
  --bg-canvas:#F0F2F4; --bg-surface:#F8F9FB; --bg-sunken:#F0F2F4; --bg-hover:#F4F5F7;
  --bg-selected:#E1E3E6; --bg-overlay:#FFFFFF;
  --line-row:#E9EBED; --line-subtle:#E1E3E6; --line-strong:#D7D9DD; --line-group:#CDCFD3;
  --ink-primary:#181B1F; --ink-secondary:#474B50; --ink-muted:#61656B; --ink-faint:#7C8187;
  --accent:#2A78D6; --accent-hover:#1C5CAB; --accent-wash:rgba(42,120,214,.10);
  --st-critical:#C42D2D;
  --s1:#2A78D6; --s2:#EB6834; --s3:#1BAF7A; --s4:#EDA100; --s5:#E87BA4;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg-canvas);color:var(--ink-primary);
  font:400 13px/20px var(--sans);-webkit-font-smoothing:antialiased;display:flex;flex-direction:column}
.num,.date,td.n,th.n{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1,"zero" 1}

/* ── top bar ─────────────────────────────────────────────── */
.top{height:48px;flex:none;display:flex;align-items:center;gap:18px;padding:0 16px;
  background:var(--bg-canvas);border-bottom:1px solid var(--line-subtle)}
.brand{display:flex;align-items:center;gap:8px;font-weight:600;letter-spacing:-.006em;font-size:14px}
.brand i{width:16px;height:16px;border-radius:3px;background:var(--accent);display:inline-block;
  font-style:normal;flex:none}
.nav{display:flex;gap:2px}
.nav button{background:none;border:0;color:var(--ink-secondary);font:500 13px var(--sans);
  padding:6px 11px;border-radius:5px;cursor:pointer}
.nav button:hover{background:var(--bg-hover);color:var(--ink-primary)}
.nav button.on{background:var(--bg-selected);color:var(--ink-primary)}
.nav .n{font-size:11px;color:var(--ink-faint);margin-left:6px;font-variant-numeric:tabular-nums}
.spacer{flex:1}
.kbtn{display:flex;align-items:center;gap:8px;background:var(--bg-surface);
  border:1px solid var(--line-subtle);border-radius:5px;padding:5px 9px;color:var(--ink-faint);
  font:400 12px var(--sans);cursor:pointer;min-width:220px}
.kbtn kbd{margin-left:auto;font:500 10px var(--mono);color:var(--ink-faint);
  border:1px solid var(--line-strong);border-radius:3px;padding:1px 4px}
.fresh{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--ink-muted);
  font-variant-numeric:tabular-nums;cursor:pointer}
.iconbtn{background:none;border:1px solid var(--line-subtle);border-radius:5px;
  color:var(--ink-secondary);width:28px;height:28px;cursor:pointer;font-size:13px;line-height:1}
.iconbtn:hover{background:var(--bg-hover);color:var(--ink-primary)}

/* ── scale line ───────────────────────────────────────────── */
.scale{flex:none;padding:10px 16px 8px;display:flex;align-items:baseline;gap:14px;
  font-variant-numeric:tabular-nums}
.scale b{font-weight:600}
.scale .sep{color:var(--line-group)}
.scale .meta{margin-left:auto;font-size:11px;color:var(--ink-muted)}

/* ── layout ───────────────────────────────────────────────── */
.main{flex:1;display:flex;min-height:0}
.rail{width:216px;flex:none;border-right:1px solid var(--line-subtle);overflow-y:auto;
  padding:4px 0 24px;background:var(--bg-canvas)}
.rail.hide{display:none}
.grp{padding:12px 12px 4px}
.grp h4{margin:0 0 6px;font:500 11px/14px var(--sans);letter-spacing:.04em;text-transform:uppercase;
  color:var(--ink-muted);display:flex;align-items:center;gap:6px}
.grp h4 .c{margin-left:auto;color:var(--ink-faint);font-variant-numeric:tabular-nums}
.opt{display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:4px;cursor:pointer;
  font-size:12px;color:var(--ink-secondary);line-height:18px}
.opt:hover{background:var(--bg-hover);color:var(--ink-primary)}
.opt .bx{width:12px;height:12px;flex:none;border:1px solid var(--line-group);border-radius:3px;
  display:grid;place-items:center;font-size:9px;color:var(--bg-canvas)}
.opt.on .bx{background:var(--accent);border-color:var(--accent);color:#fff}
.opt .lbl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.opt .n{margin-left:auto;font-size:11px;color:var(--ink-faint);font-variant-numeric:tabular-nums}
.opt.mono .lbl{font-family:var(--mono);font-size:11.5px;letter-spacing:.02em;text-transform:uppercase}
.more{font-size:11px;color:var(--accent);background:none;border:0;cursor:pointer;padding:3px 6px}
.rail input[type=text],.rail input[type=date]{width:100%;background:var(--bg-surface);
  border:1px solid var(--line-subtle);border-radius:5px;color:var(--ink-primary);
  font:400 12px var(--sans);padding:5px 8px;outline:none}
.rail input:focus{border-color:var(--accent)}
.dates{display:flex;gap:6px;align-items:center;color:var(--ink-faint);font-size:11px}

/* ── content ──────────────────────────────────────────────── */
.content{flex:1;min-width:0;display:flex;flex-direction:column;background:var(--bg-surface);
  border-left:1px solid var(--line-subtle)}
.tools{flex:none;display:flex;align-items:center;gap:10px;padding:8px 14px;
  border-bottom:1px solid var(--line-subtle);background:var(--bg-surface)}
.seg{display:flex;background:var(--bg-sunken);border:1px solid var(--line-subtle);border-radius:6px;padding:2px}
.seg button{background:none;border:0;color:var(--ink-muted);font:500 12px var(--sans);
  padding:4px 10px;border-radius:4px;cursor:pointer;white-space:nowrap}
.seg button.on{background:var(--bg-selected);color:var(--ink-primary)}
.tbtn{background:var(--bg-sunken);border:1px solid var(--line-subtle);border-radius:5px;
  color:var(--ink-secondary);font:500 12px var(--sans);padding:5px 10px;cursor:pointer}
.tbtn:hover{color:var(--ink-primary)}
.tbtn.on{border-color:var(--accent);color:var(--ink-primary);background:var(--accent-wash)}
.chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chip{display:inline-flex;align-items:center;gap:6px;background:var(--bg-sunken);
  border:1px solid var(--accent);border-radius:4px;padding:2px 6px;font-size:11.5px;color:var(--ink-secondary)}
.chip b{font-weight:500;color:var(--ink-primary)}
.chip button{background:none;border:0;color:var(--ink-faint);cursor:pointer;padding:0;font-size:12px}
.count{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:12px;
  color:var(--ink-muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.pg{background:var(--bg-sunken);border:1px solid var(--line-subtle);border-radius:4px;
  color:var(--ink-secondary);cursor:pointer;width:22px;height:22px;line-height:1;font-size:13px}
.pg:disabled{opacity:.35;cursor:default}

.wrap{flex:1;overflow:auto;position:relative}
table{width:100%;border-collapse:collapse}
thead th{position:sticky;top:0;z-index:2;background:var(--bg-sunken);text-align:left;
  font:500 11px/14px var(--sans);letter-spacing:.04em;text-transform:uppercase;color:var(--ink-muted);
  padding:9px 12px;border-bottom:1px solid var(--line-strong);white-space:nowrap;cursor:pointer;
  font-feature-settings:"case" 1}
thead th.sorted{color:var(--ink-primary);font-weight:600}
thead th.n,tbody td.n{text-align:right}
tbody td{padding:0 12px;height:var(--row-h);border-bottom:1px solid var(--line-row);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:340px}
tbody tr{position:relative}
tbody tr:hover td{background:var(--bg-hover)}
tbody tr.sel td{background:var(--bg-selected)}
tbody tr.sel td:first-child{box-shadow:inset 2px 0 0 var(--accent)}
tbody tr.clk{cursor:pointer}
.grouprule td{background:var(--bg-canvas);border-bottom:1px solid var(--line-group);
  height:22px;font:500 11px var(--mono);letter-spacing:.06em;color:var(--ink-muted);
  text-transform:uppercase}
.grouprule .gc{float:right;color:var(--ink-faint);font-variant-numeric:tabular-nums}

/* identity: typography, never hue */
.mono{font-family:var(--mono);font-size:12.5px;letter-spacing:.01em;
  font-variant-ligatures:none;font-feature-settings:"zero" 1;-webkit-user-select:all}
.vpre{color:var(--ink-faint);font-weight:400}
.vtail{color:var(--ink-primary);font-weight:600}
.mg{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:3px;
  background:var(--bg-sunken);border:1px solid var(--line-subtle);font:600 9.5px var(--mono);
  color:var(--ink-secondary);letter-spacing:.02em;flex:none}
.dev{display:flex;align-items:center;gap:8px;min-width:0}
.dev span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rgn{font-family:var(--mono);font-size:11.5px;letter-spacing:.02em;text-transform:uppercase;
  color:var(--ink-secondary)}
.unit{font-size:11px;color:var(--ink-muted)}
.nodate{color:var(--ink-faint);border-bottom:1px dotted var(--line-group);font-size:12px}
.dt i{font-style:normal;color:var(--ink-faint);padding:0 .5px}
.rel{color:var(--ink-faint);font-size:11px}
/* status: glyph + label, colour only reinforces */
.st{display:inline-flex;align-items:center;gap:5px;font:500 10.5px var(--sans);
  letter-spacing:.04em;text-transform:uppercase;white-space:nowrap}
.st .g{font-size:11px;line-height:1}
.st.good{color:var(--st-good)} .st.warn{color:var(--st-warn)}
.st.ser{color:var(--st-serious)} .st.crit{color:var(--st-critical)}
.st.unk{color:var(--ink-faint)}
.lnk{color:var(--accent);text-decoration:none;font-size:11.5px}
.lnk:hover{color:var(--accent-hover);text-decoration:underline}
.spark{display:inline-block;vertical-align:middle}
.mini-t{width:100%;border-collapse:collapse;table-layout:fixed}
.mini-t td{padding:6px 8px 6px 0;border-bottom:1px solid var(--line-row);height:auto;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.5px}
.mini-t td:last-child{padding-right:0}
.tr-dev{color:var(--ink-secondary);overflow:hidden;text-overflow:ellipsis}
.covnote{padding:6px 14px;font-size:11.5px;color:var(--ink-muted);background:var(--bg-sunken);
  border-bottom:1px solid var(--line-subtle);display:flex;gap:8px;align-items:center}
.star{background:none;border:0;cursor:pointer;color:var(--ink-faint);font-size:13px;padding:0 2px;line-height:1}
.star.on{color:var(--st-warn)}
.wbuild{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin-bottom:10px}
.wbuild label{display:block;font:500 10.5px var(--sans);letter-spacing:.04em;text-transform:uppercase;
  color:var(--ink-muted);margin-bottom:3px}
.wbuild select,.wbuild input{width:100%;background:var(--bg-sunken);border:1px solid var(--line-subtle);
  border-radius:5px;color:var(--ink-primary);font:400 12.5px var(--sans);padding:6px 8px;outline:none}
.wbuild select:focus,.wbuild input:focus{border-color:var(--accent)}
.prev{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--ink-muted);margin:2px 0 10px}
.prev b{color:var(--accent);font-variant-numeric:tabular-nums;font-size:14px}
.btn{background:var(--accent);border:0;border-radius:5px;color:#fff;font:600 12.5px var(--sans);
  padding:7px 14px;cursor:pointer}
.btn:hover{background:var(--accent-hover)}
.btn.ghost{background:none;border:1px solid var(--line-subtle);color:var(--ink-secondary)}
.wrow{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line-row)}
.wrow .lb{font-weight:500}
.wrow .pd{font-size:11px;color:var(--ink-faint);font-family:var(--mono)}
.wrow .rt{margin-left:auto;display:flex;align-items:center;gap:12px;font-size:11.5px;color:var(--ink-muted)}
.badge{display:inline-flex;align-items:center;gap:4px;background:var(--accent-wash);color:var(--accent);
  border:1px solid var(--accent);border-radius:999px;padding:1px 8px;font:600 10.5px var(--sans);
  letter-spacing:.03em}
.badge.sec{background:rgba(225,74,69,.14);color:var(--st-critical);border-color:var(--st-critical)}
.inbx{padding:8px 0;border-bottom:1px solid var(--line-row);display:flex;gap:10px;align-items:flex-start}
.inbx .bd{flex:1;min-width:0}
.inbx .t1{font-weight:500}
.inbx .t2{font-size:11.5px;color:var(--ink-muted);margin-top:2px}

/* ── cards / insights ─────────────────────────────────────── */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:12px;padding:14px}
.card{background:var(--bg-surface);border:1px solid var(--line-subtle);border-radius:7px;padding:14px 16px}
.card.wide{grid-column:1/-1}
.card h3{margin:0 0 2px;font:600 14px var(--sans);letter-spacing:-.006em}
.card .sub{margin:0 0 12px;font-size:11.5px;color:var(--ink-muted)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;padding:14px 14px 0}
.tile{background:var(--bg-surface);border:1px solid var(--line-subtle);border-radius:7px;padding:12px 14px}
.tile .v{font:600 28px/32px var(--sans);letter-spacing:-.018em;font-variant-numeric:proportional-nums}
.tile .l{font-size:11.5px;color:var(--ink-muted);margin-top:2px}
.tile .m{font-size:11px;color:var(--ink-faint);margin-top:5px;font-variant-numeric:tabular-nums}
.hero{font:600 40px/44px var(--sans);letter-spacing:-.022em;font-variant-numeric:proportional-nums}
.meter{height:4px;border-radius:2px;background:var(--line-subtle);overflow:hidden;margin-top:8px}
.meter i{display:block;height:100%;background:var(--accent)}
.bars{display:flex;flex-direction:column;gap:6px}
.bar{display:grid;grid-template-columns:132px 1fr auto;align-items:center;gap:10px;font-size:12px}
.bar .t{color:var(--ink-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar .track{height:14px;background:var(--bg-sunken);border-radius:3px;overflow:hidden}
.bar .track i{display:block;height:100%;background:var(--s1);border-radius:0 3px 3px 0}
.bar .v{font-variant-numeric:tabular-nums;color:var(--ink-secondary);font-size:11.5px}
.mini{width:100%;height:112px;display:block}
.axis{font:400 10px var(--sans);fill:var(--ink-muted);font-variant-numeric:tabular-nums}
.gridl{stroke:var(--line-subtle);stroke-width:1}
.basel{stroke:var(--line-strong);stroke-width:1}

/* ── drawer ───────────────────────────────────────────────── */
.scrim{position:fixed;inset:0;background:rgba(4,7,12,.45);opacity:0;pointer-events:none;
  transition:opacity 140ms cubic-bezier(.4,0,1,1);z-index:40}
.scrim.show{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;bottom:0;width:520px;max-width:92vw;background:var(--bg-overlay);
  border-left:1px solid var(--line-strong);transform:translateX(100%);
  transition:transform 180ms cubic-bezier(.2,0,0,1);z-index:41;display:flex;flex-direction:column;
  box-shadow:-12px 0 32px rgba(0,0,0,.4)}
.drawer.show{transform:none}
.dh{flex:none;padding:14px 16px;border-bottom:1px solid var(--line-subtle);display:flex;
  align-items:flex-start;gap:10px}
.dh h2{margin:0;font:600 16px/22px var(--sans);letter-spacing:-.006em}
.dh .sub{font-size:11.5px;color:var(--ink-muted);margin-top:2px}
.db{flex:1;overflow-y:auto;padding:0 16px 24px}
.sect{padding:14px 0;border-bottom:1px solid var(--line-row)}
.sect .st2{font:500 11px var(--sans);letter-spacing:.04em;text-transform:uppercase;
  color:var(--ink-muted);margin-bottom:8px}
.kv{display:grid;grid-template-columns:132px 1fr;gap:5px 12px;font-size:12.5px}
.kv .k{color:var(--ink-muted)}
.kv .v{color:var(--ink-primary);word-break:break-word}
.romrow{display:flex;align-items:center;gap:9px;padding:6px 0;border-bottom:1px solid var(--line-row);
  font-size:12px}
.romrow .meta{margin-left:auto;color:var(--ink-muted);font-size:11px;display:flex;gap:9px;align-items:center;
  font-variant-numeric:tabular-nums}

/* ── palette ──────────────────────────────────────────────── */
.pal{position:fixed;top:14vh;left:50%;transform:translateX(-50%) scale(.985);width:min(640px,92vw);
  background:var(--bg-overlay);border:1px solid var(--line-strong);border-radius:10px;
  box-shadow:0 24px 64px rgba(0,0,0,.55);z-index:60;opacity:0;pointer-events:none;
  transition:opacity 80ms ease-out,transform 80ms ease-out;overflow:hidden}
.pal.show{opacity:1;pointer-events:auto;transform:translateX(-50%) scale(1)}
.pal input{width:100%;background:none;border:0;border-bottom:1px solid var(--line-subtle);
  color:var(--ink-primary);font:400 15px var(--sans);padding:14px 16px;outline:none}
.pal .res{max-height:52vh;overflow-y:auto;padding:6px}
.pal .sec{font:500 10.5px var(--sans);letter-spacing:.05em;text-transform:uppercase;
  color:var(--ink-faint);padding:8px 10px 4px}
.pal .it{display:flex;align-items:center;gap:9px;padding:7px 10px;border-radius:5px;cursor:pointer;font-size:13px}
.pal .it.on,.pal .it:hover{background:var(--bg-hover)}
.pal .it .m{margin-left:auto;font-size:11px;color:var(--ink-faint);font-variant-numeric:tabular-nums}
.pal .ft{border-top:1px solid var(--line-subtle);padding:7px 12px;font-size:10.5px;color:var(--ink-faint);
  display:flex;gap:14px}
.empty{padding:56px 20px;text-align:center;color:var(--ink-muted)}
.empty .h{font-size:13px;margin-bottom:6px;color:var(--ink-secondary)}
.copied{position:absolute;font:600 10px var(--mono);letter-spacing:.06em;color:var(--accent);
  text-transform:uppercase;pointer-events:none}
@media (prefers-reduced-motion:reduce){*{transition-duration:0ms!important;animation-duration:0ms!important}}
</style></head><body>

<div class="top">
  <div class="brand"><i></i>Firmware Atlas</div>
  <nav class="nav" id="nav"></nav>
  <div class="spacer"></div>
  <button class="kbtn" id="kbtn"><span>⌕</span><span>Search products, builds, chips…</span><kbd>⌘K</kbd></button>
  <div class="fresh" id="fresh"></div>
  <button class="iconbtn" id="theme" title="Toggle theme">◑</button>
</div>

<div class="scale" id="scale"></div>

<div class="main">
  <aside class="rail" id="rail"></aside>
  <section class="content">
    <div class="tools" id="tools"></div>
    <div class="wrap" id="wrap">
      <table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table>
      <div class="grid" id="grid" style="display:none"></div>
    </div>
  </section>
</div>

<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer"></aside>
<div class="pal" id="pal">
  <input id="palq" placeholder="Search products, model codes, chipsets…" autocomplete="off">
  <div class="res" id="palres"></div>
  <div class="ft"><span>↑↓ navigate</span><span>⏎ open</span><span>esc close</span></div>
</div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??"").replace(/[&<>"]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m]));
let ALL=null, VIEW="products", ROWS={rows:[],total:0,offset:0,limit:200}, PAGE_SIZE=200;
let SORT={col:"updated_at",d:-1}, SEL=-1, _seq=0, _t=null;
const F={vendor:new Set(),source:new Set(),region:new Set(),android:new Set(),type:new Set(),
         chip:new Set(),name:"",from:"",to:""};
let LATEST=true, DATED=false, SHOWALL={};

/* ── identity helpers: typography, not colour ─────────────── */
const MONOGRAM={Samsung:"SM",Apple:"AP",Xiaomi:"XM",Tecno:"TC",Google:"GG",OnePlus:"OP",
  Oppo:"OP",Vivo:"VV",Realme:"RM",Motorola:"MT",Other:"··"};
const mg=v=>`<i class="mg" title="${esc(v||"unknown")}">${esc(MONOGRAM[v]||(v||"··").slice(0,2).toUpperCase())}</i>`;
/* dim the common prefix so the discriminating tail pops */
function vstr(v,prefixLen){
  if(!v) return '<span class="nodate">—</span>';
  const s=String(v);
  if(!prefixLen||prefixLen>=s.length-2) return `<span class="mono">${esc(s)}</span>`;
  return `<span class="mono"><span class="vpre">${esc(s.slice(0,prefixLen))}</span><span class="vtail">${esc(s.slice(prefixLen))}</span></span>`;
}
function commonPrefix(list){
  if(list.length<2) return 0;
  let p=list[0]||"";
  for(const s of list){ let i=0; while(i<p.length&&i<(s||"").length&&p[i]===s[i])i++; p=p.slice(0,i); if(!p)break; }
  return Math.max(0,p.length-0);
}
function fdate(v){
  if(!v) return '<span class="nodate">·· no date</span>';
  const d=String(v).slice(0,10).split("-");
  if(d.length<3) return `<span class="mono">${esc(v)}</span>`;
  const days=Math.floor((Date.now()-Date.parse(String(v).slice(0,10)))/864e5);
  const rel=days<1?"today":days<30?days+"d":days<365?Math.floor(days/30)+"mo":Math.floor(days/365)+"y";
  return `<span class="mono dt">${d[0]}<i>‑</i>${d[1]}<i>‑</i>${d[2]}</span> <span class="rel">${rel}</span>`;
}
function fsize(v){ if(!v) return '<span class="nodate">—</span>';
  const m=String(v).match(/^([\d.,]+)\s*(\D+)?$/);
  return m?`<span class="num">${esc(m[1])}</span><span class="unit"> ${esc((m[2]||"").trim())}</span>`:esc(v); }
function status(r,ix){
  const d=r[ix("updated_at")];
  if(!d) return '<span class="st unk"><span class="g">—</span>unknown</span>';
  const days=Math.floor((Date.now()-Date.parse(String(d).slice(0,10)))/864e5);
  if(days<=45) return '<span class="st good"><span class="g">●</span>current</span>';
  if(days<=180) return '<span class="st warn"><span class="g">◐</span>ageing</span>';
  return '<span class="st ser"><span class="g">○</span>stale</span>';
}

/* ── data ─────────────────────────────────────────────────── */
function params(){
  const p=new URLSearchParams();
  for(const k of ["vendor","source","region","android","type","chip"]) if(F[k].size)p.set(k,[...F[k]].join("|"));
  if(F.name)p.set("q",F.name);
  if(F.from)p.set("from",F.from); if(F.to)p.set("to",F.to);
  if(LATEST)p.set("latest","1"); if(DATED)p.set("dated","1");
  p.set("sort",SORT.col); p.set("dir",SORT.d>0?"asc":"desc");
  p.set("offset",ROWS.offset); p.set("limit",PAGE_SIZE);
  return p.toString();
}
async function fetchRows(reset){
  if(reset){ROWS.offset=0;SEL=-1;}
  const seq=++_seq;
  try{
    const d=await (await fetch("/api/rows?"+params(),{cache:"no-store"})).json();
    if(seq!==_seq)return;
    ROWS={rows:d.rows,total:d.total,offset:d.offset,limit:d.limit};
    ALL.roms.columns=d.columns; render();
  }catch(e){ $("#body").innerHTML=`<tr><td colspan="9"><div class="empty"><div class="h">Could not load rows</div></div></td></tr>`; }
}
const debounce=(f,ms)=>{let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>f(...a),ms)}};
const refetch=debounce(()=>fetchRows(true),220);

/* ── chrome ───────────────────────────────────────────────── */
function renderNav(){
  const s=ALL.stats;
  $("#nav").innerHTML=[["board","Dashboard",""],["products","Devices",s.devices],
    ["roms","Releases",s.roms],["watch","Watch",""],["updates","Updates",""],["insights","Insights",""]]
    .map(([k,l,n])=>`<button data-v="${k}" class="${k===VIEW?"on":""}">${l}${n!==""?`<span class="n">${n.toLocaleString()}</span>`:""}</button>`).join("");
  $$("#nav button").forEach(b=>b.onclick=()=>setView(b.dataset.v));
  const c=(ALL.crawl||[])[0];
  $("#fresh").innerHTML=c?`<span class="st good"><span class="g">●</span>fresh</span><span>${esc(c.ran_at)}</span>`
    :`<span class="st unk"><span class="g">—</span>no pull recorded</span>`;
}
function renderScale(){
  const s=ALL.stats;
  $("#scale").innerHTML=`<span><b>${s.roms.toLocaleString()}</b> releases</span><span class="sep">·</span>
    <span><b>${s.devices}</b> devices</span><span class="sep">·</span>
    <span><b>${(ALL.facets.vendor||[]).length}</b> vendors</span><span class="sep">·</span>
    <span><b>${s.regions}</b> regions</span>
    <span class="meta">${s.last_refresh?"updated "+esc(s.last_refresh):"never pulled"}</span>`;
}
function facetGroup(key,label,opts,mono){
  const show=SHOWALL[key]?opts.length:6, more=opts.length-show;
  return `<div class="grp"><h4>${label}<span class="c">${opts.length}</span></h4>
    ${opts.slice(0,show).map(o=>`<div class="opt ${mono?"mono":""} ${F[key].has(o.v)?"on":""}" data-k="${key}" data-v="${esc(o.v)}">
      <span class="bx">${F[key].has(o.v)?"✓":""}</span><span class="lbl">${esc(o.v)}</span><span class="n">${(o.n||0).toLocaleString()}</span></div>`).join("")}
    ${more>0?`<button class="more" data-more="${key}">${SHOWALL[key]?"show less":"… "+more+" more"}</button>`:""}</div>`;
}
function renderRail(){
  const f=ALL.facets||{};
  $("#rail").innerHTML=
    `<div class="grp"><h4>Find</h4><input type="text" id="fname" placeholder="device name…" value="${esc(F.name)}"></div>`
    + facetGroup("vendor","Vendor",f.vendor||[])
    + facetGroup("chip","Chipset",f.chip||[])
    + facetGroup("region","Region",f.region||[],true)
    + facetGroup("android","OS",f.android||[],true)
    + facetGroup("source","Source",f.source||[])
    + `<div class="grp"><h4>Released</h4><div class="dates">
         <input type="date" id="ffrom" value="${esc(F.from)}"><span>→</span><input type="date" id="fto" value="${esc(F.to)}"></div></div>`;
  $$("#rail .opt").forEach(el=>el.onclick=()=>{
    const k=el.dataset.k,v=el.dataset.v;
    F[k].has(v)?F[k].delete(v):F[k].add(v); renderRail(); renderTools(); fetchRows(true);});
  $$("#rail .more").forEach(b=>b.onclick=()=>{SHOWALL[b.dataset.more]=!SHOWALL[b.dataset.more];renderRail();});
  $("#fname").oninput=e=>{F.name=e.target.value.trim();renderTools();refetch();};
  $("#ffrom").onchange=e=>{F.from=e.target.value;renderTools();fetchRows(true);};
  $("#fto").onchange=e=>{F.to=e.target.value;renderTools();fetchRows(true);};
}
function activeChips(){
  const out=[];
  for(const k of ["vendor","chip","region","android","source","type"])
    for(const v of F[k]) out.push([k,v]);
  if(F.name)out.push(["name",F.name]);
  if(F.from)out.push(["from",F.from]);
  if(F.to)out.push(["to",F.to]);
  return out;
}
function renderTools(){
  if(["watch","insights","board","updates"].includes(VIEW)){ $("#tools").innerHTML=""; return; }
  const isRel=VIEW==="roms", chips=activeChips();
  $("#tools").innerHTML=
    (isRel?`<div class="seg"><button data-g="1" class="${LATEST?"on":""}">Latest per device</button>
      <button data-g="0" class="${LATEST?"":"on"}">All releases</button></div>
      <button class="tbtn ${DATED?"on":""}" id="dated">Dated only</button>`:"")
    + `<div class="chips">${chips.map(([k,v])=>`<span class="chip"><b>${esc(k)}</b>${esc(v)}<button data-x="${esc(k)}" data-xv="${esc(v)}">✕</button></span>`).join("")}
        ${chips.length?`<button class="more" id="clr">Clear all</button>`:""}</div>`
    + `<div class="count" id="cnt"></div>`;
  $$("#tools .seg button").forEach(b=>b.onclick=()=>{LATEST=b.dataset.g==="1";renderTools();fetchRows(true);});
  const dt=$("#dated"); if(dt)dt.onclick=()=>{DATED=!DATED;renderTools();fetchRows(true);};
  $$("#tools .chip button").forEach(b=>b.onclick=()=>{
    const k=b.dataset.x,v=b.dataset.xv;
    if(F[k] instanceof Set)F[k].delete(v); else F[k]="";
    renderRail();renderTools();fetchRows(true);});
  const c=$("#clr"); if(c)c.onclick=()=>{
    ["vendor","source","region","android","type","chip"].forEach(k=>F[k].clear());
    F.name=F.from=F.to=""; renderRail();renderTools();fetchRows(true);};
  renderCovNote();
  renderCount();
}
function renderCovNote(){
  const c=(ALL&&ALL.coverage)||{}; if(!c.total)return;
  const notes=[];
  if(F.chip.size&&c.chipset!=null&&c.chipset<c.total)
    notes.push(`${(c.total-c.chipset).toLocaleString()} builds have no chipset on record and are excluded`);
  if(F.android.size&&c.android!=null&&c.android<c.total)
    notes.push(`${(c.total-c.android).toLocaleString()} builds have no OS on record and are excluded`);
  let el=document.getElementById("covnote");
  if(!notes.length){ if(el)el.remove(); return; }
  if(!el){ el=document.createElement("div"); el.id="covnote"; el.className="covnote";
    $("#tools").insertAdjacentElement("afterend",el); }
  el.innerHTML=`<span class="st unk"><span class="g">—</span>note</span> ${notes.join(" · ")}`;
}
function renderCount(){
  const el=$("#cnt"); if(!el)return;
  if(VIEW!=="roms"){el.textContent=`${ALL.devices.rows.length} devices`;return;}
  const a=ROWS.offset+1,b=Math.min(ROWS.offset+ROWS.rows.length,ROWS.total);
  const noun=LATEST?"devices":"releases";
  el.innerHTML=`<button class="pg" id="pv" ${ROWS.offset>0?"":"disabled"}>‹</button>
    <span>${ROWS.total?a.toLocaleString():0}–${b.toLocaleString()} of ${ROWS.total.toLocaleString()} ${noun}</span>
    <button class="pg" id="nx" ${ROWS.offset+ROWS.limit<ROWS.total?"":"disabled"}>›</button>`;
  const pv=$("#pv"),nx=$("#nx");
  if(pv)pv.onclick=()=>{ROWS.offset=Math.max(0,ROWS.offset-ROWS.limit);fetchRows(false);};
  if(nx)nx.onclick=()=>{ROWS.offset+=ROWS.limit;fetchRows(false);};
}

/* ── table ────────────────────────────────────────────────── */
const REL_COLS=[["","st",34],["device","Device",0],["model","Model",0],["version","Version",0],
  ["region","Rgn",0],["android","OS",0],["updated_at","Released",0],["size","Size",0],["",""]];
function render(){
  if(VIEW==="insights"){renderInsights();return;}
  if(VIEW==="watch"){renderWatch();return;}
  if(VIEW==="board"){renderBoard();return;}
  if(VIEW==="updates"){renderUpdates();return;}
  $("#grid").style.display="none"; $(".wrap table").style.display="";
  VIEW==="roms"?renderReleases():renderDevices();
  renderCount();
}
function renderReleases(){
  const cols=ALL.roms.columns, ix=c=>cols.indexOf(c);
  const rows=ROWS.rows;
  $("#head").innerHTML=REL_COLS.map(([k,l])=>
    `<th data-c="${k}" class="${["size","android"].includes(k)?"n":""} ${SORT.col===k?"sorted":""}">${esc(l)}${SORT.col===k?(SORT.d>0?" ▲":" ▼"):""}</th>`).join("");
  $$("#head th").forEach(th=>th.onclick=()=>{const c=th.dataset.c; if(!c)return;
    SORT.d=SORT.col===c?-SORT.d:-1;SORT.col=c;fetchRows(true);});
  if(!rows.length){
    $("#body").innerHTML=`<tr><td colspan="9"><div class="empty"><div class="h">No releases match these filters</div>
      <div>${activeChips().length} filter${activeChips().length===1?"":"s"} active — try removing one.</div></div></td></tr>`;return;}
  // common prefix per device, so the discriminating tail of the build id pops
  const byDev={}; rows.forEach(r=>{(byDev[r[ix("device")]]??=[]).push(r[ix("version")]||"");});
  const pfx={}; for(const d in byDev) pfx[d]=byDev[d].length>1?commonPrefix(byDev[d]):0;
  let html="",lastMonth=null;
  rows.forEach((r,i)=>{
    const d=r[ix("updated_at")], m=d?String(d).slice(0,7):"nodate";
    if(SORT.col==="updated_at"&&m!==lastMonth){
      lastMonth=m;
      const lbl=d?String(d).slice(0,7).replace("-"," · "):"NO DATE";
      html+=`<tr class="grouprule"><td colspan="9">${esc(lbl)}</td></tr>`;
    }
    html+=`<tr class="clk ${i===SEL?"sel":""}" data-i="${i}">
      <td>${status(r,ix)}</td>
      <td><div class="dev">${mg(r[ix("vendor")])}<span>${esc(r[ix("device")])}</span></div></td>
      <td><span class="mono">${esc(r[ix("model")]||"—")}</span></td>
      <td>${vstr(r[ix("version")],pfx[r[ix("device")]]||0)}</td>
      <td><span class="rgn">${esc(r[ix("region")]||"—")}</span></td>
      <td class="n">${esc(r[ix("android")]||"—")}</td>
      <td>${fdate(d)}</td>
      <td class="n">${fsize(r[ix("size")])}</td>
      <td>${(()=>{const u=r[ix("download_url")]; if(!u)return "";
        const k=ix("link_kind")>=0?r[ix("link_kind")]:"";
        const lbl=k==="page"?"↗ page":k==="file"?"↓ file":"↓ get";
        return `<a class="lnk" href="${esc(u)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="${esc(k||"link")}">${lbl}</a>`;})()}</td></tr>`;
  });
  $("#body").innerHTML=html;
  $$("#body tr.clk").forEach(tr=>tr.onclick=()=>selectRow(+tr.dataset.i));
}
function renderDevices(){
  const D=ALL.devices, cols=D.columns, ix=c=>cols.indexOf(c);
  const q=F.name.toLowerCase();
  const rows=D.rows.filter(r=>{
    if(q&&!String(r[ix("name")]).toLowerCase().includes(q))return false;
    if(F.chip.size&&!F.chip.has(r[ix("Platform — Chipset")]))return false;
    return true;});
  $("#head").innerHTML=["Device","Chipset","OS","Announced","Releases",""].map((l,i)=>
    `<th class="${l==="Releases"?"n":""}">${esc(l)}</th>`).join("");
  if(!rows.length){$("#body").innerHTML=`<tr><td colspan="6"><div class="empty"><div class="h">No devices match</div></div></td></tr>`;return;}
  $("#body").innerHTML=rows.map((r,i)=>{
    const n=+(r[ix("rom_count")]||0);
    return `<tr class="clk ${i===SEL?"sel":""}" data-i="${i}">
      <td><div class="dev">${mg(vendorOf(r[ix("name")]))}<span>${esc(r[ix("name")])}</span></div></td>
      <td><span style="color:var(--ink-secondary)">${esc(r[ix("Platform — Chipset")]||"—")}</span></td>
      <td><span class="rgn">${esc((r[ix("Platform — OS")]||"—").split(",")[0])}</span></td>
      <td>${esc(r[ix("Launch — Announced")]||"—")}</td>
      <td class="n"><span class="num">${n}</span></td>
      <td><span class="lnk">open →</span></td></tr>`;}).join("");
  $$("#body tr.clk").forEach(tr=>tr.onclick=()=>openDevice(rows[+tr.dataset.i]));
}
function vendorOf(n){n=(n||"").trim();
  if(/^iphone|^ipad/i.test(n))return "Apple";
  if(/^samsung|^sm-/i.test(n))return "Samsung";
  if(/^(redmi|poco|xiaomi|mi |mix)/i.test(n))return "Xiaomi";
  if(/^tecno/i.test(n))return "Tecno";
  return "Other";}
function selectRow(i){SEL=i;$$("#body tr").forEach(t=>t.classList.remove("sel"));
  const tr=$(`#body tr[data-i="${i}"]`); if(tr)tr.classList.add("sel");
  openRelease(ROWS.rows[i]);}

/* ── drawer ───────────────────────────────────────────────── */
function closeDrawer(){$("#drawer").classList.remove("show");$("#scrim").classList.remove("show");}
$("#scrim").onclick=closeDrawer;
function openRelease(r){
  if(!r)return;
  const cols=ALL.roms.columns, g=c=>r[cols.indexOf(c)];
  $("#drawer").innerHTML=`<div class="dh"><div style="flex:1;min-width:0">
      <h2>${esc(g("device"))}</h2>
      <div class="sub">${esc(g("model")||"")} · ${esc(g("vendor")||"")}</div></div>
      <button class="iconbtn" onclick="closeDrawer()">✕</button></div>
    <div class="db">
      <div class="sect"><div class="st2">Release</div>
        <div class="kv">
          <div class="k">Version</div><div class="v mono">${esc(g("version")||"—")}</div>
          <div class="k">Released</div><div class="v">${fdate(g("updated_at"))}</div>
          <div class="k">OS</div><div class="v">${esc(g("android")||"—")}</div>
          <div class="k">Region</div><div class="v rgn">${esc(g("region")||"—")}</div>
          <div class="k">Size</div><div class="v">${fsize(g("size"))}</div>
          <div class="k">Source</div><div class="v">${esc(g("source")||"—")}</div>
          ${g("chipset")?`<div class="k">Chipset</div><div class="v">${esc(g("chipset"))}</div>`:""}
          ${g("baseband")?`<div class="k">Baseband</div><div class="v mono">${esc(g("baseband"))}</div>`:""}
        </div></div>
      ${g("security_patch")?`<div class="sect"><div class="st2">Security</div>
        <a class="lnk" href="${esc(g("security_patch"))}" target="_blank" rel="noopener">⛨ Vendor security advisory ↗</a></div>`:""}
      <div class="sect"><div class="st2">Actions</div>
        ${g("download_url")?`<a class="lnk" href="${esc(g("download_url"))}" target="_blank" rel="noopener">↓ Download firmware</a><br>`:""}
        ${g("model_url")?`<a class="lnk" href="${esc(g("model_url"))}" target="_blank" rel="noopener">↗ Source page</a>`:""}
      </div></div>`;
  $("#drawer").classList.add("show");$("#scrim").classList.add("show");
}
async function openDevice(row){
  const cols=ALL.devices.columns, g=c=>{const i=cols.indexOf(c);return i<0?null:row[i];};
  const id=g("device_id"), name=g("name");
  const secs={}; cols.forEach((c,i)=>{const m=c.match(/^(.*?) — (.*)$/); if(m&&row[i])(secs[m[1]]??=[]).push([m[2],row[i]]);});
  $("#drawer").innerHTML=`<div class="dh"><div style="flex:1;min-width:0">
      <h2>${esc(name)}</h2><div class="sub">${esc(g("codenames")||"")} · ${g("rom_count")||0} releases</div></div>
      <button class="iconbtn" onclick="closeDrawer()">✕</button></div>
    <div class="db"><div class="sect"><div class="st2">Releases</div><div id="dr">loading…</div></div>
    ${Object.entries(secs).map(([s,kv])=>`<div class="sect"><div class="st2">${esc(s)}</div>
      <div class="kv">${kv.map(([k,v])=>`<div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>`).join("")}</div></div>`).join("")}</div>`;
  $("#drawer").classList.add("show");$("#scrim").classList.add("show");
  try{
    const d=await (await fetch("/api/device_roms?id="+encodeURIComponent(id),{cache:"no-store"})).json();
    const rc=d.columns, gi=c=>rc.indexOf(c);
    const vers=d.rows.map(r=>r[gi("version")]||""); const p=commonPrefix(vers);
    $("#dr").innerHTML=d.rows.length? d.rows.slice(0,40).map(r=>`<div class="romrow">
        <span class="rgn">${esc(r[gi("region")]||"—")}</span>${vstr(r[gi("version")],p)}
        <span class="meta">${r[gi("android")]?"A"+esc(r[gi("android")]):""}
          ${fdate(r[gi("updated_at")])}
          ${r[gi("download_url")]?`<a class="lnk" href="${esc(r[gi("download_url")])}" target="_blank" rel="noopener">↓</a>`:""}</span></div>`).join("")
      +(d.rows.length>40?`<div style="padding:8px 0;color:var(--ink-faint);font-size:11px">+ ${d.rows.length-40} more</div>`:"")
      : `<div style="color:var(--ink-faint);font-size:12px">No releases linked to this device yet.</div>`;
  }catch(e){$("#dr").textContent="could not load";}
}

/* ── watch / insights ─────────────────────────────────────── */
function svgBars(data,w,h,fmtX){
  if(!data.length)return `<div class="empty"><div class="h">No data</div></div>`;
  const max=Math.max(...data.map(d=>d[1]))||1, pl=38,pr=8,pt=8,pb=22;
  const iw=w-pl-pr, ih=h-pt-pb, bw=Math.max(2,Math.min(24,iw/data.length-2));
  let g="";
  for(let i=0;i<=4;i++){const y=pt+ih*i/4;
    g+=`<line class="gridl" x1="${pl}" y1="${y}" x2="${w-pr}" y2="${y}"/>
        <text class="axis" x="${pl-6}" y="${y+3}" text-anchor="end">${Math.round(max*(4-i)/4).toLocaleString()}</text>`;}
  const bars=data.map((d,i)=>{const bh=d[1]/max*ih, x=pl+i*(iw/data.length)+((iw/data.length)-bw)/2;
    return `<rect x="${x}" y="${pt+ih-bh}" width="${bw}" height="${Math.max(1,bh)}" rx="3" fill="var(--s1)"><title>${esc(d[0])}: ${d[1].toLocaleString()}</title></rect>`;}).join("");
  const step=Math.ceil(data.length/6);
  const lbl=data.map((d,i)=>i%step===0?`<text class="axis" x="${pl+i*(iw/data.length)+(iw/data.length)/2}" y="${h-6}" text-anchor="middle">${esc(fmtX?fmtX(d[0]):d[0])}</text>`:"").join("");
  return `<svg class="mini" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${g}${bars}
    <line class="basel" x1="${pl}" y1="${pt+ih}" x2="${w-pr}" y2="${pt+ih}"/>${lbl}</svg>`;
}
async function renderInsights(){
  $(".wrap table").style.display="none"; const g=$("#grid"); g.style.display="";
  g.innerHTML=`<div class="card"><h3>Loading…</h3></div>`;
  let a={}; try{ a=await (await fetch("/api/analytics",{cache:"no-store"})).json(); }catch(e){}
  const months=(a.by_month||[]).filter(m=>m[0]).slice(-24);
  const vend=(a.by_source||[]).slice(0,10);
  const vtot=vend.reduce((s,x)=>s+x[1],0)||1;
  const lead=(a.by_vendor||[])[0]||null;
  g.innerHTML=`
    <div class="card wide"><h3>Release cadence</h3><p class="sub">Builds per month · last 24 months${(a.undated||0)?` · ${a.undated.toLocaleString()} undated builds excluded`:""}</p>
      ${svgBars(months,1000,150,x=>String(x).replace("-"," "))}</div>
    ${lead?`<div class="card"><h3>Vendor concentration</h3><p class="sub">Share of all releases</p>
      <div class="hero">${((lead[1]/(a.total||1))*100).toFixed(1)}%</div>
      <div class="sub" style="margin:2px 0 0">of all releases — ${esc(lead[0])}</div>
      <div class="meter"><i style="width:${((lead[1]/(a.total||1))*100).toFixed(1)}%"></i></div></div>`:""}
    <div class="card"><h3>Releases by source</h3><p class="sub">Where the corpus comes from</p>
      <div class="bars">${vend.map(v=>`<div class="bar"><span class="t">${esc(v[0])}</span>
        <span class="track"><i style="width:${(v[1]/vend[0][1]*100).toFixed(1)}%"></i></span>
        <span class="v">${v[1].toLocaleString()}</span></div>`).join("")}</div></div>
    <div class="card wide"><h3>Top regions</h3><p class="sub">Builds per market code</p>
      <div class="bars">${(a.by_region||[]).slice(0,12).map(r=>`<div class="bar">
        <span class="t rgn">${esc(r[0])}</span><span class="track"><i style="width:${(r[1]/(a.by_region[0][1])*100).toFixed(1)}%"></i></span>
        <span class="v">${r[1].toLocaleString()}</span></div>`).join("")}</div></div>`;
}
async function renderWatch(){
  $(".wrap table").style.display="none"; const g=$("#grid"); g.style.display="";
  g.innerHTML=`<div class="card"><h3>Loading…</h3></div>`;
  let w={},inb={},sec=[],ws=[];
  try{ [w,inb,sec,ws]=await Promise.all([
    fetch("/api/watch",{cache:"no-store"}).then(r=>r.json()),
    fetch("/api/inbox",{cache:"no-store"}).then(r=>r.json()),
    fetch("/api/security",{cache:"no-store"}).then(r=>r.json()),
    fetch("/api/watches",{cache:"no-store"}).then(r=>r.json())]); }catch(e){}
  const cl=ALL.crawl||[], lr=w.latest||[];
  const f=ALL.facets||{};
  const opts=(arr,ph)=>`<option value="">${ph}</option>`+(arr||[]).map(o=>`<option value="${esc(o.v)}">${esc(o.v)}${o.n?" ("+o.n.toLocaleString()+")":""}</option>`).join("");

  g.innerHTML=`
    <div class="card wide" style="border-left:3px solid var(--accent)">
      <div class="sub" style="letter-spacing:.08em;text-transform:uppercase;margin:0">Newest release in the corpus</div>
      <div style="font:600 22px/28px var(--sans);margin:4px 0 2px">${esc(lr[1]||"—")}
        <span style="color:var(--ink-muted);font-size:13px;font-weight:400">${esc(lr[0]||"")}</span></div>
      <div style="color:var(--ink-secondary)"><span class="mono">${esc(lr[3]||"")}</span> · ${fdate(lr[4])} · ${esc(lr[5]||"")}</div></div>

    <div class="card wide"><h3>Watch something new</h3>
      <p class="sub">Watch a device, a slice, or a rule — e.g. anything on Android 17+, or anything that ships a security patch.</p>
      <div class="wbuild">
        <div><label>Device</label><input id="wDev" placeholder="exact device name…" list="devList">
          <datalist id="devList">${(ALL.devices.rows||[]).map(r=>`<option value="${esc(r[ALL.devices.columns.indexOf("name")])}">`).join("")}</datalist></div>
        <div><label>Vendor</label><select id="wVen">${opts(f.vendor,"any vendor")}</select></div>
        <div><label>Region</label><select id="wRgn">${opts(f.region,"any region")}</select></div>
        <div><label>Chipset contains</label><input id="wChip" placeholder="e.g. Snapdragon 8"></div>
        <div><label>Android ≥</label><input id="wAnd" type="number" min="1" max="30" placeholder="e.g. 17"></div>
        <div><label>Only with security patch</label><select id="wSec"><option value="">no</option><option value="1">yes</option></select></div>
        <div><label>Notify me on</label><select id="wNot">
          <option value="any">any new build</option><option value="security">security patches only</option>
          <option value="os_major">new OS major only</option></select></div>
        <div><label>Label</label><input id="wLbl" placeholder="name this watch"></div>
      </div>
      <div class="prev" id="wPrev">Set a condition to preview how many builds match.</div>
      <button class="btn" id="wAdd">＋ Add watch</button></div>

    <div class="card wide"><h3>Your watches <span class="sub" style="display:inline">${ws.length}</span></h3>
      ${ws.length? ws.map(x=>`<div class="wrow">
          <span class="star on">★</span>
          <div style="min-width:0"><div class="lb">${esc(x.label)}</div>
            <div class="pd">${esc(JSON.stringify(x.predicate))} · notify: ${esc(x.notify)}</div></div>
          <div class="rt"><span>${(inb.watches||[]).find(i=>i.watch.id===x.id)?.total_matching?.toLocaleString()||0} matching</span>
            <button class="btn ghost" data-del="${x.id}">Remove</button></div></div>`).join("")
        : `<div style="color:var(--ink-faint);font-size:12.5px;padding:8px 0">No watches yet — add one above. Try <b>Android ≥ 17</b> or <b>security patch = yes</b>.</div>`}</div>

    <div class="card wide"><h3>Inbox
        ${inb.new_total?`<span class="badge">${inb.new_total} new</span>`:""}
        ${inb.security_total?`<span class="badge sec">⛨ ${inb.security_total} security</span>`:""}</h3>
      <p class="sub">${inb.watermark?`New since you last marked seen · ${esc(inb.watermark)}`
        :`No watermark yet — press “Mark all seen” to start tracking what’s new. (Builds ingested before now have no first-seen timestamp and are never reported as new.)`}</p>
      ${(inb.watches||[]).filter(x=>x.items.length).map(x=>`
        <div style="margin-bottom:12px"><div style="font:500 11px var(--sans);letter-spacing:.04em;
          text-transform:uppercase;color:var(--ink-muted);margin-bottom:4px">${esc(x.watch.label)}</div>
          ${x.items.map(i=>`<div class="inbx">${mg(i.vendor)}
            <div class="bd"><div class="t1">${esc(i.device)} ${i.security_patch?`<span class="badge sec">⛨ security</span>`:""}</div>
              <div class="t2"><span class="mono">${esc(i.version||"")}</span> · ${fdate(i.updated_at)}
                ${i.regions&&i.regions.length?" · "+i.regions.slice(0,6).map(esc).join(" "):""}
                ${i.n>1?` · ${i.n} builds`:""}</div></div>
            ${i.download_url?`<a class="lnk" href="${esc(i.download_url)}" target="_blank" rel="noopener">↓ get</a>`:""}</div>`).join("")}
        </div>`).join("") || `<div style="color:var(--ink-faint);font-size:12.5px">Nothing new for your watches.</div>`}
      <div style="margin-top:10px"><button class="btn ghost" id="wSeen">Mark all seen</button></div></div>

    <div class="card wide"><h3>⛨ Security patches</h3>
      <p class="sub">Recent builds carrying a vendor security advisory — newest first</p>
      <table class="mini-t"><tbody>${sec.length? sec.slice(0,20).map(r=>`<tr>
        <td style="width:130px;white-space:nowrap">${fdate(r.updated_at)}</td>
        <td class="tr-dev"><div class="dev">${mg(r.vendor)}<span>${esc(r.device)}</span></div></td>
        <td style="width:28%"><span class="mono">${esc(r.version||"")}</span></td>
        <td style="width:70px" class="rgn">${esc(r.region||"")}</td>
        <td style="text-align:right;width:120px"><a class="lnk" href="${esc(r.security_patch)}" target="_blank" rel="noopener">⛨ advisory ↗</a></td></tr>`).join("")
        :`<tr><td style="color:var(--ink-faint);font-size:12px">No security advisories in the corpus yet — run <span class="mono">python3 ios_security.py</span>.</td></tr>`}</tbody></table></div>

    <div class="card"><h3>Latest build per vendor</h3><p class="sub">Newest release we hold, by manufacturer</p>
      <table class="mini-t"><tbody>${(w.per_vendor||[]).map(r=>`<tr>
        <td style="width:96px"><div class="dev">${mg(r[0])}<span>${esc(r[0])}</span></div></td>
        <td class="tr-dev">${esc(r[1])}</td>
        <td style="text-align:right;white-space:nowrap">${fdate(r[3])}</td></tr>`).join("")}</tbody></table></div>

    <div class="card"><h3>Data freshness</h3><p class="sub">When each source last ran</p>
      <table class="mini-t"><tbody>${cl.length?cl.map(c=>`<tr>
        <td>${esc(c.source)}</td>
        <td style="text-align:right;white-space:nowrap"><span class="st good"><span class="g">●</span>${esc(c.ran_at)}</span></td>
        <td style="text-align:right;width:64px" class="num">${c.rows==null?"—":c.rows.toLocaleString()}</td></tr>`).join("")
        :`<tr><td style="color:var(--ink-faint);font-size:12px">No pulls recorded yet — run <span class="mono">bash refresh.sh</span>.</td></tr>`}</tbody></table></div>`;

  // --- live preview of the predicate being built ---
  const readPred=()=>{
    const p={};
    const d=$("#wDev").value.trim(); if(d)p.device=d;
    const v=$("#wVen").value; if(v)p.vendor=v;
    const r=$("#wRgn").value; if(r)p.region=r;
    const c=$("#wChip").value.trim(); if(c)p.chip=c;
    const a=$("#wAnd").value; if(a)p.android_min=a;
    if($("#wSec").value==="1")p.security=true;
    return p;
  };
  const preview=debounce(async()=>{
    const p=readPred();
    if(!Object.keys(p).length){$("#wPrev").innerHTML="Set a condition to preview how many builds match.";return;}
    const q=new URLSearchParams(); for(const k in p)q.set(k,p[k]===true?"1":p[k]);
    try{ const d=await (await fetch("/api/watch_preview?"+q)).json();
      $("#wPrev").innerHTML=`<b>${d.count.toLocaleString()}</b> builds match right now`
        +(d.sample&&d.sample[0]?` · newest: <span class="mono">${esc(d.sample[0].version||"")}</span> ${esc(d.sample[0].device)}`:"");
    }catch(e){}
  },250);
  ["wDev","wVen","wRgn","wChip","wAnd","wSec"].forEach(id=>{
    const el=$("#"+id); el.addEventListener("input",preview); el.addEventListener("change",preview);});
  $("#wAdd").onclick=async()=>{
    const p=readPred();
    if(!Object.keys(p).length){$("#wPrev").innerHTML="Add at least one condition first.";return;}
    const lbl=$("#wLbl").value.trim()|| (p.device||[p.vendor,p.region,p.chip,p.android_min?("Android ≥"+p.android_min):"",p.security?"security":""].filter(Boolean).join(" · "));
    const kind=p.device?"device":(p.android_min||p.security||p.chip)?"criterion":"slice";
    await fetch("/api/watch_add",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({label:lbl,kind,predicate:p,notify:$("#wNot").value})});
    renderWatch();
  };
  $$("#grid [data-del]").forEach(b=>b.onclick=async()=>{
    await fetch("/api/watch_del",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:+b.dataset.del})}); renderWatch();});
  const sb=$("#wSeen"); if(sb)sb.onclick=async()=>{await fetch("/api/seen",{method:"POST"});renderWatch();};
}


/* ── Dashboard: only what I watch ─────────────────────────── */
let BF={chip:"",vendor:"",region:""};
async function renderBoard(){
  $(".wrap table").style.display="none"; const g=$("#grid"); g.style.display="";
  g.innerHTML=`<div class="card"><h3>Loading…</h3></div>`;
  const qs=new URLSearchParams(); for(const k in BF) if(BF[k])qs.set(k,BF[k]);
  let b={}; try{ b=await (await fetch("/api/board?"+qs,{cache:"no-store"})).json(); }catch(e){}
  const c=b.counts||{}, f=ALL.facets||{};
  const opt=(arr,cur,ph)=>`<option value="">${ph}</option>`+(arr||[]).map(o=>
    `<option value="${esc(o.v!==undefined?o.v:o)}" ${((o.v!==undefined?o.v:o)===cur)?"selected":""}>${esc(o.v!==undefined?o.v:o)}</option>`).join("");
  g.innerHTML=`
    <div class="card wide"><h3>My dashboard</h3>
      <p class="sub">Scoped to your watches${b.watermark?"":" · no watermark yet — press “Mark all seen” on the Watch tab to start tracking what’s new"}</p>
      <div class="wbuild" style="grid-template-columns:repeat(auto-fit,minmax(190px,1fr))">
        <div><label>Chipset</label><select id="bChip">${opt(b.chips,BF.chip,"any chipset")}</select></div>
        <div><label>Vendor</label><select id="bVen">${opt(f.vendor,BF.vendor,"any vendor")}</select></div>
        <div><label>Region</label><select id="bRgn">${opt(f.region,BF.region,"any region")}</select></div>
      </div>
      <div class="kpis" style="display:flex;gap:12px;margin-top:4px">
        <div class="tile" style="flex:1"><div class="v">${(c.devices||0).toLocaleString()}</div><div class="l">watched devices</div></div>
        <div class="tile" style="flex:1"><div class="v" style="color:var(--st-critical)">${(c.security||0).toLocaleString()}</div><div class="l">⛨ security items</div></div>
        <div class="tile" style="flex:1"><div class="v" style="color:var(--accent)">${(c.fresh||0).toLocaleString()}</div><div class="l">new since last seen</div></div>
      </div></div>

    <div class="card wide"><h3>New firmware on my devices</h3>
      <p class="sub">Appeared since your watermark</p>
      ${(b.fresh||[]).length? `<table class="mini-t"><tbody>${b.fresh.map(r=>`<tr>
          <td style="width:120px;white-space:nowrap">${fdate(r.updated_at)}</td>
          <td class="tr-dev"><div class="dev">${mg(r.vendor)}<span>${esc(r.device)}</span></div></td>
          <td style="width:30%"><span class="mono">${esc(r.version||"")}</span></td>
          <td style="width:64px" class="rgn">${esc(r.region||"")}</td></tr>`).join("")}</tbody></table>`
        : `<div style="color:var(--ink-faint);font-size:12.5px">Nothing new since your watermark${b.watermark?` (${esc(b.watermark)})`:""}.</div>`}</div>

    <div class="card wide"><h3>⛨ Security patches — my devices${BF.chip?` · ${esc(BF.chip)}`:""}</h3>
      <p class="sub">Only devices matching your watches${BF.chip||BF.vendor||BF.region?", narrowed by your filter":""}</p>
      ${(b.security||[]).length? `<table class="mini-t"><tbody>${b.security.map(r=>`<tr>
          <td style="width:118px;white-space:nowrap">${fdate(r.updated_at)}</td>
          <td class="tr-dev"><div class="dev">${mg(r.vendor)}<span>${esc(r.device)}</span></div></td>
          <td style="width:24%"><span class="mono">${esc(r.version||"")}</span></td>
          <td style="width:64px" class="rgn">${esc(r.region||"")}</td>
          <td style="width:120px">${r.security_level?`<span class="badge sec">⛨ ${esc(r.security_level)}</span>`:""}</td>
          <td style="text-align:right;width:110px">${r.security_url?`<a class="lnk" href="${esc(r.security_url)}" target="_blank" rel="noopener">advisory ↗</a>`:""}</td></tr>`).join("")}</tbody></table>`
        : `<div style="color:var(--ink-faint);font-size:12.5px">No security patches for the current selection.</div>`}</div>

    <div class="card wide"><h3>My devices — latest ROM each</h3>
      <p class="sub">${(c.devices||0).toLocaleString()} devices from your watches, newest release first</p>
      <table class="mini-t"><tbody>${(b.devices||[]).map(r=>`<tr>
        <td class="tr-dev" style="width:26%"><div class="dev">${mg(r.vendor)}<span>${esc(r.device)}</span></div></td>
        <td style="width:96px"><span class="mono">${esc(r.model||"")}</span></td>
        <td style="width:26%"><span class="mono">${esc(r.version||"")}</span></td>
        <td style="width:56px" class="rgn">${esc(r.region||"")}</td>
        <td style="width:48px" class="num">${esc(r.android||"")}</td>
        <td style="width:118px;white-space:nowrap">${fdate(r.updated_at)}</td>
        <td style="text-align:right;color:var(--ink-faint);font-size:11px">${esc(r.chipset||"")}</td></tr>`).join("")}</tbody></table></div>`;
  $("#bChip").onchange=e=>{BF.chip=e.target.value;renderBoard();};
  $("#bVen").onchange=e=>{BF.vendor=e.target.value;renderBoard();};
  $("#bRgn").onchange=e=>{BF.region=e.target.value;renderBoard();};
}

/* ── Updates: schedule + run, from the web ────────────────── */
let _upTimer=null;
async function renderUpdates(){
  $(".wrap table").style.display="none"; const g=$("#grid"); g.style.display="";
  let st={}; try{ st=await (await fetch("/api/refresh_status",{cache:"no-store"})).json(); }catch(e){}
  const cfg=st.config||{}, steps=(cfg.sources||"").split(",").filter(Boolean);
  const ALLSTEPS=[["ios","Apple firmware"],["ios_security","Apple security advisories"],
    ["iphone_specs","iPhone specs"],["samsung","Samsung A/S manifest"],
    ["chipsets","Link chipsets"],["fix","Repair data defects"],["audit","Audit"]];
  g.innerHTML=`
    <div class="card wide"><h3>Update schedule</h3>
      <p class="sub">Configured here — no cron editing needed. The app runs it in the background.</p>
      <div class="wbuild" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr))">
        <div><label>Run automatically</label><select id="uAuto">
          <option value="0" ${cfg.auto!=="1"?"selected":""}>off (manual only)</option>
          <option value="1" ${cfg.auto==="1"?"selected":""}>on</option></select></div>
        <div><label>Every</label><select id="uInt">
          ${[1,2,3,4,6,8,12,24].map(h=>`<option value="${h}" ${String(cfg.interval_hours)===String(h)?"selected":""}>${h} hour${h>1?"s":""}</option>`).join("")}
        </select></div>
      </div>
      <div style="margin:10px 0 6px"><label style="font:500 10.5px var(--sans);letter-spacing:.04em;
        text-transform:uppercase;color:var(--ink-muted)">Which steps to run</label></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
        ${ALLSTEPS.map(([k,l])=>`<label class="tgl" style="display:inline-flex;gap:6px;align-items:center;
          background:var(--bg-sunken);border:1px solid ${steps.includes(k)?"var(--accent)":"var(--line-subtle)"};
          border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer">
          <input type="checkbox" class="ustep" value="${k}" ${steps.includes(k)?"checked":""}
            style="accent-color:var(--accent)"> ${esc(l)}</label>`).join("")}</div>
      <button class="btn" id="uSave">Save schedule</button>
      <button class="btn ghost" id="uRun" ${st.running?"disabled":""} style="margin-left:8px">
        ${st.running?"● Running…":"▶ Run update now"}</button>
      <span id="uMsg" style="margin-left:10px;font-size:12px;color:var(--ink-muted)"></span></div>

    <div class="card wide"><h3>Run status</h3>
      <p class="sub">${st.running?`<span class="st good"><span class="g">●</span>running</span> since ${esc(st.started||"")}`
        : st.finished?`last run finished ${esc(st.finished)} · exit ${st.rc}`:"never run from the web yet"}</p>
      <pre style="background:var(--bg-canvas);border:1px solid var(--line-subtle);border-radius:6px;
        padding:10px 12px;max-height:280px;overflow:auto;font:400 11.5px/17px var(--mono);
        color:var(--ink-secondary);white-space:pre-wrap;margin:0">${esc(st.log||"(no log yet — press Run update now)")}</pre></div>

    <div class="card wide"><h3>Source freshness</h3>
      <p class="sub">When each ingester last completed</p>
      <table class="mini-t"><tbody>${(st.crawl||[]).length?(st.crawl||[]).map(c=>`<tr>
        <td>${esc(c.source)}</td>
        <td style="text-align:right;white-space:nowrap"><span class="st good"><span class="g">●</span>${esc(c.ran_at)}</span></td>
        <td style="text-align:right;width:70px" class="num">${c.rows==null?"—":Number(c.rows).toLocaleString()}</td></tr>`).join("")
        :`<tr><td style="color:var(--ink-faint);font-size:12px">No runs recorded yet.</td></tr>`}</tbody></table></div>`;

  $("#uSave").onclick=async()=>{
    const chosen=[...document.querySelectorAll(".ustep:checked")].map(x=>x.value).join(",");
    await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({auto:$("#uAuto").value,interval_hours:$("#uInt").value,sources:chosen})});
    $("#uMsg").textContent="saved"; setTimeout(()=>renderUpdates(),700);
  };
  $("#uRun").onclick=async()=>{
    const chosen=[...document.querySelectorAll(".ustep:checked")].map(x=>x.value);
    const r=await (await fetch("/api/refresh_now",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({steps:chosen})})).json();
    $("#uMsg").textContent=r.ok?"started":(r.error||"could not start");
    renderUpdates();
  };
  clearTimeout(_upTimer);
  if(st.running) _upTimer=setTimeout(()=>{ if(VIEW==="updates") renderUpdates(); },3000);
}

/* ── views ────────────────────────────────────────────────── */
function setView(v){
  VIEW=v; SEL=-1; closeDrawer();
  $("#rail").classList.toggle("hide",["watch","insights","board","updates"].includes(v));
  SORT=v==="roms"?{col:"updated_at",d:-1}:SORT;
  renderNav(); renderTools();
  if(v==="roms")fetchRows(true); else render();
}

/* ── command palette ──────────────────────────────────────── */
let palIx=0, palItems=[];
function openPal(){$("#pal").classList.add("show");$("#palq").value="";$("#palq").focus();palRender("");}
function closePal(){$("#pal").classList.remove("show");}
function palRender(q){
  q=q.toLowerCase().trim();
  const devs=(ALL.devices.rows||[]).map(r=>({n:r[ALL.devices.columns.indexOf("name")],
    c:r[ALL.devices.columns.indexOf("Platform — Chipset")],k:r[ALL.devices.columns.indexOf("rom_count")]}))
    .filter(d=>!q||String(d.n).toLowerCase().includes(q)).slice(0,7);
  const chips=((ALL.facets||{}).chip||[]).filter(c=>!q||c.v.toLowerCase().includes(q)).slice(0,5);
  palItems=[...devs.map(d=>({t:"dev",label:d.n,meta:(d.k||0)+" releases",sub:d.c})),
            ...chips.map(c=>({t:"chip",label:c.v,meta:c.n+" builds"})),
            {t:"view",label:"Go to Releases",meta:"⌘2"},{t:"view2",label:"Go to Watch",meta:"⌘3"}];
  palIx=0;
  $("#palres").innerHTML=
    (devs.length?`<div class="sec">Devices</div>`+devs.map((d,i)=>
      `<div class="it ${i===0?"on":""}" data-i="${i}">${mg(vendorOf(d.n))}<span>${esc(d.n)}</span><span class="m">${(d.k||0)} releases</span></div>`).join(""):"")
    +(chips.length?`<div class="sec">Chipset filter</div>`+chips.map((c,i)=>
      `<div class="it" data-i="${devs.length+i}"><span class="mono" style="font-size:11.5px">${esc(c.v)}</span><span class="m">${c.n.toLocaleString()}</span></div>`).join(""):"")
    +`<div class="sec">Views</div>
      <div class="it" data-i="${palItems.length-2}">Releases</div>
      <div class="it" data-i="${palItems.length-1}">Watch</div>`;
  $$("#palres .it").forEach(el=>el.onclick=()=>palGo(+el.dataset.i));
}
function palGo(i){
  const it=palItems[i]; if(!it)return; closePal();
  if(it.t==="dev"){F.name=it.label;setView("roms");renderRail();renderTools();fetchRows(true);}
  else if(it.t==="chip"){F.chip.add(it.label);setView("roms");renderRail();renderTools();fetchRows(true);}
  else if(it.t==="view")setView("roms"); else setView("watch");
}
$("#kbtn").onclick=openPal;
$("#palq").addEventListener("input",e=>palRender(e.target.value));
$("#palq").addEventListener("keydown",e=>{
  const its=$$("#palres .it");
  if(e.key==="ArrowDown"){e.preventDefault();palIx=Math.min(palIx+1,its.length-1);}
  else if(e.key==="ArrowUp"){e.preventDefault();palIx=Math.max(0,palIx-1);}
  else if(e.key==="Enter"){e.preventDefault();const el=its[palIx];if(el)palGo(+el.dataset.i);return;}
  else return;
  its.forEach((el,i)=>el.classList.toggle("on",i===palIx));
  if(its[palIx])its[palIx].scrollIntoView({block:"nearest"});
});

/* ── keyboard ─────────────────────────────────────────────── */
document.addEventListener("keydown",e=>{
  const typing=/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="k"){e.preventDefault();openPal();return;}
  if(e.key==="Escape"){ if($("#pal").classList.contains("show"))closePal(); else closeDrawer(); return; }
  if(typing)return;
  if(e.key==="/"){e.preventDefault();openPal();}
  if(VIEW==="roms"&&(e.key==="j"||e.key==="k"||e.key==="ArrowDown"||e.key==="ArrowUp")){
    e.preventDefault();
    const d=(e.key==="j"||e.key==="ArrowDown")?1:-1;
    const n=Math.max(0,Math.min(ROWS.rows.length-1,(SEL<0?-1:SEL)+d));
    selectRow(n);
    const tr=$(`#body tr[data-i="${n}"]`); if(tr)tr.scrollIntoView({block:"nearest"});
  }
});
$("#theme").onclick=()=>{
  const cur=document.documentElement.getAttribute("data-theme");
  const next=cur==="light"?"dark":"light";
  document.documentElement.setAttribute("data-theme",next);
  localStorage.setItem("fa_theme",next);
};
if(localStorage.getItem("fa_theme"))document.documentElement.setAttribute("data-theme",localStorage.getItem("fa_theme"));

/* ── boot ─────────────────────────────────────────────────── */
fetch("/api/all").then(r=>r.json()).then(d=>{
  ALL=d; renderNav(); renderScale(); renderRail(); renderTools(); render();
});
</script></body></html>"""
