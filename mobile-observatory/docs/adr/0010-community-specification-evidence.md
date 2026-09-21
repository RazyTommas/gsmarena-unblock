# ADR 0010: community specification assertions remain separate from hardware facts

Status: implemented, 2026-09-17.

LineageOS publishes explicit device/chip/model lists. They can expand product
exploration, but are community assertions and cannot establish stock firmware,
OEM support or security applicability. Converting them directly into canonical
hardware-silicon rows would improperly increase confidence.

Migration 0010 adds an append-only source specification read model referencing
preserved source artifacts and evidence. Only deterministic unique identities can
populate the existing product-level silicon read model. Exact hardware model-list
intersections are returned as separate evidence, never promoted relationships.
Ambiguity, conflicting chipset strings and remembered blocked reviews remain
queryable source assertions without rewriting canonical facts. No core identity,
firmware, support or security semantics change. Tests cover scoped variants,
source hash validation, idempotence, conflicts, reviews and exact model boundaries.
