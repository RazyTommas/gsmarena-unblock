from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).parents[1]/'src'))
from mobile_observatory.collectors.adapters.samsung_history import SamsungFotaHistoryAdapter
from mobile_observatory.collectors.adapters.xiaomi_tracker import _region_from_codename_and_name
from mobile_observatory.database import Database
from mobile_observatory.enrichment import promote_approved_product_observations
from mobile_observatory.source_corrections import correct_source_interpretations, firmware_date_evidence

NOW='2026-09-17T00:00:00Z'


class SourceCorrectionTests(unittest.TestCase):
    def test_samsung_correction_preserves_observations_and_audits_old_date(self):
        from mobile_observatory.repository import CanonicalRepository
        db=Database.migrated(); c=db.connection
        try:
            hardware=CanonicalRepository(db).create_device(manufacturer='Samsung Electronics',brand='Samsung',
                family='Test',variant='Test',model_code='SM-TEST')
            c.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',('samsung.fota','Samsung FOTA',None,'primary',1,NOW))
            c.execute('INSERT INTO ingestion_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)',('r','samsung.fota',NOW,NOW,'succeeded','samsung_fota_history_csv','1.0.0',1,1,0,None))
            c.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)',('a','samsung.fota','r','a'*64,'text/csv',None,NOW,'file.csv',1))
            payload=json.dumps({'data':{'release_time':'2026-01-01','build':'BUILD'}})
            c.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)',('o','samsung.fota','r','a','firmware_release','o',NOW,payload,hashlib.sha256(payload.encode()).hexdigest(),'promoted',None))
            c.execute('INSERT INTO evidence VALUES(?,?,?,?,?,?)',('e','a','o','CSV line 2',None,NOW))
            c.execute('INSERT INTO firmware_releases(id,hardware_model_id,build_id,vendor_released_at,first_observed_at,last_observed_at,created_at) VALUES(?,?,?,?,?,?,?)',('f',hardware,'BUILD','2026-01-01',NOW,NOW,NOW))
            c.execute('INSERT INTO firmware_release_evidence VALUES(?,?,?)',('f','e','availability'))
            self.assertEqual(correct_source_interpretations(c)['samsung_dates_cleared'],1)
            self.assertIsNone(c.execute('SELECT vendor_released_at FROM firmware_releases').fetchone()[0])
            self.assertEqual(firmware_date_evidence(c,'f')['build_derived_month'],'2026-01')
            self.assertEqual(c.execute('SELECT payload_json FROM observations').fetchone()[0],payload)
            self.assertEqual(correct_source_interpretations(c)['samsung_dates_cleared'],0)
        finally:db.close()

    def test_regional_global_suffixes_and_conflicting_names(self):
        for code,name,expected in [('umi_tr_global','Mi 10 Global','TR'),('nezha_in_global','Pad India','IN'),
            ('foo_ru_global','Phone Russia','RU'),('foo_id_global','Phone Indonesia','ID'),
            ('foo_tw_global','Phone Taiwan','TW'),('foo_jp_global','Phone Japan','JP'),
            ('foo_eea_global','Phone EEA','EEA'),('foo_global','Phone Global','GLOBAL'),
            ('foo','Phone China','CN'),('foo_in_global','Phone Russia','SOURCE_UNSPECIFIED'),
            ('china_phone','No region specified','SOURCE_UNSPECIFIED')]:
            with self.subTest(code=code):self.assertEqual(_region_from_codename_and_name(code,name),expected)

    def test_samsung_parser_never_treats_encoded_month_as_vendor_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'history.csv'
            path.write_text('model,csc,version,fetched_at,device,cp,pda_month,kind\nSM-TEST,ILO,BUILD,2026-09-17T00:00:00Z,Test,CP,2026-01-01,latest\n')
            adapter=SamsungFotaHistoryAdapter(path)
            row=next(iter(adapter.parse(next(iter(adapter.fetch())),'a'*64)))
            self.assertIsNone(row.data['release_time'])
            self.assertEqual(row.data['build_derived_month'],'2026-01')
            self.assertNotIn('android',row.data)
            self.assertNotIn('security_patch',row.data)

    def test_current_event_view_preserves_superseded_history(self):
        db=Database.migrated()
        try:
            for eid,corrects,kind in [('old',None,'android_version_changed'),('correction','old','identity_corrected')]:
                db.connection.execute('INSERT INTO domain_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (eid,kind,'source_product','product',eid,NOW,NOW,None,'{}',None,corrects))
            self.assertEqual(db.connection.execute('SELECT count(*) FROM domain_events').fetchone()[0],2)
            self.assertEqual([r[0] for r in db.connection.execute('SELECT id FROM v_current_domain_events')],['correction'])
            with self.assertRaises(sqlite3.IntegrityError):db.connection.execute("DELETE FROM domain_events WHERE id='old'")
        finally:db.close()

    def test_corrected_product_region_replay_is_idempotent(self):
        db=Database.migrated(); c=db.connection
        try:
            c.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',('xiaomi.community.firmware_tracker','Xiaomi',None,'secondary',1,NOW))
            c.execute('INSERT INTO ingestion_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)',('r','xiaomi.community.firmware_tracker',NOW,NOW,'succeeded','xiaomi_firmware_tracker_csv','1.0.0',1,1,0,None))
            c.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)',('a','xiaomi.community.firmware_tracker','r','a'*64,'text/csv',None,NOW,'file.csv',1))
            c.execute('INSERT INTO source_products VALUES(?,?,?,?,?,?,?,?)',('p','Xiaomi','Phone','phone','approved',None,NOW,NOW))
            c.execute('INSERT INTO source_identity_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('i','xiaomi.community.firmware_tracker','codename','phone_tr_global','phone_tr_global','p','approved','exact','1','high',NOW,NOW))
            data={'model_code':'phone_tr_global','source_device_name':'Phone Global','region_code':'GLOBAL',
                  'build':'BUILD','branch':'Stable','android':'14','release_date':'2026-01-01','delivery_method':None}
            for oid in ['o1','o2']:
                payload=json.dumps({'data':data,'parser_variant':oid})
                c.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)',(oid,'xiaomi.community.firmware_tracker','r','a','firmware_release',oid,NOW,payload,hashlib.sha256(payload.encode()).hexdigest(),'valid',None))
                c.execute('INSERT INTO observation_product_links VALUES(?,?,?,?,?)',(oid,'p','i','approved',NOW))
            promote_approved_product_observations(c)
            self.assertEqual(c.execute('SELECT count(*) FROM product_firmware_releases').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT region_code FROM product_firmware_releases').fetchone()[0],'TR')
            c.execute("UPDATE product_firmware_releases SET region_code='GLOBAL'")
            self.assertEqual(correct_source_interpretations(c)['xiaomi_regions_corrected'],1)
            self.assertEqual(correct_source_interpretations(c)['xiaomi_regions_corrected'],0)
            self.assertEqual(c.execute('SELECT count(*) FROM source_data_corrections').fetchone()[0],1)
            self.assertIn('GLOBAL',c.execute("SELECT payload_json FROM observations WHERE id='o1'").fetchone()[0])
        finally:db.close()
