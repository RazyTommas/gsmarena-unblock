from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact


class MifirmArchiveAdapter(SourceAdapter):
    """Xiaomi firmware HISTORY, re-parsed from captured mifirm.net model pages.

    WHY THIS IS A SEPARATE SOURCE FROM xiaomi.community.firmware_tracker
    The tracker export is a LATEST-release snapshot: one current build per
    device, 4,886 rows. This is the archive behind it -- every build a model
    page still lists, 44,351 rows spanning 2015 to 2026. They answer different
    questions and disagreeing with each other is meaningful, so they stay
    distinct rather than being merged into one source id.

    PROVENANCE, AND WHY IT IS NOT A TABLE COPY
    docs/LEGACY_IMPORT_POLICY.md forbids copying a legacy table wholesale and
    permits reusing captured payloads whose origin and retrieval time are
    recoverable. So the CSV this reads is not an export of the old `roms`
    table -- it is a fresh parse of the 337 cached HTML pages, and every row
    carries the SHA-256 of the page it came from plus that file's mtime as
    retrieved_at. Any row can be traced to bytes on disk and re-derived.

    WHAT THE RE-PARSE RECOVERED, measured rather than asserted
    The legacy ingest stored 21,843 rows, one per (codename, version, region,
    branch). Re-parsing the same pages yields 44,351 distinct releases, and
    every one of the 21,843 legacy keys is present -- a strict superset. The
    difference is two kinds of loss in the old ingest:
      - it collapsed `type`, so a build published as BOTH a fastboot image and
        a recovery ROM was stored once. Those are different artifacts with
        different download URLs; treating them as one discards a real fact.
      - 5,690 (codename, version, region, branch) combinations were absent
        from the legacy table entirely.

    IDENTITY
    A Xiaomi codename ("agate", "HM2013022") is a source identity hint, not a
    canonical model code, exactly as the tracker adapter treats it. Nothing here
    promotes a codename to hardware identity; that stays behind the review
    boundary.
    """

    source_id = "mifirm.community.firmware_archive"
    parser_name = "mifirm_archive_csv"
    parser_version = "1.0.0"
    health_policy = SourceHealthPolicy(
        minimum_observations=10_000,
        required_kinds=("firmware_release",),
    )

    # mifirm labels regions with a NAME; the corpus keys them by code, and the
    # tracker source already established GLOBAL / CN / EEA. Mapping is explicit
    # and closed: an unmapped value raises rather than inventing a code, because
    # a silently wrong region splits one device's history into two identities.
    REGIONS = {
        "China": "CN",
        "Global": "GLOBAL",
        "EEA": "EEA",
        "Russian": "RU",
        "Taiwan": "TW",
        "Indo": "ID",
        "Turkey": "TR",
        "Japan": "JP",
        "India": "IN",
    }

    def __init__(self, artifact: Path, observed_at: str = "2026-09-02T14:14:00Z"):
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
        seen: set[str] = set()
        for line, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
            codename = (row.get("codename") or "").strip()
            build = (row.get("version") or "").strip()
            region_name = (row.get("region") or "").strip()
            if not (codename and build and region_name):
                continue

            region = self.REGIONS.get(region_name)
            if region is None:
                raise ValueError(
                    f"unmapped mifirm region {region_name!r} at CSV line {line}; "
                    "add it to MifirmArchiveAdapter.REGIONS rather than letting it "
                    "through as a new region identity"
                )

            branch = (row.get("branch") or "").strip() or "stable"
            kind = (row.get("type") or "").strip() or "unspecified"

            # `type` is part of the key: the same build ships as a fastboot image
            # and a recovery ROM, and those are two releases, not one.
            record_id = f"{codename}:{branch}:{build}:{region}:{kind}"
            if record_id in seen:
                continue          # a page occasionally lists the same row twice
            seen.add(record_id)

            # mifirm prints a full timestamp; keep the DATE only. It is a vendor
            # publication date, not an Android patch level, and must never be
            # read as one -- see the day-of-month rule in samsung_aspl.
            updated = (row.get("updated_at") or "").strip()
            released = updated[:10] if len(updated) >= 10 else None

            yield Observation(
                kind="firmware_release",
                source_id=self.source_id,
                source_record_id=record_id,
                observed_at=(row.get("retrieved_at") or self.observed_at),
                artifact_sha256=artifact_sha256,
                data={
                    "model_code": codename,
                    "source_device_name": (row.get("device_name") or "").strip(),
                    "build": build,
                    "region_code": region,
                    "region_source_label": region_name,
                    "channel": branch,
                    "delivery_method": kind,
                    "android": (row.get("android") or "").strip() or None,
                    "vendor_released_at": released,
                    "download_url": (row.get("download_url") or "").strip() or None,
                    "artifact_size": (row.get("size") or "").strip() or None,
                    "identity_state": "unresolved_codename",
                    "date_basis": "vendor_publication_date_not_patch_level",
                },
                identity_hints={
                    "manufacturer": "Xiaomi",
                    "source_codename": codename,
                    "source_device_name": (row.get("device_name") or "").strip(),
                },
                evidence={
                    "artifact_pointer": f"CSV line {line}",
                    "authority": "community",
                    "source_url": (row.get("source_url") or "").strip(),
                    "source_page_sha256": (row.get("source_sha256") or "").strip(),
                },
            )
