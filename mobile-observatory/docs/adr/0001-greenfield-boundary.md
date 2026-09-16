# ADR 0001: Greenfield boundary

Status: accepted

The Mobile Observatory schema, package, and API have no compatibility dependency
on the legacy crawler. Reuse is permitted only for source knowledge, raw fixtures,
and isolated logic after contract tests. This prevents old denormalized identity
and fuzzy-linking assumptions becoming architecture.
