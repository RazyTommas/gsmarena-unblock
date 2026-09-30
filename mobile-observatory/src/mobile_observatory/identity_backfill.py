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
Play `device_codes: ["vili"]`). It is deliberately NOT made here. This module's
contract is that an identity is approved only on a captured artifact's exact word;
a stem rule is a new rule and belongs to whoever owns identity decisions.

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
