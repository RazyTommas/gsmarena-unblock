"""Approve identities that arrived AFTER their product was already concluded.

THE GAP THIS CLOSES
`automate_identity_review` iterates products and skips any that already carry an
`identity_conclusions` row (`if remembered or blocked: continue`). That is correct
for the product -- re-deciding a settled product would churn -- but it means an
identity added to that product by a LATER source is never evaluated. It keeps
`resolution_state='proposed'` forever, `observation_product_links.link_state`
mirrors that, and `promote_approved_product_observations` requires BOTH to be
approved. So the evidence is in the corpus and cannot reach the surface.

Measured on 2026-09-22: the mifirm archive contributed 312 Xiaomi identities, of
which 8 were approved and 304 were stranded. 205 of the stranded ones belong to
products whose conclusion was ALREADY auto-approved, and together they gate
27,914 firmware releases.

WHY THIS IS NOT WIDENING THE GATE
It applies exactly the test `automate_identity_review` applies, against exactly
the same artifact: Xiaomi's own captured `devices.yml`. A codename is approved
only when the catalog maps it to a name that matches the product's canonical name
after the same region-suffix strip. `aristotle -> Xiaomi 13T` is NOT in the
catalog and stays proposed; `agate -> Xiaomi 11T China` is, and is approved on the
vendor's own word. Nothing is approved because it is merely plausible, and nothing
is approved for a product that was never approved itself.

WHAT IT ACTUALLY APPROVES TODAY: NOTHING, and that is the point of wiring it in.
Measured 2026-09-30 against the captured `devices.yml`, with this function called
from run_batch for the first time: of the 58 stranded identities it targets --
together gating 8,332 observations, none of which reach
product_firmware_releases -- it approves **0**. 49 of their codenames are absent
from the catalog and 9 mismatch on name.

The reason is structural, not a bug here. `devices.yml` is keyed by REGIONAL
codename variants (`vili_global`, `vili_eea_global`, `vili_tw_global`, ...), which
is what `xiaomi.community.firmware_tracker` publishes, and every one of those is
approved. The mifirm archive publishes the BARE stem (`vili`), which is not a key
in the catalog at all, so the exact-match test cannot reach it.

Closing the remaining 8,332 therefore requires asserting that the bare stem names
the same product as its regional variants -- an identity judgement, and one with
real corroborating evidence (the products' own recorded conclusions carry Google
Play `device_codes: ["vili"]`). That judgement is NOT made by
`approve_catalog_confirmed_identities`, whose whole contract is one exact key in
one captured artifact. It is made by `approve_stem_corroborated_identities` at the
bottom of this file, which is a separate rule with a separate version, a named
source field, and its own refusals -- and which approves 11 of the 58, not 58.
The "8,332" in this docstring is the size of the STALL, and it was never the size
of the fix: 46 of the 58 stems have no captured Google Play device code for their
product at all, so nothing corroborates them and they stay proposed.

So this is wired in for the same reason a check is wired in before it fires: the
stall it closes is a mechanical one that WILL recur the next time a source
contributes an identity to a product that was concluded in an earlier run, and
there must not be a second discovery of it. A test asserting only that it
"approves things" would be vacuously green on this corpus, so
tests/test_identity_backfill.py drives it from a fixture catalog and asserts what
it REFUSES.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from .enrichment import _xiaomi_catalog, _norm, _REGION
from .identity_rationales import record_rationale


def approve_catalog_confirmed_identities(connection: sqlite3.Connection, *,
                                         devices_yml: Path) -> dict[str, int]:
    catalog = _xiaomi_catalog(devices_yml)
    totals: dict[str, int] = defaultdict(int)
    if not catalog:
        return {"catalog_empty": 1}

    rows = connection.execute("""
        SELECT sir.id, sir.source_value, sir.product_id, sir.resolution_state,
               sp.canonical_name, sp.manufacturer, sp.review_state,
               ic.conclusion
        FROM source_identity_registry sir
        JOIN source_products sp ON sp.id = sir.product_id
        LEFT JOIN identity_conclusions ic ON ic.product_id = sp.id
        WHERE sir.resolution_state != 'approved'
    """).fetchall()

    with connection:
        for r in rows:
            totals["examined"] += 1
            if r["manufacturer"] != "Xiaomi":
                totals["skipped_not_xiaomi"] += 1
                continue
            # the product itself must already be settled and approved; an identity
            # can never be more certain than the product it points at
            if r["review_state"] != "approved" or r["conclusion"] != "auto_approved":
                totals["skipped_product_not_approved"] += 1
                continue

            names = catalog.get(r["source_value"], [])
            if not names:
                totals["skipped_codename_not_in_catalog"] += 1
                continue

            normalized = _norm(_REGION.sub("", r["canonical_name"]))
            if not any(_norm(_REGION.sub("", n)) == normalized for n in names):
                totals["skipped_catalog_name_mismatch"] += 1
                continue

            connection.execute(
                "UPDATE source_identity_registry SET resolution_state='approved',"
                " resolution_method='xiaomi_vendor_codename_catalog' WHERE id=?",
                (r["id"],))
            totals["approved"] += 1

        # link_state mirrors the identity's resolution_state; refresh the links this
        # pass just settled so promotion can see them.
        #
        # Routed through the one rule rather than repeating it. This site used to
        # match on `identity_id IN (SELECT id ... WHERE resolution_state='approved')`
        # -- the same product-blind lookup that gave the proposed product "Redmi 1"
        # nine approved links -- so the module written to close a stall could also
        # have widened one. See identity_bridge.LINK_LICENCE_CONDITION.
        from .identity_bridge import refresh_observation_link_states
        totals.update(refresh_observation_link_states(connection))
    return dict(totals)


# --------------------------------------------------------------------------
# THE STEM RULE
# --------------------------------------------------------------------------
# The stall the function above measures and then refuses to close: `devices.yml`
# is keyed by REGIONAL codename variants (`vili_global`, `vili_eea_global`, ...),
# which is what xiaomi.community.firmware_tracker publishes and which are all
# already approved. The mifirm archive publishes the BARE STEM (`vili`), which is
# never a key in the catalog, so an exact-key test can never reach it.
#
# This rule closes it for the stems -- and only the stems -- that a second,
# independent captured source corroborates.
#
# THE FIELD IT IS CHECKABLE AGAINST, named because a rule whose basis cannot be
# named is not a rule:
#
#     identity_conclusions.evidence_json
#       -> the entries whose "source" is "google_play_supported_devices"
#       -> their "device_codes" list
#
# That list is Google Play's own supported-device catalog as captured for THIS
# product, written by `enrichment._conclude` when it concluded the product from
# `crawler/relay/results/google-play-devices/supported_devices.csv`. It is not
# model world-knowledge, not a similarity score, and not the Xiaomi catalog
# restated: Google publishes the device code `vili` under the marketing name this
# product carries, and Xiaomi publishes `vili_global` naming the same marketing
# name. Two publishers, one relationship. If the stem is not in a captured
# `device_codes` list, this rule REFUSES -- there is nothing else it would be
# standing on.
#
# WHAT APPROVAL MEANS, exactly as for the rule above: this captured source
# identity belongs to this source product. No hardware model is created, no model
# code is fabricated, and the catalog's regional variants are not collapsed.
#
# MEASURED, on a copy of the live corpus taken 2026-09-30: of the 58 stranded
# identities -- together gating 8,332 observations -- it approves 11, releasing
# 2,080 observations, and refuses 47. The distance between 8,332 and 2,080 is the
# whole reason the corroboration requirement is written this way round: 46 of
# those stems have NO Google Play device code recorded for their product at all,
# so there is no second source and the rule has nothing to stand on. The 47th is
# `lisa`, which Google Play lists for TWO approved products ("Xiaomi 11 Lite 5G
# NE" and "Mi 11 LE") and whose bare catalog key `lisa` names the second one --
# exactly the ambiguity that has to refuse rather than pick, and it is refused
# twice over, once by each half of the rule.
STEM_RULE = "xiaomi_codename_stem_google_play_corroborated"
# Versioned independently of enrichment.RULE_VERSION on purpose. That constant
# gates whether a PRODUCT CONCLUSION is reopened (enrichment._conclude: a
# conclusion reopens only when it resolved nothing AND its rule_version differs),
# and bumping it to announce this rule would reopen every conclusion that resolved
# nothing under version 2 -- measured 2026-09-30: 465 of them, of which 0 carry a
# Google Play device code, so not one is a stem this rule could resolve. This rule
# decides registry rows on products whose conclusions are already auto_approved
# and are therefore never reopened, so it needs no bump and must not cause one.
# The string is deliberately not a number so it cannot be mistaken for a point on
# that counter.
STEM_RULE_VERSION = "stem-1"

PLAY_EVIDENCE_SOURCE = "google_play_supported_devices"


def _play_device_codes(evidence_json: str) -> set[str]:
    """The Google Play device codes a conclusion actually RECORDED, case-folded.

    Deliberately tolerant of shape, because evidence_json is written by another
    module and read here: a malformed or unexpected entry yields no codes, which
    makes the rule REFUSE. It must never be able to make the rule approve.
    """
    codes: set[str] = set()
    try:
        entries = json.loads(evidence_json or "[]")
    except (TypeError, ValueError):
        return codes
    if not isinstance(entries, list):
        return codes
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("source") != PLAY_EVIDENCE_SOURCE:
            continue
        for code in entry.get("device_codes") or ():
            if isinstance(code, str) and code.strip():
                codes.add(code.strip().casefold())
    return codes


def _stem_variants(catalog: dict[str, list[str]], stem: str) -> dict[str, list[str]]:
    """Catalog keys that are this stem, or this stem plus a suffix.

    `vili` -> `vili_global`, `vili_eea_global`, ... and `vili` itself when the
    catalog carries it. The underscore is required, so `lisa` does not collect
    `lisandra` and a short stem cannot swallow an unrelated device.
    """
    return {key: names for key, names in catalog.items()
            if key == stem or key.startswith(stem + "_")}


def approve_stem_corroborated_identities(connection: sqlite3.Connection, *,
                                         devices_yml: Path) -> dict[str, int]:
    """Approve a bare codename stem only when a second captured source names it.

    Every refusal is recorded in `identity_resolution_rationales` beside every
    approval. The refusals are the more useful half: they are the residue a human
    has to look at, and a decision whose basis is not recorded cannot be
    re-checked later.
    """
    catalog = _xiaomi_catalog(devices_yml)
    totals: dict[str, int] = defaultdict(int)
    if not catalog:
        # An unreadable or absent artifact is not "no objection".
        return {"catalog_empty": 1}

    # Which products Google Play names each device code for, across the WHOLE
    # corpus -- not only the products this rule examines. A stem claimed by two
    # products is ambiguous even when the second claimant is a product this rule
    # would never look at, and scoping the lookup to the candidates is what would
    # have hidden exactly the `lisa` case.
    products_by_code: dict[str, set[str]] = defaultdict(set)
    codes_by_product: dict[str, set[str]] = {}
    for row in connection.execute(
            "SELECT product_id, evidence_json FROM identity_conclusions"):
        codes = _play_device_codes(row["evidence_json"])
        codes_by_product[row["product_id"]] = codes
        for code in codes:
            products_by_code[code].add(row["product_id"])

    # And which products the REGISTRY already files this source value under. One
    # value resolving to two products is the state
    # identity_bridge.LINK_LICENCE_CONDITION exists for; approving such a value
    # hands one product's approval to another product's links.
    products_by_value: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
            "SELECT normalized_value, product_id FROM source_identity_registry"):
        if row["product_id"]:
            products_by_value[(row["normalized_value"] or "").casefold()].add(row["product_id"])

    rows = connection.execute("""
        SELECT sir.id, sir.source_value, sir.normalized_value, sir.product_id,
               sir.last_seen_at, sp.canonical_name, sp.manufacturer, sp.review_state,
               ic.conclusion, ic.evidence_json
        FROM source_identity_registry sir
        JOIN source_products sp ON sp.id = sir.product_id
        LEFT JOIN identity_conclusions ic ON ic.product_id = sp.id
        WHERE sir.resolution_state != 'approved'
        ORDER BY sir.source_value
    """).fetchall()

    with connection:
        for r in rows:
            totals["examined"] += 1
            if r["manufacturer"] != "Xiaomi":
                # devices.yml is Xiaomi's publication and says nothing about anyone
                # else. Not this rule's subject, so no rationale row is written: a
                # judgement was not made, and recording one would put 554 rows of
                # "not applicable" in front of the 47 that are the actual residue.
                totals["skipped_not_xiaomi"] += 1
                continue
            if r["review_state"] != "approved" or r["conclusion"] != "auto_approved":
                # An identity can never be more certain than the product it points
                # at -- and the corroboration this rule reads IS that product's own
                # recorded conclusion, so there is nothing to read here either.
                totals["skipped_product_not_approved"] += 1
                continue

            stem = (r["source_value"] or "").strip().casefold()
            codes = codes_by_product.get(r["product_id"], set())
            variants = _stem_variants(catalog, stem)
            regional = {key: names for key, names in variants.items() if key != stem}
            target = _norm(_REGION.sub("", r["canonical_name"]))
            disagreeing = {key: names for key, names in variants.items()
                           if not any(_norm(_REGION.sub("", n)) == target for n in names)}
            registry_claimants = products_by_value.get(
                (r["normalized_value"] or "").casefold(), set())
            evidence = [
                {"source": PLAY_EVIDENCE_SOURCE, "matched_on": "device_code_stem",
                 "read_from": "identity_conclusions.evidence_json[*].device_codes",
                 "stem": stem, "device_codes": sorted(codes),
                 "products_naming_this_device_code": len(products_by_code.get(stem, ()))},
                {"source": "xiaomi_devices_yml", "stem": stem,
                 "catalog_variants": {k: v for k, v in sorted(variants.items())},
                 "product_canonical_name": r["canonical_name"]},
            ]

            def record(outcome: str, reason: str, rationale: str, row=r) -> None:
                # identity_rationales.record_rationale is the ONLY writer of this
                # table -- see its module docstring. The INSERT used to be spelled
                # here and byte-identically in adjudication._record, and migration
                # 0033's `subject` key would have been a tenth column for both to
                # derive separately.
                record_rationale(
                    connection, identity_id=row["id"], rule=STEM_RULE,
                    rule_version=STEM_RULE_VERSION, product_id=row["product_id"],
                    outcome=outcome, reason=reason, rationale=rationale,
                    evidence_json=json.dumps(evidence, sort_keys=True),
                    decided_at=row["last_seen_at"])
                totals["approved" if outcome == "approved" else "refused_" + reason] += 1

            if not stem:
                record("refused", "empty_source_value",
                       "The registry row carries no source value to corroborate.")
                continue
            # THE LICENCE. Nothing below this line can approve a stem that no
            # captured device_codes list names.
            if stem not in codes:
                record("refused", "stem_not_in_captured_device_codes",
                       f"The captured Google Play supported-device evidence recorded for "
                       f"'{r['canonical_name']}' does not list the device code '{stem}' "
                       f"(it lists {sorted(codes) or 'nothing'}), so no second publisher "
                       f"corroborates the bare codename stem and this rule has no basis to "
                       f"approve it.")
                continue
            if not regional:
                record("refused", "stem_has_no_regional_catalog_variants",
                       f"Xiaomi's captured devices.yml carries no regional variant of the "
                       f"stem '{stem}', so there is no vendor statement that a stem and a "
                       f"regional codename describe one product, which is the relationship "
                       f"this rule reads.")
                continue
            if len(products_by_code.get(stem, ())) > 1:
                record("refused", "stem_names_several_products",
                       f"The captured Google Play catalog lists the device code '{stem}' for "
                       f"{len(products_by_code[stem])} different source products, so which "
                       f"one the stem names is exactly what is unknown. Two candidates is "
                       f"not a match.")
                continue
            if len(registry_claimants) > 1:
                record("refused", "stem_registered_on_several_products",
                       f"The captured source value '{stem}' is filed in the identity registry "
                       f"under {len(registry_claimants)} products; approving it would hand "
                       f"one product's approval to another product's links.")
                continue
            if disagreeing:
                record("refused", "catalog_variants_disagree",
                       f"Xiaomi's captured catalog does not agree with itself about the stem "
                       f"'{stem}': "
                       f"{json.dumps({k: v for k, v in sorted(disagreeing.items())})} name "
                       f"something other than '{r['canonical_name']}' once the region suffix "
                       f"is stripped. A stem whose own variants disagree beyond the region "
                       f"suffix is not one product.")
                continue

            connection.execute(
                "UPDATE source_identity_registry SET resolution_state='approved',"
                " resolution_method=?, rule_version=?, confidence='high' WHERE id=?",
                (STEM_RULE, STEM_RULE_VERSION, r["id"]))
            record("approved", "stem_corroborated_by_captured_device_code",
                   f"The bare codename stem '{stem}' is named as a device code by the "
                   f"captured Google Play supported-device catalog for this product, and all "
                   f"{len(variants)} of Xiaomi's captured catalog variants of that stem "
                   f"({', '.join(sorted(variants))}) name '{r['canonical_name']}' once the "
                   f"region suffix is stripped. Two independent publishers therefore tie the "
                   f"stem to this product. Approval means this captured source identity "
                   f"belongs to this source product; no hardware model is created and no "
                   f"model code is fabricated.")

        # link_state mirrors resolution_state FOR THIS PRODUCT; routed through the
        # one rule rather than repeated here. See identity_bridge.
        from .identity_bridge import refresh_observation_link_states
        totals.update(refresh_observation_link_states(connection))
    return dict(totals)
