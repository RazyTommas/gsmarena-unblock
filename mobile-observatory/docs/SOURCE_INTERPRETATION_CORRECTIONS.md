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

## The shape rule was only half the rule (2026-10-04, migration 0034)

A date that looks like a date is half of reading a stated date. The other half is
the NAME the source wrote it under, and that half was broken in exactly the shape
this codebase keeps paying for.

`promote_approved_product_observations` read `$.data.release_date` and
`$.data.branch` — the `xiaomi.community.firmware_tracker` vocabulary.
`mifirm.community.firmware_archive` publishes the identical two facts as
`$.data.vendor_released_at` and `$.data.channel`. Nothing errored. All **21,845**
promoted mifirm releases simply stored `vendor_released_at IS NULL` and
`channel='unknown'` with a real date and a real branch sitting in the payload
beside them.

The consequence was worse than a blank column. `android_version_changed` picks its
before/after pair by walking
`ORDER BY product_id, region_code, channel, vendor_released_at, id`, and with every
date NULL the sort collapses onto `id` —
`uuid5(_NS,'product-firmware'||observation_id)`. Measured on a read-only copy of
the live corpus:

| | |
|---|---|
| `android_version_changed` events published | **5,804** |
| ordered on a pair where BOTH releases are undated | **5,643** |
| ordered on two stated dates | 56 |
| citing release rows the corpus no longer holds | 105 |

Two independent instruments agree on the 5,643: the pair's dates read back through
`dedupe_key`, and the shape of `occurred_at` (`vendor_released_at or created_at`,
so a full timestamp there *is* the fallback).

**Replayed with the dates and channels the sources stated all along: of the 5,690
events whose inputs still exist, 5,588 would not have been produced and 1,064 that
should exist never were.** 102 are the same claim either way. The date alone
accounts for 5,524 of the 5,588; the channel accounts for the rest by splitting
mifirm's `stable` (17,210) and `developer` (4,630) branches, which had been walked
as one sequence. 278 surviving pairs assert a direction the real dates reverse.

### The fix, and what was deliberately not done

- **The spellings are declared once**, in `source_dates.py`, beside the shape test:
  `VENDOR_RELEASE_DATE_FIELDS`, `STATED_CHANNEL_FIELDS`, `PUBLICATION_DATE_FIELDS`,
  `SECURITY_PATCH_LEVEL_FIELDS`, and `DATE_FIELDS_NOT_A_RELEASE_DATE` with each
  adapter's own stated reason for why its date is not one.
  `tests/test_firmware_order_basis.py` **scans every adapter** for a date-ish or
  channel-ish payload key that appears in none of those lists. That is the part
  that catches the next adapter rather than the last one.
- Two more writers went through the same rule, found by that scan and not by
  reading: `collectors.promotion.SamsungFirmwarePromoter` (a third spelling of the
  date, a second of the channel, and its default was `"stable"` — an assertion,
  not an absence) and `hmd_updates.import_hmd_updates`. Both were measured to be
  byte-identical for their own sources first; the Samsung channel is an input to
  `release_id`, so a different value would have re-keyed 21,186 rows.
- **The 5,804 events are not retracted.** `domain_events` carries
  `domain_events_no_update` / `_no_delete`; deleting published facts is a larger
  harm than leaving them, and it is not an agent's call. Instead
  `domain_event_ordering` records what decided each one, **measured before** the
  backfill and frozen afterwards. The order of migration 0034's two halves is
  load-bearing: reversed, it would stamp `vendor_release_date` onto 5,643
  hash-ordered events.
- **The 21,845 release dates and channels are repaired**, in the same migration,
  with one `source_data_corrections` row each — the precedent migration 0031 set
  for the same column, for the same reason: `product_firmware_releases` is a
  derived read model and the payload is untouched. The 3,190 releases whose
  publishers name no channel, and the 3,196 whose publishers state no date, keep
  saying so.

### One gap left open, on purpose

`STATED_DATE_FIELDS` — which drives `observations.effective_at` (migration 0031) —
still does **not** list `$.data.vendor_released_at`, so that generated column
reports the capture time for all **44,351** mifirm observations.
`product_firmware_releases.vendor_released_at` is unaffected; it reads every
declared spelling.

It is open because `effective_at` already mixes three different measurements in
one sortable column (a vendor release date, a bulletin publication date, a capture
time), the Explore observations tab sorts on it, and adding a fourth publisher's
real dates changes that ordering for 46% of the corpus. The honest repair is the
banding decision `devices_page` already had to make for `latest_desc`, which is a
judgement and not a rename. `check_corpus` reports it as
`source_stated_release_date_not_read_by_the_generated_column` (warning, 44,351) so
it is loud rather than silent — on the **deep** scan only, because it is a 222ms
96,319-row scan and `check_corpus(deep=False)` runs on every page load.

### Reported, not just fixed

| check | severity | live corpus |
|---|---|---|
| `firmware_order_decided_by_row_id_not_a_date` | warning | **5,748** (5,643 + the 105 whose releases are gone) |
| `event_order_basis_not_recorded` | **error** | 0 — rows missing while the table exists can only mean a writer bypassed `firmware_order` |
| `event_order_basis_table_absent` | warning | 0 after 0034; 5,804 before it. A warning because the feed degrades to a null basis and the client prints "Order basis not recorded", which is true |
| `source_stated_release_date_not_read_by_the_generated_column` | warning (deep) | **44,351** |
| `headline_build_decided_by_publisher_name` | warning | **243** (181 before this change) |

That last one is a consequence, not a repair, and it is the user-visible part.
`current_firmware`'s device-level pick ranks by `latest_basis`, then the
publisher's `currency_rank`, then `source_id` — deliberately, so every date
comparison after it is between rows of one publisher. mifirm's rows used to be
`observation_order_only` and lost to the tracker's dated rows on basis alone; now
both are `vendor_release_date`, both publishers are rank 50, and `mifirm.…` sorts
before `xiaomi.…`. **39 Xiaomi devices' headline build changes publisher for that
reason, and for 7 of them the new headline carries an earlier date than the one it
replaced.** Three more change inside mifirm, from capture order to real dates,
which is the unambiguous repair. Deciding that one of these publishers outranks
the other is an authority judgement and was not made.
