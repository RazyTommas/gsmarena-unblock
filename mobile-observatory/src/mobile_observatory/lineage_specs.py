"""Captured LineageOS wiki assertions, deliberately below canonical hardware facts.

LineageOS describes custom-ROM targets. Its versions and maintenance state are
never imported as stock Android, manufacturer support, firmware or security facts.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .enrichment import _capture_evidence, _id, _soc_parts
from .product_specs import name_key

SOURCE = 'lineageos.captured.specifications'


def read_capture(path: Path) -> tuple[dict, list[tuple[dict, Path]]]:
    """Validate the complete capture before any database changes."""
    capture = json.loads(path.read_text(encoding='utf-8'))
    if capture.get('format_version') != 1 or not re.fullmatch(r'[a-f0-9]{40}', capture.get('commit', '')):
        raise ValueError('Unsupported capture format or missing immutable source commit')
    rows, keys = [], set()
    root = path.parent.resolve()
    for row in capture['records']:
        if not all(isinstance(row.get(k), str) and row[k].strip() for k in
                   ('key', 'vendor', 'name', 'codename', 'soc', 'source_url', 'raw_path', 'sha256', 'observed_at')):
            raise ValueError('Missing specification identity, evidence or observation time')
        if row['key'] in keys or not re.fullmatch(r'[A-Za-z0-9_-]+', row['key']):
            raise ValueError('Duplicate or invalid source key')
        keys.add(row['key'])
        expected = f"https://raw.githubusercontent.com/LineageOS/lineage_wiki/{capture['commit']}/_data/devices/{row['key']}.yml"
        if row['source_url'] != expected:
            raise ValueError('Source URL must identify the captured commit and exact YAML')
        raw = (root / row['raw_path']).resolve()
        if not raw.is_relative_to(root) or hashlib.sha256(raw.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('Captured artifact is missing, outside the capture, or changed')
        if not isinstance(row.get('models'), list) or any(not isinstance(m, str) or not m.strip() for m in row['models']):
            raise ValueError('Model identifiers must be an explicit string list')
        rows.append((row, raw))
    return capture, rows


def enrich_lineage_specs(connection: sqlite3.Connection, capture_path: Path,
                         decisions: list[dict] | None = None) -> dict[str, int]:
    capture, rows = read_capture(capture_path)
    counts = Counter(captured_rows=len(rows))
    codename_counts = Counter((r['vendor'].casefold(), r['codename']) for r, _ in rows)
    name_counts = Counter((r['vendor'].casefold(), name_key(r['name'], r['vendor'])) for r, _ in rows)
    products = [dict(p) for p in connection.execute('SELECT * FROM source_products')]
    identities = [dict(i) for i in connection.execute('SELECT * FROM source_identity_registry')]
    database_file = connection.execute('PRAGMA database_list').fetchone()[2]
    if not connection.in_transaction:
        connection.execute('BEGIN IMMEDIATE')
    with connection:
        for row, raw in rows:
            now, maker, key = row['observed_at'], row['vendor'], row['key']
            existing_identity = next((i for i in identities if i['source_id'] == SOURCE and i['source_value'] == key), None)
            candidates = set()
            if codename_counts[(maker.casefold(), row['codename'])] == 1:
                for identity in identities:
                    if identity['namespace'] == 'codename' and identity['source_value'] == row['codename']:
                        candidates.update(p['id'] for p in products if p['id'] == identity['product_id'] and p['manufacturer'].casefold() == maker.casefold())
            nkey = name_key(row['name'], maker)
            if name_counts[(maker.casefold(), nkey)] == 1:
                candidates.update(p['id'] for p in products if p['manufacturer'].casefold() == maker.casefold()
                                  and name_key(p['canonical_name'], maker) == nkey)
            if existing_identity:
                candidates = {existing_identity['product_id']}
            blocked = any(p['id'] in candidates and p['review_state'] != 'approved' for p in products)
            blocked = blocked or any(i['product_id'] in candidates and (i['resolution_state'] != 'approved' or
                i['resolution_method'] == 'manual_product_review') for i in identities)
            blocked = blocked or any(d.get('decision') in ('different', 'defer') and
                (d.get('canonical_id') in candidates or d.get('source_namespace') == SOURCE and d.get('source_value') == key)
                for d in (decisions or []))
            pid = next(iter(candidates)) if len(candidates) == 1 else None
            method = 'exact_unique_product_identity' if pid else 'standalone_source_target'
            if blocked:
                pid, method = None, 'blocked_by_review'
            elif len(candidates) > 1:
                pid, method = None, 'ambiguous_product_identity'
            prior = connection.execute('SELECT * FROM observed_product_silicon WHERE product_id=?', (pid,)).fetchone() if pid else None
            if prior and prior['raw_chipset'] != row['soc']:
                # A spelling difference is still unresolved; do not invent a chip alias.
                pid, method = None, 'conflicting_or_unresolved_chipset'
            if database_file:
                target = Path(database_file).parent / 'evidence' / (row['sha256'] + '.yml')
                target.parent.mkdir(exist_ok=True)
                if not target.exists():
                    target.write_bytes(raw.read_bytes())
                raw = target
            eid = _capture_evidence(connection, source_id=SOURCE, source_name='LineageOS captured device specifications',
                source_url=row['source_url'], path=raw, locator='yaml:device', excerpt=json.dumps(row, sort_keys=True), now=now)
            if not candidates and not blocked:
                pid = _id('lineageos-product', maker, key)
                # Scope the identity by the actual wiki target, retaining every variant.
                connection.execute('INSERT OR IGNORE INTO source_products VALUES(?,?,?,?,?,?,?,?)',
                    (pid, maker, row['name'], 'lineageos:' + key, 'approved', json.dumps({'chipset':row['soc'], 'source':SOURCE}), now, now))
                counts['standalone_products'] += 1
            if pid:
                connection.execute('INSERT OR IGNORE INTO source_identity_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (_id('lineageos-identity', key), SOURCE, 'lineageos_target', key, key, pid,
                     'approved', method, '1', 'medium', now, now))
                vendor, part, marketing = _soc_parts(row['soc'])
                proof = dict(source='lineageos_wiki', source_url=row['source_url'], source_commit=capture['commit'],
                    device_name=row['name'], chipset=row['soc'], models=row['models'], codename=row['codename'],
                    artifact_sha256=row['sha256'], locator='yaml:device', evidence_id=eid, observed_at=now,
                    match_method=method, confidence_boundary='Community specification; no canonical hardware, stock Android, OEM support or security applicability claim',
                    license=capture.get('license'), attribution=capture.get('attribution'))
                if not prior:
                    connection.execute('INSERT OR IGNORE INTO observed_product_silicon VALUES(?,?,?,?,?,?,?,?)',
                        (pid,row['soc'],vendor,part,marketing,json.dumps([proof],sort_keys=True),'medium',now))
                counts['linked_products'] += 1
            else:
                counts[method] += 1
            connection.execute('INSERT OR IGNORE INTO source_specifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (_id('lineageos-specification',key,row['sha256']),SOURCE,key,pid,eid,maker,row['name'],row['codename'],
                 row['soc'],json.dumps(row['models']),row['source_url'],now,method))
        connection.execute("UPDATE sources SET authority_scope='secondary' WHERE id=?", (SOURCE,))
        connection.execute("UPDATE artifacts SET media_type='application/yaml' WHERE source_id=?", (SOURCE,))
        connection.execute("UPDATE ingestion_runs SET parser_name='lineageos-captured-specifications' WHERE source_id=?", (SOURCE,))
    counts['silicon_observations'] = connection.execute('SELECT count(*) FROM observed_product_silicon').fetchone()[0]
    counts['exact_hardware_evidence_links'] = connection.execute('''SELECT count(DISTINCT dc.hardware_model_id || ':' || ss.id)
        FROM v_device_catalog dc JOIN source_specifications ss ON lower(ss.manufacturer)=lower(dc.brand)
        JOIN json_each(ss.models_json) models ON models.value=dc.model_code
        WHERE ss.match_method!='blocked_by_review' ''').fetchone()[0]
    return dict(counts)


def hardware_specification_evidence(connection: sqlite3.Connection, model: str) -> list[dict]:
    """Exact model strings only; never strip /DS, expand wildcards or match names."""
    rows = connection.execute('''SELECT DISTINCT ss.*,e.artifact_id,a.sha256 artifact_sha256
        FROM v_device_catalog dc JOIN source_specifications ss ON lower(ss.manufacturer)=lower(dc.brand)
        JOIN json_each(ss.models_json) models ON models.value=dc.model_code
        JOIN evidence e ON e.id=ss.evidence_id JOIN artifacts a ON a.id=e.artifact_id
        WHERE dc.model_code=? AND ss.match_method!='blocked_by_review' ORDER BY ss.observed_at DESC,ss.source_key''', (model,))
    return [{**{k:v for k,v in dict(r).items() if k!='models_json'}, 'models':json.loads(r['models_json']),
             'confidence':'medium', 'identity_scope':'exact_model_in_community_specification',
             'canonical_mapping':False} for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--capture', type=Path, required=True)
    args = parser.parse_args()
    from .database import Database
    db = Database.migrated(args.data_dir / 'corpus.sqlite')
    local = args.data_dir / 'local.sqlite'
    decisions = []
    if local.exists():
        with sqlite3.connect(local) as c:
            c.row_factory = sqlite3.Row
            if c.execute("SELECT 1 FROM sqlite_schema WHERE name='identity_decisions'").fetchone():
                decisions = [dict(r) for r in c.execute('SELECT * FROM identity_decisions')]
    try:
        print(json.dumps(enrich_lineage_specs(db.connection,args.capture,decisions),indent=2,sort_keys=True))
    finally:
        db.close()


if __name__ == '__main__':
    main()
