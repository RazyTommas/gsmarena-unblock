# Architecture

## Context

The canonical core is the stable center. Collectors submit immutable artifacts
and observations through contracts. Resolution promotes valid observations.
Applications and agents read a versioned API; they do not infer domain meaning
from raw tables.

```text
sources -> artifact store -> staging observations -> validation/quarantine
                                                   -> identity resolution
                                                   -> canonical core -> events
                                                                    -> API
                                            proposals -------------> review

API -> Radar | Explorer | Security | Admin | read-only agent tools
```

## Ownership boundaries

| Component | Owns | Must not do |
|---|---|---|
| Core | identities, canonical facts, evidence links, invariants | fetch sources or render UI |
| Collectors | retrieval and source-specific parsing | write canonical tables |
| Resolver | deterministic matching and review candidates | fuzzy auto-merge |
| Security engine | typed applicability/fix rules and verdicts | equate date vocabularies |
| Event projector | before/after comparison and append-only events | mutate history |
| API | stable query vocabulary and pagination | expose storage accidents as contract |
| Agent | queries, explanations, proposals | canonical writes or verdict invention |
| Apps | user workflows and local preferences | duplicate domain rules |

## Data flow and trust levels

1. Store response bytes and a SHA-256 digest before parsing.
2. A versioned parser emits source observations using a declared contract.
3. Validate required fields and run-level expectations. Invalid or suspicious
   batches remain queryable in quarantine.
4. Resolve exact identifiers and reviewed aliases. Ambiguous identities create
   review items.
5. Promote facts in a transaction and attach supporting evidence.
6. Compare canonical state and append idempotent domain events.

Trust order: canonical facts > accepted proposals > valid staged observations >
quarantined observations. Provenance quality is distinct from source priority.

## Identity rules

- UUIDs are opaque internal identity; source keys live in `external_ids`.
- Model codes are normalized only by a namespace-specific deterministic rule.
- An alias points to one typed entity. Reassignment is a reviewed correction.
- Family, variant, hardware model, firmware target, part, and revision never
  collapse into one record.
- Exact authoritative identifiers may auto-link. Fuzzy similarity only proposes.
- Corrections supersede facts; they do not erase source history.

## Time model

All timestamps are UTC ISO-8601 text (`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`). Release,
publication, collection, first-observed, valid-from/to, and evaluation times are
separate. Unknown times remain NULL; they are not synthesized.

## Offline model

`corpus.sqlite` is replaceable and read-only at runtime. `local.sqlite` holds
watches, acknowledgements, and saved searches. A signed/hashed manifest declares
schema version, generation time, source cutoffs, record counts, and file hashes.
Replacing a corpus never replaces local state.

## Change control

Core contract changes require an ADR, migration, invariant tests, glossary/API
updates, and explicit review. Collector or UI work cannot alter core semantics.
See ADRs in `docs/adr`.
