"""A process-death-safe, nonblocking worker lock (also supported on Windows)."""
from contextlib import contextmanager
from pathlib import Path
import os


@contextmanager
def exclusive_worker(path: Path, holder: str = 'A collection worker'):
    """Hold `path` exclusively for the duration of the block.

    `holder` names what is holding it, and appears verbatim at the start of
    the error raised when a second process finds it taken -- so it carries its
    own article. It is a parameter because the batch takes this lock too, and
    reporting "A collection worker is already active" when the real holder is
    last night's still-running ingest sends whoever reads the log to the wrong
    process.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f'{holder} is already active; retry after it finishes.') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
