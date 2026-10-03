-- The last table a changeset revert could not see, given a PRIMARY KEY.
--
-- The session extension identifies a row by its declared PRIMARY KEY. A table
-- without one is not recorded AND NOT COMPLAINED ABOUT: its changes are simply
-- absent from the diff, so a `tools/corpus_changeset.py revert` would roll a
-- batch back everywhere except here -- and here is where every automated
-- identity conclusion records WHY it was reached. 721 rows on the live corpus
-- (663 unresolvable_on_captured_evidence, 58
-- xiaomi_codename_stem_google_play_corroborated), 2 of them product-scoped.
-- `check_corpus` has been reporting the hole as `table_absent_from_every_changeset`
-- (warning, 1) since changesets were introduced; this closes it.
--
-- WHY A STORED `subject` COLUMN AND NOT ONE OF THE OBVIOUS KEYS.
-- Migration 0030 did not leave this table keyless by oversight -- it explains at
-- length why its uniqueness is an EXPRESSION index,
-- `ifnull(identity_id,'product:'||product_id)`, and every one of those reasons
-- still holds. identity_id must stay nullable (Xiaomi "Redmi 1" and itel "ACE2N"
-- carry no row in source_identity_registry at all, so their terminal state has no
-- identity to hang on), a UNIQUE index treats NULLs as distinct, and
-- INSERT OR REPLACE would therefore append a row per run instead of rewriting
-- one. Four shapes were tried against this box's SQLite 3.45.1 before this one:
--
--   PRIMARY KEY (ifnull(identity_id,'product:'||product_id), rule)
--       -> "expressions prohibited in PRIMARY KEY and UNIQUE constraints"
--   subject GENERATED ALWAYS AS (...) STORED, PRIMARY KEY (subject, rule)
--       -> "generated columns cannot be part of the PRIMARY KEY"
--   PRIMARY KEY (identity_id, rule)
--       -> accepted, then fails at INSERT time: STRICT makes a PK column
--          implicitly NOT NULL, so the two product-scoped rows cannot be stored.
--          A key that refuses exactly the rows 0030 was written for.
--   id INTEGER PRIMARY KEY
--       -> accepted and tracked, but INSERT OR REPLACE mints a new rowid every
--          run, so `SELECT *` moves on a run that changed nothing. That is the
--          churn tests/test_identity_stem_rule.py::test_running_it_twice_changes_nothing
--          exists to catch, and a surrogate key would hide it inside the revert
--          machinery instead.
--
-- So the expression is MATERIALISED into an ordinary stored column and the column
-- is the key. `subject` goes LAST so no positional column index moves. The
-- derivation is enforced by the DATABASE, not by the writer, because a key whose
-- value a future writer can get wrong is a key that silently stops being the
-- subject: CHECK (subject = ifnull(identity_id,'product:'||product_id)) refuses a
-- row whose subject does not derive from the row it is on. src/mobile_observatory/
-- identity_rationales.py is the single writer and derives it the same way;
-- tests/test_rationale_changeset_coverage.py fails if a second writer appears.
--
-- WHY identity_rationale_subject_idx IS NOT RECREATED.
-- PRIMARY KEY (subject, rule) on a rowid table IS a UNIQUE index --
-- sqlite_autoindex_identity_resolution_rationales_1, over exactly (subject,
-- rule). With subject pinned by the CHECK above to
-- ifnull(identity_id,'product:'||product_id), that autoindex enforces
-- byte-identically what the old expression index enforced; the collision set is
-- the same set, which this migration measures rather than asserts (the
-- INSERT..SELECT below would itself raise if any two of the 721 rows collided
-- under the new key and did not under the old). Keeping the named index would be
-- the same constraint twice: two B-trees maintained per write, and -- worse -- a
-- revert that collides would be reported through the redundant index as
-- "CONSTRAINT (a constraint other than the primary key)" instead of "CONFLICT (an
-- insert collides on the PRIMARY KEY)", which is precisely the wrong-label-on-an-
-- honest-error that changesets.CONFLICT_NAMES carries a comment about. Nothing
-- reads the index by name: `grep -r identity_rationale_subject_idx` over the whole
-- repository finds it only in migration 0030. The other two indexes are plain
-- lookup indexes and ARE recreated unchanged.
--
-- The rebuild follows 0030's pattern exactly, including PRAGMA foreign_keys=OFF
-- for the reason the SQLite manual gives (with it ON, ALTER TABLE ... RENAME TO
-- rewrites referencing clauses to point at the temporary name). This table has no
-- FK children, but it IS a child of source_identity_registry, so the post-rename
-- assertion that pragma_foreign_key_check is empty is what stops a rebuild that
-- lost the REFERENCES clause from committing: a CHECK on a temp table is the only
-- way a .sql migration can refuse to commit.
PRAGMA foreign_keys=OFF;
BEGIN;

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
  decided_at TEXT NOT NULL,
  -- The materialised subject of the decision: the identity it judges, or
  -- 'product:<id>' when the product has no identity to judge. Last in the column
  -- order so nothing positional moved.
  subject TEXT NOT NULL,
  CHECK (subject = ifnull(identity_id,'product:'||product_id)),
  PRIMARY KEY (subject, rule)
) STRICT;

INSERT INTO identity_resolution_rationales_rebuilt
  (identity_id,rule,rule_version,product_id,outcome,reason,rationale,
   evidence_json,decided_at,subject)
SELECT identity_id,rule,rule_version,product_id,outcome,reason,rationale,
       evidence_json,decided_at,
       ifnull(identity_id,'product:'||product_id)
  FROM identity_resolution_rationales;

-- Before the original is dropped, while both still exist: refuse to continue if
-- the new key merged or lost a row. The UNIQUE enforcement above would have
-- raised on a collision, so this is the other direction -- a backfill that
-- dropped rows -- and it is the only moment at which the two counts can be
-- compared at all.
CREATE TEMP TABLE _migration_0033_rowcount_assert (
  rows_lost INTEGER CHECK (rows_lost = 0));
INSERT INTO _migration_0033_rowcount_assert
SELECT (SELECT count(*) FROM identity_resolution_rationales)
       - (SELECT count(*) FROM identity_resolution_rationales_rebuilt);
DROP TABLE _migration_0033_rowcount_assert;

DROP TABLE identity_resolution_rationales;
ALTER TABLE identity_resolution_rationales_rebuilt
  RENAME TO identity_resolution_rationales;
CREATE INDEX idx_identity_rationale_outcome
  ON identity_resolution_rationales(outcome, reason);
CREATE INDEX idx_identity_rationale_product
  ON identity_resolution_rationales(product_id);

-- Two refusals, both on the rebuilt table under its final name.
--   orphans: enforcement was OFF across the rebuild, so this is the only proof
--     the REFERENCES clause survived the rename and points at real parents.
--   subjects_wrong: the CHECK makes this unfalsifiable today, which is the point
--     of measuring it separately -- it guards the BACKFILL expression, which
--     could be wrong independently of the constraint that will police future
--     writes.
CREATE TEMP TABLE _migration_0033_assert (
  orphans INTEGER CHECK (orphans = 0),
  subjects_wrong INTEGER CHECK (subjects_wrong = 0));
INSERT INTO _migration_0033_assert
SELECT (SELECT count(*) FROM pragma_foreign_key_check),
       (SELECT count(*) FROM identity_resolution_rationales
         WHERE subject IS NOT ifnull(identity_id,'product:'||product_id));
DROP TABLE _migration_0033_assert;

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(33,'rationale_primary_key',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
PRAGMA foreign_keys=ON;
