from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from ..repository import CanonicalRepository, Event, normalize_identifier, utc_now

_NS = uuid.UUID("3c39ed17-7d28-4a98-b304-f18457fd65bd")


def _id(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "\x1f".join(parts)))


@dataclass(frozen=True)
class PromotionResult:
    promoted: int = 0
    skipped: int = 0
    events: int = 0


class SamsungFirmwarePromoter:
    """Promote only exact, unique model-code matches; ambiguity stays staged."""

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection

    def promote_pending(self) -> PromotionResult:
        rows = self.db.execute(
            """SELECT o.*, json_extract(o.payload_json,'$.data.model_code') model_code
               FROM observations o WHERE o.source_id='samsung.fota'
                 AND o.record_type='firmware_release' AND o.validation_state='valid'
               ORDER BY o.observed_at,
                 CASE json_extract(o.payload_json,'$.data.manifest_position') WHEN 'latest' THEN 1 ELSE 0 END,
                 o.id"""
        ).fetchall()
        promoted = skipped = events = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                hardware = self.db.execute("SELECT id FROM hardware_models WHERE model_code_normalized=?",
                                           (normalize_identifier(row["model_code"]),)).fetchall()
                if len(hardware) != 1:
                    skipped += 1
                    continue
                events += self._promote_one(row, str(hardware[0]["id"]))
                promoted += 1
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return PromotionResult(promoted, skipped, events)

    def _promote_one(self, row: sqlite3.Row, hardware_id: str) -> int:
        payload = json.loads(row["payload_json"]); data = payload["data"]; now = utc_now()
        target_id = _id("target", "samsung", data["region_code"])
        release_id = _id("firmware", hardware_id, target_id, data["build"], data.get("channel", "stable"))
        evidence_id = _id("evidence", row["id"])
        previous = self.db.execute(
            """SELECT fr.*, os.major android FROM firmware_releases fr LEFT JOIN os_releases os ON os.id=fr.os_release_id
               WHERE fr.hardware_model_id=? AND fr.firmware_target_id=? AND fr.id<>?
               ORDER BY COALESCE(fr.vendor_released_at,fr.first_observed_at) DESC LIMIT 1""",
            (hardware_id, target_id, release_id)).fetchone()
        os_id = None
        if data.get("android"):
            os_id = _id("android", str(data["android"])); major = int(str(data["android"]).split('.')[0])
            self.db.execute("INSERT OR IGNORE INTO os_releases(id,platform,major,display_name) VALUES(?,?,?,?)",
                            (os_id, "android", major, f"Android {data['android']}"))
        self.db.execute("""INSERT OR IGNORE INTO firmware_targets
          (id,vendor_namespace,target_code,target_kind,display_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
          (target_id, "samsung", data["region_code"], "csc", data["region_code"], now, now))
        self.db.execute("INSERT OR IGNORE INTO evidence(id,artifact_id,observation_id,locator,created_at) VALUES(?,?,?,?,?)",
                        (evidence_id, row["artifact_id"], row["id"], payload.get("evidence", {}).get("artifact_pointer"), now))
        self.db.execute("""INSERT INTO firmware_releases
          (id,hardware_model_id,firmware_target_id,build_id,channel,os_release_id,security_patch_level,baseband_version,
           vendor_released_at,first_observed_at,last_observed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(hardware_model_id,firmware_target_id,build_id,channel) DO UPDATE SET
           last_observed_at=MAX(last_observed_at,excluded.last_observed_at),
           baseband_version=COALESCE(excluded.baseband_version,baseband_version),
           security_patch_level=COALESCE(excluded.security_patch_level,security_patch_level)""",
          (release_id, hardware_id, target_id, data["build"], data.get("channel", "stable"), os_id,
           data.get("security_patch"), data.get("baseband"), data.get("release_time"), row["observed_at"], row["observed_at"], now))
        for role in (("availability", "baseband") if data.get("baseband") else ("availability",)):
            self.db.execute("INSERT OR IGNORE INTO firmware_release_evidence VALUES(?,?,?)", (release_id, evidence_id, role))
        self.db.execute("UPDATE observations SET validation_state='promoted' WHERE id=?", (row["id"],))
        # Upgrade-list rows are history discovered during this capture, not changes
        # that happened now. Only the manifest's explicit `latest` creates Radar events.
        if data.get("manifest_position") != "latest":
            return 0
        repo = CanonicalRepository.__new__(CanonicalRepository); repo.db = type("DB", (), {"connection": self.db})()
        before = None if previous is None else {"build": previous["build_id"], "baseband": previous["baseband_version"], "android": previous["android"], "security_patch": previous["security_patch_level"]}
        after = {"build": data["build"], "baseband": data.get("baseband"), "android": data.get("android"), "security_patch": data.get("security_patch")}
        event_type = "firmware_first_observed" if previous is None else "firmware_replaced"
        _, made = repo.append_event(Event(event_type, "firmware_release", release_id,
            f"samsung:{hardware_id}:{target_id}:{data['build']}", row["observed_at"], before, after, evidence_id))
        return int(made)
