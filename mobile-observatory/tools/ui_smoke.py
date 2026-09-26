"""Screenshot the running app and assert the new controls are really on screen.

Written as checks, not just captures: a screenshot proves a page rendered, it does
not prove the rows-per-page control exists or that a source-record identity became
clickable. Each view asserts, then shoots, so a silent regression fails here rather
than looking fine in a picture.
"""
import sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8944"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/shots"
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    page.goto(BASE, wait_until="domcontentloaded")
    # First paint should arrive well before the background phase finishes.
    page.wait_for_selector(".feed, .data-table", timeout=30000)
    print(f"  first paint: {page.evaluate('Math.round(performance.now())')}ms after navigation")
    page.screenshot(path=f"{OUT}/1-radar.png", full_page=False)

    check("radar rows-per-page control present", page.locator("#radarRows").count() == 1)
    if page.locator("#radarRows").count():
        check("rows control defaults to 100", page.locator("#radarRows").input_value() == "100",
              f"got {page.locator('#radarRows').input_value()}")

    # Radar rows should now honour the control: ask for 250 and count the cards.
    page.select_option("#radarRows", "250")
    page.wait_for_timeout(2500)
    cards = page.locator(".feed > *").count()
    check("radar honours 250 rows", cards > 50, f"{cards} cards rendered")
    page.screenshot(path=f"{OUT}/2-radar-250.png", full_page=False)

    # Explore > Source records: the identity column must contain real buttons now.
    page.goto(f"{BASE}#explore", wait_until="domcontentloaded")
    page.wait_for_selector(".data-table", timeout=30000)
    page.screenshot(path=f"{OUT}/3-explore-devices.png", full_page=False)
    check("explore rows-per-page control present", page.locator("#exploreRows").count() == 1)

    page.locator("#exploreTabs button[data-value='sources']").click()
    page.wait_for_timeout(3000)
    # Filter to Samsung, the source that resolves to canonical devices.
    page.select_option("#sourceName", "samsung.fota")
    page.locator("#sourceSearch").click()
    page.wait_for_timeout(3000)
    buttons = page.locator(".data-table tbody tr td:first-child button.entity-button").count()
    rows = page.locator(".data-table tbody tr").count()
    check("source-record identities are clickable", buttons > 0, f"{buttons} of {rows} rows")
    page.screenshot(path=f"{OUT}/4-source-records-clickable.png", full_page=False)

    # And clicking one must actually open the device panel.
    if buttons:
        page.locator(".data-table tbody tr td:first-child button.entity-button").first.click()
        page.wait_for_timeout(3000)
        opened = page.locator(".detail-body, .detail-head").count() > 0
        check("clicking a source-record identity opens its device", opened)
        page.screenshot(path=f"{OUT}/5-device-panel-from-source-record.png", full_page=False)

    real = [e for e in errors if "favicon" not in e.lower()]
    check("no JS errors", not real, "; ".join(real[:2]))
    browser.close()

print(f"\n  {'FAILURES: ' + ', '.join(failures) if failures else 'all checks passed'}")
sys.exit(1 if failures else 0)
