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
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


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
