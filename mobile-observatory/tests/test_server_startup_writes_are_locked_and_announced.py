"""The server WRITES at startup. Nothing used to say so, or stop two writers.

Two defects found by reading what `main()` actually does before it serves a
byte, and confirmed against the live corpus:

  * **It migrates, ungated and silently.** `apply_migrations()` applies every
    pending migration in place, with no prompt, no count and no backup. That is
    how `.observatory-data` reached schema 33 while HANDOFF.md still said a
    human had to apply 0033 -- nobody did; a restart did. The packaged bundle is
    worse: it sits at schema 8 and gets carried 23 versions forward on first
    launch (measured).

  * **It writes OUTSIDE the batch lock.** `batch.py` takes
    `<data-dir>/batch.lock` around its whole run; `server.py` never referenced
    it, while doing `apply_migrations()` and a projection rebuild of its own. A
    nightly timer plus any systemd restart policy puts two writers on one
    corpus.sqlite, which is not a rare race -- it is a scheduled one.

The announcement is a line, not a refusal: the migration is usually correct and
necessary, and a server that will not start because its schema is behind is a
worse outage than the one it prevents. The LOCK is a refusal, because two
writers on one SQLite file is not a thing to warn about and continue past.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory import server as server_module  # noqa: E402
from mobile_observatory.worker_lock import exclusive_worker  # noqa: E402


class WhatIsAboutToHappenIsSaidFirst(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def test_pending_migrations_can_be_asked_before_they_are_applied(self) -> None:
        """The whole reason it could not say anything: nothing could ask."""
        db = Database(self.data / "corpus.sqlite")
        self.addCleanup(db.close)
        pending = db.pending_migrations()
        self.assertTrue(pending, "a fresh corpus has migrations to apply")
        self.assertEqual(0, db.schema_version())
        db.apply_migrations()
        self.assertEqual([], db.pending_migrations())
        self.assertEqual(pending[-1], db.schema_version())

    def test_applying_them_twice_applies_nothing_the_second_time(self) -> None:
        db = Database.migrated(self.data / "corpus.sqlite")
        self.addCleanup(db.close)
        version = db.schema_version()
        db.apply_migrations()
        self.assertEqual(version, db.schema_version())

    def test_the_server_names_the_count_and_the_versions_before_applying(self) -> None:
        source = inspect.getsource(server_module.main)
        announce = source.index("applying {len(pending)} migration(s)")
        apply_at = source.index("corpus.apply_migrations()")
        self.assertLess(announce, apply_at,
                        "it is said after the fact, when there is nothing left to say it "
                        "about")
        self.assertIn("no backup", source)
        self.assertIn("pending_migrations()", source)

    def test_a_real_server_prints_the_count_before_it_migrates(self) -> None:
        """Behavioural, not a source scan: a fresh data directory is at schema 0
        and has every migration pending, so `--demo` exercises exactly the path
        an upgrade takes."""
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            expected = len(Database(data / "probe.sqlite").pending_migrations())
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "mobile_observatory.server", "--demo",
                 "--data-dir", str(data), "--port", "8938"],
                cwd=ROOT, env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 90
                seen = ""
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(
                                "http://127.0.0.1:8938/api/v1/devices?limit=1", timeout=5):
                            break
                    except (urllib.error.URLError, OSError):
                        time.sleep(0.5)
            finally:
                process.terminate()
                try:
                    seen = process.communicate(timeout=30)[0]
                except subprocess.TimeoutExpired:
                    process.kill()
                    seen = process.communicate()[0]
            self.assertIn(f"applying {expected} migration(s)", seen)
            self.assertIn("no backup", seen)
            self.assertIn("schema 0 ->", seen)

    def test_run_py_announces_it_before_the_server_is_even_started(self) -> None:
        """run.py restores a bundle at schema 8; by the time the server speaks,
        the operator has already typed the command that did it."""
        import importlib.util
        import io
        import contextlib

        spec = importlib.util.spec_from_file_location("portable_launcher2", ROOT / "run.py")
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            Database(data / "corpus.sqlite").close()        # schema 0, all pending
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                pending = launcher.announce_pending_migrations(data)
            said = buffer.getvalue()
        self.assertTrue(pending)
        self.assertIn(f"{len(pending)} migration(s) IN PLACE", said)
        self.assertIn("no backup and no undo", said)

    def test_run_py_says_it_too_because_the_server_speaks_too_late(self) -> None:
        """By the time the server prints, the operator has already typed the
        command that did it. The bundle is at schema 8."""
        source = (ROOT / "run.py").read_text(encoding="utf-8")
        self.assertIn("announce_pending_migrations", source)
        self.assertIn("IN PLACE, with no backup and no undo", source)


class EveryStartupWriteIsInsideTheBatchLock(unittest.TestCase):
    def test_the_lock_wraps_the_writes_and_not_merely_the_open(self) -> None:
        source = inspect.getsource(server_module.main)
        lock = source.index('exclusive_worker(data_dir / "batch.lock"')
        for write in ("corpus.apply_migrations()", "build_current_firmware(corpus)",
                      "seed_demonstration(corpus"):
            with self.subTest(write=write):
                self.assertLess(lock, source.index(write),
                                f"{write} happens outside the lock")

    def test_a_server_started_during_a_batch_refuses_rather_than_writing_too(self) -> None:
        """A real subprocess against a real held lock. The policy-shaped version
        of this test would pass against a server that never takes it."""
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            Database.migrated(data / "corpus.sqlite").close()
            with exclusive_worker(data / "batch.lock", holder="A pretend ingest batch"):
                result = subprocess.run(
                    [sys.executable, "-m", "mobile_observatory.server", "--demo",
                     "--data-dir", str(data), "--port", "0"],
                    cwd=ROOT, env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
                    capture_output=True, text=True, timeout=120)
            self.assertNotEqual(0, result.returncode,
                                "it started and wrote while a batch held the lock")
            self.assertIn("already active", result.stderr.lower() + result.stdout.lower())
            self.assertIn("two writers", result.stderr + result.stdout)

    def test_it_starts_normally_when_no_batch_is_running(self) -> None:
        """The companion assertion. A lock that is always held would make the
        test above pass while breaking every deployment."""
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "mobile_observatory.server", "--demo",
                 "--data-dir", str(data), "--port", "8937"],
                cwd=ROOT, env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                served = False
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        with urllib.request.urlopen(
                                "http://127.0.0.1:8937/api/v1/devices?limit=1", timeout=5):
                            served = True
                            break
                    except (urllib.error.URLError, OSError):
                        time.sleep(0.5)
                self.assertTrue(served, "the server never answered")
                # And the lock is RELEASED once the writes are done, or the next
                # batch could never run.
                with exclusive_worker(data / "batch.lock", holder="A later batch"):
                    pass
            finally:
                process.terminate()
                try:
                    process.wait(30)
                except subprocess.TimeoutExpired:
                    process.kill()
                process.stdout.close()
                process.stderr.close()


class ThePostureBannerCarriesTheSchema(unittest.TestCase):
    def test_the_banner_names_the_schema_and_whether_this_startup_moved_it(self) -> None:
        from mobile_observatory.posture import Posture

        moved = Posture(bind_host="0.0.0.0", port=8000, token_source="environment",
                        open=False, acknowledged=frozenset(), schema_version=33,
                        migrations_applied=(30, 31, 32, 33))
        self.assertIn("schema=33(migrated+4-this-startup)", moved.banner())
        still = Posture(bind_host="127.0.0.1", port=8000, token_source="none",
                        open=True, acknowledged=frozenset(), schema_version=33)
        self.assertIn("schema=33", still.banner())
        self.assertNotIn("migrated+", still.banner())

    def test_an_unknown_schema_says_unknown_rather_than_zero(self) -> None:
        from mobile_observatory.posture import Posture

        self.assertIn("schema=unknown",
                      Posture(bind_host="127.0.0.1", port=1, token_source="none",
                              open=True, acknowledged=frozenset()).banner())


if __name__ == "__main__":
    unittest.main()
