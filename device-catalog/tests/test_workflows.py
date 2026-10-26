import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import connect,put,encoded,now
from catalog import list_rows,prompt_for,quality
from research import SpecsParser,release_date,seed_targets,import_assertions
from updater import config,save_config,review


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.data=Path(self.tmp.name);self.c=connect(self.data)
        for i in range(36):
            put(self.c,'devices',dict(id=f'd{i:03}',manufacturer='Maker',name=f'Phone {i:03}',identity_state='source_reported',
                specs_json=encoded({'chipset':'Chip 1'} if i%2 else {}),released_at='2020-01' if i%3 else ''))
        self.c.commit()

    def tearDown(self):self.c.close();self.tmp.cleanup()

    def test_page_sizes_no_duplicates_and_last_page_clamping(self):
        settings=config(self.data)
        first=list_rows(self.c,settings,limit=10)
        second=list_rows(self.c,settings,limit=10,offset=10)
        self.assertEqual(len(first['rows']),10)
        self.assertFalse({r['id'] for r in first['rows']}&{r['id'] for r in second['rows']})
        last=list_rows(self.c,settings,limit=10,offset=9999)
        self.assertEqual((last['offset'],len(last['rows'])),(30,6))
        for limit in [0,501]:
            with self.assertRaises(ValueError):list_rows(self.c,settings,limit=limit)

    def test_sort_and_missing_filters(self):
        result=list_rows(self.c,config(self.data),missing='released_at',sort='name',direction='desc',limit=500)
        self.assertEqual(result['total'],12)
        self.assertEqual(result['rows'][0]['name'],'Phone 033')
        with self.assertRaises(ValueError):list_rows(self.c,config(self.data),sort='name; DROP TABLE devices')
        self.assertEqual(list_rows(self.c,config(self.data),query='%')['total'],0)

    def test_region_preferences_and_linked_firmware(self):
        for i,region in enumerate(['ILO','MID','GLOBAL','CN']):
            put(self.c,'firmwares',dict(id=f'f{i}',device_id='d000',model_id=None,source='test',source_key=str(i),build='v1',region=region,record_layer='source_reported'))
        self.c.commit()
        s=config(self.data)
        self.assertEqual(list_rows(self.c,s,table='firmwares',related_device='d000')['total'],3)
        self.assertEqual(list_rows(self.c,{**s,'firmware_regions':None},table='firmwares')['total'],4)
        self.assertEqual(list_rows(self.c,{**s,'firmware_regions':[]},table='firmwares')['total'],0)
        self.assertEqual(list_rows(self.c,s,table='firmwares',related_device='d001')['total'],0)

    def test_prompt_targets_parent_without_inventing_model(self):
        put(self.c,'firmwares',dict(id='fw',device_id='d000',model_id=None,source='test',source_key='fw',build='v1',record_layer='source_reported'))
        p=prompt_for(self.c,'firmwares','fw','model_released_at')
        self.assertEqual((p['table'],p['id'],p['field']),('devices','d000','released_at'))
        self.assertIn('research-import',p['prompt']);self.assertIn('NOT announcement',p['prompt'])
        with self.assertRaises(ValueError):prompt_for(self.c,'devices','d000','__sql__')

    def test_quality_counts_and_specs_missing(self):
        q=quality(self.c)
        released=next(x for x in q['fields'] if x['table']=='devices' and x['field']=='released_at')
        self.assertEqual((released['present'],released['missing']),(24,12))
        self.assertEqual(list_rows(self.c,config(self.data),missing='cpu')['total'],36)

    def test_spec_parser_preserves_nested_text_and_release_precision(self):
        parser=SpecsParser();parser.feed('<h1>Maker Phone</h1><td data-spec="chipset">Chip <b>One</b><br>5 nm</td><span data-spec="released-hl">Released 2020, January</span>')
        self.assertEqual(parser.fields['chipset'],'Chip One 5 nm')
        self.assertEqual(release_date(parser.fields['released-hl']),'2020-01')
        self.assertEqual(release_date('Announced 2019, December 17. Released 2020, January 30'),'2020-01-30')
        self.assertEqual(release_date('Announced 2019, December 17'),'')

    def test_seed_uses_url_and_name_in_correct_columns(self):
        path=self.data/'old.sqlite'
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE source_specifications(product_id,name,source_url)')
            c.execute('INSERT INTO source_specifications VALUES (?,?,?)',('d000','Maker Phone','https://www.gsmarena.com/phone-1.php'))
        seed_targets(self.data,path)
        row=self.c.execute('SELECT * FROM research_targets').fetchone()
        self.assertEqual(row['url'],'https://www.gsmarena.com/phone-1.php')
        self.assertEqual(row['expected_name'],'Maker Phone')

    def test_research_excerpt_verification_and_staging(self):
        body=b'Maker Phone became available on January 30, 2020.'
        def fake_fetch(c,data,source,url):
            import hashlib
            (data/'evidence').mkdir(exist_ok=True);(data/'evidence/page.bin').write_bytes(body)
            c.execute('INSERT OR IGNORE INTO captures VALUES (?,?,?,?,?,?,?)',('proof',source,url,now(),hashlib.sha256(body).hexdigest(),'evidence/page.bin',len(body)));c.commit()
            return body,'proof'
        assertion=dict(table='devices',id='d000',field='released_at',value='2020-01-30',source_url='https://example.com/phone',quote='available on January 30, 2020')
        with patch('research.fetch',side_effect=fake_fetch):
            with self.assertRaises(ValueError):import_assertions(self.data,[{**assertion,'quote':'This quote does not exist'}])
            result=import_assertions(self.data,[assertion])
        self.assertEqual(result['staged'],1)
        self.assertEqual(self.c.execute("SELECT released_at FROM devices WHERE id='d000'").fetchone()[0],'')
        candidate=self.c.execute('SELECT id FROM candidates').fetchone()[0]
        response={'decisions':[{'id':candidate,'decision':'approve','rationale':'Exact source evidence matches record','capture_id':'proof'}]}
        with patch('updater.invoke',return_value=response):review(self.c,self.data,config(self.data))
        self.assertEqual(self.c.execute("SELECT released_at FROM devices WHERE id='d000'").fetchone()[0],'2020-01-30')
        self.assertEqual(self.c.execute('SELECT method FROM research_facts').fetchone()[0],'llm_reviewed')

    def test_model_research_does_not_create_extra_product(self):
        from updater import stage,apply_candidate
        put(self.c,'models',dict(id='m',manufacturer='Maker',name='Phone',model_code='M1'))
        self.c.execute('INSERT INTO captures VALUES (?,?,?,?,?,?,?)',('c','test','https://example.com',now(),'hash','x',1))
        row=dict(self.c.execute("SELECT * FROM models WHERE id='m'").fetchone());row['released_at']='2020-01'
        stage(self.c,'model',row,'c')
        candidate=self.c.execute('SELECT id FROM candidates').fetchone()[0]
        self.c.execute("UPDATE candidates SET status='review'");self.c.commit()
        apply_candidate(self.c,candidate)
        self.assertEqual(self.c.execute('SELECT count(*) FROM devices').fetchone()[0],36)


if __name__=='__main__':unittest.main()
