from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from ..repository import (CanonicalRepository, clean_device_name, normalize_identifier,
                          new_id, usable_device_name, utc_now)


@dataclass(frozen=True)
class DevicePromotionResult:
    """Counts for one promotion pass; every field is a disjoint outcome for one product."""

    promoted: int = 0                      # a NEW hardware_model was created
    linked_existing: int = 0               # matched an already-existing device by code or name
    linked_by_code: int = 0                # same model code as an existing device, spelled differently
    disambiguated_name: int = 0            # distinct code sharing a marketing name; name qualified
    already_linked: int = 0                # idempotent no-op: this product was promoted before
    skipped_not_approved: int = 0          # review_state is 'proposed' or 'rejected'
    skipped_no_model_code: int = 0         # approved, but no genuine model code is observed
    skipped_ambiguous_existing_match: int = 0  # more than one existing device shares the name
    skipped_collision: int = 0             # a different product already claimed this device
    firmware_links_backfilled: int = 0     # product_firmware_releases rows now point at a device


# A qualifier a vendor puts after its own name in some catalogues. Google Play
# lists the same manufacturer as both "Tecno" and "Tecno Mobile", so a marketing
# name can arrive as "TECNO Mobile SPARK 30 Pro". Stripping only the brand left
# "Mobile SPARK 30 Pro", which then failed to match the device already called
# "SPARK 30 Pro" and the product was refused -- 20 firmware rows unreachable
# because of one leftover word.
_VENDOR_QUALIFIER = re.compile(r"^(mobile|mobility)\s+", re.I)


def _strip_brand_prefix(name: str, manufacturer: str) -> str:
    """Remove a leading, whole-token manufacturer name; mirrors identity_bridge._norm."""
    prefix = manufacturer.strip() + " "
    stripped = name
    if name.casefold().startswith(prefix.casefold()) and len(name) > len(prefix):
        stripped = name[len(prefix):]
        # Only after the brand matched, so a device genuinely called
        # "Mobile something" by a different vendor keeps its name.
        without_qualifier = _VENDOR_QUALIFIER.sub("", stripped).strip()
        if without_qualifier:
            stripped = without_qualifier
    stripped = stripped.strip()
    return stripped or name.strip()


def _family_from_variant(variant_name: str) -> str:
    words = variant_name.split()
    return " ".join(words[:2]) if len(words) > 1 else variant_name


def _observed_model_code(connection: sqlite3.Connection, product_id: str) -> tuple[str | None, str | None]:
    """Return (model_code, codename) genuinely observed for this product, or (None, None).

    TWO sources are accepted, and the distinction between them is the whole point.

    1. Google Play's supported-devices "Model" column -- authoritative, and the
       original sole source.
    2. A VENDOR FIRMWARE MODEL CODE: an approved identity in the `model_code`
       namespace. Transsion ships firmware as CM8-15.1.3.115SP05-GL001PF001AZ;
       CM8 is the identifier the vendor organises its own firmware under, carried
       identically by FRBox, naijarom and Google's OTA endpoint. It is observed,
       not derived, and refusing it stranded 901 reviewed products outside the
       device catalogue while their firmware sat in the corpus.

    Still NOT accepted, for the same reasons as before: a GSMArena device name or
    page slug is not a model code, and a Xiaomi "codename" is an internal build
    codename rather than a retail model number -- see xiaomi_tracker.py. Those are
    identifiers for something other than the hardware.

    If more than one distinct code is on record the identifier is not unique, so it
    is refused rather than guessed.
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

    # No Google Play code. Fall back to a vendor firmware model code, which is
    # only ever present for sources that key on one (identity_bridge records it
    # in the `model_code` namespace). Still requires a UNIQUE approved identity:
    # two codes means we do not know which device this is.
    vendor = {
        r["source_value"].strip()
        for r in connection.execute(
            """SELECT source_value FROM source_identity_registry
               WHERE product_id=? AND namespace='model_code'
                 AND resolution_state='approved' AND source_value IS NOT NULL""",
            (product_id,),
        )
        if r["source_value"] and r["source_value"].strip()
    }
    if len(vendor) == 1:
        return next(iter(vendor)), codename
    return None, codename


def _code_key(model_code: str, manufacturer: str) -> str:
    """A model code with any leading brand removed, for comparison only.

    Play writes "TECNO CM5"; the product and the OTA check-in write "CM5". They
    are one code. The stored value is never rewritten -- only the comparison is
    prefix-insensitive -- because the code a source published is evidence.
    """
    value = (model_code or "").strip()
    brand = (manufacturer or "").strip()
    if brand:
        for separator in (" ", "-", "_"):
            prefix = brand + separator
            if value.casefold().startswith(prefix.casefold()) and len(value) > len(prefix):
                value = value[len(prefix):]
                break
    return normalize_identifier(value)


def _same_device_name(left: str, right: str) -> bool:
    """Do two device names refer to the same phone?

    Folded on case and whitespace only. Anything looser would merge
    "CAMON 40 5G" with "CAMON 40 Pro 5G", which are different phones that
    happen to share a Google Play model code.
    """
    normalise = lambda value: " ".join((value or "").replace("\u00a0", " ").split()).casefold()
    return normalise(left) == normalise(right)


def _record_unresolved(connection: sqlite3.Connection, product_id: str, owner_id: str,
                       model_code: str, owner_name: str, claimed_name: str, now: str) -> None:
    """Write down a refusal, so it is auditable rather than a number in a log.

    source_data_corrections is the corpus's existing place for "we decided
    something about this row and here is the before and after". A refusal is
    exactly that: the product asked to be this device and was told no.
    """
    connection.execute(
        """INSERT OR REPLACE INTO source_data_corrections
             (id, entity_type, entity_id, reason, before_json, after_json, evidence_id, recorded_at)
           VALUES (?,?,?,?,?,?,NULL,?)""",
        (new_id(), "source_product", product_id,
         "model_code_claimed_by_a_differently_named_device",
         json.dumps({"model_code": model_code, "claimed_name": claimed_name}, sort_keys=True),
         json.dumps({"held_by_device": owner_id, "held_by_name": owner_name}, sort_keys=True),
         now))


def _corroborated_name(connection: sqlite3.Connection, product_id: str,
                       *, code_hint: str = "", brand_hint: str = "") -> str | None:
    """The commercial name an approved conclusion matched this product's CODE to.

    Only set when the conclusion matched on the model code and the catalogue
    gave exactly one name for it; a code listed under several names concludes
    ambiguous and never reaches promotion.
    """
    row = connection.execute(
        "SELECT evidence_json FROM identity_conclusions "
        "WHERE product_id=? AND conclusion='auto_approved'", (product_id,)).fetchone()
    if row is None:
        return None
    for entry in json.loads(row["evidence_json"]):
        if entry.get("source") == "google_play_supported_devices" and entry.get("matched_on") == "model_code":
            # device_name is set only when the review found exactly ONE usable
            # name -- not the brand, not the code back again, not a name that
            # covers several devices.
            name = clean_device_name(entry.get("device_name") or "")
            if name:
                return name
            # Evidence written before device_name existed carries only
            # marketing_names. Applying the same usability rule at read time
            # lets an older corpus heal on the next batch instead of refusing
            # its own devices: without this, a product whose conclusion was
            # already auto_approved fell back to its bare code, stopped matching
            # the device it belongs to, and was recorded as a refusal. Measured
            # on a corpus carrying older evidence: 53 such refusals.
            names = [clean_device_name(n) for n in (entry.get("marketing_names") or [])]
            usable = [n for n in names
                      if usable_device_name(n, code=code_hint, brand=brand_hint)]
            if len(usable) == 1:
                return usable[0]
    return None


def _from_play(connection: sqlite3.Connection, product_id: str) -> bool:
    """Did the accepted code come from Google Play, or from the vendor's firmware?

    Recorded per link so provenance is never ambiguous: "we promoted this because
    Google says so" and "we promoted this because the vendor ships firmware under
    this name" are different claims and the corpus should be able to tell them
    apart afterwards.
    """
    row = connection.execute(
        "SELECT evidence_json FROM identity_conclusions "
        "WHERE product_id=? AND conclusion='auto_approved'", (product_id,)).fetchone()
    if row is None:
        return False
    for entry in json.loads(row["evidence_json"]):
        if entry.get("source") == "google_play_supported_devices" and entry.get("model_codes"):
            return True
    return False

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
    # Keyed prefix-insensitively. 131 hardware_models store the code WITH the
    # brand on the front ("TECNO CM5") because Google Play's Model column
    # carries it there, while the product and the OTA feed carry the bare "CM5".
    # Comparing the normalized strings made those different codes, so the code
    # path did not see the device already existed, the name path found it, and
    # the link then failed -- taking the product's entire firmware history with
    # it. Measured: 196 approved products produced no device and 1,287 firmware
    # rows were left unreachable.
    device_by_code: dict[str, str] = {}
    for row in connection.execute(
            """SELECT hm.id, hm.model_code, dc.brand FROM hardware_models hm
                 JOIN v_device_catalog dc ON dc.hardware_model_id = hm.id"""):
        device_by_code.setdefault(_code_key(row["model_code"], row["brand"]), row["id"])

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
            # A product named by a bare hardware code ("X6962") becomes a device
            # named by the commercial product the catalogue maps that code to
            # ("ZERO Flip"). Without this a promoted Transsion device would be
            # called X6962 in the grid, which is the code the user is trying to
            # look UP, not a name to show them.
            variant_name = _strip_brand_prefix(
                _corroborated_name(connection, product["id"],
                                   code_hint=product["canonical_name"],
                                   brand_hint=manufacturer) or product["canonical_name"], manufacturer)
            model_code, codename = _observed_model_code(connection, product["id"])
            now = utc_now()

            def link(hardware_id: str, source: str) -> bool:
                """Attach this product to a device. Never silently drops it."""
                connection.execute(
                    """INSERT INTO product_hardware_links
                       (product_id, hardware_model_id, model_code_source, evidence_id, created_at)
                       VALUES (?, ?, ?, NULL, ?)""",
                    (product["id"], hardware_id, source, now))
                return True

            # 1. THE CODE FIRST. It is the stronger identity, and matching it
            #    prefix-insensitively is what stops "CM5" standing up a duplicate
            #    of the device already stored as "TECNO CM5" -- or, as happened
            #    before this, being dropped entirely along with its firmware.
            code_key = _code_key(model_code, manufacturer) if model_code else None
            if code_key and code_key in device_by_code:
                owner = device_by_code[code_key]
                owner_name = connection.execute(
                    "SELECT variant FROM v_device_catalog WHERE hardware_model_id=?", (owner,)).fetchone()
                owner_variant = owner_name["variant"] if owner_name else ""
                if _same_device_name(owner_variant, variant_name):
                    # Same phone, spelled differently: "TECNO CM5" and "CM5".
                    link(owner, "existing_canonical_device_code_match")
                    counts["linked_by_code"] += 1
                    counts["linked_existing"] += 1
                    continue
                # DIFFERENT phones reporting the same code. TECNO CAMON 40 5G
                # and CAMON 40 Pro 5G both list Google Play model "TECNO CM7";
                # linking the second to the first would answer for a phone with
                # another phone's firmware. Refused -- and recorded, because the
                # previous version of this refusal was a counter nobody read
                # while the product's whole firmware history went unreachable.
                counts["skipped_collision"] += 1
                _record_unresolved(connection, product["id"], owner, model_code,
                                   owner_variant, variant_name, now)
                continue

            # 2. No code at all: a name match can still identify the device, but
            #    nothing here can key a NEW one.
            if not model_code:
                existing = _find_existing_device(connection, manufacturer=manufacturer, variant_name=variant_name)
                if len(existing) > 1:
                    counts["skipped_ambiguous_existing_match"] += 1
                    continue
                if len(existing) == 1:
                    link(existing[0], "existing_canonical_device_name_match")
                    counts["linked_existing"] += 1
                    continue
                counts["skipped_no_model_code"] += 1
                continue

            # 3. A code nothing has claimed. If the NAME is already taken, this
            #    is a different SKU that shares a marketing name -- Play lists
            #    eleven codes as "SPARK 7" -- not the same phone. It gets its own
            #    device, qualified by the code, so a user who knows their code
            #    can still find exactly their phone. Collapsing them would hide
            #    ten devices and answer for all of them with one SKU's firmware.
            existing = _find_existing_device(connection, manufacturer=manufacturer, variant_name=variant_name)
            if existing:
                variant_name = f"{variant_name} ({model_code})"
                counts["disambiguated_name"] += 1
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
                    (product["id"], hardware_id,
                     "google_play_supported_devices" if _from_play(connection, product["id"])
                     else "vendor_firmware_model_code", now),
                )
            except sqlite3.IntegrityError:
                # The device row itself was created inside this same transaction
                # above, so a collision here is a genuine two-products-one-code
                # race, not a replay. Drop the device we just created rather than
                # leave an unlinked, unreachable hardware_model behind.
                connection.execute("DELETE FROM hardware_models WHERE id=?", (hardware_id,))
                counts["skipped_collision"] += 1
                continue
            # Index the new device by its code so a later product carrying the
            # same code links to it instead of creating a twin.
            device_by_code[code_key] = hardware_id
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
