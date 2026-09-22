"""Tests for the two traps called out for any scheduled/long-running piece
of this project:

1. Python block-buffers a non-TTY stdout, so a killed/crashed process can
   leave a zero-byte log. The fix must be proven by checking the log file's
   byte size WHILE THE PROCESS IS STILL RUNNING, not after it exits --
   letting a process exit and then checking its log proves nothing, because
   normal interpreter shutdown flushes everything regardless of buffering
   mode. See src/mobile_observatory/batch_logging.py.

2. A detector is worthless if nothing reads its output. These tests also
   prove mobile_observatory.batch.main() turns a "silent" finding into a
   real, checkable signal (nonzero exit + a logged ALARM line) without
   requiring the (slow, multi-minute) real batch pipeline to run.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class LineBufferingProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def test_configured_logger_writes_bytes_before_the_process_exits(self):
        """Positive case: our helper must have written the first log line to
        disk while the process is still alive (mid-sleep), proving it is not
        waiting for interpreter shutdown to flush."""
        log_path = Path(self.temp.name) / "proof.log"
        script = (
            "import sys, time; sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from mobile_observatory.batch_logging import configure_batch_logging\n"
            "logger = configure_batch_logging(Path(%r))\n"
            "logger.info('first line before sleeping')\n"
            "time.sleep(4)\n"
            "logger.info('second line after sleeping')\n"
        ) % (str(ROOT / "src"), str(log_path))
        process = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        try:
            deadline = time.monotonic() + 5
            saw_bytes_while_running = False
            while time.monotonic() < deadline:
                if log_path.exists() and log_path.stat().st_size > 0:
                    # The critical assertion happens HERE, before the process
                    # has had any chance to exit -- a test that only checked
                    # after process.wait() would pass even with full buffering.
                    self.assertIsNone(process.poll(), "process must still be running for this check to mean anything")
                    saw_bytes_while_running = True
                    break
                time.sleep(0.05)
            if not saw_bytes_while_running:
                process.wait(timeout=10)
                self.fail(f"log file never gained bytes while the process was alive; "
                          f"subprocess output was: {process.stdout.read()!r}")
        finally:
            process.wait(timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertIn("first line before sleeping", log_path.read_text())
        self.assertIn("second line after sleeping", log_path.read_text())

    def test_negative_control_plain_buffered_write_withholds_bytes_while_sleeping(self):
        """Proves this test methodology actually discriminates: a naive
        `open(path, 'w')` (full buffering, the historical bug) must NOT show
        bytes while the process sleeps. If this assertion ever fails, the
        positive test above is not measuring anything."""
        log_path = Path(self.temp.name) / "control.log"
        script = (
            "import time\n"
            "f = open(%r, 'w')\n"
            "f.write('first line before sleeping\\n')\n"
            "time.sleep(3)\n"
            "f.write('second line after sleeping\\n')\n"
            "f.close()\n"
        ) % (str(log_path),)
        process = subprocess.Popen([sys.executable, "-c", script])
        try:
            time.sleep(1.5)  # well inside the 3s sleep, so the process is still alive
            self.assertIsNone(process.poll())
            size_while_running = log_path.stat().st_size if log_path.exists() else 0
            self.assertEqual(size_while_running, 0,
                              "expected the unfixed pattern to withhold bytes until close/exit")
        finally:
            process.wait(timeout=10)
        self.assertGreater(log_path.stat().st_size, 0)  # flushed only once the process exited


class BatchAlarmWiringTests(unittest.TestCase):
    """batch.main() is the only piece that can turn silence into something
    outside the process (nonzero exit + logged ALARM). Faking run_batch's
    return value keeps this fast and independent of the real (multi-minute)
    ingestion pipeline."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "data"

    def tearDown(self):
        self.temp.cleanup()

    def _run_main(self, fake_results: dict):
        from mobile_observatory import batch
        argv = ["mobile_observatory.batch", "--data-dir", str(self.data_dir), "--legacy-root", str(self.temp.name)]
        with patch.object(batch, "run_batch", return_value=fake_results), patch.object(sys, "argv", argv):
            batch.main()

    def test_silent_finding_exits_nonzero_and_logs_an_alarm(self):
        from mobile_observatory import batch
        fake_results = {"totals": {"devices": 1}, "silence": [{
            "source_id": "s1", "source_name": "planted-silent-source", "status": "silent",
            "last_activity_at": "2020-01-01T00:00:00Z", "expected_interval_hours": 1.0,
            "overdue_hours": 5000.0, "finished_run_count": 9, "checked_at": "2026-01-01T00:00:00Z",
        }]}
        argv = ["mobile_observatory.batch", "--data-dir", str(self.data_dir), "--legacy-root", str(self.temp.name)]
        with patch.object(batch, "run_batch", return_value=fake_results), patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                batch.main()
        self.assertEqual(ctx.exception.code, 2)
        log_text = (self.data_dir / "batch.log").read_text()
        self.assertIn("ALARM", log_text)
        self.assertIn("planted-silent-source", log_text)

    def test_healthy_findings_do_not_exit_nonzero_or_alarm(self):
        from mobile_observatory import batch
        fake_results = {"totals": {"devices": 1}, "silence": [{
            "source_id": "s2", "source_name": "planted-healthy-source", "status": "healthy",
            "last_activity_at": "2026-01-01T00:00:00Z", "expected_interval_hours": 1.0,
            "overdue_hours": 0.1, "finished_run_count": 9, "checked_at": "2026-01-01T00:00:00Z",
        }]}
        self._run_main(fake_results)  # must NOT raise SystemExit
        log_text = (self.data_dir / "batch.log").read_text()
        self.assertNotIn("ALARM", log_text)


if __name__ == "__main__":
    unittest.main()
