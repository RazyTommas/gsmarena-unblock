"""One-time read-only extraction; no runtime dependency on Observatory."""
import json
import sqlite3
from pathlib import Path
from core import connect, encoded, now, put, sha256


def technical_specs(old, product_id, specs_json):
    specs = json.loads(specs_json or '{}')
    observations = [dict(r) for r in old.execute('''SELECT raw_chipset,vendor,part_number,marketing_name,
        confidence,evidence_json,observed_at FROM observed_product_silicon WHERE product_id=? ORDER BY raw_chipset''', (product_id,))]
    if observations:
        specs['chipset_observations'] = observations
    return encoded(specs)


def import_corpus(source: Path, data: Path):
    if not source.is_file():
        raise ValueError('Source corpus does not exist')
    c = connect(data)
    if c.execute('SELECT count(*) FROM devices').fetchone()[0]:
        c.close()
        raise ValueError('Import requires an empty catalog; existing data will not be overwritten')
    old = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    old.row_factory = sqlite3.Row
    try:
        old.execute('BEGIN')
        with c:
            for p in old.execute('SELECT * FROM source_products'):
                spec = old.execute('SELECT source_url,observed_at FROM source_specifications WHERE product_id=? ORDER BY observed_at DESC LIMIT 1', (p['id'],)).fetchone()
                put(c, 'devices', dict(id=p['id'], manufacturer=p['manufacturer'], name=p['canonical_name'],
                    identity_state=p['review_state'], specs_json=technical_specs(old, p['id'], p['specification_json']),
                    source_url=spec['source_url'] if spec else '', observed_at=spec['observed_at'] if spec else p['updated_at']))
            for m in old.execute('SELECT * FROM device_catalog_flat'):
                put(c, 'models', dict(id=m['hardware_model_id'], manufacturer=m['brand'] or m['manufacturer'],
                    name=m['variant'] or m['family'], model_code=m['model_code'] or '', codename=m['codename'] or '',
                    chipset=m['chip_marketing_name'] or m['chip_part_number'] or ''))
            for row in old.execute('SELECT product_id,hardware_model_id FROM product_hardware_links'):
                c.execute('INSERT OR IGNORE INTO device_models VALUES (?,?)', tuple(row))
            # These are source-local product keys, not assertions of canonical hardware identity.
            for row in old.execute("SELECT source_id,source_value,product_id FROM source_identity_registry"):
                # Conflicting source aliases cannot be used to auto-link future updates.
                existing = c.execute('SELECT device_id FROM identities WHERE source=? AND external_id=?', tuple(row)[:2]).fetchone()
                if existing and existing[0] != row[2]:
                    c.execute('UPDATE identities SET device_id=NULL WHERE source=? AND external_id=?', tuple(row)[:2])
                else:
                    c.execute('INSERT OR IGNORE INTO identities VALUES (?,?,?)', tuple(row))
            query = '''SELECT p.*,o.payload_json,o.observed_at,o.source_key AS observation_key FROM product_firmware_releases p
                       JOIN observations o ON o.id=p.observation_id'''
            for f in old.execute(query):
                payload = json.loads(f['payload_json']); details = payload.get('data', {})
                evidence = payload.get('evidence', {})
                put(c, 'firmwares', dict(id=f['id'], device_id=f['product_id'], model_id=f['hardware_model_id'],
                    source=f['source_id'], source_key=f['observation_key'], build=f['build_id'],
                    version=details.get('ios_version') or f['android_version'] or '',
                    os=details.get('os_name') or ('Android' if f['android_version'] else ''),
                    region=f['region_code'] or '', channel=f['channel'] or '', delivery=f['delivery_method'] or '',
                    released_at=f['vendor_released_at'] or '', patch_level=details.get('security_patch_level') or '',
                    baseband=details.get('baseband_version') or '', download_url=details.get('download_url') or '',
                    source_url=evidence.get('source_url') or details.get('catalog_url') or '',
                    observed_at=f['observed_at'], record_layer='source_product'))
            query = '''SELECT f.*,t.target_code,os.display_name AS os_label FROM firmware_releases f
                       LEFT JOIN firmware_targets t ON t.id=f.firmware_target_id
                       LEFT JOIN os_releases os ON os.id=f.os_release_id'''
            for f in old.execute(query):
                # A canonical release relates to exact hardware, not an arbitrarily selected product.
                put(c, 'firmwares', dict(id=f['id'], device_id=None, model_id=f['hardware_model_id'],
                    source='samsung.fota', source_key=f['id'], build=f['build_id'], version=f['os_label'] or '',
                    os='Android', region=f['target_code'] or '', channel=f['channel'] or '',
                    released_at=f['vendor_released_at'] or '', patch_level=f['security_patch_level'] or '',
                    baseband=f['baseband_version'] or '', observed_at=f['last_observed_at'] or '',
                    source_url='https://fota-cloud-dn.ospserver.net/', record_layer='canonical_hardware'))
            # Retain firmware observations that were held back by the older product's identity
            # promotion rules. They remain explicitly unresolved, never canonical facts.
            query = '''SELECT o.*,l.product_id,l.link_state FROM observations o
                       JOIN observation_product_links l ON l.observation_id=o.id
                       WHERE o.record_type='firmware_release' AND o.source_id!='samsung.fota'
                       AND NOT EXISTS(SELECT 1 FROM product_firmware_releases p WHERE p.observation_id=o.id)'''
            unresolved = 0
            for f in old.execute(query):
                payload = json.loads(f['payload_json']); details = payload.get('data', {}); evidence = payload.get('evidence', {})
                put(c, 'firmwares', dict(id=f['id'], device_id=f['product_id'], model_id=None,
                    source=f['source_id'], source_key=f['source_key'], build=details.get('build') or f['source_key'],
                    version=details.get('ios_version') or details.get('android') or '',
                    os=details.get('os_name') or ('Android' if details.get('android') else ''),
                    region=details.get('region_code') or '', channel=details.get('channel') or details.get('branch') or '',
                    delivery=details.get('delivery_method') or '',
                    released_at=details.get('vendor_released_at') or details.get('release_date') or '',
                    patch_level=details.get('security_patch_level') or '', baseband=details.get('baseband_version') or '',
                    download_url=details.get('download_url') or '', source_url=evidence.get('source_url') or details.get('catalog_url') or '',
                    observed_at=f['observed_at'], record_layer='unresolved_observation'))
                unresolved += 1
            meta = {'schema_version': '1', 'imported_at': now(), 'source_corpus_sha256': sha256(source),
                    'source_corpus_name': source.name, 'unresolved_firmware_observations': str(unresolved),
                    'scope': 'devices, models, technical specifications, firmware'}
            c.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', meta.items())
            problems = c.execute('PRAGMA foreign_key_check').fetchall()
            if problems:
                raise ValueError('Import contains broken relationships')
    finally:
        old.close()
        c.close()
