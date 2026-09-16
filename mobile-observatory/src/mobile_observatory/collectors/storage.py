from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from tempfile import NamedTemporaryFile

from .contracts import Observation, RawArtifact, SourceRun
from .validation import ValidationIssue


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, path)


class IngestionStore:
    """Filesystem ledger suitable for inspection, replay, and offline import."""

    def __init__(self, root: Path):
        self.root = root

    def save_artifact(self, artifact: RawArtifact) -> str:
        digest = hashlib.sha256(artifact.content).hexdigest()
        body = self.root / "raw" / artifact.source_id / digest[:2] / f"{digest}.bin"
        metadata = body.with_suffix(".json")
        if not body.exists():
            _atomic_write(body, artifact.content)
        if not metadata.exists():
            value = asdict(artifact)
            value.pop("content")
            _atomic_write(metadata, json.dumps(value, sort_keys=True, indent=2).encode())
        return digest

    def save_observations(self, run_id: str, observations: list[Observation]) -> Path:
        path = self.root / "staging" / f"{run_id}.jsonl"
        rows = "".join(json.dumps(o.as_dict(), sort_keys=True) + "\n" for o in observations)
        _atomic_write(path, rows.encode())
        return path

    def save_quarantine(self, run_id: str, rows: list[tuple[Observation, list[ValidationIssue]]]) -> Path:
        path = self.root / "quarantine" / f"{run_id}.jsonl"
        content = "".join(json.dumps({"observation": o.as_dict(), "issues": [asdict(i) for i in issues]}, sort_keys=True) + "\n" for o, issues in rows)
        _atomic_write(path, content.encode())
        return path

    def save_run(self, run: SourceRun) -> Path:
        path = self.root / "runs" / run.source_id / f"{run.run_id}.json"
        _atomic_write(path, json.dumps(run.as_dict(), sort_keys=True, indent=2).encode())
        latest = self.root / "runs" / run.source_id / "latest.json"
        _atomic_write(latest, json.dumps(run.as_dict(), sort_keys=True, indent=2).encode())
        return path
