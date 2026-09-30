from __future__ import annotations

import csv
import json
import re
import sqlite3
import uuid
from pathlib import Path

_NS = uuid.UUID("89ee14cf-8108-43ad-9730-55f847f53837")
_REGION_SUFFIX = re.compile(r"\s+(EEA|Global|China|India|Indonesia|Japan|Russia|Taiwan|Turkey)$", re.I)


def _id(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "\x1f".join(parts)))


def _norm(value: str, maker: str = "xiaomi") -> str:
    """Normalise a product name for use as an identity key.

    The manufacturer's own name is stripped when it LEADS the product name, because
    source_products already carries the manufacturer in its own column and the UNIQUE
    key is (manufacturer, normalized_name). Repeating the brand inside the name makes
    the same device storable twice.

    This used to hardcode "xiaomi ", so exactly one vendor was handled: 'Xiaomi 12'
    normalised to '12' while 'TECNO POVA Neo' stayed 'tecno pova neo' and the same
    device arriving as 'POVA Neo' produced 'pova neo'. Two keys, so the
    ON CONFLICT(manufacturer, normalized_name) upsert never fired and both rows were
    inserted -- seven TECNO devices ended up stored twice, all review_state=approved.

    Only a LEADING, whole-token occurrence is removed, and never if that would empty
    the name. Sub-brands are unaffected: with maker='Xiaomi', 'Redmi 12' keeps its
    'redmi' because Redmi is not the manufacturer token -- which is what keeps
    'Redmi 12' and 'Xiaomi 12' correctly distinct.

    The default keeps every pre-existing call site byte-identical; only the product
    identity path passes a real maker.
    """
    s = " ".join(value.casefold().split())
    m = " ".join((maker or "").casefold().split())
    if m:
        stripped = re.sub(rf"^{re.escape(m)}\s+", "", s, count=1)
        if stripped:
            s = stripped
    return s


def _product_name(source: str, payload: dict) -> tuple[str, str, str, str] | None:
    data = payload["data"]
    if source == "xiaomi.community.firmware_tracker":
        name = _REGION_SUFFIX.sub("", data["source_device_name"]).strip()
        return "Xiaomi", name, "codename", data["model_code"]
    if source == "mifirm.community.firmware_archive":
        # Deliberately IDENTICAL to the tracker branch above. Both sources describe
        # the same Xiaomi devices, so they must normalise to the same key: product
        # id is uuid5(manufacturer, normalized_name), and any divergence here would
        # file the archive's history under a second product row for a device the
        # tracker already owns -- the exact duplicate-device failure migration 0016
        # had to repair. Same manufacturer, same region-suffix strip, same namespace.
        name = _REGION_SUFFIX.sub("", data["source_device_name"]).strip()
        if not name:
            return None
        return "Xiaomi", name, "codename", data["model_code"]
    # --- Transsion: FRBox, naijarom and the OTA probe all describe the SAME
    # devices, so they must converge on one product each or we manufacture the
    # duplicate-device problem this schema exists to prevent. The MODEL CODE is
    # what they agree on -- measured: frbox<->naijarom overlap 232, frbox<->ota
    # 45 of 45 -- while device NAMES diverge wildly ("A14", "Tecno Orange Rise
    # 32", "TECNO PHANTOM V Fold 5G" for the same class of thing). So the model
    # code is the identity, which is also how firmware is actually organised by
    # these vendors.
    if source in ("frbox.community.transsion_catalog",
                  "naijarom.community.transsion_firmware",
                  "google.ota.checkin"):
        maker = (data.get("manufacturer") or "").strip()
        code = (data.get("model_code") or "").strip()
        if not (maker and code):
            return None
        # Transsion ships three brands; keep them distinct rather than collapsing
        # TECNO/Infinix/itel into one manufacturer.
        maker = {"TECNO": "TECNO", "INFINIX": "Infinix", "ITEL": "itel"}.get(maker.upper(), maker)
        return maker, code, "model_code", code

    if source == "ipsw.me.firmware_index":
        # Apple's own per-model identifier is the stable key; the marketing name
        # ("iPhone 2G") is what a person recognises, so it is the canonical name
        # and the identifier is the namespace value.
        name = (data.get("source_device_name") or "").strip()
        ident = (data.get("source_model_identifier") or data.get("model_code") or "").strip()
        if not (name and ident):
            return None
        return "Apple", name, "apple_identifier", ident

    if source == "tecno.vendor.security_device_scope":
        return "TECNO", data["device"].strip(), "commercial_name", data["device"].strip()
    return None


CHILD_TABLES = ("observation_product_links", "source_identity_registry",
                "identity_conclusions", "observed_product_silicon",
                "product_firmware_releases", "product_security_publications",
                "source_specifications", "source_build_product_links",
                "product_hardware_links")


# THE ONE RULE that decides whether an observation's link may serve.
#
# observation_product_links.link_state is not an independent fact: it MIRRORS the
# resolution_state of the registry row the link names. Nothing here decides an
# identity; it only reads a decision that source_identity_registry already holds.
#
# Both halves of the condition are load-bearing, and the second one is the half
# four separate call sites each left out:
#
#   * `resolution_state='approved'` -- the identity is settled.
#   * `sir.product_id = opl.product_id` -- and settled FOR THIS PRODUCT.
#
# source_identity_registry.id is uuid5(source_id, namespace, normalized_value) and
# carries NO product component, while ON CONFLICT(source_id,namespace,
# normalized_value) means the row keeps whichever product_id inserted it first. So
# one identity id can be reached from observations that resolve to DIFFERENT
# products, and the approval granted to one of them then licensed the others.
#
# Measured on the live corpus: the Xiaomi tracker publishes model code HM2013023
# as "Redmi 1 W Global" (which the vendor catalogue confirms, so that product is
# approved) and also as "Redmi 1 China / Global / Taiwan", which the region strip
# turns into the separate product "Redmi 1". Nine links on the still-proposed
# "Redmi 1" were `link_state='approved'`, inheriting an approval granted to
# "Redmi 1 W" -- a state promotion half-admits and nobody granted. Four more sit
# on "MI 3" holding "MI 3 / Mi 4"'s approval. 13 in total, and the only reason
# none of them served a falsehood is that promotion also requires the PRODUCT to
# be approved, which "Redmi 1" is not.
#
# Whether "Redmi 1" and "Redmi 1 W" are one phone is an identity judgement and is
# deliberately NOT made here: the rule refuses rather than infers, and
# integrity.check_corpus reports the cross-product links so the judgement is
# visible instead of silently taken.
LINK_LICENCE_CONDITION = """EXISTS (
        SELECT 1 FROM source_identity_registry sir
         WHERE sir.id = observation_product_links.identity_id
           AND sir.product_id = observation_product_links.product_id
           AND sir.resolution_state = 'approved')"""


def refresh_observation_link_states(connection: sqlite3.Connection, *,
                                    product_id: str | None = None) -> dict[str, int]:
    """Bring link_state into line with LINK_LICENCE_CONDITION, both ways.

    The ONLY writer of link_state outside the bridge's own insert. Before this
    existed the rule had four copies -- the insert below, `_conclude`'s
    per-product UPDATE, `identity_backfill`'s bulk refresh, and the server's
    manual-review path -- three of which matched on identity_id alone and so
    could approve a link for a product the identity does not belong to. The same
    rule in four places is how the same defect survived in three of them.

    It DEMOTES as well as approves, because a link that is approved while its
    licence is not is exactly the state that must not be representable.

    WHAT DEMOTION DOES NOT DO, measured so it is not a surprise: it withdraws
    permission to promote NEW rows and never retracts an already promoted
    product_firmware_releases row. Promotion is INSERT OR IGNORE and deletes
    nothing. On the live corpus the first reconciliation demotes 13 links and
    leaves 4 releases behind on the device "MI 3", promoted earlier under the
    borrowed approval of "MI 3 / Mi 4" -- 27,841 releases and 845 devices with
    current firmware, unchanged either way. Retracting those four would be
    withdrawing a published fact on the strength of a refusal, and whether the two
    products are one device is exactly the identity decision this rule declines to
    make; integrity.check_corpus reports the pair so the decision is visible.
    """
    approved = connection.execute(
        f"UPDATE observation_product_links SET link_state='approved' "
        f" WHERE link_state <> 'approved' AND {LINK_LICENCE_CONDITION}"
        + (" AND product_id=?" if product_id else ""),
        (product_id,) if product_id else ()).rowcount
    demoted = connection.execute(
        f"UPDATE observation_product_links SET link_state='proposed' "
        f" WHERE link_state = 'approved' AND NOT {LINK_LICENCE_CONDITION}"
        + (" AND product_id=?" if product_id else ""),
        (product_id,) if product_id else ()).rowcount
    return {"links_approved": approved, "links_demoted": demoted}


def repair_derived_ids(connection: sqlite3.Connection) -> int:
    """Re-derive product ids that no longer match their normalised name.

    source_products.id is uuid5(manufacturer, normalized_name) -- the id is DERIVED,
    not arbitrary. Anything that rewrites normalized_name without recomputing the id
    breaks that invariant, and the break is invisible until the next ingest: the bridge
    computes the correct id, the upsert lands on the existing row via
    ON CONFLICT(manufacturer, normalized_name) and leaves the OLD id in place, and then
    the link insert references an id that does not exist. FOREIGN KEY constraint failed.

    That is exactly what migration 0016 did. It merged the brand-prefixed duplicates
    correctly and rewrote their normalized_name, and SQL cannot compute a uuid5, so the
    ids were left stale. Running this makes the repair idempotent and self-healing: a
    later migration that touches names cannot leave the corpus unloadable.

    Foreign keys are suspended for the swap because the new and old rows cannot both
    satisfy UNIQUE(manufacturer, normalized_name) while children are repointed. They
    are re-enabled and verified before returning.
    """
    rows = connection.execute(
        "SELECT id, manufacturer, normalized_name FROM source_products").fetchall()
    stale = [(r[0], _id("product", r[1], r[2])) for r in rows]
    stale = [(old, new) for old, new in stale if old != new]
    if not stale:
        return 0
    fk = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        for old, new in stale:
            connection.execute("UPDATE source_products SET id=? WHERE id=?", (new, old))
            for t in CHILD_TABLES:
                try:
                    connection.execute(f"UPDATE {t} SET product_id=? WHERE product_id=?",
                                       (new, old))
                except sqlite3.OperationalError:
                    pass
        connection.commit()
    finally:
        connection.execute(f"PRAGMA foreign_keys={'ON' if fk else 'OFF'}")
    bad = connection.execute(
        "SELECT COUNT(*) FROM observation_product_links "
        "WHERE product_id NOT IN (SELECT id FROM source_products)").fetchone()[0]
    if bad:
        raise RuntimeError(f"id repair left {bad} dangling product links")
    return len(stale)


def rebuild_identity_registry(connection: sqlite3.Connection, specs_csv: Path | None = None) -> dict[str, int]:
    specs: dict[str, dict] = {}
    if specs_csv and specs_csv.is_file():
        with specs_csv.open(encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                specs[_norm(row["device_name"])] = row
    repaired = repair_derived_ids(connection)
    products: set[str] = set()
    identities: set[str] = set()
    links = 0
    rows = connection.execute("SELECT id,source_id,observed_at,payload_json FROM observations ORDER BY observed_at").fetchall()
    with connection:
        for row in rows:
            payload = json.loads(row["payload_json"])
            resolved = _product_name(row["source_id"], payload)
            if not resolved:
                continue
            maker, name, namespace, source_value = resolved
            normalized_name = _norm(name, maker)
            product_id = _id("product", maker, normalized_name)
            identity_id = _id("identity", row["source_id"], namespace, _norm(source_value))
            spec = specs.get(normalized_name) if maker == "Xiaomi" else None
            now = row["observed_at"]
            connection.execute("""INSERT INTO source_products
              (id,manufacturer,canonical_name,normalized_name,review_state,specification_json,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(manufacturer,normalized_name) DO UPDATE SET
              updated_at=max(updated_at,excluded.updated_at),
              specification_json=coalesce(source_products.specification_json,excluded.specification_json)""",
              (product_id, maker, name, normalized_name, "proposed",
               json.dumps(spec, sort_keys=True) if spec else None, now, now))
            connection.execute("""INSERT INTO source_identity_registry
              (id,source_id,namespace,source_value,normalized_value,product_id,resolution_state,resolution_method,
               rule_version,confidence,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(source_id,namespace,normalized_value) DO UPDATE SET
              last_seen_at=max(last_seen_at,excluded.last_seen_at)""",
              (identity_id, row["source_id"], namespace, source_value, _norm(source_value), product_id,
               "proposed", "deterministic_source_catalog", "1", "high" if spec else "medium", now, now))
            # The licence has to be read for THIS product, not for the identity
            # id alone. Asking only `WHERE id=?` is what minted the nine approved
            # links on the proposed product "Redmi 1": the identity is approved,
            # but for "Redmi 1 W". See LINK_LICENCE_CONDITION.
            licensed = connection.execute(
                "SELECT 1 FROM source_identity_registry WHERE id=? AND product_id=?"
                " AND resolution_state='approved'", (identity_id, product_id)).fetchone()
            connection.execute("""INSERT OR REPLACE INTO observation_product_links
              (observation_id,product_id,identity_id,link_state,created_at) VALUES(?,?,?,?,?)""",
              (row["id"], product_id, identity_id, "approved" if licensed else "proposed", now))
            products.add(product_id); identities.add(identity_id); links += 1
    return {"products": len(products), "identities": len(identities), "links": links,
            "ids_repaired": repaired,
            "specification_matches": connection.execute("SELECT count(*) FROM source_products WHERE specification_json IS NOT NULL").fetchone()[0]}
