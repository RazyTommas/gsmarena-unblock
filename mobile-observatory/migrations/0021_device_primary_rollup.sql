BEGIN;

-- Precompute the per-device answer, not just the per-target one.
--
-- 0019 removed a whole-corpus window from every request and replaced it with a
-- lookup, which took /devices from 452ms to 26ms. But devices_page still had to
-- turn 4.2 target rows per device into ONE row to render, and it did that the
-- same way the old view did: a row_number() window over the entire projection,
-- plus a GROUP BY aggregate over the entire projection for the build and region
-- counts. Both are computed in full before LIMIT 100 discards almost all of it.
--
-- At dev volume that is 1,271 rows and invisible. Measured against a corpus
-- inflated to the shape production actually grows into -- 53,436 devices
-- (176x), 4.2 targets each, 225,743 projection rows -- /devices costs 1,549ms.
-- Better than the ~41s the old derivation extrapolates to, and still not an
-- interactive page.
--
-- An earlier attempt at this measurement inflated targets-per-device instead of
-- devices and reported 1,309ms with a 1.3MB response, because group_concat was
-- joining 840 region codes per device. That is not how the corpus grows; the
-- number was measuring the fixture, not the system.
--
-- So the rollup moves to build time. One row per device is marked primary and
-- carries the device-level totals, and the partial index below makes finding it
-- a seek. The per-target rows are unchanged and still serve the detail views.
ALTER TABLE device_current_firmware ADD COLUMN is_device_primary INTEGER NOT NULL DEFAULT 0;
ALTER TABLE device_current_firmware ADD COLUMN device_release_total INTEGER;
ALTER TABLE device_current_firmware ADD COLUMN device_target_total INTEGER;
ALTER TABLE device_current_firmware ADD COLUMN device_target_codes TEXT;

ALTER TABLE device_current_firmware_staging ADD COLUMN is_device_primary INTEGER NOT NULL DEFAULT 0;
ALTER TABLE device_current_firmware_staging ADD COLUMN device_release_total INTEGER;
ALTER TABLE device_current_firmware_staging ADD COLUMN device_target_total INTEGER;
ALTER TABLE device_current_firmware_staging ADD COLUMN device_target_codes TEXT;

-- Partial: the grid only ever wants the primary row, and indexing the other
-- ~3.2 rows per device would be paying to find rows this index exists to skip.
CREATE INDEX device_current_firmware_primary_idx
  ON device_current_firmware(hardware_model_id) WHERE is_device_primary=1;

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(21,'device_primary_rollup',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
