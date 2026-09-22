from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .collectors.adapters.samsung_fota import SamsungFotaArtifactAdapter
from .collectors.adapters.samsung_history import SamsungFotaHistoryAdapter
from .collectors.adapters.tecno_security import TecnoSecurityPatchAdapter
from .collectors.adapters.xiaomi_tracker import XiaomiFirmwareTrackerAdapter
from .collectors.importer import IngestionImporter
from .collectors.pipeline import CollectorPipeline
from .collectors.promotion import SamsungFirmwarePromoter
from .collectors.device_promotion import promote_approved_products_to_devices
from .worker_lock import exclusive_worker
from .identity_bridge import rebuild_identity_registry
from .enrichment import automate_identity_review, promote_approved_product_observations, write_agent_review_bundle


# Dates from the preserved distribution's artifact manifest, keyed by exact bytes.
# A file replacement cannot inherit a historical date merely from its filename.
CAPTURE_TIMES = {
    ("samsung.fota", "0edbab28550088ae8388de010fc2c9ec0b55f2b001fd993d8b831d62265bb221"): "2026-09-16T06:00:00Z",
    ("samsung.fota", "b267174f050ad71899d2e01f0e3cacbaef57c13dd06469d1ba4c3ca4f8169ff6"): "2026-09-13T09:16:43Z",
    ("xiaomi.community.firmware_tracker", "2db6f7c2f1d34a55085597ba34b6f1ed2c0d67820f2408cf437659006060c3a8"): "2026-09-13T10:55:00Z",
    ("xiaomi.community.firmware_tracker", "92b05dda60c72d44f367c12e341f2a03f3675a8805e8e4e6cecba8b23aaa293d"): "2026-09-13T10:55:00Z",
    ("tecno.vendor.security_device_scope", "17bfe2b15bea7ddfe1de24f2b22040dceadb38a0788b7f9927495e48716e3c88"): "2026-09-13T11:33:40Z",
}

def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def migrate_collection_queue(db: sqlite3.Connection) -> None:
    """Upgrade old local databases without making the corpus migration-dependent."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(collection_requests)")}
    additions = {
        "execution_mode": "TEXT NOT NULL DEFAULT 'captured_replay'",
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "run_id": "TEXT",
        "result_json": "TEXT",
        "log_json": "TEXT NOT NULL DEFAULT '[]'",
        "retry_of": "INTEGER",
    }
    for name, declaration in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE collection_requests ADD COLUMN {name} {declaration}")
    db.commit()


@dataclass(frozen=True)
class WorkerPaths:
    ledger: Path
    legacy_root: Path
    fixture_root: Path


class CollectionWorker:
    """Executes one durable request using only explicitly captured artifacts.

    No adapter in this worker performs a live network request. The execution mode
    and result summary deliberately expose that boundary to API and UI clients.
    """

    def __init__(self, local: sqlite3.Connection, corpus: sqlite3.Connection,
                 paths: WorkerPaths) -> None:
        self.local = local
        self.corpus = corpus
        self.paths = paths
        migrate_collection_queue(local)
        database = local.execute("PRAGMA database_list").fetchone()[2]
        self.lock_path = Path(database).with_suffix(".collection.lock") if database else paths.ledger.parent / "collection.lock"

    def recover_interrupted(self) -> list[int]:
        # A live worker owns the OS lock. A crashed worker loses it immediately,
        # including after a reboot; no clock/lease guess can interrupt active work.
        with exclusive_worker(self.lock_path):
            return self._recover_interrupted()

    def _recover_interrupted(self) -> list[int]:
        rows = self.local.execute("SELECT id,log_json FROM collection_requests WHERE status='running'").fetchall()
        for row in rows:
            logs = json.loads(row["log_json"] or "[]")
            logs.append({"at": _now(), "level": "warning", "message":
                         "Previous worker stopped before recording completion. Review and retry explicitly; committed evidence is retained."})
            self._finish(row["id"], "interrupted", {
                "executionMode": "captured_replay", "liveNetwork": False,
                "error": "Worker interrupted; some ingestion may already have committed.",
                "accepted": None, "rejected": None}, logs)
        return [row["id"] for row in rows]

    def retry(self, request_id: int) -> dict:
        with self.local:
            row = self.local.execute("SELECT * FROM collection_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise KeyError(request_id)
            if row["status"] not in ("failed", "partial", "interrupted"):
                raise ValueError("Only failed, partial or interrupted requests can be retried.")
            pending = self.local.execute("SELECT id FROM collection_requests WHERE retry_of=? AND status IN ('queued','running')", (request_id,)).fetchone()
            if pending:
                return self.get(pending[0])
            cursor = self.local.execute(
                "INSERT INTO collection_requests(target,source,scope,retry_of) VALUES(?,?,?,?)",
                (row["target"], row["source"], row["scope"], request_id))
        return self.get(cursor.lastrowid)

    def process_next(self) -> dict | None:
        with exclusive_worker(self.lock_path):
            self._recover_interrupted()
            row = self.local.execute(
                "SELECT id FROM collection_requests WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            return self._process(int(row[0])) if row else None

    def process(self, request_id: int) -> dict:
        with exclusive_worker(self.lock_path):
            self._recover_interrupted()
            return self._process(request_id)

    def _process(self, request_id: int) -> dict:
        self.local.execute("BEGIN IMMEDIATE")
        row = self.local.execute(
            "SELECT * FROM collection_requests WHERE id=?", (request_id,)
        ).fetchone()
        if row is None:
            self.local.rollback()
            raise KeyError(request_id)
        if row["status"] != "queued":
            self.local.rollback()
            raise ValueError(f"request is {row['status']}, not queued")
        started = _now()
        run_id = f"manual-{request_id}-{started.replace(':', '').replace('-', '')}"
        logs = [{"at": started, "level": "info", "message":
                 "Started captured-artifact replay; this is not a live network collection."}]
        self.local.execute(
            "UPDATE collection_requests SET status='running',started_at=?,run_id=?,log_json=? WHERE id=?",
            (started, run_id, json.dumps(logs), request_id))
        self.local.commit()

        imported = None
        phase = "dispatch"
        try:
            adapter, limitation = self._dispatch(row)
            phase = "capture_parse"
            result = CollectorPipeline(self.paths.ledger).run(adapter, run_id)
            phase = "import"
            imported = IngestionImporter(self.paths.ledger, self.corpus).import_run(adapter.source_id, run_id)
            phase = "promotion"
            promoted = None
            if row["source"] == "samsung":
                p = SamsungFirmwarePromoter(self.corpus).promote_pending()
                promoted = {"promoted": p.promoted, "skipped": p.skipped, "events": p.events}
            elif row["source"] in ("xiaomi", "tecno"):
                rebuild_identity_registry(self.corpus, self.paths.legacy_root / "T004-gsmarena-slugs" / "gsm_specs.csv")
                identity = automate_identity_review(
                    self.corpus, devices_yml=self.paths.legacy_root / "xiaomi-tracker" / "devices.yml",
                    specs_csv=self.paths.legacy_root / "T004-gsmarena-slugs" / "gsm_specs.csv",
                    google_play_csv=self.paths.legacy_root / "google-play-devices" / "supported_devices.csv",
                    decisions=[dict(r) for r in self.local.execute("SELECT * FROM identity_decisions")])
                product = promote_approved_product_observations(self.corpus)
                device = vars(promote_approved_products_to_devices(self.corpus))
                bundle = write_agent_review_bundle(self.corpus, self.paths.ledger.parent / "agent-review")
                promoted = {"identity": identity, "product": product, "device": device,
                            "remainingAgentCandidates": bundle["candidate_count"]}
            status = "succeeded" if result.run.state == "healthy" else "partial"
            summary = {
                "executionMode": "captured_replay", "liveNetwork": False,
                "sourceId": adapter.source_id, "pipelineState": result.run.state,
                "evidenceObservedAt": getattr(adapter, "observed_at", None),
                "importedAt": _now(),
                "accepted": imported["valid"], "rejected": imported["invalid"],
                "promotion": promoted, "limitation": limitation,
            }
            logs.append({"at": _now(), "level": "info", "message":
                         f"Replay imported {imported['valid']} valid observations."})
            self._finish(request_id, status, summary, logs)
        except Exception as exc:
            logs.append({"at": _now(), "level": "error", "message": f"{type(exc).__name__}: {exc}"})
            self._finish(request_id, "failed", {
                "executionMode": "captured_replay", "liveNetwork": False,
                "error": str(exc), "phase": phase,
                "accepted": imported["valid"] if imported else (None if phase == "import" else 0),
                "rejected": imported["invalid"] if imported else (None if phase == "import" else 0),
            }, logs)
        return self.get(request_id)

    def get(self, request_id: int) -> dict:
        row = self.local.execute("SELECT * FROM collection_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise KeyError(request_id)
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json") or "null")
        item["logs"] = json.loads(item.pop("log_json") or "[]")
        item["live_network"] = False
        return item

    def _finish(self, request_id: int, status: str, summary: dict, logs: list[dict]) -> None:
        self.local.execute(
            "UPDATE collection_requests SET status=?,finished_at=?,result_json=?,log_json=? WHERE id=?",
            (status, _now(), json.dumps(summary, sort_keys=True), json.dumps(logs), request_id))
        self.local.commit()

    def _capture_time(self, path: Path, source_id: str) -> str:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        row = self.corpus.execute("SELECT retrieved_at FROM artifacts WHERE source_id=? AND sha256=?", (source_id, digest)).fetchone()
        captured_at = row[0] if row else CAPTURE_TIMES.get((source_id, digest))
        if not captured_at:
            raise ValueError("Capture timestamp provenance is missing for these exact artifact bytes; import its captured metadata first.")
        return captured_at

    def _dispatch(self, row: sqlite3.Row):
        source, scope, target = row["source"], row["scope"], row["target"].strip()
        if source == "xiaomi" and scope in ("latest_firmware", "firmware_history", "device_profile"):
            path = self.paths.legacy_root / "xiaomi-tracker" / ("latest.yml" if scope == "firmware_history" else "xiaomi-firmware-latest.csv")
            return XiaomiFirmwareTrackerAdapter(path, self._capture_time(path, XiaomiFirmwareTrackerAdapter.source_id)), (
                f"Captured tracker snapshot replayed in full; target '{target}' is a review hint, not a live query.")
        if source == "tecno" and scope == "security":
            path = self.paths.legacy_root / "tecno-security-comprehensive" / "tecno-security-updates.csv"
            return TecnoSecurityPatchAdapter(path, self._capture_time(path, TecnoSecurityPatchAdapter.source_id)), (
                f"Captured vendor security snapshot replayed in full; target '{target}' is a review hint.")
        if source == "samsung" and scope in ("latest_firmware", "firmware_history"):
            if scope == "firmware_history":
                path = self.paths.legacy_root / "T005-fota-modem" / "samsung_fota.csv"
                self._capture_time(path, SamsungFotaHistoryAdapter.source_id)
                return SamsungFotaHistoryAdapter(path), "Captured Samsung history replay; no network request was made."
            normalized = target.upper().replace(" ", "")
            if "SM-S938B" not in normalized or ("ILO" not in normalized and "/" in normalized):
                raise ValueError("captured latest-FOTA replay is currently available only for SM-S938B / ILO")
            path = self.paths.fixture_root / "samsung" / "fota_sm-s938b_ilo.xml"
            return SamsungFotaArtifactAdapter(path, model_code="SM-S938B", csc="ILO", observed_at=self._capture_time(path, SamsungFotaArtifactAdapter.source_id)), (
                "Captured SM-S938B/ILO manifest replay; no network request was made.")
        raise ValueError(f"no safe captured replay for source={source}, scope={scope}; live collector unavailable")


def main() -> None:
    parser = argparse.ArgumentParser(description="Process one queued Mobile Observatory collection request")
    parser.add_argument("--data-dir", default=".observatory-data")
    parser.add_argument("--legacy-root", required=True)
    parser.add_argument("--request-id", type=int)
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    local = sqlite3.connect(data_dir / "local.sqlite"); local.row_factory = sqlite3.Row
    corpus = sqlite3.connect(data_dir / "corpus.sqlite")
    root = Path(__file__).resolve().parents[2]
    worker = CollectionWorker(local, corpus, WorkerPaths(data_dir / "ledger", Path(args.legacy_root), root / "fixtures"))
    result = worker.process(args.request_id) if args.request_id else worker.process_next()
    print(json.dumps(result or {"status": "idle"}, indent=2, sort_keys=True))
    local.close(); corpus.close()


if __name__ == "__main__":
    main()
