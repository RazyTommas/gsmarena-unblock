"""Deterministic product-level specification enrichment; never hardware promotion."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from .enrichment import _capture_evidence, _id, _soc_parts, _xiaomi_catalog, _REGION

SOURCE = 'gsmarena.captured.specifications'


def name_key(value: str, maker: str) -> str:
    value = value.casefold().strip()
    prefix = maker.casefold() + ' '
    if value.startswith(prefix):
        value = value[len(prefix):]
    # '+' and regional/network qualifiers distinguish products; do not erase them.
    return ' '.join(re.sub(r'[^\w+]+', ' ', value).split())


def enrich_product_specs(connection: sqlite3.Connection, *, specs_csv: Path,
                         devices_yml: Path, google_play_csv: Path | None = None,
                         decisions: list[dict] | None = None) -> dict[str, int]:
    """Reuse exact identities, or retain a standalone specification product.

    Catalog aliases require exact captured codenames. Google Play only corroborates
    an exact name or model/device identifier, never supplies chipset facts. Multiple
    specification candidates remain unresolved, even if their chip strings agree.
    Existing review conclusions and local negative/deferred decisions are retained.
    """
    with specs_csv.open(encoding='utf-8-sig') as handle:
        specs = [dict(row, _line=str(line)) for line, row in enumerate(csv.DictReader(handle), 2)]
    catalog = _xiaomi_catalog(devices_yml)
    digest = hashlib.sha256(specs_csv.read_bytes()).hexdigest()
    catalog_digest = hashlib.sha256(devices_yml.read_bytes()).hexdigest()
    database_file = connection.execute('PRAGMA database_list').fetchone()[2]
    if database_file:
        capture_dir = Path(database_file).parent / 'evidence'
        capture_dir.mkdir(exist_ok=True)
        for path in (specs_csv, devices_yml, google_play_csv):
            if path and path.is_file():
                content = path.read_bytes()
                destination = capture_dir / (hashlib.sha256(content).hexdigest() + path.suffix)
                if not destination.exists():
                    destination.write_bytes(content)
                if path == specs_csv:
                    specs_csv = destination
    play_digest = hashlib.sha256(google_play_csv.read_bytes()).hexdigest() if google_play_csv and google_play_csv.is_file() else None
    indexed = defaultdict(list)
    for spec in specs:
        maker = spec.get('brand') or ('Xiaomi' if spec['device_name'].startswith('Xiaomi ') else
                                      'TECNO' if spec['device_name'].lower().startswith('tecno ') else '')
        spec['_maker'] = maker
        if maker and spec.get('slug') and spec.get('chipset', '').strip():
            indexed[(maker.casefold(), name_key(spec['device_name'], maker))].append(spec)
    play = defaultdict(list)
    if google_play_csv and google_play_csv.is_file():
        with google_play_csv.open(encoding='utf-8-sig') as handle:
            for row in csv.DictReader(handle):
                brand = row['Retail Branding'].casefold()
                brand = 'xiaomi' if brand in ('poco', 'redmi') else brand
                for value in (row['Marketing Name'], row['Model'], row['Device']):
                    if value.strip():
                        play[(brand, value.casefold().strip())].append(row)
    products = connection.execute('SELECT * FROM source_products').fetchall()
    occupied = {(p['manufacturer'].casefold(), name_key(p['canonical_name'], p['manufacturer'])) for p in products}
    claimed = set()
    totals = {'matched_products': 0, 'specification_only_products': 0, 'ambiguous_products': 0,
              'blocked_products': 0, 'specification_rows': len(specs)}

    def save(product_id, spec, chain, method):
        now = spec.get('fetched_at')
        if not now:
            return  # No invented observation date.
        url = 'https://www.gsmarena.com/' + spec['slug']
        evidence_id = _capture_evidence(connection, source_id=SOURCE, source_name='GSMArena captured specifications',
            source_url=url, path=specs_csv, locator='csv:line=' + spec['_line'],
            excerpt=json.dumps({k: v for k, v in spec.items() if not k.startswith('_')}, sort_keys=True), now=now)
        proof = {'source': 'gsmarena_captured_specs', 'source_url': url, 'slug': spec['slug'],
                 'device_name': spec['device_name'], 'chipset': spec['chipset'],
                 'artifact_sha256': digest, 'locator': 'csv:line=' + spec['_line'],
                 'evidence_id': evidence_id, 'observed_at': now, 'match_method': method,
                 'specification': {k: v for k, v in spec.items() if not k.startswith('_')}}
        vendor, part, marketing = _soc_parts(spec['chipset'])
        connection.execute('INSERT OR REPLACE INTO observed_product_silicon VALUES(?,?,?,?,?,?,?,?)',
            (product_id, spec['chipset'], vendor, part, marketing, json.dumps([proof, *chain], sort_keys=True), 'high', now))
        claimed.add(spec['slug'])

    if not connection.in_transaction:
        connection.execute('BEGIN IMMEDIATE')
    with connection:
        for product in products:
            maker = product['manufacturer']
            identities = [dict(r) for r in connection.execute('SELECT * FROM source_identity_registry WHERE product_id=?', (product['id'],))]
            blocked = product['review_state'] == 'rejected' or any(i['resolution_state'] == 'rejected' or (i['resolution_method'] == 'manual_product_review' and i['resolution_state'] != 'approved') for i in identities)
            blocked = blocked or any(d.get('decision') in ('different', 'defer') and
                (d.get('canonical_id') == product['id'] or any(
                    d.get('source_namespace') in (i['namespace'], i['source_id']) and d.get('source_value') == i['source_value']
                    for i in identities)) for d in (decisions or []))
            if blocked:
                totals['blocked_products'] += 1
                continue
            candidates = {}
            names = [(product['canonical_name'], [], 'exact_product_name')]
            # Only reuse approved source identities. Region suffix removal applies
            # to catalog market labels, never to specification product variants.
            for identity in identities:
                if identity['resolution_state'] != 'approved':
                    continue
                if maker == 'Xiaomi' and identity['namespace'] == 'codename':
                    for alias in catalog.get(identity['source_value'], []):
                        names.append((_REGION.sub('', alias), [{'source': 'xiaomi_devices_yml',
                            'identity': identity['source_value'], 'catalog_name': alias,
                            'artifact_sha256': catalog_digest}], 'exact_catalog_codename'))
                if identity['namespace'] in ('codename', 'model_code'):
                    rows = play.get((maker.casefold(), identity['source_value'].casefold()), [])
                    unique_names = {r['Marketing Name'] for r in rows if r['Marketing Name']}
                    if len(unique_names) == 1:
                        alias = next(iter(unique_names))
                        names.append((alias, [{'source': 'google_play_supported_devices',
                            'identity': identity['source_value'], 'marketing_name': alias,
                            'artifact_sha256': play_digest, 'models': sorted({r['Model'] for r in rows})}], 'exact_play_identifier'))
            for name, chain, method in names:
                for spec in indexed.get((maker.casefold(), name_key(name, maker)), []):
                    candidates.setdefault(spec['slug'], (spec, chain, method))
            if len(candidates) == 1:
                spec, chain, method = next(iter(candidates.values()))
                # Duplicate rows with conflicting assertions are not unique evidence.
                duplicates = indexed[(maker.casefold(), name_key(spec['device_name'], maker))]
                if len({(s['slug'], s['chipset']) for s in duplicates}) != 1:
                    totals['ambiguous_products'] += 1
                    continue
                if any(i['namespace'] == 'specification_slug' and i['source_value'] == spec['slug'] for i in identities):
                    method = 'standalone_captured_specification'
                save(product['id'], spec, chain, method)
                totals['matched_products'] += 1
            elif candidates:
                totals['ambiguous_products'] += 1

        for key, rows in indexed.items():
            if key in occupied or len({(r['slug'], r['chipset']) for r in rows}) != 1:
                continue
            spec = rows[0]
            if spec['slug'] in claimed or not spec.get('fetched_at'):
                continue
            pid = _id('specification-product', spec['_maker'], key[1])
            now = spec['fetched_at']
            connection.execute('INSERT OR IGNORE INTO source_products VALUES(?,?,?,?,?,?,?,?)',
                (pid, spec['_maker'], spec['device_name'], key[1], 'approved', None, now, now))
            save(pid, spec, [], 'standalone_captured_specification')
            connection.execute('INSERT OR IGNORE INTO source_identity_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (_id('specification-identity', spec['slug']), SOURCE, 'specification_slug', spec['slug'],
                 spec['slug'], pid, 'approved', 'captured_specification_identity', '1', 'high', now, now))
            totals['specification_only_products'] += 1
        connection.execute("UPDATE sources SET authority_scope='secondary' WHERE id=?", (SOURCE,))
        # This source is third-party specification evidence, not a vendor assertion.
        connection.execute("UPDATE ingestion_runs SET parser_name='captured-product-specifications' WHERE source_id=?", (SOURCE,))
    totals['silicon_observations'] = connection.execute('SELECT count(*) FROM observed_product_silicon').fetchone()[0]
    return totals
