BEGIN;

-- 5,643 of 5,804 published "this build came after that one" claims were ordered
-- by a hash, and the corpus did not say so.
--
-- `promote_approved_product_observations` read `$.data.release_date` and
-- `$.data.branch` -- one publisher's vocabulary.
-- `mifirm.community.firmware_archive` publishes the identical two facts as
-- `$.data.vendor_released_at` and `$.data.channel`, so all 21,845 of its
-- promoted releases stored `vendor_released_at IS NULL` and `channel='unknown'`
-- while the payload beside them carried a real date and a real branch. Nothing
-- errored. `android_version_changed` then ordered its before/after pair by
-- `ORDER BY ... vendor_released_at, id`, and with every date NULL the sort
-- collapsed onto `id` -- `uuid5(_NS,'product-firmware'||observation_id)`, a
-- digest.
--
-- Replayed over the same corpus with the dates and channels the sources stated
-- all along: of the 5,690 events whose inputs still exist, 5,588 would not have
-- been produced and 1,064 that should exist never were. 102 are the same either
-- way. 278 of the surviving pairs assert a direction the real dates reverse.
--
-- THIS MIGRATION RETRACTS NOTHING. Every `domain_events` row stays exactly as
-- published; the table's own `domain_events_no_update` / `_no_delete` triggers
-- say that is the contract and it is kept. Three things happen instead, and the
-- ORDER OF THE TWO HALVES IS LOAD-BEARING:
--
--   1. every existing event's ordering basis is recorded, measured on the
--      corpus AS IT STANDS -- undated;
--   2. and only THEN are the 21,845 release dates and channels repaired.
--
-- Reversing those two steps would stamp `vendor_release_date` onto 5,643
-- hash-ordered events, because by then the releases they cite would carry dates.
-- That is the single mistake this whole migration exists to prevent and it is
-- why the basis is a frozen record rather than something derived at read time.
--
-- See src/mobile_observatory/firmware_order.py for why this is a table and not a
-- column on `domain_events`, and src/mobile_observatory/source_dates.py for the
-- field-name rule and the full per-adapter inventory.

CREATE TABLE domain_event_ordering (
  -- One row per event whose claim is an ORDER. Not every event type has one:
  -- firmware_first_observed orders nothing.
  event_id            TEXT PRIMARY KEY REFERENCES domain_events(id),
  -- The same words migration 0019 gave `latest_basis`, because they mean the
  -- same thing and a second vocabulary for one concept is how the first drifts.
  -- Keep in step with firmware_order.ORDERING_BASES; the test asserts it.
  ordering_basis      TEXT NOT NULL CHECK (ordering_basis IN (
                        'vendor_release_date',
                        'observation_order_only',
                        'mixed_dated_and_undated',
                        'cited_releases_absent')),
  -- The pair, named. A reader can go look.
  before_release_id   TEXT,
  after_release_id    TEXT,
  -- The dates AS THEY STOOD when the order was decided. Deliberately a copy and
  -- not a join: the second half of this migration changes 21,845 of them, and a
  -- join would make every old event claim the new evidence.
  before_released_at  TEXT,
  after_released_at   TEXT,
  recorded_at         TEXT NOT NULL,
  -- A basis that names dates it does not have, or has dates it does not name, is
  -- a mislabel -- the one failure mode that would make this table worse than no
  -- table. The database enforces it rather than the writer.
  CHECK ((ordering_basis='vendor_release_date')
           = (before_released_at IS NOT NULL AND after_released_at IS NOT NULL)),
  CHECK (ordering_basis!='observation_order_only'
           OR (before_released_at IS NULL AND after_released_at IS NULL))
) STRICT;

CREATE INDEX domain_event_ordering_basis_idx ON domain_event_ordering(ordering_basis);

-- STEP 1, BEFORE THE REPAIR. The pair is read out of `dedupe_key`, which the
-- promotion builds as
--   product-android:<product_id>:<region_code>:<channel>:<old_id>:<new_id>
-- and every id is a 36-character uuid5, so the last two are fixed-width slices.
-- Verified on the live corpus rather than assumed: all 5,804 keys split into
-- exactly 6 colon-separated segments, `substr(dedupe_key,-36)` matches a uuid in
-- 5,804 of 5,804, `substr(dedupe_key,-73,36)` likewise with ':' at -37, and all
-- 29,921 product_firmware_releases ids are 36 characters.
--
-- LEFT JOIN, not JOIN: 105 events cite release rows the corpus no longer holds
-- (products survive; the rows were re-keyed away by a later merge). Those get
-- `cited_releases_absent` and NOT `observation_order_only`, because "we cannot
-- establish this" and "this was decided by a row id" are different statements
-- and only one of them is measured.
INSERT OR IGNORE INTO domain_event_ordering
  (event_id,ordering_basis,before_release_id,after_release_id,
   before_released_at,after_released_at,recorded_at)
SELECT de.id,
       CASE WHEN b.id IS NULL OR a.id IS NULL THEN 'cited_releases_absent'
            WHEN b.vendor_released_at IS NOT NULL AND a.vendor_released_at IS NOT NULL
                 THEN 'vendor_release_date'
            WHEN b.vendor_released_at IS NULL AND a.vendor_released_at IS NULL
                 THEN 'observation_order_only'
            ELSE 'mixed_dated_and_undated' END,
       substr(de.dedupe_key,-73,36),
       substr(de.dedupe_key,-36),
       -- NULL when the row is gone, so the two CHECK constraints above hold for
       -- `cited_releases_absent` too: it names no dates and claims none.
       b.vendor_released_at,
       a.vendor_released_at,
       de.recorded_at
  FROM domain_events de
  LEFT JOIN product_firmware_releases b ON b.id = substr(de.dedupe_key,-73,36)
  LEFT JOIN product_firmware_releases a ON a.id = substr(de.dedupe_key,-36)
 WHERE de.event_type='android_version_changed';

-- STEP 2, AFTER the basis of every existing event is frozen.
--
-- `product_firmware_releases` is a DERIVED read model, rebuildable from
-- `observations`, so repairing it is not a rewrite of captured evidence --
-- migration 0031 made the same correction to the same column for the same
-- reason, and like 0031 every change is recorded in `source_data_corrections`
-- so the value that was stored is not simply gone. The payload is untouched; it
-- is where the right answer was sitting the whole time.
--
-- One correction row per release rather than one per column: a release's repair
-- is one event, and both fields were lost to the same cause. The reason names
-- the cause and not the symptom, so a reader can tell these 21,845 from the 6
-- that 0031 corrected.
INSERT OR IGNORE INTO source_data_corrections
  (id, entity_type, entity_id, reason, before_json, after_json, evidence_id, recorded_at)
SELECT lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-' ||
       lower(hex(randomblob(2))) || '-' || lower(hex(randomblob(2))) || '-' ||
       lower(hex(randomblob(6))),
       'product_firmware_release', pfr.id,
       'source_stated_date_and_channel_were_read_under_another_publishers_field_names',
       json_object('vendor_released_at', pfr.vendor_released_at, 'channel', pfr.channel),
       json_object('vendor_released_at',
                   CASE WHEN json_extract(o.payload_json,'$.data.release_date')
                             GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                        THEN json_extract(o.payload_json,'$.data.release_date')
                        WHEN json_extract(o.payload_json,'$.data.vendor_released_at')
                             GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                        THEN json_extract(o.payload_json,'$.data.vendor_released_at') END,
                   'channel',
                   coalesce(nullif(trim(ifnull(json_extract(o.payload_json,'$.data.branch'),'')),''),
                            nullif(trim(ifnull(json_extract(o.payload_json,'$.data.channel'),'')),''),
                            'unknown')),
       NULL, strftime('%Y-%m-%dT%H:%M:%fZ','now')
  FROM product_firmware_releases pfr
  JOIN observations o ON o.id = pfr.observation_id
 WHERE (pfr.vendor_released_at IS NULL
        AND (json_extract(o.payload_json,'$.data.release_date')
               GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
             OR json_extract(o.payload_json,'$.data.vendor_released_at')
               GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'))
    OR (pfr.channel='unknown'
        AND coalesce(nullif(trim(ifnull(json_extract(o.payload_json,'$.data.branch'),'')),''),
                     nullif(trim(ifnull(json_extract(o.payload_json,'$.data.channel'),'')),'')) IS NOT NULL);

-- Only where the column says nothing and the payload says something. A release
-- that already carries a date keeps it, and the 3,190 releases whose sources
-- publish no channel at all (naijarom, google.ota.checkin, frbox) stay honestly
-- 'unknown' -- absence a source left empty is still absence.
--
-- The GLOB is source_dates.stated_date_sql's shape rule spelled in SQL, in the
-- same precedence source_dates.VENDOR_RELEASE_DATE_FIELDS declares.
UPDATE product_firmware_releases SET vendor_released_at = (
    SELECT CASE WHEN json_extract(o.payload_json,'$.data.release_date')
                     GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                THEN json_extract(o.payload_json,'$.data.release_date')
                WHEN json_extract(o.payload_json,'$.data.vendor_released_at')
                     GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                THEN json_extract(o.payload_json,'$.data.vendor_released_at') END
      FROM observations o WHERE o.id = product_firmware_releases.observation_id)
 WHERE vendor_released_at IS NULL
   AND (SELECT CASE WHEN json_extract(o.payload_json,'$.data.release_date')
                         GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                    THEN json_extract(o.payload_json,'$.data.release_date')
                    WHEN json_extract(o.payload_json,'$.data.vendor_released_at')
                         GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                    THEN json_extract(o.payload_json,'$.data.vendor_released_at') END
         FROM observations o WHERE o.id = product_firmware_releases.observation_id) IS NOT NULL;

-- `channel` is NOT NULL and part of UNIQUE(product_id,identity_id,build_id,
-- channel,delivery_method), so this could in principle collide. It cannot:
-- 'unknown' is one value, and every row currently holding it is already unique
-- under it, so replacing 'unknown' with a real channel can only SPLIT a group
-- and never merge two. Measured after, on a copy: 0 constraint failures.
UPDATE product_firmware_releases SET channel = (
    SELECT coalesce(nullif(trim(ifnull(json_extract(o.payload_json,'$.data.branch'),'')),''),
                    nullif(trim(ifnull(json_extract(o.payload_json,'$.data.channel'),'')),''))
      FROM observations o WHERE o.id = product_firmware_releases.observation_id)
 WHERE channel='unknown'
   AND (SELECT coalesce(nullif(trim(ifnull(json_extract(o.payload_json,'$.data.branch'),'')),''),
                        nullif(trim(ifnull(json_extract(o.payload_json,'$.data.channel'),'')),''))
          FROM observations o WHERE o.id = product_firmware_releases.observation_id) IS NOT NULL;

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(34,'firmware_order_basis',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
