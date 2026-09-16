# Product evidence batch validation — 2026-09-17

The reviewed application runs at <http://127.0.0.1:8124/> using
`/tmp/mobile-observatory-product-review-20260917`. Its original input snapshot,
`/tmp/mobile-observatory-review-20260916`, remains unchanged.

## Results

| Check | Result |
|---|---|
| Python suite | 48 passed |
| Frontend API tests | 5 passed |
| JS syntax | app.js and api.js passed |
| Captured specification input | 36 Xiaomi rows; 33 state a chipset |
| Product silicon observations | 14 → 33 |
| New standalone specification products | 19 |
| Combined silicon rows | 183 |
| Product detail/history validation | All 616 products |
| SQLite integrity and foreign keys | Clean |
| Enrichment replay | Identical silicon observations on second pass |
| Combined silicon query | 1,266 ms on this snapshot |

Exact row comparisons verified preservation of 83 hardware models, 4 exact
hardware-silicon relationships, 21,186 canonical firmware releases, 4,886 product
firmware releases, 862 security publications, 597 identity conclusions, 820 domain
events, 6,381 applicability claims and 3,812 fix claims. No data was live-fetched.

The full machine-readable report is in the new snapshot as
`product-batch-validation.json`. Reproduce on a fresh output directory with:

```sh
python3 tools/validate_product_batch.py \
  --source /tmp/mobile-observatory-review-20260916 \
  --output /tmp/mobile-observatory-product-review-new
python3 -m pytest -q
node --test apps/web/api.test.mjs
node --check apps/web/app.js
node --check apps/web/api.js
```

## Browser review

- Explore → Silicon: mobile-linked parts sort first; Qualcomm filtering works.
- Snapdragon 8 Elite Gen 5 → POCO F9 Ultra opens captured GSMArena evidence and ROM history.
- Snapdragon 6s Gen 4 → Xiaomi Poco X8 opens a specification-only product with explicit missing-history/applicability messages.
- Product Evidence → Redmi Pad 2 SE 4G shows captured download URLs; CN + Stable filters produce three captured rows.
- Product Evidence → TECNO patches → TECNO SPARK Go 1S shows six product publications and captured TECNO source links.
- Main product table page 101–200 loads from the backend; product sorting returns Mi 10 with all 32 captured releases in its drawer.
- CVE details retain NVD and original MediaTek bulletin links.
- Light and dark product drawers inspected visually; browser reports no console errors.

Real products have at most 32 captured ROM records here. A regression fixture
traverses 235 releases without duplicates, checks exact product/region/channel
filters, and prevents a reverse chip lookup from including a similarly named part.
Other regression fixtures cover plus/network/brand distinctions, conflicting
specifications, rejected/deferred decisions, remembered conclusions, independent
capture provenance and idempotency.

## Remaining work

Canonical Samsung specification provenance, advanced/shared filters, dedicated
CVE details and security filters, faster silicon read models and live collectors
remain follow-up work. The 19 additional specification products do not establish
new hardware mappings or ROM coverage. Specification launch OS never substitutes
for observed firmware Android versions; publications never establish CVE safety.
