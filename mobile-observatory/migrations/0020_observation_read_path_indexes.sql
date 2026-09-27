BEGIN;

-- Make the source-records browser's sort indexable, and give the product-link
-- lookup an index instead of one SQLite builds per request.
--
-- /api/v1/source-records was the slowest endpoint in the app at 730ms, ahead of
-- /devices. Its ORDER BY is a coalesce of four json_extract()s over
-- observations.payload_json, so no index on any stored column could serve it:
-- the plan was `SCAN o` across all 96,319 rows and 102MB of payload, then
-- `USE TEMP B-TREE FOR ORDER BY`, to return 100 rows. Measured at 182ms for the
-- sort alone.
--
-- effective_at is a VIRTUAL generated column rather than a stored one that the
-- ingest fills. That choice is deliberate: a stored copy is a second source of
-- truth for a value the row already contains, and it goes wrong exactly when
-- someone corrects a payload without knowing a derived column shadows it. A
-- generated column cannot disagree with its own row. VIRTUAL costs nothing in
-- the table; the INDEX below is what materialises the values, so the sort
-- becomes an index scan while reads of the column stay derived.
--
-- Measured after: `SCAN o USING INDEX observations_effective_at_idx`, no temp
-- B-tree, 182ms -> 0.1ms for the sorted page. Cost +5.1MB on a 225MB database.
--
-- The coalesce order is the read path's own, kept byte-for-byte so the index
-- and the query agree. $.data.release_time is retained even though it is JSON
-- null in all 21,188 rows that carry it: that is a source recording its
-- silence, not a path that does not exist, and dropping it would change what
-- the column means if a capture ever states one.
ALTER TABLE observations ADD COLUMN effective_at TEXT
  GENERATED ALWAYS AS (coalesce(json_extract(payload_json,'$.data.release_date'),
                                json_extract(payload_json,'$.data.publish_date'),
                                json_extract(payload_json,'$.data.release_time'),
                                observed_at)) VIRTUAL;

CREATE INDEX observations_effective_at_idx
  ON observations(effective_at DESC, observed_at DESC);

-- observation_product_links had exactly one index: the implicit primary key on
-- observation_id. Every lookup by product_id -- which is what the identity
-- pages do -- planned as `SCAN observation_product_links` over 59,393 rows, and
-- the counting query planned as `SEARCH ... USING AUTOMATIC COVERING INDEX`,
-- meaning SQLite rebuilt a throwaway index over the whole table on each
-- request because doing so was still cheaper than the scan.
CREATE INDEX observation_product_links_product_idx
  ON observation_product_links(product_id);

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(20,'observation_read_path_indexes',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
