"""Line-buffered logging for the scheduled batch run.

A backgrounded/scheduled process's stdout is not a TTY, and CPython
block-buffers a non-TTY stream (default 8KB) unless told otherwise. A process
that is killed, times out, or crashes before that buffer fills writes nothing
to its log -- and a truncated cron/systemd log looks exactly like a source
that produced nothing, which is precisely the failure mode this project is
trying to detect, not add. See docs/SOURCE_SILENCE_DETECTION.md.

Two independent safeguards are applied, deliberately redundant:
  1. the log file is opened with `buffering=1` (line buffering), so every
     newline is a flush regardless of what writes to it;
  2. `logging.StreamHandler.emit` calls `self.flush()` after every record.

Verify this by checking the log file's byte size while the process is still
running -- not by checking the process exited cleanly (see tests/test_batch_logging.py).
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

#: A phase quiet for longer than this gets a heartbeat line. See ProgressLog.
DEFAULT_HEARTBEAT_SECONDS = 60.0
#: How many count fields one phase line may carry. A phase returning forty
#: counters must not produce a forty-field log line.
MAX_COUNT_FIELDS = 5


def configure_batch_logging(log_path: Path | str, *, logger_name: str = "mobile_observatory.batch") -> logging.Logger:
    """Configure and return a logger that writes line-buffered to `log_path`
    and (if stdout is attached) also to stdout, for `tail -f` during a manual run.

    Safe to call more than once per process: existing handlers on this logger
    are replaced, not stacked.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT)

    file_stream = open(log_path, "a", buffering=1, encoding="utf-8")
    file_handler = logging.StreamHandler(file_stream)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    logger.propagate = False
    return logger


def summarise_counts(value, *, limit: int = MAX_COUNT_FIELDS) -> str:
    """A compact `key=n` summary of whatever a batch step returned.

    Derived from the step's OWN return value rather than hand-picked per call
    site, so a step whose result shape changes keeps logging a count instead of
    quietly logging none. Only integers are reported: a step's counts are what
    an operator needs mid-run, and a path or a digest on a progress line is
    noise that pushes the number off the end.

    Truncation is STATED. `(+3 more)` rather than silently showing five of eight
    -- presenting a truncated list as complete is the one thing this project
    refuses to do, and a log line is not exempt from it.
    """
    if value is None:
        return "count=none-returned"
    if isinstance(value, bool):
        return f"ok={value}"
    if isinstance(value, int):
        return f"count={value}"
    if isinstance(value, (list, tuple, set, frozenset)):
        return f"items={len(value)}"
    fields = value if isinstance(value, dict) else getattr(value, "__dict__", None)
    if not isinstance(fields, dict):
        return "count=not-a-count"
    numbers = [(k, v) for k, v in fields.items()
               if isinstance(v, int) and not isinstance(v, bool)]
    if not numbers:
        # A step whose result carries no number at all. Saying so beats an empty
        # tail that reads as "zero of everything".
        return "count=none-stated"
    shown = numbers[:limit]
    text = " ".join(f"{k}={v}" for k, v in shown)
    if len(numbers) > limit:
        text += f" (+{len(numbers) - limit} more)"
    return text


class ProgressLog:
    """One line per batch phase, with a count and an elapsed time.

    WHY THIS EXISTS. The batch emitted exactly two lines for a ten-minute run:
    "batch starting" and "batch finished totals=...". In production, ten minutes
    of silence is indistinguishable from a hang, and an operator watching it had
    no way to tell a slow source from a wedged one -- not which source, not how
    far in, not whether anything had been ingested at all.

    WHAT IT DELIBERATELY IS NOT. It is not per-record progress and not a
    start-line/end-line pair. One line per phase, written when the phase ENDS,
    carrying the phase's own counts: ~28 lines for the whole run against the two
    there were. Two lines per phase would double a log somebody has to read at
    3am to find the one line that matters.

    THE HEARTBEAT IS THE PART THAT ANSWERS THE ACTUAL QUESTION. A completion
    line tells you a phase finished; it cannot tell you that the phase you are
    waiting on is still alive. So a phase still open after
    `heartbeat_seconds` says so, repeatedly, naming itself and how long it has
    been in there. A normal run on today's corpus emits NONE of these -- the
    longest phase is seconds -- so this costs nothing until something is wrong,
    which is the only time anybody reads it.

    `run-batch.sh` runs python with `-u` and configure_batch_logging flushes
    every record, so these lines are on disk as they happen rather than when the
    process exits. That is the whole point: a buffered progress log is a log that
    arrives after the hang it was describing.

    A `logger` of None makes every method a no-op, for the tests and callers that
    drive the ingest without a configured log.
    """

    def __init__(self, logger: logging.Logger | None, *,
                 heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
                 clock=time.monotonic) -> None:
        self._logger = logger
        self._heartbeat = heartbeat_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._index = 0
        self._started = clock()
        # name, index, really-started, when-the-next-heartbeat-is-due. The start
        # and the heartbeat deadline are two fields on purpose: re-arming the
        # beat by moving `started` forward made the completion line report a
        # `took=` one interval short of the truth.
        self._open: tuple[str, int, float, float] | None = None
        self._beat: threading.Thread | None = None
        self._stop = threading.Event()

    # -- the one call site shape -----------------------------------------

    @contextmanager
    def phase(self, name: str):
        """Run one phase and log a single line for it.

        Yields a one-element list the caller may fill with the phase's result;
        whatever is in it at exit becomes the line's count. A list rather than a
        return value because the interesting counts are usually produced by the
        call inside the block, and a context manager cannot see that.

        A phase that RAISES still logs, naming the exception type, before the
        exception continues on its way. A ten-minute run that dies in the middle
        has to say which phase it died in; the traceback says where in the code
        and not which of twelve sources.
        """
        if self._logger is None:
            yield [None]
            return
        with self._lock:
            self._index += 1
            index = self._index
            started = self._clock()
            self._open = (name, index, started, started + self._heartbeat)
        self._ensure_heartbeat()
        holder: list = [None]
        try:
            yield holder
        except BaseException as exc:
            self._line(name, index, started, f"FAILED {type(exc).__name__}")
            raise
        else:
            self._line(name, index, started, summarise_counts(holder[0]))
        finally:
            with self._lock:
                if self._open and self._open[1] == index:
                    self._open = None

    def close(self) -> None:
        """Stop the heartbeat thread. Safe to call more than once."""
        self._stop.set()
        beat, self._beat = self._beat, None
        if beat is not None:
            beat.join(timeout=2 * self._heartbeat + 1)

    # -- internals --------------------------------------------------------

    @property
    def phases(self) -> int:
        """How many phases have been entered. Reported on the finishing line
        instead of as a per-line denominator, because the total is not knowable
        when the first phase logs and a hand-maintained one can be wrong."""
        with self._lock:
            return self._index

    def _line(self, name: str, index: int, started: float, tail: str) -> None:
        self._logger.info("phase %d %s %s took=%.1fs elapsed=%.1fs",
                          index, name, tail,
                          self._clock() - started, self._clock() - self._started)

    def _ensure_heartbeat(self) -> None:
        if self._beat is not None or self._heartbeat <= 0 or self._logger is None:
            return
        # Daemon: a heartbeat thread must never be the reason a finished batch
        # does not exit. close() joins it anyway, so the daemon flag is the
        # backstop for a caller that forgets rather than the normal path.
        self._beat = threading.Thread(target=self._pulse, name="batch-progress",
                                      daemon=True)
        self._beat.start()

    def _pulse(self) -> None:
        # Waits in short slices rather than one long sleep, so close() returns
        # promptly instead of after a whole heartbeat interval.
        slice_seconds = min(1.0, max(0.05, self._heartbeat / 10))
        while not self._stop.wait(slice_seconds):
            with self._lock:
                current = self._open
            if current is None:
                continue
            name, index, started, due = current
            now = self._clock()
            if now < due:
                continue
            self._logger.info(
                # "phase", not "source": the first heartbeat this ever printed on
                # the real corpus named identity:bridge-registry, which is a
                # derivation and not a source. A line that calls a derivation a
                # source sends an operator to look at a publisher.
                "phase %d %s still running after %.0fs (elapsed %.0fs); no count yet -- "
                "this phase has not returned", index, name, now - started,
                now - self._started)
            with self._lock:
                # Re-arm one interval out, so the next line comes an interval
                # later rather than on every wait slice.
                if self._open and self._open[1] == index:
                    self._open = (name, index, started, now + self._heartbeat)
