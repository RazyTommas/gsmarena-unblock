BEGIN;

-- Why an automated identity rule reached the decision it reached.
--
-- source_identity_registry records the OUTCOME (resolution_state,
-- resolution_method, rule_version, confidence) and nothing about the basis. That
-- was survivable while every automated approval came from one exact match against
-- one artifact -- resolution_method named the artifact and that was the whole
-- story. It stops being survivable for a rule that combines two captured sources,
-- because "approved by rule X" is then not re-checkable: a reader cannot tell
-- WHICH device code corroborated the codename, which catalog variants agreed, or
-- -- for the ones it turned down -- why.
--
-- So the rationale travels with the decision, for refusals as well as approvals.
-- The refusals are the more useful half: they are the residue a human has to look
-- at, and the alternative to recording them is re-deriving them from a report that
-- ages out of date the next time the catalog is captured.
--
-- NOT a general audit log and deliberately not append-only: one CURRENT row per
-- (identity, rule), rewritten in place, so a run that reaches the same conclusion
-- twice changes nothing. Decision HISTORY for human decisions lives in
-- local.sqlite and is a different thing (see docs/IDENTITY_RESOLUTION.md).
--
-- product_id is stored WITHOUT a foreign key, on purpose. It records which product
-- the identity was judged against AT DECISION TIME; dedupe repoints
-- source_identity_registry.product_id at a surviving duplicate afterwards, and a
-- FK here would either have to be added to two hand-maintained CHILD_TABLES lists
-- (the kind of list dedupe.py's own docstring records going stale and orphaning
-- rows) or it would leave a dangling reference that PRAGMA foreign_key_check
-- reports as a corpus error. The identity_id FK does the real work: it CASCADEs,
-- so a decision cannot outlive the identity it was about.
CREATE TABLE identity_resolution_rationales (
  identity_id TEXT NOT NULL
    REFERENCES source_identity_registry(id) ON DELETE CASCADE,
  rule TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  product_id TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('approved', 'refused')),
  reason TEXT NOT NULL,
  rationale TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence_json)),
  decided_at TEXT NOT NULL,
  PRIMARY KEY (identity_id, rule)
) STRICT;

CREATE INDEX idx_identity_rationale_outcome
  ON identity_resolution_rationales(outcome, reason);
CREATE INDEX idx_identity_rationale_product
  ON identity_resolution_rationales(product_id);

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(29,'identity_resolution_rationale',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
