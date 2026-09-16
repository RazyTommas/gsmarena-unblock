PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE identity_conclusions (
  product_id TEXT PRIMARY KEY REFERENCES source_products(id),
  conclusion TEXT NOT NULL CHECK(conclusion IN ('auto_approved','ambiguous','insufficient_evidence')),
  confidence TEXT NOT NULL CHECK(confidence IN ('authoritative','high','medium','low')),
  method TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  rationale TEXT NOT NULL,
  candidates_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(candidates_json)),
  evidence_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_json)),
  concluded_at TEXT NOT NULL
) STRICT;

CREATE TABLE observed_product_silicon (
  product_id TEXT PRIMARY KEY REFERENCES source_products(id),
  raw_chipset TEXT NOT NULL,
  vendor TEXT,
  part_number TEXT,
  marketing_name TEXT,
  evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
  confidence TEXT NOT NULL CHECK(confidence IN ('authoritative','high','medium','low')),
  observed_at TEXT NOT NULL
) STRICT;

CREATE INDEX identity_conclusion_state_idx ON identity_conclusions(conclusion, confidence);
CREATE INDEX observed_product_silicon_vendor_idx ON observed_product_silicon(vendor, part_number);

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(4,'identity_enrichment',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
