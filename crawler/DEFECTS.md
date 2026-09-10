# Defect classes — generalised from bugs actually shipped

`audit.py` screens for these classes, so a NEW source gets checked by the same
rules instead of us re-discovering each bug by hand. `fix_data.py` repairs them.

| Class | The general failure | Instance we shipped | Mitigation |
|---|---|---|---|
| **DUP** | No unique key, so every ingester re-run multiplies rows | 23,659 dup rows — the corpus was **40% duplicates** | `ux_roms_key` UNIQUE index + `--dedupe` |
| **ORPHAN** | Joining on a free-text key drops rows silently | 52,607 firmware rows linked to no device → empty drawers | normalised-name relink |
| **COVERAGE** | A facet covering *part* of the corpus hides the rest without saying so | filtering by chipset silently excluded 32,830 rows | UI now prints the excluded count |
| **VOCAB** | One column carrying two vocabularies | `region` held CSC codes (`ILO`) *and* country names (`China`) | `region_kind` = csc / market |
| **SEMANTIC** | One column carrying two meanings | `security_patch` held advisory URLs *and* patch dates | split to `security_url` + `security_level` |
| **JUNK** | Truncated/placeholder values becoming first-class entities | 260 device names ending in `…` became fake distinct devices | strip + `name_truncated` flag |
| **TEMPORAL** | Absent or implausible dates reading as real ones | 837 undated builds; must never sort as old/new | `·· no date`, own sort bucket |
| **TIE** | "Latest" that is ambiguous, so the answer is unstable | 1,810 device+region groups shared the newest date | deterministic tie-break (version, rowid) |
| **LINK** | A link that doesn't deliver what its label promises | 7,468 rows where "↓ get" opened a page, not a file | `link_kind` → label reads `↗ page` |
| **TYPE** | Values that break a comparison and vanish silently | `android='cn'` was CAST to 0, dropping out of "Android ≥ n" | `android_num`, NULL when unparseable |
| **BLOCKED** | Assuming a network path works when it is blocked | gsmarena 429, Samsung CDN 403, samfw Cloudflare | run locally (`enrich_local.py`) or via browser-ingest |
| **CRASH** | Unvalidated input reaching a query | `android_min='abc'` → unhandled `ValueError` → 500 | parsed defensively, bad input ignored |

## Running it

```bash
python3 audit.py                 # scan; exit code = number of FAILs
python3 fix_data.py --all        # repair (verbose, --dry-run supported)
python3 enrich_local.py          # full local enrichment with live progress
```
