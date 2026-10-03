"""Give a product nobody can resolve a terminal state, and keep it reopenable.

THE PROBLEM. After `automate_identity_review` reaches its fixed point, 626 of
2,367 source products are still `review_state='proposed'` -- which the UI's own
tooltip translates as "belonging to products nobody has reviewed". That is not
what they are. Every one of them HAS been concluded, and the conclusion says the
captured evidence cannot settle the identity:

    465  insufficient_evidence / no_independent_identifier
    161  ambiguous  (82 ranked_candidates, 63 google_play_model_code_multiple_names,
                     15 google_play_name_multiple_models, 1 model code naming
                     several devices)

`proposed` therefore presented 20,955 observation links as a review queue no
reviewer could ever clear: Xiaomi 14,996, Apple 4,450, TECNO 846, itel 573,
Infinix 90. A queue item nobody can action is not pending work; it is a false
promise of future work, and the honest thing is to say so.

WHAT THIS DOES NOT DO. It does not approve anything, does not assert an identity,
does not invent a model code, does not create a hardware model, and does not touch
`source_identity_registry.resolution_state`, `observation_product_links`,
`identity_conclusions` or any firmware row. The products go on serving from the
evidence layer exactly as before. The only change is `source_products.review_state`
and a recorded basis for changing it.

THE TWO SUB-POPULATIONS ARE NOT COLLAPSED. "No independent identifier exists" and
"several candidates exist and none discriminates" are different claims about the
world, and only the first means the evidence is silent. They stay distinguishable
in three places: `identity_conclusions.conclusion`/`method` (untouched, which is
why this module does not need a second review_state per population),
`identity_resolution_rationales.reason` (`no_independent_identifier` against
`several_candidates_none_discriminating`), and `integrity.review_queue`, which
reports them as separate per-vendor columns.

REVERSIBILITY IS THE POINT. An approval is final by design; this is not, because
the claim it records is explicitly relative to what has been captured so far -- the
name says `unresolvable_on_captured_evidence` and not `unresolvable`. Two paths
reopen one:

  * Automatically. The adjudication stores a fingerprint of the basis it decided
    on: the product's registry identities, and its recorded conclusion. When a
    later capture changes either -- a new source value arrives, or the conclusion
    is re-derived under a new RULE_VERSION -- `reopen_stale_adjudications` puts the
    product back to 'proposed' and drops the frozen conclusion so the ordinary
    rules decide again from scratch.

    Dropping a remembered conclusion needs a word, because remembered conclusions
    are final by design and that rule matters (it is what stops the corpus
    depending on WHEN it last ran). This does not reintroduce that dependence: the
    trigger is a measured change in the corpus, never the clock, so a batch run
    twice over unchanged inputs reopens nothing. And it only ever fires for a
    product the corpus has already declared unresolvable -- a conclusion whose own
    evidence set no longer describes the product it was about is not a decision
    worth preserving. HANDOFF.md records the cost of the opposite choice: 8,332
    Xiaomi observations held forever because identities arrived after their
    product was concluded and a remembered conclusion was never reopened.

  * By hand. `POST /api/v1/identity/products/{id}/review` with `proposed` is the
    existing human "defer", and it returns the product to the queue. That path is
    deliberately NOT able to SET this state: a human deciding "no evidence exists"
    is a human decision and must not be recorded as an agent adjudication.

PROVENANCE, in ONE store. Every transition is recorded in
`identity_resolution_rationales`, the table the stem rule added -- not in a second
store beside it. That table is keyed on `identity_id`, which is the right grain for
"on what basis", because the basis IS the product's identities. Two of the 626
products (Xiaomi "Redmi 1" and itel "ACE2N") carry NO registry identity at all, so
migration 0030 made `identity_id` nullable: a NULL means the decision is about the
product as a whole, and those two are the most clear-cut unresolvable products in
the corpus -- not one captured identifier between them -- so an unrecorded terminal
state there would be the hole in the worst possible place. They are still counted
separately (`review_queue.unresolvable_without_identity`) because a basis that could
not be keyed on an identity is a different kind of record and a reader should be able
to see how many there are.

`source_data_corrections`, which the first draft of this used for those two, is the
WRONG store and says so itself: it carries BEFORE UPDATE and BEFORE DELETE triggers
raising 'source corrections are immutable'. An append-only log cannot hold a
fingerprint that has to be rewritten every time a product is reopened and
re-adjudicated, and using it would have made those two products either unreopenable
or reopened on every single batch forever.

WHO decided is legible three ways, which is what makes an agent adjudication
distinguishable later from a human decision and from a source having proved the
identity: `outcome='adjudicated_unresolvable'` (no other writer uses it),
`rule='unresolvable_on_captured_evidence'` with its version, and
`evidence_json.decided_by`. A human decision lives in
`local.sqlite.identity_decisions` and shows up as
`source_identity_registry.resolution_method='manual_product_review'`; a source
having proved it is `outcome='approved'` under a named rule.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from .identity_rationales import record_rationale

# The state itself. Also the rule name, because the rule has exactly one outcome
# and naming them differently would invite the two to drift.
UNRESOLVABLE = "unresolvable_on_captured_evidence"
ADJUDICATION_RULE = UNRESOLVABLE
# Bumped when the rule changes WHICH products it would adjudicate, or what it
# records. Part of the basis fingerprint, so a bump reopens every adjudication and
# re-decides it under the new rule rather than leaving yesterday's answers behind.
ADJUDICATION_RULE_VERSION = "1"
DECIDED_BY = "agent:unresolvable_adjudication"

# The conclusions this rule will close, and what it calls each. Anything else --
# including a product with NO conclusion, which means the rules have not run on it
# yet -- is left alone: adjudicating something nobody has looked at is the exact
# dishonesty this module exists to remove.
_REASONS = {
    "insufficient_evidence": "no_independent_identifier",
    "ambiguous": "several_candidates_none_discriminating",
}

_RATIONALE = {
    "no_independent_identifier": (
        "Adjudicated: no independent identifier exists for this product in any captured "
        "source, so there is nothing for a reviewer to match on and no reviewer can "
        "resolve it from available evidence. No identity is asserted and none is "
        "rejected; the product keeps serving from the evidence layer. Reopens "
        "automatically if a capture later supplies an identifier."),
    "several_candidates_none_discriminating": (
        "Adjudicated: captured evidence names more than one candidate for this product "
        "and nothing in it discriminates between them, so no reviewer can resolve it "
        "from available evidence without inventing the corroboration. The candidates are "
        "recorded and kept separate rather than merged. No identity is asserted and none "
        "is rejected; the product keeps serving from the evidence layer. Reopens "
        "automatically if a capture later discriminates."),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _identities(connection: sqlite3.Connection, product_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT id, source_id, namespace, source_value, resolution_state, resolution_method"
        "  FROM source_identity_registry WHERE product_id=? ORDER BY namespace, source_value, id",
        (product_id,)).fetchall()


def _basis(conclusion: sqlite3.Row, identities: list[sqlite3.Row]) -> dict:
    """Everything the adjudication rests on, as a JSON-able record.

    Both the evidence stored beside the decision AND the thing the fingerprint is
    taken over, deliberately the same object: a fingerprint over a subset of the
    recorded basis could report "unchanged" about evidence that had changed.
    """
    return {
        "decided_by": DECIDED_BY,
        "rule": ADJUDICATION_RULE,
        "rule_version": ADJUDICATION_RULE_VERSION,
        "conclusion": {
            "conclusion": conclusion["conclusion"],
            "method": conclusion["method"],
            "confidence": conclusion["confidence"],
            "rule_version": conclusion["rule_version"],
            "candidates": json.loads(conclusion["candidates_json"] or "[]"),
        },
        "identities": [
            {"source_id": row["source_id"], "namespace": row["namespace"],
             "source_value": row["source_value"]}
            for row in identities],
    }


def basis_fingerprint(basis: dict) -> str:
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode("utf-8")).hexdigest()


def _recorded_fingerprint(connection: sqlite3.Connection, product_id: str,
                          has_identity: bool) -> str | None:
    """The fingerprint the last adjudication of this product recorded, if any.

    A product whose rationale rows disagree with each other has no single recorded
    basis, so this returns None and the product reopens -- which is the safe answer:
    reopening costs one pass of the ordinary rules, while trusting a torn record
    would leave a terminal state whose stated basis is not the corpus's.
    """
    if has_identity:
        rows = connection.execute(
            """SELECT DISTINCT json_extract(r.evidence_json,'$.basis_fingerprint') fp
                 FROM identity_resolution_rationales r
                 JOIN source_identity_registry sir ON sir.id = r.identity_id
                WHERE sir.product_id=? AND r.rule=? AND r.outcome='adjudicated_unresolvable'""",
            (product_id, ADJUDICATION_RULE)).fetchall()
    else:
        rows = connection.execute(
            """SELECT json_extract(evidence_json,'$.basis_fingerprint') fp
                 FROM identity_resolution_rationales
                WHERE identity_id IS NULL AND product_id=? AND rule=?
                  AND outcome='adjudicated_unresolvable'""",
            (product_id, ADJUDICATION_RULE)).fetchall()
    fingerprints = {row["fp"] for row in rows}
    if len(fingerprints) != 1:
        return None
    return fingerprints.pop()


def _record(connection: sqlite3.Connection, identity_id: str | None, product_id: str,
            outcome: str, reason: str, rationale: str, evidence: str, now: str) -> None:
    """This rule's provenance rows, written through the table's only writer.

    INSERT OR REPLACE against `PRIMARY KEY (subject, rule)` -- migration 0033's
    materialisation of the unique expression index 0030 created -- so the current
    row for a subject is rewritten in place rather than appended to, matching what
    migration 0029 says the table is for. A NULL identity_id keys on
    `product:<id>`; see the module docstring for the two products that need it.

    The INSERT itself lives in identity_rationales.record_rationale and not here:
    it was previously spelled byte-identically in identity_backfill's stem rule,
    and the `subject` key would otherwise be a column two call sites derive
    separately.
    """
    record_rationale(
        connection, identity_id=identity_id, rule=ADJUDICATION_RULE,
        rule_version=ADJUDICATION_RULE_VERSION, product_id=product_id,
        outcome=outcome, reason=reason, rationale=rationale,
        evidence_json=evidence, decided_at=now)


def _human_has_spoken(product_id: str, identities: list[sqlite3.Row],
                      decisions: list[dict] | None) -> bool:
    """Whether a person has explicitly put this product back in the queue.

    Mirrors `enrichment._conclude`'s `blocked`, on purpose: an automated rule must
    not overrule a human, and this one had a specific way of doing it. Clicking
    Reopen calls `server.review_source_product(id, 'proposed')`, which writes a
    'defer' into local.sqlite and stamps the identities
    `resolution_method='manual_product_review'`. Without this test the next nightly
    batch would find a 'proposed' product with a terminal conclusion and close it
    again -- the reviewer's decision silently reverted within a day, which is
    exactly the "recorded and not acted on" failure the review endpoint's own
    republish comment exists to prevent.

    Both signals are read because the registry one cannot cover the two products
    that have no registry row at all.
    """
    if any(row["resolution_method"] == "manual_product_review" for row in identities):
        return True
    return any(decision.get("decision") in ("different", "defer")
               and decision.get("canonical_id") == product_id
               for decision in (decisions or []))


def adjudicate_unresolvable_products(connection: sqlite3.Connection, *,
                                     decisions: list[dict] | None = None) -> dict[str, int]:
    """Close every `proposed` product whose recorded conclusion cannot be resolved.

    Idempotent: a second run over an unchanged corpus finds nothing `proposed` and
    writes nothing, and re-running after a reopen re-decides from the current basis.

    `decisions` is local.sqlite's `identity_decisions`, the same list
    `automate_identity_review` takes, and is what stops this rule closing a product
    a person has just reopened.
    """
    totals: dict[str, int] = defaultdict(int)
    now = _now()
    rows = connection.execute(
        """SELECT sp.id, sp.manufacturer, sp.canonical_name,
                  ic.conclusion, ic.method, ic.confidence, ic.rule_version, ic.candidates_json
             FROM source_products sp
             LEFT JOIN identity_conclusions ic ON ic.product_id = sp.id
            WHERE sp.review_state='proposed'
            ORDER BY sp.manufacturer, sp.canonical_name""").fetchall()
    with connection:
        for row in rows:
            totals["examined"] += 1
            if row["conclusion"] is None:
                # Nobody has looked. The rules have not run on this product at all,
                # so there is no basis on which to call it unresolvable.
                totals["skipped_not_concluded"] += 1
                continue
            reason = _REASONS.get(row["conclusion"])
            if reason is None:
                # 'auto_approved' with review_state 'proposed' is a state the
                # pipeline does not produce (approval writes both), so reaching
                # here means something upstream changed and this rule should say
                # nothing rather than guess.
                totals["skipped_conclusion_not_terminal"] += 1
                continue
            identities = _identities(connection, row["id"])
            if _human_has_spoken(row["id"], identities, decisions):
                totals["skipped_human_decision"] += 1
                continue
            basis = _basis(row, identities)
            # Taken BEFORE the key is added, so the stored fingerprint is a
            # fingerprint of the basis and not of itself.
            basis["basis_fingerprint"] = basis_fingerprint(basis)
            evidence = json.dumps(basis, sort_keys=True)
            connection.execute(
                "UPDATE source_products SET review_state=?, updated_at=? WHERE id=?",
                (UNRESOLVABLE, now, row["id"]))
            # One row per identity, or -- when the product has none -- one
            # product-scoped row with identity_id NULL. Same table, same rule, same
            # outcome vocabulary, so nothing has to choose between two stores.
            for identity_id in ([i["id"] for i in identities] or [None]):
                _record(connection, identity_id, row["id"], "adjudicated_unresolvable",
                        reason, _RATIONALE[reason], evidence, now)
            totals["rationales_recorded"] += max(len(identities), 1)
            if not identities:
                totals["adjudicated_without_identity"] += 1
            totals["adjudicated"] += 1
            totals["adjudicated_" + reason] += 1
    return dict(totals)


def reopen_stale_adjudications(connection: sqlite3.Connection) -> dict[str, int]:
    """Return an adjudicated product to the queue when its basis has changed.

    Runs BEFORE the identity rules in the batch, so anything it reopens is decided
    again in the same run: nothing spends a night in 'proposed' waiting for
    tomorrow's pass.
    """
    totals: dict[str, int] = defaultdict(int)
    now = _now()
    rows = connection.execute(
        """SELECT sp.id, sp.manufacturer, sp.canonical_name,
                  ic.conclusion, ic.method, ic.confidence, ic.rule_version, ic.candidates_json
             FROM source_products sp
             LEFT JOIN identity_conclusions ic ON ic.product_id = sp.id
            WHERE sp.review_state=?
            ORDER BY sp.manufacturer, sp.canonical_name""", (UNRESOLVABLE,)).fetchall()
    with connection:
        for row in rows:
            totals["examined"] += 1
            identities = _identities(connection, row["id"])
            recorded = _recorded_fingerprint(connection, row["id"], bool(identities))
            current = (basis_fingerprint(_basis(row, identities))
                       if row["conclusion"] is not None else None)
            if recorded is not None and recorded == current:
                totals["unchanged"] += 1
                continue
            connection.execute(
                "UPDATE source_products SET review_state='proposed', updated_at=? WHERE id=?",
                (now, row["id"]))
            # The frozen conclusion goes with it: the evidence it was derived from
            # is not the evidence the product now has, so `_conclude` must decide
            # again rather than return the remembered answer. See the docstring.
            connection.execute("DELETE FROM identity_conclusions WHERE product_id=?", (row["id"],))
            reopened = json.dumps({
                "decided_by": DECIDED_BY, "rule": ADJUDICATION_RULE,
                "rule_version": ADJUDICATION_RULE_VERSION,
                "recorded_basis_fingerprint": recorded,
                "current_basis_fingerprint": current,
                "identities": [{"source_id": i["source_id"], "namespace": i["namespace"],
                                "source_value": i["source_value"]} for i in identities],
            }, sort_keys=True)
            for identity_id in ([i["id"] for i in identities] or [None]):
                _record(connection, identity_id, row["id"], "reopened",
                        "captured_basis_changed",
                        "The captured basis this product was adjudicated on is no longer "
                        "the basis the corpus holds, so the adjudication is withdrawn and "
                        "the ordinary identity rules decide again.",
                        reopened, now)
            totals["reopened"] += 1
    return dict(totals)
