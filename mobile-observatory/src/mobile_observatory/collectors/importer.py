from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

_NAMESPACE = uuid.UUID("a4ec9562-674d-4e18-8f2c-b5da67602a4d")


def _id(*parts: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, "\x1f".join(parts)))


class IngestionImporter:
    """Idempotently imports a filesystem run into ingestion tables only."""

    def __init__(self, ledger_root: Path, connection: sqlite3.Connection):
        self.root = ledger_root
        self.db = connection

    def import_run(self, source_id: str, run_id: str) -> dict[str, int]:
        run = json.loads((self.root / "runs" / source_id / f"{run_id}.json").read_text())
        valid = self._jsonl(self.root / "staging" / f"{run_id}.jsonl")
        invalid = self._jsonl(self.root / "quarantine" / f"{run_id}.jsonl")
        outcome = {"healthy": "succeeded", "degraded": "partial", "failed": "failed", "quarantined": "quarantined"}[run["state"]]
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO sources(id,name,authority_scope,created_at) VALUES(?,?,?,?)",
                (source_id, source_id, "fixture" if source_id.startswith("fixture.") else "secondary", run["started_at"]),
            )
            self.db.execute(
                """INSERT INTO ingestion_runs(id,source_id,started_at,finished_at,outcome,parser_name,parser_version,fetched_count,accepted_count,rejected_count,error)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET finished_at=excluded.finished_at,outcome=excluded.outcome,
                   fetched_count=excluded.fetched_count,accepted_count=excluded.accepted_count,rejected_count=excluded.rejected_count,error=excluded.error""",
                (run_id, source_id, run["started_at"], run["completed_at"], outcome, run["parser_name"], run["parser_version"], run["observation_count"], run["valid_count"], run["quarantined_count"], "; ".join(run["messages"]) or None),
            )
            artifact_ids: dict[str, str] = {}
            digests = {row["artifact_sha256"] for row in valid} | {row["observation"]["artifact_sha256"] for row in invalid}
            for digest in digests:
                metadata_path = self.root / "raw" / source_id / digest[:2] / f"{digest}.json"
                metadata = json.loads(metadata_path.read_text())
                binary_path = metadata_path.with_suffix(".bin")
                artifact_id = _id("artifact", source_id, digest)
                artifact_ids[digest] = artifact_id
                self.db.execute(
                    """INSERT OR IGNORE INTO artifacts(id,source_id,run_id,sha256,media_type,source_url,retrieved_at,storage_uri,byte_length)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (artifact_id, source_id, run_id, digest, metadata["media_type"], metadata.get("request_url"), metadata["retrieved_at"], str(binary_path), binary_path.stat().st_size),
                )
            for row in valid:
                self._insert_observation(row, run_id, artifact_ids, "valid", None)
            for wrapped in invalid:
                self._insert_observation(wrapped["observation"], run_id, artifact_ids, "invalid", wrapped["issues"])
        return {"valid": len(valid), "invalid": len(invalid)}

    @staticmethod
    def _jsonl(path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _insert_observation(self, row: dict, run_id: str, artifact_ids: dict[str, str], state: str, errors: list[dict] | None) -> None:
        payload = json.dumps({"data": row["data"], "identity_hints": row["identity_hints"], "evidence": row["evidence"]}, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        observation_id = _id("observation", row["source_id"], row["kind"], row["source_record_id"], digest)
        self.db.execute(
            """INSERT OR IGNORE INTO observations(id,source_id,run_id,artifact_id,record_type,source_key,observed_at,payload_json,content_sha256,validation_state,validation_errors_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (observation_id, row["source_id"], run_id, artifact_ids[row["artifact_sha256"]], row["kind"], row["source_record_id"], row["observed_at"], payload, digest, state, json.dumps(errors, sort_keys=True) if errors else None),
        )
