PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE INDEX firmware_history_query_idx
ON firmware_releases(hardware_model_id, firmware_target_id, channel,
                     vendor_released_at DESC, first_observed_at DESC, id);
CREATE INDEX firmware_build_query_idx ON firmware_releases(build_id COLLATE NOCASE);
CREATE INDEX firmware_android_query_idx ON firmware_releases(os_release_id, hardware_model_id);
CREATE INDEX target_code_query_idx ON firmware_targets(target_code COLLATE NOCASE);
CREATE INDEX device_variant_name_idx ON device_variants(canonical_name COLLATE NOCASE);
CREATE INDEX silicon_marketing_name_idx ON silicon_parts(marketing_name COLLATE NOCASE);

CREATE VIEW v_latest_firmware AS
SELECT * FROM (
  SELECT fr.id AS firmware_release_id, fr.hardware_model_id, fr.firmware_target_id,
         fr.build_id, fr.channel, fr.os_release_id, fr.security_patch_level,
         fr.baseband_version, fr.vendor_released_at, fr.first_observed_at,
         fr.last_observed_at,
         row_number() OVER (
           PARTITION BY fr.hardware_model_id, ifnull(fr.firmware_target_id, ''), fr.channel
           ORDER BY coalesce(fr.vendor_released_at, fr.first_observed_at) DESC,
                    fr.first_observed_at DESC, fr.id DESC
         ) AS recency_rank
  FROM firmware_releases fr
) WHERE recency_rank = 1;

CREATE VIEW v_device_region_history AS
SELECT fr.id AS firmware_release_id, dc.hardware_model_id, dc.manufacturer,
       dc.brand, dc.family, dc.variant, dc.model_code, dc.codename,
       ft.id AS firmware_target_id, ft.target_code, ft.display_name AS target_name,
       ft.target_kind, fr.build_id, fr.channel, os.platform, os.major AS os_major,
       os.display_name AS os_name, fr.security_patch_level, fr.baseband_version,
       fr.vendor_released_at, fr.first_observed_at, fr.last_observed_at
FROM firmware_releases fr
JOIN v_device_catalog dc ON dc.hardware_model_id = fr.hardware_model_id
LEFT JOIN firmware_targets ft ON ft.id = fr.firmware_target_id
LEFT JOIN os_releases os ON os.id = fr.os_release_id;

CREATE VIEW v_chip_devices AS
SELECT sp.id AS part_id, sv.canonical_name AS silicon_vendor,
       sf.canonical_name AS silicon_family, sp.part_number, sp.marketing_name,
       hs.role, sr.revision_code, dc.hardware_model_id, dc.manufacturer,
       dc.brand, dc.family AS device_family, dc.variant, dc.model_code, dc.codename
FROM hardware_silicon hs
JOIN silicon_parts sp ON sp.id = hs.part_id
JOIN silicon_families sf ON sf.id = sp.family_id
JOIN silicon_vendors sv ON sv.id = sf.vendor_id
LEFT JOIN silicon_revisions sr ON sr.id = hs.revision_id
JOIN v_device_catalog dc ON dc.hardware_model_id = hs.hardware_model_id;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (2, 'query_read_models', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
COMMIT;
