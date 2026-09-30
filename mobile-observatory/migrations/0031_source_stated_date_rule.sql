BEGIN;

-- A non-date a source states where a date belongs is ABSENCE, not a value.
--
-- Six observations from xiaomi.community.firmware_tracker carry the four-character
-- STRING "null" in `$.data.release_date` (typeof = text; the only non-date value in
-- that field across all 96,319 observations). json_extract returned it faithfully,
-- coalesce treated it as present, and so the word `null` became the effective date
-- of six builds and reached the screen as one.
--
-- The payload is NOT touched. `payload_json` is the archive and stays byte-exact as
-- the source sent it; the source really did say that, and the check
-- `source_states_a_non_date_where_a_date_belongs` in integrity.py reports it. What
-- changes is the DERIVED column, which now reads a non-date as absence and falls
-- through to `observed_at` exactly as the 87,089 rows whose release_date is JSON
-- null already did.
--
-- The rule is a SHAPE test and not a sentinel list -- see
-- src/mobile_observatory/source_dates.py for why, and for the measurement showing
-- it discards those 6 rows and nothing else. Each CASE below is the literal output
-- of `source_dates.stated_date_sql(<the same json_extract>)`, kept on one line so
-- tests/test_source_stated_dates.py can assert the stored schema CONTAINS the
-- string that function generates. That is what stops this becoming a second copy
-- of the rule that drifts from the Python one.
--
-- DROP COLUMN + ADD COLUMN rather than a table rebuild: `observations` holds 96,319
-- rows and 102MB of payload, and a VIRTUAL generated column stores nothing, so
-- redefining it is a schema edit. The index must be dropped first (SQLite refuses
-- to drop a column an index names) and rebuilt after -- it is the index, not the
-- column, that materialises the values the sort reads.
DROP INDEX observations_effective_at_idx;
ALTER TABLE observations DROP COLUMN effective_at;
ALTER TABLE observations ADD COLUMN effective_at TEXT
  GENERATED ALWAYS AS (coalesce(
    CASE WHEN (json_extract(payload_json,'$.data.release_date')) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' THEN (json_extract(payload_json,'$.data.release_date')) END,
    CASE WHEN (json_extract(payload_json,'$.data.publish_date')) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' THEN (json_extract(payload_json,'$.data.publish_date')) END,
    CASE WHEN (json_extract(payload_json,'$.data.release_time')) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' THEN (json_extract(payload_json,'$.data.release_time')) END,
    observed_at)) VIRTUAL;
CREATE INDEX observations_effective_at_idx
  ON observations(effective_at DESC, observed_at DESC);

-- The same six values, already copied into the derived read model by an earlier
-- run of promote_approved_product_observations. That function now writes through
-- source_dates.stated_date, so it cannot arrive again; this repairs what is
-- already stored. product_firmware_releases is derived and rebuildable, so this
-- is not a rewrite of captured evidence -- and the correction is recorded in
-- source_data_corrections, the same table samsung_build_month_not_release_date
-- uses, so the value that was there is not simply gone.
INSERT OR IGNORE INTO source_data_corrections
  (id, entity_type, entity_id, reason, before_json, after_json, evidence_id, recorded_at)
SELECT lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-' ||
       lower(hex(randomblob(2))) || '-' || lower(hex(randomblob(2))) || '-' ||
       lower(hex(randomblob(6))),
       'product_firmware_release', pfr.id, 'source_stated_non_date_read_as_absence',
       json_object('vendor_released_at', pfr.vendor_released_at),
       json_object('vendor_released_at', NULL), NULL,
       strftime('%Y-%m-%dT%H:%M:%fZ','now')
  FROM product_firmware_releases pfr
 WHERE pfr.vendor_released_at IS NOT NULL
   AND pfr.vendor_released_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*';

UPDATE product_firmware_releases SET vendor_released_at = NULL
 WHERE vendor_released_at IS NOT NULL
   AND vendor_released_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*';

-- The sibling derived date column, fed from `$.data.publish_date` by the same
-- function. Measured 0 offending rows of 862 on the live corpus, so this statement
-- changes nothing today; it is here because the rule is the SAME rule and a reader
-- comparing the two tables should not have to wonder whether one was exempt.
UPDATE product_security_publications SET published_at = NULL
 WHERE published_at IS NOT NULL
   AND published_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*';

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(31,'source_stated_date_rule',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
