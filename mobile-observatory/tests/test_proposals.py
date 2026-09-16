from pathlib import Path
import json
import tempfile
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from mobile_observatory import Database
from mobile_observatory.server import ObservatoryService


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Database.migrated()
        self.service=ObservatoryService(self.db,Path(self.temp.name)/'local.sqlite',demonstration=False)
        self.db.connection.execute("INSERT INTO source_products VALUES('p1','Xiaomi','Example','example','proposed',NULL,'2026-01-01','2026-01-01')")
        self.proposal={'product_id':'p1','decision':'defer','canonical_name':'Example','model_codes':[],
           'aliases':[],'confidence':'low','evidence':[{'url':'https://example.com/spec','note':'Example research reference; no exact model code established'}],
           'rationale':'Insufficient evidence for an exact hardware identity'}

    def tearDown(self):
        self.service.local.close();self.db.close();self.temp.cleanup()

    def test_batch_validation_is_atomic_and_rejects_unsafe_or_missing_evidence(self):
        for invalid in ({**self.proposal,'product_id':'missing'}, {**self.proposal,'evidence':[]},
                        {**self.proposal,'evidence':[{'url':'javascript:alert(1)','note':'unsafe'}]},
                        {**self.proposal,'evidence':[{'artifact_id':'imaginary','note':'not captured'}]},
                        {**self.proposal,'confidence':'authoritative'}, {**self.proposal,'extra':True}):
            with self.assertRaises(ValueError):self.service.import_agent_proposals([self.proposal,invalid])
            self.assertEqual(self.service.agent_proposals(),[])

    def test_review_is_local_idempotent_remembered_and_preserves_correction_history(self):
        before=list(self.db.connection.iterdump())
        first=self.service.import_agent_proposals([self.proposal])
        self.assertEqual(first['canonicalChanges'],0)
        self.assertEqual(self.service.import_agent_proposals([self.proposal])['ids'],first['ids'])
        identifier=first['ids'][0]
        self.service.review_agent_proposal(identifier,{'decision':'different','rationale':'Conflicting hardware evidence'})
        self.service.import_agent_proposals([{**self.proposal,'rationale':'A repeated suggestion with new wording'}])
        self.assertTrue(all(x['status']=='rejected' for x in self.service.agent_proposals()))
        self.service.review_agent_proposal(identifier,{'decision':'same','rationale':'Human correction after inspecting evidence'})
        items=self.service.agent_proposals()
        self.assertTrue(all(x['status']=='approved' for x in items))
        reviewed=next(x for x in items if x['id']==identifier)
        self.assertEqual([r['decision'] for r in reviewed['reviews']],['different','same'])
        self.assertEqual(before,list(self.db.connection.iterdump()))
        self.service.local.close()
        self.service=ObservatoryService(self.db,Path(self.temp.name)/'local.sqlite',demonstration=False)
        self.assertTrue(all(x['status']=='approved' for x in self.service.agent_proposals()))

    def test_identity_memory_keeps_previous_decisions_and_one_current_null_target(self):
        value={'sourceNamespace':'example','sourceValue':'name','canonicalType':'hardware_model',
               'canonicalId':None,'decision':'different','rationale':'first review'}
        self.service.save_identity_decision(value)
        self.service.save_identity_decision({**value,'decision':'defer','rationale':'needs more evidence'})
        self.assertEqual(len(self.service.identity_decisions()),1)
        self.assertEqual(self.service.identity_decisions()[0]['decision'],'defer')
        self.assertEqual([x['decision'] for x in self.service.identity_history()],['defer','different'])

    def test_agent_bundle_suppresses_previously_reviewed_targets(self):
        bundle=Path(self.temp.name)/'agent-review';bundle.mkdir()
        (bundle/'agent-review-prompt.md').write_text('Assignment')
        (bundle/'identity-candidates.json').write_text(json.dumps([{'id':'p1','canonical_name':'Example'}]))
        identifier=self.service.import_agent_proposals([self.proposal])['ids'][0]
        self.service.review_agent_proposal(identifier,{'decision':'defer','rationale':'Wait for vendor evidence'})
        self.assertEqual(self.service.agent_review_bundle()['candidateCount'],0)
        self.assertEqual(self.service.agent_review_bundle()['rememberedReviews'][0]['status'],'deferred')

    def test_product_review_survives_replaceable_corpus_without_promoting_hardware(self):
        self.service.review_source_product('p1','rejected')
        self.assertEqual(self.service.identity_decisions()[0]['decision'],'different')
        history_length=len(self.service.identity_history())
        self.service.local.close()
        self.db.connection.execute("UPDATE source_products SET review_state='proposed' WHERE id='p1'")
        self.service=ObservatoryService(self.db,Path(self.temp.name)/'local.sqlite',demonstration=False)
        self.assertEqual(self.db.connection.execute("SELECT review_state FROM source_products WHERE id='p1'").fetchone()[0],'rejected')
        self.assertEqual(len(self.service.identity_history()),history_length)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM hardware_models').fetchone()[0],0)
        self.service.review_source_product('p1','proposed')
        self.assertEqual(self.service.identity_decisions()[0]['decision'],'defer')
        self.assertEqual([x['decision'] for x in self.service.identity_history()],['defer','different'])

    def test_empty_real_corpus_does_not_borrow_demonstration_timestamps(self):
        self.assertIsNone(self.service.meta['dataAsOf'])
        self.assertIn('unknown',self.service.meta['snapshot'])
        self.assertEqual(self.service.health(),[])

    def test_superseded_upgrades_and_corrections_are_not_current_firmware(self):
        from mobile_observatory.repository import CanonicalRepository, Event
        self.db.connection.execute('''CREATE VIEW IF NOT EXISTS v_current_domain_events AS
          SELECT original.* FROM domain_events original WHERE NOT EXISTS
          (SELECT 1 FROM domain_events correction WHERE correction.corrects_event_id=original.id)''')
        repo=CanonicalRepository(self.db)
        original,_=repo.append_event(Event('android_version_changed','source_product','p1','old-upgrade',
             '2020-01-01T00:00:00Z',{'android':'14'},{'android':'15'}))
        self.assertEqual(self.service.overview()['androidUpgrades'],1)
        correction,_=repo.append_event(Event('identity_corrected','source_product','p1','correct-upgrade',
             '2026-09-17T00:00:00Z',{'android':'14'},{'android':'15'},corrects_event_id=original))
        self.assertEqual(self.service.updates_page({}).total,0)
        self.assertEqual(self.service.overview()['androidUpgrades'],0)
        self.assertEqual(self.service.overview()['unseen'],0)
        self.assertEqual(self.service.product_detail('p1')['androidUpgrades'],[])
        with self.assertRaises(KeyError):self.service.acknowledge(correction)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM domain_events').fetchone()[0],2)
