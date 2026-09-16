# Security exploration and read performance — 2026-09-17

This batch does not change canonical facts or adjudicate applicability. Migration
0009 adds three reverse lookup indexes. Silicon aggregation no longer multiplies
each applicability claim by every CVE fix claim. Security aggregates independent
relations before joining, retaining the effective CVE/bulletin publication date.

## Review in the interface

Security now searches and pages the whole captured catalog. Advanced filters cover
bulletin vendor/source, publication range (inclusive dates), exact affected part,
exact reviewed model, reviewed-hardware links, and presence/absence of a **CVE fix
coordinate**. No filter promises that a device is fixed or unfixed. Product-only
specification relationships are deliberately excluded from hardware applicability.

A CVE opens `/api/v1/security/cves/{cve}` independently of the currently loaded
page. It includes every bulletin, applicability claim and constraint, fix coordinate,
current adjudicated verdict, and reviewed hardware linked through affected silicon.
Evidence exposes captured source URL, source, locator, SHA-256 and observation time.
A CVE-level fix is not assumed to fix every affected part or any installed firmware.
The chip attention badge now says “without CVE fix coordinate”, replacing “open”.

## Validation

Baseline: 51 Python tests passed before edits. The expanded suite includes a CVE
beyond the first100 entries, unrelated component fix coordinates, a negative
applicability claim, exact-part and date boundaries, route404, and migration replay.
Frontend API checks exercise encoded detail/filter arguments. Parent integration
performs browser QA on the running application.

The original snapshot was opened read-only and backed up to
`/tmp/mo-security-validation/corpus.sqlite` before applying indexes. Measured on the
same copy and process:

| Query | Before | After |
|---|---:|---:|
| Silicon first100 (183 total) | 2.819s | 0.012s |
| Security first100 (4192 total) | 1.369s | 0.123s |

`python3 tools/validate_security_batch.py /path/to/migrated/corpus.sqlite` checks
all4192 catalog rows across21 pages, independent details beyond page1, filter
partitions, integrity and foreign keys. Maximum measured security page:127ms.
Current real coverage:206 exact-part CVEs,3778 CVEs with a coordinate,414 without,
0 with a reviewed hardware link. The zero is a coverage gap, not device safety.

Remaining: source-specific exact advisory URLs where only landing pages were
captured; product-spec links do not establish hardware/CVE applicability; no new
verdicts or severity scores are fabricated. Dedicated detail exposes existing
verdicts only. Mobile-link filtering currently traverses affected silicon-part
claims and reviewed hardware relationships, not broader component/family claims.

## Canonical history follow-up

The dedicated `/devices/{model-code}` drawer exposes all captured ROM history in
50-row pages, exact region/channel filters, aliases, separate reviewed silicon and
community specification assertions, and independent security pagination. The old
200-row cap and substring model lookup are removed from this flow. Date columns
label vendor release, first observation, and build-derived month separately when
available. A query returning many same-time observations is not called “latest
firmware”. Chip drawers fetch exact-part security independently of the main100
CVEs and link to the full filtered Security page when more than50 findings exist.

Tests add235 canonical releases, page every row exactly once, check118 beta rows,
and reject partial model codes in the exact detail lookup. The combined suite
with the LineageOS source dependency passed62 tests; frontend checks passed7.
Bulletins lacking artifact evidence now retain their known advisory source name
and label the source homepage as such; capture hash/time remain unavailable.
