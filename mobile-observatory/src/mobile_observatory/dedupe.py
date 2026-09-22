from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

"""Merge products that BOTH the spelling and the vendor catalogue call one device.

This runs INSIDE run_batch, not as a one-off tool, and the reason is a mistake worth
recording. The duplicate was merged by hand, the batch was re-run, and the duplicate
came straight back: the TECNO source emits both spellings every time, so a data fix
the pipeline undoes is not a fix at all. Same shape as an enrichment with no restore
line -- if an ingest can destroy it, the ingest has to rebuild it.

THE RULE requires two independent signals, because neither is sufficient:
  * names differing only by spacing/punctuation -- a string signal, and string signals
    on device names are what collapsed 'Galaxy S25+' into 'S25'
  * a shared Google Play model code -- a vendor signal, and that alone binds
    'CAMON 20' and 'CAMON 20 PRO' to CK6n, which are different phones
Together they are strong: the catalogue confirms what the spelling suggests. Derived
from ESC-0001, where two independent agents established SPARK 8 P and SPARK 8P are one
device. The rule names no vendor and no product.
"""

CHILD_TABLES = ("observation_product_links", "source_identity_registry",
                "identity_conclusions", "observed_product_silicon",
                "product_firmware_releases", "product_security_publications",
                "source_specifications", "source_build_product_links")


def squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())




def merge_confirmed_duplicates(connection: sqlite3.Connection, catalog: Path) -> dict:
    """Returns counts; safe to call on every batch run."""
    if not Path(catalog).is_file():
        return {"merged": 0, "reason": "catalogue absent; refusing to merge on spelling alone"}
    name_codes: dict[str, set[str]] = {}
    with Path(catalog).open(encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            nm = (r.get("Marketing Name") or "").strip().lower()
            code = (r.get("Model") or "").strip()
            if nm and code:
                name_codes.setdefault(nm, set()).add(code)
    rows = connection.execute(
        "SELECT id, manufacturer, canonical_name FROM source_products").fetchall()
    groups: dict[tuple, list] = {}
    for pid, man, name in rows:
        for code in name_codes.get(name.strip().lower(), ()):
            groups.setdefault((man, code, squash(name)), []).append((pid, name))
    merged = 0
    for _, members in groups.items():
        if len({p for p, _ in members}) < 2:
            continue
        counts = {pid: connection.execute(
            "SELECT COUNT(*) FROM observation_product_links WHERE product_id=?",
            (pid,)).fetchone()[0] for pid, _ in members}
        keep = sorted(members, key=lambda m: (-counts[m[0]], m[0]))[0]
        for pid, _ in (m for m in members if m[0] != keep[0]):
            for t in CHILD_TABLES:
                try:
                    connection.execute(
                        f"UPDATE OR IGNORE {t} SET product_id=? WHERE product_id=?",
                        (keep[0], pid))
                    connection.execute(f"DELETE FROM {t} WHERE product_id=?", (pid,))
                except sqlite3.OperationalError:
                    pass
            connection.execute("DELETE FROM source_products WHERE id=?", (pid,))
            merged += 1
    connection.commit()
    return {"merged": merged}


"""Retire observations that turned out to be the same captured fact twice.

THE INCIDENT THIS EXISTS FOR: commit 2827a10 bumped SamsungFotaHistoryAdapter from
parser_version 1.0.0 to 1.1.0 and changed how a build's date was encoded (release_time
went from a guessed calendar date to null + an explicit build_derived_month/date_basis
pair -- a correctness fix, not a data change). run_batch replays that adapter under the
SAME fixed run_id ("samsung-fota-history-captured") every batch, and observation ids
are a hash of `data`, so the reshaped output got brand-new ids and content hashes.
INSERT OR IGNORE could not catch it -- there was nothing to conflict with -- and the
previous rows from the 1.0.0 shape were never retired. 21,186 facts got a second,
differently-shaped row apiece: 42,372 rows for 21,186 real observations.

THE RULE for what counts as a true duplicate, not a guess: two observations in the
SAME run (same source_id, run_id, source_key) collapse only if every field they hold
in common is byte-identical, identity_hints and evidence match exactly, and every
field that differs is on a named allow-list of known reshape fields. Anything else
about the pair -- a different baseband, a different device name -- blocks the merge
instead of guessing past it.

THE OTHER HALF is the dependents. `evidence`, `observation_product_links`,
`product_firmware_releases`, `product_security_publications`, and everything that
hangs off `evidence` (firmware_release_evidence, domain_events, ...) are discovered
from the schema itself via PRAGMA foreign_key_list, not a hand-maintained table list --
the previous CHILD_TABLES list above is exactly the kind of list that goes stale and
silently orphans rows (it did, once: `UPDATE OR IGNORE` + unconditional `DELETE` on a
PK-shaped child table dropped 5 observations no one noticed until it was measured).
Every dependent row is either remapped onto the surviving observation, or -- only once
proven byte-identical to a row the surviving side already has -- dropped as truly
redundant. A dependent that cannot be proven redundant raises instead of vanishing.
"""

BENIGN_RESHAPE_FIELDS = frozenset({"release_time", "build_derived_month", "date_basis"})


def _fk_referencing(connection: sqlite3.Connection, target_table: str) -> list[tuple[str, str]]:
    """(table, column) pairs anywhere in the schema whose FK points at target_table(id)."""
    tables = [r[0] for r in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    refs = []
    for table in tables:
        for fk in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            # fk row shape: (id, seq, table, from, to, on_update, on_delete, match)
            if fk[2] == target_table:
                refs.append((table, fk[3]))
    return refs


def _remap_or_raise(connection: sqlite3.Connection, table: str, col: str,
                     delete_val: str, keep_val: str) -> int:
    """Repoint `table.col` from delete_val to keep_val. If that collides with a row
    the keep side already has, the retiring row is dropped ONLY once every other
    column on it matches some row the keep side already has -- never on a guess.
    """
    rows = connection.execute(f"SELECT * FROM {table} WHERE {col}=?", (delete_val,)).fetchall()
    if not rows:
        return 0
    try:
        # No explicit transaction here on purpose: this may run nested inside a
        # caller's own `with connection:` block (import_run's), and SQLite's default
        # per-statement rollback on a failed UPDATE already leaves that outer
        # transaction intact for us to fall through and retry as a DELETE below.
        connection.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (keep_val, delete_val))
        return len(rows)
    except sqlite3.IntegrityError:
        pass
    columns = [d[1] for d in connection.execute(f"PRAGMA table_info({table})").fetchall()]
    fk_idx = columns.index(col)
    kept_rows = connection.execute(f"SELECT * FROM {table} WHERE {col}=?", (keep_val,)).fetchall()
    kept_projection = {tuple(r[i] for i in range(len(columns)) if i != fk_idx) for r in kept_rows}
    for row in rows:
        projected = tuple(row[i] for i in range(len(columns)) if i != fk_idx)
        if projected not in kept_projection:
            raise ValueError(
                f"{table}: row {dict(zip(columns, row))} for {col}={delete_val} has no "
                f"equivalent under {col}={keep_val}; refusing to retire it")
    connection.execute(f"DELETE FROM {table} WHERE {col}=?", (delete_val,))
    return len(rows)


def _immutable_tables(connection: sqlite3.Connection) -> set[str]:
    """Tables the schema itself refuses to UPDATE or DELETE (a BEFORE trigger that
    RAISEs, e.g. domain_events and source_data_corrections are an append-only log by
    design). A dependent row in one of these can never be remapped or dropped --
    retiring code has to work around it, not through it.
    """
    rows = connection.execute(
        "SELECT tbl_name, sql FROM sqlite_schema WHERE type='trigger'").fetchall()
    return {tbl for tbl, sql in rows if sql and "immutable" in sql.lower()}


def _plan_evidence_retirement(connection: sqlite3.Connection, delete_to_keep: dict[str, str],
                               evidence_children: list[tuple[str, str]],
                               immutable: set[str]) -> tuple[dict[str, str], list[str], int]:
    """Repoint each evidence row of a retiring observation onto the surviving one,
    OR -- where that collides -- work out what it collided with, without deleting
    anything yet (children still reference these rows by evidence_id; they get
    remapped first, by the caller, before any evidence row is actually dropped).

    evidence's only uniqueness besides its own `id` is (artifact_id, observation_id,
    locator), so a plain UPDATE either succeeds outright (the surviving observation had
    no evidence citing that exact artifact+locator yet -- the common case right after
    import_run, before promotion has created any) or it fails for exactly one possible
    reason: the surviving observation ALREADY has a row for that same artifact+locator,
    which proves this one is a redundant citation of the identical source passage.

    That redundant row is normally safe to drop -- EXCEPT when an immutable table
    (domain_events) still cites it by evidence_id: that citation can never be remapped
    or removed, so the row is detached instead (observation_id set to NULL, a value the
    column already allows) rather than destroyed, keeping the historical citation intact
    and orphan-free.

    Returns ({dropped_evidence_id: surviving_evidence_id}, [ids safe to delete once
    their children are clear], count of rows detached instead of dropped).
    """
    redirect: dict[str, str] = {}
    to_delete: list[str] = []
    detached = 0
    for delete_obs, keep_obs in delete_to_keep.items():
        rows = connection.execute(
            "SELECT id, artifact_id, locator FROM evidence WHERE observation_id=?", (delete_obs,)).fetchall()
        for evidence_id, artifact_id, locator in rows:
            try:
                connection.execute("UPDATE evidence SET observation_id=? WHERE id=?", (keep_obs, evidence_id))
                continue
            except sqlite3.IntegrityError:
                pass
            survivor = connection.execute(
                "SELECT id FROM evidence WHERE observation_id=? AND artifact_id=? AND locator IS ?",
                (keep_obs, artifact_id, locator)).fetchone()
            if survivor is None:
                raise ValueError(
                    f"evidence {evidence_id} (observation {delete_obs}) collided remapping onto "
                    f"{keep_obs} but no equivalent (artifact_id, locator) row exists there; refusing")
            cited_by_immutable = any(
                table in immutable and connection.execute(
                    f"SELECT 1 FROM {table} WHERE {col}=? LIMIT 1", (evidence_id,)).fetchone()
                for table, col in evidence_children)
            if cited_by_immutable:
                connection.execute("UPDATE evidence SET observation_id=NULL WHERE id=?", (evidence_id,))
                detached += 1
                continue
            redirect[evidence_id] = survivor[0]
            to_delete.append(evidence_id)
    return redirect, to_delete, detached


def retire_observations(connection: sqlite3.Connection, delete_to_keep: dict[str, str]) -> dict:
    """Delete each key of `delete_to_keep`, an observation id, in favour of its value
    -- a surviving observation id already carrying the same fact -- remapping or
    dropping every dependent row along the way. Safe to call with an empty dict.
    """
    counts: dict[str, int] = {}
    if not delete_to_keep:
        return counts
    delete_ids = list(delete_to_keep)

    for table, col in _fk_referencing(connection, "observations"):
        if table == "evidence":
            continue
        for delete_val, keep_val in delete_to_keep.items():
            n = _remap_or_raise(connection, table, col, delete_val, keep_val)
            if n:
                counts[f"{table}.{col}"] = counts.get(f"{table}.{col}", 0) + n

    evidence_children = _fk_referencing(connection, "evidence")
    immutable = _immutable_tables(connection)
    evidence_redirect, evidence_to_delete, evidence_detached = _plan_evidence_retirement(
        connection, delete_to_keep, evidence_children, immutable)

    # Children that cite evidence by id must move off the about-to-be-dropped rows
    # BEFORE those rows are deleted, or the delete trips evidence's own referents.
    # Immutable tables were never asked to move -- their citation was preserved above
    # by detaching the evidence row instead of dropping it -- so skip them here.
    for table, col in evidence_children:
        if table in immutable:
            continue
        for delete_val, keep_val in evidence_redirect.items():
            n = _remap_or_raise(connection, table, col, delete_val, keep_val)
            if n:
                counts[f"{table}.{col}"] = counts.get(f"{table}.{col}", 0) + n

    if evidence_to_delete:
        ev_placeholders = ",".join("?" * len(evidence_to_delete))
        connection.execute(f"DELETE FROM evidence WHERE id IN ({ev_placeholders})", evidence_to_delete)
        counts["evidence_dropped_redundant"] = len(evidence_to_delete)
    if evidence_detached:
        counts["evidence_detached_immutable_citation"] = evidence_detached

    placeholders = ",".join("?" * len(delete_ids))
    connection.execute(f"DELETE FROM observations WHERE id IN ({placeholders})", delete_ids)
    counts["observations_deleted"] = len(delete_ids)
    return counts


def dedupe_reingested_observations(connection: sqlite3.Connection, *, source_id: str | None = None,
                                    run_id: str | None = None,
                                    benign_fields: frozenset[str] = BENIGN_RESHAPE_FIELDS,
                                    commit: bool = True) -> dict:
    """Collapse observations that are the same captured fact re-ingested a second time
    under an unchanged run_id after a parser output-shape change. Idempotent: once
    collapsed, every group has exactly one row and a second call is a no-op.

    A group is (source_id, run_id, source_key). Within a group, rows collapse only if
    identity_hints and evidence match exactly across all of them AND every `data` field
    they hold in common is byte-identical AND every field that differs is in
    `benign_fields`. Anything else about the group is left untouched and counted in
    `groups_skipped_unsafe` -- this function never guesses a device's real history away.

    `commit=False` runs the same logic and leaves every change in the open transaction
    for the caller to inspect or roll back -- the way to preview a run against a real
    file without a real edit ever landing on disk.
    """
    where = []
    params: list[str] = []
    if source_id is not None:
        where.append("source_id=?"); params.append(source_id)
    if run_id is not None:
        where.append("run_id=?"); params.append(run_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = connection.execute(
        f"SELECT id, source_id, run_id, source_key, payload_json FROM observations {clause}",
        params).fetchall()

    groups: dict[tuple[str, str, str], list[tuple[str, dict]]] = defaultdict(list)
    for obs_id, sid, rid, key, payload in rows:
        groups[(sid, rid, key)].append((obs_id, json.loads(payload)))

    delete_to_keep: dict[str, str] = {}
    groups_collapsed = 0
    groups_skipped_unsafe = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        first_identity_hints = members[0][1]["identity_hints"]
        first_evidence = members[0][1]["evidence"]
        core_views = [{k: v for k, v in payload["data"].items() if k not in benign_fields}
                      for _, payload in members]
        safe = (all(payload["identity_hints"] == first_identity_hints and
                    payload["evidence"] == first_evidence for _, payload in members)
                and all(cv == core_views[0] for cv in core_views))
        if not safe:
            groups_skipped_unsafe += 1
            continue
        ranked = sorted(members, key=lambda m: (-len(m[1]["data"]), m[0]))
        keep_id = ranked[0][0]
        for obs_id, _ in ranked[1:]:
            delete_to_keep[obs_id] = keep_id
        groups_collapsed += 1

    result = retire_observations(connection, delete_to_keep)
    if commit:
        connection.commit()
    result["groups_collapsed"] = groups_collapsed
    result["groups_skipped_unsafe"] = groups_skipped_unsafe
    return result
