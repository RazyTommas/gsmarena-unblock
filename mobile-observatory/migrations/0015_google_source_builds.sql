PRAGMA foreign_keys=ON;
BEGIN IMMEDIATE;
-- A source-code build target list is not an OTA shipment or support assertion.
CREATE TABLE source_build_catalog (
 id TEXT PRIMARY KEY,
 source_id TEXT NOT NULL REFERENCES sources(id),
 build_id TEXT NOT NULL,
 source_tag TEXT NOT NULL,
 version_label TEXT NOT NULL,
 android_version TEXT,
 supported_products_json TEXT NOT NULL CHECK(json_valid(supported_products_json)),
 security_patch_level TEXT,
 evidence_id TEXT NOT NULL REFERENCES evidence(id),
 source_url TEXT NOT NULL,
 observed_at TEXT NOT NULL,
 UNIQUE(source_id,build_id,source_tag,evidence_id)
) STRICT;
CREATE TABLE source_build_product_links (
 build_id TEXT NOT NULL REFERENCES source_build_catalog(id),
 product_id TEXT NOT NULL REFERENCES source_products(id),
 match_method TEXT NOT NULL,
 confidence TEXT NOT NULL CHECK(confidence IN ('high','medium','low')),
 PRIMARY KEY(build_id,product_id)
) WITHOUT ROWID,STRICT;
CREATE INDEX source_build_product_lookup_idx ON source_build_product_links(product_id);
INSERT INTO schema_migrations(version,name,applied_at)
VALUES(15,'google_source_builds',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
