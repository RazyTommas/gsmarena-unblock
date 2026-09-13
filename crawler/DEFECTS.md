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

## Sources verified and NOT built (2026-09-13)

Recorded so nobody re-derives them. A verified source that adds nothing is a result.

**MediaTek bulletin — no longer needed.** `mediatek.com/product-security-bulletin/{month}-{YYYY}`
is reachable with our own crawler UA and robots-clean. It was on the roadmap because
NVD's enrichment collapsed in April 2026 (MediaTek 28/28 enriched in January, 0/7 in
July), making the bulletin look like the only place the affected-chip list survived.

The CNA passthrough fix closed that instead. Measured against three months including
the collapse month itself:

    july-2026      bulletin   7 CVEs · we hold 7 · missing 0
    august-2026    bulletin  34 CVEs · we hold 34 · missing 0
    september-2026 bulletin  18 CVEs · we hold 18 · missing 0

Zero gap. Both routes carry the same data because both originate with MediaTek as
CNA — NVD's `affected[].affectedData[]` IS the vendor's own list, passed through. A
bulletin parser would be a second path to data we already have, with its own HTML to
break. Revisit only if that overlap stops being total.

**deviceinfohw.ru — declined.** 403s our self-identifying crawler UA
(`device-crawler/1.0`), 200s a browser UA, robots.txt 404 so no stated policy. A 403
to a declared crawler is the site refusing automated access; sending a browser string
to get past it is misrepresenting what we are. The earlier hand-verification used a
browser UA, which is why it looked open. Code is written and tested against the
response shape (`deviceinfo_hw.py`) and stays disabled pending permission.

Being honest in the User-Agent is what got refused. That does not make dishonesty the fix.

**HMD / Nokia — correct, zero overlap.** 84 phones, distribution `{01: 84}`, passes.
Our five Nokia entries are all archival (BB5, E6-00, Lumia 710). Module kept, coverage
unchanged.

**Xiaomi — data held, deliberately not applied.** Feed dead since 2025-02 (HTTP 200,
body `null`, verified across six recent months) while the EOS feed on the same host
stamped 2026-09-10. Stored as a floor with `as_of`; writing it as a current patch
level would assert OPEN against every CVE fixed in 19 months, on 331 devices.
