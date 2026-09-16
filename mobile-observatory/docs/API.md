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
