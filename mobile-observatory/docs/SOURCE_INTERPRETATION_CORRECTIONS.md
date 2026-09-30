# Audited source interpretation corrections (2026-09-17)

Two parser defects were found by checking preserved capture code and fields.

Samsung FOTA history CSV `pda_month` was calculated by `crawler/common.py` from
the firmware build identifier. It is an approximate **build month**, not a
vendor release date or Android security patch level. The history adapter now
emits `build_derived_month` and leaves `release_time` null. The canonical promoter
also rejects `release_time` from older history-parser observations, so replaying
immutable old observations cannot reintroduce the error. The correction clears
only matching dates from that exact parser and skips independent release-date
evidence. On the reviewed real corpus, 21,154 dates were cleared (32 releases
already had unknown dates). Android, patch, baseband and build values are intact.

Xiaomi regional targets such as `umi_tr_global` and `nezha_in_global` were matched
to `GLOBAL` before examining their explicit regional suffix. Explicit EEA,
India, Indonesia, Russia, Turkey, Taiwan and Japan suffixes now take precedence
over generic Global. Conflicting specific name/code regions remain unspecified.
No build-string region inference is used. On the real corpus, 2,194 product ROM
regions were corrected. Replaying old observations uses the corrected rule and
natural release identity, avoiding duplicate releases even when delivery method
is null or a new parser produces another observation of the same release.

Apply only after backing up the runtime using SQLite backup:

```sh
PYTHONPATH=src python3 -m mobile_observatory.source_corrections --data-dir COPIED_RUNTIME
```

Migration 0012 adds immutable `source_data_corrections` records with before/after,
reason, time and evidence reference when available. Original artifact bytes and
observation payloads are unchanged. Corrections are idempotent. Invalid old Android
upgrade events are preserved and superseded by explicit `identity_corrected`
events carrying `corrects_event_id`. The real corpus retained its 94 original
upgrades, superseded 56, and added nine valid same-region upgrades: 47 active
upgrades, rather than counting cross-region comparisons as upgrades.

Current event readers use `v_current_domain_events` and filter event type as
usual. Raw `domain_events` remains the full audit log. `firmware_date_evidence`
returns the separate build month and its evidence without presenting it as a
release date. A firmware manifest's explicit `latest` position, not build-month
guessing or tied observation timestamps, should determine its latest build.

## A source that states a non-date where a date belongs (2026-09-30)

A third defect, and a different kind: not a parser misreading a source, but a
source spelling absence as a value and the corpus repeating it as a date.

Six observations from `xiaomi.community.firmware_tracker` carry the four-character
**string** `"null"` in `$.data.release_date` (`typeof` = `text`). It is the only
non-date value in that field across all 96,319 observations; `$.data.publish_date`
and `$.data.release_time` have none. `json_extract` returned it faithfully,
`coalesce` treated it as present, so `observations.effective_at` never fell through
to `observed_at` and the word `null` was published as the effective date of six
builds. It propagated into `product_firmware_releases.vendor_released_at`, where
`releases_page` printed it with no guard.

**The payload is not rewritten.** `payload_json` is the archive and still says
exactly what the source said. What changed is the derived layer, in one place per
derivation:

- `observations.effective_at` — migration 0031 redefines the generated column so
  each stated field passes through the shared rule before `coalesce` sees it;
- `product_firmware_releases.vendor_released_at` and
  `product_security_publications.published_at` — written through
  `source_dates.stated_date` in `promote_approved_product_observations`. Migration
  0031 repairs the six rows already stored and records each in
  `source_data_corrections` under `source_stated_non_date_read_as_absence`.

**One rule, at the writers, and no guards on the readers.** The sentinel had been
noticed twice and patched locally both times — `nullif(pfr.vendor_released_at,
'null')` in `current_firmware.EVIDENCE_SQL` plus four copies in `server.py` — while
the two places that *derive* the value went on storing it, and one reader column
never got a guard at all. All five reader-side guards are gone, and
`tests/test_source_stated_dates.py` fails if one comes back.

**It is a shape test, not a sentinel list.** `NOT IN ('null','N/A','unknown',…)`
would be guesses that grow with every source; a date either looks like a date or it
is not one. Measured over all 96,319 observations, every value any source states in
these three fields is either JSON null or ISO-date-prefixed, so the shape test
discards those six rows and nothing else. `"N/A"` needs no entry to be covered.

**Reported, not just fixed.** `check_corpus` gains two checks:

- `source_states_a_non_date_where_a_date_belongs` — **warning**, 6 on the live
  corpus. The defect is the source's and stays in the archive; since the derived
  columns now read it as absence the corpus states nothing false, so it is not an
  error. It is reported because a source that has *started* spelling absence is a
  change in that source worth seeing. This takes the corpus from `0 errors, 2
  warnings` to `0 errors, 3 warnings`.
- `derived_date_column_holds_a_non_date` — **error**, 0. A source cannot cause it;
  it can only mean a write bypassed `source_dates` or migration 0031 was reverted,
  and then a reader is shown a non-date under a date label.
