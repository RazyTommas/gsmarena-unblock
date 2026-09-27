"""Invariants that answer "how would I know the corpus is wrong?".

The answer before this module was: you would not, and the suite would stay
green. tests/test_schema.py runs PRAGMA foreign_key_check against
Database.migrated(), which defaults to ":memory:" -- a freshly migrated,
zero-row database. There is nothing in it to violate, so the one test in the
repo that looks like a corpus integrity check has never examined a row of the
corpus. tools/validate_product_batch.py and tools/validate_security_batch.py
have the same shape. batch.py ran no integrity check at all.

Every check below corresponds to a way the corpus has actually been wrong, or
could lie to a user, rather than to a way SQLite could complain. Each is written
so it CAN fail -- tests/test_integrity.py plants a violation for each one and
asserts it is caught, because a check that cannot fail is decoration.

Findings are returned, not raised. Two reasons. Several of these are violated by
the live corpus today: the run-accounting check finds 4 of 21 runs whose
accepted_count disagrees with the observations they actually hold, and the
manufacturer check finds 491 of 2,347 source_products whose manufacturer string
resolves to no manufacturers row. Those are pre-existing facts about data that
is already ingested; aborting tonight's batch does not repair them, it only
stops the corpus being updated. And a finding nobody can see is not a finding,
so they are surfaced on /api/v1/admin/health as well as in the batch result.

Severity is about what a reader would conclude, not about how alarming it
sounds:
  error   -- the corpus would make the UI state something false
  warning -- the corpus is internally inconsistent but nothing is misreported
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str  # 'error' | 'warning'
    count: int
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


def _scalar(connection, sql: str, params: tuple = ()) -> int:
    return connection.execute(sql, params).fetchone()[0]


def check_corpus(connection) -> list[Finding]:
    """Run every invariant. Returns findings, newest concern first."""
    findings: list[Finding] = []

    # -- referential integrity ------------------------------------------------
    orphans = connection.execute("PRAGMA foreign_key_check").fetchall()
    if orphans:
        tables = sorted({row[0] for row in orphans})
        findings.append(Finding("foreign_key_orphans", "error", len(orphans),
                                f"rows referencing a missing parent in: {', '.join(tables)}"))

    corrupt = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if corrupt != "ok":
        findings.append(Finding("database_integrity", "error", 1, corrupt[:200]))

    # -- a device must never be told firmware was not observed when it was ----
    # This is the defect device_current_firmware exists to remove, asserted
    # against the corpus rather than against a fixture. 156 of 303 devices were
    # in this state before the projection.
    invisible = _scalar(connection, """
        SELECT count(*) FROM hardware_models hm
         WHERE (EXISTS (SELECT 1 FROM firmware_releases f WHERE f.hardware_model_id=hm.id)
             OR EXISTS (SELECT 1 FROM product_firmware_releases p WHERE p.hardware_model_id=hm.id))
           AND NOT EXISTS (SELECT 1 FROM device_current_firmware d WHERE d.hardware_model_id=hm.id)""")
    if invisible:
        findings.append(Finding(
            "firmware_observed_but_not_served", "error", invisible,
            f"{invisible} devices have observed firmware that the read path cannot see; "
            "the projection is stale or was never built (python3 -m mobile_observatory.current_firmware)"))

    # -- the projection must not outlive the evidence it summarises ----------
    stale = connection.execute("""
        SELECT p.built_at, (SELECT max(observed_at) FROM observations)
          FROM projection_state p WHERE p.name='device_current_firmware'""").fetchone()
    if stale and stale[0] and stale[1] and stale[1] > stale[0]:
        findings.append(Finding(
            "projection_older_than_evidence", "warning", 1,
            f"observations run to {stale[1]} but the projection was built at {stale[0]}"))

    # -- one device, one layer ------------------------------------------------
    both = _scalar(connection, """
        SELECT count(*) FROM (SELECT hardware_model_id FROM device_current_firmware
                               GROUP BY hardware_model_id HAVING count(DISTINCT fact_layer)>1)""")
    if both:
        findings.append(Finding("device_served_from_both_layers", "error", both,
                                "precedence between the canonical and evidence layers is undefined"))

    # -- a patch level must be well formed and attributed --------------------
    bad_patch = _scalar(connection, """
        SELECT count(*) FROM device_current_firmware
         WHERE (security_patch_level IS NOT NULL
                AND security_patch_level NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
            OR (security_patch_level IS NULL AND security_patch_level_source_id IS NOT NULL)""")
    if bad_patch:
        findings.append(Finding("malformed_or_unattributed_patch_level", "error", bad_patch,
                                "a patch level is shown without a publisher, or is not a date"))

    # -- run accounting -------------------------------------------------------
    # A run whose accepted_count does not match the observations it holds is a
    # run that CANNOT be audited: a failed-partway run and a clean one look
    # identical to anything reading the counter. Measured: 4 of 21 live runs,
    # three of which claim 828/560/33 accepted while holding zero observations.
    mismatched = connection.execute("""
        SELECT r.id, r.accepted_count, r.rejected_count,
               (SELECT count(*) FROM observations o WHERE o.run_id=r.id)
          FROM ingestion_runs r
         WHERE ifnull(r.accepted_count,0)+ifnull(r.rejected_count,0)
               <> (SELECT count(*) FROM observations o WHERE o.run_id=r.id)""").fetchall()
    if mismatched:
        worst = ", ".join(f"{row[0]}: claims {row[1]}, holds {row[3]}" for row in mismatched[:3])
        findings.append(Finding("run_accounting_mismatch", "warning", len(mismatched),
                                f"runs whose counters disagree with their observations ({worst})"))

    # -- source products must name a manufacturer the corpus knows ----------
    unknown_maker = _scalar(connection, """
        SELECT count(*) FROM source_products sp
         WHERE NOT EXISTS (SELECT 1 FROM manufacturers m
                            WHERE m.canonical_name = sp.manufacturer COLLATE NOCASE)""")
    if unknown_maker:
        findings.append(Finding("source_product_unknown_manufacturer", "warning", unknown_maker,
                                "source_products.manufacturer resolves to no manufacturers row, so "
                                "these products can never join the canonical layer"))

    order = {"error": 0, "warning": 1}
    findings.sort(key=lambda f: (order.get(f.severity, 2), -f.count))
    return findings


def summarise(findings: list[Finding]) -> str:
    if not findings:
        return "corpus invariants: all clear"
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = len(findings) - errors
    return f"corpus invariants: {errors} error(s), {warnings} warning(s)"


def main() -> None:
    import argparse
    from pathlib import Path

    from .database import Database

    parser = argparse.ArgumentParser(description="Check corpus invariants")
    parser.add_argument("--data-dir", default=".observatory-data")
    parser.add_argument("--strict", action="store_true",
                        help="exit nonzero if any error-severity finding is present")
    args = parser.parse_args()

    db = Database.migrated(Path(args.data_dir) / "corpus.sqlite")
    try:
        findings = check_corpus(db.connection)
        print(summarise(findings))
        for finding in findings:
            print(f"  [{finding.severity}] {finding.check}: {finding.count} — {finding.detail}")
        if args.strict and any(f.severity == "error" for f in findings):
            raise SystemExit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
