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

Findings are returned, not raised, because a violation usually describes data
that is already ingested: aborting tonight's batch does not repair it, it only
stops the corpus being updated. And a finding nobody can see is not a finding,
so they are surfaced on /api/v1/admin/health as well as in the batch result.

TWO OF THESE CHECKS WERE WRONG, and both reported a healthy corpus as defective
before being caught. They are documented at their sites rather than quietly
deleted, because the failure mode -- trusting a new instrument that had never
been run against data that violates it -- is exactly the one this module exists
to prevent:

  * run accounting asserted accepted_count == count(observations). Adapters
    legitimately fan out: the TECNO feed splits a device group without inventing
    a day, so 828 accepted records emit 862 observations. accepted_count was
    right and the check was wrong.
  * vendor resolution checked the manufacturers table only, so all 425 Samsung
    products failed -- sources say "Samsung" where the canonical manufacturer is
    "Samsung Electronics", which the brands table already maps -- and it claimed
    they could "never join the canonical layer" when promotion creates the
    manufacturer on demand.

Severity is about what a reader would conclude, not about how alarming it
sounds:
  error   -- the corpus would make the UI state something false
  warning -- the corpus is internally inconsistent but nothing is misreported
Work that is merely PENDING is not a finding at all; see review_queue().
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

    # -- the grid must be able to see every device ---------------------------
    # Sharper than the firmware case: a device missing from the identity
    # projection is absent from the catalogue entirely, not merely missing its
    # firmware, and nothing else in the UI would reveal it.
    unlisted = _scalar(connection, """
        SELECT count(*) FROM hardware_models hm
         WHERE NOT EXISTS (SELECT 1 FROM device_catalog_flat d
                            WHERE d.hardware_model_id = hm.id)""")
    if unlisted:
        findings.append(Finding(
            "device_missing_from_catalogue_projection", "error", unlisted,
            f"{unlisted} hardware models are invisible in the device grid; the identity "
            "projection is stale (python3 -m mobile_observatory.current_firmware)"))

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

    # -- a run that accepted records must hold some --------------------------
    #
    # This check replaces an earlier one asserting
    #   accepted_count + rejected_count == count(observations)
    # which was WRONG, and wrong in the way worth writing down: it assumed one
    # source record yields one observation. Adapters legitimately fan out. The
    # TECNO security feed splits a device group without inventing a day, so
    # 828 accepted CSV records emit 862 observations -- measured exactly: 828
    # distinct artifact_pointers, 34 of them yielding two observations each.
    # accepted_count was right and the invariant was wrong, and it spent two
    # runs reporting a healthy corpus as defective.
    #
    # What IS a real fault is a run that claims to have accepted records while
    # holding none, with nothing else holding them either. A re-ingest under a
    # new run id is normal and leaves the old run empty -- that is supersession,
    # matched on parser_name because a re-ingest can also change the source id
    # (google.ota.checkin.tecno -> google.ota.checkin did exactly that).
    stranded = connection.execute("""
        SELECT r.id, r.accepted_count, r.parser_name
          FROM ingestion_runs r
         WHERE r.accepted_count > 0
           AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.run_id=r.id)
           AND NOT EXISTS (
                 SELECT 1 FROM ingestion_runs sibling
                  WHERE sibling.parser_name = r.parser_name AND sibling.id <> r.id
                    AND EXISTS (SELECT 1 FROM observations o2 WHERE o2.run_id=sibling.id))""").fetchall()
    if stranded:
        detail = ", ".join(f"{row[0]} claims {row[1]}" for row in stranded[:3])
        findings.append(Finding("run_accepted_but_holds_nothing", "error", len(stranded),
                                f"runs that accepted records, hold none, and were not superseded "
                                f"by a later run of the same parser ({detail})"))

    # -- source products must name a vendor the corpus can place -------------
    #
    # Resolved against brands as well as manufacturers, because
    # source_products.manufacturer holds whatever the SOURCE called the vendor,
    # and sources say "Samsung" where the canonical manufacturer is "Samsung
    # Electronics" -- a brand, which brands already maps. The earlier version
    # checked manufacturers alone and reported all 425 Samsung products as
    # unplaceable, along with a claim that they "can never join the canonical
    # layer". That claim was also false: CanonicalRepository._find_or_insert
    # creates a manufacturer on demand during promotion.
    #
    # What remains worth flagging is a vendor string the corpus cannot place at
    # ALL -- neither manufacturer nor brand -- because nothing else in the
    # corpus corroborates that the vendor exists.
    # Scoped to APPROVED products only, deliberately. An unapproved product
    # naming an unknown vendor is the evidence layer doing its job -- it exists
    # to hold identities the corpus has not vetted, and requiring its vendor
    # strings to pre-exist in the canonical layer would invert the two-layer
    # model. Apple sits here: 66 proposed products, no manufacturer and no
    # brand, and nothing wrong. review_queue() reports it as pending work.
    #
    # An APPROVED product naming a vendor nothing corroborates is different:
    # promotion will mint a manufacturer from that string, so a typo becomes a
    # permanent canonical row.
    unplaceable = connection.execute("""
        SELECT sp.manufacturer, count(*) FROM source_products sp
         WHERE sp.review_state = 'approved'
           AND NOT EXISTS (SELECT 1 FROM manufacturers m
                            WHERE m.canonical_name = sp.manufacturer COLLATE NOCASE)
           AND NOT EXISTS (SELECT 1 FROM brands b
                            WHERE b.canonical_name = sp.manufacturer COLLATE NOCASE)
         GROUP BY sp.manufacturer""").fetchall()
    if unplaceable:
        total = sum(row[1] for row in unplaceable)
        names = ", ".join(f"{row[0]} ({row[1]})" for row in unplaceable)
        findings.append(Finding("approved_product_unplaceable_vendor", "error", total,
                                f"approved products name a vendor matching no manufacturer and no "
                                f"brand, so promotion would mint one from the string: {names}"))

    order = {"error": 0, "warning": 1}
    findings.sort(key=lambda f: (order.get(f.severity, 2), -f.count))
    return findings


def review_queue(connection) -> list[dict]:
    """Evidence that is captured but not yet serving, and what is holding it.

    This is not a fault, so it is not a Finding -- it is the state of the review
    queue, and it exists because the UI otherwise shows a vendor as a silent
    zero. Apple is the clearest case: 4,450 ipsw.me firmware observations are in
    the corpus and 66 Apple products carry them, but every one is review_state
    'proposed', so promote_approved_product_observations (enrichment.py:219,
    `sp.review_state='approved'`) skips them and the catalogue shows no Apple
    device at all.

    That gate is correct and should stay closed. automate_identity_review
    approves only "exact, independently supported matches", and it corroborates
    against the Xiaomi catalogue, the GSMArena specs and the Google Play device
    list -- none of which carry Apple. So there is no evidence to approve on,
    and approving anyway would be inventing the corroboration the two-layer
    model exists to require.

    What was wrong was not the gate but the silence around it. A reader saw
    "Apple 0" and could not tell whether the corpus had never looked, had
    looked and found nothing, or was holding thousands of observations behind a
    review. This reports the third case as the number it is.
    """
    return [dict(row) for row in connection.execute("""
        SELECT sp.manufacturer AS vendor,
               count(DISTINCT sp.id) AS products,
               sum(sp.review_state='approved') AS approved,
               (SELECT count(*) FROM observation_product_links opl
                  JOIN source_products s2 ON s2.id = opl.product_id
                 WHERE s2.manufacturer = sp.manufacturer
                   AND s2.review_state <> 'approved') AS observations_awaiting_review,
               (SELECT count(DISTINCT phl.hardware_model_id) FROM product_hardware_links phl
                  JOIN source_products s3 ON s3.id = phl.product_id
                 WHERE s3.manufacturer = sp.manufacturer) AS canonical_devices
          FROM source_products sp
         GROUP BY sp.manufacturer
         ORDER BY observations_awaiting_review DESC, sp.manufacturer""")]


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
        print()
        print("review queue (captured evidence not yet serving):")
        print("  %-10s %9s %9s %9s %s" % ("vendor", "products", "approved", "devices", "obs awaiting review"))
        for row in review_queue(db.connection):
            print("  %-10s %9d %9d %9d %d" % (
                row["vendor"], row["products"], row["approved"],
                row["canonical_devices"], row["observations_awaiting_review"]))
        if args.strict and any(f.severity == "error" for f in findings):
            raise SystemExit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
