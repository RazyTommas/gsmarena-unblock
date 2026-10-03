"""The one route by which a row reaches `identity_resolution_rationales`.

Two rules record their provenance in this table -- the Xiaomi codename-stem rule
(`identity_backfill.approve_stem_corroborated_identities`) and the unresolvable
adjudication (`adjudication.adjudicate_unresolvable_products`) -- and until
migration 0033 both spelled the same nine-column `INSERT OR REPLACE` by hand,
byte for byte. That is the shape this codebase has already paid for once: one bug
living at five call sites, fixed three times, each at whichever site the batch
reached first.

0033 gives the table a PRIMARY KEY so a changeset revert can see it, and the key
is a stored `subject` column holding `ifnull(identity_id,'product:'||product_id)`
-- the expression migration 0030's unique index was built on, materialised,
because SQLite permits neither an expression nor a generated column in a PRIMARY
KEY. A tenth column every writer must now derive identically is exactly the kind
of duplication that drifts, so there is one writer and it is this module. The
database enforces the derivation too (`CHECK (subject = ifnull(identity_id,
'product:'||product_id))`), so a hand-written INSERT that forgot it fails loudly
rather than storing a row under the wrong subject; and
`tests/test_rationale_changeset_coverage.py` scans the source so a second writer
cannot be added quietly.

No import of `identity_backfill` or `adjudication` here, in either direction's
interest: both import this, so an import back would be a cycle.
"""
from __future__ import annotations

import sqlite3

#: The columns a rationale row is written with, in the order the INSERT names
#: them. `subject` is last because 0033 appended it last.
COLUMNS = ("identity_id", "rule", "rule_version", "product_id", "outcome", "reason",
           "rationale", "evidence_json", "decided_at", "subject")


def subject_key(identity_id: str | None, product_id: str) -> str:
    """The subject of a decision: the identity judged, else the product.

    Mirrors SQL `ifnull(identity_id,'product:'||product_id)` exactly, which means
    it tests for NULL and nothing else -- an empty-string identity_id is a value,
    not an absence, and both this and `ifnull` keep it. The table's CHECK compares
    the two, so a divergence here is a refused write rather than a wrong row.
    """
    return identity_id if identity_id is not None else f"product:{product_id}"


def record_rationale(connection: sqlite3.Connection, *, identity_id: str | None,
                     rule: str, rule_version: str, product_id: str, outcome: str,
                     reason: str, rationale: str, evidence_json: str,
                     decided_at: str) -> None:
    """Write (or rewrite) the current rationale for one subject and rule.

    INSERT OR REPLACE against `PRIMARY KEY (subject, rule)`, so re-running a rule
    rewrites its row in place instead of appending one per run -- what migration
    0029 says the table is for, and what 0030's unique index was protecting before
    the key existed.

    `evidence_json` is a JSON string, not an object: both callers already hold one
    (the stem rule json.dumps its evidence list, the adjudication its basis) and
    the column carries a `json_valid` CHECK, so serialising here would mean
    guessing at a sort order the callers have already chosen.
    """
    connection.execute(
        "INSERT OR REPLACE INTO identity_resolution_rationales"
        " (identity_id,rule,rule_version,product_id,outcome,reason,rationale,"
        "  evidence_json,decided_at,subject) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (identity_id, rule, rule_version, product_id, outcome, reason, rationale,
         evidence_json, decided_at, subject_key(identity_id, product_id)))
