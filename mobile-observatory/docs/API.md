# Read API contract (v1)

The transport may be HTTP or an in-process offline adapter, but both expose the
same typed queries. Storage table names are not API. All collections use cursor
pagination and stable identifiers. Responses include `data_as_of`, applicable
source freshness, and coverage status.

Implemented collection endpoints accept `limit` (default 50, maximum 200) and
either `cursor` or `offset`. The cursor is opaque to clients and must be echoed
unchanged. Page metadata includes `returned`, `total`, `limit`, `offset`, and
`nextCursor`. Filtering runs in SQLite before pagination. `q` and resource
filters use case-insensitive contains matching, supporting both broad filters
such as `vendor=Qualcomm` and exact parts such as `part=SM8750-AB`.

## Core resources

- `GET /v1/radar/events` — filters: since, event type, watch scope, brand,
  hardware model, firmware target/market; expands before/after/evidence.
- `GET /v1/devices` — facets for manufacturer, brand, family, support status,
  Android comparator, market, silicon vendor/family/part/revision.
- `GET /v1/releases` — device/region firmware history; filters include `q`,
  `maker`, `model`, `region`, and `channel`.
- `GET /v1/devices/{id}` — exact hardware identity plus family/variant, aliases,
  support assertions, silicon, latest firmware per target, and coverage.
- `GET /v1/firmware/{id}` — build facts, target-to-market mappings, observation
  times, prior/successor builds, and evidence.
- `GET /v1/silicon` and `/v1/silicon/{id}` — vendor hierarchy, exact parts and
  revisions, components, related hardware, advisories, and evidence.
- `GET /v1/security/vulnerabilities` and `/{id}` — advisories, applicability,
  fix claims, subject verdicts, and traceable evidence path.
- `GET /v1/coverage` — expected versus discovered inventory and explicit
  `complete`, `partial`, `missing`, `stale`, or `unknown` status.
- `GET /v1/sources/health` — source/run freshness independent of domain results.

## Write resources

User-local state (watches, event acknowledgements, saved searches) writes only
to the local database. Admin writes create review decisions or trigger jobs.
Agent credentials can only `POST /v1/proposals`; they cannot call canonical
mutation or verdict endpoints.

## Empty and uncertain results

A collection response never uses an empty `items` array alone. It also returns:

```json
{
  "items": [],
  "coverage": {"status": "partial", "reason": "source_stale"},
  "data_as_of": "2026-09-16T06:00:00Z"
}
```

`null` means unknown/not supplied and is distinct from false, zero, an empty
string, `not_applicable`, or `not_adjudicable`.

## Compatibility

Breaking semantic changes create `/v2`. Additive fields may appear in v1.
Every snapshot declares supported API and schema versions. Clients must not
query SQLite tables directly except documented versioned export views.

## Snapshot read models

Migration 2 publishes semantic views used by the API and offline analysis:

- `v_latest_firmware`: latest release per hardware model, target, and channel;
- `v_device_region_history`: firmware history with canonical device/target,
  Android, SPL, baseband, release, and observation fields;
- `v_chip_devices`: exact silicon vendor/family/part/revision to device links.

They are indexed ordinary views, so snapshots need no refresh cycle and cannot
serve stale materializations. A corpus builder may materialize them behind the
same API contract later if production benchmarks justify it.

Product evidence detail (product confidence, never an exact hardware assertion):

- `GET /api/v1/products/{id}` returns identity state/conclusion, source identities,
  last observation, chipset evidence, Android upgrade events, region/channel
  inventory, and the first 50 firmware/security records. Missing IDs return 404.
- `GET /api/v1/product-releases?product={id}&region=...&channel=...&cursor=...`
  traverses the full captured history; region/channel filters use exact values.
  Download URL, original captured source URL, release date and observation date
  remain distinct. Unknown Android remains null.
- `GET /api/v1/product-security?product={id}&cursor=...` returns only that product's
  publications; a publication is not a CVE applicability assessment.
- `GET /api/v1/chips/products?vendor=...&part=...&cursor=...` returns stable product
  IDs, confidence and row-level specification evidence for reverse navigation.

All paged responses expose `meta.page.total` and `meta.page.nextCursor`. Detail
history is independent of the currently loaded main-screen page. `evidence`
includes captured specification URL, SHA-256, CSV locator, matching rule and
specification fields; `file:` paths never become external UI links.

## Security evidence exploration (2026-09-17)

`GET /api/v1/security/findings`: paginated whole-catalog query. Filters: `q`,
`vendor` (bulletin source-name substring), `date_from`/`date_to` (inclusive effective
publication dates), `part` (exact affected part number), `model` (exact reviewed
hardware code through affected silicon), `mobile_linked=1`, `exact_part=1`,
`fix_status=with_coordinate|without_coordinate`, `sort=oldest_asc` (default newest).
These filters describe captured evidence, never device fix verdicts.

`GET /api/v1/security/cves/{CVE-ID}` returns one CVE independent of pagination,
with `bulletins`, `claims` (decoded `constraint`), `fixes` (decoded `coordinate`),
`verdicts` (current only), `mappedHardware`, and explicit `boundaries`. Unknown CVE:
404 `cve_not_found`. Claims/fixes retain evidence identifiers, source URL, capture
SHA-256, locator, observed timestamp and source. Fix coordinates are CVE-level;
joining them to the CVE does not establish their applicability to each part/device.

### Honest support and event time

`GET /devices?support=Unknown` (or `Supported`, `Likely supported`, `End announced`,
`Unsupported`, or the stored snake_case status) filters before pagination. Support
comes from the latest currently valid exact hardware assertion. With no assertion,
status is `Unknown`; catalog inclusion alone does not prove support. Responses
include `support_status`, `support_evidence_id`, and `support_asserted_at`.

Radar `detectedAt` is the event's recorded time (or the firmware first observation
for the no-event fallback); `effectiveAt` is its effective date. `age` remains a
compatibility alias for detection time. Feed ordering uses detection time.

An unavailable API is displayed as an error with a retry control. It never swaps
real data for synthetic fixtures; explicit server `--demo` remains available.
`GET /api/v1/devices/{model-code}` uses an exact reviewed hardware code (404 if
absent), returning canonical identity, hardware aliases, reviewed silicon,
separate `specifications` source assertions, region/channel counts, independently
paged firmware and exact-part security links. `/releases` accepts `model_exact`,
`region_exact`, and `channel_exact` for stable detail pagination. Existing broad
search filters remain available. Detail pages start at50 records; subsequent
pages use their `nextCursor`. No200-row cap or currently-loaded-page dependency.

Security publication precision is explicit: the known Android/MediaTek captured
CSV importers contain bulletin months, not publication days. Such rows return
`published_at: YYYY-MM` and `published_precision: month`; inclusive date filters
match any overlapping month. Other sources retain day precision. No canonical
security dates are rewritten by this read model. `silicon_vendor` may additionally
scope an exact `part` relationship; it is separate from bulletin `vendor`.
Hardware relationships now carry their own mapping provenance; current verdicts
include decoded `evidence_summary` as well as rule inputs.

Canonical release rows include `build_derived_month` and `date_basis` from the
immutable source correction ledger (or newer explicitly typed observations),
when available. These are separate from `released`, the vendor release date.

### Local watches and complete Radar pagination

`GET /watches` lists durable local preferences. `POST /watches` accepts
`{"subjectType":"hardware_model","subjectId":"<existing ID>","enabled":true}`;
`source_product` is also supported and `enabled:false` removes a watch. Invalid
or unknown targets are rejected; removing a watch surviving corpus replacement
remains possible. Watches never alter canonical evidence or assert support.

`GET /updates` accepts `tab=new|watched|history`, `change=Android upgrade|Security
patch`, existing `q`, `maker`, `model`, `region`, `limit` and `offset`. Filters and
unseen/watch selection apply before pagination, across the entire corpus. Rows
provide typed `subjectType`, `subjectId`, and actual `watched` state. Region uses
captured target codes (e.g. ILO, MID, GLOBAL); no guessed geographic expansion.
`devices` now includes `software_state_basis`:
`source_manifest_latest`, `vendor_release_date`, or `observation_order_only`.
An observation-only fallback does not establish current Android/SPL; these remain
unknown and do not satisfy Android-version filters. Exact device detail includes
`latestFirmware` by region/channel only for a definite captured ordering. The
newest Samsung manifest's explicit latest marker outranks same-time historical
rows; no firmware build strings are treated as chronology.
