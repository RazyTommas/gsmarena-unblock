"""Ten minutes of silence is indistinguishable from a hang.

THE DEFECT. The batch wrote two lines for a ten-minute run -- "batch starting"
and "batch finished totals=..." -- so an operator watching it could not tell a
slow source from a wedged one, could not tell which source was running, and
could not tell whether anything had been ingested at all. Measured on the live
corpus's own batch.log: 2 lines (3 once retention landed) per run, every run.

WHAT IT MUST NOT BECOME. Chatty. One line per phase, written when the phase
ENDS; not a start line and an end line, and not per-record progress. Measured
after: 39 lines for the whole run.

THE PART THAT ACTUALLY ANSWERS THE QUESTION is the heartbeat, and the first
time it ran against the real corpus it earned its place immediately:
`identity:bridge-registry` took **202.7s of a 218.1s run** -- 93% of the batch
in one phase that had never once been visible. A completion line cannot tell you
the phase you are waiting on is still alive; only something that speaks while it
is open can.

Two things here are guards against mistakes already made in this file's subject
matter:
  * a heartbeat must report a GROWING age. The first implementation re-armed
    itself by moving the phase's start time forward one interval, which would
    have had every beat say "still running after 60s" however long it had
    really been -- an honest-looking number that cannot tell a stall from a
    blip, which is the entire signal.
  * a step whose result carries no integer must SAY so, not print nothing.
    An empty tail reads as zero of everything.
"""
from __future__ import annotations

import inspect
import logging
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import batch  # noqa: E402
from mobile_observatory.batch_logging import (DEFAULT_HEARTBEAT_SECONDS,  # noqa: E402
                                               MAX_COUNT_FIELDS, ProgressLog,
                                               summarise_counts)


class Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record) -> None:
        self.lines.append(record.getMessage())


class ProgressLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger(f"test.progress.{id(self)}")
        self.logger.handlers.clear()
        self.logger.setLevel(logging.INFO)
        self.recorder = Recorder()
        self.logger.addHandler(self.recorder)
        self.logger.propagate = False
        self.clock = [0.0]

    def progress(self, **kwargs):
        log = ProgressLog(self.logger, clock=lambda: self.clock[0], **kwargs)
        self.addCleanup(log.close)
        return log

    # -- one line, with a count and a time ---------------------------------

    def test_one_phase_is_exactly_one_line(self) -> None:
        """Two lines per phase would double a log somebody reads at 3am to find
        the one line that matters."""
        progress = self.progress(heartbeat_seconds=0)
        with progress.phase("source:xiaomi") as phase:
            self.clock[0] = 12.5
            phase[0] = {"valid": 3588, "invalid": 0}
        self.assertEqual(1, len(self.recorder.lines))
        line = self.recorder.lines[0]
        self.assertIn("phase 1 source:xiaomi", line)
        self.assertIn("valid=3588", line)
        self.assertIn("took=12.5s", line)
        self.assertIn("elapsed=12.5s", line)

    def test_phases_are_numbered_and_elapsed_accumulates(self) -> None:
        progress = self.progress(heartbeat_seconds=0)
        with progress.phase("a"):
            self.clock[0] = 5.0
        with progress.phase("b"):
            self.clock[0] = 9.0
        self.assertIn("phase 2 b", self.recorder.lines[1])
        self.assertIn("took=4.0s", self.recorder.lines[1])
        self.assertIn("elapsed=9.0s", self.recorder.lines[1])
        self.assertEqual(2, progress.phases)

    def test_there_is_no_hand_maintained_denominator(self) -> None:
        """A progress display reading "4/12" on a run of thirteen states a
        number it cannot source. The total is not knowable when the first phase
        logs, so the count of phases run is reported once, at the end."""
        progress = self.progress(heartbeat_seconds=0)
        with progress.phase("a"):
            pass
        self.assertNotIn("/", self.recorder.lines[0].split(" took=")[0])

    # -- a phase that dies says which phase it was -------------------------

    def test_a_failing_phase_logs_before_the_exception_continues(self) -> None:
        progress = self.progress(heartbeat_seconds=0)
        with self.assertRaises(KeyError):
            with progress.phase("source:broken"):
                self.clock[0] = 3.0
                raise KeyError("missing column")
        self.assertEqual(1, len(self.recorder.lines))
        self.assertIn("source:broken FAILED KeyError", self.recorder.lines[0])
        self.assertIn("took=3.0s", self.recorder.lines[0])

    def test_a_failing_phase_does_not_report_a_count_it_does_not_have(self) -> None:
        progress = self.progress(heartbeat_seconds=0)
        with self.assertRaises(ValueError):
            with progress.phase("source:broken") as phase:
                phase[0] = {"valid": 10}       # set, then the step died
                raise ValueError("halfway")
        self.assertNotIn("valid=10", self.recorder.lines[0],
                         "a partial count on a failed phase reads as the phase's result")

    # -- the heartbeat -----------------------------------------------------

    def test_a_still_open_phase_says_so_and_keeps_saying_so(self) -> None:
        progress = self.progress(heartbeat_seconds=10)
        with progress.phase("identity:bridge-registry") as phase:
            self.clock[0] = 10.1
            self._wait_for_lines(1)
            self.clock[0] = 20.2
            self._wait_for_lines(2)
            self.clock[0] = 30.3
            self._wait_for_lines(3)
            phase[0] = {"products": 1904}
        beats = [l for l in self.recorder.lines if "still running" in l]
        self.assertEqual(3, len(beats))
        self.assertIn("no count yet", beats[0])
        self.assertIn("this phase has not returned", beats[0],
                      "it must not call a derivation a source; the first real heartbeat "
                      "named identity:bridge-registry, which is not a publisher")
        # Each beat reports how long the phase has been open, which GROWS. This
        # is why `_open` carries the real start and the next-beat deadline as two
        # fields: re-arming by moving `started` forward would have every beat say
        # "still running after 10s" no matter how long it had really been -- an
        # honest-looking number that cannot distinguish a stall from a blip, and
        # exactly the signal this exists to give. The real run printed 61s, 121s,
        # 181s and then took=202.7s.
        ages = [float(l.split("after ")[1].split("s")[0]) for l in beats]
        self.assertEqual(sorted(ages), ages)
        self.assertGreater(ages[-1], ages[0] + 15,
                           f"the beats are not reporting a growing age: {ages}")

    def test_the_completion_line_reports_the_real_duration_after_heartbeats(self) -> None:
        progress = self.progress(heartbeat_seconds=10)
        with progress.phase("slow") as phase:
            self.clock[0] = 25.0
            self._wait_for_lines(1)
            self.clock[0] = 42.0
            phase[0] = 7
        done = [l for l in self.recorder.lines if "still running" not in l]
        self.assertEqual(1, len(done))
        self.assertIn("took=42.0s", done[0])

    def test_a_fast_phase_emits_no_heartbeat_at_all(self) -> None:
        """A normal run on today's corpus must produce none of these, or the
        line that matters is buried in lines that do not."""
        progress = self.progress(heartbeat_seconds=60)
        with progress.phase("quick") as phase:
            self.clock[0] = 0.4
            phase[0] = 3
        time.sleep(0.3)
        self.assertEqual([], [l for l in self.recorder.lines if "still running" in l])

    def test_the_heartbeat_does_not_fire_between_phases(self) -> None:
        progress = self.progress(heartbeat_seconds=10)
        with progress.phase("one") as phase:
            phase[0] = 1
        self.clock[0] = 500.0
        time.sleep(0.3)
        self.assertEqual([], [l for l in self.recorder.lines if "still running" in l])

    def test_the_default_interval_is_declared_rather_than_typed_at_a_call_site(self) -> None:
        self.assertEqual(60.0, DEFAULT_HEARTBEAT_SECONDS)
        self.assertIn("--progress-heartbeat-seconds",
                      inspect.getsource(batch.main),
                      "an operator must be able to change it without editing source")

    def _wait_for_lines(self, count, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len([l for l in self.recorder.lines if "still running" in l]) >= count:
                return
            time.sleep(0.02)
        self.fail(f"only {len(self.recorder.lines)} line(s) after {timeout}s: "
                  f"{self.recorder.lines}")

    # -- the no-op ---------------------------------------------------------

    def test_a_progress_log_with_no_logger_is_inert_and_still_yields(self) -> None:
        """So the ingest has one object to call either way, rather than every
        phase being guarded by `if progress`."""
        progress = ProgressLog(None)
        with progress.phase("x") as phase:
            phase[0] = 5
        self.assertEqual(0, progress.phases)


class SummarisingWhateverAStepReturned(unittest.TestCase):
    def test_the_shapes_the_batch_actually_returns(self) -> None:
        self.assertEqual("count=42", summarise_counts(42))
        self.assertEqual("valid=10 invalid=0", summarise_counts({"valid": 10, "invalid": 0}))
        self.assertEqual("items=3", summarise_counts([1, 2, 3]))

    def test_a_truncated_summary_says_it_is_truncated(self) -> None:
        """Presenting a truncated list as complete is the one thing this project
        refuses to do, and a log line is not exempt."""
        many = {f"k{i}": i for i in range(MAX_COUNT_FIELDS + 3)}
        text = summarise_counts(many)
        self.assertIn("(+3 more)", text)
        self.assertEqual(MAX_COUNT_FIELDS, text.count("="),
                         "exactly the cap is shown, and the rest is declared")

    def test_absence_of_a_count_is_stated_and_not_shown_as_zero(self) -> None:
        self.assertEqual("count=none-returned", summarise_counts(None))
        self.assertEqual("count=none-stated", summarise_counts({"path": "/tmp/x"}))
        self.assertEqual("count=not-a-count", summarise_counts("a string"))

    def test_non_integers_do_not_push_the_numbers_off_the_line(self) -> None:
        text = summarise_counts({"digest": "a" * 64, "rows": 2769, "devices": 845})
        self.assertNotIn("a" * 10, text)
        self.assertIn("rows=2769", text)

    def test_a_dataclass_like_result_is_summarised_from_its_attributes(self) -> None:
        class Report:
            def __init__(self):
                self.promoted = 4
                self.skipped = 1
        self.assertEqual("promoted=4 skipped=1", summarise_counts(Report()))

    def test_a_bool_is_not_reported_as_a_count_of_one(self) -> None:
        """`isinstance(True, int)` is True in Python, so `count=1` for a flag is
        one `isinstance` away and reads as a row count."""
        self.assertEqual("ok=True", summarise_counts(True))
        self.assertEqual("count=none-stated", summarise_counts({"off": True}))


class TheIngestIsActuallyInstrumented(unittest.TestCase):
    """Source scans, because the ingest reads 16 real inputs and takes minutes;
    the full run's log is measured separately and reported in the handoff."""

    def setUp(self) -> None:
        self.source = inspect.getsource(batch._ingest)

    def test_every_captured_source_is_its_own_phase(self) -> None:
        self.assertIn('progress.phase(f"source:{run_id}")', self.source,
                      "the adapter loop is the per-source progress the operator asked for")

    def test_a_source_phase_is_named_by_run_id_not_only_by_source_id(self) -> None:
        """`tecno.ota.checkin` is TWO captures under one source id. A line that
        cannot tell them apart cannot say which one is slow."""
        self.assertNotIn('progress.phase(f"source:{adapter.source_id}")', self.source)

    def test_the_derivations_are_phases_too_not_one_silent_block(self) -> None:
        """The slowest phase on the real corpus is a derivation, not a source:
        identity:bridge-registry, 202.7s of 218.1s."""
        for expected in ("identity:bridge-registry", "identity:automate-review",
                         "dedupe:confirmed-duplicates", "project:current-firmware",
                         "check:corpus-invariants", "record:corpus-identity"):
            with self.subTest(phase=expected):
                self.assertIn(expected, self.source)

    def test_the_count_of_phases_reaches_the_finishing_line(self) -> None:
        self.assertIn("batch finished phases=", inspect.getsource(batch.main))

    def test_run_batch_sh_still_runs_python_unbuffered(self) -> None:
        """Line buffering in batch_logging is one of two safeguards; `-u` is the
        other, and a progress log that arrives at process exit describes a hang
        that is already over."""
        script = (ROOT / "scheduling" / "run-batch.sh").read_text()
        self.assertIn("python3 -u", script.replace("python -u", "python3 -u"))


class ThePhaseLinesReachDiskWhileTheRunIsStillGoing(unittest.TestCase):
    """The whole point. Proven the way tests/test_batch_logging.py proves its
    own case: by reading the file WHILE the process is alive, because normal
    interpreter shutdown flushes everything regardless of buffering."""

    def test_a_phase_line_is_on_disk_before_the_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "progress.log"
            script = (
                "import sys, time; sys.path.insert(0, %r)\n"
                "from pathlib import Path\n"
                "from mobile_observatory.batch_logging import configure_batch_logging, ProgressLog\n"
                "logger = configure_batch_logging(Path(%r))\n"
                "progress = ProgressLog(logger, heartbeat_seconds=0)\n"
                "with progress.phase('source:first') as phase:\n"
                "    phase[0] = {'valid': 7}\n"
                "time.sleep(6)\n"
            ) % (str(ROOT / "src"), str(log))
            process = subprocess.Popen([sys.executable, "-c", script],
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.monotonic() + 5
                seen = ""
                while time.monotonic() < deadline:
                    if log.exists():
                        seen = log.read_text()
                        if "source:first" in seen:
                            break
                    time.sleep(0.05)
                self.assertIsNone(process.poll(),
                                  "the process exited; this test then proves nothing about "
                                  "buffering, only about interpreter shutdown")
                self.assertIn("valid=7", seen)
            finally:
                process.kill()
                process.wait(10)
                if process.stdout is not None:
                    process.stdout.close()


if __name__ == "__main__":
    unittest.main()
