from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .base import SourceAdapter
from .contracts import Observation, SourceRun, utc_now
from .storage import IngestionStore
from .validation import validate_observation


@dataclass(frozen=True)
class PipelineResult:
    run: SourceRun
    valid: tuple[Observation, ...]


class CollectorPipeline:
    def __init__(self, data_root: Path):
        self.store = IngestionStore(data_root)

    def run(self, adapter: SourceAdapter, run_id: str | None = None) -> PipelineResult:
        started = utc_now()
        run_id = run_id or f"{adapter.source_id}-{started.replace(':', '').replace('-', '')}"
        observations: list[Observation] = []
        messages: list[str] = []
        artifacts = 0
        try:
            for artifact in adapter.fetch():
                if artifact.source_id != adapter.source_id:
                    raise ValueError("artifact source_id does not match adapter")
                digest = self.store.save_artifact(artifact)
                artifacts += 1
                observations.extend(adapter.parse(artifact, digest))
        except Exception as exc:
            run = SourceRun(run_id, adapter.source_id, started, utc_now(), "failed", adapter.parser_name, adapter.parser_version, artifacts, len(observations), 0, 0, (f"{type(exc).__name__}: {exc}",))
            self.store.save_run(run)
            return PipelineResult(run, ())

        # Stable output supports byte-for-byte comparisons and repeatable imports.
        observations.sort(key=lambda o: (o.kind, o.source_record_id, hashlib.sha256(repr(o.data).encode()).hexdigest()))
        valid: list[Observation] = []
        quarantined = []
        for item in observations:
            issues = validate_observation(item)
            if issues:
                quarantined.append((item, issues))
            else:
                valid.append(item)
        self.store.save_observations(run_id, valid)
        self.store.save_quarantine(run_id, quarantined)
        policy = adapter.health_policy
        observed_kinds = {item.kind for item in valid}
        missing_kinds = set(policy.required_kinds) - observed_kinds
        outside_count = len(valid) < policy.minimum_observations or (policy.maximum_observations is not None and len(valid) > policy.maximum_observations)
        if not observations:
            state = "degraded"
            messages.append("source returned no observations")
        elif quarantined and not valid:
            state = "quarantined"
            messages.append("all observations failed validation")
        elif quarantined:
            state = "degraded"
            messages.append(f"{len(quarantined)} observation(s) quarantined")
        elif outside_count or missing_kinds:
            state = "degraded"
            if outside_count:
                messages.append(f"accepted count {len(valid)} outside configured range")
            if missing_kinds:
                messages.append(f"missing required observation kinds: {', '.join(sorted(missing_kinds))}")
        else:
            state = "healthy"
        run = SourceRun(run_id, adapter.source_id, started, utc_now(), state, adapter.parser_name, adapter.parser_version, artifacts, len(observations), len(valid), len(quarantined), tuple(messages))
        self.store.save_run(run)
        return PipelineResult(run, tuple(valid))
