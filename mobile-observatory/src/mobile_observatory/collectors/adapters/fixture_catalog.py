from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact


class FixtureCatalogAdapter(SourceAdapter):
    """Reference adapter and deterministic demo-data source.

    It deliberately emits source observations, not canonical device objects.
    """

    source_id = "fixture.supported_catalog"
    parser_name = "fixture_catalog"
    parser_version = "1.0.0"
    health_policy = SourceHealthPolicy(minimum_observations=4, required_kinds=("device_catalog", "firmware_release"))

    def __init__(self, fixture: Path, observed_at: str = "2026-09-16T00:00:00Z"):
        self.fixture = fixture
        self.observed_at = observed_at

    def fetch(self) -> Iterable[RawArtifact]:
        yield RawArtifact(
            source_id=self.source_id,
            retrieved_at=self.observed_at,
            media_type="application/json",
            content=self.fixture.read_bytes(),
            request_url=self.fixture.resolve().as_uri(),
            status_code=200,
        )

    def parse(self, artifact: RawArtifact, artifact_sha256: str) -> Iterable[Observation]:
        payload = json.loads(artifact.content)
        for row in payload["devices"]:
            record_id = f"device:{row['manufacturer'].lower()}:{row['model_code'].lower()}"
            yield Observation(
                kind="device_catalog",
                source_id=self.source_id,
                source_record_id=record_id,
                observed_at=self.observed_at,
                artifact_sha256=artifact_sha256,
                data=row,
                identity_hints={
                    "manufacturer": row["manufacturer"],
                    "model_code": row["model_code"],
                    "aliases": row.get("aliases", []),
                },
                evidence={"artifact_pointer": f"$.devices[?(@.model_code=='{row['model_code']}')]"},
            )
        for row in payload.get("firmware", []):
            record_id = f"firmware:{row['model_code'].lower()}:{row['region_code'].lower()}:{row['build'].lower()}"
            yield Observation(
                kind="firmware_release",
                source_id=self.source_id,
                source_record_id=record_id,
                observed_at=self.observed_at,
                artifact_sha256=artifact_sha256,
                data=row,
                identity_hints={"model_code": row["model_code"], "region_code": row["region_code"]},
                evidence={"artifact_pointer": f"$.firmware[?(@.build=='{row['build']}')]"},
            )
