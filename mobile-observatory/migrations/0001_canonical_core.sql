PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT,
    authority_scope TEXT NOT NULL DEFAULT 'secondary',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE ingestion_runs (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('running','succeeded','partial','failed','quarantined')),
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    fetched_count INTEGER NOT NULL DEFAULT 0 CHECK (fetched_count >= 0),
    accepted_count INTEGER NOT NULL DEFAULT 0 CHECK (accepted_count >= 0),
    rejected_count INTEGER NOT NULL DEFAULT 0 CHECK (rejected_count >= 0),
    error TEXT
) STRICT;

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    media_type TEXT,
    source_url TEXT,
    retrieved_at TEXT NOT NULL,
    storage_uri TEXT NOT NULL,
    byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
    UNIQUE(source_id, sha256)
) STRICT;

CREATE TABLE observations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    record_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    validation_state TEXT NOT NULL CHECK (validation_state IN ('pending','valid','invalid','quarantined','promoted')),
    validation_errors_json TEXT CHECK (validation_errors_json IS NULL OR json_valid(validation_errors_json)),
    UNIQUE(source_id, record_type, source_key, content_sha256)
) STRICT;

CREATE INDEX observations_review_idx ON observations(validation_state, record_type);

CREATE TABLE evidence (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    observation_id TEXT REFERENCES observations(id),
    locator TEXT,
    excerpt TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, observation_id, locator)
) STRICT;

CREATE TABLE manufacturers (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    website TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE brands (
    id TEXT PRIMARY KEY,
    manufacturer_id TEXT NOT NULL REFERENCES manufacturers(id),
    canonical_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(manufacturer_id, canonical_name)
) STRICT;

CREATE TABLE device_families (
    id TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL REFERENCES brands(id),
    canonical_name TEXT NOT NULL,
    introduced_on TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(brand_id, canonical_name)
) STRICT;

CREATE TABLE device_variants (
    id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL REFERENCES device_families(id),
    canonical_name TEXT NOT NULL,
    form_factor TEXT,
    announced_on TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(family_id, canonical_name)
) STRICT;

CREATE TABLE hardware_models (
    id TEXT PRIMARY KEY,
    variant_id TEXT NOT NULL REFERENCES device_variants(id),
    model_code TEXT NOT NULL,
    model_code_normalized TEXT NOT NULL,
    codename TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(variant_id, model_code_normalized)
) STRICT;

CREATE INDEX hardware_model_code_idx ON hardware_models(model_code_normalized);
CREATE INDEX hardware_codename_idx ON hardware_models(codename);

CREATE TABLE aliases (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('manufacturer','brand','device_family','device_variant','hardware_model','market','firmware_target','silicon_vendor','silicon_family','silicon_part','silicon_revision','component')),
    entity_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    alias TEXT NOT NULL,
    alias_normalized TEXT NOT NULL,
    review_state TEXT NOT NULL DEFAULT 'accepted' CHECK (review_state IN ('accepted','deprecated')),
    created_at TEXT NOT NULL,
    UNIQUE(entity_type, namespace, alias_normalized)
) STRICT;

CREATE INDEX aliases_lookup_idx ON aliases(alias_normalized, entity_type);

CREATE TABLE external_ids (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    external_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_id, namespace, external_key),
    UNIQUE(source_id, entity_type, entity_id, namespace)
) STRICT;

CREATE TABLE markets (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES markets(id),
    kind TEXT NOT NULL CHECK (kind IN ('global','region','subregion','country','economic_area','carrier_market')),
    canonical_name TEXT NOT NULL,
    iso_country_code TEXT CHECK (iso_country_code IS NULL OR length(iso_country_code) = 2),
    created_at TEXT NOT NULL,
    UNIQUE(parent_id, kind, canonical_name)
) STRICT;

CREATE TABLE firmware_targets (
    id TEXT PRIMARY KEY,
    vendor_namespace TEXT NOT NULL,
    target_code TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('global','region','csc','multi_csc','carrier','channel','product_code','other')),
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(vendor_namespace, target_code)
) STRICT;

CREATE TABLE firmware_target_markets (
    firmware_target_id TEXT NOT NULL REFERENCES firmware_targets(id),
    market_id TEXT NOT NULL REFERENCES markets(id),
    relationship TEXT NOT NULL CHECK (relationship IN ('includes','sold_in','observed_in','intended_for')),
    confidence TEXT NOT NULL CHECK (confidence IN ('authoritative','high','medium','low')),
    evidence_id TEXT REFERENCES evidence(id),
    PRIMARY KEY (firmware_target_id, market_id, relationship)
) WITHOUT ROWID, STRICT;

CREATE TABLE os_releases (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('android','ios','other')),
    major INTEGER,
    minor INTEGER,
    patch INTEGER,
    qualifier TEXT,
    display_name TEXT NOT NULL,
    released_on TEXT,
    UNIQUE(platform, major, minor, patch, qualifier)
) STRICT;

CREATE TABLE skin_releases (
    id TEXT PRIMARY KEY,
    vendor_namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    UNIQUE(vendor_namespace, name, version)
) STRICT;

CREATE TABLE firmware_releases (
    id TEXT PRIMARY KEY,
    hardware_model_id TEXT NOT NULL REFERENCES hardware_models(id),
    firmware_target_id TEXT REFERENCES firmware_targets(id),
    build_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'stable' CHECK (channel IN ('stable','beta','preview','internal','unknown')),
    os_release_id TEXT REFERENCES os_releases(id),
    skin_release_id TEXT REFERENCES skin_releases(id),
    security_patch_level TEXT,
    baseband_version TEXT,
    vendor_released_at TEXT,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (security_patch_level IS NULL OR security_patch_level GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    UNIQUE(hardware_model_id, firmware_target_id, build_id, channel)
) STRICT;

CREATE INDEX firmware_latest_idx ON firmware_releases(hardware_model_id, firmware_target_id, first_observed_at DESC);
CREATE INDEX firmware_spl_idx ON firmware_releases(security_patch_level);

CREATE TABLE firmware_release_evidence (
    firmware_release_id TEXT NOT NULL REFERENCES firmware_releases(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    role TEXT NOT NULL CHECK (role IN ('identity','release_date','os_version','security_patch','baseband','availability')),
    PRIMARY KEY (firmware_release_id, evidence_id, role)
) WITHOUT ROWID, STRICT;

CREATE TABLE support_assertions (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('device_family','device_variant','hardware_model')),
    subject_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('officially_supported','likely_supported','end_announced','unsupported','unknown')),
    valid_from TEXT,
    valid_to TEXT,
    asserted_at TEXT NOT NULL,
    evidence_id TEXT REFERENCES evidence(id),
    confidence TEXT NOT NULL CHECK (confidence IN ('authoritative','high','medium','low'))
) STRICT;

CREATE INDEX support_current_idx ON support_assertions(subject_type, subject_id, valid_to, asserted_at DESC);

CREATE TABLE silicon_vendors (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE silicon_families (
    id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES silicon_vendors(id),
    parent_id TEXT REFERENCES silicon_families(id),
    canonical_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(vendor_id, canonical_name)
) STRICT;

CREATE TABLE silicon_parts (
    id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL REFERENCES silicon_families(id),
    part_number TEXT NOT NULL,
    marketing_name TEXT,
    process_nm REAL,
    introduced_on TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(family_id, part_number)
) STRICT;

CREATE INDEX silicon_part_number_idx ON silicon_parts(part_number);

CREATE TABLE silicon_revisions (
    id TEXT PRIMARY KEY,
    part_id TEXT NOT NULL REFERENCES silicon_parts(id),
    revision_code TEXT NOT NULL,
    display_name TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(part_id, revision_code)
) STRICT;

CREATE TABLE components (
    id TEXT PRIMARY KEY,
    component_type TEXT NOT NULL CHECK (component_type IN ('soc','cpu','gpu','modem','dsp','wifi','bluetooth','secure_element','kernel','driver','firmware','other')),
    vendor_id TEXT REFERENCES silicon_vendors(id),
    canonical_name TEXT NOT NULL,
    version TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(component_type, vendor_id, canonical_name, version)
) STRICT;

CREATE TABLE silicon_part_components (
    part_id TEXT NOT NULL REFERENCES silicon_parts(id),
    component_id TEXT NOT NULL REFERENCES components(id),
    role TEXT NOT NULL,
    evidence_id TEXT REFERENCES evidence(id),
    PRIMARY KEY(part_id, component_id, role)
) WITHOUT ROWID, STRICT;

CREATE TABLE hardware_silicon (
    hardware_model_id TEXT NOT NULL REFERENCES hardware_models(id),
    part_id TEXT NOT NULL REFERENCES silicon_parts(id),
    revision_id TEXT REFERENCES silicon_revisions(id),
    role TEXT NOT NULL CHECK (role IN ('primary_soc','modem','connectivity','secure_element','other')),
    evidence_id TEXT REFERENCES evidence(id),
    valid_from TEXT,
    valid_to TEXT,
    PRIMARY KEY(hardware_model_id, part_id, role, valid_from)
) WITHOUT ROWID, STRICT;

CREATE INDEX hardware_silicon_reverse_idx ON hardware_silicon(part_id, revision_id, role);

CREATE TABLE vulnerabilities (
    id TEXT PRIMARY KEY,
    cve_id TEXT NOT NULL UNIQUE CHECK (cve_id GLOB 'CVE-[0-9][0-9][0-9][0-9]-[0-9]*'),
    summary TEXT,
    published_at TEXT,
    modified_at TEXT
) STRICT;

CREATE TABLE advisories (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    advisory_key TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT,
    modified_at TEXT,
    evidence_id TEXT REFERENCES evidence(id),
    UNIQUE(source_id, advisory_key)
) STRICT;

CREATE TABLE advisory_vulnerabilities (
    advisory_id TEXT NOT NULL REFERENCES advisories(id),
    vulnerability_id TEXT NOT NULL REFERENCES vulnerabilities(id),
    PRIMARY KEY(advisory_id, vulnerability_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE applicability_claims (
    id TEXT PRIMARY KEY,
    vulnerability_id TEXT NOT NULL REFERENCES vulnerabilities(id),
    subject_type TEXT NOT NULL CHECK (subject_type IN ('component','silicon_family','silicon_part','silicon_revision','hardware_model','os_release')),
    subject_id TEXT NOT NULL,
    relationship TEXT NOT NULL CHECK (relationship IN ('affected','not_affected','under_investigation')),
    constraint_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(constraint_json)),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX applicability_subject_idx ON applicability_claims(subject_type, subject_id, vulnerability_id);

CREATE TABLE fix_claims (
    id TEXT PRIMARY KEY,
    vulnerability_id TEXT NOT NULL REFERENCES vulnerabilities(id),
    fix_kind TEXT NOT NULL CHECK (fix_kind IN ('android_spl','firmware_build','component_version','driver_version','vendor_release','other')),
    coordinate_json TEXT NOT NULL CHECK (json_valid(coordinate_json)),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE security_verdicts (
    id TEXT PRIMARY KEY,
    vulnerability_id TEXT NOT NULL REFERENCES vulnerabilities(id),
    subject_type TEXT NOT NULL CHECK (subject_type IN ('firmware_release','hardware_model','silicon_part','silicon_revision')),
    subject_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open','claimed_fixed','unknown','not_adjudicable','not_applicable')),
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    inputs_json TEXT NOT NULL CHECK (json_valid(inputs_json)),
    evaluated_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES security_verdicts(id),
    evidence_summary_json TEXT NOT NULL CHECK (json_valid(evidence_summary_json)),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1))
) STRICT;

CREATE UNIQUE INDEX security_current_unique ON security_verdicts(vulnerability_id, subject_type, subject_id)
WHERE is_current = 1;

CREATE TABLE domain_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN ('device_discovered','firmware_first_observed','firmware_replaced','android_version_changed','security_patch_changed','baseband_changed','support_status_changed','advisory_published','cve_applicability_changed','source_stale','identity_corrected')),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    before_json TEXT CHECK (before_json IS NULL OR json_valid(before_json)),
    after_json TEXT CHECK (after_json IS NULL OR json_valid(after_json)),
    evidence_id TEXT REFERENCES evidence(id),
    corrects_event_id TEXT REFERENCES domain_events(id)
) STRICT;

CREATE INDEX events_feed_idx ON domain_events(recorded_at DESC, event_type);
CREATE INDEX events_subject_idx ON domain_events(subject_type, subject_id, recorded_at DESC);

CREATE TRIGGER domain_events_no_update
BEFORE UPDATE ON domain_events BEGIN SELECT RAISE(ABORT, 'domain events are immutable'); END;
CREATE TRIGGER domain_events_no_delete
BEFORE DELETE ON domain_events BEGIN SELECT RAISE(ABORT, 'domain events are immutable'); END;

CREATE TABLE proposals (
    id TEXT PRIMARY KEY,
    proposer_type TEXT NOT NULL CHECK (proposer_type IN ('agent','human','deterministic_rule')),
    proposer_id TEXT NOT NULL,
    proposal_type TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    patch_json TEXT NOT NULL CHECK (json_valid(patch_json)),
    rationale TEXT NOT NULL,
    citations_json TEXT NOT NULL CHECK (json_valid(citations_json)),
    run_metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(run_metadata_json)),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected','superseded')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    CHECK ((status = 'pending' AND decided_at IS NULL AND decided_by IS NULL) OR status <> 'pending')
) STRICT;

CREATE TABLE fact_evidence (
    fact_type TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    role TEXT NOT NULL DEFAULT 'supports' CHECK (role IN ('supports','contradicts','supersedes')),
    PRIMARY KEY(fact_type, fact_id, evidence_id, role)
) WITHOUT ROWID, STRICT;

CREATE VIEW v_device_catalog AS
SELECT hm.id AS hardware_model_id,
       m.canonical_name AS manufacturer,
       b.canonical_name AS brand,
       df.canonical_name AS family,
       dv.canonical_name AS variant,
       hm.model_code,
       hm.codename
FROM hardware_models hm
JOIN device_variants dv ON dv.id = hm.variant_id
JOIN device_families df ON df.id = dv.family_id
JOIN brands b ON b.id = df.brand_id
JOIN manufacturers m ON m.id = b.manufacturer_id;

CREATE VIEW v_silicon_catalog AS
SELECT sp.id AS part_id,
       sv.canonical_name AS vendor,
       sf.canonical_name AS family,
       sp.part_number,
       sp.marketing_name,
       sr.id AS revision_id,
       sr.revision_code
FROM silicon_parts sp
JOIN silicon_families sf ON sf.id = sp.family_id
JOIN silicon_vendors sv ON sv.id = sf.vendor_id
LEFT JOIN silicon_revisions sr ON sr.part_id = sp.id;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (1, 'canonical_core', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
