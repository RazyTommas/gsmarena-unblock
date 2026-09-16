PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE security_coverage_gaps (
    id TEXT PRIMARY KEY,
    vendor TEXT NOT NULL,
    capability TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('missing','partial','available')),
    reason TEXT NOT NULL,
    evidence_uri TEXT,
    checked_at TEXT NOT NULL,
    UNIQUE(vendor, capability)
) STRICT;

CREATE VIEW v_security_findings AS
SELECT v.id AS vulnerability_id, v.cve_id, v.summary, v.published_at,
       group_concat(DISTINCT a.title) AS bulletins,
       group_concat(DISTINCT s.name) AS sources,
       count(DISTINCT ac.id) AS applicability_claim_count,
       count(DISTINCT CASE WHEN ac.subject_type='silicon_part' THEN ac.subject_id END) AS affected_part_count,
       count(DISTINCT hs.hardware_model_id) AS affected_device_count,
       count(DISTINCT fc.id) AS fix_claim_count
FROM vulnerabilities v
LEFT JOIN advisory_vulnerabilities av ON av.vulnerability_id=v.id
LEFT JOIN advisories a ON a.id=av.advisory_id
LEFT JOIN sources s ON s.id=a.source_id
LEFT JOIN applicability_claims ac ON ac.vulnerability_id=v.id AND ac.relationship='affected'
LEFT JOIN hardware_silicon hs ON ac.subject_type='silicon_part' AND hs.part_id=ac.subject_id
LEFT JOIN fix_claims fc ON fc.vulnerability_id=v.id
GROUP BY v.id;

CREATE VIEW v_silicon_security AS
SELECT sp.id AS part_id, sv.canonical_name AS vendor, sf.canonical_name AS family,
       sp.part_number, sp.marketing_name,
       count(DISTINCT hs.hardware_model_id) AS device_count,
       count(DISTINCT ac.vulnerability_id) AS affected_cve_count,
       count(DISTINCT CASE WHEN fc.id IS NOT NULL THEN ac.vulnerability_id END) AS cves_with_fix_coordinate
FROM silicon_parts sp
JOIN silicon_families sf ON sf.id=sp.family_id
JOIN silicon_vendors sv ON sv.id=sf.vendor_id
LEFT JOIN hardware_silicon hs ON hs.part_id=sp.id
LEFT JOIN applicability_claims ac ON ac.subject_type='silicon_part' AND ac.subject_id=sp.id
                                   AND ac.relationship='affected'
LEFT JOIN fix_claims fc ON fc.vulnerability_id=ac.vulnerability_id
GROUP BY sp.id;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (8, 'security_intelligence', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
COMMIT;
