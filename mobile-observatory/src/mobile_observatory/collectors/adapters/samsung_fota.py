from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact

_LATEST = re.compile(r"<latest[^>]*>([^<]+)</latest>", re.I)
_VALUE = re.compile(r"<value[^>]*>([^<]+)</value>", re.I)


def parse_version_triplets(xml: str) -> list[tuple[str, str, str, str]]:
    """Return (kind, AP, CSC, CP), preserving CP as an independent fact."""
    rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()

    def add(kind: str, value: str) -> None:
        parts = [part.strip() for part in value.split("/") if part.strip()]
        if not parts or parts[0] in seen:
            return
        seen.add(parts[0])
        rows.append((kind, parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))

    for match in _LATEST.finditer(xml):
        add("latest", match.group(1))
    for match in _VALUE.finditer(xml):
        add("upgrade", match.group(1))
    return rows


class SamsungFotaArtifactAdapter(SourceAdapter):
    """Parse captured Samsung OTA manifests; tests never perform network I/O."""

    source_id = "samsung.fota"
    parser_name = "samsung_fota_manifest"
    parser_version = "1.0.0"
    health_policy = SourceHealthPolicy(minimum_observations=1, required_kinds=("firmware_release",))

    def __init__(self, manifest: Path, *, model_code: str, csc: str, observed_at: str):
        self.manifest = manifest
        self.model_code = model_code.strip().upper()
        self.csc = csc.strip().upper()
        self.observed_at = observed_at

    def fetch(self) -> Iterable[RawArtifact]:
        yield RawArtifact(self.source_id, self.observed_at, "application/xml", self.manifest.read_bytes(),
                          f"https://fota-cloud-dn.ospserver.net/firmware/{self.csc}/{self.model_code}/version.xml", 200)

    def parse(self, artifact: RawArtifact, artifact_sha256: str) -> Iterable[Observation]:
        for kind, ap, csc_build, cp in parse_version_triplets(artifact.content.decode("utf-8", "replace")):
            yield Observation(
                "firmware_release", self.source_id, f"{self.model_code}:{self.csc}:{ap}", self.observed_at,
                artifact_sha256,
                {"model_code": self.model_code, "region_code": self.csc, "build": ap,
                 "csc_build": csc_build or None, "baseband": cp or None,
                 "manifest_position": kind, "channel": "stable"},
                {"manufacturer": "Samsung", "model_code": self.model_code, "region_code": self.csc},
                {"artifact_pointer": f"{kind}:{ap}"},
            )
