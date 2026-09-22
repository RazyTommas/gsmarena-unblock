from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import defaultdict
from pathlib import Path

from ..dedupe import retire_observations

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
            fresh_ids_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
            for row in valid:
                obs_id = self._insert_observation(row, run_id, artifact_ids, "valid", None)
                fresh_ids_by_key[(row["kind"], row["source_record_id"])].add(obs_id)
            for wrapped in invalid:
                row = wrapped["observation"]
                obs_id = self._insert_observation(row, run_id, artifact_ids, "invalid", wrapped["issues"])
                fresh_ids_by_key[(row["kind"], row["source_record_id"])].add(obs_id)
            retired = self._retire_superseded(source_id, run_id, fresh_ids_by_key)
        return {"valid": len(valid), "invalid": len(invalid), "retired": retired}

    @staticmethod
    def _jsonl(path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _insert_observation(self, row: dict, run_id: str, artifact_ids: dict[str, str], state: str, errors: list[dict] | None) -> str:
        payload = json.dumps({"data": row["data"], "identity_hints": row["identity_hints"], "evidence": row["evidence"]}, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        observation_id = _id("observation", row["source_id"], row["kind"], row["source_record_id"], digest)
        self.db.execute(
            """INSERT OR IGNORE INTO observations(id,source_id,run_id,artifact_id,record_type,source_key,observed_at,payload_json,content_sha256,validation_state,validation_errors_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (observation_id, row["source_id"], run_id, artifact_ids[row["artifact_sha256"]], row["kind"], row["source_record_id"], row["observed_at"], payload, digest, state, json.dumps(errors, sort_keys=True) if errors else None),
        )
        return observation_id

    def _retire_superseded(self, source_id: str, run_id: str,
                           fresh_ids_by_key: dict[tuple[str, str], set[str]]) -> int:
        """A fixed run_id is replayed on every batch (see dedupe.py's incident note
        above `dedupe_reingested_observations`): if the parser's output shape changes,
        the newly parsed row for a source_key gets a new content-derived id and sits
        ALONGSIDE whatever this exact run_id inserted last time, instead of replacing
        it. Once this import has settled on exactly one fresh id per (kind,
        source_key), any OTHER row already living under this run_id with that same key
        is stale content this import has just superseded -- retire it onto the fresh
        row. A key that produced more than one fresh id in this very call is
        ambiguous and is left alone rather than guessed at.
        """
        delete_to_keep: dict[str, str] = {}
        for (kind, source_key), fresh_ids in fresh_ids_by_key.items():
            if len(fresh_ids) != 1:
                continue
            keep_id = next(iter(fresh_ids))
            stale = self.db.execute(
                """SELECT id FROM observations
                   WHERE source_id=? AND run_id=? AND record_type=? AND source_key=? AND id<>?""",
                (source_id, run_id, kind, source_key, keep_id)).fetchall()
            for (stale_id,) in stale:
                delete_to_keep[stale_id] = keep_id
        if not delete_to_keep:
            return 0
        return retire_observations(self.db, delete_to_keep).get("observations_deleted", 0)
