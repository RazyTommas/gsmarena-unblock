BEGIN;

-- Flatten the device identity join so the grid can sort by index.
--
-- 0021 made the firmware side of /devices a seek. What was left, and what the
-- query plan then showed plainly, is the identity side:
--
--     USE TEMP B-TREE FOR ORDER BY
--
-- devices_page orders by brands.canonical_name, device_variants.canonical_name
-- and hardware_models.model_code -- three columns in three different tables. No
-- index can span them, so SQLite joins all five identity tables for every
-- device, sorts the whole result in a temporary B-tree, and then discards all
-- but 100 rows. Measured on a corpus scaled to the shape production grows into
-- (53,436 devices): 404ms, essentially all of it here.
--
-- This is the same fix as device_current_firmware and it carries the same risk,
-- one notch sharper: a device missing from this table is invisible in the grid
-- entirely, where a device missing from the firmware projection merely loses
-- its firmware. Three things contain that risk:
--   * it is built and published inside the SAME transaction as the firmware
--     projection, so a reader can never see identity from one generation and
--     firmware from another;
--   * _validate() refuses to publish unless the row count equals
--     count(*) FROM hardware_models exactly;
--   * integrity.check_corpus() reports any hardware_model the grid cannot see,
--     so a stale projection is loud rather than silently short.
CREATE TABLE device_catalog_flat (
  hardware_model_id TEXT PRIMARY KEY REFERENCES hardware_models(id),
  manufacturer      TEXT NOT NULL,
  brand             TEXT NOT NULL,
  family            TEXT NOT NULL,
  variant           TEXT NOT NULL,
  model_code        TEXT NOT NULL,
  codename          TEXT
) STRICT;

-- Exactly the grid's default ORDER BY, so the sort becomes an ordered index
-- scan that stops at LIMIT instead of a full sort of the catalogue.
CREATE INDEX device_catalog_flat_name_idx
  ON device_catalog_flat(brand, variant, model_code);
CREATE INDEX device_catalog_flat_model_idx
  ON device_catalog_flat(model_code);

CREATE TABLE device_catalog_flat_staging (
  hardware_model_id TEXT PRIMARY KEY,
  manufacturer      TEXT NOT NULL,
  brand             TEXT NOT NULL,
  family            TEXT NOT NULL,
  variant           TEXT NOT NULL,
  model_code        TEXT NOT NULL,
  codename          TEXT
) STRICT;

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(22,'device_identity_projection',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
