from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from mobile_observatory import Database
from mobile_observatory.repository import CanonicalRepository
from mobile_observatory.seed import seed_demonstration
from mobile_observatory.server import ObservatoryService


class LatestFirmwareEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Database.migrated()
        seed_demonstration(self.db,ROOT/'fixtures/supported_catalog.sample.json')
        self.service=ObservatoryService(self.db,Path(self.temp.name)/'local.sqlite',demonstration=True)
        c=self.db.connection;now='2026-09-01T00:00:00Z'
        self.hardware=CanonicalRepository(self.db).create_device(manufacturer='Samsung',brand='Samsung',family='Latest fixture',variant='Latest fixture',model_code='SM-LATEST')
        c.execute("INSERT INTO sources VALUES('samsung.fota','Samsung test',NULL,'vendor',1,?)",(now,))
        c.execute("INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,parser_version) VALUES('latest-run','samsung.fota',?,'succeeded','samsung_fota_history_csv','1.0.1')",(now,))
        c.execute("INSERT INTO artifacts VALUES('latest-artifact','samsung.fota','latest-run',?,'text/csv','https://example.test/manifest',?,'fixture.csv',1)",('a'*64,now))
        self.os=c.execute('SELECT id FROM os_releases ORDER BY major DESC LIMIT 1').fetchone()[0]
        for rid,position in [('a-source-latest','latest'),('z-history','upgrade')]:
            c.execute("""INSERT INTO firmware_releases(id,hardware_model_id,build_id,os_release_id,
                first_observed_at,last_observed_at,created_at) VALUES(?,?,?,?,?,?,?)""",(rid,self.hardware,rid,self.os,now,now,now))
            payload=json.dumps({'data':{'manifest_position':position}})
            c.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,NULL)',(rid,'samsung.fota','latest-run','latest-artifact','firmware_release',rid,now,payload,'b'*64,'valid'))
            c.execute('INSERT INTO evidence VALUES(?,?,?,?,?,?)',(rid,'latest-artifact',rid,rid,'test',now))
            c.execute("INSERT INTO firmware_release_evidence VALUES(?,?,'identity')",(rid,rid))

    def tearDown(self):
        self.service.local.close();self.db.close();self.temp.cleanup()

    def test_explicit_source_latest_beats_arbitrary_capture_order(self):
        row=self.db.connection.execute('SELECT * FROM v_latest_firmware WHERE hardware_model_id=?',(self.hardware,)).fetchone()
        self.assertEqual(row['firmware_release_id'],'a-source-latest')
        self.assertEqual(row['latest_basis'],'source_manifest_latest')
        device=self.service.devices_page({'model':['SM-LATEST']}).items[0]
        self.assertEqual(device['software_state_basis'],'source_manifest_latest')
        self.assertNotEqual(device['android'],'Unknown')
        latest=self.service.device_detail('SM-LATEST')['latestFirmware']
        self.assertEqual(latest[0]['build'],'a-source-latest')

    def test_stale_source_latest_is_not_reused_after_a_newer_manifest(self):
        c=self.db.connection
        c.execute("UPDATE observations SET observed_at='2026-10-01T00:00:00Z' WHERE id='z-history'")
        row=c.execute('SELECT * FROM v_latest_firmware WHERE hardware_model_id=?',(self.hardware,)).fetchone()
        self.assertEqual(row['latest_basis'],'observation_order_only')
        device=self.service.devices_page({'model':['SM-LATEST']}).items[0]
        self.assertEqual(device['android'],'Unknown')
        self.assertEqual(device['patch'],'Unknown')
        self.assertEqual(self.service.device_detail('SM-LATEST')['latestFirmware'],[])
        self.assertEqual(self.service.devices_page({'model':['SM-LATEST'],'max_android':['99']}).total,0)
