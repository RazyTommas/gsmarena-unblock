from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from mobile_observatory import Database
from mobile_observatory.server import ObservatoryService
from mobile_observatory.worker_lock import exclusive_worker
from mobile_observatory.collection_worker import CollectionWorker

ROOT = Path(__file__).resolve().parents[1]


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database.migrated()
        self.local_path = Path(self.temp.name) / 'local.sqlite'
        self.service = ObservatoryService(self.db, self.local_path, demonstration=False)
        self.worker = CollectionWorker(self.service.local, self.db.connection, self.service.worker_paths)

    def tearDown(self):
        self.service.local.close()
        self.db.close()
        self.temp.cleanup()

    def job(self):
        return self.service.request_collection({'target':'SM-S938B / ILO','source':'samsung','scope':'latest_firmware'})

    def test_dead_worker_recovery_retains_logs_and_requires_explicit_retry(self):
        job = self.job()
        self.service.local.execute("UPDATE collection_requests SET status='running',run_id='old-run',log_json=? WHERE id=?",
                                   (json.dumps([{'message':'old checkpoint'}]), job['id']))
        self.service.local.commit()
        self.assertEqual(self.worker.recover_interrupted(), [job['id']])
        old = self.worker.get(job['id'])
        self.assertEqual(old['status'], 'interrupted')
        self.assertEqual(old['run_id'], 'old-run')
        self.assertEqual(old['logs'][0]['message'], 'old checkpoint')
        self.assertIsNone(old['result']['accepted'])  # interrupted is not zero ingestion
        self.assertIsNone(self.worker.process_next())
        retry = self.worker.retry(job['id'])
        self.assertEqual(retry['retry_of'], job['id'])
        self.assertEqual(self.worker.retry(job['id'])['id'], retry['id'])
        finished = self.worker.process_next()
        self.assertIn(finished['status'], ('succeeded','partial'))
        self.assertEqual(self.worker.get(job['id'])['status'], 'interrupted')

    def test_active_process_cannot_be_recovered_and_process_death_releases_lock(self):
        code = 'import sys,time; from pathlib import Path; from mobile_observatory.worker_lock import exclusive_worker\nwith exclusive_worker(Path(sys.argv[1])):\n print("locked",flush=True)\n time.sleep(30)'
        process = subprocess.Popen([sys.executable,'-c',code,str(self.worker.lock_path)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            with self.assertRaisesRegex(ValueError, 'already active'):
                self.worker.recover_interrupted()
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
        self.assertEqual(self.worker.recover_interrupted(), [])

    def test_replay_keeps_capture_time_and_health_separates_import(self):
        finished = self.worker.process(self.job()['id'])
        self.assertEqual(finished['result']['evidenceObservedAt'], '2026-09-16T06:00:00Z')
        observed = self.db.connection.execute('SELECT DISTINCT observed_at FROM observations').fetchall()
        self.assertEqual([x[0] for x in observed], ['2026-09-16T06:00:00Z'])
        health = self.service.health()[0]
        self.assertEqual(health['captured_at'], '2026-09-16T06:00:00Z')
        self.assertNotEqual(health['last'], health['captured_at'])
        self.assertFalse(health['live_network'])
        self.assertIn('no scheduler', health['next'])
        self.assertEqual(health['execution_mode'], 'captured_replay')

    def test_service_restart_recovers_pending_job_without_execution(self):
        job = self.job()
        self.service.local.execute("UPDATE collection_requests SET status='running' WHERE id=?", (job['id'],))
        self.service.local.commit()
        self.service.local.close()
        self.service = ObservatoryService(self.db, self.local_path, demonstration=False)
        self.assertEqual(self.service.collection_requests()[0]['status'], 'interrupted')
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM observations').fetchone()[0], 0)
