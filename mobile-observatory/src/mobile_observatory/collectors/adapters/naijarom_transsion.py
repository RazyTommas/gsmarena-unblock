from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact


class NaijaromTranssionAdapter(SourceAdapter):
    """Transsion firmware listings captured from naijarom.com.

    WHAT THIS IS WORTH, stated honestly because the first estimate was wrong.
    Sampling one device (Camon 40 Premier) suggested 6 of 14 builds were new, and
    I projected a large gain from that. Measured across the whole capture it is
    much smaller: 1,338 rows over 51 distinct model codes, ZERO models that FRBox
    does not already carry, and 237 builds (18%) that FRBox lacks. This is a
    supplement, not a primary source, and it is recorded here at that weight.

    COLLECTION
    Picked up after the collector's box went offline mid-run. Their scoping stands:
    romprovider.com was deliberately skipped because its robots.txt Disallows
    ClaudeBot and a browser UA in front of that is circumvention, not collection.
    naijarom names no AI crawler; I re-verified its robots.txt myself rather than
    inherit the claim. Honest user agent, 2s between requests, 742 pages, no block.
    159 pages were fetched and listed no firmware -- recorded as misses, because
    "checked, nothing there" and "not checked" are different facts.

    THE FILENAME IS THE RECORD
    Everything here is derived from a published filename such as
    Tecno_Camon_30_5G_CL7_MT6855_15.0.3.127_IN001PF001AZ_260525_MXML.zip
    The filename travels VERBATIM in `source_filename`. The parsed columns sit
    beside it and are best-effort: 560 of 1,338 rows yielded a model code. We can
    parse again later; we cannot un-parse.

    date_token IS NOT A PATCH LEVEL
    The 6-digit token in the filename is a build/publication stamp in the vendor's
    own format and its meaning is not consistent across rows. It is carried as an
    opaque token and must never be joined as an Android security patch level --
    the same trap that made three earlier sources offer a release date under a
    patch-level name.
    """

    source_id = "naijarom.community.transsion_firmware"
    parser_name = "naijarom_transsion_csv"
    parser_version = "1.0.0"
    health_policy = SourceHealthPolicy(
        minimum_observations=500,
        required_kinds=("firmware_release",),
    )

    def __init__(self, artifact: Path, observed_at: str = "2026-09-23T00:00:00Z"):
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
            filename = (row.get("filename") or "").strip()
            model = (row.get("model_code") or "").strip()
            build = (row.get("build") or "").strip()
            market = (row.get("market") or "").strip()
            if not filename:
                continue
            # The contract requires model_code, build and region_code. A row whose
            # filename did not parse into those cannot satisfy it, and inventing a
            # model code to make it fit would be worse than dropping it. 778 rows
            # are skipped for this reason and that is the honest outcome.
            if not (model and build and market):
                continue

            record_id = f"{model}:{market}:{build}:{filename}"
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
                    "model_code": model,
                    "build": build,
                    "region_code": market,
                    "region_basis": "vendor_market_code_not_iso_region",
                    "manufacturer": (row.get("brand") or "").strip().upper() or "TECNO",
                    "source_device_name": (row.get("device_name") or "").strip() or None,
                    "chipset": (row.get("chipset") or "").strip() or None,
                    "chipset_basis": "vendor_platform_part_number_from_filename",
                    "source_filename": filename,
                    "artifact_size": (row.get("file_size") or "").strip() or None,
                    "date_token": (row.get("date_token") or "").strip() or None,
                    "date_basis": "opaque_vendor_token_not_a_patch_level",
                    "identity_state": "unresolved_transsion_model_code",
                },
                identity_hints={
                    "manufacturer": (row.get("brand") or "").strip().upper() or "TECNO",
                    "source_model_code": model,
                    "source_device_name": (row.get("device_name") or "").strip() or None,
                },
                evidence={
                    "artifact_pointer": f"CSV line {line}",
                    "authority": "community",
                    "source_url": (row.get("source_url") or "").strip(),
                },
            )
