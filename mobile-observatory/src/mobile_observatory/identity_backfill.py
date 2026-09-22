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
        connection.execute("""
            UPDATE observation_product_links SET link_state='approved'
            WHERE link_state != 'approved' AND identity_id IN (
                SELECT id FROM source_identity_registry WHERE resolution_state='approved')""")
    return dict(totals)
