from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact


class XiaomiFirmwareTrackerAdapter(SourceAdapter):
    """Adapter for the captured XiaomiFirmwareUpdater latest-release export.

    Codenames are source identity hints, not canonical model codes. Promotion is
    intentionally blocked until a reviewed codename/device mapping exists.
    """

    source_id = "xiaomi.community.firmware_tracker"
    parser_name = "xiaomi_firmware_tracker_csv"
    parser_version = "1.0.0"
    health_policy = SourceHealthPolicy(minimum_observations=100, required_kinds=("firmware_release",))

    def __init__(self, artifact: Path, observed_at: str = "2026-09-13T10:55:00Z"):
        self.artifact = artifact
        self.observed_at = observed_at

    def fetch(self) -> Iterable[RawArtifact]:
        yield RawArtifact(
            source_id=self.source_id,
            retrieved_at=self.observed_at,
            media_type="text/csv",
            content=self.artifact.read_bytes(),
            request_url=self.artifact.resolve().as_uri(),
            status_code=200,
        )

    def parse(self, artifact: RawArtifact, artifact_sha256: str) -> Iterable[Observation]:
        text = artifact.content.decode("utf-8-sig")
        rows = (_yaml_rows(text) if self.artifact.suffix.lower() in (".yml", ".yaml")
                else csv.DictReader(io.StringIO(text)))
        for line, row in enumerate(rows, start=2):
            codename = row["codename"].strip()
            build = row["version"].strip()
            name = row["name"].strip()
            branch = row["branch"].strip()
            channel = row["branch"].strip()
            method = row.get("method", "").strip()
            yield Observation(
                kind="firmware_release",
                source_id=self.source_id,
                source_record_id=f"{codename}:{branch}:{build}:{method or 'unspecified'}",
                observed_at=self.observed_at,
                artifact_sha256=artifact_sha256,
                data={
                    "model_code": codename,
                    "source_device_name": name,
                    "build": build,
                    "region_code": _region_from_codename_and_name(codename, name),
                    "android": row["android"].strip(),
                    "branch": branch,
                    "release_date": row["date"].strip(),
                    "delivery_method": method or None,
                    "download_url": row.get("link", "").strip() or None,
                    "artifact_md5": row.get("md5", "").strip() or None,
                    "identity_state": "unresolved_codename",
                },
                identity_hints={
                    "manufacturer": "Xiaomi",
                    "source_codename": codename,
                    "source_device_name": name,
                },
                evidence={"artifact_pointer": f"CSV line {line}", "authority": "community"},
            )


def _yaml_rows(text: str) -> list[dict[str, str]]:
    """Parse the flat sequence-of-mappings emitted by Xiaomi Firmware Tracker.

    Keeping this tiny parser local avoids making replay of captured artifacts
    depend on a YAML package. The feed has no nested structures.
    """
    rows: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        match = re.match(r"^(?:- |  )([a-zA-Z0-9_]+):(?:\s*(.*))?$", raw)
        if not match:
            continue
        if raw.startswith("- "):
            if current is not None:
                rows.append(current)
            current = {}
        if current is None:
            continue
        value = (match.group(2) or "").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].replace("''", "'")
        current[match.group(1)] = value
    if current is not None:
        rows.append(current)
    return rows


def _region_from_codename_and_name(codename: str, name: str) -> str:
    value = f"{codename} {name}".lower()
    for needle, region in (
        ("_eea_", "EEA"), (" eea", "EEA"), ("_global", "GLOBAL"),
        (" global", "GLOBAL"), (" india", "IN"), ("_in_", "IN"),
        (" china", "CN"), (" japan", "JP"), (" taiwan", "TW"),
        (" turkey", "TR"), (" russia", "RU"), (" indonesia", "ID"),
    ):
        if needle in value:
            return region
    return "SOURCE_UNSPECIFIED"
