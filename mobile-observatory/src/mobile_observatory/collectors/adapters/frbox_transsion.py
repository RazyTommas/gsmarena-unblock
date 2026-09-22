from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact


class FrboxTranssionCatalogAdapter(SourceAdapter):
    """Transsion firmware catalogue (TECNO / Infinix / itel), captured from FRBox.

    WHY THIS SOURCE MATTERS OUT OF PROPORTION TO ITS ROW COUNT
    Transsion was the worst-covered brand family in the corpus by a wide margin:
    496 ROM rows across 482 devices, i.e. 1.03 per device, against Samsung's 77
    and Xiaomi's 66. We knew the phones existed and held almost no firmware for
    them. This is 2,947 rows over 1,088 distinct model codes.

    The silicon is the bigger prize. 2,902 of 2,947 rows (98%) carry a
    chipset/platform, and they arrive as MediaTek PART NUMBERS ("MT6765") rather
    than marketing names -- the exact form the silicon join wants, with no alias
    bridge required. Transsion chipset coverage before this was 29 devices.

    REGION IS KEPT VERBATIM AND IS NOT AN ISO CODE
    market_type carries Transsion's own market vocabulary: GL, RU, IN, TR, EU,
    but also OP, OPPJ, COCL, ZAVC, KESF. Several are operator or composite codes
    with no country meaning we have verified. Mapping "OP" onto a region would be
    inventing a fact, so the source value travels unchanged and is labelled as a
    vendor market code. A wrong region silently splits one device's history in
    two, which is the failure migration 0016 had to repair.

    version_date IS A BUILD DATE, NOT A PATCH LEVEL
    The collector said so in their manifest and I verified it the way this project
    verifies every date column that might be a patch level: day-of-month
    distribution. A real Android patch level lands only on 01 or 05. These spread
    across 08, 09, 10, 11, 13, 14, 16, 17, 18, 20 over 1,551 parsed dates. So it
    is recorded as a build date and must never be joined as a security level.

    DOWNLOAD LINKS ARE EXPIRED, DELIBERATELY
    Every share link in this catalogue is dead, verified by the collector against
    the host's API. They are retained as provenance -- where the row came from --
    not as something to fetch. Nobody should authenticate anywhere to revive them.
    """

    source_id = "frbox.community.transsion_catalog"
    parser_name = "frbox_transsion_catalog_csv"
    parser_version = "1.0.0"
    health_policy = SourceHealthPolicy(
        minimum_observations=1000,
        required_kinds=("firmware_release",),
    )

    def __init__(self, artifact: Path, observed_at: str = "2026-09-22T00:00:00Z"):
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
            model = (row.get("model_code") or "").strip()
            build = (row.get("version") or "").strip()
            market = (row.get("market_type") or "").strip()
            if not (model and build and market):
                continue

            record_id = f"{model}:{market}:{build}"
            if record_id in seen:
                continue
            seen.add(record_id)

            brand = (row.get("brand") or "").strip() or "TECNO"
            android = (row.get("android_version") or "").strip() or None
            platform = (row.get("platform") or "").strip() or None

            yield Observation(
                kind="firmware_release",
                source_id=self.source_id,
                source_record_id=record_id,
                observed_at=self.observed_at,
                artifact_sha256=artifact_sha256,
                data={
                    "model_code": model,
                    "build": build,
                    # verbatim vendor market code; NOT normalised to an ISO region
                    "region_code": market,
                    "region_basis": "vendor_market_code_not_iso_region",
                    "source_device_name": (row.get("project_name") or "").strip() or None,
                    "manufacturer": brand,
                    "android": android,
                    "chipset": platform,
                    "chipset_basis": "vendor_platform_part_number",
                    "build_date": (row.get("version_date") or "").strip() or None,
                    "date_basis": "build_date_extracted_from_version_string_not_patch_level",
                    "download_url": (row.get("download_link") or "").strip() or None,
                    "download_state": (row.get("download_status") or "").strip() or "expired",
                    "identity_state": "unresolved_transsion_model_code",
                },
                identity_hints={
                    "manufacturer": brand,
                    "source_model_code": model,
                    "source_device_name": (row.get("project_name") or "").strip() or None,
                },
                evidence={
                    "artifact_pointer": f"CSV line {line}",
                    "authority": "community",
                    "source_url": "https://frbox.net/",
                },
            )
