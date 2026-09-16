from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

SCHEMA_VERSION = "1.0.0"
ObservationKind = Literal[
    "device_catalog",
    "firmware_release",
    "support_status",
    "silicon_assignment",
    "security_patch_publication",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RawArtifact:
    source_id: str
    retrieved_at: str
    media_type: str
    content: bytes
    request_url: str | None = None
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Observation:
    """Versioned staging contract. `data` retains source vocabulary.

    `source_record_id` must be stable inside the source. `identity_hints` are
    resolver inputs, never canonical IDs or asserted matches.
    """

    kind: ObservationKind
    source_id: str
    source_record_id: str
    observed_at: str
    artifact_sha256: str
    data: dict[str, Any]
    identity_hints: dict[str, Any]
    evidence: dict[str, Any]
    schema_version: str = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceRun:
    run_id: str
    source_id: str
    started_at: str
    completed_at: str
    state: Literal["healthy", "degraded", "failed", "quarantined"]
    parser_name: str
    parser_version: str
    artifact_count: int
    observation_count: int
    valid_count: int
    quarantined_count: int
    messages: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["messages"] = list(self.messages)
        return result
