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
_PROCESS_ANNOTATION = re.compile(r"\(\d+(?:\.\d+)?\s*nm\+?\)", re.I)
_SOC_VENDOR = (("qualcomm", "Qualcomm"), ("snapdragon", "Qualcomm"),
               ("mediatek", "MediaTek"), ("dimensity", "MediaTek"), ("helio", "MediaTek"),
               ("exynos", "Samsung"), ("samsung", "Samsung"), ("xring", "Xiaomi"),
               ("unisoc", "Unisoc"), ("kirin", "Huawei"), ("apple", "Apple"),
               ("google tensor", "Google"), ("nvidia tegra", "NVIDIA"),
               ("intel atom", "Intel"), ("ti omap", "Texas Instruments"))
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
    connection.execute("INSERT OR IGNORE INTO sources(id,name,base_url,authority_scope,enabled,created_at) VALUES(?,?,?,?,?,?)",
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


# Google Play spells these vendors differently from the corpus ("Tecno",
# "Tecno Mobile" and "Itel" against "TECNO" and "itel"), so both sides are
# folded before comparison. This is a spelling alias for one vendor, not a
# judgement that two vendors are the same.
# Bumped when a rule changes what the corpus can conclude. Conclusions that
# resolved NOTHING under an older version are re-evaluated; resolved ones are
# left alone. Version 2 adds the code-keyed Google Play lookup.
RULE_VERSION = "2"

_PLAY_BRAND_ALIASES = {"tecno mobile": "tecno"}
# Play's Model column sometimes carries the brand as a prefix ("TECNO CN7c")
# and sometimes not ("CN7c"); the OTA feed always reports it bare.
_BRAND_PREFIX = re.compile(r"^(tecno mobile|tecno|infinix|itel)\s+", re.I)


def _play_brand(value: str) -> str:
    folded = value.strip().casefold()
    return _PLAY_BRAND_ALIASES.get(folded, folded)


def _bare_code(value: str) -> str:
    """A vendor hardware code with any brand prefix removed, case-folded."""
    return _BRAND_PREFIX.sub("", value.strip()).strip().casefold()


from .repository import clean_device_name as _clean_name  # noqa: E402  (shared definition)


def _name_key(value: str) -> str:
    """Fold a marketing name for COUNTING distinct names."""
    return _BRAND_PREFIX.sub("", _clean_name(value)).strip().casefold()


# A marketing name that covers SEVERAL devices. Play publishes
# "SMART 7  or SMART 7 PLUS" for Infinix X6517; promoting it would make two
# phones one row that answers for both.
_NAMES_SEVERAL = re.compile(r"\bor\b", re.I)


from .repository import usable_device_name as _usable_device_name  # noqa: E402



def _conclude(connection, product, *, catalog, specs, play, play_by_code, decisions, totals):
    """Decide one product's identity and record it.

    Extracted so the main pass and the convergence pass below cannot drift:
    two copies of this decision would be two different rules the day one of
    them is edited.
    """
    identities = connection.execute(
        "SELECT * FROM source_identity_registry WHERE product_id=? ORDER BY source_value", (product["id"],)
    ).fetchall()
    remembered = connection.execute(
        "SELECT conclusion, rule_version FROM identity_conclusions WHERE product_id=?",
        (product["id"],)).fetchone()
    blocked = product['review_state'] == 'rejected' or any(
        i['resolution_state'] == 'rejected' or i['resolution_method'] == 'manual_product_review' for i in identities)
    blocked = blocked or any(d.get('decision') in ('different', 'defer') and
        (d.get('canonical_id') == product['id'] or any(d.get('source_namespace') in (i['namespace'], i['source_id'])
            and d.get('source_value') == i['source_value'] for i in identities)) for d in (decisions or []))
    # A remembered conclusion is normally final: re-deciding an identity
    # every run would make the corpus depend on when it last ran. The one
    # exception is a conclusion that resolved nothing under an OLDER rule
    # version -- there is no decision there to preserve, and a new rule
    # exists precisely to resolve it. An auto_approved or ambiguous
    # conclusion is never reopened, and a human decision (`blocked`)
    # never is either.
    reconsider = (remembered is not None
                  and remembered["conclusion"] == "insufficient_evidence"
                  and remembered["rule_version"] != RULE_VERSION)
    if (remembered and not reconsider) or blocked:
        totals[remembered['conclusion'] if remembered else 'insufficient_evidence'] += 1
        return
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
    # Reverse lookup: this product is NAMED by a hardware code, so ask
    # the catalogue what that code is called. Only a code the catalogue
    # maps to exactly ONE marketing name counts -- "Infinix X603" lists
    # both Zero 5 and Zero 5 Pro, and collapsing those would invent a
    # device relationship the evidence does not support.
    raw_code_names = sorted(play_by_code.get(
        (_play_brand(product["manufacturer"]), _bare_code(product["canonical_name"])), set()))
    # Distinct by FOLDED name, so "NOTE 5" and "Note 5" are one device and not an
    # ambiguity. Not folding this side while folding the code side made Play's
    # own inconsistent spelling look like two phones: 26 of 89 refusals were
    # spelling alone, including six spellings of "itel A58 Lite" for A631W.
    by_key: dict[str, str] = {}
    for candidate in raw_code_names:
        by_key.setdefault(_name_key(candidate), _clean_name(candidate))
    code_names = [by_key[key] for key in sorted(by_key)]
    names_several = any(_NAMES_SEVERAL.search(name) for name in code_names)
    usable = [name for name in code_names
              if _usable_device_name(name, code=product["canonical_name"],
                                     brand=product["manufacturer"])]
    if code_names:
        candidate_names.extend(code_names)
        entry = {"source": "google_play_supported_devices",
                 "matched_on": "model_code",
                 "model_codes": [product["canonical_name"].strip()],
                 "marketing_names": code_names}
        # device_name is what promotion will actually call the device, and it is
        # set only when exactly one USABLE name exists. A code whose only name is
        # the brand ("TECNO" for BF7) or the code back again ("X5010") is still
        # corroborated -- the catalogue confirms the code -- but it names
        # nothing, so the device keeps its code rather than being called TECNO.
        if len(usable) == 1 and not names_several:
            entry["device_name"] = usable[0]
        evidence.append(entry)
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
    elif names_several:
        conclusion, confidence, method = "ambiguous", "medium", "google_play_model_code_names_several_devices"
        rationale = ("The captured Google Play catalog gives this hardware code a name covering "
                     "more than one device; promoting it would answer for both.")
    elif len(code_names) == 1:
        conclusion, confidence, method = "auto_approved", "high", "exact_unique_google_play_model_code"
        rationale = ("The captured Google Play supported-device catalog maps this vendor hardware "
                     "code to exactly one commercial product name.")
    elif len(code_names) > 1:
        conclusion, confidence, method = "ambiguous", "medium", "google_play_model_code_multiple_names"
        rationale = ("The captured Google Play catalog lists this hardware code under several "
                     "commercial names; no silent merge is safe.")
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
      VALUES(?,?,?,?,?,?,?,?,?)""", (product["id"], conclusion, confidence, method, RULE_VERSION, rationale,
      json.dumps(sorted(set(candidate_names))), json.dumps(evidence, sort_keys=True), now))
    if conclusion == "auto_approved":
        connection.execute("UPDATE source_products SET review_state='approved',updated_at=? WHERE id=?", (now, product["id"]))
        connection.execute("UPDATE source_identity_registry SET resolution_state='approved',resolution_method=?,rule_version=?,confidence=? WHERE product_id=?",
                           (method, RULE_VERSION, confidence, product["id"]))
        connection.execute("UPDATE observation_product_links SET link_state='approved' WHERE product_id=?", (product["id"],))
    totals[conclusion] += 1


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
    # The same catalogue indexed the other way round: by HARDWARE CODE rather
    # than marketing name. Transsion products arrive from identity_bridge named
    # by their bare vendor code ("X6962", "CN7c"), never by a marketing name, so
    # the name-keyed lookup above can never match one and every Transsion
    # product concluded insufficient_evidence -- 1,049 of them.
    play_by_code: dict[tuple[str, str], set[str]] = defaultdict(set)
    if google_play_csv and google_play_csv.is_file():
        with google_play_csv.open(encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                brand, name = row["Retail Branding"].strip(), row["Marketing Name"].strip()
                if brand and name:
                    play[(brand.casefold(), _norm(name))].append(row)
                model = row["Model"].strip()
                if brand and name and model:
                    play_by_code[(_play_brand(brand), _bare_code(model))].add(name)
    totals = defaultdict(int)
    rows = connection.execute("SELECT * FROM source_products ORDER BY manufacturer,canonical_name").fetchall()
    with connection:
        for product in rows:
            _conclude(connection, product, catalog=catalog, specs=specs, play=play,
                      play_by_code=play_by_code, decisions=decisions, totals=totals)
    from .product_specs import enrich_product_specs
    enrich_product_specs(connection, specs_csv=specs_csv, devices_yml=devices_yml, google_play_csv=google_play_csv, decisions=decisions)

    # enrich_product_specs CREATES products, for captured specifications that no
    # existing product claims. Those arrive after the loop above has run, so a
    # single pass left them without a conclusion and they were only concluded --
    # and only then promotable -- by the NEXT batch. Measured: run 1 produced 691
    # devices, run 2 produced 693, runs 3 and 4 produced 693. The pipeline
    # converged, but a nightly run should reach its own fixed point rather than
    # leaving a pass of work for tomorrow.
    #
    # Bounded, not `while True`: a rule that somehow created a product per pass
    # would otherwise spin forever on the nightly job. Two extra passes is one
    # more than the one case that exists; if it is ever not enough, the corpus is
    # not converging and that is a bug to see rather than to grind through.
    for _ in range(2):
        pending = connection.execute(
            """SELECT * FROM source_products sp
                WHERE NOT EXISTS (SELECT 1 FROM identity_conclusions ic WHERE ic.product_id = sp.id)
                ORDER BY sp.manufacturer, sp.canonical_name""").fetchall()
        if not pending:
            break
        totals["second_pass_products"] += len(pending)
        with connection:
            for product in pending:
                _conclude(connection, product, catalog=catalog, specs=specs, play=play,
                          play_by_code=play_by_code, decisions=decisions, totals=totals)

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
                region = data.get("region_code") or "SOURCE_UNSPECIFIED"
                if row['source_id'] == 'xiaomi.community.firmware_tracker':
                    from .collectors.adapters.xiaomi_tracker import _region_from_codename_and_name
                    region = _region_from_codename_and_name(data['model_code'],data['source_device_name'])
                android = str(data.get("android") or "").strip() or None
                major = None
                if android:
                    match = re.match(r"^(\d+)", android)
                    major = int(match.group(1)) if match else None
                release_id = _id("product-firmware", row["observation_id"])
                existing = connection.execute('''SELECT id FROM product_firmware_releases
                    WHERE product_id=? AND identity_id=? AND source_id=? AND region_code=?
                      AND build_id=? AND channel=? AND delivery_method IS ? LIMIT 1''',
                    (row['product_id'],row['identity_id'],row['source_id'],region,data['build'],
                     data.get('branch') or 'unknown',data.get('delivery_method'))).fetchone()
                if existing:
                    release_id = existing['id']
                before = connection.total_changes
                connection.execute("""INSERT OR IGNORE INTO product_firmware_releases
                  (id,product_id,identity_id,observation_id,source_id,region_code,build_id,channel,
                   android_version,android_major,vendor_released_at,delivery_method,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (release_id,row["product_id"],row["identity_id"],row["observation_id"],row["source_id"],
                   region,data["build"],data.get("branch") or "unknown",
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
          WHERE android_major IS NOT NULL AND region_code!='SOURCE_UNSPECIFIED'
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


# Must match product_specs.SOURCE. Duplicated (not imported) to avoid a circular
# import: product_specs already imports helpers from this module.
GSMARENA_SPECIFICATIONS_SOURCE = "gsmarena.captured.specifications"


def enrich_gsmarena_hardware_silicon(connection: sqlite3.Connection, specs_csv: Path) -> dict[str, int]:
    """Attach a canonical chipset from GSMArena only on an exact, unambiguous name match.

    Authority order is strict, never averaged: GSMArena is community, the lowest
    tier here. A hardware model that already carries a primary_soc row -- from
    enrich_canonical_silicon's higher-authority cross-reference, or any other
    source -- keeps that row untouched; this never overwrites it and never adds a
    second competing primary_soc row for the same model. Matching is by exact,
    case-insensitive (brand, variant) name only: no fuzzy matching, no promoting a
    source_product identity into a hardware model just to attach a chip. When
    GSMArena's own rows disagree with each other for the same exact name (an
    unresolved spelling/variant difference), or list an unrecognized vendor
    string, nothing is attached. A hardware model with no unique match stays
    without silicon -- that is correct, not a gap to guess at.

    GSMArena also sometimes packs two regional SoC variants into a single
    chipset cell with no separator, e.g. "...Snapdragon 8 Gen 3 (4 nm) -
    USA/Canada/ChinaExynos 2400 (4 nm) - International" for one device page
    (measured: 39 rows in the current capture). Concatenating that string's
    vendor and part would fabricate a chip that does not exist -- worse than
    inventing, since it looks sourced. Any row whose chipset string names more
    than one distinct SoC vendor is treated the same as a conflicting row: skip it.
    """
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with specs_csv.open(encoding="utf-8-sig") as handle:
        for line, row in enumerate(csv.DictReader(handle), 2):
            brand = (row.get("brand") or "").strip()
            device = (row.get("device") or "").strip()
            chipset = (row.get("chipset") or "").strip()
            if brand and device and chipset:
                grouped[(brand.casefold(), device.casefold())].append({**row, "_line": str(line)})

    attached = parts = skipped_conflicting = skipped_existing = skipped_unrecognized_vendor = 0
    with connection:
        models = connection.execute("SELECT hardware_model_id, brand, variant FROM v_device_catalog").fetchall()
        for hw in models:
            rows = grouped.get((hw["brand"].casefold(), hw["variant"].casefold()))
            if not rows:
                continue
            chipsets = {r["chipset"].strip() for r in rows}
            if len(chipsets) != 1:
                skipped_conflicting += 1
                continue
            spec = rows[0]
            found_vendors = {name for token, name in _SOC_VENDOR if token in spec["chipset"].casefold()}
            packed = len(found_vendors) > 1 or len(_PROCESS_ANNOTATION.findall(spec["chipset"])) > 1
            if packed:
                # A single cell naming multiple SoC vendors, or carrying more than
                # one "(N nm)" process annotation, is a packed multi-region
                # description glued into one string (measured: e.g. two same-vendor
                # regional part numbers back to back) -- not one device's chip.
                skipped_conflicting += 1
                continue
            if connection.execute(
                "SELECT 1 FROM hardware_silicon WHERE hardware_model_id=? AND role='primary_soc'",
                (hw["hardware_model_id"],),
            ).fetchone():
                skipped_existing += 1
                continue
            vendor, part, marketing = _soc_parts(spec["chipset"])
            if not vendor:
                skipped_unrecognized_vendor += 1
                continue
            now = (spec.get("fetched_at") or "").strip()
            if not now:
                continue  # No invented observation date.
            url = "https://www.gsmarena.com/" + spec["slug"]
            evidence_id = _capture_evidence(
                connection, source_id=GSMARENA_SPECIFICATIONS_SOURCE,
                source_name="GSMArena captured specifications", source_url=url, path=specs_csv,
                locator="csv:line=" + spec["_line"],
                excerpt=json.dumps({k: v for k, v in spec.items() if not k.startswith("_")}, sort_keys=True),
                now=now)
            vendor_id = _id("vendor", vendor)
            family_id = _id("family", vendor, vendor)
            part_id = _id("part", vendor, part)
            connection.execute("INSERT OR IGNORE INTO silicon_vendors VALUES(?,?,?)", (vendor_id, vendor, now))
            connection.execute("INSERT OR IGNORE INTO silicon_families VALUES(?,?,NULL,?,?)",
                               (family_id, vendor_id, vendor, now))
            before = connection.total_changes
            connection.execute("INSERT OR IGNORE INTO silicon_parts VALUES(?,?,?,?,?,?,?)",
                               (part_id, family_id, part, marketing, None, None, now))
            parts += connection.total_changes > before
            before = connection.total_changes
            connection.execute("INSERT OR IGNORE INTO hardware_silicon VALUES(?,?,NULL,'primary_soc',?,?,NULL)",
                               (hw["hardware_model_id"], part_id, evidence_id, now))
            attached += connection.total_changes > before
        connection.execute("UPDATE sources SET authority_scope='secondary' WHERE id=?",
                           (GSMARENA_SPECIFICATIONS_SOURCE,))
    return {"hardware_silicon_attached": attached, "silicon_parts_created": parts,
            "skipped_conflicting_chipset": skipped_conflicting,
            "skipped_existing_silicon": skipped_existing,
            "skipped_unrecognized_vendor": skipped_unrecognized_vendor}


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
