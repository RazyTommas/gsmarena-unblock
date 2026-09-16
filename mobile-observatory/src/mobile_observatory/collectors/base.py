from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

from .contracts import Observation, RawArtifact


@dataclass(frozen=True)
class SourceHealthPolicy:
    minimum_observations: int = 1
    maximum_observations: int | None = None
    required_kinds: tuple[str, ...] = ()


class SourceAdapter(ABC):
    """Network/source boundary. Adapters are deterministic over artifacts."""

    source_id: str
    parser_name: str
    parser_version: str
    health_policy = SourceHealthPolicy()

    @abstractmethod
    def fetch(self) -> Iterable[RawArtifact]:
        """Fetch raw source material without mutating persistent state."""

    @abstractmethod
    def parse(self, artifact: RawArtifact, artifact_sha256: str) -> Iterable[Observation]:
        """Parse one artifact. Parsing must not perform network I/O."""
