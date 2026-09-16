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

## Agent response inbox (local, proposal-only)

Admin can import the response to **Copy complete agent assignment** as a JSON
array. Every item has exactly `product_id`, `decision` (`same/different/defer`),
`canonical_name`, `model_codes` (array), `aliases` (array), `confidence`
(`low/medium/high`), `evidence` (nonempty array), and `rationale`.

Every evidence entry contains `note` and one or more of `url` (HTTP/HTTPS),
`artifact_id`, or `observation_id`. Captured IDs and product IDs must already
exist; URLs remain uncaptured external references. Import validates the entire
batch before writing, rejects unknown fields, accepts at most 100 proposals / 1
MB, and deduplicates identical content. An agent cannot claim authoritative
confidence. Validation checks structure and reference existence, not truth of an
external page or the agent's reasoning.

Human review requires a rationale. **Accept proposal**, **Reject**, and **Defer**
record a local assessment and append an immutable review event. None promotes a
model code, alias, chipset, firmware record, or security claim. Existing explicit
product review remains a separate workflow. Repeated suggestions for the same
product/name/model-code target inherit its remembered outcome, even when the
rationale changes; an explicit review can correct that outcome with history.
Rejected targets remain negative evidence for alternative research; accepted and
deferred product assignments are withheld from the next agent handoff.

- `GET/POST /api/v1/identity/agent-proposals`
- `POST /api/v1/identity/agent-proposals/{id}/review`
- `GET /api/v1/identity/history`

Manual identity decisions also retain append-only local history. Startup imports
the last surviving legacy decisions into that history; earlier overwritten
legacy decisions cannot be reconstructed. The current-decision projection uses
NULL-safe keys, so repeated targetless decisions no longer create duplicate rows.

Explicit source-product approve/reject/defer actions now record durable local
identity memory before applying the source-product review projection. On startup,
those exact product decisions are reapplied when the product still exists in a
replacement snapshot. Startup never creates a missing product or hardware model,
and never promotes an accepted agent proposal. Existing review history is not
re-appended during recovery. The manual model resolver displays a remembered
outcome and requires opening its review control before asking the same question.
