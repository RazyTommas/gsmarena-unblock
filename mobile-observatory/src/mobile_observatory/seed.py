from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from .database import Database
from .repository import CanonicalRepository

DEMO_NAMESPACE = uuid.UUID("bf1afadf-8b11-4bf4-b778-f250144395cb")
DEMO_TIME = "2026-09-16T08:40:00Z"


def stable_id(kind: str, value: str) -> str:
    return str(uuid.uuid5(DEMO_NAMESPACE, f"{kind}:{value}"))


def seed_demonstration(db: Database, fixture_path: str | Path) -> None:
    """Seed synthetic fixtures. This function must never be used as live ingestion."""
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    if not str(fixture.get("fixture_notice", "")).startswith("Synthetic demonstration"):
        raise ValueError("refusing to seed a fixture without the demonstration notice")
    con = db.connection
    if con.execute("SELECT count(*) FROM hardware_models").fetchone()[0]:
        return

    source_id = stable_id("source", "demo")
    run_id = stable_id("run", DEMO_TIME)
    artifact_id = stable_id("artifact", "supported_catalog.sample.json")
    digest = hashlib.sha256(Path(fixture_path).read_bytes()).hexdigest()
    con.execute(
        "INSERT INTO sources(id,name,base_url,authority_scope,enabled,created_at) VALUES (?, ?, NULL, 'demonstration', 0, ?)",
        (source_id, "Synthetic demonstration fixture", DEMO_TIME),
    )
    con.execute(
        # fetched 1, accepted 0. The fixture writes firmware_releases and
        # evidence directly and never creates an observation, so a nonzero
        # accepted_count claimed records that do not exist anywhere in the
        # corpus -- which is precisely what integrity.check_corpus flags as
        # run_accepted_but_holds_nothing. The counter was the wrong part.
        """INSERT INTO ingestion_runs VALUES
           (?, ?, ?, ?, 'succeeded', 'demo-seed', '1', 1, 0, 0, NULL)""",
        (run_id, source_id, DEMO_TIME, DEMO_TIME),
    )
    con.execute(
        "INSERT INTO artifacts VALUES (?, ?, ?, ?, 'application/json', NULL, ?, ?, ?)",
        (
            artifact_id,
            source_id,
            run_id,
            digest,
            DEMO_TIME,
            "fixture:supported_catalog.sample.json",
            Path(fixture_path).stat().st_size,
        ),
    )
    evidence_id = stable_id("evidence", "demo")
    con.execute(
        "INSERT INTO evidence VALUES (?, ?, NULL, '$', ?, ?)",
        (evidence_id, artifact_id, fixture["fixture_notice"], DEMO_TIME),
    )

    repo = CanonicalRepository(db)
    hardware_by_code: dict[str, str] = {}
    for item in fixture["devices"]:
        hardware_id = repo.create_device(
            manufacturer=item["manufacturer"],
            brand=item["brand"],
            family=item["commercial_name"],
            variant=item["commercial_name"],
            model_code=item["model_code"],
            codename=item.get("codename"),
        )
        hardware_by_code[item["model_code"]] = hardware_id
        for alias in item.get("aliases", []):
            repo.add_alias(
                entity_type="hardware_model",
                entity_id=hardware_id,
                namespace="demo",
                alias=alias,
            )
        con.execute(
            """INSERT INTO support_assertions
               (id, subject_type, subject_id, status, asserted_at, evidence_id, confidence)
               VALUES (?, 'hardware_model', ?, 'officially_supported', ?, ?, 'authoritative')""",
            (stable_id("support", hardware_id), hardware_id, DEMO_TIME, evidence_id),
        )
        _seed_silicon(con, hardware_id, item["chip"], evidence_id)
        for code in item["market_codes"]:
            _seed_target(con, code)

    for fw in fixture["firmware"]:
        hardware_id = hardware_by_code[fw["model_code"]]
        target_id = stable_id("target", fw["region_code"])
        os_id = stable_id("android", fw["android"])
        con.execute(
            """INSERT OR IGNORE INTO os_releases
               (id, platform, major, display_name) VALUES (?, 'android', ?, ?)""",
            (os_id, int(fw["android"]), f"Android {fw['android']}"),
        )
        release_id = stable_id("firmware", f"{fw['model_code']}:{fw['region_code']}:{fw['build']}")
        con.execute(
            """INSERT INTO firmware_releases
               (id, hardware_model_id, firmware_target_id, build_id, os_release_id,
                security_patch_level, vendor_released_at, first_observed_at,
                last_observed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                release_id,
                hardware_id,
                target_id,
                fw["build"],
                os_id,
                fw["security_patch"],
                fw["release_time"],
                DEMO_TIME,
                DEMO_TIME,
                DEMO_TIME,
            ),
        )
        con.execute(
            "INSERT INTO firmware_release_evidence VALUES (?, ?, 'identity')",
            (release_id, evidence_id),
        )

    # Publish the serving projection, for the same reason run_batch does: a
    # corpus whose rows exist but whose projection was never built reports every
    # device as having no firmware, which is the exact falsehood the projection
    # was added to remove. Seeding is this fixture's ingest, so it ends the same
    # way ingest does.
    from .current_firmware import build as build_current_firmware
    build_current_firmware(db)
    # And the search index, for exactly the same reason and in the same order the
    # batch uses: after the projection that decides the model codes and variants
    # it holds. A seeded corpus whose index was never built is a corpus that
    # `check_corpus` correctly reports as stale -- seeding is this fixture's
    # ingest, so it has to leave behind what an ingest leaves behind.
    from .search_index import build as build_search_index
    build_search_index(db.connection)


def _seed_target(con, code: str) -> None:
    con.execute(
        """INSERT OR IGNORE INTO firmware_targets
           (id, vendor_namespace, target_code, target_kind, display_name, created_at, updated_at)
           VALUES (?, 'demo', ?, ?, ?, ?, ?)""",
        (
            stable_id("target", code),
            code,
            "global" if code == "GLOBAL" else "region",
            code,
            DEMO_TIME,
            DEMO_TIME,
        ),
    )


def _seed_silicon(con, hardware_id: str, chip: dict, evidence_id: str) -> None:
    vendor_id = stable_id("silicon_vendor", chip["vendor"])
    marketing_name = _canonical_chip_name(chip["marketing_name"])
    family_name = _chip_family(chip["vendor"], marketing_name)
    family_id = stable_id("silicon_family", f"{chip['vendor']}:{family_name}")
    part_id = stable_id("silicon_part", f"{chip['vendor']}:{chip['part_number']}")
    con.execute(
        "INSERT OR IGNORE INTO silicon_vendors VALUES (?, ?, ?)",
        (vendor_id, chip["vendor"], DEMO_TIME),
    )
    con.execute(
        "INSERT OR IGNORE INTO silicon_families VALUES (?, ?, NULL, ?, ?)",
        (family_id, vendor_id, family_name, DEMO_TIME),
    )
    con.execute(
        """INSERT OR IGNORE INTO silicon_parts
           (id, family_id, part_number, marketing_name, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (part_id, family_id, chip["part_number"], marketing_name, DEMO_TIME),
    )
    con.execute(
        """INSERT INTO hardware_silicon
           (hardware_model_id, part_id, role, evidence_id, valid_from)
           VALUES (?, ?, 'primary_soc', ?, '')""",
        (hardware_id, part_id, evidence_id),
    )


def _canonical_chip_name(value: str) -> str:
    """Keep device-bin branding out of the identity of a shared silicon part."""
    return value.removesuffix(" for Galaxy")


def _chip_family(vendor: str, marketing_name: str) -> str:
    if vendor == "Qualcomm" and marketing_name.startswith("Snapdragon 8"):
        return "Snapdragon 8"
    if vendor == "Samsung" and marketing_name.startswith("Exynos"):
        return "Exynos"
    if vendor == "MediaTek" and marketing_name.startswith("Dimensity"):
        return "Dimensity"
    if vendor == "Google" and "Tensor" in marketing_name:
        return "Tensor"
    return marketing_name
