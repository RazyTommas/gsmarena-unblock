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
