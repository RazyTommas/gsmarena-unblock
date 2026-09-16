from __future__ import annotations
import importlib.util
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('portable_launcher',ROOT/'run.py')
launcher=importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

class PortableTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.original=launcher.ARCHIVE
        bundle=self.root/'bundle';bundle.mkdir()
        (bundle/'evidence').mkdir();evidence=bundle/'evidence/spec.bin';evidence.write_bytes(b'captured evidence')
        with sqlite3.connect(bundle/'corpus.sqlite') as db:
            db.execute('CREATE TABLE artifacts(id TEXT,storage_uri TEXT,sha256 TEXT)')
            db.execute('INSERT INTO artifacts VALUES(?,?,?)',('a','evidence/spec.bin',launcher.sha(evidence)))
        with sqlite3.connect(bundle/'local.sqlite') as db:
            db.execute('CREATE TABLE settings(value TEXT)')
        (bundle/'provenance-paths.json').write_text(json.dumps([{'artifact_id':'a','portable_storage_uri':'evidence/spec.bin'}]))
        manifest={'databases':['corpus.sqlite','local.sqlite'],'files':{
            p.relative_to(bundle).as_posix():{'sha256':launcher.sha(p),'bytes':p.stat().st_size}
            for p in bundle.rglob('*') if p.is_file()}}
        (bundle/'manifest.json').write_text(json.dumps(manifest))
        launcher.ARCHIVE=self.root/'snapshot.zip'
        with zipfile.ZipFile(launcher.ARCHIVE,'w') as z:
            for p in bundle.rglob('*'):
                if p.is_file():z.write(p,p.relative_to(bundle))
        launcher.ARCHIVE.with_suffix('.json').write_text(json.dumps({'sha256':launcher.sha(launcher.ARCHIVE)}))

    def tearDown(self):
        launcher.ARCHIVE=self.original;self.tmp.cleanup()

    def test_restore_preserves_local_changes_and_rebases_after_move(self):
        target=self.root/'computer one'
        self.assertTrue(launcher.restore(target))
        with sqlite3.connect(target/'local.sqlite') as db:db.execute("INSERT INTO settings VALUES('keep')")
        self.assertFalse(launcher.restore(target))
        moved=self.root/'computer two';target.rename(moved)
        launcher.rebase_evidence(moved)
        with sqlite3.connect(moved/'corpus.sqlite') as db:
            self.assertEqual(db.execute('SELECT storage_uri FROM artifacts').fetchone()[0],str(moved/'evidence/spec.bin'))
        with sqlite3.connect(moved/'local.sqlite') as db:
            self.assertEqual(db.execute('SELECT value FROM settings').fetchone()[0],'keep')

    def test_corrupt_archive_does_not_create_destination(self):
        with launcher.ARCHIVE.open('ab') as f:f.write(b'changed')
        with self.assertRaisesRegex(ValueError,'checksum'):launcher.restore(self.root/'restore')
        self.assertFalse((self.root/'restore').exists())

    def test_incomplete_existing_directory_is_not_overwritten(self):
        target=self.root/'existing';target.mkdir();(target/'notes').write_text('keep')
        with self.assertRaisesRegex(ValueError,'incomplete'):launcher.restore(target)
        self.assertEqual((target/'notes').read_text(),'keep')
