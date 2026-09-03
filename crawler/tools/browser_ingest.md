# Browser-ingest: crawling Cloudflare-gated sources (samfw.com, givemerom.com)

Some firmware sites (samfw.com, givemerom.com) sit behind a Cloudflare **managed
challenge** that defeats plain HTTP *and* headless Chromium from this environment
(the challenge never clears — TLS/JA3 + headless fingerprinting). They can only be read
by a **real browser** that has passed the challenge.

`crawler.py` still can't fetch them, but `samfw.py` (and the generic samfw-shaped path in
`export.py`) can *parse* them. The bridge is a browser-side ingest:

## The mechanism
1. **Open the site in a real browser** and let Cloudflare clear (it auto-solves in a real
   browser after a few seconds — no CAPTCHA solving required).
2. From that cleared tab, **same-origin `fetch()` carries the clearance cookie**, so you
   can pull any other page on that domain:
   ```js
   const html = await fetch('https://samfw.com/firmware/SM-A276B', {credentials:'include'})
                       .then(r => r.text());
   ```
3. **Extract the rows in-page** (parse `html` with `DOMParser`) and POST them to the
   running app's ingest endpoint:
   ```js
   await fetch('http://localhost:8765/ingest', {
     method:'POST', headers:{'Content-Type':'application/json'},
     body: JSON.stringify({ source:'samfw.com', models:[ {model, name, roms:[...]}, ... ] })
   });
   ```
   `app.py` writes each model to `output/<source>/<model>.json`, which `export.py` then
   folds into `data/devices.db` like any other source.

## Notes / gotchas
- Chrome **Private Network Access** can block a public-origin page POSTing to `localhost`.
  The app answers preflight with `Access-Control-Allow-Private-Network: true`; if a POST
  still hangs, fall back to pulling the rows out as compact text and writing the JSON
  locally (that's how the current `output/samfw.com/` and `output/givemerom.com/` files
  were produced).
- **samfw** is keyed by model + **CSC** (region/carrier) → joins to gsmarena's
  `Misc — Models` field. Very granular (80+ CSC regions per model).
- **givemerom** (Tecno/Infinix/itel/Realme) is a **nested folder tree**, not per-device
  pages. Files encode `MODEL…_version_date`; the download is `?a=downloads&b=file&id=N`.
  A production givemerom crawler is a recursive folder walk — the sample here is from one
  firmware bucket to prove the pipeline end-to-end.

## Do NOT
- Never solve a CAPTCHA/Turnstile to get past a challenge. If a site needs an interactive
  checkbox, stop — only auto-clearing challenges are in scope.

## Device specs via kimovil (clean alternative to a gsmarena-banned IP)
When gsmarena rate-limits the IP (429), device **specs** can be pulled cleanly from
**kimovil.com** through a real browser (it Cloudflare-clears the same way samfw does —
an auto-solving challenge, NOT a CAPTCHA). Same-origin `fetch()` from the cleared tab
pulls `where-to-buy-<slug>` pages; the SoC/chipset extracts reliably from the raw HTML
(battery/display render client-side — read them from the live DOM if needed). Ingest as
device docs with a `sections` block and the codename in `Misc — Models` so they join to
firmware. This is how the Tecno specs (Camon 40=Helio G100, Spark 40=G91, Pop 6 Pro=Helio
A22, Pova Curve 5G=Dimensity 7300, …) were obtained without any ban-evasion or CAPTCHA.
