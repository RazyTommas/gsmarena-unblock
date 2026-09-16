# Mobile Observatory — continuation handoff

Updated: 2026-09-17 (Asia/Jerusalem)

## Continuation completed — product details and captured silicon

The application at `http://127.0.0.1:8124/` now serves the validated snapshot
`/tmp/mobile-observatory-product-review-20260917`. The original
`/tmp/mobile-observatory-review-20260916` is preserved. Local state was copied
with SQLite backup; new UI activity writes to the new snapshot's local database.

- Silicon product observations increased **14 → 33** (19 Qualcomm, 7 MediaTek,
  6 Unisoc, 1 Xiaomi), with **183** combined silicon rows and **616** products.
- Correction to the previous coverage assumption: the captured `gsm_specs.csv`
  contains **36 Xiaomi rows**, only 33 with chipsets. The 19 additions are
  specification-only products, with no invented firmware or hardware identities.
- Matching preserves brand, `+`, network/regional qualifiers, exact catalog
  codenames, Google Play identifier ambiguity and remembered review conclusions.
- Each product silicon relationship now retains its exact specification URL,
  CSV locator, capture hash, observed time, matching rule and specification fields.
  Captured files are preserved in the new snapshot's `evidence/` directory.
- Product Evidence names and chip-drawer products open a dedicated product detail
  API/drawer with aliases, confidence, specifications, ROM history by region/channel,
  Android upgrades, source/download links and security publications. Histories page
  independently of the main table; missing data and applicability remain explicit.
- No canonical hardware, hardware-silicon, firmware, security claim, event or
  identity-conclusion records changed. Exact hardware-silicon mappings remain 4.
- Validation: **48 Python tests**, **5 frontend API tests**, both JS syntax checks;
  all 616 real product details and complete paginated histories checked, SQLite
  integrity/foreign keys clean, enrichment idempotent. Silicon query: ~1.27 seconds.
- Browser QA: Qualcomm filter → chip → product, specification-only coverage gaps,
  Xiaomi downloads and region/channel filters, TECNO publication links, CVE/NVD and
  bulletin links, main-table page 101–200 and global sorting, readable dark drawer;
  no browser console errors. Test fixtures exercise history beyond 200 releases.

See `docs/PRODUCT_BATCH_VALIDATION.md` and `tools/validate_product_batch.py` for
reproduction. New implementation: `src/mobile_observatory/product_specs.py`;
backend, enrichment/replay, frontend, API contract and tests were extended.

Continue next with canonical Samsung provenance, optional advanced filters,
dedicated CVE detail/security prioritization and silicon read-model performance.
Broader silicon coverage requires more captured specifications; do not infer
mappings from the GSMArena slug inventory or marketing/model catalogs alone.
Live collection remains a separate future adapter task.

## Portable repository distribution

The repository now contains `portable/mobile-observatory-2026-09-17.zip` and its
checksum/count manifest. It includes current and previous reviewed corpus/local
SQLite backups, the legacy crawler DB (five DB files total), original ledger,
captured evidence, agent-review bundle and provenance paths. No active WAL files
are copied; databases are made with SQLite backup.

On another computer, download/clone the whole repository and run
`python3 mobile-observatory/run.py` from its root (Windows: `py -3 ...`). Python
3.11+ is sufficient. It restores real data into `.observatory-data` once, preserves
later local state, and rebases evidence paths when moving that runtime directory.
See `docs/PORTABLE_RUN.md`. The old `start.sh` remains the demonstration launcher.

Distribution validation: 63 repository tests passed, 1 skipped (51 tests within
Mobile Observatory); 5 frontend tests and both syntax checks passed. A separate
copied installation restored the archive, served the API, and resolved all eight
artifact paths entirely within the copied installation. Credential-signature scan
found no matches in outgoing files or archive contents.

## Start here

The active product is the greenfield application in:

`/home/user/gsmarena-unblock/mobile-observatory`

Do not try to force the product back into the legacy crawler UI or its schema. The legacy material under `../crawler/relay/results` is evidence/input for reviewed ingestion. Preserve the system's central rule: raw observations, product-level evidence, reviewed hardware identities, and canonical facts are different confidence layers.

Read these before changing architecture:

- `README.md`
- `docs/PRODUCT.md`
- `docs/ARCHITECTURE.md`
- `docs/IDENTITY_RESOLUTION.md`
- `src/mobile_observatory/server.py`
- `src/mobile_observatory/enrichment.py`
- `apps/web/app.js`

## Product intent

The main user wants a fast, simple, local-first mobile intelligence dashboard:

1. Immediate firmware-update visibility, refreshed several times daily.
2. Complete device/region ROM history with Android, security patch, modem/baseband, original source, and direct ROM download when the source provides one.
3. Mobile silicon exploration by vendor, family, exact part, revision, and reverse device/product lookup.
4. Security relationships connecting CVE → bulletin/component → silicon → device/product → patch or firmware fix, without inventing applicability.
5. Android-version questions such as supported Samsung/Xiaomi devices stuck on Android 14/15/16.
6. Israel and surrounding Middle East/Levant regions are especially important, while retaining global coverage.
7. Offline export/query capability.
8. Agent-assisted identity resolution and evidence research, with remembered decisions and no silent canonical changes.

The user strongly prefers a clean interface. Do not solve coverage gaps by making the main screens complicated.

## Current runtime

The reviewed snapshot currently runs at:

`http://127.0.0.1:8124/`

Start/restart it with:

```sh
cd /home/user/gsmarena-unblock/mobile-observatory
PYTHONPATH=src python3 -m mobile_observatory.server \
  --data-dir /tmp/mobile-observatory-product-review-20260917 \
  --host 127.0.0.1 --port 8124
```

Run validation with:

```sh
cd /home/user/gsmarena-unblock/mobile-observatory
python3 -m pytest -q
node --check apps/web/app.js
node --check apps/web/api.js
```

Previous-batch result: **41 Python tests passed**, both frontend syntax checks passed, and browser QA reported no current console errors.

## Previous-batch data snapshot (see continuation above)

Approximate current corpus:

- 83 reviewed Samsung hardware models.
- 21,186 canonical Samsung firmware releases.
- 4,886 Xiaomi product-level firmware releases.
- 862 TECNO product security publications.
- 94 evidence-backed Android upgrade events.
- 174 silicon rows in the combined mobile/security read model.
- 4 exact reviewed hardware↔silicon mappings.
- 14 GSMArena-backed product↔silicon observations: 9 Qualcomm, 4 MediaTek, 1 Xiaomi/XRing.
- 4,192 CVEs plus MediaTek/Android applicability and fix-coordinate claims.
- Only a small remaining identity-agent candidate set; do not reintroduce hundreds of manual reviews.

The low number of exact hardware↔silicon mappings is a **coverage gap**, not proof that devices lack chips. Product-level mappings are deliberately shown separately from reviewed hardware-model mappings.

## What the latest batch changed

### Reliability and navigation

- “Mark visible seen” now uses one bulk API transaction: `POST /api/v1/updates/acknowledge-bulk`.
- Radar device names are clickable.
- Device and chip drawers expose history and reverse relationships.
- Android `Unknown` is explained as source absence; build strings are not guessed into Android versions.
- Search/filter fields retain focus and chip vendor/family/part inputs commit on Enter.
- Reset alignment and dark tables were fixed.

### Silicon

- `chips_page` now builds a combined read model from canonical `silicon_parts` and `observed_product_silicon`.
- Default sort is **mobile-linked first**, then link count, CVE count, and name.
- Each row separates:
  - `canonical_devices`: exact reviewed hardware mappings.
  - `product_devices`: product-level GSMArena specification mappings.
  - `devices`: their display total, labeled “mobile links.”
- Qualcomm now appears from captured product evidence.
- Chip details list reviewed hardware models and product-evidence names in separate sections.
- Bulletin-only MediaTek parts remain available but no longer dominate the first page.
- Chip endpoint time was reduced from roughly 2.6s to roughly 1.5s on the current snapshot. It can still be optimized further.

### Security

- Findings sort by effective publication date descending.
- “Open findings” was replaced with a more truthful “Unfixed linked claims” explanation.
- Every security row is clickable and opens its reasoning/evidence chain.
- Details link to the standard NVD CVE record and captured original vendor bulletin URL.
- Zero device mappings never means “safe”; the UI says applicability is incomplete.

### ROM and source provenance

- Xiaomi product ROM rows expose captured direct `bigota.d.miui.com` download URLs.
- Product rows expose GSMArena links when an exact captured specification slug exists.
- Samsung canonical firmware exposes the original FOTA manifest URL when captured.
- A Samsung manifest link is not mislabeled as a ROM package download.
- Source Records expose captured download/source URLs.
- Only `http:` and `https:` URLs are rendered as external links; local `file:` evidence paths are not exposed as web links.

### Filtering and sorting

- Product Evidence supports product/codename/build search, maker, region, and date/name/Android sorting.
- Explore now has mode-specific sorting:
  - Devices: latest firmware, name, Android.
  - Silicon: mobile-linked, device count, CVE count, name.
  - ROMs: newest, oldest, device name, Android.
  - Source Records: newest, oldest, source, identity.
- Backend pagination receives these filters/sorts; this is not limited to filtering the visible 100 rows.

### Manual collection

- Admin accepts one or more installed captured-replay sources.
- “Smart” maps compatible evidence scopes per source.
- The target field is explicitly described as a matching hint, not an AI prompt or live web query.
- Incompatible source/scope combinations are rejected before queueing.

## Important files changed

- `apps/web/app.js`
- `apps/web/api.js`
- `apps/web/styles.css`
- `src/mobile_observatory/server.py`
- `tests/test_api.py`

Do not overwrite unrelated changes in the workspace. The outer repository currently contains untracked/modified work, including this application directory.

## Known gaps and recommended next batch

Proceed in this order:

1. **Expand mobile silicon coverage safely — first captured batch completed above.**
   - The available capture has 36 Xiaomi specifications, 33 with chipsets; all 33 are now represented at product confidence.
   - Improve name/codename/model matching using `gsm_specs.csv`, Xiaomi `devices.yml`, Google Play supported devices, and remembered identity conclusions.
   - Promote only exact/defensible relationships. Keep ambiguous matches at product level or in the agent proposal queue.
   - Add evidence/source links to each silicon relationship, not just a generic “GSMArena-backed” label.

2. **Create a real product detail drawer/page — completed above.**
   - Product Evidence product names should be clickable.
   - Show all aliases/codenames, complete ROM history by region/channel, Android upgrades, chipset/specification evidence, security publications, source URLs, ROM downloads, last observation, and identity confidence.
   - Chip drawer product names should link to this detail.

3. **Improve canonical device provenance.**
   - Add exact GSMArena/vendor specification links for canonical Samsung devices where defensible.
   - Surface ROM package links only when actually present; otherwise show manifest/source links with correct labeling.

4. **Improve filters without clutter.**
   - Add clickable sortable table headers and optional advanced filters/drawers rather than permanently adding many controls.
   - Add maker, Android, channel, date range, source, confidence, “has download,” “has chip,” and “has security mapping” where appropriate.
   - Preserve URL/query state if practical so filtered views can be shared/reopened.

5. **Security prioritization.**
   - Allow “mobile-linked only,” vendor, date range, fixed/unfixed, exact-part applicability, and device/product filters.
   - Add a dedicated CVE detail API instead of relying only on the loaded 100-row page.
   - Import exact advisory URLs/month URLs when the captured datasets provide them; current MediaTek URL is the vendor bulletin landing page.

6. **Performance/read models.**
   - The combined Silicon query is still about 1.5s. Consider a materialized/read-model table refreshed during batch ingestion.
   - Avoid attaching the legacy database directly to web requests.

7. **Live collection boundary.**
   - Current manual jobs replay preserved local artifacts. They are truthful about not being live.
   - Add live vendor/OTA collectors only with explicit adapters, rate/error handling, provenance, and coverage health. Do not make a replay look like a refresh.

## Non-negotiable correctness rules

- Never invent hardware model codes, Android versions, security applicability, fix state, ROM download URLs, or device↔chip mappings.
- A bulletin mentioning a CVE is not device applicability.
- A product-level specification match is not automatically an exact hardware-model match.
- Keep effective vendor dates separate from observation/import dates.
- Preserve original evidence and provenance.
- Agent output enters proposals/review; it does not silently rewrite canonical facts.
- “No records” and “zero open findings” must never be presented as proof of absence or safety.

## Suggested immediate acceptance checks

After the next change, verify in the browser:

1. Explore → Silicon opens with mobile-linked Dimensity/Snapdragon rows first.
2. Qualcomm filter works and shows linked product/hardware counts.
3. Clicking a Qualcomm chip shows its mobile products.
4. Security CVE details open both NVD and original bulletin links.
5. Product Evidence exposes valid Xiaomi ROM downloads and GSMArena links.
6. Dark theme tables and filters remain readable.
7. Sorting/filtering works beyond the first 100 rows.
8. `python3 -m pytest -q` remains green.
