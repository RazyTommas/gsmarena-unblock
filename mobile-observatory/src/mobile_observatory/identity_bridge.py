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
    if source == "tecno.vendor.security_device_scope":
        return "TECNO", data["device"].strip(), "commercial_name", data["device"].strip()
    return None


def rebuild_identity_registry(connection: sqlite3.Connection, specs_csv: Path | None = None) -> dict[str, int]:
    specs: dict[str, dict] = {}
    if specs_csv and specs_csv.is_file():
        with specs_csv.open(encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                specs[_norm(row["device_name"])] = row
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
            remembered = connection.execute(
                "SELECT resolution_state FROM source_identity_registry WHERE id=?", (identity_id,)
            ).fetchone()[0]
            connection.execute("""INSERT OR REPLACE INTO observation_product_links
              (observation_id,product_id,identity_id,link_state,created_at) VALUES(?,?,?,?,?)""",
              (row["id"], product_id, identity_id, "approved" if remembered == "approved" else "proposed", now))
            products.add(product_id); identities.add(identity_id); links += 1
    return {"products": len(products), "identities": len(identities), "links": links,
            "specification_matches": connection.execute("SELECT count(*) FROM source_products WHERE specification_json IS NOT NULL").fetchone()[0]}
