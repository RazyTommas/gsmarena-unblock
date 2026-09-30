"""DEV TOOL (not shipped, not imported by the app): measure the rendered layout.

Why this exists beside tools/ui_smoke.py rather than replacing it
----------------------------------------------------------------
ui_smoke.py answers "did the feature render at all" — it shoots 5 frames at one
viewport (1500x1000), one theme, and asserts controls exist. It measures nothing
about geometry or colour, so every defect below was invisible to it and it
stayed green through all of them. This tool answers the other question: "given
that it rendered, is any of it unreachable or unreadable". The two are
complementary; keep both. ui_smoke is the feature guard, this is the layout
guard.

Five assertions, per view x per viewport x per theme:

  1. no body horizontal scroll        document.scrollWidth <= clientWidth
  2. nothing unreachable              no element with computed overflow-x:hidden
                                      may have scrollWidth > clientWidth
  3. contrast                         every visible text node >= 4.5:1 against
                                      its actually-painted (composited)
                                      background
  4. no placeholder garbage           no rendered text contains "undefined",
                                      "NaN", "[object Object]" or a bare "null"
  5. the system theme follows the OS   with OS dark and NO data-theme, body
                                      background must be the dark token

A note on the instrument, because this project has been burned by instruments
before: assertion 3 composites every background layer it walks past, multiplies
cumulative `opacity` into the foreground alpha, and refuses to guess when it
meets a gradient or an image — those are reported as SKIPPED-UNKNOWN with a
count, never silently counted as passes. An element it cannot measure is not an
element that passed.

Usage:
    python3 tools/ui_layout_audit.py [BASE_URL] [SHOT_DIR]

Exit 0 = every assertion held everywhere. Exit 1 = at least one measured
failure. Exit 2 = the harness could not reach a view it was asked to measure
(an inconclusive run, which is NOT a pass).
"""

import json
import sys
from collections import Counter

from playwright.sync_api import sync_playwright

_args = [a for a in sys.argv[1:] if not a.startswith("--")]
_flags = [a for a in sys.argv[1:] if a.startswith("--")]
BASE = _args[0] if _args else "http://127.0.0.1:8899"
SHOTS = _args[1] if len(_args) > 1 else None
ONLY = [v for a in _flags if a.startswith("--only=") for v in a.split("=", 1)[1].split(",")]

VIEWPORTS = [(1920, 1080), (1366, 768), (800, 600), (690, 900)]
for a in _flags:
    if a.startswith("--viewports="):
        VIEWPORTS = [tuple(int(n) for n in v.split("x")) for v in a.split("=", 1)[1].split(",")]
THEMES = ["light", "dark", "contrast"]
for a in _flags:
    if a.startswith("--themes="):
        THEMES = a.split("=", 1)[1].split(",")
SKIP_PANELS = "--no-panels" in _flags
SKIP_SEARCH = "--no-search" in _flags

# --css=PATH serves PATH in place of apps/web/styles.css, by intercepting the
# request in the browser. That is how a defect gets planted back to prove this
# harness can fail: the repo is never mutated, so a planted defect cannot be
# committed by accident and the "before" run is reproducible from git alone.
CSS_OVERRIDE = next((a.split("=", 1)[1] for a in _flags if a.startswith("--css=")), None)

# --plant=NAME re-introduces one defect at runtime so that this harness's
# ability to FAIL is reproducible rather than asserted. A check that has never
# been seen to fail proves nothing about the thing it checks.
#
#   data-card-hidden  half of the original assertion-2 defect, on its own
#   shell-bare-fr     half of the original assertion-1 defect, on its own
#   original-clip     assertion 2 — the original clipping pair, restored
#   original-shell    assertion 1 — the original body-scroll trio, restored
#   low-contrast      assertion 3 — greys .subtle out below AA
#   garbage           assertion 4 — renders the word "undefined" on the page
#   (assertion 5 is planted with --css=<a stylesheet with no prefers block>)
#
# MEASURED, and worth keeping in front of the next reader: neither
# `data-card-hidden` nor `shell-bare-fr` fails on its own any more. Each of the
# original defects needed two things to be true at once — a container that
# clipped or a track that inflated, AND cells that refused to wrap. Reverting
# one leaves the other holding. That is why `overflow:auto` below 700px and
# `minmax(0,...)` on `.shell` are both still here even though the wrapping
# cells make each of them individually redundant today: a defect that needs two
# regressions to reappear is the point, not an accident.
_ORIGINAL_TD_CLIP = (".data-table td{white-space:nowrap!important;max-width:28ch!important;"
                     "overflow:hidden!important;text-overflow:ellipsis!important}"
                     ".data-table td:first-child{max-width:34ch!important}")
PLANTS = {
    "data-card-hidden": ("style", ".data-card{overflow:hidden!important}"),
    "shell-bare-fr": ("style", ".shell{grid-template-columns:224px 1fr!important}"
                               "@media(max-width:1000px){.shell{grid-template-columns:76px 1fr!important}}"),
    "original-clip": ("style", ".data-card{overflow:hidden!important}" + _ORIGINAL_TD_CLIP),
    "original-shell": ("style", ".data-card{overflow-x:visible!important}"
                                ".data-table td{white-space:nowrap!important}"
                                ".admin-grid{grid-template-columns:1.4fr .8fr!important}"
                                ".shell{grid-template-columns:224px 1fr!important}"
                                "@media(max-width:1000px){.shell{grid-template-columns:76px 1fr!important}}"),
    "low-contrast": ("style", ".subtle,.data-table td .subtle{color:#c9cdc9!important}"),
    "garbage": ("dom", "undefined"),
}
PLANT = next((a.split("=", 1)[1] for a in _flags if a.startswith("--plant=")), None)
if PLANT and PLANT not in PLANTS:
    sys.exit(f"unknown --plant={PLANT}; choose from {sorted(PLANTS)}")


def _install_css_override(target):
    if not CSS_OVERRIDE:
        return
    with open(CSS_OVERRIDE, "rb") as fh:
        body = fh.read()
    target.route("**/styles.css*", lambda route: route.fulfill(
        status=200, body=body, headers={"content-type": "text/css; charset=utf-8",
                                        "cache-control": "no-store"}))

# route, optional tab container + tab value, label
VIEWS = [
    ("radar", None, None, "radar"),
    ("watchlist", None, None, "watchlist"),
    ("explore", "#exploreTabs", "devices", "explore-devices"),
    ("explore", "#exploreTabs", "silicon", "explore-silicon"),
    ("explore", "#exploreTabs", "releases", "explore-releases"),
    ("explore", "#exploreTabs", "sources", "explore-sources"),
    ("products", "#productEvidenceTabs", "firmware", "products-firmware"),
    ("products", "#productEvidenceTabs", "patches", "products-patches"),
    ("security", None, None, "security"),
    ("admin", None, None, "admin"),
]

# The dark palette token, as declared in styles.css. Assertion 5 compares
# against this literal on purpose: reading the token back out of the stylesheet
# would make the test agree with whatever the stylesheet says.
DARK_PAPER = "rgb(19, 32, 27)"
LIGHT_PAPER = "rgb(245, 246, 242)"

# ---------------------------------------------------------------------------
# The in-page probe. Everything geometric or chromatic is measured by the
# browser itself; Python only decides pass/fail.
# ---------------------------------------------------------------------------
PROBE = r"""
() => {
  const parse = s => {
    if (!s) return null;
    const m = s.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
    if (p.length < 3 || p.some(Number.isNaN)) return null;
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const lum = c => {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  };
  const over = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a),
    a: 1,
  });
  const desc = el => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    if (el.classList.length) s += '.' + [...el.classList].join('.');
    return s;
  };

  // --- painted background of an element -----------------------------------
  // Walks outward collecting layers, then composites them front-to-back over
  // the first opaque one. Returns {unknown:true} rather than a guess when it
  // meets a gradient/image, so an unmeasurable element can never read as a pass.
  const painted = el => {
    const layers = [];
    for (let n = el; n; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return { unknown: desc(n) };
      const c = parse(cs.backgroundColor);
      if (c && c.a > 0) {
        layers.push(c);
        if (c.a >= 1) break;
      }
    }
    let bg = { r: 255, g: 255, b: 255, a: 1 };          // the UA canvas
    for (let i = layers.length - 1; i >= 0; i--) bg = over(layers[i], bg);
    return { color: bg };
  };

  const out = {
    doc: {
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      bodyBg: getComputedStyle(document.body).backgroundColor,
      dataTheme: document.documentElement.getAttribute('data-theme'),
    },
    clipped: [],
    contrast: [],
    unknownBg: [],
    measured: 0,
  };

  const all = document.querySelectorAll('*');
  for (const el of all) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const rect = el.getBoundingClientRect();

    // --- assertion 2: overflow-x:hidden with content past the edge ---------
    if (cs.overflowX === 'hidden' && el.scrollWidth - el.clientWidth > 1
        && rect.width > 0 && rect.height > 0) {
      out.clipped.push({
        sel: desc(el),
        hidden: el.scrollWidth - el.clientWidth,
        text: (el.textContent || '').trim().slice(0, 60),
      });
    }

    // --- assertion 3: contrast of this element's OWN text ------------------
    let own = '';
    for (const n of el.childNodes) if (n.nodeType === 3) own += n.nodeValue;
    own = own.replace(/\s+/g, ' ').trim();
    if (!own) continue;
    if (rect.width <= 0 || rect.height <= 0) continue;
    const size = parseFloat(cs.fontSize) || 0;
    if (size < 1) continue;                       // collapsed-to-nothing text
    // cumulative opacity, and the clip/scale an ancestor may apply
    let op = 1;
    for (let n = el; n; n = n.parentElement) op *= parseFloat(getComputedStyle(n).opacity);
    if (op <= 0.05) continue;

    const fg = parse(cs.color);
    if (!fg || fg.a * op <= 0.05) continue;
    const bg = painted(el);
    if (bg.unknown) { out.unknownBg.push({ sel: desc(el), because: bg.unknown, text: own.slice(0, 40) }); continue; }
    const eff = over({ ...fg, a: fg.a * op }, bg.color);
    const r = ratio(eff, bg.color);
    out.measured++;
    if (r < 4.5) {
      const weight = parseInt(cs.fontWeight, 10) || 400;
      out.contrast.push({
        sel: desc(el),
        ratio: Math.round(r * 100) / 100,
        fg: cs.color, bg: `rgb(${Math.round(bg.color.r)}, ${Math.round(bg.color.g)}, ${Math.round(bg.color.b)})`,
        size, weight,
        aaLarge: (size >= 24 || (size >= 18.66 && weight >= 700)) && r >= 3.0,
        text: own.slice(0, 50),
      });
    }
  }

  // --- assertion 4: placeholder garbage in rendered text -------------------
  const txt = document.body.innerText || '';
  const bad = [];
  for (const [label, re] of [['undefined', /\bundefined\b/], ['NaN', /\bNaN\b/],
                             ['[object Object]', /\[object Object\]/], ['null', /\bnull\b/]]) {
    const m = txt.match(re);
    if (m) {
      const i = txt.indexOf(m[0]);
      bad.push({ token: label, context: txt.slice(Math.max(0, i - 40), i + 40).replace(/\s+/g, ' ') });
    }
  }
  out.garbage = bad;
  return out;
}
"""

# A dedicated probe for finding 7: is the dropdown's own truncation footer
# actually reachable? Not "is it in the DOM" — scroll the dropdown to its end
# and check the footer lands inside both the dropdown's client box and the
# viewport.
SEARCH_PROBE = r"""
() => {
  const box = document.getElementById('searchResults');
  if (!box || !box.classList.contains('open')) return { skipped: 'dropdown not open' };
  const cs = getComputedStyle(box);
  box.scrollTop = box.scrollHeight;
  const br = box.getBoundingClientRect();
  const more = box.querySelector('.search-more') || box.lastElementChild;
  const mr = more ? more.getBoundingClientRect() : null;
  return {
    maxHeight: cs.maxHeight,
    overflowY: cs.overflowY,
    boxBottom: Math.round(br.bottom),
    viewportH: window.innerHeight,
    overflowsViewport: Math.round(br.bottom - window.innerHeight),
    footerText: more ? (more.textContent || '').trim().slice(0, 90) : null,
    footerInsideBox: mr ? (mr.bottom <= br.bottom + 1 && mr.top >= br.top - 1) : null,
    footerInViewport: mr ? (mr.bottom <= window.innerHeight + 1 && mr.top >= 0) : null,
    hits: box.querySelectorAll('.search-hit').length,
  };
}
"""

failures = []
skipped_views = []
unknown_bg_total = Counter()
contrast_seen = 0


def fail(where, msg):
    failures.append(f"{where}: {msg}")
    print(f"    FAIL  {where}: {msg}")


def settle(page):
    try:
        page.wait_for_selector(".data-table, .feed, .empty, .metric, .admin-grid", timeout=20000)
    except Exception:
        return False
    page.wait_for_timeout(450)
    return True


def goto_view(page, route, tabs, tab):
    page.evaluate("r => { if (location.hash !== '#'+r) location.hash = '#'+r; }", route)
    page.wait_for_timeout(350)
    if not settle(page):
        return False
    if tabs:
        ok = page.evaluate(
            """([sel, val]) => { const b = document.querySelector(sel + " button[data-value='" + val + "']");
                                 if (!b) return false; b.click(); return true; }""",
            [tabs, tab],
        )
        if not ok:
            return False
        page.wait_for_timeout(1200)
        if not settle(page):
            return False
    return True


def apply_plant(page):
    """Re-insert one defect just before measuring, so the harness's own failure
    path is exercised. Injected per-measurement because a render replaces #app."""
    if not PLANT:
        return
    kind, payload = PLANTS[PLANT]
    if kind == "style":
        page.evaluate("""css => { let s = document.getElementById('__plant');
                                  if (!s) { s = document.createElement('style'); s.id='__plant';
                                            document.head.appendChild(s); }
                                  s.textContent = css; }""", payload)
    else:
        page.evaluate("""word => { let p = document.getElementById('__plantdom');
                                   if (!p) { p = document.createElement('p'); p.id='__plantdom';
                                             document.body.appendChild(p); }
                                   p.textContent = word; }""", payload)


def audit(page, where, shot=None):
    global contrast_seen
    apply_plant(page)
    res = page.evaluate(PROBE)
    d = res["doc"]
    if d["scrollWidth"] - d["clientWidth"] > 1:
        fail(where, f"body scrolls horizontally: scrollWidth {d['scrollWidth']} > clientWidth {d['clientWidth']} "
                    f"({d['scrollWidth'] - d['clientWidth']}px)")
    if res["clipped"]:
        worst = sorted(res["clipped"], key=lambda c: -c["hidden"])[:4]
        fail(where, f"{len(res['clipped'])} element(s) clip content with overflow-x:hidden and no scroll route; "
                    + "; ".join(f"{c['sel']} hides {c['hidden']}px ({c['text'][:34]!r})" for c in worst))
    if res["contrast"]:
        worst = sorted(res["contrast"], key=lambda c: c["ratio"])[:5]
        fail(where, f"{len(res['contrast'])} text node(s) below 4.5:1; "
                    + "; ".join(f"{c['sel']} {c['ratio']} ({c['fg']} on {c['bg']}, {c['text'][:28]!r})" for c in worst))
    if res["garbage"]:
        fail(where, "placeholder text rendered: "
                    + "; ".join(f"{g['token']} in ...{g['context']}..." for g in res["garbage"]))
    contrast_seen += res["measured"]
    for u in res["unknownBg"]:
        unknown_bg_total[u["because"]] += 1
    if shot and SHOTS:
        page.screenshot(path=f"{SHOTS}/{shot}.png", full_page=False)
    return res


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()

        # ------------------------------------------------------------------
        # Assertion 5, first, in its own contexts: the OS preference.
        # ------------------------------------------------------------------
        print("\n=== assertion 5: the theme follows the OS when nothing is chosen ===")
        for os_scheme, want, label in [("dark", DARK_PAPER, "OS dark, no data-theme -> dark paper"),
                                       ("light", LIGHT_PAPER, "OS light, no data-theme -> light paper")]:
            ctx = browser.new_context(viewport={"width": 1366, "height": 768}, color_scheme=os_scheme)
            _install_css_override(ctx)
            pg = ctx.new_page()
            pg.add_init_script("try{localStorage.clear()}catch(e){}")
            pg.goto(BASE, wait_until="domcontentloaded")
            settle(pg)
            got = pg.evaluate("() => [getComputedStyle(document.body).backgroundColor, "
                              "document.documentElement.getAttribute('data-theme'), "
                              "document.getElementById('themeSelect').value]")
            bg, attr, sel = got
            print(f"  OS={os_scheme}: data-theme={attr!r} select={sel!r} body bg={bg}")
            if sel != "system":
                fail(f"theme/{os_scheme}", f"expected the select to sit on 'system', got {sel!r}")
            elif bg != want:
                fail(f"theme/{os_scheme}", f"{label} — got {bg}, expected {want}")
            else:
                print(f"    PASS  {label}")
            # and an explicit choice must win over the OS in BOTH directions
            for choice, expect in [("light", LIGHT_PAPER), ("dark", DARK_PAPER)]:
                pg.select_option("#themeSelect", choice)
                pg.wait_for_timeout(120)
                bg2 = pg.evaluate("() => getComputedStyle(document.body).backgroundColor")
                if bg2 != expect:
                    fail(f"theme/{os_scheme}/explicit-{choice}",
                         f"explicit data-theme={choice} under OS {os_scheme} gave {bg2}, expected {expect}")
                else:
                    print(f"    PASS  explicit {choice} wins under OS {os_scheme} ({bg2})")
            ctx.close()

        # ------------------------------------------------------------------
        # Assertions 1-4 across the matrix.
        # ------------------------------------------------------------------
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
        _install_css_override(ctx)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE, wait_until="domcontentloaded")
        if not settle(page):
            print("  the app never painted; cannot measure")
            sys.exit(2)

        for (w, h) in VIEWPORTS:
            page.set_viewport_size({"width": w, "height": h})
            for theme in THEMES:
                page.select_option("#themeSelect", theme)
                page.wait_for_timeout(150)
                print(f"\n=== {w}x{h} {theme} ===")
                for route, tabs, tab, label in VIEWS:
                    if ONLY and label not in ONLY:
                        continue
                    where = f"{label}@{w}x{h}/{theme}"
                    if not goto_view(page, route, tabs, tab):
                        skipped_views.append(where)
                        print(f"    SKIP  {where}: view never reached (inconclusive, not a pass)")
                        continue
                    before = len(failures)
                    res = audit(page, where, shot=f"{label}-{w}x{h}-{theme}")
                    d = res["doc"]
                    # the prefix reads off the failure LIST, not off two of the
                    # four things that can fail: a body-scroll failure used to
                    # print "ok" on the very next line because clipped and
                    # contrast happened to be zero
                    print(f"    {'ok  ' if len(failures) == before else '--  '}"
                          f"{where}  doc {d['scrollWidth']}/{d['clientWidth']}  "
                          f"clipped={len(res['clipped'])}  low-contrast={len(res['contrast'])}")

                # --- finding 7: the search dropdown, measured for reachability
                if SKIP_SEARCH:
                    continue
                where = f"search-dropdown@{w}x{h}/{theme}"
                page.evaluate("location.hash = '#radar'")
                page.wait_for_timeout(300)
                settle(page)
                # the handler needs >= 2 characters before it will open the list
                page.fill("#globalSearch", "gal")
                page.wait_for_timeout(1800)
                s = page.evaluate(SEARCH_PROBE)
                if s.get("skipped"):
                    skipped_views.append(where)
                    print(f"    SKIP  {where}: {s['skipped']}")
                else:
                    print(f"    {where}: hits={s['hits']} max-height={s['maxHeight']} overflow-y={s['overflowY']} "
                          f"bottom={s['boxBottom']}/{s['viewportH']} footer={s['footerText']!r}")
                    if s["overflowsViewport"] > 1:
                        fail(where, f"dropdown runs {s['overflowsViewport']}px past the bottom of the viewport")
                    if s["footerText"] and not s["footerInsideBox"]:
                        fail(where, "the truncation footer is outside the dropdown's own scroll box")
                    if s["footerText"] and not s["footerInViewport"]:
                        fail(where, f"the truncation footer is unreachable: {s['footerText']!r}")
                    if s["hits"] and "Showing" in (s["footerText"] or "") is None:
                        pass
                page.fill("#globalSearch", "")
                page.wait_for_timeout(300)

        # --- the two panel views the findings name explicitly ---------------
        print("\n=== detail panels (device panel, review profile) ===")
        for (w, h) in ([] if SKIP_PANELS else VIEWPORTS):
            page.set_viewport_size({"width": w, "height": h})
            for theme in THEMES:
                page.select_option("#themeSelect", theme)
                goto_view(page, "explore", "#exploreTabs", "devices")
                opened = page.evaluate(
                    """() => { const b = document.querySelector('#app .data-table [data-device]');
                               if (!b) return false; b.click(); return true; }""")
                where = f"device-panel@{w}x{h}/{theme}"
                if not opened:
                    skipped_views.append(where)
                    print(f"    SKIP  {where}: no device button to open")
                else:
                    page.wait_for_timeout(1500)
                    r = audit(page, where, shot=f"device-panel-{w}x{h}-{theme}")
                    print(f"    {where}  doc {r['doc']['scrollWidth']}/{r['doc']['clientWidth']}  "
                          f"clipped={len(r['clipped'])}  low-contrast={len(r['contrast'])}")
                    page.evaluate("() => document.querySelector('.detail-close')?.click()")
                    page.wait_for_timeout(300)
                # the review-profile table does not live on a fixed route; look
                # for it rather than assuming, and record a SKIP if no route has
                # one, so an unmeasured panel never reads as a measured pass.
                rp = False
                for route in ("products", "explore", "security", "admin", "watchlist", "radar"):
                    page.evaluate("r => { location.hash = '#'+r; }", route)
                    page.wait_for_timeout(400)
                    settle(page)
                    rp = page.evaluate(
                        """() => { const b = document.querySelector('[data-review-profile]');
                                   if (!b) return false; b.click(); return true; }""")
                    if rp:
                        break
                where = f"review-profile@{w}x{h}/{theme}"
                if not rp:
                    skipped_views.append(where)
                    print(f"    SKIP  {where}: no review-profile button on this view")
                else:
                    page.wait_for_timeout(1500)
                    r = audit(page, where, shot=f"review-profile-{w}x{h}-{theme}")
                    print(f"    {where}  doc {r['doc']['scrollWidth']}/{r['doc']['clientWidth']}  "
                          f"clipped={len(r['clipped'])}  low-contrast={len(r['contrast'])}")
                    page.evaluate("() => document.querySelector('.detail-close')?.click()")
                    page.wait_for_timeout(300)

        real_errors = [e for e in errors if "favicon" not in e.lower()]
        if real_errors:
            fail("js", "; ".join(real_errors[:3]))
        browser.close()

    print("\n" + "=" * 72)
    print(f"text nodes measured for contrast: {contrast_seen}")
    if unknown_bg_total:
        print("backgrounds the harness refused to guess at (reported, not passed):")
        for k, v in unknown_bg_total.most_common():
            print(f"  {v:5d}  behind {k}")
    if skipped_views:
        print(f"\nINCONCLUSIVE — {len(skipped_views)} view(s) never reached:")
        for s in sorted(set(skipped_views)):
            print(f"  {s}")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    if skipped_views:
        sys.exit(2)
    print("\nall assertions held across every view x viewport x theme")
    sys.exit(0)


if __name__ == "__main__":
    main()
