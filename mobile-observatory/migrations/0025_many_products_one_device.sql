BEGIN;

-- A canonical device may be described by more than one source product.
--
-- product_hardware_links.hardware_model_id was UNIQUE, so a device could be
-- claimed by exactly ONE product. That is backwards for a corpus whose canonical
-- layer is defined by corroboration: two sources naming the same device is the
-- evidence the layer exists to collect, and the constraint made the second one
-- an error.
--
-- It did more than refuse a link. device_promotion catches the IntegrityError,
-- counts skipped_collision, and moves on -- so the product and ALL ITS FIRMWARE
-- were discarded silently. Measured on the live corpus after the code-keyed
-- identity rule landed: 196 of 584 approved products produced no device, 1,287
-- product_firmware_releases rows were left with no hardware_model_id, and 46
-- devices displayed no firmware at all while their own builds sat in the corpus
-- unreachable. TECNO CAMON 40 showed nothing with 31 of its builds present.
--
-- The cause was a brand prefix, which is the same thing this work already fixed
-- once on the read side: 131 hardware_models store the prefixed code
-- ("TECNO CM5") because Google Play's Model column carries it, while the
-- product and the OTA feed carry the bare "CM5". codes_in_use compares
-- normalize_identifier() of each, so CM5 and TECNO CM5 did not match, the code
-- path did not recognise the device already existed, and the name path found it
-- and then could not link to it.
--
-- product_id stays the primary key: one product still describes exactly one
-- device. What is dropped is the reverse restriction.
CREATE TABLE product_hardware_links_new (
  product_id        TEXT PRIMARY KEY REFERENCES source_products(id),
  hardware_model_id TEXT NOT NULL REFERENCES hardware_models(id),
  model_code_source TEXT NOT NULL,
  evidence_id       TEXT REFERENCES evidence(id),
  created_at        TEXT NOT NULL
) STRICT;

INSERT INTO product_hardware_links_new
  (product_id, hardware_model_id, model_code_source, evidence_id, created_at)
SELECT product_id, hardware_model_id, model_code_source, evidence_id, created_at
  FROM product_hardware_links;

DROP TABLE product_hardware_links;
ALTER TABLE product_hardware_links_new RENAME TO product_hardware_links;

-- The lookup the promotion path and every per-device join actually make. It
-- replaces the index the UNIQUE constraint used to provide.
CREATE INDEX product_hardware_links_model_idx
  ON product_hardware_links(hardware_model_id);

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(25,'many_products_one_device',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
