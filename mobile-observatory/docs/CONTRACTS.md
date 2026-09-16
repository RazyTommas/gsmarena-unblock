# Contracts and invariants

## Collector envelope

Every staged observation supplies: source, run, artifact digest, parser name and
version, source record type/key, observed time, unmodified JSON payload, and
validation state. A run records start/end/outcome plus counts. A collector may
only write ingestion tables.

Run validation can quarantine an entire batch for authentication pages, sharp
inventory contraction, implausible count growth, parser error rate, or missing
required partitions. Thresholds are source-specific configuration, not core
constants.

## Promotion

Promotion is transactional and idempotent by source namespace/key and content
digest. It records which evidence supports each resulting canonical assertion.
Unresolved or ambiguous references produce review items; they are never dropped.

## Security verdict contract

A verdict contains subject type/id, vulnerability, status, rule identifier and
version, structured input snapshot, evaluation time, and evidence. Allowed
statuses are `open`, `claimed_fixed`, `unknown`, `not_adjudicable`, and
`not_applicable`. “Claimed fixed” is intentionally not “safe.” Re-evaluation
adds a verdict and supersedes the previous one.

## Event contract

An event has a stable dedupe key, type, subject, occurrence time, recording time,
before/after JSON, and provenance. Replaying the same promotion cannot duplicate
an event. Corrective events reference the event they correct.

## Agent contract

Agent tools use allow-listed read models. Agent writes are limited to proposals
with rationale, candidate patch, citations, model/run metadata, and status.
Only deterministic validation or explicit human acceptance may promote them.
