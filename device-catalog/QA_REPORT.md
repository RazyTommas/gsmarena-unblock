# Catalog QA — 2026-10-23

Implemented dark, compact tables; custom 1–500 rows per page; stable sorting and pagination;
manufacturer and missing-field filters; readable record details; linked firmware navigation;
coverage charts; copy/download agent prompts; evidence import and reasoning review.

Validation: 28 automated tests passed, Python compilation and JavaScript syntax checks passed.
HTTP checks passed for pagination, invalid page sizes/sort fields, and missing mutation token.
Browser checks covered saved 25-row pages, next-page range, chipset search, prompt generation
and copy action, coverage navigation, invalid settings rejection and preset restoration.
Desktop and narrow mobile layouts were inspected; a mobile search-width issue was fixed.
Rows measured approximately 30 px with single-line cells.

Research: restored 6,250 technical fields from existing captured specification evidence.
Fetched 536 mapped pages: 500 exact title matches, 36 held for identity review; filled 498
release-date values. Imported 28 official Apple assertions for 14 products; the configured
Claude Opus CLI approved and applied all 14 proposals. Source URLs and captured evidence retained.

Remaining limits: this is not a complete technical database. Missing dates, chipsets, firmware
links, patch levels and exact-variant specifications remain visible in Data quality. Some fields
are not applicable to every record. Commercial launch dates are not copied onto hardware
variants without exact evidence. Source mismatches require identity review. Historical firmware
was retained; this pass did not claim a fresh comprehensive firmware crawl.

## Coverage after research

| Table | Field | Present | Total |
|---|---|---:|---:|
| devices | released_at | 512 | 2335 |
| devices | chipset | 1067 | 2335 |
| devices | source_url | 550 | 2335 |
| devices | cpu | 539 | 2335 |
| devices | gpu | 528 | 2335 |
| devices | memory | 539 | 2335 |
| devices | display | 539 | 2335 |
| devices | battery | 539 | 2335 |
| models | released_at | 0 | 854 |
| models | chipset | 537 | 854 |
| models | model_code | 854 | 854 |
| models | codename | 79 | 854 |
| models | source_url | 0 | 854 |
| firmwares | released_at | 52259 | 78401 |
| firmwares | model_released_at | 11885 | 78401 |
| firmwares | chipset | 28363 | 78401 |
| firmwares | version | 52969 | 78401 |
| firmwares | region | 78401 | 78401 |
| firmwares | download_url | 55405 | 78401 |
| firmwares | source_url | 74813 | 78401 |
| firmwares | baseband | 25601 | 78401 |
| firmwares | patch_level | 0 | 78401 |

Offline ZIP: 22,126,258 bytes; extracted into a temporary directory, every manifest hash verified,
and standalone catalog status passed with 2,335 devices and 78,401 firmware records.
Production UI restarted at http://127.0.0.1:8130.
