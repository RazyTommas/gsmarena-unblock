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

from . import changesets, corpus_identity, search_index
from .adjudication import UNRESOLVABLE
from .current_firmware import PRIMARY_BASIS_DECIDED_BY_NAME
from .source_dates import ISO_DATE_GLOB, STATED_DATE_FIELDS


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


def check_corpus(connection, *, deep: bool = True, identity_baseline=None) -> list[Finding]:
    """Run the invariants. Returns findings, most serious first.

    `identity_baseline` overrides where the corpus-identity baseline is read
    from; by default it is `<the corpus file's directory>/corpus-identity.json`.
    Pass the baseline recorded on the corpus you believe you reproduced to ask
    whether you actually did -- see corpus_identity.py.

    `deep` runs the two whole-database page scans. They are the right checks and
    the wrong thing to do on every request: PRAGMA integrity_check alone is
    1,233ms on the live corpus, and wiring the full set into /api/v1/admin/health
    -- which the UI calls on every load -- took that endpoint from 57ms to
    2,066ms. What they detect is disk corruption, which does not appear between
    two page loads, so the batch and the CLI run them and the HTTP path does not.
    """
    findings: list[Finding] = []

    if deep:
        # -- referential integrity --------------------------------------------
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
    # Counted first, scanned only if the counts disagree. Every projection row
    # names a device that has firmware, and at most one row per
    # (device,target,channel), so "as many distinct devices in the projection as
    # have firmware" is equivalent to "the same devices" -- and it is two
    # aggregates instead of a per-device existence test over the whole
    # catalogue. At 153,896 devices the scan form cost 1,071ms on an endpoint
    # the UI polls; this is the same answer without paying for it every time.
    # "Firmware observed FOR THIS DEVICE" excludes a build whose identifier names
    # a sibling model -- the same rule the projection selects by. Without the
    # matching exclusion this check contradicted the selection and reported TECNO
    # i3 as broken: its only build is "i3Pro-...", correctly not chosen, and
    # correctly reported as no current firmware.
    # The tie test asks, per candidate row, "does another source sharing my
    # currency_rank describe my (model, region, channel) partition?" As a
    # correlated NOT EXISTS that was an index search over
    # product_firmware_releases plus two `sources` lookups for each of the 10,135
    # rows with a hardware model, and it cost 205ms of this check's 211ms --
    # measured on the live corpus, warm disk, by timing the predicates
    # separately. It is the same question partition_publishers_tied_on_rank
    # answers a few checks below as ONE aggregate, so it is computed once here
    # too: 211.3ms -> 22.7ms, returning the identical set of 845 ids.
    #
    # `=` and not `IS` in the membership test, deliberately. The correlated form
    # compared with `=`, so a NULL on either side made EXISTS false and KEPT the
    # row; GROUP BY instead puts all NULLs in one group, so `IS` here would start
    # dropping rows the original kept. LEFT JOIN on `sources` for the same
    # reason: the original's inner join made a row with an unresolvable source_id
    # fail the EXISTS and survive, where an inner join here would delete it.
    with_firmware = _scalar(connection, """
        WITH tied_partitions AS (
          SELECT pfr.hardware_model_id AS hardware_model_id, pfr.region_code AS region_code,
                 pfr.channel AS channel, s.currency_rank AS currency_rank
            FROM product_firmware_releases pfr
            JOIN sources s ON s.id = pfr.source_id
           WHERE pfr.hardware_model_id IS NOT NULL
           GROUP BY 1, 2, 3, 4
          HAVING count(DISTINCT pfr.source_id) > 1)
        SELECT count(*) FROM (
          SELECT hardware_model_id FROM firmware_releases
           UNION
          SELECT pfr.hardware_model_id FROM product_firmware_releases pfr
           LEFT JOIN sources ms ON ms.id = pfr.source_id
           WHERE pfr.hardware_model_id IS NOT NULL
             -- Tied publishers leave a partition unanswered by design; a device
             -- whose only partitions are tied has firmware and correctly has no
             -- current-firmware row. See partition_publishers_tied_on_rank.
             AND NOT EXISTS (
               SELECT 1 FROM tied_partitions t
                WHERE t.hardware_model_id = pfr.hardware_model_id
                  AND t.region_code = pfr.region_code
                  AND t.channel = pfr.channel
                  AND t.currency_rank = ms.currency_rank)
             AND NOT EXISTS (
               SELECT 1 FROM device_catalog_flat d
                WHERE d.hardware_model_id = pfr.hardware_model_id
                  AND instr(pfr.build_id,'-') > 1
                  AND lower(substr(pfr.build_id, 1, instr(pfr.build_id,'-') - 1)) LIKE
                      lower(replace(replace(replace(d.model_code, d.brand || ' ', ''),
                                            d.brand || '-', ''), ' ', '')) || '_%'))""")
    served = _scalar(connection, "SELECT count(DISTINCT hardware_model_id) FROM device_current_firmware")
    invisible = 0
    if with_firmware != served:
        invisible = _scalar(connection, """
            SELECT count(*) FROM hardware_models hm
             WHERE (EXISTS (SELECT 1 FROM firmware_releases f WHERE f.hardware_model_id=hm.id)
                 OR EXISTS (SELECT 1 FROM product_firmware_releases p
                             WHERE p.hardware_model_id=hm.id
                               AND NOT EXISTS (
                                 SELECT 1 FROM device_catalog_flat d
                                  WHERE d.hardware_model_id = p.hardware_model_id
                                    AND instr(p.build_id,'-') > 1
                                    AND lower(substr(p.build_id, 1, instr(p.build_id,'-') - 1)) LIKE
                                        lower(replace(replace(replace(d.model_code, d.brand || ' ', ''),
                                                              d.brand || '-', ''), ' ', '')) || '_%')))
               AND NOT EXISTS (SELECT 1 FROM device_current_firmware d WHERE d.hardware_model_id=hm.id)""")
    if invisible:
        findings.append(Finding(
            "firmware_observed_but_not_served", "error", invisible,
            f"{invisible} devices have observed firmware that the read path cannot see; "
            "the projection is stale or was never built (PYTHONPATH=src python3 -m mobile_observatory.current_firmware)"))

    # -- the grid must be able to see every device ---------------------------
    # Sharper than the firmware case: a device missing from the identity
    # projection is absent from the catalogue entirely, not merely missing its
    # firmware, and nothing else in the UI would reveal it.
    # Same shape. device_catalog_flat's primary key is hardware_model_id and it
    # references hardware_models, so every row names a distinct real device:
    # equal counts and a bijection are the same statement here.
    catalogued = _scalar(connection, "SELECT count(*) FROM hardware_models")
    flattened = _scalar(connection, "SELECT count(*) FROM device_catalog_flat")
    unlisted = 0
    if catalogued != flattened:
        unlisted = _scalar(connection, """
            SELECT count(*) FROM hardware_models hm
             WHERE NOT EXISTS (SELECT 1 FROM device_catalog_flat d
                                WHERE d.hardware_model_id = hm.id)""") or abs(catalogued - flattened)
    if unlisted:
        findings.append(Finding(
            "device_missing_from_catalogue_projection", "error", unlisted,
            f"{unlisted} hardware models are invisible in the device grid; the identity "
            "projection is stale (PYTHONPATH=src python3 -m mobile_observatory.current_firmware)"))

    # -- the projection must not outlive what it was built from --------------
    # Compares the INPUT state, not two clocks. The previous version compared
    # max(observed_at) against built_at, which reads healthy for a replay whose
    # observation times are older than the build, for a promotion that creates
    # devices without adding an observation, and for a currency_rank change that
    # alters which build every affected device is said to be running.
    from .current_firmware import input_fingerprint

    # Degrades rather than raising on a corpus that predates the column: an
    # integrity checker that crashes reports nothing at all, which is worse than
    # reporting one check less.
    has_fingerprint = any(row[1] == "input_fingerprint"
                          for row in connection.execute("PRAGMA table_info(projection_state)"))
    recorded = connection.execute(
        "SELECT input_fingerprint FROM projection_state WHERE name='device_current_firmware'"
    ).fetchone() if has_fingerprint else None
    if recorded and recorded[0] and recorded[0] != input_fingerprint(connection):
        findings.append(Finding(
            "projection_older_than_its_inputs", "warning", 1,
            "the tables the projection is built from have changed since it was published; "
            "rebuild with PYTHONPATH=src python3 -m mobile_observatory.current_firmware"))

    # -- exactly one headline row per device ----------------------------------
    # devices_page dropped its GROUP BY because both projections are one row per
    # device. _validate() proves that of the STAGING table at one build instant;
    # nothing proved it of the table being served. The partial index is not
    # unique, so a repair, a migration bug or a hand-edit could mark two rows
    # primary and every affected device would silently render twice and inflate
    # the page total.
    # Two index reads instead of grouping the whole projection. Both conditions
    # are needed and together they are exact: if one device had no primary and
    # another had two, the TOTAL would still match, but the count of distinct
    # devices holding one would not.
    devices_total = _scalar(connection, "SELECT count(DISTINCT hardware_model_id) FROM device_current_firmware")
    primaries = _scalar(connection, "SELECT count(*) FROM device_current_firmware WHERE is_device_primary=1")
    devices_with_primary = _scalar(connection,
        "SELECT count(DISTINCT hardware_model_id) FROM device_current_firmware WHERE is_device_primary=1")
    bad_primary = 0
    if primaries != devices_total or devices_with_primary != devices_total:
        bad_primary = _scalar(connection, """
            SELECT count(*) FROM (SELECT hardware_model_id FROM device_current_firmware
                                   GROUP BY hardware_model_id HAVING sum(is_device_primary) <> 1)""")
    if bad_primary:
        findings.append(Finding("device_without_exactly_one_primary_row", "error", bad_primary,
                                "the device grid would duplicate or drop these devices and "
                                "miscount its own total"))

    # -- one device, one layer ------------------------------------------------
    # Counted per layer and summed. A device in both layers is counted twice, so
    # the sum exceeding the distinct total is exactly the condition -- without
    # grouping 485,323 rows to find out.
    canonical_devices = _scalar(connection,
        "SELECT count(DISTINCT hardware_model_id) FROM device_current_firmware WHERE fact_layer='canonical'")
    evidence_devices = _scalar(connection,
        "SELECT count(DISTINCT hardware_model_id) FROM device_current_firmware WHERE fact_layer='evidence'")
    both = 0
    if canonical_devices + evidence_devices != devices_total:
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

    # -- a source states a non-date where a date belongs ----------------------
    # The captured payload is the archive and is never rewritten, so this defect
    # stays in the corpus by design; what must not stay is it reaching a reader as
    # a date. Reported the way firmware_build_names_a_sibling_model reports its
    # class: name the number, attribute it to the source, and let the reader see it
    # is a source's mistake rather than the corpus's.
    #
    # WARNING, not error. The distinction this module uses is "would the corpus make
    # the UI state something false". Since migration 0031 it would not: the derived
    # columns read a non-date as absence and the value falls through to the capture
    # time exactly as a JSON null already did. Before 0031 this was an error-class
    # fault and it was live -- `observations.effective_at` published the word `null`
    # as the effective date of six Xiaomi builds and `releases_page` printed it
    # unguarded. It is reported at all because a source that has started writing
    # `"null"` where it used to write a date is a change in that source worth
    # seeing, and because absence that a source SPELLS is different from absence it
    # leaves empty. The companion error-severity check is the next one.
    sentinel_dates = _scalar(connection, f"""
        SELECT count(*) FROM observations
         WHERE {' OR '.join(
             f"(json_extract(payload_json,'{field}') IS NOT NULL"
             f" AND json_extract(payload_json,'{field}') NOT GLOB '{ISO_DATE_GLOB}*')"
             for field in STATED_DATE_FIELDS)}""")
    if sentinel_dates:
        findings.append(Finding("source_states_a_non_date_where_a_date_belongs", "warning",
                                sentinel_dates,
                                "these observations carry a value in a release/publish date field "
                                "that is not a date -- the source wrote absence as a value. The "
                                "payload keeps it because the payload is the archive; the derived "
                                "columns read it as absence (see source_dates.py)"))

    # -- a derived date column must never hold a non-date ---------------------
    # The error-severity half. source_dates is the single rule and it is applied
    # where these values are PRODUCED, so a non-date here cannot be a source's
    # doing: it means a write bypassed the rule, or migration 0031's redefinition of
    # observations.effective_at was reverted. Either way a reader is being shown a
    # non-date labelled as a date, which is the definition this module uses for an
    # error.
    derived_non_dates = _scalar(connection, f"""
        SELECT (SELECT count(*) FROM observations
                 WHERE effective_at IS NOT NULL
                   AND effective_at NOT GLOB '{ISO_DATE_GLOB}*')
             + (SELECT count(*) FROM product_firmware_releases
                 WHERE vendor_released_at IS NOT NULL
                   AND vendor_released_at NOT GLOB '{ISO_DATE_GLOB}*')
             + (SELECT count(*) FROM product_security_publications
                 WHERE published_at IS NOT NULL
                   AND published_at NOT GLOB '{ISO_DATE_GLOB}*')""")
    if derived_non_dates:
        findings.append(Finding("derived_date_column_holds_a_non_date", "error", derived_non_dates,
                                "a derived date column holds a value that is not a date, so a "
                                "reader is shown a non-date under a date label; every writer of "
                                "these columns is supposed to go through source_dates"))

    # -- a published "came after" claim must say what decided the order -------
    # The three checks below are one family. `android_version_changed` states that
    # one build came after another, and the pair is chosen by
    # `ORDER BY ... vendor_released_at, id`; with both dates NULL the sort falls
    # through to `id`, which is a uuid5 digest. 5,643 of 5,804 published events
    # were ordered that way because the promotion read one publisher's field names
    # (see source_dates.py). They are not retracted -- domain_events is append-only
    # by trigger and deleting published facts is the larger harm -- so what has to
    # hold instead is that a reader can tell the two kinds of claim apart.
    #
    # The ERROR is the missing basis, not the row-id ordering. Row-id ordering is a
    # fact about the evidence and it is reported as such; an event with no basis
    # row is a reader who cannot find out, which is the thing this corpus is not
    # allowed to do.
    #
    # Guarded on the TABLE and not on the schema version, because that is the
    # thing these two queries actually need. `check_corpus` is pointed at restored
    # backups and at corpora a tool opened read-only, where migration 0034 may not
    # have run, and a check that raises `no such table` reports every other
    # finding as "the check crashed" -- which is how one missing table becomes a
    # health endpoint that answers nothing. The absence is itself reported below
    # rather than passed over in silence.
    has_ordering = bool(connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='domain_event_ordering'"
    ).fetchone())
    ordering_events = _scalar(connection, """
        SELECT count(*) FROM domain_events WHERE event_type='android_version_changed'""")
    # WARNING and not error, by this module's own test -- "would the corpus make
    # the UI state something false". It would not: the feed detects the table's
    # absence (ObservatoryService._event_ordering_available) and reports the basis
    # as null, which the client renders as "Order basis not recorded". That is a
    # true sentence about a corpus that genuinely cannot attribute these claims.
    # The error-severity half is the next check: rows missing while the table
    # EXISTS can only mean a writer bypassed firmware_order, which is the same
    # split as source_states_a_non_date (warning, the source's defect) against
    # derived_date_column_holds_a_non_date (error, a write bypassed the rule).
    if not has_ordering and ordering_events:
        findings.append(Finding("event_order_basis_table_absent", "warning", ordering_events,
                                "this corpus publishes events claiming one build came after "
                                "another and has no domain_event_ordering table to say what "
                                "decided the order -- migration 0034 has not been applied, so "
                                "every one of these claims is unattributable. Apply it: the "
                                "batch and the server both do, inside the batch lock"))
    unexplained_order = _scalar(connection, """
        SELECT count(*) FROM domain_events de
         WHERE de.event_type='android_version_changed'
           AND NOT EXISTS (SELECT 1 FROM domain_event_ordering o WHERE o.event_id=de.id)""") \
        if has_ordering else 0
    if unexplained_order:
        findings.append(Finding("event_order_basis_not_recorded", "error", unexplained_order,
                                "these android_version_changed events state that one build came "
                                "after another and nothing records what decided that order. "
                                "promote_approved_product_observations writes the basis WITH the "
                                "event (firmware_order.record_ordering_basis), so a missing row "
                                "means a writer bypassed it or migration 0034 was reverted -- and "
                                "a basis cannot honestly be filled in later, because the releases "
                                "may have been re-dated since"))

    row_id_ordered = _scalar(connection, """
        SELECT count(*) FROM domain_event_ordering
         WHERE ordering_basis IN ('observation_order_only','mixed_dated_and_undated',
                                  'cited_releases_absent')""") if has_ordering else 0
    if row_id_ordered:
        findings.append(Finding("firmware_order_decided_by_row_id_not_a_date", "warning",
                                row_id_ordered,
                                "these published events claim one build came after another on an "
                                "order that no stated release date supports: the pair was sorted "
                                "by its uuid5 row id, or by one date against an absent one, or it "
                                "cites releases the corpus no longer holds. Each row names its own "
                                "basis in domain_event_ordering and the feed prints it. They are "
                                "left published deliberately -- see firmware_order.py; this is the "
                                "size of what is already out there, not a repair that is pending"))

    # -- a headline build decided by a publisher's NAME, or by nothing --------
    # ASKED OF THE RECORDED ANSWER, not re-derived from the inputs.
    #
    # The first version of this check counted devices that *could* be decided by
    # `source_id` -- two publishers, same `currency_rank`, same `latest_basis` --
    # and reported 243. That is the population at risk, not the population it
    # happened to. Since migration 0035 the projection records which ordering key
    # actually separated the winner from the runner-up
    # (`device_current_firmware.device_primary_basis`, derived from
    # current_firmware._PRIMARY_KEYS in the same pass as the pick), so the honest
    # count is of devices where the name really did decide. Measuring the risk
    # when the fact is available is the "a true sentence missing its qualifier is
    # a wrong one" shape: 243 was not false, but it was not the number anybody
    # reading it thought it was.
    #
    # Guarded on the column, for the reason the ordering guard above is guarded on
    # its table: this runs against restored backups and corpora a tool opened
    # read-only, and `no such column` reports every other finding as a crash.
    has_primary_basis = any(
        row[1] == "device_primary_basis"
        for row in connection.execute("PRAGMA table_info(device_current_firmware)"))
    if not has_primary_basis:
        if _scalar(connection, "SELECT count(*) FROM device_current_firmware"
                               " WHERE is_device_primary=1"):
            findings.append(Finding(
                "headline_build_basis_not_recorded", "warning",
                _scalar(connection, "SELECT count(*) FROM device_current_firmware"
                                    " WHERE is_device_primary=1"),
                "the grid shows one build per device out of several candidates and this corpus "
                "records nothing about what chose it -- migration 0035 has not been applied, so "
                "the pick cannot be audited"))
    else:
        name_decided = _scalar(connection, f"""
            SELECT count(*) FROM device_current_firmware
             WHERE is_device_primary=1
               AND device_primary_basis='{PRIMARY_BASIS_DECIDED_BY_NAME}'""")
        if name_decided:
            findings.append(Finding("headline_build_decided_by_publisher_name", "warning",
                                    name_decided,
                                    "for these devices the build the grid shows was chosen by the "
                                    "publisher's NAME: two publishers describe the device, the "
                                    "corpus has no basis to rank them (same currency_rank) and "
                                    "their dates are not the same kind of measurement, so "
                                    "source_id ascending -- alphabetically -- decided it. "
                                    "Assigning one of them a higher currency_rank is a human "
                                    "judgement about authority and is not made in code"))

        # The other confession `device_primary_basis` can hold: nothing in the
        # evidence separated the candidates and the row's own coordinates broke
        # the tie. Reported separately because the repair is different -- a
        # currency_rank fixes the one above and cannot fix this one.
        #
        # And deliberately NOT counting `one_publishers_region_choice` with it.
        # That value means the runner-up was the SAME publisher's row for another
        # region, so the arbitrariness is which region the grid shows -- which the
        # grid already shows, beside the device's region count. Measured on the
        # live corpus the two are 1 and 364; folding them together would report
        # 365 unresolvable ties, a true sentence that reads as a far worse fact
        # than the one it describes.
        arbitrary = _scalar(connection, """
            SELECT count(*) FROM device_current_firmware
             WHERE is_device_primary=1 AND device_primary_basis='arbitrary_stable_order'""")
        if arbitrary:
            findings.append(Finding("headline_build_tie_broken_arbitrarily", "warning", arbitrary,
                                    "for these devices two DIFFERENT publishers describe a build "
                                    "the evidence cannot separate -- same basis, same stated "
                                    "release date, same Android major -- so the pick was broken on "
                                    "the row's own (target, channel), which is stable across "
                                    "rebuilds and means nothing. Recorded rather than presented "
                                    "as a decision. Whether two such builds are the same build "
                                    "under two naming conventions is an identity judgement"))

    # -- the same field-name mismatch, in the one place it is still open ------
    # `observations.effective_at` (migration 0031) coalesces over
    # source_dates.STATED_DATE_FIELDS, which does not list `$.data.vendor_released_at`,
    # so for every mifirm observation the generated column falls through to the
    # capture time while a real vendor date sits in the payload beside it. The
    # promotion path is fixed; this one is not, and the reason is in
    # source_dates.py: `effective_at` already mixes a vendor release date, a
    # bulletin publication date and a capture time in one sortable column, the
    # Explore observations tab sorts on it, and adding a fourth publisher's real
    # dates changes that ordering for 46% of the corpus. The honest repair is the
    # banding decision `devices_page` already had to make, which is a judgement.
    # Reported so it is loud rather than silent, and counted so it cannot be
    # described as small without someone looking.
    #
    # `deep` guards it for the reason at the top of this function, measured rather
    # than assumed: it is a 96,319-row scan evaluating a VIRTUAL generated column
    # and a json_extract per row, 222ms warm on the live corpus, and
    # `check_corpus(deep=False)` is what /api/v1/admin/health runs on every page
    # load. The quantity it measures is a property of the CAPTURE and changes only
    # when a batch ingests, so once per batch is the right cadence and not a
    # concession.
    unread_stated_dates = _scalar(connection, f"""
        SELECT count(*) FROM observations
         WHERE effective_at = observed_at
           AND json_extract(payload_json,'$.data.vendor_released_at')
                 GLOB '{ISO_DATE_GLOB}*'""") if deep else 0
    if unread_stated_dates:
        findings.append(Finding("source_stated_release_date_not_read_by_the_generated_column",
                                "warning", unread_stated_dates,
                                "these observations state a vendor release date under a field name "
                                "source_dates.STATED_DATE_FIELDS does not list, so "
                                "observations.effective_at reports the capture time instead. "
                                "product_firmware_releases.vendor_released_at is NOT affected -- "
                                "that path reads every declared spelling. This is the same "
                                "mismatch in the one consumer where closing it would change a "
                                "read-path ordering; see source_dates.py"))

    # -- an adjudicated product must record why -------------------------------
    # `unresolvable_on_captured_evidence` is a terminal state an agent rule sets
    # without a human, so a row carrying it with no recorded basis is a conclusion
    # nobody can re-check -- and re-checkability is the whole reason migration 0029
    # exists.
    #
    # ONE store, asked one way. A rationale row is normally keyed on one of the
    # product's identities, and is keyed on the product itself (identity_id NULL)
    # for the two products that have no identity to key it on. Written as a single
    # NOT EXISTS over `product_id` rather than as a CASE choosing between two
    # stores, because a check with two branches is a check whose untaken branch is
    # never exercised.
    unrecorded = _scalar(connection, f"""
        SELECT count(*) FROM source_products sp
         WHERE sp.review_state='{UNRESOLVABLE}'
           AND NOT EXISTS (SELECT 1 FROM identity_resolution_rationales r
                            WHERE r.product_id = sp.id
                              AND r.outcome='adjudicated_unresolvable')""")
    if unrecorded:
        findings.append(Finding("adjudicated_product_without_recorded_basis", "error", unrecorded,
                                "these products rest in a terminal state an agent set, with no "
                                "recorded basis for it, so nobody can re-check the decision or "
                                "tell it from a human's"))

    # -- approved evidence must not be stranded -------------------------------
    # An approved product carries firmware. If it never reached a device, that
    # firmware is in the corpus and unreachable from the catalogue -- the same
    # falsehood as "firmware not observed", one layer up. It went unnoticed for
    # a full promotion run because the only signal was a skipped_collision
    # counter printed once into a batch log, while 1,287 releases went dark and
    # 46 devices showed nothing with their own builds present.
    #
    # A refusal RECORDED in source_data_corrections is exempt: that is a
    # decision someone can audit, not a silent drop.
    # Scoped to products that COULD key a device -- exactly one Google Play
    # model code. An approved product without one cannot be promoted at all and
    # belongs in the evidence layer by design; 352 Xiaomi products approved from
    # the vendor codename catalogue sit there legitimately, and flagging them
    # would be this check asserting that the two-layer model is a bug.
    stranded = _scalar(connection, """
        SELECT count(*) FROM source_products sp
          JOIN identity_conclusions ic ON ic.product_id = sp.id
         WHERE sp.review_state = 'approved'
           AND NOT EXISTS (SELECT 1 FROM product_hardware_links l WHERE l.product_id = sp.id)
           AND EXISTS (SELECT 1 FROM product_firmware_releases p
                        WHERE p.product_id = sp.id AND p.hardware_model_id IS NULL)
           AND NOT EXISTS (SELECT 1 FROM source_data_corrections c
                            WHERE c.entity_type = 'source_product' AND c.entity_id = sp.id)
           -- One code across ALL the evidence, which is what
           -- _observed_model_code requires. Asking whether ANY entry holds a
           -- single code was looser than the rule it checks, and flagged 32
           -- products that promotion correctly refuses: Xiaomi Mi 10 carries
           -- two entries naming "Mi 10" and "Umi", so which code the device is
           -- keyed by is exactly what is unknown.
           AND (SELECT count(DISTINCT code.value)
                  FROM json_each(ic.evidence_json) e,
                       json_each(json_extract(e.value,'$.model_codes')) code
                 WHERE json_extract(e.value,'$.source') = 'google_play_supported_devices') = 1""")
    if stranded:
        findings.append(Finding(
            "approved_product_firmware_unreachable", "error", stranded,
            f"{stranded} approved products hold firmware that no canonical device can reach, "
            "and no recorded decision explains why"))

    # -- a link may not hold an approval nobody granted it --------------------
    # observation_product_links.link_state mirrors the resolution_state of the
    # registry row the link names -- FOR THIS PRODUCT. A link that is 'approved'
    # while no approved registry row ties its identity to its own product is a
    # state that should not be representable: promotion's other gate
    # (sp.review_state='approved') is the only thing between it and serving.
    #
    # Measured before the fix: 13. Nine on the still-proposed product "Redmi 1",
    # holding the approval of "Redmi 1 W"; four on "MI 3", holding "MI 3 / Mi 4"'s.
    # Nothing was misreported, which is exactly why it went unseen -- so this is a
    # warning, and the repair lives in the pipeline
    # (identity_bridge.refresh_observation_link_states) rather than in a one-off
    # script a later ingest would undo.
    unlicensed_links = _scalar(connection, """
        SELECT count(*) FROM observation_product_links
         WHERE link_state = 'approved'
           AND NOT EXISTS (SELECT 1 FROM source_identity_registry sir
                            WHERE sir.id = observation_product_links.identity_id
                              AND sir.product_id = observation_product_links.product_id
                              AND sir.resolution_state = 'approved')""")
    if unlicensed_links:
        findings.append(Finding(
            "observation_link_approved_without_an_approved_identity", "warning", unlicensed_links,
            f"{unlicensed_links} observation links are approved to serve while no approved "
            "source identity ties them to the product they name; the approval was granted "
            "elsewhere (PYTHONPATH=src python3 -m mobile_observatory.batch reconciles them)"))

    # -- and the reason those links exist ------------------------------------
    # source_identity_registry.id is uuid5(source_id, namespace, normalized_value):
    # no product component. Combined with
    # ON CONFLICT(source_id,namespace,normalized_value), the row keeps whichever
    # product_id inserted it FIRST, while a later observation naming the same
    # source value can resolve to a different product and link to that one.
    #
    # On the live corpus that is 18 links across four source values. The clearest
    # is model code HM2013023, which the Xiaomi tracker publishes both as
    # "Redmi 1 W Global" -- the name the vendor catalogue confirms -- and as
    # "Redmi 1 China / Global / Taiwan", which the region strip turns into a
    # SEPARATE product "Redmi 1".
    #
    # Reported rather than repaired, and deliberately: deciding whether "Redmi 1"
    # and "Redmi 1 W" (or "MI 3" and "MI 3 / Mi 4", or TECNO's ACE2N and itel's)
    # are one phone is an identity judgement. The refusal is safe -- an approval is
    # never inferred across products -- and this is the number that says a
    # judgement is outstanding rather than letting it be taken by an id collision.
    crossed = connection.execute("""
        SELECT sir.source_value, owner.canonical_name, named.canonical_name, count(*)
          FROM observation_product_links opl
          JOIN source_identity_registry sir ON sir.id = opl.identity_id
          JOIN source_products owner ON owner.id = sir.product_id
          JOIN source_products named ON named.id = opl.product_id
         WHERE sir.product_id <> opl.product_id
         GROUP BY 1, 2, 3 ORDER BY count(*) DESC""").fetchall()
    if crossed:
        pairs = "; ".join(f"{row[0]}: {row[2]!r} vs {row[1]!r} ({row[3]})" for row in crossed[:4])
        findings.append(Finding(
            "observation_link_identity_owned_by_another_product", "warning",
            sum(row[3] for row in crossed),
            f"one captured source value resolves to more than one product, so these links "
            f"name a product their identity row does not belong to; whether they are the "
            f"same device is an outstanding identity decision: {pairs}"))

    # -- a build that names a sibling model ----------------------------------
    # Transsion build identifiers start with the model they were built for, so a
    # row whose build begins with the device's code PLUS MORE ("L9Plus-..." on
    # the device "L9") is a sibling model's ROM filed under this one by the
    # source. It is the source's mistake, not a join error -- the product really
    # is "L9" -- and it cannot be corrected from here without inventing the
    # sibling device. What it must not be is invisible: a reader looking at
    # TECNO L9 sees two builds that are not for their phone.
    #
    # Deliberately narrow. It fires only when the prefix STARTS WITH the
    # device's own code and is longer, so build formats that do not encode a
    # model never reach it.
    mislabelled_build = _scalar(connection, """
        SELECT count(*) FROM product_firmware_releases pfr
          JOIN device_catalog_flat d ON d.hardware_model_id = pfr.hardware_model_id
         WHERE instr(pfr.build_id,'-') > 1
           AND lower(substr(pfr.build_id, 1, instr(pfr.build_id,'-') - 1)) <>
               lower(replace(replace(replace(d.model_code, d.brand || ' ', ''), d.brand || '-', ''), ' ', ''))
           AND lower(substr(pfr.build_id, 1, instr(pfr.build_id,'-') - 1)) LIKE
               lower(replace(replace(replace(d.model_code, d.brand || ' ', ''), d.brand || '-', ''), ' ', '')) || '_%'""")
    if mislabelled_build:
        findings.append(Finding("firmware_build_names_a_sibling_model", "warning", mislabelled_build,
                                "these builds identify a different model than the device they are "
                                "attached to; the source filed a sibling model's ROM under this code"))

    # -- partitions no publisher ordering can settle --------------------------
    # Two publishers describing the same device, region and channel, with the
    # same currency_rank. The projection leaves these unanswered rather than
    # comparing their dates with each other, so the device simply has no current
    # firmware for that region. Reported because "no answer" and "not collected"
    # look identical to a reader, and only one of them can be fixed by ranking
    # the sources.
    tied = _scalar(connection, """
        SELECT count(*) FROM (
          SELECT 1 FROM product_firmware_releases pfr
            JOIN sources s ON s.id = pfr.source_id
           WHERE pfr.hardware_model_id IS NOT NULL
           GROUP BY pfr.hardware_model_id, pfr.region_code, pfr.channel, s.currency_rank
          HAVING count(DISTINCT pfr.source_id) > 1)""")
    if tied:
        findings.append(Finding("partition_publishers_tied_on_rank", "warning", tied,
                                "these (device, region, channel) partitions are described by "
                                "publishers sharing a currency_rank, so no current firmware is "
                                "stated for them; give the sources distinct ranks to resolve"))

    # -- one region label, two vendor namespaces ------------------------------
    # firmware_targets is UNIQUE(vendor_namespace, target_code), so one code may
    # exist under several namespaces. The served projection is keyed on the CODE,
    # because the code is what a reader sees as the region. Two namespaces
    # claiming one code for one device is therefore an ambiguity the projection
    # cannot express, and it used to be worse than unexpressed: it violated the
    # staging primary key and killed the entire batch, so one device's ambiguity
    # cost every other device its projection. Those partitions are now excluded
    # and counted here instead.
    # Asked of firmware_releases directly rather than of v_latest_firmware, which
    # was 360ms of this endpoint's 618ms -- the single most expensive check --
    # measured on the live corpus, warm disk.
    #
    # v_latest_firmware exists to pick WHICH release is current in each
    # (hardware_model_id, ifnull(firmware_target_id,''), channel) partition: that
    # is its window PARTITION BY, and it keeps recency_rank = 1, so it emits
    # exactly one row per partition. This check groups by
    # (hardware_model_id, target_code, channel) and counts distinct target ids --
    # both the group key and the counted expression are functions of the partition
    # key ALONE. Which row won a partition therefore cannot change the answer, so
    # the entire ranking the view computes (two window functions over 21,186
    # releases, plus the samsung.fota json_extract scan behind source_positions)
    # was computed and thrown away. DISTINCT over the partition key is the same
    # input set: 726 triples instead of 21,186 rows, 360.3ms -> 3.6ms.
    #
    # ifnull() on the target id matches the view's partition expression exactly,
    # so a NULL and an empty-string target collapse into one group here as they do
    # there. The equality is proven over the live corpus and over constructed data
    # where the finding is NON-ZERO by
    # tests/test_integrity_fast_checks_agree.py -- the live corpus reports 0 here,
    # and two queries agreeing on an empty answer prove nothing.
    #
    # ONE PRECONDITION, found by testing the NULL case rather than reasoning about
    # it: this joins firmware_targets on the COLLAPSED id, where the view joined on
    # whichever raw id won the partition. Those differ only if a single
    # (model, channel) partition holds both a NULL target and an empty-string one
    # AND a firmware_target exists whose id is the empty string -- otherwise
    # `ft.id = ''` matches nothing and both forms produce the same empty code.
    # No firmware_target has an empty id (0 of 19 on the corpus) and a
    # firmware_target_id of '' is unreachable while the foreign key holds, because
    # there is no parent row to point at. The test asserts that precondition
    # directly and demonstrates the divergence when it is violated, so this is a
    # guarded assumption rather than a silent one.
    shared_code = _scalar(connection, """
        SELECT count(*) FROM (
          SELECT d.hardware_model_id, coalesce(ft.target_code,'') AS code, d.channel
            FROM (SELECT DISTINCT hardware_model_id, ifnull(firmware_target_id,'') AS target_id,
                         channel FROM firmware_releases) d
            LEFT JOIN firmware_targets ft ON ft.id = d.target_id
           GROUP BY 1, 2, 3
          HAVING count(DISTINCT d.target_id) > 1)""")
    if shared_code:
        findings.append(Finding("device_targets_share_a_region_code", "warning", shared_code,
                                "these (device, region code, channel) partitions are claimed by "
                                "more than one vendor namespace, so which namespace's build is "
                                "current for that region is unknown; no firmware is stated for "
                                "them rather than one namespace being picked silently"))

    # -- one vendor, one spelling --------------------------------------------
    # Sources capitalise vendors differently ("Tecno"/"TECNO", "Itel"/"itel").
    # CanonicalRepository matches manufacturers EXACTLY, so two spellings become
    # two vendors: the grid splits, every per-vendor total is wrong, and nothing
    # reports it. Caught in a dry run when a spec-only product arrived as
    # "Tecno" beside 618 products named "TECNO".
    split_vendors = connection.execute("""
        SELECT lower(manufacturer), count(DISTINCT manufacturer), group_concat(DISTINCT manufacturer)
          FROM source_products GROUP BY lower(manufacturer) HAVING count(DISTINCT manufacturer) > 1""").fetchall()
    if split_vendors:
        names = "; ".join(row[2] for row in split_vendors)
        findings.append(Finding("vendor_spelled_several_ways", "error", len(split_vendors),
                                f"the same vendor appears under more than one spelling, which splits "
                                f"it into separate vendors on promotion: {names}"))

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

    # -- the search index must describe the corpus it is consulted about --------
    #
    # A stale index that looks fresh is worse than no index: the scan it replaced
    # was slow and right. `staleness()` re-derives the whole indexed basis (66ms)
    # rather than comparing a row count, because an edit that leaves the count
    # alone -- a model code corrected, a region renamed -- is exactly the change a
    # count cannot see. An ERROR, not a warning: while this is true the type-ahead
    # can report a release that exists as absent.
    #
    # `deep` guards it for the reason at the top of this function: it is a
    # re-derivation over 21,186 rows and /api/v1/admin/health is called on every
    # page load. The request path has its own cheap tripwire (search_index.plan)
    # which falls back to the scan, so the gap between these two checks is
    # covered by being CORRECT and slow rather than by being unchecked.
    if deep and connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
            (search_index.STATE,)).fetchone():
        stale, why = search_index.staleness(connection)
        if stale:
            findings.append(Finding("search_index_does_not_match_the_corpus", "error", 1, why))

    # -- what a changeset cannot record --------------------------------------
    #
    # Reported so the limit is a measurement rather than a paragraph in
    # changesets.py that nobody re-reads. The session extension identifies rows
    # by primary key, so a table without one is not recorded AND NOT COMPLAINED
    # ABOUT -- its changes are simply absent from the diff, and an operator
    # reverting a batch would get a silently partial rollback.
    #
    # On this corpus it is now NO table. It was one --
    # `identity_resolution_rationales`, which records WHY each automated identity
    # conclusion was reached -- until migration 0033 gave it a PRIMARY KEY. This
    # check stays, and stays a warning rather than being deleted as satisfied: it
    # reads the live schema, so the next table created without a key is reported
    # here instead of joining a blind spot nobody is looking for. A warning and
    # not an error because nothing is misreported by such a table -- the rollback
    # is merely narrower than it appears, and it becomes an error's worth of
    # surprise only if somebody believes a revert was total.
    untracked = changesets.tables_invisible_to_a_changeset(connection)
    if untracked:
        findings.append(Finding(
            "table_absent_from_every_changeset", "warning", len(untracked),
            f"no PRIMARY KEY, so the session extension does not record changes to it and a "
            f"changeset revert leaves it untouched: {', '.join(untracked)}"))

    # -- the second, worse blind spot: a generated column --------------------
    #
    # A table with a GENERATED column cannot be in a changeset at all, and
    # attaching it makes `sqlite3session_changeset` return SQLITE_SCHEMA for the
    # WHOLE SESSION -- so the choice is "this table is missing" or "the whole
    # changeset is missing". `observations.effective_at` is such a column, and
    # `observations` is 57% of the corpus.
    #
    # Separate from the no-PRIMARY-KEY finding above because the reason and the
    # fix differ: that one is repaired by giving the table a key (migration 0033
    # did exactly that), this one by removing the generated column or accepting
    # the gap. Folding them together would hide which repair applies.
    #
    # A WARNING, on the same reasoning as its neighbour: nothing is misreported
    # by it, the rollback is merely narrower than it looks -- and it becomes an
    # error's worth of surprise only if somebody believes a revert was total.
    generated = changesets.tables_with_a_generated_column(connection)
    if generated:
        findings.append(Finding(
            "table_cannot_be_in_a_changeset_generated_column", "warning", len(generated),
            f"a GENERATED column makes the session extension unable to produce a "
            f"changeset for this table -- and attaching it returns SQLITE_SCHEMA for the "
            f"whole session, so it is excluded and its rows are in NO changeset. A revert "
            f"restores everything except these: {', '.join(generated)}"))

    # -- is this the corpus it claims to be? ---------------------------------
    #
    # "A rebuild is not a restore": 759 devices identical, 106 only in the live
    # corpus, 95 only in a rebuild, 201 resolving differently -- and until this
    # check existed, nothing noticed. The changesets added before it are a
    # NARROWER thing worth not confusing with it: they make a WRITE reversible,
    # they do not compare a rebuild against this corpus.
    #
    # The test is CONTAINMENT, not equality. A nightly batch adds conclusions,
    # registry rows and devices, so an equality test would fire every night and
    # be switched off within a week. What a batch never does is FORGET, because
    # a concluded identity is final by design -- so a subject the baseline
    # recorded and this corpus no longer has is the divergence, and an addition
    # is not. See corpus_identity.compare.
    #
    # `deep` guards it for the reason at the top of this function: it reads
    # ~7,000 subjects across four tables and /api/v1/admin/health is called on
    # every page load.
    if deep:
        findings.extend(_identity_findings(connection, identity_baseline))

    order = {"error": 0, "warning": 1}
    findings.sort(key=lambda f: (order.get(f.severity, 2), -f.count))
    return findings


def _identity_findings(connection, identity_baseline) -> list[Finding]:
    """Compare this corpus against the identity it was recorded under.

    Absence of a baseline is REPORTED, never treated as a pass. A check whose
    only states are PASS and FAIL reports an absent measurement as a negative
    one, and "nothing to compare against" is the state a fresh rebuild is in --
    exactly the case somebody would otherwise read as "no divergence found".
    """
    path = (identity_baseline if identity_baseline is not None
            else corpus_identity.baseline_path(connection))
    if path is None:
        return [Finding("corpus_identity_cannot_be_located", "warning", 1,
                        "this corpus has no file on disk (it is in memory), so there is "
                        "nowhere to record or read its identity baseline")]
    baseline, why = corpus_identity.read_baseline(path)
    if baseline is None:
        return [Finding(
            "corpus_identity_has_no_recorded_baseline", "warning", 1,
            f"{why}. Nothing says what this corpus is supposed to be, so a rebuild "
            f"cannot be told from a restore. The next batch records one; "
            f"`PYTHONPATH=src python3 -m mobile_observatory.corpus_identity record` does it now.")]
    report = corpus_identity.compare(baseline, corpus_identity.fingerprint(connection))
    if not report["comparable"]:
        return [Finding("corpus_identity_baseline_is_not_comparable", "warning", 1,
                        report["incomparable_reason"])]
    if report["identical"]:
        return []
    moved = report["forgotten"] + report["changed"]
    if not moved:
        # Grew only. Not a finding: this is what every nightly batch does, and
        # reporting it would be the always-fires check this was designed around.
        return []
    parts = []
    for name, part in report["components"].items():
        if part["digest_matches"]:
            continue
        parts.append(f"{name}: {part['baseline_count']} -> {part['current_count']} "
                     f"(forgotten {part['forgotten']}, changed {part['changed']}, "
                     f"added {part['added']})"
                     + (f" e.g. {', '.join(part['examples'][:3])}" if part["examples"] else ""))
    return [Finding(
        "corpus_no_longer_matches_its_recorded_identity", "error", moved,
        f"baseline recorded {report['baseline_recorded_at']} (digest "
        f"{str(report['baseline_digest'])[:12]}) says things this corpus does not: "
        f"{moved} subjects forgotten or re-decided, {report['added']} added. A "
        f"concluded identity is final by design, so this corpus is not the one that "
        f"baseline describes -- a rebuild rather than a restore. "
        + "; ".join(parts)
        # The MECHANISM, not more of the size. A count sends an operator looking
        # for damage; "a later rule version stripped the brand prefix" tells
        # them whether it is the divergence they already know about.
        + ("  MECHANISM: " + " ".join(report["mechanism"]) if report.get("mechanism") else ""))]


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

    A SECOND WAY TO BE HELD, which this reported as zero for a full month.
    Promotion needs review_state='approved' on the product AND
    link_state='approved' on the link. `observations_awaiting_review` counts only
    the first gate, so an observation on an ALREADY APPROVED product whose link is
    still 'proposed' was in neither column: not serving, and not reported as held.
    Measured: 8,332 Xiaomi observations, on 57 auto-approved products, 0 of them
    in product_firmware_releases -- the mifirm archive contributed identities to
    products that had already been concluded, and a remembered conclusion is never
    reopened, so the identities stayed 'proposed' forever. Xiaomi read 14,996
    awaiting review when 23,328 observations were actually held.

    The fix is NOT to widen `observations_awaiting_review`, because that number has
    a meaning a reader relies on (the UI's tooltip names it: observations belonging
    to products nobody has reviewed). The two states are held by different gates
    and are cleared by different work, so they are two numbers:

      observations_awaiting_review      -- the product itself is unreviewed
      observations_held_by_link_review  -- the product IS approved; the identity
                                           the observation arrived under is not
      observations_not_serving          -- the real stalled population, their sum

    Still not Findings. Both are pending work rather than faults, and the
    distinction this module is built on -- "never looked" vs "looked and found
    nothing" vs "held behind a review" -- needs three answers, not a bigger one.

    A THIRD WAY TO BE HELD, and the one that could never be cleared.
    `observations_awaiting_review` used to be `review_state <> 'approved'`, which
    put 20,955 observations in a column the UI labels "awaiting review" -- against
    626 products whose own recorded conclusion says the captured evidence cannot
    resolve them (465 with no independent identifier at all, 161 naming several
    candidates and nothing to choose between them). No reviewer could clear one of
    them. That is not pending work; it is a false promise of future work, and
    counting it as pending is the same mistake as reporting Apple's 4,450 held
    observations as a silent zero -- an honest number given a meaning it does not
    have.

    So those products now rest in `unresolvable_on_captured_evidence` (see
    adjudication.py) and this function keeps the three answers it argues for above
    apart, rather than folding a fourth into one of them:

      observations_awaiting_review            review_state='proposed' -- nobody has
                                              looked. Exactly what the UI tooltip
                                              claims it is, which it was not before:
                                              a REJECTED product's observations also
                                              counted, and a reviewer had already
                                              looked at those.
      observations_held_by_link_review        the product IS approved; the identity
                                              the observation arrived under is not
      observations_adjudicated_unresolvable   looked at, and no evidence can resolve
                                              it. NOT pending, and not hidden either
      observations_pending_review             the sum of the first two -- the work a
                                              human could actually do
      observations_not_serving                everything not serving, unchanged

    And the two sub-populations are reported separately, because "the sources are
    silent" and "the sources disagree" are different claims about the world and only
    the first means there is nothing to find:

      unresolvable_no_identifier          no independent identifier exists
      unresolvable_several_candidates     candidates exist, none discriminates
      unresolvable_without_identity       adjudicated with no registry identity at
                                          all, so the basis could not be keyed on
                                          one and was recorded product-level
                                          (2 products; see adjudication.py)
    """
    return [dict(row) for row in connection.execute(f"""
        SELECT sp.manufacturer AS vendor,
               count(DISTINCT sp.id) AS products,
               sum(sp.review_state='approved') AS approved,
               sum(sp.review_state='{UNRESOLVABLE}') AS unresolvable,
               -- Read from identity_conclusions, which is where the difference
               -- between the two populations already lives. A second copy on
               -- source_products would be a second thing to keep in step.
               (SELECT count(*) FROM source_products s7
                  JOIN identity_conclusions ic7 ON ic7.product_id = s7.id
                 WHERE s7.manufacturer = sp.manufacturer
                   AND s7.review_state = '{UNRESOLVABLE}'
                   AND ic7.conclusion = 'insufficient_evidence') AS unresolvable_no_identifier,
               (SELECT count(*) FROM source_products s8
                  JOIN identity_conclusions ic8 ON ic8.product_id = s8.id
                 WHERE s8.manufacturer = sp.manufacturer
                   AND s8.review_state = '{UNRESOLVABLE}'
                   AND ic8.conclusion = 'ambiguous') AS unresolvable_several_candidates,
               (SELECT count(*) FROM source_products s9
                 WHERE s9.manufacturer = sp.manufacturer
                   AND s9.review_state = '{UNRESOLVABLE}'
                   AND NOT EXISTS (SELECT 1 FROM source_identity_registry sir9
                                    WHERE sir9.product_id = s9.id)) AS unresolvable_without_identity,
               (SELECT count(*) FROM observation_product_links opl
                  JOIN source_products s2 ON s2.id = opl.product_id
                 WHERE s2.manufacturer = sp.manufacturer
                   AND s2.review_state = 'proposed') AS observations_awaiting_review,
               (SELECT count(*) FROM observation_product_links opl
                  JOIN source_products sA ON sA.id = opl.product_id
                 WHERE sA.manufacturer = sp.manufacturer
                   AND sA.review_state = '{UNRESOLVABLE}') AS observations_adjudicated_unresolvable,
               (SELECT count(*) FROM observation_product_links opl
                  JOIN source_products sB ON sB.id = opl.product_id
                 WHERE sB.manufacturer = sp.manufacturer
                   AND (sB.review_state = 'proposed'
                        OR (sB.review_state = 'approved'
                            AND opl.link_state <> 'approved'))) AS observations_pending_review,
               -- The product is through review; the LINK is not. Invisible in
               -- every column this function had, and invisible in the corpus: no
               -- counter, no finding, nothing in the UI.
               (SELECT count(*) FROM observation_product_links opl
                  JOIN source_products s5 ON s5.id = opl.product_id
                 WHERE s5.manufacturer = sp.manufacturer
                   AND s5.review_state = 'approved'
                   AND opl.link_state <> 'approved') AS observations_held_by_link_review,
               (SELECT count(*) FROM observation_product_links opl
                  JOIN source_products s6 ON s6.id = opl.product_id
                 WHERE s6.manufacturer = sp.manufacturer
                   AND (s6.review_state <> 'approved'
                        OR opl.link_state <> 'approved')) AS observations_not_serving,
               (SELECT count(DISTINCT phl.hardware_model_id) FROM product_hardware_links phl
                  JOIN source_products s3 ON s3.id = phl.product_id
                 WHERE s3.manufacturer = sp.manufacturer) AS canonical_devices,
               -- Reviewed, but with no identifier that can key a canonical
               -- device, so they serve from the evidence layer only. Reported
               -- because "574 products, 89 devices" otherwise reads as 485
               -- things gone missing.
               (SELECT count(*) FROM source_products s4
                 WHERE s4.manufacturer = sp.manufacturer AND s4.review_state = 'approved'
                   AND NOT EXISTS (SELECT 1 FROM product_hardware_links l WHERE l.product_id = s4.id)
                 ) AS approved_evidence_only
          FROM source_products sp
         GROUP BY sp.manufacturer
         ORDER BY observations_not_serving DESC, sp.manufacturer""")]


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
    parser.add_argument("--identity-baseline", default=None,
                        help="Compare the corpus's identity conclusions against this "
                             "baseline file instead of <data-dir>/corpus-identity.json. "
                             "Point it at the baseline from the corpus you believe you "
                             "reproduced; see corpus_identity.py.")
    args = parser.parse_args()

    db = Database.migrated(Path(args.data_dir) / "corpus.sqlite")
    try:
        findings = check_corpus(db.connection,
                                identity_baseline=args.identity_baseline)
        print(summarise(findings))
        for finding in findings:
            print(f"  [{finding.severity}] {finding.check}: {finding.count} — {finding.detail}")
        print()
        print("review queue (captured evidence not yet serving):")
        # Every gate, separately. One column could not tell a product nobody has
        # reviewed from an approved product whose identity is still proposed, and
        # the second kind held 8,332 observations while reading as zero. The third --
        # adjudicated unresolvable -- is printed in its own column and never folded
        # into a pending one: it is not work anybody can do, and it is not hidden.
        print("  %-10s %8s %8s %7s %9s %11s %9s %13s %11s" % (
            "vendor", "products", "approved", "devices", "evid-only",
            "await:prod", "held:link", "adjudicated", "not serving"))
        for row in review_queue(db.connection):
            print("  %-10s %8d %8d %7d %9d %11d %9d %13d %11d" % (
                row["vendor"], row["products"], row["approved"], row["canonical_devices"],
                row["approved_evidence_only"], row["observations_awaiting_review"],
                row["observations_held_by_link_review"],
                row["observations_adjudicated_unresolvable"],
                row["observations_not_serving"]))
        print()
        print("adjudicated unresolvable (looked at; captured evidence cannot resolve):")
        print("  %-10s %13s %13s %16s %14s" % (
            "vendor", "products", "no identifier", "several candid.", "no identity"))
        for row in review_queue(db.connection):
            if not row["unresolvable"]:
                continue
            print("  %-10s %13d %13d %16d %14d" % (
                row["vendor"], row["unresolvable"], row["unresolvable_no_identifier"],
                row["unresolvable_several_candidates"], row["unresolvable_without_identity"]))
        if args.strict and any(f.severity == "error" for f in findings):
            raise SystemExit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
