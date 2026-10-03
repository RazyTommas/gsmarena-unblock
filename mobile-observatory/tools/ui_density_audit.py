"""DEV TOOL (not shipped, not imported by the app): measure row density.

Why this exists beside tools/ui_layout_audit.py
-----------------------------------------------
ui_layout_audit.py answers "is anything unreachable or unreadable". It does not
answer "how much does one screen hold", which is the whole question a density
mode is judged on -- and a density mode is exactly the kind of change that is
easy to *assert* ("rows are tighter now") and easy to get wrong in the direction
that matters (a row that looks tighter because a value was clipped away).

So this tool measures three things per view, per density:

  1. mean rendered row height, over every row actually in the DOM
  2. rows per screen, derived from (1) as
         floor((viewport height - 68px sticky topbar) / mean row height)
     -- a derived figure, stated as a formula rather than as an observation
  3. the toolbar's WRAPPED LINE COUNT: the number of distinct y-offsets its
     visible children occupy. 1 means one line. This is what "the export csv is
     in a new line" is, measured.

And one correctness check that belongs here rather than in the layout audit,
because it is specific to a density mode:

  4. NO CELL IS CLIPPED. For every `.data-table td`, scrollWidth must not
     exceed clientWidth, and no cell may compute to
     `text-overflow:ellipsis` or `white-space:nowrap`. Compact mode is allowed
     to hide a whole labelled fact; it is never allowed to truncate one, which
     is the defect styles.css's own comment block records at length.

Usage:
    python3 tools/ui_density_audit.py [BASE_URL] [--viewport=1920x1080]
                                      [--themes=light,dark] [--json=PATH]
                                      [--plant=NAME]

Exit 0 = measured, and assertion 4 held in both densities.
Exit 1 = a cell was clipped, or the toolbar wrapped at the wide viewport.
Exit 2 = a view could not be reached (inconclusive, NOT a pass).
"""

import json
import sys

from playwright.sync_api import sync_playwright

_args = [a for a in sys.argv[1:] if not a.startswith("--")]
_flags = [a for a in sys.argv[1:] if a.startswith("--")]
BASE = _args[0] if _args else "http://127.0.0.1:8877"
VIEWPORT = next((a.split("=", 1)[1] for a in _flags if a.startswith("--viewport=")), "1920x1080")
VW, VH = (int(n) for n in VIEWPORT.split("x"))
THEMES = next((a.split("=", 1)[1] for a in _flags if a.startswith("--themes=")), "light").split(",")
JSON_OUT = next((a.split("=", 1)[1] for a in _flags if a.startswith("--json=")), None)
SHOTS = next((a.split("=", 1)[1] for a in _flags if a.startswith("--shots=")), None)

# --plant=NAME re-introduces one defect at runtime so this harness's ability to
# FAIL is reproducible rather than asserted.
#
#   compact-clips     compact mode truncates a cell instead of hiding a fact
#   toolbar-wraps     the toolbar wraps again at the wide viewport
#   no-inherit        one view does not inherit the density rule
#   no-persist        the toggle sets the attribute but stores nothing
#   no-keyboard       the toggle is reachable only with a mouse
PLANTS = {
    "compact-clips": ('[data-density="compact"] .data-table td{'
                      'white-space:nowrap!important;max-width:28ch!important;'
                      'overflow:hidden!important;text-overflow:ellipsis!important}'),
    "toolbar-wraps": '.toolbar{flex-wrap:wrap!important}.toolbar .spacer{flex:1 0 100%!important}',
    # ONE view left behind, not all of them: the Security grid is the only table
    # whose cells carry `.security-detail`, so this is what a per-page density
    # rule that forgot a page would actually look like.
    "no-inherit": ('[data-density="compact"] .data-table td:has(.security-detail) .subtle{'
                   'display:block!important}'),
    "no-persist": "",      # handled in check_control(), not as a stylesheet
    "no-keyboard": "#densityToggle{outline:0!important}",
}
# Only the stylesheet plants go through the <style> element.
CSS_PLANTS = {k: v for k, v in PLANTS.items() if v}
PLANT = next((a.split("=", 1)[1] for a in _flags if a.startswith("--plant=")), None)
if PLANT and PLANT not in PLANTS:
    sys.exit(f"unknown --plant={PLANT}; choose from {sorted(PLANTS)}")

# route, tab container, tab value, label -- all twelve views
VIEWS = [
    ("radar", "#radarTabs", "new", "radar-new"),
    ("radar", "#radarTabs", "watched", "radar-watched"),
    ("radar", "#radarTabs", "history", "radar-history"),
    ("explore", "#exploreTabs", "devices", "explore-devices"),
    ("explore", "#exploreTabs", "silicon", "explore-silicon"),
    ("explore", "#exploreTabs", "releases", "explore-releases"),
    ("explore", "#exploreTabs", "sources", "explore-sources"),
    ("watchlist", None, None, "watchlist"),
    ("products", "#productEvidenceTabs", "firmware", "products-firmware"),
    ("products", "#productEvidenceTabs", "patches", "products-patches"),
    ("security", None, None, "security"),
    ("admin", None, None, "admin"),
]

TOPBAR = 68  # the sticky header, as declared in styles.css

PROBE = r"""
(topbar) => {
  const vis = el => { const cs = getComputedStyle(el);
                      return cs.display !== 'none' && cs.visibility !== 'hidden'; };
  const desc = el => { let s = el.tagName.toLowerCase();
                       if (el.id) s += '#' + el.id;
                       if (el.classList.length) s += '.' + [...el.classList].join('.');
                       return s; };

  // --- toolbars: how many lines does each occupy ---------------------------
  // NOT `new Set(children.map(top))`. `.toolbar` is align-items:center, so two
  // controls of different heights sitting on the SAME row have different tops:
  // that counted the 8-item Explore toolbar as 5 lines when it occupied 2, and
  // would have made any "fewer lines" claim unfalsifiable. Lines are vertical
  // BANDS -- a child opens a new one only when it does not overlap the band so
  // far, which is what a flex line is.
  const lineCount = kids => {
    const rects = kids.map(c => c.getBoundingClientRect()).sort((a, b) => a.top - b.top);
    let lines = 0, bottom = -1e9;
    for (const r of rects) {
      if (r.top + 1 >= bottom) { lines++; bottom = r.bottom; }
      else bottom = Math.max(bottom, r.bottom);
    }
    return lines;
  };
  const bars = [...document.querySelectorAll('#app .toolbar')].map(t => {
    const kids = [...t.children].filter(vis);
    return { lines: lineCount(kids), items: kids.length,
             height: Math.round(t.getBoundingClientRect().height),
             has_export: !!t.querySelector('#exportView'),
             overflows: Math.max(0, t.scrollWidth - t.clientWidth),
             label: (t.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 52) };
  });

  // --- the repeating unit: a table row, or a Radar feed card ---------------
  let unit = 'tr', rows = [...document.querySelectorAll('#app .data-table tbody tr')].filter(vis);
  if (!rows.length) { unit = 'update-card';
                      rows = [...document.querySelectorAll('#app .update-card')].filter(vis); }
  const hs = rows.map(r => r.getBoundingClientRect().height).filter(h => h > 0);
  const mean = hs.length ? hs.reduce((a, b) => a + b, 0) / hs.length : null;

  // --- assertion 4: no cell may be clipped --------------------------------
  const clipped = [];
  for (const td of document.querySelectorAll('#app .data-table td, .detail-panel .data-table td')) {
    if (!vis(td)) continue;
    const cs = getComputedStyle(td);
    const over = td.scrollWidth - td.clientWidth;
    const bad = [];
    if (over > 1) bad.push('scrollWidth +' + over + 'px');
    if (cs.textOverflow === 'ellipsis') bad.push('text-overflow:ellipsis');
    if (cs.whiteSpace === 'nowrap' || cs.whiteSpace === 'pre') bad.push('white-space:' + cs.whiteSpace);
    if (bad.length) clipped.push({ sel: desc(td), why: bad.join(' + '),
                                   text: (td.textContent || '').trim().slice(0, 44) });
  }

  // --- what compact actually removed from view ----------------------------
  // Tables and Radar cards are counted SEPARATELY because they are promised
  // different things: a table's second line is HIDDEN in compact (so the count
  // must reach 0 on every view, which is what "no per-page exception" means),
  // while a Radar card's is collapsed inline and must stay visible.
  const tableSecondary = [...document.querySelectorAll(
      '#app .data-table td .subtle, #app .data-table td small')];
  const cardSecondary = [...document.querySelectorAll('#app .update-card p')];

  // --- assertion 5: compact may hide a LINE, never a whole cell -----------
  // A cell whose only content was its second line would render as an empty box,
  // which is a fact removed with no trace that it ever existed -- a different
  // and worse thing than a hidden labelled line. Reported separately so it can
  // never be absorbed into the "hidden nodes" count.
  const emptied = [];
  for (const td of document.querySelectorAll('#app .data-table td')) {
    if (!vis(td)) continue;
    if ((td.textContent || '').trim()) continue;
    if (td.querySelector('img, svg, input, canvas')) continue;
    if (!(td.innerHTML || '').trim()) continue;            // genuinely empty in the data
    emptied.push({ sel: desc(td), html: td.innerHTML.trim().slice(0, 70) });
  }

  return {
    density: document.documentElement.getAttribute('data-density'),
    // WHICH view was actually measured. The first run of this tool reported
    // byte-identical numbers for Security and Admin, because settle() waits for
    // `.data-table` -- which the PREVIOUS view's table already satisfies. An
    // unmeasured view read as a measured one.
    route: (document.getElementById('crumb') || {}).textContent || null,
    h1: (document.querySelector('#app h1') || {}).textContent || null,
    active_tab: (document.querySelector('#app .segmented button.active') || {}).textContent || null,
    toolbars: bars,
    unit, row_count: hs.length,
    mean_row_height: mean === null ? null : Math.round(mean * 10) / 10,
    rows_per_screen: mean ? Math.floor((window.innerHeight - topbar) / mean) : null,
    clipped: clipped.slice(0, 6), clipped_total: clipped.length,
    emptied: emptied.slice(0, 6), emptied_total: emptied.length,
    table_secondary: tableSecondary.length,
    table_secondary_shown: tableSecondary.filter(vis).length,
    card_secondary: cardSecondary.length,
    card_secondary_shown: cardSecondary.filter(vis).length,
  };
}
"""

failures = []


def fail(where, msg):
    failures.append(f"{where}: {msg}")
    print(f"    FAIL  {where}: {msg}")


def settle(page):
    try:
        page.wait_for_selector(".data-table, .feed, .empty, .metric, .admin-grid", timeout=20000)
    except Exception:
        return False
    page.wait_for_timeout(400)
    return True


# The eyebrow each renderer writes into #app -- proof that THIS view painted.
# Not the crumb and not the rail's `.active` flag: markRoute() writes both
# BEFORE render() runs, so a renderer that throws leaves the previous view's DOM
# under the new view's name. renderAdmin() did exactly that, and the first run of
# this tool reported byte-identical numbers for Security and Admin as a result.
VIEW_PAINTED = {
    "radar": "Update Radar",
    "explore": "Device & Silicon Explorer",
    "watchlist": "Your watchlist",
    "products": "Resolved product evidence",
    "security": "Security Relations",
    "admin": "Operations",
}


def _on_route(page, route):
    return page.evaluate(
        """w => ((document.querySelector('#app .eyebrow')||{}).textContent||'')
                .trim().toLowerCase() === w.toLowerCase()""", VIEW_PAINTED[route])


def goto_view(page, route, tabs, tab):
    page.evaluate("r => { if (location.hash !== '#'+r) location.hash = '#'+r; }", route)
    for _ in range(40):
        page.wait_for_timeout(150)
        if _on_route(page, route):
            break
    else:
        return False
    if not settle(page):
        return False
    if tabs:
        ok = page.evaluate(
            """([sel, val]) => { const b = document.querySelector(sel + " button[data-value='" + val + "']");
                                 if (!b) return false; b.click(); return true; }""",
            [tabs, tab])
        if not ok:
            return False
        for _ in range(40):
            page.wait_for_timeout(150)
            if page.evaluate(
                """([sel, val]) => { const b = document.querySelector(sel + " button[data-value='" + val + "']");
                                     return !!(b && b.classList.contains('active')); }""", [tabs, tab]):
                break
        else:
            return False
        page.wait_for_timeout(700)
        if not settle(page):
            return False
    return True


def apply_plant(page):
    if PLANT not in CSS_PLANTS:
        return
    page.evaluate("""css => { let s = document.getElementById('__plant');
                              if (!s) { s = document.createElement('style'); s.id = '__plant';
                                        document.head.appendChild(s); }
                              s.textContent = css; }""", CSS_PLANTS[PLANT])


def check_control(page):
    """The control itself: keyboard-reachable, visibly focused, and remembered.

    Measured through the BROWSER, never through the attribute: a toggle that
    writes data-density but stores nothing looks identical on screen until the
    next page load, and 'the state is obvious' is a claim about what a reader
    sees, not about what the DOM holds."""
    print("\n=== the control ===")
    # keyboard: Tab from the theme select must land on it, and Space must work
    page.focus("#themeSelect")
    page.keyboard.press("Tab")
    focus = page.evaluate("""() => { const a = document.activeElement;
        const cs = getComputedStyle(a);
        return { id: a.id, outline: cs.outlineStyle + ' ' + cs.outlineWidth + ' ' + cs.outlineColor }; }""")
    if focus["id"] != "densityToggle":
        fail("control/keyboard", f"Tab from the theme select landed on {focus['id']!r}, "
                                 f"not the density toggle")
    elif focus["outline"].startswith("none") or focus["outline"].split()[1] in ("0px", "0"):
        fail("control/focus-ring", f"focused with the keyboard, the toggle computes "
                                   f"outline:{focus['outline']} -- an invisible focus state")
    else:
        print(f"    PASS  Tab reaches #densityToggle; focus ring outline:{focus['outline']}")
    before = page.evaluate("() => document.documentElement.getAttribute('data-density')")
    page.keyboard.press(" ")
    page.wait_for_timeout(200)
    after = page.evaluate("() => document.documentElement.getAttribute('data-density')")
    if before == after:
        fail("control/space", f"Space on the focused toggle did nothing (data-density stayed {before!r})")
    else:
        print(f"    PASS  Space toggles it: {before!r} -> {after!r}")

    # persistence: set compact, reload, and read the ROOT and the control back
    set_density(page, "compact")
    if PLANT == "no-persist":
        page.evaluate("() => localStorage.removeItem('observatory-density')")
    stored = page.evaluate("() => localStorage.getItem('observatory-density')")
    page.reload(wait_until="domcontentloaded")
    settle(page)
    got = page.evaluate("""() => [document.documentElement.getAttribute('data-density'),
                                  document.getElementById('densityToggle').getAttribute('aria-pressed')]""")
    if got != ["compact", "true"]:
        fail("control/persistence",
             f"stored {stored!r}, but after a reload the root reads data-density={got[0]!r} "
             f"and the control reads aria-pressed={got[1]!r} -- the choice did not survive")
    else:
        print(f"    PASS  compact survives a reload (localStorage {stored!r}, "
              f"root {got[0]!r}, aria-pressed {got[1]!r})")
    # and full must survive too, as the ABSENCE of the attribute
    set_density(page, "full")
    page.reload(wait_until="domcontentloaded")
    settle(page)
    got = page.evaluate("""() => [document.documentElement.getAttribute('data-density'),
                                  document.getElementById('densityToggle').getAttribute('aria-pressed')]""")
    if got != [None, "false"]:
        fail("control/persistence-full",
             f"after choosing full and reloading, the root reads data-density={got[0]!r} "
             f"and the control aria-pressed={got[1]!r}")
    else:
        print("    PASS  full survives a reload as an ABSENT attribute, control unpressed")


def set_density(page, density):
    """Drive the real control, not the attribute: a toggle that does not
    persist or does not reach the root must show up as a measurement."""
    # `want` is the STRING "full" or "compact", never None: Playwright turns a
    # Python None into JS `undefined`, and `getAttribute(...) === undefined` is
    # false for an absent attribute, so the loop clicked three times and left
    # both passes measuring compact. The tool's own cross-check caught it, which
    # is the only reason it is not in the numbers.
    page.evaluate("""want => {
        const b = document.getElementById('densityToggle');
        if (!b) throw new Error('no #densityToggle in the chrome');
        const now = () => document.documentElement.getAttribute('data-density') === 'compact'
                          ? 'compact' : 'full';
        for (let i = 0; i < 3; i++) { if (now() === want) return; b.click(); }
        throw new Error('the toggle would not settle on ' + want + '; it reads ' + now());
    }""", density)
    page.wait_for_timeout(250)


def main():
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": VW, "height": VH})
        page = ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded")
        if not settle(page):
            print("the app never painted; cannot measure")
            sys.exit(2)
        apply_plant(page)
        check_control(page)
        inconclusive = []
        for theme in THEMES:
            page.select_option("#themeSelect", theme)
            page.wait_for_timeout(150)
            for density in ("full", "compact"):
                set_density(page, density)
                got = page.evaluate("() => document.documentElement.getAttribute('data-density')")
                want = None if density == "full" else "compact"
                if got != want:
                    fail(f"density/{density}", f"the root reads data-density={got!r}, expected {want!r}")
                print(f"\n=== {VW}x{VH} {theme} / {density} (root data-density={got!r}) ===")
                for route, tabs, tab, label in VIEWS:
                    where = f"{label}@{VW}x{VH}/{theme}/{density}"
                    if not goto_view(page, route, tabs, tab):
                        inconclusive.append(where)
                        print(f"    SKIP  {where}: view never reached (inconclusive, not a pass)")
                        continue
                    apply_plant(page)
                    page.wait_for_timeout(120)
                    r = page.evaluate(PROBE, TOPBAR)
                    results[where] = r
                    wrapped = [b for b in r["toolbars"] if b["lines"] > 1]
                    print(f"    {label:<18} {r['unit']:<12} n={r['row_count']:<4} "
                          f"mean row {str(r['mean_row_height']):>6}px  "
                          f"rows/screen {str(r['rows_per_screen']):>4}  "
                          f"toolbars {[b['lines'] for b in r['toolbars']]}  "
                          f"table-2nd {r['table_secondary_shown']}/{r['table_secondary']}  "
                          f"card-2nd {r['card_secondary_shown']}/{r['card_secondary']}  "
                          f"emptied {r['emptied_total']}")
                    # EVERY view inherits the rule, or it is not one rule.
                    if density == "compact" and r["table_secondary_shown"]:
                        fail(where, f"{r['table_secondary_shown']} of {r['table_secondary']} table "
                                    f"second-line nodes are still visible in compact -- this view "
                                    f"does not inherit the density rule")
                    if density == "full" and r["table_secondary_shown"] != r["table_secondary"]:
                        fail(where, f"full mode hides {r['table_secondary'] - r['table_secondary_shown']} "
                                    f"second-line node(s); full must be the untouched rendering")
                    if density == "compact" and r["card_secondary"] and not r["card_secondary_shown"]:
                        fail(where, "compact hid a Radar card's second line instead of collapsing it "
                                    "inline -- those four columns are all primary")
                    if r["clipped_total"]:
                        fail(where, f"{r['clipped_total']} cell(s) CLIPPED rather than hidden; "
                                    + "; ".join(f"{c['sel']} {c['why']} {c['text']!r}"
                                                for c in r["clipped"]))
                    if r["emptied_total"]:
                        fail(where, f"{r['emptied_total']} cell(s) render EMPTY while holding markup "
                                    f"-- a fact removed with no trace, not a hidden second line; "
                                    + "; ".join(f"{c['sel']} {c['html']!r}" for c in r["emptied"]))
                    if VW >= 1366 and wrapped:
                        fail(where, f"{len(wrapped)} toolbar(s) wrap at {VW}px: "
                                    + "; ".join(f"{b['lines']} lines, export={b['has_export']}, "
                                                f"{b['label']!r}" for b in wrapped))
                    if SHOTS:
                        page.screenshot(path=f"{SHOTS}/{label}-{density}-{theme}.png", full_page=False)
        browser.close()

    if JSON_OUT:
        with open(JSON_OUT, "w") as fh:
            json.dump(results, fh, indent=1, sort_keys=True)
        print(f"\nmeasurements written to {JSON_OUT}")
    print("\n" + "=" * 72)
    if inconclusive:
        print(f"INCONCLUSIVE -- {len(inconclusive)} view(s) never reached:")
        for s in sorted(set(inconclusive)):
            print(f"  {s}")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    if inconclusive:
        sys.exit(2)
    print("every view measured; no cell clipped in either density; no toolbar wrapped")
    sys.exit(0)


if __name__ == "__main__":
    main()
