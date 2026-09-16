# ADR 0014: distinguish source-declared latest firmware from observation order

Status: accepted for the read model, 2026-09-17.

## Problem

Captured Samsung history puts hundreds of older builds at the same observation
time. Clearing the incorrectly interpreted build-month release dates exposed an
existing flaw: the latest view chose an arbitrary release ID when dates tied.
That can misstate current firmware and Android state.

## Decision

The latest view first honors `manifest_position=latest` from the newest preserved
Samsung FOTA manifest observation per hardware/target/channel. Only the declared
Samsung parser/source contracts participate. An older manifest's latest marker
loses priority if newer manifest evidence is present. Next preference is the
latest known vendor release date. Observation order remains a navigation fallback
and is explicitly labeled `observation_order_only`, never a definitive software
state. No build identifiers are parsed into Android, patch levels, or chronology.

The API exposes `software_state_basis`. For observation-only fallback, current
Android/SPL stay unknown, and Android version filters exclude that unknown state.
Captured individual release facts remain queryable in full history. Device detail
separately exposes `latestFirmware` by region only when explicit source latest or
vendor-date evidence establishes an ordering. Snapshot age remains visible.

The devices query computes regional latest choices once, rather than evaluating
the latest view in a correlated subquery for each hardware model. Migration0014
changes a read view only; no canonical release, observation, or evidence is changed.

## Verification

Tests distinguish explicit latest from an arbitrary higher-sorting historical ID
and ensure a newer manifest invalidates a stale latest marker. On the corrected
real snapshot, all726 regional target rows select source-declared latest. The
83-device query completes in approximately0.50s on this machine. Firmware history
remains21186 rows across83 models. Source recency never proves installed firmware
or support policy.
