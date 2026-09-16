# Identity resolution and agent memory

Device identity is resolved as a review workflow, never as an automatic fuzzy
merge. A source value (name, model code, codename, or regional label) produces
ranked canonical candidates with evidence. The user may decide `same`,
`different`, or `defer`.

Decisions live in `local.sqlite.identity_decisions`, separate from the replaceable
corpus. They therefore survive snapshot upgrades. Both the Admin resolver and a
future local agent must read this table before proposing a match:

- `same` suppresses repeat questions and may create a human-confirmed promotion.
- `different` is negative evidence and prevents the same bad suggestion.
- `defer` preserves the candidate without changing canonical facts.

The agent receives narrow tools: `search_identity_candidates`,
`list_identity_decisions`, and `propose_identity_match`. It may enumerate source
records and open the same confirmation UI used manually. It cannot write aliases,
external IDs, canonical devices, or security verdicts. Only an explicit user
decision or a deterministic authoritative identifier promotes a relationship.

Every promotion records the source value, canonical target, decision author,
timestamp, and evidence. Corrections supersede earlier decisions; history is not
deleted.
