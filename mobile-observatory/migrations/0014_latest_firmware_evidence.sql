-- Prefer the latest build explicitly named by the newest preserved Samsung
-- manifest. Historical rows sharing a capture timestamp cannot displace it.
BEGIN IMMEDIATE;
DROP VIEW v_latest_firmware;
CREATE VIEW v_latest_firmware AS
WITH source_positions AS (
 SELECT fre.firmware_release_id,
        max(o.observed_at) manifest_observed_at,
        max(CASE WHEN json_extract(o.payload_json,'$.data.manifest_position')='latest'
                 THEN o.observed_at END) declared_latest_at
 FROM firmware_release_evidence fre JOIN evidence e ON e.id=fre.evidence_id
 JOIN observations o ON o.id=e.observation_id JOIN ingestion_runs r ON r.id=o.run_id
 WHERE o.source_id='samsung.fota'
   AND r.parser_name IN ('samsung_fota_manifest','samsung_fota_history_csv')
   AND json_extract(o.payload_json,'$.data.manifest_position') IN ('latest','upgrade')
 GROUP BY fre.firmware_release_id
), candidates AS (
 SELECT fr.id AS firmware_release_id,fr.hardware_model_id,fr.firmware_target_id,
        fr.build_id,fr.channel,fr.os_release_id,fr.security_patch_level,
        fr.baseband_version,fr.vendor_released_at,fr.first_observed_at,fr.last_observed_at,
        p.manifest_observed_at,p.declared_latest_at,
        max(p.manifest_observed_at) OVER (
          PARTITION BY fr.hardware_model_id,ifnull(fr.firmware_target_id,''),fr.channel
        ) newest_manifest_at
 FROM firmware_releases fr LEFT JOIN source_positions p ON p.firmware_release_id=fr.id
), ranked AS (
 SELECT *,
        CASE WHEN declared_latest_at=newest_manifest_at THEN 'source_manifest_latest'
             WHEN vendor_released_at IS NOT NULL THEN 'vendor_release_date'
             ELSE 'observation_order_only' END latest_basis,
        row_number() OVER (
          PARTITION BY hardware_model_id,ifnull(firmware_target_id,''),channel
          ORDER BY CASE WHEN declared_latest_at=newest_manifest_at THEN 1 ELSE 0 END DESC,
                   vendor_released_at IS NOT NULL DESC,vendor_released_at DESC,
                   first_observed_at DESC,firmware_release_id DESC
        ) recency_rank
 FROM candidates
)
SELECT * FROM ranked WHERE recency_rank=1;
INSERT INTO schema_migrations(version,name,applied_at)
VALUES(14,'latest_firmware_evidence',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
