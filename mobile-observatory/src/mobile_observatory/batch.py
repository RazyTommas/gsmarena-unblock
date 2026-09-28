from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

from .collectors.adapters.apple_ipsw import AppleIpswFirmwareAdapter
from .collectors.adapters.frbox_transsion import FrboxTranssionCatalogAdapter
from .collectors.adapters.naijarom_transsion import NaijaromTranssionAdapter
from .collectors.adapters.tecno_ota_checkin import TecnoOtaCheckinAdapter
from .collectors.adapters.mifirm_archive import MifirmArchiveAdapter
from .collectors.adapters.samsung_aspl import SamsungAsplAdapter
from .collectors.adapters.samsung_fota import SamsungFotaArtifactAdapter
from .collectors.adapters.samsung_history import SamsungFotaHistoryAdapter
from .collectors.adapters.tecno_security import TecnoSecurityPatchAdapter
from .collectors.adapters.xiaomi_tracker import XiaomiFirmwareTrackerAdapter
from .collectors.importer import IngestionImporter
from .collectors.pipeline import CollectorPipeline
from .dedupe import merge_confirmed_duplicates
from .collectors.promotion import SamsungFirmwarePromoter
from .collectors.device_promotion import promote_approved_products_to_devices
from .database import Database
from .repository import CanonicalRepository, normalize_identifier
from .current_firmware import ProjectionError, build as build_current_firmware
from .integrity import check_corpus, summarise
from .identity_bridge import rebuild_identity_registry
from .silence import STATUS_SILENT, detect_silence
from .enrichment import (automate_identity_review, enrich_canonical_silicon,
                         enrich_observed_hardware_silicon,
                         enrich_gsmarena_hardware_silicon, import_mediatek_catalog, import_security_catalog,
                         promote_approved_product_observations, write_agent_review_bundle)


def seed_reviewed_samsung(db: Database, profiles_path: Path) -> int:
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))["profiles"]
    repo = CanonicalRepository(db)
    created = 0
    for item in profiles:
        exists = db.connection.execute(
            "SELECT 1 FROM hardware_models WHERE model_code_normalized=?",
            (normalize_identifier(item["model_code"]),),
        ).fetchone()
        if exists:
            continue
        words = item["commercial_name"].split()
        family = " ".join(words[:2]) if len(words) > 1 else item["commercial_name"]
        repo.create_device(manufacturer="Samsung Electronics", brand="Samsung", family=family,
                           variant=item["commercial_name"], model_code=item["model_code"])
        created += 1
    return created


def seed_samsung_history_identities(db: Database, history_path: Path) -> int:
    repo = CanonicalRepository(db); created = 0
    with history_path.open(encoding="utf-8-sig") as handle:
        identities = {(row["model"].strip(), row["device"].strip()) for row in csv.DictReader(handle)}
    for model_code, device in sorted(identities):
        if db.connection.execute("SELECT 1 FROM hardware_models WHERE model_code_normalized=?",
                                 (normalize_identifier(model_code),)).fetchone():
            continue
        commercial = device.removeprefix("Samsung ").strip()
        family = " ".join(commercial.split()[:2])
        repo.create_device(manufacturer="Samsung Electronics", brand="Samsung", family=family,
                           variant=commercial, model_code=model_code)
        created += 1
    return created


def run_batch(*, data_dir: Path, legacy_root: Path, fixture_root: Path) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger = data_dir / "ledger"
    db = Database.migrated(data_dir / "corpus.sqlite")
    pipeline = CollectorPipeline(ledger)
    importer = IngestionImporter(ledger, db.connection)
    results: dict[str, object] = {}
    try:
        results["reviewed_samsung_devices_created"] = seed_reviewed_samsung(
            db, fixture_root / "samsung" / "reviewed_profiles.json")
        samsung_history = legacy_root / "T005-fota-modem" / "samsung_fota.csv"
        results["samsung_history_devices_created"] = seed_samsung_history_identities(db, samsung_history)
        adapters = [
            (XiaomiFirmwareTrackerAdapter(legacy_root / "xiaomi-tracker" / "latest.yml"), "xiaomi-captured-history"),
            (TecnoSecurityPatchAdapter(legacy_root / "tecno-security-comprehensive" / "tecno-security-updates.csv"), "tecno-captured"),
            (SamsungFotaArtifactAdapter(fixture_root / "samsung" / "fota_sm-s938b_ilo.xml",
                                        model_code="SM-S938B", csc="ILO", observed_at="2026-09-16T06:00:00Z"),
             "samsung-captured-sm-s938b-ilo"),
            (SamsungFotaHistoryAdapter(samsung_history), "samsung-fota-history-captured"),
            # Android patch level per build. Without this nothing in the corpus can be
            # adjudicated -- a device with no patch level is undecidable, not safe.
            (SamsungAsplAdapter(legacy_root / "samsung-aspl" / "samsung_aspl.csv"),
             "samsung-aspl-captured"),
            # Xiaomi firmware HISTORY. The tracker adapter above carries only the
            # LATEST build per device (4,886 rows); this is the archive behind it,
            # re-parsed from the 337 captured mifirm.net model pages. The legacy
            # corpus held 21,843 of these and collapsed fastboot/recovery into a
            # single row -- see MifirmArchiveAdapter for the measured difference.
            (MifirmArchiveAdapter(legacy_root / "mifirm-archive" / "mifirm-firmware-archive.csv"),
             "mifirm-archive-captured"),
            # Apple has no Android-style patch level (no -01/-05 tier, no aspl_month);
            # this is firmware_release only. See apple_ipsw.py's docstring for why
            # security_patch_publication is deliberately not attempted here.
            (AppleIpswFirmwareAdapter(legacy_root / "ipsw-me" / "ipsw-me-firmware.csv"),
             "ipsw-me-captured"),
            # The three Transsion sources below were ingested by hand and never
            # wired in here. The adapters existed, the captured artifacts were
            # committed, and the batch simply did not list them -- so the corpus
            # on this machine could not be reproduced from a clone. Measured: a
            # fresh checkout rebuilt to 228 devices and 90,143 observations
            # against 865 and 96,319, with Infinix and itel absent entirely,
            # because these are the sources that carry them.
            (FrboxTranssionCatalogAdapter(
                legacy_root / "frbox-transsion-catalog" / "transsion_frbox_catalog.csv"),
             "frbox-transsion-captured"),
            (NaijaromTranssionAdapter(
                legacy_root / "naijarom-transsion" / "naijarom-merged.csv"),
             "naijarom-merged-captured"),
            # One source, two captures: the same endpoint and response shape
            # answering for different brands, which is why f2de0ff gave them one
            # source id rather than three.
            (TecnoOtaCheckinAdapter(
                legacy_root / "tecno-ota-checkin" / "tecno_ota_checkin.csv"),
             "ota-checkin-tecno"),
            (TecnoOtaCheckinAdapter(
                legacy_root / "infinix-itel-ota-checkin" / "infinix_itel_ota_checkin.csv"),
             "ota-checkin-infinix-itel"),
        ]
        for adapter, run_id in adapters:
            result = pipeline.run(adapter, run_id)
            imported = importer.import_run(adapter.source_id, run_id)
            results[adapter.source_id] = {"state": result.run.state, **imported}
        promotion = SamsungFirmwarePromoter(db.connection).promote_pending()
        results["samsung_promotion"] = {
            "promoted": promotion.promoted, "skipped": promotion.skipped, "events": promotion.events}
        results["identity_bridge"] = rebuild_identity_registry(
            db.connection, legacy_root / "T004-gsmarena-slugs" / "gsm_specs.csv")
        decisions = []
        if (data_dir / 'local.sqlite').is_file():
            with sqlite3.connect(data_dir / 'local.sqlite') as local:
                local.row_factory = sqlite3.Row
                if local.execute("SELECT 1 FROM sqlite_schema WHERE name='identity_decisions'").fetchone():
                    decisions = [dict(r) for r in local.execute('SELECT * FROM identity_decisions')]
        results["identity_automation"] = automate_identity_review(
            db.connection, devices_yml=legacy_root / "xiaomi-tracker" / "devices.yml",
            specs_csv=legacy_root / "T004-gsmarena-slugs" / "gsm_specs.csv",
            google_play_csv=legacy_root / "google-play-devices" / "supported_devices.csv", decisions=decisions)
        # Must run on EVERY batch: the TECNO source re-emits both spellings each time,
        # so a merge done once is undone by the next ingest.
        results["dedupe"] = merge_confirmed_duplicates(
            db.connection, legacy_root / "google-play-devices" / "supported_devices.csv")
        results["product_promotion"] = promote_approved_product_observations(db.connection)
        results["device_promotion"] = vars(promote_approved_products_to_devices(db.connection))
        results["canonical_silicon"] = enrich_canonical_silicon(
            db.connection, legacy_root / "cross-reference" / "device-chipset-cve-xref.csv")
        # Fills only the gaps enrich_canonical_silicon left empty; never overwrites
        # its higher-authority rows (see enrich_gsmarena_hardware_silicon docstring).
        results["gsmarena_canonical_silicon"] = enrich_gsmarena_hardware_silicon(
            db.connection, legacy_root / "T004-gsmarena-slugs" / "gsm_specs.csv")
        # Last of the silicon steps, so it only fills what the higher-authority
        # captures left empty.
        results["observed_silicon"] = enrich_observed_hardware_silicon(db.connection)
        results["security_catalog"] = import_security_catalog(
            db.connection, legacy_root / "google-asb-cves" / "android-security-bulletin-cves.csv")
        results["mediatek_security_catalog"] = import_mediatek_catalog(
            db.connection, legacy_root / "mediatek-cve-chipsets" / "mediatek-cve-chipsets.csv")
        results["agent_review_bundle"] = write_agent_review_bundle(
            db.connection, data_dir / "agent-review")
        # Last derivation step, because it reads what every step above wrote.
        # A failure here leaves the previously published generation serving --
        # stale rather than absent -- so it is reported and does not abort the
        # run, whose observations are already committed and correct.
        try:
            report = build_current_firmware(db)
            results["current_firmware"] = {
                "generation": report.generation, "rows": report.rows,
                "devices": report.devices, "canonical_rows": report.canonical_rows,
                "evidence_rows": report.evidence_rows, "digest": report.digest}
        except ProjectionError as error:
            results["current_firmware"] = {"error": str(error), "published": False}
        # Check the corpus we just produced, against the corpus -- not against a
        # fresh in-memory schema, which is what every existing validator did and
        # is why none of them could ever fail. Reported, not raised: the
        # violations that exist today describe already-ingested data, and
        # aborting the run repairs none of it.
        findings = check_corpus(db.connection)
        results["integrity"] = {"summary": summarise(findings),
                                "findings": [f.as_dict() for f in findings]}
        # Advisory only: never gates or alters the run above. See silence.py
        # and docs/SOURCE_SILENCE_DETECTION.md. `main()` below turns a
        # "silent" finding into a nonzero process exit and a logged ALARM,
        # which is the only part of this that can reach anyone -- and only
        # if the scheduled invocation's exit code is wired to alerting.
        results["silence"] = detect_silence(db.connection)
        results["totals"] = {
            "devices": db.connection.execute("SELECT count(*) FROM hardware_models").fetchone()[0],
            "observations": db.connection.execute("SELECT count(*) FROM observations").fetchone()[0],
            "firmware_releases": db.connection.execute("SELECT count(*) FROM firmware_releases").fetchone()[0],
            "product_firmware_releases": db.connection.execute(
                "SELECT count(*) FROM product_firmware_releases").fetchone()[0],
            "devices_with_current_firmware": db.connection.execute(
                "SELECT count(DISTINCT hardware_model_id) FROM device_current_firmware").fetchone()[0],
            "radar_events": db.connection.execute("SELECT count(*) FROM domain_events").fetchone()[0],
        }
        return results
    finally:
        db.close()


def main() -> None:
    from .batch_logging import configure_batch_logging

    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Replay captured mobile-source artifacts into a local corpus")
    parser.add_argument("--data-dir", default=".observatory-data")
    parser.add_argument("--legacy-root", default=str(root.parent / "crawler" / "relay" / "results"))
    parser.add_argument("--log-file", default=None,
                         help="Defaults to <data-dir>/batch.log. See docs/SOURCE_SILENCE_DETECTION.md.")
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    log_path = Path(args.log_file) if args.log_file else data_dir / "batch.log"
    logger = configure_batch_logging(log_path)

    logger.info("batch starting data_dir=%s legacy_root=%s", data_dir, args.legacy_root)
    try:
        results = run_batch(data_dir=data_dir, legacy_root=Path(args.legacy_root), fixture_root=root / "fixtures")
    except Exception:
        logger.exception("batch failed before completion")
        raise
    print(json.dumps(results, indent=2, sort_keys=True))
    logger.info("batch finished totals=%s", json.dumps(results.get("totals", {})))

    silent = [f for f in results.get("silence", []) if f["status"] == STATUS_SILENT]
    for finding in silent:
        logger.warning(
            "ALARM source silent (advisory): %s last_activity=%s expected_interval_hours=%.2f overdue_hours=%.2f",
            finding["source_name"], finding["last_activity_at"],
            finding["expected_interval_hours"], finding["overdue_hours"],
        )
    if silent:
        logger.warning(
            "%d source(s) went silent. This process's exit code (2) is the only alarm that "
            "reaches anyone outside this log and the admin health API -- wire it to your "
            "scheduler's failure notification (systemd OnFailure=, cron MAILTO, monitoring "
            "check-on-exit-code) if you want a person actually paged.", len(silent),
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
