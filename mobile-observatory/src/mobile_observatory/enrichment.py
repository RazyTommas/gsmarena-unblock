from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import uuid
from collections import defaultdict
from pathlib import Path

_NS = uuid.UUID("a61652aa-f30a-40c4-9135-7e38dc86f330")
_REGION = re.compile(r"\s+(EEA|Global|China|India|Indonesia|Japan|Russia|Taiwan|Turkey)$", re.I)
_PROCESS = re.compile(r"\s*\(\d+(?:\.\d+)?\s*nm\)\s*$", re.I)
_SOC_VENDOR = (("qualcomm", "Qualcomm"), ("snapdragon", "Qualcomm"),
               ("mediatek", "MediaTek"), ("dimensity", "MediaTek"), ("helio", "MediaTek"),
               ("exynos", "Samsung"), ("samsung", "Samsung"), ("xring", "Xiaomi"),
               ("unisoc", "Unisoc"), ("kirin", "Huawei"), ("apple", "Apple"))
_MT_PART = re.compile(r"\bMT\d{4,5}\b", re.I)


def _id(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "\x1f".join(parts)))


def _norm(value: str) -> str:
    value = value.casefold().replace("xiaomi ", "", 1).replace("tecno ", "", 1)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def _xiaomi_catalog(path: Path) -> dict[str, list[str]]:
    """Read this repository's deliberately simple string-list YAML without a YAML dependency."""
    result: dict[str, list[str]] = {}
    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw and not raw.startswith((" ", "-")) and raw.endswith(":"):
            current = raw[:-1].strip(); result[current] = []
        elif current and raw.startswith("- "):
            result[current].append(raw[2:].strip().strip("'\""))
    return result


def _spec_index(path: Path) -> dict[str, list[dict[str, str]]]:
    found: dict[str, list[dict[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            found[_norm(row["device_name"])].append(row)
    return found


def _vendor(raw: str) -> str | None:
    low = raw.casefold()
    return next((name for token, name in _SOC_VENDOR if token in low), None)


def _soc_parts(raw: str) -> tuple[str | None, str, str]:
    clean = _PROCESS.sub("", raw).strip()
    vendor = _vendor(clean)
    part = clean
    if vendor:
        part = re.sub(rf"^{re.escape(vendor)}\s+", "", clean, flags=re.I)
    return vendor, part, clean


def _capture_evidence(connection: sqlite3.Connection, *, source_id: str, source_name: str,
                      source_url: str, path: Path, locator: str, excerpt: str,
                      now: str) -> str:
    """Register a captured input and return row-level evidence for a derived fact."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    run_id = _id("capture-run", source_id, digest)
    artifact_id = _id("capture-artifact", source_id, digest)
    evidence_id = _id("capture-evidence", artifact_id, locator)
    connection.execute("INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?,?)",
                       (source_id, source_name, source_url, "primary", 1, now))
    connection.execute("""INSERT OR IGNORE INTO ingestion_runs
      (id,source_id,started_at,finished_at,outcome,parser_name,parser_version,
       fetched_count,accepted_count,rejected_count,error)
      VALUES(?,?,?,?,'succeeded','captured-security-csv','1',0,0,0,NULL)""",
                       (run_id, source_id, now, now))
    connection.execute("""INSERT OR IGNORE INTO artifacts
      VALUES(?,?,?,?,?,?,?,?,?)""", (artifact_id, source_id, run_id, digest, "text/csv",
      source_url, now, str(path), path.stat().st_size))
    connection.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,NULL,?,?,?)",
                       (evidence_id, artifact_id, locator, excerpt[:1000], now))
    return evidence_id


def _component_type(name: str) -> str:
    low = name.casefold()
    if "kernel" in low: return "kernel"
    if "wifi" in low or "wi-fi" in low or "wlan" in low: return "wifi"
    if "bluetooth" in low: return "bluetooth"
    if "modem" in low or "telephony" in low: return "modem"
    if "driver" in low: return "driver"
    if "firmware" in low: return "firmware"
    return "other"


def automate_identity_review(connection: sqlite3.Connection, *, devices_yml: Path,
                             specs_csv: Path, google_play_csv: Path | None = None,
                             decisions: list[dict] | None = None) -> dict[str, int]:
    """Conclude every product, but approve only exact, independently supported matches.

    Approval means the captured source identity belongs to the source product. It never
    creates a hardware model or fabricates a model code.
    """
    catalog = _xiaomi_catalog(devices_yml)
    specs = _spec_index(specs_csv)
    play: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    if google_play_csv and google_play_csv.is_file():
        with google_play_csv.open(encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                brand, name = row["Retail Branding"].strip(), row["Marketing Name"].strip()
                if brand and name:
                    play[(brand.casefold(), _norm(name))].append(row)
    totals = defaultdict(int)
    rows = connection.execute("SELECT * FROM source_products ORDER BY manufacturer,canonical_name").fetchall()
    with connection:
        for product in rows:
            identities = connection.execute(
                "SELECT * FROM source_identity_registry WHERE product_id=? ORDER BY source_value", (product["id"],)
            ).fetchall()
            remembered = connection.execute("SELECT conclusion FROM identity_conclusions WHERE product_id=?", (product["id"],)).fetchone()
            blocked = product['review_state'] == 'rejected' or any(
                i['resolution_state'] == 'rejected' or i['resolution_method'] == 'manual_product_review' for i in identities)
            blocked = blocked or any(d.get('decision') in ('different', 'defer') and
                (d.get('canonical_id') == product['id'] or any(d.get('source_namespace') in (i['namespace'], i['source_id'])
                    and d.get('source_value') == i['source_value'] for i in identities)) for d in (decisions or []))
            if remembered or blocked:
                totals[remembered['conclusion'] if remembered else 'insufficient_evidence'] += 1
                continue
            candidate_names: list[str] = []
            evidence: list[dict] = []
            exact_catalog = False
            if product["manufacturer"] == "Xiaomi":
                for identity in identities:
                    names = catalog.get(identity["source_value"], [])
                    candidate_names.extend(names)
                    normalized = _norm(_REGION.sub("", product["canonical_name"]))
                    exact_catalog = exact_catalog or any(_norm(_REGION.sub("", n)) == normalized for n in names)
                    if names:
                        evidence.append({"source": "xiaomi_devices_yml", "identity": identity["source_value"],
                                         "catalog_names": names})
            matches = specs.get(_norm(product["canonical_name"]), [])
            if len(matches) == 1:
                evidence.append({"source": "gsmarena_captured_specs", "slug": matches[0]["slug"],
                                 "device_name": matches[0]["device_name"]})
            play_matches = play.get((product["manufacturer"].casefold(), _norm(product["canonical_name"])), [])
            play_models = sorted({r["Model"].strip() for r in play_matches if r["Model"].strip()})
            if play_models:
                candidate_names.extend(play_models)
                evidence.append({"source": "google_play_supported_devices", "marketing_name": product["canonical_name"],
                                 "model_codes": play_models,
                                 "device_codes": sorted({r["Device"].strip() for r in play_matches if r["Device"].strip()})})
            official_tecno_scope = (product["manufacturer"] == "TECNO" and any(
                i["source_id"] == "tecno.vendor.security_device_scope" for i in identities))
            if official_tecno_scope:
                # The source is TECNO's own device-scope publication. It is
                # authoritative for the commercial product relationship even
                # when Google Play lists several regional hardware codes. Those
                # hardware variants remain candidates; they are not collapsed.
                conclusion, confidence, method = "auto_approved", "authoritative", "tecno_vendor_device_scope"
                rationale = ("TECNO's captured security publication explicitly scopes the record to this "
                             "commercial product. Candidate hardware codes remain separate unresolved variants.")
            elif exact_catalog and len(matches) == 1:
                conclusion, confidence, method = "auto_approved", "high", "vendor_catalog_plus_exact_spec_name"
                rationale = "Exact Xiaomi codename catalog name and unique captured specification name agree."
            elif exact_catalog:
                conclusion, confidence, method = "auto_approved", "authoritative", "xiaomi_vendor_codename_catalog"
                rationale = "Exact codename-to-product name relationship is present in Xiaomi's captured device catalog."
            elif len(play_models) == 1:
                conclusion, confidence, method = "auto_approved", "high", "exact_unique_google_play_name"
                rationale = "Brand and commercial name uniquely match one model in the captured Google Play supported-device catalog."
            elif len(play_models) > 1:
                conclusion, confidence, method = "ambiguous", "medium", "google_play_name_multiple_models"
                rationale = "The captured Google Play catalog confirms the product name but lists multiple hardware models."
            elif len(matches) == 1:
                conclusion, confidence, method = "auto_approved", "high", "exact_unique_spec_name"
                rationale = "Unique exact normalized commercial-name match in captured specifications."
            elif candidate_names or len(matches) > 1:
                conclusion, confidence, method = "ambiguous", "medium", "ranked_candidates"
                rationale = "Evidence produces multiple or non-exact candidate names; no silent merge is safe."
            else:
                conclusion, confidence, method = "insufficient_evidence", "low", "no_independent_identifier"
                rationale = "No independent authoritative identifier or unique specification match is available."
            now = max((i["last_seen_at"] for i in identities), default=product["updated_at"])
            connection.execute("""INSERT OR REPLACE INTO identity_conclusions
              VALUES(?,?,?,?,?,?,?,?,?)""", (product["id"], conclusion, confidence, method, "1", rationale,
              json.dumps(sorted(set(candidate_names))), json.dumps(evidence, sort_keys=True), now))
            if conclusion == "auto_approved":
                connection.execute("UPDATE source_products SET review_state='approved',updated_at=? WHERE id=?", (now, product["id"]))
                connection.execute("UPDATE source_identity_registry SET resolution_state='approved',resolution_method=?,rule_version='1',confidence=? WHERE product_id=?",
                                   (method, confidence, product["id"]))
                connection.execute("UPDATE observation_product_links SET link_state='approved' WHERE product_id=?", (product["id"],))
            totals[conclusion] += 1
    from .product_specs import enrich_product_specs
    enrich_product_specs(connection, specs_csv=specs_csv, devices_yml=devices_yml, google_play_csv=google_play_csv, decisions=decisions)
    totals["total"] = len(rows)
    totals["silicon_observations"] = connection.execute("SELECT count(*) FROM observed_product_silicon").fetchone()[0]
    return dict(totals)


def promote_approved_product_observations(connection: sqlite3.Connection) -> dict[str, int]:
    """Build useful product histories without pretending products are hardware models.

    This is intentionally a separate read model from ``firmware_releases``. A
    Xiaomi codename or TECNO commercial name is enough to navigate source-backed
    history, but not enough to manufacture a canonical hardware model code.
    """
    promoted_firmware = promoted_security = android_events = 0
    rows = connection.execute("""SELECT o.id observation_id,o.source_id,o.observed_at,o.payload_json,
      opl.product_id,opl.identity_id
      FROM observation_product_links opl
      JOIN observations o ON o.id=opl.observation_id
      JOIN source_products sp ON sp.id=opl.product_id
      WHERE opl.link_state='approved' AND sp.review_state='approved'
      ORDER BY o.observed_at,o.id""").fetchall()
    with connection:
        for row in rows:
            data = json.loads(row["payload_json"])["data"]
            if data.get("build"):
                android = str(data.get("android") or "").strip() or None
                major = None
                if android:
                    match = re.match(r"^(\d+)", android)
                    major = int(match.group(1)) if match else None
                release_id = _id("product-firmware", row["observation_id"])
                before = connection.total_changes
                connection.execute("""INSERT OR IGNORE INTO product_firmware_releases
                  (id,product_id,identity_id,observation_id,source_id,region_code,build_id,channel,
                   android_version,android_major,vendor_released_at,delivery_method,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (release_id,row["product_id"],row["identity_id"],row["observation_id"],row["source_id"],
                   data.get("region_code") or "SOURCE_UNSPECIFIED",data["build"],data.get("branch") or "unknown",
                   android,major,data.get("release_date") or None,data.get("delivery_method"),row["observed_at"]))
                promoted_firmware += connection.total_changes > before
            elif data.get("aspl_month"):
                publication_id = _id("product-security", row["observation_id"])
                before = connection.total_changes
                connection.execute("""INSERT OR IGNORE INTO product_security_publications
                  (id,product_id,identity_id,observation_id,source_id,security_patch_month,published_at,title,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?)""",
                  (publication_id,row["product_id"],row["identity_id"],row["observation_id"],row["source_id"],
                   data["aspl_month"],data.get("publish_date"),data.get("title"),row["observed_at"]))
                promoted_security += connection.total_changes > before

        # Only increasing Android majors on the same product, market and
        # channel are upgrade evidence. Cross-region differences and apparent
        # downgrades are deliberately not labelled upgrades.
        histories = connection.execute("""SELECT pfr.*,sp.canonical_name
          FROM product_firmware_releases pfr JOIN source_products sp ON sp.id=pfr.product_id
          WHERE android_major IS NOT NULL
          ORDER BY product_id,region_code,channel,vendor_released_at,id""").fetchall()
        previous: dict[tuple[str, str, str], sqlite3.Row] = {}
        for release in histories:
            key = (release["product_id"],release["region_code"],release["channel"])
            old = previous.get(key)
            if old and release["android_major"] > old["android_major"]:
                dedupe = (f"product-android:{release['product_id']}:{release['region_code']}:"
                          f"{release['channel']}:{old['id']}:{release['id']}")
                event_id = _id("domain-event", dedupe)
                before = connection.total_changes
                connection.execute("""INSERT OR IGNORE INTO domain_events
                  (id,event_type,subject_type,subject_id,dedupe_key,occurred_at,recorded_at,
                   before_json,after_json,evidence_id,corrects_event_id)
                  VALUES(?,'android_version_changed','source_product',?,?,?,?,?,?,NULL,NULL)""",
                  (event_id,release["product_id"],dedupe,release["vendor_released_at"] or release["created_at"],
                   release["created_at"],json.dumps({"android":old["android_version"],"build":old["build_id"],
                                                     "region":old["region_code"]},sort_keys=True),
                   json.dumps({"android":release["android_version"],"build":release["build_id"],
                               "region":release["region_code"],"device":release["canonical_name"],
                               "source_identity":release["identity_id"]},sort_keys=True)))
                android_events += connection.total_changes > before
            if old is None or (release["vendor_released_at"] or "") >= (old["vendor_released_at"] or ""):
                previous[key] = release
    return {"firmware_releases": promoted_firmware, "security_publications": promoted_security,
            "android_upgrade_events": android_events}


def enrich_canonical_silicon(connection: sqlite3.Connection, xref_csv: Path) -> dict[str, int]:
    """Attach SoCs only when a captured xref model exactly equals a canonical model code."""
    attached = parts = 0
    with xref_csv.open(encoding="utf-8-sig") as handle, connection:
        for row in csv.DictReader(handle):
            hw = connection.execute("SELECT id FROM hardware_models WHERE model_code_normalized=?",
                                    (" ".join(row["model"].strip().upper().split()),)).fetchone()
            if not hw or not row["soc"].strip():
                continue
            vendor, part, marketing = _soc_parts(row["soc"])
            if not vendor:
                continue
            vendor_id, family_id, part_id = _id("vendor", vendor), _id("family", vendor, vendor), _id("part", vendor, part)
            now = "2026-09-16T00:00:00Z"
            connection.execute("INSERT OR IGNORE INTO silicon_vendors VALUES(?,?,?)", (vendor_id, vendor, now))
            connection.execute("INSERT OR IGNORE INTO silicon_families VALUES(?,?,NULL,?,?)", (family_id, vendor_id, vendor, now))
            before = connection.total_changes
            connection.execute("INSERT OR IGNORE INTO silicon_parts VALUES(?,?,?,?,?,?,?)",
                               (part_id, family_id, part, marketing, None, None, now))
            parts += connection.total_changes > before
            before = connection.total_changes
            # WITHOUT ROWID makes every primary-key column non-null; use the
            # captured assertion date as the start of this evidence interval.
            connection.execute("INSERT OR IGNORE INTO hardware_silicon VALUES(?,?,NULL,'primary_soc',NULL,?,NULL)",
                               (hw["id"], part_id, now))
            attached += connection.total_changes > before
    return {"hardware_silicon_attached": attached, "silicon_parts_created": parts}


def import_security_catalog(connection: sqlite3.Connection, asb_csv: Path) -> dict[str, int]:
    """Import ASB CVEs, named component claims, and explicit SPL fix coordinates."""
    now = "2026-09-16T00:00:00Z"; source_id = "android.security.bulletins.captured"
    advisories: set[str] = set(); cves: set[str] = set(); fixes: set[str] = set(); claims: set[str] = set()
    with connection:
        with asb_csv.open(encoding="utf-8-sig") as handle:
            for line, row in enumerate(csv.DictReader(handle), 2):
                cve = row["cve"].strip().upper(); month = row["bulletin_month"].strip()
                if not re.fullmatch(r"CVE-\d{4}-\d+", cve) or not month:
                    continue
                evidence_id = _capture_evidence(connection, source_id=source_id,
                    source_name="Android Security Bulletins (captured)",
                    source_url="https://source.android.com/docs/security/bulletin", path=asb_csv,
                    locator=f"csv:line={line}", excerpt=json.dumps(row, sort_keys=True), now=now)
                vid, aid = _id("vulnerability", cve), _id("advisory", source_id, month)
                connection.execute("INSERT OR IGNORE INTO vulnerabilities VALUES(?,?,?,?,?)", (vid, cve, row["section"] or None, month + "-01", None))
                connection.execute("INSERT OR IGNORE INTO advisories VALUES(?,?,?,?,?,?,NULL)",
                                   (aid, source_id, month, f"Android Security Bulletin {month}", month + "-01", None))
                connection.execute("INSERT OR IGNORE INTO advisory_vulnerabilities VALUES(?,?)", (aid, vid))
                tier = row.get("tier", "").strip()
                if tier in {"01", "05", "06"}:
                    fix_id = _id("asb-spl-fix", cve, month, tier)
                    coordinate = {"security_patch_level": f"{month}-{tier}", "meaning": "addressed_by"}
                    connection.execute("INSERT OR IGNORE INTO fix_claims VALUES(?,?,'android_spl',?,?,?)",
                                       (fix_id, vid, json.dumps(coordinate, sort_keys=True), evidence_id, now))
                    fixes.add(fix_id)
                component = (row.get("component") or "").strip()
                if component:
                    vendor_name = "Qualcomm" if "qualcomm" in row.get("section", "").casefold() else None
                    vendor_id = _id("vendor", vendor_name) if vendor_name else None
                    if vendor_name:
                        connection.execute("INSERT OR IGNORE INTO silicon_vendors VALUES(?,?,?)", (vendor_id, vendor_name, now))
                    component_id = _id("component", vendor_name or "generic", component)
                    connection.execute("INSERT OR IGNORE INTO components VALUES(?,?,?,?,?,?)",
                        (component_id, _component_type(component), vendor_id, component, None, now))
                    claim_id = _id("asb-component-affected", cve, component_id, month, tier)
                    connection.execute("INSERT OR IGNORE INTO applicability_claims VALUES(?,?,'component',?,'affected',?,?,?)",
                        (claim_id, vid, component_id, json.dumps({"bulletin_month": month, "tier": tier}), evidence_id, now))
                    claims.add(claim_id)
                advisories.add(aid); cves.add(vid)
    return {"advisories": len(advisories), "vulnerabilities": len(cves),
            "component_claims": len(claims), "spl_fix_claims": len(fixes)}


def import_mediatek_catalog(connection: sqlite3.Connection, mediatek_csv: Path) -> dict[str, int]:
    """Import vendor-declared CVE-to-MediaTek-part applicability with row evidence."""
    now = "2026-09-16T00:00:00Z"; source_id = "mediatek.security.bulletins.captured"
    advisories: set[str] = set(); cves: set[str] = set(); parts: set[str] = set(); claims: set[str] = set()
    with connection:
        vendor_id, family_id = _id("vendor", "MediaTek"), _id("family", "MediaTek", "MediaTek")
        connection.execute("INSERT OR IGNORE INTO silicon_vendors VALUES(?,?,?)", (vendor_id, "MediaTek", now))
        connection.execute("INSERT OR IGNORE INTO silicon_families VALUES(?,?,NULL,?,?)", (family_id, vendor_id, "MediaTek", now))
        with mediatek_csv.open(encoding="utf-8-sig") as handle:
            for line, row in enumerate(csv.DictReader(handle), 2):
                cve, month = row["cve"].strip().upper(), row["bulletin_month"].strip()
                if not re.fullmatch(r"CVE-\d{4}-\d+", cve) or not month:
                    continue
                evidence_id = _capture_evidence(connection, source_id=source_id,
                    source_name="MediaTek Security Bulletins (captured)",
                    source_url="https://corp.mediatek.com/product-security-bulletin", path=mediatek_csv,
                    locator=f"csv:line={line}", excerpt=json.dumps(row, sort_keys=True), now=now)
                vid, aid = _id("vulnerability", cve), _id("advisory", source_id, month)
                connection.execute("INSERT OR IGNORE INTO vulnerabilities VALUES(?,?,?,?,?)",
                                   (vid, cve, row["component"] or None, month + "-01", None))
                connection.execute("INSERT OR IGNORE INTO advisories VALUES(?,?,?,?,?,?,NULL)",
                                   (aid, source_id, month, f"MediaTek Security Bulletin {month}", month + "-01", None))
                connection.execute("INSERT OR IGNORE INTO advisory_vulnerabilities VALUES(?,?)", (aid, vid))
                for part_number in sorted({m.upper() for m in _MT_PART.findall(row.get("chipsets", ""))}):
                    part_id = _id("part", "MediaTek", part_number)
                    connection.execute("INSERT OR IGNORE INTO silicon_parts VALUES(?,?,?,?,?,?,?)",
                                       (part_id, family_id, part_number, part_number, None, None, now))
                    claim_id = _id("mediatek-part-affected", cve, part_number, month)
                    constraint = {"bulletin_month": month, "severity": row.get("severity") or None,
                                  "component": row.get("component") or None}
                    connection.execute("INSERT OR IGNORE INTO applicability_claims VALUES(?,?,'silicon_part',?,'affected',?,?,?)",
                        (claim_id, vid, part_id, json.dumps(constraint, sort_keys=True), evidence_id, now))
                    parts.add(part_id); claims.add(claim_id)
                advisories.add(aid); cves.add(vid)
        connection.execute("""INSERT OR REPLACE INTO security_coverage_gaps
          VALUES(?,?,?,?,?,?,?)""", (_id("coverage-gap", "Qualcomm", "chipset_cve_mapping"), "Qualcomm",
          "chipset_cve_mapping", "missing",
          "No captured authoritative Qualcomm advisory-to-chipset artifact is present; Android bulletins identify Qualcomm components but not defensible affected part mappings.",
          "crawler/relay/results/cross-reference/result.json", now))
        connection.execute("""INSERT OR REPLACE INTO security_coverage_gaps
          VALUES(?,?,?,?,?,?,?)""", (_id("coverage-gap", "MediaTek", "chipset_cve_mapping"), "MediaTek",
          "chipset_cve_mapping", "available", "Captured vendor bulletins enumerate affected MT part numbers.",
          str(mediatek_csv), now))
    return {"advisories": len(advisories), "vulnerabilities": len(cves),
            "silicon_parts": len(parts), "part_applicability_claims": len(claims)}


def write_agent_review_bundle(connection: sqlite3.Connection, output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = connection.execute("""SELECT sp.id,sp.manufacturer,sp.canonical_name,ic.conclusion,ic.confidence,
      ic.rationale,ic.candidates_json,ic.evidence_json,group_concat(DISTINCT sir.source_value) source_values,
      count(DISTINCT opl.observation_id) observation_count
      FROM source_products sp JOIN identity_conclusions ic ON ic.product_id=sp.id
      LEFT JOIN source_identity_registry sir ON sir.product_id=sp.id
      LEFT JOIN observation_product_links opl ON opl.product_id=sp.id
      WHERE ic.conclusion!='auto_approved' GROUP BY sp.id ORDER BY sp.manufacturer,sp.canonical_name""").fetchall()
    candidates = [{**dict(r), "candidates": json.loads(r["candidates_json"]), "evidence": json.loads(r["evidence_json"])} for r in rows]
    for item in candidates:
        item.pop("candidates_json"); item.pop("evidence_json")
    prompt = """# Mobile Observatory identity-review assignment

Review the attached `identity-candidates.json`. The objective is to connect source identities to real products and hardware models without inventing model codes.

Rules:
1. Prefer vendor model codes, vendor codenames, regulatory records, and captured primary evidence.
2. A commercial-name similarity alone is never enough to assert a hardware model code.
3. Preserve regional variants and aliases; do not collapse distinct hardware revisions.
4. Return one JSON object per product with: product_id, decision (same/different/defer), canonical_name, model_codes, aliases, confidence, evidence, rationale.
5. Use `defer` whenever evidence is missing or conflicting. Never silently promote a guess.
6. Explain chipset evidence independently from identity evidence.

The system stores observations immutably, remembers reviewed decisions, and automatically reuses approved source identities for later firmware or security records. Your output is a proposal for validation, not permission to overwrite canonical facts.
"""
    (output_dir / "agent-review-prompt.md").write_text(prompt, encoding="utf-8")
    (output_dir / "identity-candidates.json").write_text(json.dumps(candidates, indent=2, sort_keys=True), encoding="utf-8")
    digest = hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest()
    return {"candidate_count": len(candidates), "prompt": str(output_dir / "agent-review-prompt.md"),
            "candidates": str(output_dir / "identity-candidates.json"), "sha256": digest}
