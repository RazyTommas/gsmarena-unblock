from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
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
from . import changesets, corpus_identity, retention, search_index
from .dedupe import merge_confirmed_duplicates
from .collectors.promotion import SamsungFirmwarePromoter
from .adjudication import adjudicate_unresolvable_products, reopen_stale_adjudications
from .collectors.device_promotion import promote_approved_products_to_devices
from .batch_logging import DEFAULT_HEARTBEAT_SECONDS, ProgressLog
from .database import Database
from .repository import CanonicalRepository, normalize_identifier
from .current_firmware import ProjectionError, build as build_current_firmware
from .integrity import check_corpus, summarise
from .identity_backfill import (approve_catalog_confirmed_identities,
                                approve_stem_corroborated_identities)
from .identity_bridge import rebuild_identity_registry, refresh_observation_link_states
from .silence import STATUS_SILENT, detect_silence
from .worker_lock import exclusive_worker
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


def first_build(connection) -> bool:
    """Is this corpus being created by this run?

    A first build's inverse is `rm corpus.sqlite`, not a changeset -- and its
    changeset would be the size of the corpus, because every row in it is an
    insert. changesets.py's own docstring has said so since it was written; this
    is that argument applied rather than only recorded.

    Asked of `sources`, not of the file: `Database(...)` CREATES an empty file
    before anything is read, so "the file does not exist" is already false by
    the time anyone can ask.
    """
    present = connection.execute(
        "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name='sources'"
    ).fetchone()[0]
    if not present:
        return True
    return connection.execute("SELECT count(*) FROM sources").fetchone()[0] == 0


def run_batch(*, data_dir: Path, legacy_root: Path, fixture_root: Path,
              record_changeset: bool = True, progress=None) -> dict:
    """Run the ingest, recording an invertible diff of what it changed.

    The recording wraps the migrations too, so a run that alters the schema says
    so -- a changeset carries rows, not DDL, and a caller about to revert needs
    to know its undo is partial BEFORE it runs. See changesets.py.

    `record_changeset=False` runs the ingest with no session attached. It exists
    for the tests that need the un-recorded path and for a first build, where the
    changeset is the size of the corpus and its inverse is `rm corpus.sqlite`.

    `progress` is a batch_logging.ProgressLog. None means an unlogged run -- a
    ProgressLog(None) is built so `_ingest` has one object to call either way,
    rather than guarding every phase with `if progress`.
    """
    progress = progress if progress is not None else ProgressLog(None)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "corpus.sqlite")
    support = changesets.session_support()
    recording = None
    # BEFORE the migrations, for two separate reasons.
    #
    # `cold` decides whether to record at all: a first build's changeset is the
    # whole corpus and its inverse is `rm corpus.sqlite`.
    #
    # `schema_before` is handed to the session so `schema_changed` still reports
    # a migrating run truthfully even though the DDL now happens outside the
    # session. DDL INSIDE a session does not merely go unrecorded -- it returned
    # SQLITE_SCHEMA (rc=17) from sqlite3session_changeset and produced no
    # changeset at all. See changesets.record_changes.
    cold = first_build(db.connection)
    schema_before = changesets.schema_digest(db.connection)
    db.apply_migrations()
    migrated = schema_before != changesets.schema_digest(db.connection)
    # db.close() lives HERE and not in _ingest, which is where it used to be.
    # Asking the session for its changeset is a call against the open `sqlite3 *`
    # handle, so the connection has to outlive the recording -- closing it inside
    # the ingest would have handed the session extension a freed handle on every
    # run, which is not an error anybody gets told about.
    try:
        if record_changeset and support.available and not cold:
            with changesets.record_changes(
                    db.connection, schema_digest_before=schema_before) as recording:
                results = _ingest(db, data_dir=data_dir, legacy_root=legacy_root,
                                  fixture_root=fixture_root, progress=progress)
        else:
            results = _ingest(db, data_dir=data_dir, legacy_root=legacy_root,
                              fixture_root=fixture_root, progress=progress)
            if not record_changeset:
                reason = "not requested by the caller"
            elif cold:
                reason = ("first build: this corpus did not exist before this run, so its "
                          "changeset would be the whole corpus and its inverse is "
                          "`rm corpus.sqlite`. The next run records one.")
            else:
                reason = support.reason
            results["changeset"] = {"recorded": False, "reason": reason,
                                    "first_build": cold, "schema_changed": migrated}
            return results
    finally:
        # Written even when the ingest raised: this ingest commits incrementally,
        # so a crash leaves committed work behind and that is the run somebody
        # actually needs to undo.
        stored = _store_changeset(recording, data_dir)
        db.close()
    results["changeset"] = changeset_result(support.reason, cold, stored)
    return results


def changeset_result(reason: str, cold: bool, stored: dict) -> dict:
    """What the run actually produced, derived rather than asserted.

    `recorded` used to be a hardcoded True beside `**stored`, so a run whose
    collection FAILED reported

        {"error": "sqlite3session_changeset failed (rc=17)", "recorded": true}

    -- and a scheduler reading that JSON was told it had a rollback artifact
    that does not exist. Measured on a cold build: no `changesets/` directory on
    disk at all, and `recorded: true` in the summary, on the one run with the
    most to undo.

    A separate function so the derivation can be tested without running a
    ten-minute ingest to reach it; `run_batch` has no second copy of it.
    """
    return {"reason": reason, "first_build": cold, **stored,
            "recorded": bool(stored.get("path")) and not stored.get("error")}


def _store_changeset(recording, data_dir: Path) -> dict:
    if recording is None:
        return {"error": "the session was never created"}
    if recording.error:
        return {"error": recording.error}
    store = changesets.ChangesetStore(data_dir / "changesets")
    path = store.write(recording, run=datetime.now(timezone.utc).strftime("batch-%Y%m%dT%H%M%SZ"))
    return {"path": str(path), **recording.as_dict()}


def _ingest(db: Database, *, data_dir: Path, legacy_root: Path, fixture_root: Path,
            progress=None) -> dict:
    # Every step below runs inside `progress.phase(...)`, which writes ONE line
    # per phase naming the step, its own counts, how long it took and the run's
    # elapsed time -- and, if a phase outlasts the heartbeat interval, says so
    # while it is still in there. Before this the batch wrote two lines for a
    # ten-minute run, so "slow source" and "wedged source" produced the same
    # log. See batch_logging.ProgressLog.
    progress = progress if progress is not None else ProgressLog(None)
    ledger = data_dir / "ledger"
    pipeline = CollectorPipeline(ledger)
    importer = IngestionImporter(ledger, db.connection)
    results: dict[str, object] = {}
    with progress.phase("seed:samsung-reviewed-profiles") as phase:
        phase[0] = results["reviewed_samsung_devices_created"] = seed_reviewed_samsung(
            db, fixture_root / "samsung" / "reviewed_profiles.json")
    samsung_history = legacy_root / "T005-fota-modem" / "samsung_fota.csv"
    with progress.phase("seed:samsung-fota-history-identities") as phase:
        phase[0] = results["samsung_history_devices_created"] = \
            seed_samsung_history_identities(db, samsung_history)
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
    # The twelve captured sources, each its own phase.
    #
    # There is deliberately NO "4 of 37" denominator on these lines. The total
    # is not knowable when the first phase logs (the adapter list is built after
    # the two seeds), so it would have to be a constant maintained by hand --
    # and a progress display reading "4/12" on a run of thirteen states a number
    # it cannot source, which is the one thing this project does not do. The
    # count of phases actually run is reported once, on the finishing line.
    for adapter, run_id in adapters:
        # Named by run id, not only by source id: `tecno.ota.checkin` is TWO
        # captures (TECNO, and Infinix+itel) under one source id, and a progress
        # line that cannot tell them apart cannot say which one is slow.
        with progress.phase(f"source:{run_id}") as phase:
            result = pipeline.run(adapter, run_id)
            imported = importer.import_run(adapter.source_id, run_id)
            results[adapter.source_id] = {"state": result.run.state, **imported}
            phase[0] = imported
    with progress.phase("promote:samsung-firmware") as phase:
        promotion = SamsungFirmwarePromoter(db.connection).promote_pending()
        results["samsung_promotion"] = {
            "promoted": promotion.promoted, "skipped": promotion.skipped, "events": promotion.events}
        phase[0] = results["samsung_promotion"]
    with progress.phase("identity:bridge-registry") as phase:
        phase[0] = results["identity_bridge"] = rebuild_identity_registry(
            db.connection, legacy_root / "T004-gsmarena-slugs" / "gsm_specs.csv")
    decisions = []
    if (data_dir / 'local.sqlite').is_file():
        with sqlite3.connect(data_dir / 'local.sqlite') as local:
            local.row_factory = sqlite3.Row
            if local.execute("SELECT 1 FROM sqlite_schema WHERE name='identity_decisions'").fetchone():
                decisions = [dict(r) for r in local.execute('SELECT * FROM identity_decisions')]
    # BEFORE the identity rules, so anything a new capture has unblocked is
    # decided again in THIS run rather than spending a night in 'proposed'. An
    # adjudication is terminal, not closed: it is withdrawn the moment the basis
    # it was taken on stops describing the corpus. See adjudication.py.
    with progress.phase("identity:reopen-stale-adjudications") as phase, db.connection:
        phase[0] = results["adjudication_reopened"] = reopen_stale_adjudications(db.connection)
    with progress.phase("identity:automate-review") as phase:
        phase[0] = results["identity_automation"] = automate_identity_review(
            db.connection, devices_yml=legacy_root / "xiaomi-tracker" / "devices.yml",
            specs_csv=legacy_root / "T004-gsmarena-slugs" / "gsm_specs.csv",
            google_play_csv=legacy_root / "google-play-devices" / "supported_devices.csv", decisions=decisions)
    # Identities that arrived AFTER their product was concluded. A remembered
    # conclusion is final by design, so `_conclude` returns early for the
    # product and never evaluates the new identity: it stays 'proposed',
    # its links stay 'proposed', and promotion needs both approved. The
    # evidence is captured and cannot reach the surface.
    #
    # This existed, unreferenced by anything, since 2026-09-22. It approves 0
    # identities against today's captured devices.yml -- see the module
    # docstring for the measured reason and for why it is wired in anyway.
    with progress.phase("identity:backfill-catalog-confirmed") as phase:
        phase[0] = results["identity_backfill"] = approve_catalog_confirmed_identities(
            db.connection, devices_yml=legacy_root / "xiaomi-tracker" / "devices.yml")
    # Second, and only for what the first one cannot reach: the catalog is keyed
    # by regional codename variants and the mifirm archive publishes the bare
    # stem, so an exact-key test can never match one. This approves a stem only
    # when the product's OWN recorded conclusion carries a captured Google Play
    # device code equal to it, and refuses (with the reason recorded) otherwise.
    # It runs after the exact-key rule, never instead of it: anything the
    # vendor's own word settles should be settled on the vendor's own word.
    with progress.phase("identity:stem-corroborated") as phase:
        phase[0] = results["identity_stem_rule"] = approve_stem_corroborated_identities(
            db.connection, devices_yml=legacy_root / "xiaomi-tracker" / "devices.yml")
    # Must run on EVERY batch: the TECNO source re-emits both spellings each time,
    # so a merge done once is undone by the next ingest.
    with progress.phase("dedupe:confirmed-duplicates") as phase:
        phase[0] = results["dedupe"] = merge_confirmed_duplicates(
            db.connection, legacy_root / "google-play-devices" / "supported_devices.csv")
    # dedupe repoints observation_product_links.product_id and
    # source_identity_registry.product_id at the surviving product, and the two
    # are not guaranteed to move together -- a link can land on the survivor
    # while its registry row stays with a product that no longer exists as its
    # owner. Reconcile before promotion reads link_state, so promotion never
    # sees a state a merge left behind.
    with progress.phase("identity:refresh-link-states") as phase, db.connection:
        phase[0] = results["link_states_after_dedupe"] = refresh_observation_link_states(db.connection)
    # AFTER every identity rule and after dedupe: a product is only unresolvable
    # once everything that could resolve it has had its turn, and adjudicating
    # before dedupe would record a basis for a product about to be merged away.
    # It approves nothing, so promotion below is unaffected by it.
    with progress.phase("identity:adjudicate-unresolvable") as phase:
        phase[0] = results["adjudication"] = adjudicate_unresolvable_products(
            db.connection, decisions=decisions)
    with progress.phase("promote:product-observations") as phase:
        phase[0] = results["product_promotion"] = promote_approved_product_observations(db.connection)
    with progress.phase("promote:products-to-devices") as phase:
        phase[0] = results["device_promotion"] = vars(promote_approved_products_to_devices(db.connection))
    with progress.phase("silicon:canonical-xref") as phase:
        phase[0] = results["canonical_silicon"] = enrich_canonical_silicon(
            db.connection, legacy_root / "cross-reference" / "device-chipset-cve-xref.csv")
    # Fills only the gaps enrich_canonical_silicon left empty; never overwrites
    # its higher-authority rows (see enrich_gsmarena_hardware_silicon docstring).
    with progress.phase("silicon:gsmarena-specs") as phase:
        phase[0] = results["gsmarena_canonical_silicon"] = enrich_gsmarena_hardware_silicon(
            db.connection, legacy_root / "T004-gsmarena-slugs" / "gsm_specs.csv")
    # Last of the silicon steps, so it only fills what the higher-authority
    # captures left empty.
    with progress.phase("silicon:observed") as phase:
        phase[0] = results["observed_silicon"] = enrich_observed_hardware_silicon(db.connection)
    with progress.phase("security:android-bulletin-catalog") as phase:
        phase[0] = results["security_catalog"] = import_security_catalog(
            db.connection, legacy_root / "google-asb-cves" / "android-security-bulletin-cves.csv")
    with progress.phase("security:mediatek-catalog") as phase:
        phase[0] = results["mediatek_security_catalog"] = import_mediatek_catalog(
            db.connection, legacy_root / "mediatek-cve-chipsets" / "mediatek-cve-chipsets.csv")
    with progress.phase("write:agent-review-bundle") as phase:
        phase[0] = results["agent_review_bundle"] = write_agent_review_bundle(
            db.connection, data_dir / "agent-review")
    # Last derivation step, because it reads what every step above wrote.
    # A failure here leaves the previously published generation serving --
    # stale rather than absent -- so it is reported and does not abort the
    # run, whose observations are already committed and correct.
    with progress.phase("project:current-firmware") as phase:
        try:
            report = build_current_firmware(db)
            results["current_firmware"] = {
                "generation": report.generation, "rows": report.rows,
                "devices": report.devices, "canonical_rows": report.canonical_rows,
                "evidence_rows": report.evidence_rows, "digest": report.digest}
        except ProjectionError as error:
            results["current_firmware"] = {"error": str(error), "published": False}
        # Inside the phase, so the line carries the counts either way. A failure
        # here is REPORTED and does not abort the run, so it must not reach the
        # phase's FAILED branch -- a log saying this phase failed when the run
        # went on is the wrong sentence about the right event.
        phase[0] = results["current_firmware"]
    # AFTER current_firmware, which is what rebuilds device_catalog_flat, and so
    # what decides the model codes and variants this index holds. Built here and
    # nowhere else: an index maintained by hand is an index that is stale by the
    # next ingest, and this one is read on every keystroke of the type-ahead.
    #
    # It rebuilds only when its basis digest moved -- see search_index.build. On
    # a run that changes no release text that is both free and, more importantly,
    # 5.6 MB that does not enter this run's changeset for no change at all.
    with progress.phase("index:search-trigram") as phase:
        phase[0] = results["search_index"] = search_index.build(db.connection)
    # The query planner has no statistics unless something runs ANALYZE, and
    # nothing ever had: 129 indices and no sqlite_stat1 table at all, so every
    # plan on this corpus was chosen from SQLite's built-in guesses about how
    # selective an index is. Measured cost of fixing that: 0.1s. Measured
    # effect: -56% on /api/v1/releases.
    #
    # It belongs HERE, not in a one-off command, for two reasons. Statistics
    # describe a snapshot, and this batch is what changes the snapshot -- a
    # manual ANALYZE is correct until the next ingest and then quietly stale.
    # And sqlite_stat1 lives IN the database file, so a rebuild starts with no
    # statistics again; anything not run by the thing that builds the corpus is
    # lost every time the corpus is rebuilt.
    #
    # After the derivations above and before check_corpus, so the invariant
    # queries are themselves planned with statistics, and so what is analysed is
    # the corpus this run actually produced.
    with progress.phase("analyze:query-statistics") as phase:
        db.connection.execute("ANALYZE")
        results["query_statistics"] = {
            "analyzed_tables": db.connection.execute(
                "SELECT count(DISTINCT tbl) FROM sqlite_stat1").fetchone()[0]}
        phase[0] = results["query_statistics"]
    # Check the corpus we just produced, against the corpus -- not against a
    # fresh in-memory schema, which is what every existing validator did and
    # is why none of them could ever fail. Reported, not raised: the
    # violations that exist today describe already-ingested data, and
    # aborting the run repairs none of it.
    with progress.phase("check:corpus-invariants") as phase:
        findings = check_corpus(db.connection)
        results["integrity"] = {"summary": summarise(findings),
                                "findings": [f.as_dict() for f in findings]}
        phase[0] = findings
    # Advisory only: never gates or alters the run above. See silence.py
    # and docs/SOURCE_SILENCE_DETECTION.md. `main()` below turns a
    # "silent" finding into a nonzero process exit and a logged ALARM,
    # which is the only part of this that can reach anyone -- and only
    # if the scheduled invocation's exit code is wired to alerting.
    with progress.phase("check:source-silence") as phase:
        phase[0] = results["silence"] = detect_silence(db.connection)
    # AFTER check_corpus, so this run is checked against the PREVIOUS run's
    # baseline and not against one written seconds earlier -- recording first
    # would make every comparison trivially identical, which is the shape of a
    # check that cannot fail. See corpus_identity.py for why containment rather
    # than equality, and why the baseline is a sidecar rather than a table.
    with progress.phase("record:corpus-identity") as phase:
        recorded = corpus_identity.write_baseline(
            db.connection, data_dir / corpus_identity.BASELINE_FILENAME)
        results["corpus_identity"] = {
            "digest": recorded["digest"],
            "path": str(data_dir / corpus_identity.BASELINE_FILENAME),
            **{name: part["count"] for name, part in recorded["components"].items()}}
        phase[0] = results["corpus_identity"]
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


def _prune(logger, *, data_dir: Path, log_path: Path, args) -> dict:
    """Bounded retention over the ledger and batch.log, inside the batch lock.

    ORDER IS LOAD-BEARING, twice over.

    It runs INSIDE `exclusive_worker(batch.lock)`: the manual collection worker
    takes a different lock (`local.collection.lock`) and writes `ledger/staging`,
    so pruning outside the batch lock could delete a staging file under a
    collection that is mid-write. Running it before `run_batch` also means the
    plan sees the previous run's output, which is what the minimum age is
    measured against.

    And `configure_batch_logging` holds `batch.log` open for the whole process,
    so a rotation renames the file under a live handle and this process would go
    on writing to `batch.log.1` -- silently, for the next ten minutes. The logger
    is therefore reattached to the fresh file BEFORE anything is logged about the
    rotation, which is also what puts the retention line in the new generation
    rather than the archived one.
    """
    from .batch_logging import configure_batch_logging

    if args.retention_off:
        logger.info("retention off: nothing pruned, nothing rotated (--retention-off)")
        return {"off": True,
                "reason": "disabled on the command line with --retention-off"}
    plan = retention.plan(
        data_dir,
        policy=retention.default_policy(max_age_days=args.retention_days,
                                        min_age_hours=args.retention_min_age_hours),
        max_bytes=int(args.retention_max_mb * 1024 * 1024),
        log_path=log_path,
        log_max_bytes=int(args.log_max_mb * 1024 * 1024),
        log_keep=args.log_keep,
        now=time.time())
    report = retention.apply(plan, data_dir, dry_run=args.retention_dry_run)
    if report["log"].get("rotated"):
        # Same Logger object (logging keys them by name); its handlers are what
        # get closed and reopened, so the caller's reference follows along.
        configure_batch_logging(log_path)
        logger.info("batch.log rotated to %s.1; the lines before this one are in it",
                    log_path.name)
    # `logger.info(template, *fields)`, never `logger.info(*log_fields(...))`.
    # logging unwraps a single MAPPING argument and nothing else, so handing it
    # the tuple whole makes `msg % self.args` see one argument for fourteen %s
    # and raise inside the handler. That is reported on stderr as a "Logging
    # error" and swallowed: the run carries on, and the line simply never
    # appears in batch.log. Found by running the batch, not by reading it.
    template, fields = retention.log_fields(report)
    logger.info(template, *fields)
    if report["refused_code"] == retention.REFUSED_NO_CORPUS:
        # Not an ALARM. A first build has no corpus until run_batch makes one, and
        # an alarm on the normal first-run state teaches a reader to ignore the
        # real one. tests/test_batch_logging.py holds that line.
        logger.info("retention pruned nothing: %s", report["refused"])
    elif report["refused"]:
        logger.warning("ALARM retention refused and pruned nothing: %s", report["refused"])
    elif report["residual_bytes"]:
        # Only when the budget was actually applied: under a refusal the refusal
        # line already says why nothing moved, and two reasons for one fact is
        # how the second one gets believed on its own.
        logger.warning("ALARM retention could not meet its byte budget: %s",
                       report["residual_reason"])
    for error in report["errors"]:
        logger.warning("ALARM retention could not remove a file: %s", error)
    return report


def main() -> None:
    from .batch_logging import configure_batch_logging

    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Replay captured mobile-source artifacts into a local corpus")
    parser.add_argument("--data-dir", default=".observatory-data")
    parser.add_argument("--legacy-root", default=str(root.parent / "crawler" / "relay" / "results"))
    parser.add_argument("--log-file", default=None,
                         help="Defaults to <data-dir>/batch.log. See docs/SOURCE_SILENCE_DETECTION.md.")
    # Retention. Every default is measured rather than picked -- see
    # retention.py's module docstring and docs/SCHEDULING.md "Log growth".
    parser.add_argument("--retention-days", type=float, default=retention.DEFAULT_MAX_AGE_DAYS,
                        help="Prune a managed ledger file this many days after it was last "
                             "written. Default %(default)s: fourteen consecutive nightly runs "
                             "in which this run id was not re-emitted.")
    parser.add_argument("--retention-max-mb", type=float, default=retention.DEFAULT_MAX_MB,
                        help="Total byte budget for ledger/staging, ledger/quarantine and "
                             "ledger/raw together. Default %(default)s MiB, 2.4x the measured "
                             "108.2 MiB; it reports an honest residual rather than deleting a "
                             "cited or current-run file to reach the number.")
    parser.add_argument("--retention-min-age-hours", type=float,
                        default=retention.DEFAULT_MIN_AGE_HOURS,
                        help="No managed file younger than this can be pruned by the byte "
                             "budget. Default %(default)s, two nightly cadences, which is what "
                             "makes it impossible to take a file the current or previous run "
                             "wrote.")
    parser.add_argument("--log-max-mb", type=float, default=retention.DEFAULT_LOG_MAX_MB,
                        help="Rotate batch.log at this size. Default %(default)s MiB; at the "
                             "measured 232 B per run it will not fire for decades, so this is a "
                             "bound against a verbosity regression, not against today's growth.")
    parser.add_argument("--log-keep", type=int, default=retention.DEFAULT_LOG_KEEP,
                        help="How many rotated batch.log generations to keep. Default "
                             "%(default)s, so at most 5x --log-max-mb of log on disk. Below 1 "
                             "rotation is refused rather than deleting the live log.")
    parser.add_argument("--retention-dry-run", action="store_true",
                        help="Report what retention would prune and rotate, and do neither.")
    parser.add_argument("--retention-off", action="store_true",
                        help="Skip retention entirely. The log says so rather than reporting a "
                             "run that pruned nothing.")
    parser.add_argument("--progress-heartbeat-seconds", type=float,
                        default=DEFAULT_HEARTBEAT_SECONDS,
                        help="A phase still running after this many seconds says so, and keeps "
                             "saying so. Default %(default)s; on today's corpus no phase reaches "
                             "it, so a normal run emits none and only a stall is audible. 0 "
                             "turns the heartbeat off and leaves the one-line-per-phase log.")
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    log_path = Path(args.log_file) if args.log_file else data_dir / "batch.log"
    logger = configure_batch_logging(log_path)

    logger.info("batch starting data_dir=%s legacy_root=%s", data_dir, args.legacy_root)
    # Two batches must never run against one corpus. The timer fires on a
    # calendar, not on the previous run finishing: today's batch takes 110s and
    # the unit allows 30m, but the same work at the production volume this is
    # sized for runs for hours, so a nightly timer will eventually start a run
    # while the last one is still going. Both would then hold write
    # transactions against the same SQLite file, and the second would fail on
    # busy_timeout somewhere in the middle -- a half-ingested run reported as a
    # crash, at 3am.
    #
    # The collection worker already had exactly this guard; the batch, which is
    # the bigger writer, never took it. Nonblocking on purpose: a second batch
    # should say so and exit, not queue up behind the first and start the
    # moment it ends.
    try:
        with exclusive_worker(data_dir / "batch.lock", holder="An ingest batch"):
            # Inside the lock, before the ingest. See _prune's docstring: both
            # halves of that ordering are load-bearing.
            retention_report = _prune(logger, data_dir=data_dir, log_path=log_path, args=args)
            # Built AFTER _prune, because _prune may rotate batch.log and
            # reattach the logger's handlers. The Logger object is the same one
            # either way (logging keys them by name), so this reference follows
            # the rotation -- but constructing it after is one less thing to
            # reason about.
            progress = ProgressLog(logger,
                                   heartbeat_seconds=args.progress_heartbeat_seconds)
            try:
                results = run_batch(data_dir=data_dir, legacy_root=Path(args.legacy_root),
                                    fixture_root=root / "fixtures", progress=progress)
            finally:
                progress.close()
            results["retention"] = retention_report
            results["phases"] = progress.phases
    except ValueError as exc:
        # exclusive_worker raises ValueError when the lock is already held.
        logger.error("batch refused to start: %s", exc)
        sys.exit(3)
    except Exception:
        logger.exception("batch failed before completion")
        raise
    print(json.dumps(results, indent=2, sort_keys=True))
    logger.info("batch finished phases=%s totals=%s", results.get("phases", "unreported"),
                json.dumps(results.get("totals", {})))

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
