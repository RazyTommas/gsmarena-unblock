BEGIN;

-- Fold the silicon columns into the device projection.
--
-- 0022 removed the identity join from the request path. Profiling the REAL
-- statement afterwards -- captured off the connection rather than transcribed --
-- showed the remaining cost was never the identity side at all:
--
--     MATERIALIZE v_chip_devices
--     ... six-table silicon join ...
--     SEARCH cd USING AUTOMATIC COVERING INDEX (hardware_model_id=? AND part_id=?)
--
-- v_chip_devices joins hardware_silicon, silicon_parts, silicon_families,
-- silicon_vendors, hardware_models and device_variants. SQLite materialised the
-- whole view and then built a throwaway index over it, for BOTH the row query
-- and the count query, on every request. At 53,436 devices that was 180ms and
-- 44ms respectively.
--
-- The grid renders exactly two silicon values per device, and the filters test
-- three more. All five are a property of the device, settled at ingest, so they
-- belong on the row the grid already reads.
--
-- A device can carry several silicon parts. The projection keeps the PRIMARY
-- one -- lowest role rank, then part number, so the choice is stable across
-- rebuilds and never depends on row order -- and records how many there are, so
-- a reader is never shown one part as though it were the whole story.
ALTER TABLE device_catalog_flat ADD COLUMN chip_marketing_name TEXT;
ALTER TABLE device_catalog_flat ADD COLUMN chip_part_number TEXT;
ALTER TABLE device_catalog_flat ADD COLUMN silicon_vendor TEXT;
ALTER TABLE device_catalog_flat ADD COLUMN silicon_family TEXT;
ALTER TABLE device_catalog_flat ADD COLUMN silicon_part_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE device_catalog_flat_staging ADD COLUMN chip_marketing_name TEXT;
ALTER TABLE device_catalog_flat_staging ADD COLUMN chip_part_number TEXT;
ALTER TABLE device_catalog_flat_staging ADD COLUMN silicon_vendor TEXT;
ALTER TABLE device_catalog_flat_staging ADD COLUMN silicon_family TEXT;
ALTER TABLE device_catalog_flat_staging ADD COLUMN silicon_part_count INTEGER NOT NULL DEFAULT 0;

-- The three columns the Explore filters test.
CREATE INDEX device_catalog_flat_silicon_idx
  ON device_catalog_flat(silicon_vendor, silicon_family, chip_part_number);

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(23,'device_catalog_silicon',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
