from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact

# Apple's own per-model hardware identifier, e.g. "iPhone1,1", "iPhone17,1".
# It is stable, vendor-assigned, and unrelated in shape to an Android model
# code -- comma-separated "<family><major>,<minor>", never dotted/slashed.
_SOURCE_MODEL = re.compile(r"^[A-Za-z]+[0-9]+,[0-9]+$")

# "iOS 18.7.10 (22H374)" -- marketing version and Apple's own build id. There
# is no separate Android-style patch-level dimension: Apple ties a security
# fix to a specific build, one axis, not two.
_VERSION = re.compile(r"^(?P<os>iOS)\s+(?P<marketing>[0-9]+(?:\.[0-9]+){0,3})\s+\((?P<build>[A-Za-z0-9]+)\)$")


class AppleIpswFirmwareAdapter(SourceAdapter):
    """Adapter for the captured ipsw.me per-device IPSW/OTA index.

    THIS IS A firmware_release SOURCE ONLY.

    Apple does not fit the security_patch_publication kind and this adapter
    does not attempt it. That kind's required field is `aspl_month`, and every
    existing producer of it (Samsung, TECNO) means the same specific thing:
    Google's Android Security Patch Level, a calendar-month tier (-01 vs -05,
    see samsung_aspl.py) that is DECOUPLED from the OS build -- a device can
    sit on last month's patch level while already running this month's
    feature build. Apple has no such second axis. A security fix ships bound
    to one specific build (the `version` column here already names it,
    e.g. "iOS 18.7.10 (22H374)"), and Apple's advisory link
    (support.apple.com/NNNNNN) is per-build, not per-month. Writing that
    build's release date into `aspl_month` would assert a monthly patch-tier
    that does not exist for this vendor, and there is no honest value for the
    required `patch_tier` field (Google's 1/5 split) at all -- so the kind is
    skipped entirely rather than filled with invented values. The advisory
    link is instead preserved, un-promoted, as `data.security_advisory_url` on
    the firmware_release it belongs to.

    WHAT DOES FIT
    firmware_release only requires model_code, build, and region_code -- none
    of those are Android-specific:
      - model_code: Apple's own identifier (e.g. "iPhone1,1") with the comma
        replaced by a hyphen SOLELY to satisfy the observation contract's
        model_code format (which forbids commas). The untransformed original
        is kept in data.source_model_identifier and identity_hints, so the
        substitution is recoverable, never a loss.
      - build: Apple's own build id (e.g. "22H374"), extracted from `version`.
      - region_code: ipsw.me carries a single global catalog; the source's
        own `region` column is always "Global" for this export.
    The iOS marketing version goes into data.ios_version -- a new key, not a
    repurposed `android_version`/`launch_android` field. No Android vocabulary
    is touched anywhere in this adapter.
    """

    source_id = "ipsw.me.firmware_index"
    parser_name = "apple_ipsw_csv"
    parser_version = "1.0.0"
    health_policy = SourceHealthPolicy(minimum_observations=1000, required_kinds=("firmware_release",))

    def __init__(self, artifact: Path, observed_at: str = "2026-09-21T23:15:28Z"):
        self.artifact = artifact
        self.observed_at = observed_at

    def fetch(self) -> Iterable[RawArtifact]:
        yield RawArtifact(
            source_id=self.source_id,
            retrieved_at=self.observed_at,
            media_type="text/csv",
            content=self.artifact.read_bytes(),
            request_url="https://ipsw.me",
            status_code=200,
        )

    def parse(self, artifact: RawArtifact, artifact_sha256: str) -> Iterable[Observation]:
        text = artifact.content.decode("utf-8-sig")
        for line, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
            source_model = (row.get("model") or "").strip()
            device_name = (row.get("device") or "").strip()
            version_raw = (row.get("version") or "").strip()
            if not (source_model and device_name and version_raw):
                continue
            match = _VERSION.match(version_raw)
            if not match:
                # Never seen in the captured export (all 4450 rows matched at
                # capture time), but a format this repo has never observed
                # is a reason to skip the row, not to guess a build id.
                continue
            model_code = _model_code(source_model)
            if model_code is None:
                continue
            build = match.group("build")
            region = (row.get("region") or "").strip()
            region_code = "GLOBAL" if region.lower() == "global" else ("SOURCE_UNSPECIFIED" if not region else region.upper())
            yield Observation(
                kind="firmware_release",
                source_id=self.source_id,
                source_record_id=f"{source_model}:{build}",
                observed_at=self.observed_at,
                artifact_sha256=artifact_sha256,
                data={
                    "model_code": model_code,
                    "build": build,
                    "region_code": region_code,
                    "os_name": match.group("os"),
                    "ios_version": match.group("marketing"),
                    "source_model_identifier": source_model,
                    "source_device_name": device_name,
                    "release_date": (row.get("updated_at") or "").strip() or None,
                    "chipset": (row.get("chipset") or "").strip() or None,
                    "baseband_version": (row.get("baseband") or "").strip() or None,
                    "download_url": (row.get("download_url") or "").strip() or None,
                    "catalog_url": (row.get("model_url") or "").strip() or None,
                    # Present but deliberately NOT a security_patch_publication:
                    # see class docstring. Per-build advisory, not a monthly tier.
                    "security_advisory_url": (row.get("security_url") or "").strip() or None,
                    "identity_state": "unresolved_apple_identifier",
                },
                identity_hints={
                    "manufacturer": "Apple",
                    "source_model_identifier": source_model,
                    "source_device_name": device_name,
                },
                evidence={
                    "artifact_pointer": f"CSV line {line}",
                    "authority": "aggregator-indexing-vendor-urls",
                    "source_url": "https://ipsw.me",
                },
            )


def _model_code(source_model: str) -> str | None:
    """Apple's "Family<major>,<minor>" identifier -> contract-legal model_code.

    The comma is replaced with a hyphen because the observation contract's
    model_code format forbids commas; nothing else about the identifier is
    touched, and the untransformed original always travels alongside it in
    data.source_model_identifier / identity_hints, so this is a format
    accommodation, not a normalization that discards information.
    """
    if not _SOURCE_MODEL.fullmatch(source_model):
        return None
    return source_model.replace(",", "-")
