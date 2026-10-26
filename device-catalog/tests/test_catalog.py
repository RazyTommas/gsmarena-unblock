import csv
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import SCHEMA, catalog_lock, connect, encoded, export_csv, now, put
from updater import apply_candidate, config, invoke, review, save_config, stage, xiaomi, google_play
from run import bundle


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.c = connect(self.data)
        body = b'{"name":"Phone One","manufacturer":"Maker","model":"M-01"}'
        (self.data / 'evidence').mkdir()
        (self.data / 'evidence/source.bin').write_bytes(body)
        self.c.execute('INSERT INTO captures VALUES (?,?,?,?,?,?,?)', ('capture', 'test', 'https://example.com/catalog', now(), hashlib.sha256(body).hexdigest(), 'evidence/source.bin', len(body)))
        self.c.commit()
        self.device = dict(id='device-1', manufacturer='Maker', name='Phone One', identity_state='source_reported',
                           specs_json='{"cpu":"Example CPU"}', source_url='https://example.com/catalog', observed_at=now())

    def tearDown(self):
        self.c.close(); self.tmp.cleanup()

    def candidate(self, row=None, kind='device'):
        stage(self.c, kind, row or self.device, 'capture'); self.c.commit()
        return self.c.execute('SELECT * FROM candidates ORDER BY created_at DESC').fetchone()

    def decision(self, candidate_id, decision='approve'):
        return {'id': candidate_id, 'decision': decision, 'rationale': 'Manufacturer and model agree with captured source.', 'capture_id': 'capture'}

    def test_evidence_reasoning_apply_and_idempotency(self):
        candidate = self.candidate()
        with patch('updater.invoke', return_value={'decisions': [self.decision(candidate['id'])]}) as call:
            result = review(self.c, self.data, config(self.data))
            self.assertIn('source_excerpt', call.call_args.args[1])
        self.assertEqual(result['applied'], 1)
        self.assertEqual(self.c.execute('SELECT name FROM devices').fetchone()[0], 'Phone One')
        self.assertEqual(stage(self.c, 'device', {**self.device, 'observed_at': '2099-01-01'}, 'capture'), 0)
        self.assertEqual(self.c.execute('SELECT count(*) FROM candidates').fetchone()[0], 1)

    def test_invalid_second_decision_applies_nothing(self):
        first = self.candidate()
        second = {**self.device, 'id': 'device-2', 'name': 'Second'}
        self.candidate(second)
        with patch('updater.invoke', return_value={'decisions': [self.decision(first['id']), self.decision('invented')]}):
            with self.assertRaises(ValueError):
                review(self.c, self.data, config(self.data))
        self.assertEqual(self.c.execute('SELECT count(*) FROM devices').fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT count(*) FROM candidates WHERE status='pending'").fetchone()[0], 2)

    def test_evidence_tampering_blocks_reasoning(self):
        self.candidate()
        (self.data / 'evidence/source.bin').write_text('changed')
        with patch('updater.invoke') as call:
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                review(self.c, self.data, config(self.data))
            call.assert_not_called()

    def test_stale_proposal_cannot_overwrite(self):
        candidate = self.candidate()
        put(self.c, 'devices', {**self.device, 'name': 'Operator correction'})
        self.c.execute("UPDATE candidates SET status='review'"); self.c.commit()
        self.assertEqual(apply_candidate(self.c, candidate['id']), 0)
        self.assertEqual(self.c.execute('SELECT name FROM devices').fetchone()[0], 'Operator correction')

    def test_missing_reference_stays_in_review(self):
        row = dict(id='firmware-1', device_id='absent', model_id=None, source='test', source_key='one', build='001', record_layer='source_reported')
        candidate = self.candidate(row, 'firmware')
        with patch('updater.invoke', return_value={'decisions': [self.decision(candidate['id'])]}):
            self.assertEqual(review(self.c, self.data, config(self.data))['applied'], 0)
        self.assertEqual(self.c.execute('SELECT status FROM candidates').fetchone()[0], 'review')

    def test_pending_cannot_be_applied_without_reasoning(self):
        candidate = self.candidate()
        with self.assertRaises(ValueError):
            apply_candidate(self.c, candidate['id'])

    def test_unknown_feed_fields_rejected(self):
        with self.assertRaises(ValueError):
            self.candidate({**self.device, 'unexpected_sql_column': 'anything'})

    def test_historical_http_links_are_preserved_but_script_urls_rejected(self):
        row = dict(id='f',device_id='d',model_id=None,source='test',source_key='f',build='v1',record_layer='source_reported',download_url='http://example.com/firmware.zip')
        self.assertEqual(stage(self.c,'firmware',row,'capture'),1)
        with self.assertRaises(ValueError):
            stage(self.c,'firmware',{**row,'download_url':'javascript:alert(1)'},'capture')

    def test_csv_preserves_unicode_commas_newlines_and_blocks_formulas(self):
        put(self.c, 'devices', {**self.device, 'name': '=HYPERLINK("https://example.com")', 'manufacturer': 'מותג, Maker',
                                'specs_json': encoded({'cpu': 'One\nTwo', 'chipset': '0012'})})
        export_csv(self.c, self.data / 'csv')
        path = self.data / 'csv/devices.csv'
        self.assertTrue(path.read_bytes().startswith(b'\xef\xbb\xbf'))
        with path.open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]['manufacturer'], 'מותג, Maker')
        self.assertEqual(rows[0]['cpu'], 'One\nTwo')
        self.assertEqual(rows[0]['chipset'], '0012')
        self.assertTrue(rows[0]['name'].startswith("'="))

    def test_command_contract_and_failure(self):
        result = invoke([sys.executable, '-c', 'import sys; s=sys.stdin.read(); print(\'{"ok":true}\' if s=="test" else "bad")'], 'test', 10)
        self.assertEqual(result, {'ok': True})
        with self.assertRaisesRegex(ValueError, 'valid JSON'):
            invoke([sys.executable, '-c', 'print("not JSON")'], 'test', 10)
        with self.assertRaisesRegex(ValueError, 'exited 2'):
            invoke([sys.executable, '-c', 'import sys; print("secret",file=sys.stderr); sys.exit(2)'], 'test', 10)

    def test_configuration_roundtrip_and_validation(self):
        settings = config(self.data)
        settings['command'] = ['claude', '-p', '--model', 'fable']
        save_config(self.data, settings)
        self.assertEqual(config(self.data)['command'], settings['command'])
        settings['review_batch_size'] = 0
        with self.assertRaises(ValueError):
            save_config(self.data, settings)

    def test_chipsets_include_specs_and_linked_models_without_duplicates(self):
        put(self.c, 'devices', {**self.device, 'specs_json': encoded({'chipset': 'Snapdragon 8',
            'chipset_observations': [{'raw_chipset': 'Exynos 2200', 'confidence': 'source_reported'}]})})
        for model_id, chipset in [('m1', 'Snapdragon 8'), ('m2', 'Dimensity 9000')]:
            put(self.c, 'models', dict(id=model_id, manufacturer='Maker', name='Phone One', chipset=chipset))
            self.c.execute('INSERT INTO device_models VALUES (?,?)', (self.device['id'], model_id))
        row = self.c.execute("SELECT * FROM device_details WHERE chipset LIKE '%Dimensity%'").fetchone()
        self.assertEqual(row['chipset'], 'Dimensity 9000 | Exynos 2200 | Snapdragon 8')
        export_csv(self.c, self.data / 'csv')
        with (self.data / 'csv/devices.csv').open(encoding='utf-8-sig') as f:
            self.assertEqual(next(csv.DictReader(f))['chipset'], row['chipset'])

    def test_region_defaults_and_explicit_all_or_none(self):
        settings = config(self.data)
        self.assertEqual(settings['firmware_regions'], ['ILO', 'MID', 'GLOBAL', 'GL'])
        for regions in [None, [], ['MID', 'GLOBAL']]:
            save_config(self.data, {**settings, 'firmware_regions': regions})
            self.assertEqual(config(self.data)['firmware_regions'], regions)
        with self.assertRaises(ValueError):
            save_config(self.data, {**settings, 'firmware_regions': 'MID'})

    def test_release_date_review_preserves_precision_and_rejects_invalid_dates(self):
        candidate = self.candidate({**self.device, 'released_at': '2026-09'})
        with patch('updater.invoke', return_value={'decisions': [self.decision(candidate['id'])]}):
            self.assertEqual(review(self.c, self.data, config(self.data))['applied'], 1)
        self.assertEqual(self.c.execute('SELECT released_at FROM devices').fetchone()[0], '2026-09')
        export_csv(self.c, self.data / 'csv')
        with (self.data / 'csv/devices.csv').open(encoding='utf-8-sig') as f:
            self.assertEqual(next(csv.DictReader(f))['released_at'], '2026-09')
        for value in ['2026-02-30', '2026-13', 'announced September 2026']:
            with self.assertRaises(ValueError):
                stage(self.c, 'device', {**self.device, 'released_at': value}, 'capture')

    def test_old_catalog_date_migration_preserves_records(self):
        import sqlite3
        root = self.data / 'old'; root.mkdir()
        with sqlite3.connect(root / 'catalog.sqlite') as old:
            old.executescript(SCHEMA.replace(" released_at TEXT NOT NULL DEFAULT '',\n", ''))
            old.execute("INSERT INTO devices(id,manufacturer,name,identity_state) VALUES ('old','Maker','Old phone','source_reported')")
        migrated = connect(root)
        try:
            row = migrated.execute("SELECT * FROM devices WHERE id='old'").fetchone()
            self.assertEqual(row['name'], 'Old phone')
            self.assertEqual(row['released_at'], '')
            self.assertIn('released_at', {r[1] for r in migrated.execute('PRAGMA table_info(models)')})
        finally:
            migrated.close()

    def test_xiaomi_keeps_distinct_names_and_regions(self):
        body = ''.join(f"- android: '14'\n  branch: Stable\n  codename: same\n  date: 2026-01-01\n  link: https://example.com/file.zip\n  method: Recovery\n  name: {name}\n  version: V1\n"
                       for name in ['Phone W China', 'Phone China', 'Phone Global'])
        put(self.c, 'devices', self.device)
        self.c.execute('INSERT INTO identities VALUES (?,?,?)', ('xiaomi.community.firmware_tracker','same','device-1'));self.c.commit()
        with patch('updater.fetch', return_value=(body.encode(), 'capture')):
            result = xiaomi(self.c,self.data,{'id':'xiaomi'})
        self.assertEqual(result['staged'],3)
        payloads=[json.loads(r[0]) for r in self.c.execute('SELECT payload_json FROM candidates')]
        self.assertEqual(len({r['source_key'] for r in payloads}),3)
        self.assertEqual({r['region'] for r in payloads},{'CN','GLOBAL'})

    def test_google_utf16_and_existing_model_identity(self):
        body='Retail Branding,Marketing Name,Device,Model\r\nMaker,Phone One,one,M-01\r\nMaker,Phone Two,two,M-02\r\n'
        put(self.c,'models',dict(id='known',manufacturer='Maker',name='Phone One',model_code='M-01'))
        with patch('updater.fetch',return_value=(body.encode('utf-16'),'capture')):
            result=google_play(self.c,self.data,{'id':'google-play'})
        self.assertEqual(result['staged'],1)
        self.assertEqual(json.loads(self.c.execute('SELECT payload_json FROM candidates').fetchone()[0])['model_code'],'M-02')

    def test_process_lock_blocks_second_writer(self):
        with catalog_lock(self.data):
            with self.assertRaisesRegex(ValueError,'already open'):
                with catalog_lock(self.data):
                    pass

    def test_bundle_manifest_and_standalone_operation(self):
        import zipfile
        put(self.c, 'devices', self.device); self.c.commit()
        save_config(self.data, config(self.data))
        output = self.data / 'bundle.zip'
        bundle(self.data, output)
        with zipfile.ZipFile(output) as z:
            bundled_settings = json.loads(z.read('device-catalog/data/settings.json'))
            self.assertEqual(bundled_settings, {'firmware_regions': ['ILO', 'MID', 'GLOBAL', 'GL'], 'page_size': 50})
            z.extractall(self.data / 'unpacked')
        root = self.data / 'unpacked/device-catalog'
        manifest = json.loads((root / 'manifest.json').read_text())
        for name, digest in manifest['files_sha256'].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest)
        result = subprocess.run([sys.executable, str(root / 'run.py'), 'status'], cwd=root, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['devices'], 1)


if __name__ == '__main__':
    unittest.main()
