# Legacy import policy

The old repository is evidence and research material, not a trusted database.
No legacy table is copied wholesale and no legacy identifier becomes canonical
merely because it already exists.

## May be evaluated for reuse

- Captured raw source payloads whose origin and retrieval time are recoverable.
- Source URLs, access notes, and rate-limit knowledge.
- Parser fixtures and build-code decoders with reproducible tests.
- Bulletin/CVE documents and citations.
- Source-specific region or chipset vocabulary as unresolved observations.

## Rejected by default

- The legacy schema, API, GUI, and navigation.
- Free-text or comma-separated relationships.
- Normalized names without the original value and normalization rule.
- Fuzzy links, first-match joins, and GSMArena-derived master identity.
- Security verdicts lacking typed applicability, rule version, and evidence.
- Repair scripts as production workflows.

## Import procedure

1. Inventory candidate assets without modifying canonical data.
2. Record provenance and license constraints.
3. Convert raw facts into the collector observation contract.
4. Validate against fixtures and batch health thresholds.
5. Resolve authoritative IDs; queue ambiguity for review.
6. Promote transactionally with evidence and emit events.
7. Reconcile counts and retain an import manifest with hashes.

Imported data has no special precedence over newly collected evidence.
