"""Two ingest batches must never run against one corpus.

The systemd timer fires on a calendar, not on the previous run finishing.
Today's batch takes 110s against a 30m unit timeout, so overlap looks
impossible -- but the same work at the volume this is sized for runs for
hours, and then a nightly timer starts tonight's run while last night's is
still going. Both hold write transactions against the same SQLite file, and
the second dies on busy_timeout partway through: a half-ingested run,
reported as a crash, at 3am.

The collection worker already had this guard (worker_lock.exclusive_worker).
The batch -- much the bigger writer, and the one actually on a timer -- never
took it. These tests cover the lock itself and the property that matters:
holding it is what makes a second holder refuse.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory.worker_lock import exclusive_worker  # noqa: E402


class BatchIsSingleWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.data = Path(self._temp.name)

    def test_a_second_holder_is_refused_while_the_first_holds(self) -> None:
        lock = self.data / "batch.lock"
        with exclusive_worker(lock, holder="An ingest batch"):
            with self.assertRaises(ValueError) as caught:
                with exclusive_worker(lock, holder="An ingest batch"):
                    self.fail("a second batch acquired a lock the first was holding")
        self.assertIn("An ingest batch is already active", str(caught.exception),
                      "the error must name the batch, not the collection worker")

    def test_the_lock_is_released_when_the_first_holder_finishes(self) -> None:
        """A lock that is never released turns one crashed night into every night."""
        lock = self.data / "batch.lock"
        with exclusive_worker(lock, holder="An ingest batch"):
            pass
        with exclusive_worker(lock, holder="An ingest batch"):
            pass  # must not raise

    def test_the_lock_is_released_when_the_holder_raises(self) -> None:
        lock = self.data / "batch.lock"
        with self.assertRaises(RuntimeError):
            with exclusive_worker(lock, holder="An ingest batch"):
                raise RuntimeError("batch blew up mid-run")
        with exclusive_worker(lock, holder="An ingest batch"):
            pass  # the crashed run must not wedge the next one

    def test_a_dead_process_does_not_keep_the_lock(self) -> None:
        """flock is held by the file descriptor, so the kernel drops it when the
        process dies. A lock implemented with a PID file or a sentinel would
        survive a SIGKILL and block every subsequent night; assert the real
        behaviour rather than trusting the implementation."""
        lock = self.data / "batch.lock"
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            "from pathlib import Path\n"
            "from mobile_observatory.worker_lock import exclusive_worker\n"
            f"with exclusive_worker(Path({str(lock)!r}), holder='An ingest batch'):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(60)\n"
        )
        child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.kill)
        self.assertEqual("held\n", child.stdout.readline(), "precondition: the child took the lock")

        with self.assertRaises(ValueError, msg="the child's lock must block us while it lives"):
            with exclusive_worker(lock, holder="An ingest batch"):
                pass

        child.kill()
        child.wait(timeout=30)
        with exclusive_worker(lock, holder="An ingest batch"):
            pass  # the kernel released it with the process

    def test_batch_main_takes_the_lock_before_doing_any_work(self) -> None:
        """The guard is worthless if main() acquires it after the ingest.

        Asserts against the source that the lock wraps the run_batch call,
        rather than re-running a 110s batch twice to observe it.
        """
        source = (ROOT / "src" / "mobile_observatory" / "batch.py").read_text()
        self.assertIn("exclusive_worker(data_dir / \"batch.lock\"", source)
        lock_at = source.index("exclusive_worker(data_dir / \"batch.lock\"")
        call_at = source.index("results = run_batch(", lock_at)
        between = source[lock_at:call_at]
        self.assertNotIn("\nexcept", between,
                         "run_batch must be inside the lock's with-block, not after it")


if __name__ == "__main__":
    unittest.main()
