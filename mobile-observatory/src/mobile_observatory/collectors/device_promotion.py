from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..repository import CanonicalRepository, normalize_identifier, new_id, utc_now


@dataclass(frozen=True)
class DevicePromotionResult:
    """Counts for one promotion pass; every field is a disjoint outcome for one product."""

    promoted: int = 0                      # a NEW hardware_model was created
    linked_existing: int = 0               # matched an already-existing device by name
    already_linked: int = 0                # idempotent no-op: this product was promoted before
    skipped_not_approved: int = 0          # review_state is 'proposed' or 'rejected'
    skipped_no_model_code: int = 0         # approved, but no genuine model code is observed
    skipped_ambiguous_existing_match: int = 0  # more than one existing device shares the name
    skipped_collision: int = 0             # a different product already claimed this device
    firmware_links_backfilled: int = 0     # product_firmware_releases rows now point at a device


def _strip_brand_prefix(name: str, manufacturer: str) -> str:
    """Remove a leading, whole-token manufacturer name; mirrors identity_bridge._norm."""
    prefix = manufacturer.strip() + " "
    stripped = name
    if name.casefold().startswith(prefix.casefold()) and len(name) > len(prefix):
        stripped = name[len(prefix):]
    stripped = stripped.strip()
    return stripped or name.strip()


def _family_from_variant(variant_name: str) -> str:
    words = variant_name.split()
    return " ".join(words[:2]) if len(words) > 1 else variant_name


def _observed_model_code(connection: sqlite3.Connection, product_id: str) -> tuple[str | None, str | None]:
    """Return (model_code, codename) genuinely observed for this product, or (None, None).

    Only Google Play's authoritative supported-devices "Model" column is treated as a
    real hardware model code. A GSMArena captured device name and a GSMArena page slug
    are not model codes, and a Xiaomi captured "codename" is an internal build codename,
    not a retail model number -- see collectors/adapters/xiaomi_tracker.py. If more than
    one distinct code is on record the identifier is not unique, so it is refused rather
    than guessed.
    """
    row = connection.execute(
        "SELECT evidence_json FROM identity_conclusions WHERE product_id=? AND conclusion='auto_approved'",
        (product_id,),
    ).fetchone()
    if row is None:
        return None, None
    codes: set[str] = set()
    codename: str | None = None
    for entry in json.loads(row["evidence_json"]):
        if entry.get("source") == "google_play_supported_devices":
            for code in entry.get("model_codes") or []:
                code = code.strip()
                if code:
                    codes.add(code)
        if entry.get("source") == "xiaomi_devices_yml" and entry.get("identity"):
            codename = entry["identity"]
    if len(codes) == 1:
        return next(iter(codes)), codename
    return None, codename


def _find_existing_device(connection: sqlite3.Connection, *, manufacturer: str, variant_name: str) -> list[str]:
    rows = connection.execute(
        """SELECT hm.id FROM hardware_models hm
           JOIN device_variants dv ON dv.id = hm.variant_id
           JOIN device_families df ON df.id = dv.family_id
           JOIN brands b ON b.id = df.brand_id
           WHERE b.canonical_name = ? COLLATE NOCASE AND dv.canonical_name = ? COLLATE NOCASE""",
        (manufacturer, variant_name),
    ).fetchall()
    return [str(r["id"]) for r in rows]


def _create_device(
    connection: sqlite3.Connection, *, manufacturer: str, brand: str, family: str,
    variant: str, model_code: str, codename: str | None,
) -> str:
    """Same identity levels as CanonicalRepository.create_device, run inside the
    caller's own transaction instead of opening a nested one."""
    now = utc_now()
    hardware_id = new_id()
    normalized = normalize_identifier(model_code)
    manufacturer_id = CanonicalRepository._find_or_insert(
        connection, select="SELECT id FROM manufacturers WHERE canonical_name = ?",
        select_args=(manufacturer,), insert="INSERT INTO manufacturers VALUES (?, ?, NULL, ?, ?)",
        insert_args=(manufacturer, now, now))
    brand_id = CanonicalRepository._find_or_insert(
        connection, select="SELECT id FROM brands WHERE manufacturer_id = ? AND canonical_name = ?",
        select_args=(manufacturer_id, brand), insert="INSERT INTO brands VALUES (?, ?, ?, ?, ?)",
        insert_args=(manufacturer_id, brand, now, now))
    family_id = CanonicalRepository._find_or_insert(
        connection, select="SELECT id FROM device_families WHERE brand_id = ? AND canonical_name = ?",
        select_args=(brand_id, family), insert="INSERT INTO device_families VALUES (?, ?, ?, NULL, ?, ?)",
        insert_args=(brand_id, family, now, now))
    variant_id = CanonicalRepository._find_or_insert(
        connection, select="SELECT id FROM device_variants WHERE family_id = ? AND canonical_name = ?",
        select_args=(family_id, variant), insert="INSERT INTO device_variants VALUES (?, ?, ?, NULL, NULL, ?, ?)",
        insert_args=(family_id, variant, now, now))
    connection.execute(
        "INSERT INTO hardware_models VALUES (?, ?, ?, ?, ?, ?, ?)",
        (hardware_id, variant_id, model_code, normalized, codename, now, now),
    )
    return hardware_id


def promote_approved_products_to_devices(connection: sqlite3.Connection) -> DevicePromotionResult:
    """Promote every approved source_product into hardware_models, for any manufacturer.

    This is the generalisation repository.py::create_device() itself never needed:
    create_device() already accepts any manufacturer string, it was only ever CALLED
    with a hardcoded "Samsung Electronics" from batch.py's two Samsung seed
    functions. This function is the missing caller for everyone else, gated by
    review_state='approved' and by product_hardware_links so it is safe to replay.
    """
    already_linked_ids = {
        r["product_id"] for r in connection.execute("SELECT product_id FROM product_hardware_links")
    }
    # Two different products can genuinely share one real model code (the same
    # phone sold under two marketing names, or a Google Play listing quirk --
    # e.g. "TECNO CAMON 40 5G" and "TECNO CAMON 40 Pro 5G" both list device
    # model "TECNO CM7"). hardware_models' own UNIQUE constraint is scoped to
    # (variant_id, model_code_normalized), so it would happily create a second
    # device for the second name. This set is the actual duplicate-device guard:
    # a normalized model code may be claimed by at most one NEW device per run,
    # first writer wins in the deterministic (manufacturer, canonical_name) order
    # below, and every subsequent claim of the same code is refused.
    codes_in_use = {
        r["model_code_normalized"] for r in connection.execute("SELECT model_code_normalized FROM hardware_models")
    }
    products = connection.execute(
        "SELECT id, manufacturer, canonical_name, review_state FROM source_products ORDER BY manufacturer, canonical_name"
    ).fetchall()
    counts = {field: 0 for field in DevicePromotionResult.__dataclass_fields__}
    connection.execute("BEGIN IMMEDIATE")
    try:
        for product in products:
            if product["review_state"] != "approved":
                counts["skipped_not_approved"] += 1
                continue
            if product["id"] in already_linked_ids:
                counts["already_linked"] += 1
                continue
            manufacturer = product["manufacturer"]
            variant_name = _strip_brand_prefix(product["canonical_name"], manufacturer)
            existing = _find_existing_device(connection, manufacturer=manufacturer, variant_name=variant_name)
            now = utc_now()
            if len(existing) > 1:
                counts["skipped_ambiguous_existing_match"] += 1
                continue
            if len(existing) == 1:
                hardware_id = existing[0]
                try:
                    connection.execute(
                        """INSERT INTO product_hardware_links
                           (product_id, hardware_model_id, model_code_source, evidence_id, created_at)
                           VALUES (?, ?, ?, NULL, ?)""",
                        (product["id"], hardware_id, "existing_canonical_device_name_match", now),
                    )
                except sqlite3.IntegrityError:
                    counts["skipped_collision"] += 1
                    continue
                counts["linked_existing"] += 1
                continue
            model_code, codename = _observed_model_code(connection, product["id"])
            if not model_code:
                counts["skipped_no_model_code"] += 1
                continue
            normalized_code = normalize_identifier(model_code)
            if normalized_code in codes_in_use:
                counts["skipped_collision"] += 1
                continue
            family = _family_from_variant(variant_name)
            hardware_id = _create_device(
                connection, manufacturer=manufacturer, brand=manufacturer, family=family,
                variant=variant_name, model_code=model_code, codename=codename,
            )
            try:
                connection.execute(
                    """INSERT INTO product_hardware_links
                       (product_id, hardware_model_id, model_code_source, evidence_id, created_at)
                       VALUES (?, ?, ?, NULL, ?)""",
                    (product["id"], hardware_id, "google_play_supported_devices", now),
                )
            except sqlite3.IntegrityError:
                # The device row itself was created inside this same transaction
                # above, so a collision here is a genuine two-products-one-code
                # race, not a replay. Drop the device we just created rather than
                # leave an unlinked, unreachable hardware_model behind.
                connection.execute("DELETE FROM hardware_models WHERE id=?", (hardware_id,))
                counts["skipped_collision"] += 1
                continue
            codes_in_use.add(normalized_code)
            counts["promoted"] += 1

        before = connection.total_changes
        connection.execute(
            """UPDATE product_firmware_releases
               SET hardware_model_id = (
                 SELECT hardware_model_id FROM product_hardware_links
                 WHERE product_hardware_links.product_id = product_firmware_releases.product_id
               )
               WHERE hardware_model_id IS NULL
                 AND product_id IN (SELECT product_id FROM product_hardware_links)"""
        )
        counts["firmware_links_backfilled"] = connection.total_changes - before
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return DevicePromotionResult(**counts)
