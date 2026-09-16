from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact


class SamsungFotaHistoryAdapter(SourceAdapter):
    """Replay the captured vendor-FOTA history export with exact model codes."""

    source_id = "samsung.fota"
    parser_name = "samsung_fota_history_csv"
    parser_version = "1.1.0"
    health_policy = SourceHealthPolicy(minimum_observations=1000, required_kinds=("firmware_release",))

    def __init__(self, artifact: Path):
        self.artifact = artifact

    def fetch(self) -> Iterable[RawArtifact]:
        content = self.artifact.read_bytes()
        yield RawArtifact(self.source_id, "2026-09-13T09:16:43Z", "text/csv", content,
                          self.artifact.resolve().as_uri(), 200)

    def parse(self, artifact: RawArtifact, artifact_sha256: str) -> Iterable[Observation]:
        for line, row in enumerate(csv.DictReader(io.StringIO(artifact.content.decode("utf-8-sig"))), start=2):
            yield Observation("firmware_release", self.source_id,
                f"{row['model']}:{row['csc']}:{row['version']}", row["fetched_at"], artifact_sha256,
                {"model_code": row["model"], "source_device_name": row["device"],
                 "region_code": row["csc"], "build": row["version"], "baseband": row["cp"] or None,
                 "release_time": None, "build_derived_month": (row["pda_month"] or '')[:7] or None,
                 "date_basis": "build_identifier_month_not_vendor_release",
                 "manifest_position": row["kind"], "channel": "stable"},
                {"manufacturer": "Samsung", "model_code": row["model"], "region_code": row["csc"]},
                {"artifact_pointer": f"CSV line {line}", "authority": "vendor-fota-capture"})
