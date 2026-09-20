-- 0016_merge_brand_prefixed_products.sql
--
-- Merge products stored twice under a brand-prefixed and a bare name.
--
-- source_products carries the manufacturer in its own column and is UNIQUE on
-- (manufacturer, normalized_name). identity_bridge._norm stripped a hardcoded
-- "xiaomi " prefix and nothing else, so 'Xiaomi 12' normalised to '12' while
-- 'TECNO POVA Neo' stayed 'tecno pova neo' and the same device arriving as
-- 'POVA Neo' produced 'pova neo'. Two different keys, so the
-- ON CONFLICT(manufacturer, normalized_name) upsert never fired and both rows were
-- inserted. Seven TECNO devices ended up stored twice, every one review_state
-- 'approved' -- they passed review because each looked like a legitimate distinct
-- product on its own.
--
-- _norm is manufacturer-aware as of this change, so NEW ingests collide correctly and
-- upsert. This migration repairs the rows already written.
--
-- SURVIVOR RULE: the row with the most observation links wins, because it is the one
-- the rest of the corpus already points at; ties break on the lexicographically
-- smaller id so the result is deterministic and the migration is replayable.
-- Children are repointed rather than deleted -- no evidence is discarded, only
-- re-attached. Verified by asserting that every observation holding a product link
-- before the migration still holds one after, which is the check that caught the
-- first version of this file silently orphaning five of them.

PRAGMA foreign_keys=ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE _dupe_map AS
WITH renormalised AS (
    SELECT
        id,
        manufacturer,
        canonical_name,
        -- strip a leading, whole-token manufacturer name; mirrors _norm()
        TRIM(
            CASE
                WHEN LOWER(canonical_name) LIKE LOWER(manufacturer) || ' %'
                     AND LENGTH(TRIM(SUBSTR(canonical_name, LENGTH(manufacturer) + 2))) > 0
                THEN LOWER(TRIM(SUBSTR(canonical_name, LENGTH(manufacturer) + 2)))
                ELSE LOWER(canonical_name)
            END
        ) AS key
    FROM source_products
),
ranked AS (
    SELECT
        r.id,
        r.manufacturer,
        r.key,
        (SELECT COUNT(*) FROM observation_product_links l WHERE l.product_id = r.id) AS links
    FROM renormalised r
),
winners AS (
    SELECT manufacturer, key, id AS keep_id
    FROM ranked a
    WHERE a.id = (
        SELECT b.id FROM ranked b
        WHERE b.manufacturer = a.manufacturer AND b.key = a.key
        ORDER BY b.links DESC, b.id ASC
        LIMIT 1
    )
)
SELECT r.id AS drop_id, w.keep_id, r.manufacturer, r.key
FROM ranked r
JOIN winners w ON w.manufacturer = r.manufacturer AND w.key = r.key
WHERE r.id <> w.keep_id;

-- observation_product_links has observation_id as its PRIMARY KEY: one observation
-- links to exactly ONE product. So this is an UPDATE, not an insert-then-delete.
--
-- The first version of this migration did INSERT OR IGNORE followed by DELETE. The
-- insert was silently ignored -- the observation already had a row, pointing at the
-- loser -- and the delete then removed that row, orphaning 5 observations from any
-- product. It passed every row-count check on source_products and was only visible
-- by asking "does every observation that had a link still have one".
--
-- Repointing by UPDATE cannot conflict, because the key being updated is not the key
-- that identifies the row.
UPDATE observation_product_links
SET product_id = (SELECT keep_id FROM _dupe_map WHERE drop_id = product_id)
WHERE product_id IN (SELECT drop_id FROM _dupe_map);

UPDATE OR IGNORE source_identity_registry
SET product_id = (SELECT keep_id FROM _dupe_map WHERE drop_id = product_id)
WHERE product_id IN (SELECT drop_id FROM _dupe_map);

UPDATE OR IGNORE identity_conclusions
SET product_id = (SELECT keep_id FROM _dupe_map WHERE drop_id = product_id)
WHERE product_id IN (SELECT drop_id FROM _dupe_map);

UPDATE OR IGNORE observed_product_silicon
SET product_id = (SELECT keep_id FROM _dupe_map WHERE drop_id = product_id)
WHERE product_id IN (SELECT drop_id FROM _dupe_map);

UPDATE OR IGNORE product_firmware_releases
SET product_id = (SELECT keep_id FROM _dupe_map WHERE drop_id = product_id)
WHERE product_id IN (SELECT drop_id FROM _dupe_map);

UPDATE OR IGNORE product_security_publications
SET product_id = (SELECT keep_id FROM _dupe_map WHERE drop_id = product_id)
WHERE product_id IN (SELECT drop_id FROM _dupe_map);

UPDATE OR IGNORE source_specifications
SET product_id = (SELECT keep_id FROM _dupe_map WHERE drop_id = product_id)
WHERE product_id IN (SELECT drop_id FROM _dupe_map);

UPDATE OR IGNORE source_build_product_links
SET product_id = (SELECT keep_id FROM _dupe_map WHERE drop_id = product_id)
WHERE product_id IN (SELECT drop_id FROM _dupe_map);

-- Anything still pointing at a loser could not be repointed (a UNIQUE clash on the
-- survivor). Those rows are duplicates of data the survivor already holds, so drop
-- them rather than leave dangling references.
DELETE FROM source_identity_registry     WHERE product_id IN (SELECT drop_id FROM _dupe_map);
DELETE FROM identity_conclusions         WHERE product_id IN (SELECT drop_id FROM _dupe_map);
DELETE FROM observed_product_silicon     WHERE product_id IN (SELECT drop_id FROM _dupe_map);
DELETE FROM product_firmware_releases    WHERE product_id IN (SELECT drop_id FROM _dupe_map);
DELETE FROM product_security_publications WHERE product_id IN (SELECT drop_id FROM _dupe_map);
DELETE FROM source_specifications        WHERE product_id IN (SELECT drop_id FROM _dupe_map);
DELETE FROM source_build_product_links   WHERE product_id IN (SELECT drop_id FROM _dupe_map);

DELETE FROM source_products WHERE id IN (SELECT drop_id FROM _dupe_map);

-- Re-key the survivors so the stored value matches what _norm now produces. Without
-- this the rows are merged but the UNIQUE key still holds the old spelling, and the
-- next ingest of the bare name would insert a third row.
UPDATE source_products
SET normalized_name = TRIM(
        CASE
            WHEN LOWER(canonical_name) LIKE LOWER(manufacturer) || ' %'
                 AND LENGTH(TRIM(SUBSTR(canonical_name, LENGTH(manufacturer) + 2))) > 0
            THEN LOWER(TRIM(SUBSTR(canonical_name, LENGTH(manufacturer) + 2)))
            ELSE LOWER(canonical_name)
        END
    )
WHERE normalized_name <> TRIM(
        CASE
            WHEN LOWER(canonical_name) LIKE LOWER(manufacturer) || ' %'
                 AND LENGTH(TRIM(SUBSTR(canonical_name, LENGTH(manufacturer) + 2))) > 0
            THEN LOWER(TRIM(SUBSTR(canonical_name, LENGTH(manufacturer) + 2)))
            ELSE LOWER(canonical_name)
        END
    );

DROP TABLE _dupe_map;

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(16,'merge_brand_prefixed_products',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
