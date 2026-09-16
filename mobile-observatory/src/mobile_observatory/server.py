from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .database import Database
from .seed import DEMO_TIME, seed_demonstration
from .collection_worker import CollectionWorker, WorkerPaths, migrate_collection_queue

REGION_OPTIONS = {"ILO": "Israel (Samsung CSC)", "MID": "Middle East group", "XSG": "United Arab Emirates / Gulf", "EUX": "Europe multi-CSC", "GLOBAL": "Global", "EEA": "European Economic Area"}
SOURCE_OPTIONS = {"samsung": "Samsung FOTA/OTA", "xiaomi": "Xiaomi firmware tracker", "tecno": "Tecno security", "gsmarena": "GSMArena specifications", "android": "Android bulletins", "qualcomm": "Qualcomm advisories", "mediatek": "MediaTek advisories", "apple": "Apple firmware/security"}


@dataclass(frozen=True)
class QueryPage:
    items: list[dict]
    total: int
    limit: int
    offset: int

    def metadata(self) -> dict:
        following = self.offset + len(self.items)
        return {"returned": len(self.items), "total": self.total, "limit": self.limit,
                "offset": self.offset,
                "nextCursor": str(following) if following < self.total else None}


class ObservatoryService:
    def __init__(self, corpus: Database, local_path: str | Path, *, demonstration: bool,
                 sample_path: str | Path | None = None,
                 legacy_root: str | Path | None = None,
                 fixture_root: str | Path | None = None) -> None:
        self.corpus = corpus
        self.demonstration = demonstration
        self.sample_path = Path(sample_path) if sample_path else None
        self.data_dir = Path(local_path).parent
        project_root = Path(__file__).resolve().parents[2]
        self.worker_paths = WorkerPaths(
            self.data_dir / "ledger",
            Path(legacy_root) if legacy_root else project_root.parent / "crawler" / "relay" / "results",
            Path(fixture_root) if fixture_root else project_root / "fixtures",
        )
        self.local = sqlite3.connect(str(local_path), check_same_thread=False)
        self.local_lock = threading.RLock()
        self.local.row_factory = sqlite3.Row
        self.local.execute("PRAGMA journal_mode = WAL")
        self.local.execute(
            """CREATE TABLE IF NOT EXISTS acknowledgements (
                 event_id TEXT PRIMARY KEY, acknowledged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        self.local.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
        self.local.execute("""CREATE TABLE IF NOT EXISTS collection_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, source TEXT NOT NULL,
          scope TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        migrate_collection_queue(self.local)
        self.local.execute("""CREATE TABLE IF NOT EXISTS identity_decisions (
          source_namespace TEXT NOT NULL, source_value TEXT NOT NULL, canonical_type TEXT NOT NULL,
          canonical_id TEXT, decision TEXT NOT NULL CHECK(decision IN ('same','different','defer')),
          decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, rationale TEXT,
          PRIMARY KEY(source_namespace,source_value,canonical_type,canonical_id))""")
        self.local.commit()

    @property
    def meta(self) -> dict:
        row = self.corpus.connection.execute(
            "SELECT max(observed_at) FROM observations"
        ).fetchone()
        data_as_of = row[0] if row and row[0] else DEMO_TIME
        return {
            "snapshot": f"DEMONSTRATION · {data_as_of}" if self.demonstration else f"IMPORTED SNAPSHOT · {data_as_of}",
            "mode": "demonstration" if self.demonstration else "snapshot",
            "dataAsOf": data_as_of,
        }

    def updates(self, query: dict[str, list[str]]) -> list[dict]:
        return self.updates_page(query).items

    def updates_page(self, query: dict[str, list[str]]) -> QueryPage:
        event_count = self.corpus.connection.execute("SELECT count(*) FROM domain_events").fetchone()[0]
        if event_count:
            q = _first(query, "q").strip()
            clauses = ["1=1"]
            params: list[object] = []
            if q:
                clauses.append("(coalesce(dc.brand,sp.manufacturer) LIKE ? COLLATE NOCASE OR coalesce(dc.variant,sp.canonical_name) LIKE ? COLLATE NOCASE OR dc.model_code LIKE ? COLLATE NOCASE OR de.after_json LIKE ? COLLATE NOCASE)")
                params.extend([f"%{q}%"] * 4)
            for key, column in (("maker", "coalesce(dc.brand,sp.manufacturer)"),
                                ("model", "coalesce(dc.model_code,sir.source_value)"),
                                ("region", "coalesce(ft.target_code,json_extract(de.after_json,'$.region'))")):
                value = _first(query, key).strip()
                if value:
                    clauses.append(f"{column} LIKE ? COLLATE NOCASE")
                    params.append(f"%{value}%")
            where = " AND ".join(clauses)
            joins = """FROM domain_events de
                LEFT JOIN firmware_releases fr ON fr.id=de.subject_id
                LEFT JOIN v_device_catalog dc ON dc.hardware_model_id=fr.hardware_model_id
                LEFT JOIN firmware_targets ft ON ft.id=fr.firmware_target_id
                LEFT JOIN source_products sp ON de.subject_type='source_product' AND sp.id=de.subject_id
                LEFT JOIN source_identity_registry sir ON sir.product_id=sp.id"""
            total = self.corpus.connection.execute(f"SELECT count(DISTINCT de.id) {joins} WHERE {where}", params).fetchone()[0]
            limit, offset = _pagination(query)
            rows = self.corpus.connection.execute(
                f"""SELECT de.id, de.event_type, de.occurred_at, de.before_json, de.after_json,
                           coalesce(dc.brand,sp.manufacturer) maker,
                           coalesce(dc.variant,sp.canonical_name) device,
                           coalesce(dc.model_code,
                             (SELECT source_value FROM source_identity_registry esi
                              WHERE esi.id=json_extract(de.after_json,'$.source_identity')),
                             min(sir.source_value)) model,
                           coalesce(ft.display_name,ft.target_code,json_extract(de.after_json,'$.region'),'Unknown target') region
                    {joins} WHERE {where} GROUP BY de.id ORDER BY de.occurred_at DESC, de.id DESC LIMIT ? OFFSET ?""",
                [*params, limit, offset]).fetchall()
            result = []
            for row in rows:
                before = json.loads(row["before_json"]) if row["before_json"] else {}
                after = json.loads(row["after_json"]) if row["after_json"] else {}
                android_changed = (before.get("android") is not None and
                                   after.get("android") is not None and
                                   str(before["android"]) != str(after["android"]))
                change = ("Android upgrade" if android_changed else
                          row["event_type"].replace("_", " ").title())
                result.append({"id": row["id"], "maker": row["maker"], "device": row["device"],
                    "model": row["model"], "region": row["region"], "age": row["occurred_at"],
                    "buildFrom": before.get("build", "No prior observation"), "buildTo": after.get("build", "Unknown"),
                    "androidFrom": before.get("android") or "Unknown", "androidTo": after.get("android") or "Unknown",
                    "patchFrom": before.get("security_patch") or "Unknown", "patchTo": after.get("security_patch") or "Unknown",
                    "change": change, "importance": "high" if android_changed else "medium", "watched": False})
            return QueryPage(result, total, limit, offset)
        clauses, params = _sql_filters(query, {"maker": "brand", "region": "target_name",
                                               "model": "model_code"},
                                      ("brand", "variant", "model_code", "target_code", "target_name", "build_id"))
        where = " AND ".join(clauses) if clauses else "1=1"
        total = self.corpus.connection.execute(
            f"SELECT count(*) FROM v_device_region_history WHERE {where}", params).fetchone()[0]
        limit, offset = _pagination(query)
        rows = self.corpus.connection.execute(
            f"""SELECT firmware_release_id id, brand maker, variant device,
                       model_code model, coalesce(target_name,target_code) region,
                       build_id build_to, os_major android_to,
                       security_patch_level patch_to
                FROM v_device_region_history WHERE {where}
                ORDER BY first_observed_at DESC, firmware_release_id DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset]).fetchall()
        result = [
            {
                "id": row["id"], "maker": row["maker"], "device": row["device"],
                "model": row["model"], "region": row["region"] or "Unknown target",
                "age": "synthetic snapshot", "buildFrom": "No prior observation",
                "buildTo": row["build_to"], "androidFrom": "Unknown",
                "androidTo": row["android_to"], "patchFrom": "Unknown",
                "patchTo": row["patch_to"], "change": "First observation",
                "importance": "medium", "watched": False,
            }
            for row in rows
        ]
        return QueryPage(result, total, limit, offset)

    def devices(self, query: dict[str, list[str]]) -> list[dict]:
        return self.devices_page(query).items

    def devices_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses, params = _sql_filters(query, {
            "maker": "dc.brand", "vendor": "cd.silicon_vendor", "family": "cd.silicon_family",
            "part": "cd.part_number", "region": "ft.target_code", "model": "dc.model_code"},
            ("dc.brand", "dc.variant", "dc.model_code", "dc.codename", "cd.marketing_name", "cd.part_number"))
        max_android = _first(query, "max_android").strip()
        if max_android:
            try:
                clauses.append("(os.major IS NOT NULL AND os.major <= ?)")
                params.append(int(max_android))
            except ValueError:
                clauses.append("0")
        base = f"""FROM v_device_catalog dc
               LEFT JOIN hardware_silicon hs ON hs.hardware_model_id = dc.hardware_model_id
               LEFT JOIN silicon_parts sp ON sp.id = hs.part_id
               LEFT JOIN v_chip_devices cd ON cd.hardware_model_id = dc.hardware_model_id
                                             AND cd.part_id = sp.id
               LEFT JOIN v_latest_firmware lf ON lf.firmware_release_id = (
                 SELECT lf2.firmware_release_id FROM v_latest_firmware lf2
                 WHERE lf2.hardware_model_id = dc.hardware_model_id
                 ORDER BY coalesce(lf2.vendor_released_at,lf2.first_observed_at) DESC,
                          lf2.firmware_release_id DESC LIMIT 1)
               LEFT JOIN os_releases os ON os.id = lf.os_release_id
               LEFT JOIN firmware_targets ft ON ft.id = lf.firmware_target_id
               WHERE {' AND '.join(clauses) if clauses else '1=1'}"""
        total = self.corpus.connection.execute(
            "SELECT count(DISTINCT dc.hardware_model_id) " + base, params).fetchone()[0]
        limit, offset = _pagination(query)
        sort = _first(query, "sort", "latest_desc")
        order = {"latest_desc": "latest_firmware_at IS NULL,latest_firmware_at DESC,dc.brand,dc.variant,dc.model_code",
                 "name_asc": "dc.brand,dc.variant,dc.model_code",
                 "android_desc": "os.major IS NULL,os.major DESC,dc.brand,dc.variant"}.get(
                     sort, "latest_firmware_at IS NULL,latest_firmware_at DESC,dc.brand,dc.variant,dc.model_code")
        rows = self.corpus.connection.execute(
            """SELECT dc.brand maker, dc.variant name, dc.model_code model,
                      sp.marketing_name chip, sp.part_number part, os.major android,
                      lf.security_patch_level patch, group_concat(DISTINCT ft.target_code) region,
                      (SELECT count(*) FROM firmware_releases history
                       WHERE history.hardware_model_id=dc.hardware_model_id) firmware_count,
                      (SELECT max(coalesce(history.vendor_released_at,history.first_observed_at))
                       FROM firmware_releases history
                       WHERE history.hardware_model_id=dc.hardware_model_id) latest_firmware_at """
            + base + f" GROUP BY dc.hardware_model_id ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
        result = [
            {**dict(row), "android": row["android"] or "Unknown", "patch": row["patch"] or "Unknown",
             "region": row["region"] or "Catalogued; firmware not observed",
             "firmwareCoverage": "observed" if row["firmware_count"] else "not_observed",
             "support": "Supported", "confidence": "Demonstration" if self.demonstration else "Reviewed identity"}
            for row in rows
        ]
        return QueryPage(result, total, limit, offset)

    def chips(self, query: dict[str, list[str]]) -> list[dict]:
        return self.chips_page(query).items

    def chips_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses, params = _sql_filters(query, {"vendor": "vendor", "family": "family", "part": "part"},
                                      ("vendor", "family", "name", "part", "product_names"))
        index = """WITH
          hardware_counts AS (SELECT part_id,count(DISTINCT hardware_model_id) n
            FROM hardware_silicon GROUP BY part_id),
          product_counts AS (SELECT vendor,part_number,count(DISTINCT ops.product_id) n,
            group_concat(DISTINCT prod.canonical_name) names
            FROM observed_product_silicon ops JOIN source_products prod ON prod.id=ops.product_id
            WHERE vendor IS NOT NULL AND part_number IS NOT NULL AND prod.review_state!='rejected' GROUP BY vendor,part_number),
          security_counts AS (SELECT ac.subject_id part_id,count(DISTINCT ac.vulnerability_id) advisories,
            count(DISTINCT CASE WHEN fc.id IS NULL THEN ac.vulnerability_id END) open
            FROM applicability_claims ac LEFT JOIN fix_claims fc ON fc.vulnerability_id=ac.vulnerability_id
            WHERE ac.subject_type='silicon_part' AND ac.relationship='affected' GROUP BY ac.subject_id),
          chip_index AS (
          SELECT sp.id chip_key,sv.canonical_name vendor,sf.canonical_name family,
            sp.marketing_name name,sp.part_number part,
            coalesce(hc.n,0) canonical_devices,coalesce(pc.n,0) product_devices,
            pc.names product_names,coalesce(sc.advisories,0) advisories,coalesce(sc.open,0) open
          FROM silicon_parts sp JOIN silicon_families sf ON sf.id=sp.family_id
          JOIN silicon_vendors sv ON sv.id=sf.vendor_id
          LEFT JOIN hardware_counts hc ON hc.part_id=sp.id
          LEFT JOIN product_counts pc ON pc.vendor=sv.canonical_name COLLATE NOCASE
            AND pc.part_number=sp.part_number COLLATE NOCASE
          LEFT JOIN security_counts sc ON sc.part_id=sp.id
          UNION ALL
          SELECT 'product:'||lower(pc.vendor||':'||pc.part_number),pc.vendor,pc.vendor,
            max(ops.marketing_name),pc.part_number,0,pc.n,pc.names,0,0
          FROM product_counts pc JOIN observed_product_silicon ops
            ON ops.vendor=pc.vendor AND ops.part_number=pc.part_number
          WHERE NOT EXISTS (
            SELECT 1 FROM silicon_parts sp JOIN silicon_families sf ON sf.id=sp.family_id
            JOIN silicon_vendors sv ON sv.id=sf.vendor_id
            WHERE sv.canonical_name=pc.vendor COLLATE NOCASE AND sp.part_number=pc.part_number COLLATE NOCASE)
          GROUP BY pc.vendor,pc.part_number)"""
        where = " AND ".join(clauses) if clauses else "1=1"
        limit, offset = _pagination(query)
        sort = _first(query, "sort", "mobile_desc")
        order = {"mobile_desc": "(canonical_devices+product_devices)>0 DESC,(canonical_devices+product_devices) DESC,advisories DESC,vendor,name,part",
                 "devices_desc": "(canonical_devices+product_devices) DESC,vendor,name,part",
                 "advisories_desc": "advisories DESC,(canonical_devices+product_devices) DESC,vendor,name,part",
                 "name_asc": "vendor,name,part"}.get(sort, "(canonical_devices+product_devices)>0 DESC,(canonical_devices+product_devices) DESC,advisories DESC,vendor,name,part")
        rows = self.corpus.connection.execute(index + f""" SELECT *,canonical_devices+product_devices devices,
          count(*) OVER() _total
          FROM chip_index WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?""", [*params,limit,offset]).fetchall()
        total = rows[0]["_total"] if rows else 0
        return QueryPage([{k: value for k, value in dict(row).items() if k != "_total"}
                          for row in rows], total, limit, offset)

    def chip_products_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses, params = [], []
        for key, column in (('vendor', 'ops.vendor'), ('part', 'ops.part_number')):
            value = _first(query, key).strip()
            if value:
                clauses.append(f'{column}=? COLLATE NOCASE')
                params.append(value)
        where = ' AND '.join(clauses) if clauses else '1=1'
        base = f'''FROM observed_product_silicon ops JOIN source_products sp ON sp.id=ops.product_id
                   WHERE sp.review_state!='rejected' AND {where}'''
        total = self.corpus.connection.execute(f'SELECT count(*) {base}', params).fetchone()[0]
        limit, offset = _pagination(query)
        rows = self.corpus.connection.execute(f'''SELECT sp.id,sp.canonical_name name,sp.manufacturer maker,
            ops.vendor,ops.part_number part,ops.confidence,ops.observed_at,ops.evidence_json
            {base} ORDER BY sp.canonical_name,sp.id LIMIT ? OFFSET ?''', [*params,limit,offset]).fetchall()
        return QueryPage([{**{k:v for k,v in dict(r).items() if k!='evidence_json'},
                           'evidence': json.loads(r['evidence_json'])} for r in rows], total, limit, offset)

    def product_detail(self, product_id: str) -> dict:
        db = self.corpus.connection
        row = db.execute('SELECT * FROM source_products WHERE id=?', (product_id,)).fetchone()
        if row is None:
            raise KeyError(product_id)
        product = dict(row)
        product['specification'] = json.loads(product.pop('specification_json') or 'null')
        identities = [dict(r) for r in db.execute('''SELECT sir.*,s.name source_name,s.base_url source_url
            FROM source_identity_registry sir JOIN sources s ON s.id=sir.source_id
            WHERE product_id=? ORDER BY namespace,source_value''', (product_id,))]
        conclusion = db.execute('SELECT * FROM identity_conclusions WHERE product_id=?', (product_id,)).fetchone()
        conclusion = dict(conclusion) if conclusion else None
        if conclusion:
            conclusion['evidence'] = json.loads(conclusion.pop('evidence_json'))
            conclusion['candidates'] = json.loads(conclusion.pop('candidates_json'))
        silicon = db.execute('SELECT * FROM observed_product_silicon WHERE product_id=?', (product_id,)).fetchone()
        silicon = dict(silicon) if silicon else None
        if silicon:
            silicon['evidence'] = json.loads(silicon.pop('evidence_json'))
        upgrades = []
        for event in db.execute('''SELECT * FROM domain_events WHERE subject_type='source_product'
            AND subject_id=? AND event_type='android_version_changed' ORDER BY occurred_at DESC,id''', (product_id,)):
            upgrades.append({'id': event['id'], 'effective_at': event['occurred_at'],
                'observed_at': event['recorded_at'], 'before': json.loads(event['before_json']),
                'after': json.loads(event['after_json'])})
        regions = [dict(r) for r in db.execute('''SELECT region_code region,channel,count(*) releases
            FROM product_firmware_releases WHERE product_id=? GROUP BY region_code,channel
            ORDER BY region_code,channel''', (product_id,))]
        return {'product': product, 'identities': identities, 'identityConclusion': conclusion,
            'silicon': silicon, 'androidUpgrades': upgrades, 'regions': regions,
            'lastObserved': max([i['last_seen_at'] for i in identities] +
                                ([silicon['observed_at']] if silicon else []) + [product['created_at']]),
            'firmware': _page_payload(self.product_releases_page({'product':[product_id], 'limit':['50']}), self.meta),
            'security': _page_payload(self.product_security_page({'product':[product_id], 'limit':['50']}), self.meta),
            'coverage': {'identity': 'product_only', 'hardware': 'not_established',
                         'securityApplicability': 'not_established'}, 'meta': self.meta}

    def releases(self, query: dict[str, list[str]]) -> list[dict]:
        return self.releases_page(query).items

    def source_records_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses = ["1=1"]
        params: list[object] = []
        q = _first(query, "q").strip()
        source = _first(query, "source").strip()
        kind = _first(query, "kind").strip()
        if q:
            clauses.append("(o.source_key LIKE ? COLLATE NOCASE OR o.payload_json LIKE ? COLLATE NOCASE OR o.source_id LIKE ? COLLATE NOCASE)")
            params.extend([f"%{q}%"] * 3)
        if source:
            clauses.append("o.source_id LIKE ? COLLATE NOCASE")
            params.append(f"%{source}%")
        if kind:
            clauses.append("o.record_type=?")
            params.append(kind)
        where = " AND ".join(clauses)
        total = self.corpus.connection.execute(f"SELECT count(*) FROM observations o WHERE {where}", params).fetchone()[0]
        limit, offset = _pagination(query)
        sort = _first(query, "sort", "latest_desc")
        order = {"latest_desc": "effective_at DESC,o.observed_at DESC",
                 "oldest_asc": "effective_at ASC,o.observed_at ASC",
                 "source_asc": "o.source_id,o.source_key,effective_at DESC",
                 "name_asc": "source_name,device,model_code,o.source_key"}.get(sort, "effective_at DESC,o.observed_at DESC")
        rows = self.corpus.connection.execute(f"""SELECT o.id, o.source_id source, o.record_type kind,
          o.source_key, o.observed_at, o.validation_state,
          json_extract(o.payload_json,'$.data.source_device_name') source_name,
          json_extract(o.payload_json,'$.data.device') device,
          json_extract(o.payload_json,'$.data.model_code') model_code,
          json_extract(o.payload_json,'$.data.region_code') region,
          json_extract(o.payload_json,'$.data.build') build,
          json_extract(o.payload_json,'$.data.android') android,
          json_extract(o.payload_json,'$.data.aspl_month') patch,
          coalesce(json_extract(o.payload_json,'$.data.release_date'),
                   json_extract(o.payload_json,'$.data.publish_date'),
                   json_extract(o.payload_json,'$.data.release_time'),
                   o.observed_at) effective_at,
          json_extract(o.payload_json,'$.data.identity_state') identity_state
          ,json_extract(o.payload_json,'$.data.download_url') download_url,
          coalesce(json_extract(o.payload_json,'$.data.source_url'),a.source_url,s.base_url) source_url
          FROM observations o JOIN artifacts a ON a.id=o.artifact_id JOIN sources s ON s.id=o.source_id WHERE {where}
          ORDER BY {order},o.source_id,o.source_key LIMIT ? OFFSET ?""",
          [*params, limit, offset]).fetchall()
        return QueryPage([dict(row) for row in rows], total, limit, offset)

    def source_products_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses = ["1=1"]
        params: list[object] = []
        for key, expression in (("maker", "sp.manufacturer"), ("state", "sp.review_state")):
            value = _first(query, key).strip()
            if value:
                if key == "region":
                    clauses.append(f"{expression} LIKE ? COLLATE NOCASE"); params.append(f"%{value}%")
                else:
                    clauses.append(f"{expression}=? COLLATE NOCASE"); params.append(value)
        q = _first(query, "q").strip()
        if q:
            clauses.append("(sp.canonical_name LIKE ? COLLATE NOCASE OR sir.source_value LIKE ? COLLATE NOCASE)")
            params.extend([f"%{q}%"] * 2)
        where = " AND ".join(clauses)
        base = f"""FROM source_products sp LEFT JOIN source_identity_registry sir ON sir.product_id=sp.id
                   LEFT JOIN observation_product_links opl ON opl.product_id=sp.id WHERE {where}"""
        total = self.corpus.connection.execute(f"SELECT count(DISTINCT sp.id) {base}", params).fetchone()[0]
        limit, offset = _pagination(query)
        rows = self.corpus.connection.execute(f"""SELECT sp.id,sp.manufacturer maker,sp.canonical_name name,
          sp.review_state, json_extract(sp.specification_json,'$.chipset') chipset,
          json_extract(sp.specification_json,'$.os') launch_os,
          count(DISTINCT sir.id) identities,count(DISTINCT opl.observation_id) observations,
          min(sir.first_seen_at) first_seen,max(sir.last_seen_at) last_seen,
          group_concat(DISTINCT sir.source_value) source_values
          {base} GROUP BY sp.id ORDER BY last_seen DESC,sp.manufacturer,sp.canonical_name LIMIT ? OFFSET ?""",
          [*params, limit, offset]).fetchall()
        return QueryPage([dict(row) for row in rows], total, limit, offset)

    def review_source_product(self, product_id: str, decision: str) -> dict:
        if decision not in ("approved", "rejected", "proposed"):
            raise ValueError("invalid product decision")
        if self.corpus.connection.execute("SELECT 1 FROM source_products WHERE id=?", (product_id,)).fetchone() is None:
            raise KeyError(product_id)
        with self.corpus.connection:
            self.corpus.connection.execute("UPDATE source_products SET review_state=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                           (decision, product_id))
            identity_state = "approved" if decision == "approved" else "rejected" if decision == "rejected" else "proposed"
            self.corpus.connection.execute("UPDATE source_identity_registry SET resolution_state=?,resolution_method='manual_product_review' WHERE product_id=?",
                                           (identity_state, product_id))
            self.corpus.connection.execute("UPDATE observation_product_links SET link_state=? WHERE product_id=?",
                                           ("approved" if decision == "approved" else "proposed", product_id))
        return {"ok": True, "productId": product_id, "decision": decision}

    def releases_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses, params = _sql_filters(query, {"maker": "brand", "region": "target_code",
                                               "model": "model_code", "channel": "channel"},
                                      ("brand", "variant", "model_code", "codename", "target_code", "build_id", "baseband_version"))
        where = " AND ".join(clauses) if clauses else "1=1"
        total = self.corpus.connection.execute(
            f"SELECT count(*) FROM v_device_region_history WHERE {where}", params).fetchone()[0]
        limit, offset = _pagination(query)
        sort = _first(query, "sort", "latest_desc")
        order = {"latest_desc": "coalesce(vendor_released_at,first_observed_at) DESC,variant,target_code,firmware_release_id DESC",
                 "oldest_asc": "coalesce(vendor_released_at,first_observed_at) ASC,variant,target_code,firmware_release_id",
                 "name_asc": "brand,variant,target_code,coalesce(vendor_released_at,first_observed_at) DESC",
                 "android_desc": "os_major IS NULL,os_major DESC,coalesce(vendor_released_at,first_observed_at) DESC"}.get(
                     sort, "coalesce(vendor_released_at,first_observed_at) DESC,variant,target_code,firmware_release_id DESC")
        rows = self.corpus.connection.execute(
            f"""SELECT firmware_release_id id, brand maker, variant device, model_code model,
                       target_code region, build_id build, os_major android,
                       security_patch_level patch, baseband_version baseband,
                       vendor_released_at released, first_observed_at observed, channel,
                       (SELECT max(a.source_url) FROM firmware_release_evidence fre
                        JOIN evidence e ON e.id=fre.evidence_id JOIN artifacts a ON a.id=e.artifact_id
                        WHERE fre.firmware_release_id=v_device_region_history.firmware_release_id) source_url
                FROM v_device_region_history WHERE {where}
                ORDER BY {order} LIMIT ? OFFSET ?""", [*params, limit, offset]).fetchall()
        result = [{**dict(row), "android": row["android"] or "Unknown",
                         "patch": row["patch"] or "Unknown", "baseband": row["baseband"] or "Unknown"}
                        for row in rows]
        return QueryPage(result, total, limit, offset)

    def product_releases_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses = ["1=1"]
        params: list[object] = []
        for key, expression in (("maker", "sp.manufacturer"), ("region", "pfr.region_code"),
                                ("channel", "pfr.channel"), ("product", "sp.id")):
            value = _first(query, key).strip()
            if value:
                clauses.append(f"{expression}=? COLLATE NOCASE"); params.append(value)
        q = _first(query, "q").strip()
        if q:
            clauses.append("(sp.canonical_name LIKE ? COLLATE NOCASE OR pfr.build_id LIKE ? COLLATE NOCASE OR sir.source_value LIKE ? COLLATE NOCASE)")
            params.extend([f"%{q}%"] * 3)
        where = " AND ".join(clauses)
        joins = """FROM product_firmware_releases pfr
          JOIN source_products sp ON sp.id=pfr.product_id
          JOIN source_identity_registry sir ON sir.id=pfr.identity_id
          JOIN observations o ON o.id=pfr.observation_id
          JOIN artifacts ar ON ar.id=o.artifact_id
          LEFT JOIN observed_product_silicon ops ON ops.product_id=sp.id"""
        total = self.corpus.connection.execute(f"SELECT count(*) {joins} WHERE {where}", params).fetchone()[0]
        limit, offset = _pagination(query)
        sort = _first(query, "sort", "released_desc")
        order = {"released_desc": "NULLIF(pfr.vendor_released_at,'null') IS NULL,NULLIF(pfr.vendor_released_at,'null') DESC",
                 "released_asc": "NULLIF(pfr.vendor_released_at,'null') IS NULL,NULLIF(pfr.vendor_released_at,'null') ASC",
                 "product_asc": "sp.canonical_name COLLATE NOCASE ASC",
                 "android_desc": "CAST(pfr.android_version AS INTEGER) DESC"}.get(
                     sort, "NULLIF(pfr.vendor_released_at,'null') IS NULL,NULLIF(pfr.vendor_released_at,'null') DESC")
        rows = self.corpus.connection.execute(f"""SELECT pfr.id,sp.id product_id,
          sp.manufacturer maker,sp.canonical_name device,sir.source_value source_identity,
          pfr.region_code region,pfr.build_id build,pfr.channel,pfr.android_version android,
          pfr.vendor_released_at released,pfr.delivery_method,pfr.source_id source,
          json_extract(o.payload_json,'$.data.security_patch_level') security_patch_level,
          json_extract(o.payload_json,'$.data.release_scope') release_scope,
          json_extract(o.payload_json,'$.data.comments') vendor_comments,
          json_extract(o.payload_json,'$.data.download_url') download_url,
          ar.source_url source_url,o.observed_at observed,
          coalesce(json_extract(ops.evidence_json,'$[0].slug'),json_extract(sp.specification_json,'$.slug')) spec_slug
          {joins} WHERE {where}
          ORDER BY {order},pfr.id DESC LIMIT ? OFFSET ?""",
          [*params,limit,offset]).fetchall()
        return QueryPage([dict(row) for row in rows],total,limit,offset)

    def product_security_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses = ["1=1"]
        params: list[object] = []
        product_id = _first(query, "product").strip()
        if product_id:
            clauses.append("sp.id=?"); params.append(product_id)
        maker = _first(query, "maker").strip()
        if maker:
            clauses.append("sp.manufacturer=? COLLATE NOCASE"); params.append(maker)
        q = _first(query, "q").strip()
        if q:
            clauses.append("(sp.canonical_name LIKE ? COLLATE NOCASE OR psp.security_patch_month LIKE ? COLLATE NOCASE)")
            params.extend([f"%{q}%"] * 2)
        where = " AND ".join(clauses)
        joins = """FROM product_security_publications psp
          JOIN source_products sp ON sp.id=psp.product_id
          JOIN source_identity_registry sir ON sir.id=psp.identity_id
          JOIN observations o ON o.id=psp.observation_id
          JOIN artifacts ar ON ar.id=o.artifact_id
          LEFT JOIN observed_product_silicon ops ON ops.product_id=sp.id"""
        total = self.corpus.connection.execute(f"SELECT count(*) {joins} WHERE {where}",params).fetchone()[0]
        limit,offset = _pagination(query)
        sort = _first(query, "sort", "released_desc")
        order = {"released_desc": "NULLIF(psp.published_at,'null') IS NULL,NULLIF(psp.published_at,'null') DESC,psp.security_patch_month DESC",
                 "released_asc": "NULLIF(psp.published_at,'null') IS NULL,NULLIF(psp.published_at,'null') ASC,psp.security_patch_month ASC",
                 "product_asc": "sp.canonical_name COLLATE NOCASE ASC"}.get(
                     sort, "NULLIF(psp.published_at,'null') IS NULL,NULLIF(psp.published_at,'null') DESC,psp.security_patch_month DESC")
        rows = self.corpus.connection.execute(f"""SELECT psp.id,sp.id product_id,
          sp.manufacturer maker,sp.canonical_name device,sir.source_value source_identity,
          psp.security_patch_month patch,psp.published_at,psp.title,psp.source_id source,
          json_extract(o.payload_json,'$.data.security_patch_level') security_patch_level,
          json_extract(o.payload_json,'$.data.release_date') first_live_release,
          json_extract(o.payload_json,'$.data.release_scope') release_scope,
          coalesce(json_extract(o.payload_json,'$.data.source_url'),ar.source_url) source_url,
          coalesce(json_extract(ops.evidence_json,'$[0].slug'),json_extract(sp.specification_json,'$.slug')) spec_slug
          {joins} WHERE {where} ORDER BY {order},psp.id DESC LIMIT ? OFFSET ?""",
          [*params,limit,offset]).fetchall()
        return QueryPage([dict(row) for row in rows],total,limit,offset)

    def security_page(self, query: dict[str, list[str]]) -> QueryPage:
        q = _first(query, "q").strip()
        clauses = ["1=1"]
        params: list[object] = []
        if q:
            clauses.append("(v.cve_id LIKE ? COLLATE NOCASE OR v.summary LIKE ? COLLATE NOCASE OR a.title LIKE ? COLLATE NOCASE OR s.name LIKE ? COLLATE NOCASE)")
            params.extend([f"%{q}%"] * 4)
        where = " AND ".join(clauses)
        joins = """FROM vulnerabilities v JOIN advisory_vulnerabilities av ON av.vulnerability_id=v.id
          JOIN advisories a ON a.id=av.advisory_id JOIN sources s ON s.id=a.source_id
          LEFT JOIN evidence ae ON ae.id=a.evidence_id LEFT JOIN artifacts aa ON aa.id=ae.artifact_id"""
        total = self.corpus.connection.execute(f"SELECT count(DISTINCT v.id) {joins} WHERE {where}", params).fetchone()[0]
        limit, offset = _pagination(query)
        rows = self.corpus.connection.execute(f"""SELECT v.id vulnerability_id,v.cve_id cve,min(a.title) bulletin,
          coalesce(v.published_at,max(a.published_at)) published_at,
          coalesce(v.summary,'Component not specified') component,
          group_concat(DISTINCT s.name) evidence,
          group_concat(DISTINCT coalesce(aa.source_url,s.base_url)) source_urls,
          (SELECT count(*) FROM applicability_claims ac WHERE ac.vulnerability_id=v.id) claim_count,
          (SELECT count(DISTINCT hs.hardware_model_id) FROM applicability_claims ac
             JOIN hardware_silicon hs ON ac.subject_type='silicon_part' AND hs.part_id=ac.subject_id
             WHERE ac.vulnerability_id=v.id AND ac.relationship='affected') device_count,
          (SELECT group_concat(DISTINCT sp.part_number) FROM applicability_claims ac
             JOIN silicon_parts sp ON ac.subject_type='silicon_part' AND sp.id=ac.subject_id
             WHERE ac.vulnerability_id=v.id AND ac.relationship='affected') affected_parts,
          (SELECT count(*) FROM fix_claims fc WHERE fc.vulnerability_id=v.id) fix_count {joins} WHERE {where}
          GROUP BY v.id ORDER BY published_at DESC,v.cve_id DESC LIMIT ? OFFSET ?""", [*params, limit, offset]).fetchall()
        result = [{**dict(row), "severity": "Unscored", "score": "—",
                   "cve_url": f"https://nvd.nist.gov/vuln/detail/{row['cve']}",
                   "chip": row["affected_parts"] or "Applicability unresolved",
                   "devices": row["device_count"],
                   "state": ("Part applicability + fix coordinate" if row["affected_parts"] and row["fix_count"]
                             else "Part applicability" if row["affected_parts"]
                             else "Component applicability" if row["claim_count"]
                             else "Catalog only"),
                   "reasoning": ("Exact affected silicon part and a fix coordinate are both linked."
                                 if row["affected_parts"] and row["fix_count"] else
                                 "The vendor maps this CVE to an exact silicon part, but no fix coordinate is linked."
                                 if row["affected_parts"] else
                                 "An applicability claim exists, but it does not identify an exact silicon part."
                                 if row["claim_count"] else
                                 "The CVE appears in a captured bulletin only; device impact is not established.")} for row in rows]
        return QueryPage(result, total, limit, offset)

    def security_coverage(self) -> dict:
        rows = self.corpus.connection.execute(
            "SELECT vendor,capability,status,reason,evidence_uri,checked_at FROM security_coverage_gaps ORDER BY vendor"
        ).fetchall()
        return {"items": [dict(row) for row in rows], "meta": self.meta}

    def security(self, query: dict[str, list[str]]) -> list[dict]:
        return self.security_page(query).items

    def agent_review_bundle(self) -> dict:
        root = self.data_dir / "agent-review"
        prompt = root / "agent-review-prompt.md"
        candidates = root / "identity-candidates.json"
        if not prompt.is_file() or not candidates.is_file():
            return {"candidateCount": 0, "pastePrompt": "", "candidates": []}
        candidate_data = json.loads(candidates.read_text(encoding="utf-8"))
        prompt_text = prompt.read_text(encoding="utf-8")
        paste = prompt_text + "\n\n# Candidate data\n```json\n" + json.dumps(candidate_data, indent=2) + "\n```\n"
        return {"candidateCount": len(candidate_data), "pastePrompt": paste, "candidates": candidate_data}

    def health(self) -> list[dict]:
        rows = self.corpus.connection.execute("""SELECT s.name source, s.authority_scope scope,
          ir.outcome status, ir.finished_at last, ir.accepted_count records
          FROM ingestion_runs ir JOIN sources s ON s.id=ir.source_id
          WHERE ir.started_at=(SELECT max(ir2.started_at) FROM ingestion_runs ir2 WHERE ir2.source_id=ir.source_id)
          ORDER BY s.name""").fetchall()
        if rows:
            return [{**dict(row), "next": "According to configured cadence"} for row in rows]
        return [{"source": "Synthetic demonstration fixture", "scope": "Sample only", "status": "Demo",
                 "last": DEMO_TIME, "next": "No collection scheduled", "records": "0"}]

    def overview(self) -> dict:
        with self.local_lock:
            acknowledged = {row[0] for row in self.local.execute("SELECT event_id FROM acknowledgements").fetchall()}
        event_ids = {row[0] for row in self.corpus.connection.execute("SELECT id FROM domain_events")}
        if not event_ids:
            event_ids = {row[0] for row in self.corpus.connection.execute("SELECT id FROM firmware_releases")}
        failures = self.corpus.connection.execute("SELECT count(*) FROM ingestion_runs WHERE outcome!='succeeded'").fetchone()[0]
        android_upgrades = self.corpus.connection.execute(
            """SELECT count(*) FROM domain_events
               WHERE event_type='android_version_changed'
                  OR (json_extract(before_json,'$.android') IS NOT NULL
                      AND json_extract(after_json,'$.android') IS NOT NULL
                      AND CAST(json_extract(before_json,'$.android') AS TEXT)
                          <> CAST(json_extract(after_json,'$.android') AS TEXT))"""
        ).fetchone()[0]
        return {**self.meta, "meta": self.meta, "unseen": len(event_ids - acknowledged), "androidUpgrades": android_upgrades,
                "securityPatches": self.corpus.connection.execute("SELECT count(*) FROM observations WHERE record_type='security_patch_publication'").fetchone()[0],
                "sourceWarnings": failures, "lastRun": self.meta["dataAsOf"]}

    def acknowledge(self, event_id: str) -> None:
        found = self.corpus.connection.execute("SELECT 1 FROM domain_events WHERE id = ?", (event_id,)).fetchone()
        if found is None and self.corpus.connection.execute("SELECT count(*) FROM domain_events").fetchone()[0] == 0:
            found = self.corpus.connection.execute("SELECT 1 FROM firmware_releases WHERE id = ?", (event_id,)).fetchone()
        if found is None:
            raise KeyError(event_id)
        with self.local_lock:
            self.local.execute(
                "INSERT OR REPLACE INTO acknowledgements(event_id) VALUES (?)", (event_id,)
            )
            self.local.commit()

    def acknowledge_many(self, event_ids: list[str]) -> dict:
        ids = list(dict.fromkeys(str(item).strip() for item in event_ids if str(item).strip()))
        if len(ids) > 5000:
            raise ValueError("too many update ids")
        valid = {row[0] for row in self.corpus.connection.execute("SELECT id FROM domain_events")}
        if not valid:
            valid = {row[0] for row in self.corpus.connection.execute("SELECT id FROM firmware_releases")}
        missing = [item for item in ids if item not in valid]
        if missing:
            raise KeyError(missing[0])
        with self.local_lock:
            self.local.executemany(
                "INSERT OR REPLACE INTO acknowledgements(event_id) VALUES (?)",
                [(item,) for item in ids],
            )
            self.local.commit()
        return {"acknowledged": len(ids)}

    def acknowledgements(self) -> list[str]:
        with self.local_lock:
            return [row[0] for row in self.local.execute(
                "SELECT event_id FROM acknowledgements ORDER BY acknowledged_at DESC").fetchall()]

    def search(self, query: dict[str, list[str]]) -> list[dict]:
        q = _first(query, "q", "")
        device_results = [
            {"type": "device", "id": row["model"], "label": row["name"], "detail": row["model"]}
            for row in _filter(self.devices({}), {"q": [q]})
        ]
        chip_results = [
            {"type": "chip", "id": row["part"], "label": row["name"], "detail": row["part"]}
            for row in _filter(self.chips({}), {"q": [q]})
        ]
        release_results = [
            {"type": "release", "id": row["id"], "label": row["build"],
             "detail": f"{row['device']} · {row['model']} · {row['region']}"}
            for row in _filter(self.releases({}), {"q": [q]})[:8]
        ]
        return (device_results + chip_results + release_results)[:16]

    def config(self) -> dict:
        defaults = {"cadenceHours": 6, "preferredRegions": ["ILO", "MID", "GLOBAL"],
                    "enabledSources": ["samsung", "xiaomi", "tecno"], "supportedOnly": True}
        row = self.local.execute("SELECT value_json FROM settings WHERE key='operator_config'").fetchone()
        return defaults if row is None else {**defaults, **json.loads(row[0])}

    def config_options(self) -> dict:
        return {"regions": [{"id": key, "label": value} for key, value in REGION_OPTIONS.items()],
                "sources": [{"id": key, "label": value} for key, value in SOURCE_OPTIONS.items()]}

    def save_config(self, value: dict) -> dict:
        regions = [str(x) for x in value.get("preferredRegions", [])]
        sources = [str(x) for x in value.get("enabledSources", [])]
        if any(x not in REGION_OPTIONS for x in regions) or any(x not in SOURCE_OPTIONS for x in sources):
            raise ValueError("unknown region or source")
        clean = {
            "cadenceHours": max(1, min(24, int(value.get("cadenceHours", 6)))),
            "preferredRegions": regions,
            "enabledSources": sources,
            "supportedOnly": bool(value.get("supportedOnly", True)),
        }
        self.local.execute("INSERT OR REPLACE INTO settings VALUES('operator_config',?)",
                           (json.dumps(clean, sort_keys=True),))
        self.local.commit()
        return clean

    def real_sample(self) -> dict:
        if not self.sample_path or not self.sample_path.is_file():
            return {"items": [], "notice": "No real-source sample installed."}
        return json.loads(self.sample_path.read_text(encoding="utf-8"))

    def review_profiles(self) -> dict:
        root = self.sample_path.parent if self.sample_path else None
        if not root:
            return {"profiles": []}
        profiles: list[dict] = []
        original = root / "review_profiles.json"
        if original.is_file():
            profiles.extend(json.loads(original.read_text(encoding="utf-8")).get("profiles", []))
        samsung = root / "samsung" / "reviewed_profiles.json"
        if samsung.is_file():
            for item in json.loads(samsung.read_text(encoding="utf-8")).get("profiles", []):
                profiles.append({"id": f"samsung:{item['model_code']}", "name": item["commercial_name"],
                    "tier": item["tier"], "source_identity": [item["model_code"], *item.get("regions", [])],
                    "chipset": item.get("chip", {}).get("name", "Not asserted in reviewed profile"),
                    "cpu": None, "gpu": None, "launch_os": f"Android {item.get('android', 'Unknown')}",
                    "display": None, "battery": None, "memory": None, "wifi": None, "bluetooth": None,
                    "spec_observed_at": self.meta["dataAsOf"], "radio": {}, "review_state": item["identity_state"],
                    "firmware": [{"name": region, "codename": item["model_code"], "version": build,
                                  "android": item.get("android"), "date": "captured corpus"}
                                 for region, build in item.get("latest_captured", {}).items()]})
        mixed = root / "xiaomi_tecno_review_profiles.json"
        if mixed.is_file():
            for item in json.loads(mixed.read_text(encoding="utf-8")).get("profiles", []):
                spec = item.get("specification", {})
                profiles.append({"id": item["id"], "name": item["name"], "tier": item["tier"],
                    "source_identity": item.get("source_identities", []), "chipset": spec.get("chipset", "Unknown"),
                    "cpu": spec.get("cpu"), "gpu": spec.get("gpu"), "launch_os": spec.get("launch_os"),
                    "display": spec.get("display"), "battery": spec.get("battery"), "memory": spec.get("memory"),
                    "wifi": spec.get("wifi"), "bluetooth": spec.get("bluetooth"), "spec_observed_at": spec.get("observed_at"),
                    "radio": {"4g": spec.get("4g"), "5g": spec.get("5g")},
                    "review_state": item.get("review_state", "needs identity review"),
                    "firmware": item.get("firmware_history", [])})
        unique = {item["id"]: item for item in profiles}
        return {"profiles": list(unique.values())}

    def identity_decisions(self) -> list[dict]:
        return [dict(row) for row in self.local.execute(
            "SELECT * FROM identity_decisions ORDER BY decided_at DESC")]

    def save_identity_decision(self, value: dict) -> dict:
        required = ("sourceNamespace", "sourceValue", "canonicalType", "decision")
        if any(not str(value.get(k, "")).strip() for k in required):
            raise ValueError("missing identity decision fields")
        if value["decision"] not in ("same", "different", "defer"):
            raise ValueError("invalid decision")
        self.local.execute("""INSERT OR REPLACE INTO identity_decisions
          (source_namespace,source_value,canonical_type,canonical_id,decision,rationale)
          VALUES(?,?,?,?,?,?)""", (value["sourceNamespace"], value["sourceValue"],
          value["canonicalType"], value.get("canonicalId"), value["decision"], value.get("rationale")))
        self.local.commit()
        return {"ok": True, "decision": value["decision"]}

    def collection_requests(self) -> list[dict]:
        worker = CollectionWorker(self.local, self.corpus.connection, self.worker_paths)
        return [worker.get(row[0]) for row in self.local.execute(
            "SELECT id FROM collection_requests ORDER BY id DESC LIMIT 50")]

    def request_collection(self, value: dict) -> dict:
        target = str(value.get("target", "")).strip()
        source = str(value.get("source", "")).strip()
        scope = str(value.get("scope", "latest_firmware")).strip()
        if not target or source not in SOURCE_OPTIONS or scope not in (
                "latest_firmware", "firmware_history", "device_profile", "security"):
            raise ValueError("invalid collection request")
        cursor = self.local.execute(
            "INSERT INTO collection_requests(target,source,scope) VALUES(?,?,?)",
            (target[:200], source, scope))
        self.local.commit()
        return {"id": cursor.lastrowid, "target": target[:200], "source": source,
                "scope": scope, "status": "queued", "execution_mode": "captured_replay",
                "live_network": False}

    def process_collection_request(self, request_id: int) -> dict:
        return CollectionWorker(self.local, self.corpus.connection, self.worker_paths).process(request_id)

    def process_next_collection_request(self) -> dict:
        item = CollectionWorker(self.local, self.corpus.connection, self.worker_paths).process_next()
        return item or {"status": "idle", "message": "No queued collection request."}


def make_handler(service: ObservatoryService, web_root: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            routes = {
                "/api/v1/radar/overview": service.overview,
                "/api/v1/updates": lambda: _page_payload(service.updates_page(query), service.meta),
                "/api/v1/updates/acknowledgements": lambda: {"items": service.acknowledgements()},
                "/api/v1/devices": lambda: _page_payload(service.devices_page(query), service.meta),
                "/api/v1/chips/products": lambda: _page_payload(service.chip_products_page(query), service.meta),
                "/api/v1/chips": lambda: _page_payload(service.chips_page(query), service.meta),
                "/api/v1/releases": lambda: _page_payload(service.releases_page(query), service.meta),
                "/api/v1/product-releases": lambda: _page_payload(service.product_releases_page(query), service.meta),
                "/api/v1/product-security": lambda: _page_payload(service.product_security_page(query), service.meta),
                "/api/v1/source-records": lambda: _page_payload(service.source_records_page(query), service.meta),
                "/api/v1/identity/products": lambda: _page_payload(service.source_products_page(query), service.meta),
                "/api/v1/security/findings": lambda: _page_payload(service.security_page(query), service.meta),
                "/api/v1/security/coverage": service.security_coverage,
                "/api/v1/admin/health": lambda: {"items": service.health(), "meta": service.meta},
                "/api/v1/search": lambda: {"items": service.search(query), "meta": service.meta},
                "/api/v1/admin/config": service.config,
                "/api/v1/admin/options": service.config_options,
                "/api/v1/admin/real-sample": service.real_sample,
                "/api/v1/admin/review-profiles": service.review_profiles,
                "/api/v1/identity/decisions": lambda: {"items": service.identity_decisions()},
                "/api/v1/identity/agent-bundle": service.agent_review_bundle,
                "/api/v1/admin/collection-requests": lambda: {"items": service.collection_requests()},
            }
            if parsed.path.startswith('/api/v1/products/'):
                try:
                    self._json(HTTPStatus.OK, service.product_detail(unquote(parsed.path[len('/api/v1/products/'):])) )
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {'error': 'product_not_found'})
                return
            if parsed.path in routes:
                self._json(HTTPStatus.OK, routes[parsed.path]())
                return
            self._static(parsed.path)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/v1/updates/acknowledge-bulk":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    self._json(HTTPStatus.OK, service.acknowledge_many(payload.get("ids", [])))
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "update_not_found"})
                except (ValueError, TypeError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_update_ids"})
                return
            if parsed.path == "/api/v1/admin/config":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    self._json(HTTPStatus.OK, service.save_config(payload))
                except (ValueError, TypeError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_config"})
                return
            if parsed.path == "/api/v1/identity/decisions":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    self._json(HTTPStatus.OK, service.save_identity_decision(payload))
                except (ValueError, TypeError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_identity_decision"})
                return
            if parsed.path == "/api/v1/admin/collection-requests":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    self._json(HTTPStatus.CREATED, service.request_collection(payload))
                except (ValueError, TypeError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_collection_request"})
                return
            if parsed.path == "/api/v1/admin/collection-requests/process-next":
                try:
                    self._json(HTTPStatus.OK, service.process_next_collection_request())
                except (ValueError, OSError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": "collection_failed", "detail": str(exc)})
                return
            request_prefix, process_suffix = "/api/v1/admin/collection-requests/", "/process"
            if parsed.path.startswith(request_prefix) and parsed.path.endswith(process_suffix):
                raw_id = parsed.path[len(request_prefix):-len(process_suffix)]
                try:
                    self._json(HTTPStatus.OK, service.process_collection_request(int(raw_id)))
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "collection_request_not_found"})
                except (ValueError, OSError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": "collection_request_conflict", "detail": str(exc)})
                return
            product_prefix = "/api/v1/identity/products/"
            if parsed.path.startswith(product_prefix) and parsed.path.endswith("/review"):
                product_id = unquote(parsed.path[len(product_prefix):-len("/review")])
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    self._json(HTTPStatus.OK, service.review_source_product(product_id, payload.get("decision", "")))
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "product_not_found"})
                except (ValueError, TypeError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_product_decision"})
                return
            prefix, suffix = "/api/v1/updates/", "/acknowledge"
            if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
                event_id = unquote(parsed.path[len(prefix):-len(suffix)])
                try:
                    service.acknowledge(event_id)
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "update_not_found"})
                    return
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def _json(self, status: HTTPStatus, payload) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Observatory-Data-Mode", service.meta["mode"])
            self.end_headers()
            self.wfile.write(body)

        def _static(self, raw_path: str) -> None:
            relative = "index.html" if raw_path == "/" else raw_path.lstrip("/")
            target = (web_root / relative).resolve()
            if web_root.resolve() not in target.parents and target != web_root.resolve():
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not target.is_file():
                target = web_root / "index.html"
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            return

    return Handler


def _first(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def _pagination(query: dict[str, list[str]]) -> tuple[int, int]:
    try:
        limit = max(1, min(200, int(_first(query, "limit", "50"))))
    except ValueError:
        limit = 50
    raw_offset = _first(query, "cursor", _first(query, "offset", "0"))
    try:
        offset = max(0, int(raw_offset))
    except ValueError:
        offset = 0
    return limit, offset


def _sql_filters(query: dict[str, list[str]], fields: dict[str, str],
                 searchable: tuple[str, ...]) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    q = _first(query, "q").strip()
    if q:
        clauses.append("(" + " OR ".join(f"{column} LIKE ? COLLATE NOCASE" for column in searchable) + ")")
        params.extend([f"%{q}%"] * len(searchable))
    for key, column in fields.items():
        value = _first(query, key).strip()
        if value:
            clauses.append(f"{column} LIKE ? COLLATE NOCASE")
            params.append(f"%{value}%")
    return clauses, params


def _page_payload(page: QueryPage, meta: dict) -> dict:
    return {"items": page.items, "meta": {**meta, "page": page.metadata()},
            "coverage": {"status": "partial", "reason": "snapshot_scope"},
            "data_as_of": meta["dataAsOf"]}


def _filter(rows: list[dict], query: dict[str, list[str]]) -> list[dict]:
    q = _first(query, "q").casefold().strip()
    if q:
        rows = [row for row in rows if q in " ".join(str(v) for v in row.values()).casefold()]
    for key in ("maker", "vendor", "part", "region", "support"):
        value = _first(query, key).casefold().strip()
        if value:
            rows = [row for row in rows if value in str(row.get(key, "")).casefold()]
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Mobile Observatory locally")
    parser.add_argument("--demo", action="store_true", help="seed and serve explicitly synthetic fixtures")
    parser.add_argument("--data-dir", default=".observatory-data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--legacy-root", help="captured legacy artifact root used by the manual replay worker")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = data_dir / "corpus.sqlite"
    corpus = (
        Database.migrated(corpus_path, check_same_thread=False)
        if not corpus_path.exists()
        else Database(corpus_path, check_same_thread=False)
    )
    corpus.apply_migrations()
    if args.demo:
        seed_demonstration(corpus, root / "fixtures" / "supported_catalog.sample.json")
    elif corpus.connection.execute("SELECT count(*) FROM sources").fetchone()[0] == 0:
        parser.error("empty corpus: pass --demo for synthetic data or provide an ingested corpus")
    service = ObservatoryService(corpus, data_dir / "local.sqlite", demonstration=args.demo,
                                 sample_path=root / "fixtures" / "real_source_sample.json",
                                 legacy_root=args.legacy_root)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(service, root / "apps" / "web"))
    print(f"Mobile Observatory: http://{args.host}:{args.port} ({service.meta['mode']})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.local.close()
        corpus.close()


if __name__ == "__main__":
    main()
