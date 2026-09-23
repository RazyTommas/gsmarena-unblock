from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact


class TecnoOtaCheckinAdapter(SourceAdapter):
    """The firmware Google is CURRENTLY distributing for a TECNO model+region.

    WHY THIS SOURCE IS DIFFERENT FROM EVERY OTHER ONE HERE
    FRBox is a catalogue and naijarom is an archive; both look backwards. This
    looks at right now, from the distribution point itself. For 23 models across
    GL/TR/RU/OP/IN it answers "what build would a handset receive today", which
    nothing else in the corpus can answer.

    HOW IT WAS COLLECTED, and the part that needed care
    An Android check-in probe, authorised by Ray on 2026-09-23. The request carries
    a build FINGERPRINT -- a public description of firmware -- and nothing that
    identifies a person. The prober's inherited default generated a random 15-digit
    IMEI per request; that was removed before any probe ran, because an IMEI is a
    GSMA-regulated identifier and fifteen random digits is a lottery ticket on a
    real handset rather than a fictional one. Omitting the field returned identical
    OTA data: it was never required. Session tokens and the androidId Google mints
    per probe are stripped and never stored.

    WHAT `has_update` IN THE SOURCE CSV DOES NOT MEAN
    The submitted fingerprints are SYNTHETIC -- every one carries the same Android
    build id (AP3A.240905.015.A2) with placeholder incrementals. So "33 of 47 models
    have a pending update" measures our own request, not TECNO's fleet, and must
    never be read as a statement about real devices. This adapter therefore ignores
    has_update entirely and imports only rows that carry an actual offered build.

    A build offered here is evidence of CURRENT AVAILABILITY. It is not a release
    date, not a patch level, and not proof that any device has installed it.
    """

    source_id = "google.ota.checkin.tecno"
    parser_name = "tecno_ota_checkin_csv"
    parser_version = "1.0.0"
    health_policy = SourceHealthPolicy(
        minimum_observations=20,
        required_kinds=("firmware_release",),
    )

    # Tcard_AD10-15.1.3.115SP05-GL001PF001AZ   (full image)
    # AD10-15.1.3.115SP05-GL001PF001AZ         (bare)
    TITLE = re.compile(
        r'^(?:Tcard_)?(?P<model>[A-Z0-9]+)-(?P<build>[\d.]+[A-Za-z0-9]*)-'
        r'(?P<market>[A-Z]{2,6}\d{3}[A-Z0-9]*)', re.I)

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
            title = (row.get("update_title") or "").strip()
            if not title:
                continue                      # nothing offered: no fact to record
            model = (row.get("product_base") or "").strip()
            region = (row.get("region") or "").strip()
            if not (model and region):
                continue

            m = self.TITLE.match(title)
            build = m.group("build") if m else title
            market = m.group("market") if m else region

            record_id = f"{model}:{region}:{title}"
            if record_id in seen:
                continue
            seen.add(record_id)

            yield Observation(
                kind="firmware_release",
                source_id=self.source_id,
                source_record_id=record_id,
                observed_at=(row.get("probed_at") or self.observed_at),
                artifact_sha256=artifact_sha256,
                data={
                    "model_code": model,
                    "build": build,
                    "region_code": region,
                    "region_basis": "vendor_market_code_not_iso_region",
                    "manufacturer": "TECNO",
                    "source_device_name": (row.get("model") or "").strip() or None,
                    "update_title": title,          # verbatim
                    "market_token": market,
                    "android_version": (row.get("android_version") or "").strip() or None,
                    "delivery_method": "full_image" if title.lower().startswith("tcard_") else "delta",
                    "artifact_size": (row.get("update_size") or "").strip() or None,
                    "update_urgency": (row.get("update_urgency") or "").strip() or None,
                    "target_sdk": (row.get("update_target_sdk_level") or "").strip() or None,
                    # a signed, session-bound URL: recorded as provenance, never fetched,
                    # and it may well not resolve later. Same honesty as FRBox's dead links.
                    "download_url": (row.get("update_url") or "").strip() or None,
                    "download_url_basis": "non_durable_signed_pointer_not_fetched",
                    "availability_basis": "offered_by_google_ota_at_probe_time",
                    "identity_state": "unresolved_transsion_model_code",
                },
                identity_hints={
                    "manufacturer": "TECNO",
                    "source_model_code": model,
                    "source_device_name": (row.get("model") or "").strip() or None,
                },
                evidence={
                    "artifact_pointer": f"CSV line {line}",
                    "authority": "vendor-derived",   # Google's distribution of TECNO firmware
                    "source_url": "https://android.googleapis.com/checkin",
                },
            )
