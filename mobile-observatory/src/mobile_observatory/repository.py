from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .database import Database


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Event:
    event_type: str
    subject_type: str
    subject_id: str
    dedupe_key: str
    occurred_at: str
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None
    evidence_id: str | None = None
    corrects_event_id: str | None = None


class CanonicalRepository:
    """Small write boundary for core invariants; collectors must not use it."""

    def __init__(self, database: Database) -> None:
        self.db = database

    def create_device(
        self,
        *,
        manufacturer: str,
        brand: str,
        family: str,
        variant: str,
        model_code: str,
        codename: str | None = None,
    ) -> str:
        """Create a complete device identity without collapsing its levels."""
        now = utc_now()
        hardware_id = new_id()
        normalized = normalize_identifier(model_code)
        with self.db.transaction() as con:
            manufacturer_id = self._find_or_insert(
                con,
                select="SELECT id FROM manufacturers WHERE canonical_name = ?",
                select_args=(manufacturer,),
                insert="INSERT INTO manufacturers VALUES (?, ?, NULL, ?, ?)",
                insert_args=(manufacturer, now, now),
            )
            brand_id = self._find_or_insert(
                con,
                select="SELECT id FROM brands WHERE manufacturer_id = ? AND canonical_name = ?",
                select_args=(manufacturer_id, brand),
                insert="INSERT INTO brands VALUES (?, ?, ?, ?, ?)",
                insert_args=(manufacturer_id, brand, now, now),
            )
            family_id = self._find_or_insert(
                con,
                select="SELECT id FROM device_families WHERE brand_id = ? AND canonical_name = ?",
                select_args=(brand_id, family),
                insert="INSERT INTO device_families VALUES (?, ?, ?, NULL, ?, ?)",
                insert_args=(brand_id, family, now, now),
            )
            variant_id = self._find_or_insert(
                con,
                select="SELECT id FROM device_variants WHERE family_id = ? AND canonical_name = ?",
                select_args=(family_id, variant),
                insert="INSERT INTO device_variants VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                insert_args=(family_id, variant, now, now),
            )
            con.execute(
                "INSERT INTO hardware_models VALUES (?, ?, ?, ?, ?, ?, ?)",
                (hardware_id, variant_id, model_code, normalized, codename, now, now),
            )
        return hardware_id

    @staticmethod
    def _find_or_insert(
        con: sqlite3.Connection,
        *,
        select: str,
        select_args: tuple[Any, ...],
        insert: str,
        insert_args: tuple[Any, ...],
    ) -> str:
        row = con.execute(select, select_args).fetchone()
        if row is not None:
            return str(row["id"])
        entity_id = new_id()
        con.execute(insert, (entity_id, *insert_args))
        return entity_id

    def add_alias(
        self, *, entity_type: str, entity_id: str, namespace: str, alias: str
    ) -> str:
        alias_id = new_id()
        self.db.connection.execute(
            """INSERT INTO aliases
               (id, entity_type, entity_id, namespace, alias, alias_normalized, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (alias_id, entity_type, entity_id, namespace, alias, normalize_name(alias), utc_now()),
        )
        return alias_id

    def resolve_exact_alias(
        self, *, entity_type: str, namespace: str, value: str
    ) -> str | None:
        row = self.db.connection.execute(
            """SELECT entity_id FROM aliases
               WHERE entity_type = ? AND namespace = ? AND alias_normalized = ?
                 AND review_state = 'accepted'""",
            (entity_type, namespace, normalize_name(value)),
        ).fetchone()
        return None if row is None else str(row["entity_id"])

    def append_event(self, event: Event) -> tuple[str, bool]:
        """Append once; returns (id, created) for safe promotion replay."""
        event_id = new_id()
        try:
            self.db.connection.execute(
                """INSERT INTO domain_events
                   (id, event_type, subject_type, subject_id, dedupe_key,
                    occurred_at, recorded_at, before_json, after_json,
                    evidence_id, corrects_event_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    event.event_type,
                    event.subject_type,
                    event.subject_id,
                    event.dedupe_key,
                    event.occurred_at,
                    utc_now(),
                    _json(event.before),
                    _json(event.after),
                    event.evidence_id,
                    event.corrects_event_id,
                ),
            )
            return event_id, True
        except sqlite3.IntegrityError as error:
            if "domain_events.dedupe_key" not in str(error):
                raise
            row = self.db.connection.execute(
                "SELECT id FROM domain_events WHERE dedupe_key = ?",
                (event.dedupe_key,),
            ).fetchone()
            return str(row["id"]), False

    def propose(
        self,
        *,
        proposer_type: str,
        proposer_id: str,
        proposal_type: str,
        patch: Mapping[str, Any],
        rationale: str,
        citations: list[Mapping[str, Any]],
        target_type: str | None = None,
        target_id: str | None = None,
        run_metadata: Mapping[str, Any] | None = None,
    ) -> str:
        proposal_id = new_id()
        self.db.connection.execute(
            """INSERT INTO proposals
               (id, proposer_type, proposer_id, proposal_type, target_type,
                target_id, patch_json, rationale, citations_json,
                run_metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                proposal_id,
                proposer_type,
                proposer_id,
                proposal_type,
                target_type,
                target_id,
                json.dumps(patch, sort_keys=True),
                rationale,
                json.dumps(citations, sort_keys=True),
                json.dumps(run_metadata or {}, sort_keys=True),
                utc_now(),
            ),
        )
        return proposal_id

    def record_security_verdict(
        self,
        *,
        vulnerability_id: str,
        subject_type: str,
        subject_id: str,
        status: str,
        rule_id: str,
        rule_version: str,
        inputs: Mapping[str, Any],
        evidence_summary: Mapping[str, Any],
    ) -> str:
        """Supersede a verdict without erasing its previous evaluation."""
        verdict_id = new_id()
        with self.db.transaction() as con:
            previous = con.execute(
                """SELECT id FROM security_verdicts
                   WHERE vulnerability_id = ? AND subject_type = ?
                     AND subject_id = ? AND is_current = 1""",
                (vulnerability_id, subject_type, subject_id),
            ).fetchone()
            previous_id = None if previous is None else str(previous["id"])
            if previous_id is not None:
                con.execute(
                    "UPDATE security_verdicts SET is_current = 0 WHERE id = ?",
                    (previous_id,),
                )
            con.execute(
                """INSERT INTO security_verdicts
                   (id, vulnerability_id, subject_type, subject_id, status,
                    rule_id, rule_version, inputs_json, evaluated_at,
                    supersedes_id, evidence_summary_json, is_current)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    verdict_id,
                    vulnerability_id,
                    subject_type,
                    subject_id,
                    status,
                    rule_id,
                    rule_version,
                    json.dumps(inputs, sort_keys=True),
                    utc_now(),
                    previous_id,
                    json.dumps(evidence_summary, sort_keys=True),
                ),
            )
        return verdict_id


def normalize_identifier(value: str) -> str:
    """Conservative identifier normalization; punctuation remains significant."""
    normalized = " ".join(value.strip().upper().split())
    if not normalized:
        raise ValueError("identifier cannot be empty")
    return normalized


def normalize_name(value: str) -> str:
    normalized = " ".join(value.casefold().strip().split())
    if not normalized:
        raise ValueError("name cannot be empty")
    return normalized


def _json(value: Mapping[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))
