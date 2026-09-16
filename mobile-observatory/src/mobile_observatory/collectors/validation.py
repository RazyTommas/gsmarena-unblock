from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .contracts import Observation

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODEL_CODE = re.compile(r"^[A-Z0-9][A-Z0-9._/-]{2,39}$", re.I)
_ANDROID = re.compile(r"^[0-9]{1,2}(?:\.[0-9]{1,2})?$")


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    field: str
    message: str


def _valid_time(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except (TypeError, ValueError):
        return False


def validate_observation(item: Observation) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    required_text = {
        "source_id": item.source_id,
        "source_record_id": item.source_record_id,
        "observed_at": item.observed_at,
    }
    for field, value in required_text.items():
        if not isinstance(value, str) or not value.strip():
            issues.append(ValidationIssue("required", field, "must be non-empty text"))
    if not _valid_time(item.observed_at):
        issues.append(ValidationIssue("format", "observed_at", "must be ISO-8601"))
    if not _SHA256.fullmatch(item.artifact_sha256):
        issues.append(ValidationIssue("format", "artifact_sha256", "must be lowercase SHA-256"))
    if not isinstance(item.data, dict) or not item.data:
        issues.append(ValidationIssue("required", "data", "must be a non-empty object"))
    if item.kind == "device_catalog":
        for field in ("manufacturer", "commercial_name", "model_code"):
            if not str(item.data.get(field, "")).strip():
                issues.append(ValidationIssue("required", f"data.{field}", "is required"))
        model_code = str(item.data.get("model_code", ""))
        if model_code and not _MODEL_CODE.fullmatch(model_code):
            issues.append(ValidationIssue("format", "data.model_code", "invalid source model code"))
        android = item.data.get("launch_android")
        if android is not None and not _ANDROID.fullmatch(str(android)):
            issues.append(ValidationIssue("format", "data.launch_android", "expected Android major/minor"))
    if item.kind == "firmware_release":
        for field in ("model_code", "build", "region_code"):
            if not str(item.data.get(field, "")).strip():
                issues.append(ValidationIssue("required", f"data.{field}", "is required"))
    if item.kind == "security_patch_publication":
        for field in ("device", "aspl_month", "publish_date"):
            if not str(item.data.get(field, "")).strip():
                issues.append(ValidationIssue("required", f"data.{field}", "is required"))
        month = str(item.data.get("aspl_month", ""))
        if month and not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", month):
            issues.append(ValidationIssue("format", "data.aspl_month", "expected YYYY-MM; day precision must not be invented"))
    return issues
