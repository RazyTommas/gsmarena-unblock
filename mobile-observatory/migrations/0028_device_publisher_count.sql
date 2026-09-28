BEGIN;

-- How many publishers describe this device.
--
-- When several describe one device, the projection must still show ONE row, and
-- it picks by latest_basis, then the source's currency_rank, then source_id.
-- Four community archives share a rank, so for 182 devices the choice falls to
-- a deterministic string sort -- frbox wins over naijarom because "f" < "n".
--
-- That is defensible: choosing WHICH REGION to headline is a display choice
-- among rows that do not contradict each other, unlike two publishers claiming
-- the same region and channel, which _validate() refuses outright. What is not
-- defensible is doing it silently. A reader shown one build has no way to tell
-- whether it is the only account or one of four.
--
-- So the count travels with the row and the grid prints it. The honest claim is
-- "this is frbox's answer, and two other publishers also describe this device",
-- not "this is the answer".
ALTER TABLE device_current_firmware ADD COLUMN device_source_count INTEGER;
ALTER TABLE device_current_firmware_staging ADD COLUMN device_source_count INTEGER;

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(28,'device_publisher_count',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
