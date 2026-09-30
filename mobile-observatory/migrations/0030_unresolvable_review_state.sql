-- A terminal, honest resting state for a product no reviewer can resolve.
--
-- 626 source products sit at review_state 'proposed' after
-- automate_identity_review has run to its fixed point (1,739 of the 1,741
-- approvals are auto_approved, so the residue is not a backlog of un-run work).
-- Their recorded conclusions split two ways:
--
--   465  insufficient_evidence / no_independent_identifier -- there is no
--        independent identifier to match on AT ALL. All 66 Apple products are
--        here: integrity.review_queue's own docstring records why, which is that
--        the three corroborating captures (Xiaomi's devices.yml, the GSMArena
--        specs, the Google Play supported-device list) carry no Apple.
--   161  ambiguous -- evidence exists and names MORE THAN ONE candidate
--        (82 ranked_candidates, 63 google_play_model_code_multiple_names,
--        15 google_play_name_multiple_models, 1 model code naming several devices).
--
-- 'proposed' means "waiting for a reviewer". For these it is a false promise: a
-- human reviewer brings nothing to a missing identifier that an agent does not,
-- so the 20,955 observation links behind them presented as ~21,000 queue items
-- that no amount of reviewing could ever clear.
--
-- WHY A FOURTH STATE AND NOT ONE OF THE THREE.
--   'approved' would assert the source identity belongs to the product. It does
--     not; that is precisely what could not be established.
--   'rejected' would assert the identity was judged WRONG, and would also
--     withdraw the product's hardware claim (server._apply_product_review) and
--     block the automated rules forever (enrichment._conclude's `blocked`). The
--     identity was not judged wrong. It was not judgeable.
--   'proposed' is what we are leaving, because it names work nobody can do.
--
-- The name carries its own qualifier. "unresolvable" alone reads as "can never be
-- resolved", which is a claim about the future that no captured evidence supports;
-- `unresolvable_on_captured_evidence` says what was actually measured and names
-- the thing that can change. It is a resting state, not a closed one: when a
-- capture later supplies the identifier,
-- adjudication.reopen_stale_adjudications puts the product back to 'proposed' and
-- the ordinary rules decide again. An approval, by contrast, is final by design.
--
-- NOTHING IS HIDDEN OR DELETED. These products keep serving from the evidence
-- layer exactly as before: no observation, link, conclusion or firmware row is
-- touched, and source_identity_registry.resolution_state is deliberately left
-- alone -- writing 'rejected' there is what would make the state irreversible.
--
-- The CHECK has to be replaced, and SQLite cannot alter one in place, so
-- source_products is rebuilt. PRAGMA foreign_keys is OFF across the rebuild for
-- the reason the SQLite manual gives: with it ON, `ALTER TABLE ... RENAME TO`
-- rewrites the REFERENCES clauses of the nine child tables to point at the
-- temporary name. With it OFF the children keep naming `source_products`, which
-- after the rename is the table we want them to name. Enforcement being off for
-- the duration is exactly why the last statement before COMMIT asserts
-- pragma_foreign_key_check is empty: a CHECK on a temp table is the only way a
-- .sql migration can refuse to commit, and without it this rebuild would be a
-- nine-child FK edit whose only proof was reasoning.
PRAGMA foreign_keys=OFF;
BEGIN;

CREATE TABLE source_products_rebuilt (
  id TEXT PRIMARY KEY,
  manufacturer TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  review_state TEXT NOT NULL CHECK(review_state IN (
    -- Nobody has looked yet. Clearable by a reviewer.
    'proposed',
    -- The captured source identity belongs to this product. Final by design.
    'approved',
    -- A reviewer judged the identity wrong. Withdraws the product's claims.
    'rejected',
    -- Looked at, and the captured evidence cannot resolve it: either no
    -- independent identifier exists at all, or several candidates do and none
    -- discriminates. Terminal but reopenable -- see adjudication.py.
    'unresolvable_on_captured_evidence')) DEFAULT 'proposed',
  specification_json TEXT CHECK(specification_json IS NULL OR json_valid(specification_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(manufacturer, normalized_name)
) STRICT;

INSERT INTO source_products_rebuilt
  (id,manufacturer,canonical_name,normalized_name,review_state,
   specification_json,created_at,updated_at)
SELECT id,manufacturer,canonical_name,normalized_name,review_state,
       specification_json,created_at,updated_at FROM source_products;

DROP TABLE source_products;
ALTER TABLE source_products_rebuilt RENAME TO source_products;
CREATE INDEX source_products_review_idx
  ON source_products(review_state, manufacturer, canonical_name);

-- identity_resolution_rationales is where the basis for every automated identity
-- decision already lives (migration 0029), so the adjudication records itself
-- there rather than in a second provenance store. Its outcome vocabulary has to
-- grow: 'approved' and 'refused' are the stem rule's two answers, and neither
-- says "adjudicated: no reviewer can resolve this". The two new values are what
-- makes an agent adjudication distinguishable LATER from a human decision (which
-- lives in local.sqlite.identity_decisions and shows up as
-- source_identity_registry.resolution_method='manual_product_review') and from a
-- source having proved the identity ('approved' by a named rule).
--
-- identity_id becomes NULLABLE, which is the second change, and the reason is a
-- measured hole rather than a generalisation: two of the 626 products (Xiaomi
-- "Redmi 1" and itel "ACE2N") carry NO row in source_identity_registry at all, so
-- an identity-keyed table has nothing to hang their basis on. They are the most
-- clear-cut unresolvable products in the corpus -- not one captured identifier
-- between them -- and leaving exactly those two with an unrecorded terminal state
-- would put the hole in the worst possible place.
--
-- A NULL identity_id means "this decision is about the PRODUCT, not about one of
-- its identities". That has to be spelled in an index rather than a primary key,
-- because a UNIQUE index treats NULLs as distinct and INSERT OR REPLACE would then
-- append a row per run instead of rewriting one. So the uniqueness key is
-- ifnull(identity_id, 'product:'||product_id) -- which for an identity row is
-- byte-identical to the (identity_id, rule) primary key it replaces, so the stem
-- rule's idempotency is unchanged, and for a product row is one row per product.
--
-- Deliberately NOT widened to include product_id: dedupe repoints
-- source_identity_registry.product_id at a surviving duplicate, and a key
-- containing product_id would make INSERT OR REPLACE add a second row for the same
-- identity instead of rewriting the first.
--
-- No FK children and 58 rows, so the rebuild itself is unremarkable.
CREATE TABLE identity_resolution_rationales_rebuilt (
  -- NULL = the decision is about the product as a whole, because the product has no
  -- registry identity to key it on. Never NULL for a rule that judges an identity.
  identity_id TEXT
    REFERENCES source_identity_registry(id) ON DELETE CASCADE,
  rule TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  product_id TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN (
    'approved', 'refused', 'adjudicated_unresolvable', 'reopened')),
  reason TEXT NOT NULL,
  rationale TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence_json)),
  decided_at TEXT NOT NULL
) STRICT;

INSERT INTO identity_resolution_rationales_rebuilt
SELECT identity_id,rule,rule_version,product_id,outcome,reason,rationale,
       evidence_json,decided_at FROM identity_resolution_rationales;

DROP TABLE identity_resolution_rationales;
ALTER TABLE identity_resolution_rationales_rebuilt
  RENAME TO identity_resolution_rationales;
CREATE UNIQUE INDEX identity_rationale_subject_idx
  ON identity_resolution_rationales(ifnull(identity_id,'product:'||product_id), rule);
CREATE INDEX idx_identity_rationale_outcome
  ON identity_resolution_rationales(outcome, reason);
CREATE INDEX idx_identity_rationale_product
  ON identity_resolution_rationales(product_id);

-- Refuse to commit a rebuild that orphaned a child row. The CHECK fails, the
-- INSERT raises, executescript propagates it and apply_migrations leaves the
-- corpus on the previous schema with this version unrecorded.
CREATE TEMP TABLE _migration_0030_fk_assert (orphans INTEGER CHECK (orphans = 0));
INSERT INTO _migration_0030_fk_assert
SELECT count(*) FROM pragma_foreign_key_check;
DROP TABLE _migration_0030_fk_assert;

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(30,'unresolvable_review_state',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
PRAGMA foreign_keys=ON;
