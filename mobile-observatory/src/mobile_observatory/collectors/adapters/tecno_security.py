from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact


class TecnoSecurityPatchAdapter(SourceAdapter):
    """Adapter for TECNO's vendor security device-scope publication.

    The upstream feed asserts month precision only. Names are retained exactly
    and remain unresolved; this adapter never creates a canonical device link.
    """

    source_id = "tecno.vendor.security_device_scope"
    parser_name = "tecno_security_device_scope_csv"
    parser_version = "1.0.0"
    health_policy = SourceHealthPolicy(minimum_observations=100, required_kinds=("security_patch_publication",))

    def __init__(self, artifact: Path, observed_at: str = "2026-09-13T11:33:40Z"):
        self.artifact = artifact
        self.observed_at = observed_at

    def fetch(self) -> Iterable[RawArtifact]:
        yield RawArtifact(
            source_id=self.source_id,
            retrieved_at=self.observed_at,
            media_type="text/csv",
            content=self.artifact.read_bytes(),
            request_url="https://security.tecno.com/slm/deviceScope",
            status_code=200,
        )

    def parse(self, artifact: RawArtifact, artifact_sha256: str) -> Iterable[Observation]:
        rows = csv.DictReader(io.StringIO(artifact.content.decode("utf-8-sig")))
        for line, row in enumerate(rows, start=2):
            # The vendor publication uses commas and slashes to express a list
            # of independently named products. Preserve the original group as
            # evidence, but never create a synthetic "A / B / C" product.
            names = [_tecno_name(part) for part in re.split(r"[,/，]+", row["device"])]
            names = list(dict.fromkeys(part for part in names if part))
            for position, device in enumerate(names, start=1):
                yield Observation(
                    kind="security_patch_publication",
                    source_id=self.source_id,
                    source_record_id=f"line-{line}:{position}:{row['aspl_month']}:{device}",
                    observed_at=self.observed_at,
                    artifact_sha256=artifact_sha256,
                    data={
                        "device": device,
                        "aspl_month": row["aspl_month"].strip(),
                        "publish_date": row["publish_date"].strip(),
                        "title": row["title"].strip(),
                        "source_device_group": row["device"].strip(),
                        "precision": "month",
                        "identity_state": "unresolved_source_name",
                    },
                    identity_hints={"manufacturer": "TECNO", "source_device_name": device},
                    evidence={
                        "artifact_pointer": f"CSV line {line}, device {position}",
                        "authority": "vendor-official",
                        "source_url": "https://security.tecno.com/slm/deviceScope",
                    },
                )


def _tecno_name(value: str) -> str:
    value = " ".join(value.split()).strip()
    # Some rows redundantly prefix an already branded name. Normalize only the
    # presentation; the complete source group remains in source_device_group.
    while value.upper().startswith("TECNO TECNO "):
        value = value[6:].strip()
    return value
