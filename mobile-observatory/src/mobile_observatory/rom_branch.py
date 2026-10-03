"""The product a Xiaomi source publishes is a ROM BRANCH, and this rule says when.

THE STALL
`mifirm.community.firmware_archive` publishes firmware under a BARE codename stem
(`marble`, `apollo`, `veux`) and names the thing it belongs to with a slash-joined
list of marketed phones: "Redmi Note 12 Turbo / POCO F5". `identity_bridge` turns
that name into one `source_products` row -- correctly, because one ROM is what the
source actually publishes -- and then no rule could ever approve it:

  * `enrichment._conclude`'s Xiaomi test is exact equality against ONE
    `devices.yml` key. The bare stem's catalog entry names the CHINA phone only
    (`marble` -> "Redmi Note 12 Turbo China"), so the slash-joined product name
    never equals it. Measured: 54 of these concluded `ambiguous/ranked_candidates`
    and 9 `insufficient_evidence/no_independent_identifier`.
  * `identity_backfill.approve_stem_corroborated_identities` needs a Google Play
    `device_codes` entry naming the stem. Measured: 0 of the 63 slash products
    carry one, so the stem rule has nothing to stand on for any of them.

So 63 Xiaomi products holding 10,628 captured observations sat in
`unresolvable_on_captured_evidence` because the model had no way to say "this is a
ROM branch, not a phone".

THE HYPOTHESIS, AND WHERE THE CORPUS CONTRADICTS IT
The steer was: a product is the ROM branch, and marketed devices are a separate
layer pointing at it. Measured on the live corpus, that is how these rows are
already stored -- one product per branch, from one publisher, never collapsed and
never multiplied. But the corpus contradicts it in one specific way that is
reported rather than papered over: 97 Xiaomi codename stems are filed under TWO
products, because `xiaomi.community.firmware_tracker` names the same branch by its
China marketing name ("Redmi Note 12 Turbo") while mifirm names it by the whole
slash list. Those two products are NOT merged here -- whether they are one branch
is an identity judgement, and `integrity.check_corpus` reports the count so the
judgement is visible instead of being taken by this rule.

And the slash itself is OVERLOADED, which is why the test below is set equality and
not a split-and-accept. Measured on the live corpus it means at least two different
things:
  * a branch spanning several marketed phones -- "Redmi K40/POCO F3/Mi 11X";
  * two names for ONE phone -- "Mi 8 Lite/Youth", "Redmi 3S/X",
    "Redmi 9C/PocoPhone C3", "Redmi K30 4G/Pocophone X2", "Redmi 4 Prime/Pro".
    Five of these, and in every one the vendor catalog names exactly the FIRST
    half. Treating the slash as "several phones" would have invented five phones.
This rule cannot tell those apart and does not try: it refuses the second shape as
`archive_names_a_phone_the_vendor_catalog_does_not`.

THE FIELD IT IS CHECKABLE AGAINST, named because a rule whose basis cannot be
named is not a rule:

    crawler/relay/results/xiaomi-tracker/devices.yml
      -> every key that is the stem or `stem_<suffix>`
      -> that key's FIRST list entry, which is the marketing name

versus

    source_products.canonical_name
      -> written by identity_bridge from the mifirm archive's own
         `$.data.source_device_name`

Both sides are split on "/", region-suffix stripped and normalised the SAME way,
and the two SETS must be equal. Two publishers -- the XiaomiFirmwareUpdater
catalog and the mifirm.net archive, captured separately, parsed separately -- have
to name the same phones, member for member. A branch the vendor says covers four
phones and the archive says covers two is refused, in both directions.

devices.yml's shape is checked, not assumed: all 1,422 keys carry exactly two
entries, name then internal code ("marble_global" -> ['POCO F5 Global',
'MARBLEGlobal']). A key with any other shape refuses, because reading entry 0 of a
list whose meaning changed is how a rule starts comparing a build code to a phone.

WHY devices.yml CANNOT CORROBORATE A TRACKER IDENTITY
`devices.yml` is captured from the same export directory as
`xiaomi.community.firmware_tracker`, so the two are one publisher. Approving a
tracker codename against it would be a source agreeing with itself -- the exact
reason the 66 Apple products are unresolvable, where every piece of evidence is
ipsw.me. This rule therefore refuses a tracker identity outright
(`corroborating_source_is_the_same_publisher`), which costs one registry row on
today's corpus and keeps the rule's claim true.

WHAT APPROVAL MEANS, AND THE ONE THING IT MUST NOT DO
It means: this captured source identity belongs to this source product, and the
product is the ROM branch the two publishers describe. It does NOT mean the
product is one phone, and nothing downstream turns it into one. Measured on a copy
of the live corpus: 20 products approved, 8,168 releases promoted, and **0** new
`hardware_models`, `device_variants` or `product_hardware_links`.

That guarantee has TWO layers, and saying it had one was wrong -- a planted defect
found it. The first draft of this docstring claimed the whole guarantee was that
this rule does not write `identity_conclusions`. Planting a write of
`conclusion='auto_approved'` changed NOTHING: `device_promotion._observed_model_code`
also requires either a Google Play model code in the conclusion's evidence or an
approved identity in the `model_code` namespace, and a Xiaomi codename is neither
-- it is in the `codename` namespace, and that function refuses a codename
explicitly. So:

  1. `_observed_model_code` refuses a Xiaomi codename, which is the load-bearing
     layer and belongs to code this rule does not own;
  2. this rule additionally does not write an `auto_approved` conclusion, which
     keeps `_corroborated_name` and `_from_play` from ever being asked.

Layer 1 alone is enough today. It is somebody else's invariant, which is exactly
why layer 2 is kept: a branch must not become a phone on the strength of one
function in another module continuing to behave. The plant that DOES create a
device -- a conclusion carrying a Google Play `model_codes` entry -- is in the
harness, so the guard is known not to be vacuous.

The `ambiguous` conclusion stays on the row, because which PHONE this is remains
ambiguous and that is a true statement about the corpus.

VERSIONED INDEPENDENTLY OF enrichment.RULE_VERSION, and measured before relying on
either. That constant gates whether a product CONCLUSION reopens, and
`_conclude` reopens only a conclusion whose `conclusion == 'insufficient_evidence'`
-- so a bump from "2" to "3" cannot re-decide the 54 `ambiguous` products that are
the bulk of this item, while rewriting the 465 `insufficient_evidence`
conclusions that no new rule here can resolve. A bump is inert for this work and
expensive, so the rule stamps its own decisions `branch-1`, exactly as the stem
rule stamps `stem-1`.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from .enrichment import _xiaomi_catalog, _norm, _REGION
from .identity_rationales import record_rationale

BRANCH_RULE = "xiaomi_rom_branch_name_set_agreement"
#: Deliberately a string, so it cannot be mistaken for a point on
#: `enrichment.RULE_VERSION`. See the module docstring for the measurement.
BRANCH_RULE_VERSION = "branch-1"

#: The publisher `devices.yml` is captured from. An identity from this source
#: cannot be corroborated by it -- that is one source agreeing with itself.
CATALOG_PUBLISHER = "xiaomi.community.firmware_tracker"

#: Index 0 of a `devices.yml` value is the marketing name, index 1 the vendor's
#: internal build code. Asserted per key rather than assumed; see the docstring.
CATALOG_ENTRY_LENGTH = 2


def branch_name(value: str) -> str:
    """One marketed name, folded for comparison, with `+` kept as a token.

    `enrichment._norm` strips every non-alphanumeric character, which turns
    "Pro+" into "pro" -- the same string "Pro" folds to. Both sides of this rule
    would fold identically, so set equality would still hold, but the rule would
    then be unable to tell "Redmi Note 12 Pro" from "Redmi Note 12 Pro+", and
    those are two phones. `+` becomes a word so the distinction survives.
    Measured: keeping it moves one product out of APPROVE and into a refusal
    (`pissarro`, "Redmi Note 11 Pro/Pro+ /Xiaomi 11i 5G"), which is the correct
    direction for a rule that must not merge two phones.
    """
    return _norm(_REGION.sub("", value.strip()).strip().replace("+", " plus "))


def name_set(published: str) -> frozenset[str]:
    """The set of marketed names one published device name states.

    Split on "/" because that is what both publishers use to join them. A set and
    not a list: the two publishers order them differently and order is not a fact
    about the phones.
    """
    return frozenset(filter(None, (branch_name(part) for part in str(published).split("/"))))


def catalog_branch(catalog: dict[str, list[str]], stem: str) -> tuple[list[str], list[str], frozenset[str]]:
    """The catalog keys for this stem, any whose shape is wrong, and the name set.

    A key is the stem or the stem plus an underscore and a suffix. The underscore
    is required so `lisa` cannot collect `lisandra`.

    The prefix test over-collects in one measured way: `emerald_r_global` is a
    different branch ("Redmi Note 14S") from `emerald_global` ("Redmi Note 13 Pro
    4G / POCO M6 Pro"), and `curtana_in_rf_global` ("Redmi Note 10 Lite") from
    `curtana_in_global` ("Redmi Note 9 Pro"). Over-collection can only ADD names
    to the catalog side, so it can only make the sets unequal -- it biases this
    rule towards refusing, never towards approving, and the two cases above are
    refused as `vendor_catalog_names_a_phone_the_archive_does_not`. Narrowing it
    would need a region vocabulary nothing in the capture states, so the honest
    thing is the safe direction plus this note.
    """
    keys = sorted(key for key in catalog if key == stem or key.startswith(stem + "_"))
    wrong_shape = [key for key in keys if len(catalog[key]) != CATALOG_ENTRY_LENGTH]
    names: set[str] = set()
    for key in keys:
        if len(catalog[key]) == CATALOG_ENTRY_LENGTH:
            names |= name_set(catalog[key][0])
    return keys, wrong_shape, frozenset(names)


def _human_has_spoken(product_id: str, identities: list[str], decisions: list[dict] | None) -> bool:
    """Whether a person has already decided this product. Mirrors adjudication."""
    if "manual_product_review" in identities:
        return True
    return any(decision.get("decision") in ("different", "defer")
               and decision.get("canonical_id") == product_id
               for decision in (decisions or []))


def approve_branch_corroborated_identities(connection: sqlite3.Connection, *,
                                           devices_yml: Path,
                                           decisions: list[dict] | None = None) -> dict[str, int]:
    """Approve a bare Xiaomi codename when two publishers name the same phone set.

    Every judgement -- approval and refusal alike -- is recorded in
    `identity_resolution_rationales` through its only writer. The refusals are the
    more useful half: they are the residue a human has to look at, and a decision
    whose basis is not recorded cannot be re-checked when the next capture lands.
    """
    totals: dict[str, int] = defaultdict(int)
    # An ABSENT artifact is not "no objection", and it must not be a traceback
    # either: this runs inside a batch phase, and a missing input should refuse
    # the rule rather than kill the run. `_xiaomi_catalog` raises
    # FileNotFoundError, so the file is checked before it is read.
    if not Path(devices_yml).is_file():
        return {"catalog_absent": 1}
    catalog = _xiaomi_catalog(devices_yml)
    if not catalog:
        return {"catalog_empty": 1}

    # Which products each identity ROW is linked from. This -- and not "which
    # products share a normalized_value" -- is the hazard
    # identity_bridge.LINK_LICENCE_CONDITION describes: `source_identity_registry.id`
    # is uuid5(source_id, namespace, normalized_value) with no product component,
    # so ONE row can be reached from observations that resolved to different
    # products. Two DIFFERENT publishers each filing the same codename under their
    # own product are two different ids and no collision at all; refusing those
    # would refuse 97 of the 98 shared Xiaomi codenames on today's corpus for a
    # hazard they do not have.
    linked_products: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
            "SELECT identity_id, product_id FROM observation_product_links GROUP BY 1, 2"):
        linked_products[row["identity_id"]].add(row["product_id"])

    rows = connection.execute("""
        SELECT sir.id, sir.source_id, sir.source_value, sir.normalized_value, sir.product_id,
               sir.last_seen_at, sp.canonical_name, sp.manufacturer, sp.review_state
          FROM source_identity_registry sir
          JOIN source_products sp ON sp.id = sir.product_id
         WHERE sir.resolution_state <> 'approved' AND sir.namespace = 'codename'
         ORDER BY sir.source_value
    """).fetchall()

    # Two products cannot both BE the branch. The catalog's name set is a function
    # of the stem, so two products can only both match it by carrying the same set
    # -- and then which of them the identity belongs to is exactly what is unknown.
    # Computed over the whole candidate set before anything is written, so the
    # answer does not depend on the order rows are visited in.
    #
    # MEASURED: this guard CANNOT fire on today's corpus, and is wired in anyway.
    # `source_identity_registry` is UNIQUE(source_id, namespace, normalized_value),
    # so one publisher files a codename exactly once; and today the tracker is the
    # only other Xiaomi codename publisher and is refused above as the catalog's own
    # source. The guard becomes reachable the moment a SECOND independent archive is
    # captured, which is the next thing anyone would do here -- and an unreachable
    # guard discovered later is how one gets written in a hurry. A test asserting it
    # against this corpus would be vacuously green, so
    # tests/test_rom_branch_rule.py drives it from a fixture with two archives.
    provisional: dict[str, tuple[str, list[str], list[str], frozenset[str], frozenset[str]]] = {}
    matched_products: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        stem = (r["source_value"] or "").strip().casefold()
        keys, wrong_shape, catalog_names = catalog_branch(catalog, stem)
        product_names = name_set(r["canonical_name"])
        provisional[r["id"]] = (stem, keys, wrong_shape, catalog_names, product_names)
        if keys and not wrong_shape and product_names and catalog_names == product_names:
            matched_products[stem].add(r["product_id"])

    with connection:
        for r in rows:
            totals["examined"] += 1
            if r["manufacturer"] != "Xiaomi":
                # devices.yml is a Xiaomi catalog and says nothing about anyone
                # else. Not this rule's subject, so no rationale row: a judgement
                # was not made, and recording one would bury the real residue.
                totals["skipped_not_xiaomi"] += 1
                continue
            if r["review_state"] == "rejected":
                totals["skipped_product_rejected"] += 1
                continue
            methods = [row["resolution_method"] for row in connection.execute(
                "SELECT resolution_method FROM source_identity_registry WHERE product_id=?",
                (r["product_id"],))]
            if _human_has_spoken(r["product_id"], methods, decisions):
                totals["skipped_human_decision"] += 1
                continue

            stem, keys, wrong_shape, catalog_names, product_names = provisional[r["id"]]
            other_products = linked_products.get(r["id"], set()) - {r["product_id"]}
            evidence = [
                {"source": "xiaomi_devices_yml", "matched_on": "branch_marketed_name_set",
                 "read_from": "devices.yml[<stem or stem_*>][0]",
                 "stem": stem, "catalog_variants": {k: catalog[k] for k in keys},
                 "catalog_branch_members": sorted(catalog_names)},
                {"source": r["source_id"], "matched_on": "published_device_name",
                 "read_from": "source_products.canonical_name",
                 "published_name": r["canonical_name"],
                 "archive_branch_members": sorted(product_names)},
            ]

            def record(outcome: str, reason: str, rationale: str, row=r) -> None:
                record_rationale(
                    connection, identity_id=row["id"], rule=BRANCH_RULE,
                    rule_version=BRANCH_RULE_VERSION, product_id=row["product_id"],
                    outcome=outcome, reason=reason, rationale=rationale,
                    evidence_json=json.dumps(evidence, sort_keys=True),
                    decided_at=row["last_seen_at"])
                totals["approved" if outcome == "approved" else "refused_" + reason] += 1

            if not stem:
                record("refused", "empty_source_value",
                       "The registry row carries no source value to corroborate.")
                continue
            if r["source_id"] == CATALOG_PUBLISHER:
                record("refused", "corroborating_source_is_the_same_publisher",
                       f"devices.yml is captured from the same export as '{CATALOG_PUBLISHER}', "
                       f"so approving this identity against it would be one publisher agreeing "
                       f"with itself. A second, independently captured publisher has to name the "
                       f"branch.")
                continue
            if not keys:
                record("refused", "stem_absent_from_vendor_catalog",
                       f"Xiaomi's captured devices.yml carries no key '{stem}' and no "
                       f"'{stem}_<region>' variant, so the catalog states nothing about this "
                       f"codename and there is no second publisher to agree with.")
                continue
            if wrong_shape:
                record("refused", "catalog_entry_shape_unexpected",
                       f"devices.yml entries {sorted(wrong_shape)} do not carry exactly "
                       f"{CATALOG_ENTRY_LENGTH} values (marketing name, then internal code), so "
                       f"which value is the marketing name is not known and reading one would be "
                       f"a guess about the capture's shape.")
                continue
            if other_products:
                record("refused", "identity_linked_from_another_product",
                       f"This identity row is also named by observations that resolved to "
                       f"{len(other_products)} other product(s), so approving it would hand this "
                       f"product's approval to another product's links. See "
                       f"identity_bridge.LINK_LICENCE_CONDITION.")
                continue
            if not product_names:
                record("refused", "published_name_is_not_a_name_set",
                       f"The published device name {r['canonical_name']!r} yields no marketed "
                       f"name once it is split and normalised, so there is nothing to compare.")
                continue
            if not catalog_names:
                record("refused", "catalog_states_no_marketed_name",
                       f"The {len(keys)} captured catalog entries for '{stem}' state no marketed "
                       f"name, so the catalog confirms the codename exists and says nothing about "
                       f"which phones it covers.")
                continue
            if catalog_names != product_names:
                missing = sorted(catalog_names - product_names)
                extra = sorted(product_names - catalog_names)
                if missing and not extra:
                    reason = "vendor_catalog_names_a_phone_the_archive_does_not"
                    detail = (f"the catalog additionally names {missing}, so the archive's name "
                              f"covers only part of the branch the vendor publishes")
                elif extra and not missing:
                    reason = "archive_names_a_phone_the_vendor_catalog_does_not"
                    detail = (f"the archive additionally names {extra}, which no captured catalog "
                              f"entry for this stem mentions -- the slash may be two names for one "
                              f"phone rather than two phones, and nothing captured says which")
                else:
                    reason = "branch_name_sets_disagree"
                    detail = (f"the catalog names {missing} that the archive does not, and the "
                              f"archive names {extra} that the catalog does not")
                record("refused", reason,
                       f"The two publishers do not describe the same branch for '{stem}': "
                       f"{detail}. Catalog says {sorted(catalog_names)} across "
                       f"{len(keys)} captured entries; '{r['source_id']}' published "
                       f"{r['canonical_name']!r}. A set that does not match member for member is "
                       f"not corroboration.")
                continue
            if len(matched_products.get(stem, ())) > 1:
                record("refused", "several_products_share_this_branch",
                       f"{len(matched_products[stem])} source products carry the same marketed-name "
                       f"set for the codename '{stem}', so which of them this identity belongs to "
                       f"is exactly what is unknown. Two candidates is not a match.")
                continue

            connection.execute(
                "UPDATE source_identity_registry SET resolution_state='approved',"
                " resolution_method=?, rule_version=?, confidence='high' WHERE id=?",
                (BRANCH_RULE, BRANCH_RULE_VERSION, r["id"]))
            if r["review_state"] != "approved":
                # The product has to be approved too, or
                # `promote_approved_product_observations` will not look at it. The
                # CONCLUSION is deliberately left alone -- see the module docstring:
                # it is what keeps device_promotion from minting a phone out of a
                # branch, and "which phone is this" is still genuinely ambiguous.
                connection.execute(
                    "UPDATE source_products SET review_state='approved', updated_at=? WHERE id=?",
                    (r["last_seen_at"], r["product_id"]))
                totals["products_approved"] += 1
            record("approved", "branch_corroborated_by_two_publishers",
                   f"Xiaomi's captured devices.yml states that the codename family '{stem}' "
                   f"({', '.join(keys)}) covers exactly the marketed phones "
                   f"{sorted(catalog_names)}, and '{r['source_id']}' independently publishes its "
                   f"firmware for that codename under the name {r['canonical_name']!r}, which "
                   f"names the same set member for member. Two independently captured publishers "
                   f"therefore tie this codename to this ROM branch. Approval means this captured "
                   f"source identity belongs to this source product; the product is the ROM "
                   f"branch, no hardware model is created, no model code is fabricated, and the "
                   f"{len(catalog_names)} marketed phones are recorded as the branch's members "
                   f"rather than collapsed into one device.")

        # link_state mirrors resolution_state FOR THIS PRODUCT; routed through the
        # one rule rather than repeated here. See identity_bridge.
        from .identity_bridge import refresh_observation_link_states
        totals.update(refresh_observation_link_states(connection))
    return dict(totals)


def branch_members(connection: sqlite3.Connection) -> dict[str, list[str]]:
    """The marketed phones each approved branch covers, read back from the record.

    The "marketed devices are a separate layer" half of the steer, without a
    schema change: the membership is in the rationale this rule already writes,
    and this is the reader that proves it is queryable rather than merely stored.
    """
    members: dict[str, list[str]] = {}
    for row in connection.execute(
            """SELECT product_id,
                      json_extract(evidence_json, '$[0].catalog_branch_members') members
                 FROM identity_resolution_rationales
                WHERE rule=? AND outcome='approved' ORDER BY product_id""", (BRANCH_RULE,)):
        members[row["product_id"]] = json.loads(row["members"] or "[]")
    return members
