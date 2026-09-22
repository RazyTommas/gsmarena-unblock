from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact


def _norm_key(value: str) -> str:
    """Collapse a device name to a stable key fragment.

    The record id used to be (codename, branch, build, method), which COLLIDES:
    the source lists one build of codename HM2013023 under five different device
    names -- Redmi 1 China, Redmi 1 W China, Redmi 1 Taiwan, Redmi 1 Global and
    Redmi 1 W Global. Region does not separate them either, because CN and GLOBAL
    each appear twice in that group.

    57 source keys collided this way. That is not merely untidy: retirement and
    dedupe match on (run_id, source_key), so five genuinely distinct records looked
    like five copies of one, and a retire pass was one step away from destroying
    four of them. The name is what actually distinguishes them, so the name is in
    the key.
    """
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in (value or "").strip())

class XiaomiFirmwareTrackerAdapter(SourceAdapter):
    """Adapter for the captured XiaomiFirmwareUpdater latest-release export.

    Codenames are source identity hints, not canonical model codes. Promotion is
    intentionally blocked until a reviewed codename/device mapping exists.
    """

    source_id = "xiaomi.community.firmware_tracker"
    parser_name = "xiaomi_firmware_tracker_csv"
    parser_version = "1.1.0"
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
        # The upstream catalogue repeats some rows verbatim -- 7 groups are identical
        # on codename, branch, build, method, region AND device name, i.e. every field
        # that could distinguish them. Those are one record listed twice, not two
        # records, so they are suppressed here rather than emitted as colliding keys.
        seen: set[str] = set()
        for line, row in enumerate(rows, start=2):
            codename = row["codename"].strip()
            build = row["version"].strip()
            name = row["name"].strip()
            branch = row["branch"].strip()
            channel = row["branch"].strip()
            method = row.get("method", "").strip()
            record_id = (f"{codename}:{branch}:{build}:{method or 'unspecified'}"
                         f":{_region_from_codename_and_name(codename, name)}"
                         f":{_norm_key(name)}")
            if record_id in seen:
                continue
            seen.add(record_id)
            yield Observation(
                kind="firmware_release",
                source_id=self.source_id,
                source_record_id=record_id,
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
    # Regional feeds intentionally end in `_global` too (e.g. umi_tr_global).
    # Interpret the explicit market suffix before the broad Global label.
    markets = {'eea':'EEA','in':'IN','id':'ID','ru':'RU','tr':'TR','tw':'TW','jp':'JP'}
    specific = re.search(r'_(eea|in|id|ru|tr|tw|jp)(?:_global)?$', codename.lower())
    by_code = markets[specific.group(1)] if specific else None
    names = {'eea':'EEA','india':'IN','china':'CN','japan':'JP','taiwan':'TW',
             'turkey':'TR','russia':'RU','indonesia':'ID'}
    named = {region for token,region in names.items() if re.search(r'\b'+token+r'\b', name.lower())}
    if len(named)>1 or by_code and named and named!={by_code}:
        return 'SOURCE_UNSPECIFIED'  # Preserve conflicting source labels as unknown.
    if by_code or named:
        return by_code or next(iter(named))
    if codename.lower().endswith('_global') or re.search(r'\bglobal\b',name.lower()):
        return 'GLOBAL'
    return "SOURCE_UNSPECIFIED"
