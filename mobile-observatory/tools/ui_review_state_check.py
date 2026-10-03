"""Drive the real UI and assert the wording a reader actually sees.

A screenshot proves a page rendered. These assert the SENTENCES: that no bare enum
reaches the screen, that the terminal state explains itself, that the two
sub-populations are visible as two, and that the coverage strip reports adjudicated
observations separately from pending ones.

Needs Chromium and a populated corpus, so it lives here beside tools/ui_smoke.py
rather than in tests/. Run it against a COPY of the corpus, never the live one:

    PYTHONPATH=src python3 -m mobile_observatory.server --port 8950 --data-dir <a copy>
    python3 tools/ui_review_state_check.py http://127.0.0.1:8950 /tmp/shots

ONE OF THESE CHECKS WAS BROKEN and passed against a corpus that had the defect.
`.badge` carries `text-transform:uppercase`, so `inner_text()` returns
UNRESOLVABLE_ON_CAPTURED_EVIDENCE and the lowercase match for the bare enum never
fired. Found by planting the defect -- reverting the State column to
`badge(x.review_state, ...)` -- and watching the line pass anyway. Every comparison
against rendered badge text is case-folded for that reason.
"""
import sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8950"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/shots"
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' -- ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1200})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_selector(".feed, .data-table", timeout=40000)

    # --- Admin: the identity review inbox -----------------------------------
    page.click('button[data-route="admin"]')
    page.wait_for_selector("#productState", timeout=40000)
    page.wait_for_timeout(2500)

    heading = page.locator("h2.section-title", has_text="Identity review inbox").first.inner_text()
    print("  heading:", heading)
    check("inbox lists all 626 not-serving products", "626" in heading, heading)

    notice = page.locator("#productState").locator("xpath=../preceding-sibling::div[contains(@class,'notice')][1]").inner_text()
    print("  notice:", notice.replace("\n", " ")[:400])
    check("notice says not every row is waiting on you",
          "Not every row here is waiting on you" in notice)
    check("notice explains the adjudication", "adjudicated" in notice.lower())
    check("notice says it is NOT rejected", "not rejected" in notice.lower())
    check("notice names the reopen path", "Reopen" in notice)

    rows = page.locator(".data-table tbody tr")
    body = page.locator("table.data-table").filter(has_text="Product candidate").inner_text()
    check("no bare enum on screen", "unresolvable_on_captured_evidence" not in body.lower(),
          "found the raw enum in the table")
    # .badge carries text-transform:uppercase, so inner_text() returns what the
    # reader SEES. Compared case-insensitively rather than against the styled form,
    # which would make this test a test of the stylesheet.
    check("the state is spelled out", "no identifier to resolve" in body.lower())
    check("sub-population: no independent identifier",
          "no independent identifier exists in any captured source" in body)
    check("sub-population: several candidates",
          "captured evidence names several candidates and nothing discriminates" in body)
    check("adjudicated rows offer Reopen, not Approve/Reject",
          "Reopen" in body and "Approve" not in body)

    badge = page.locator("span.badge", has_text="No identifier to resolve").first
    title = badge.get_attribute("title")
    print("  badge title:", title)
    for phrase in ("Adjudicated", "no reviewer can resolve", "NOT rejected",
                   "evidence layer", "automatically"):
        check(f"badge tooltip says '{phrase}'", phrase in (title or ""))

    page.screenshot(path=f"{OUT}/1-review-inbox.png", full_page=False)

    # The state selector is the proof they are listed, not hidden.
    page.select_option("#productState", "proposed")
    page.wait_for_timeout(2000)
    total = page.locator("h2.section-title", has_text="Identity review inbox").first.inner_text()
    print("  awaiting-review only:", total)
    check("nothing is awaiting review any more", "· 0 product candidates" in total, total)
    page.screenshot(path=f"{OUT}/2-nothing-awaiting.png", full_page=False)

    page.select_option("#productState", "unresolvable_on_captured_evidence")
    page.wait_for_timeout(2000)
    total = page.locator("h2.section-title", has_text="Identity review inbox").first.inner_text()
    print("  adjudicated only:", total)
    check("all 626 are listed under their own state", "626" in total, total)
    page.screenshot(path=f"{OUT}/3-adjudicated-listed.png", full_page=False)

    # --- Explore: the coverage strip ----------------------------------------
    page.click('button[data-route="explore"]')
    page.wait_for_selector(".coverage", timeout=40000)
    page.wait_for_timeout(1500)
    strip = page.locator(".coverage").inner_text()
    print("  coverage strip:", strip.replace("\n", " | "))
    check("Apple reports 4,450 as unresolvable, not as a queue",
          "4,450 obs no identifier can resolve" in strip, strip)
    check("nothing is described as awaiting review for Apple",
          "4,450 obs awaiting review" not in strip)
    apple_tile = page.locator(".cov", has_text="Apple").first
    tip = apple_tile.locator("small", has_text="no identifier can resolve").get_attribute("title")
    print("  Apple tooltip:", tip)
    for phrase in ("66 products", "66 have no independent identifier",
                   "Nothing here is waiting on a reviewer", "nothing is hidden"):
        check(f"coverage tooltip says '{phrase}'", phrase in (tip or ""))
    page.screenshot(path=f"{OUT}/4-coverage-strip.png", full_page=False)

    # --- a product detail view ----------------------------------------------
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_selector(".feed, .data-table", timeout=40000)
    page.click('button[data-route="admin"]')
    page.wait_for_selector("#productState", timeout=40000)
    page.wait_for_timeout(2500)

    check("no console errors", not errors, "; ".join(errors[:3]))
    browser.close()

print()
print("FAILURES:", failures or "none")
sys.exit(1 if failures else 0)
