"""Record what a write actually changed, invertibly, as a SQLite changeset.

The corpus is an ARCHIVE whose inputs are a moving window, not a cache of them:
a rebuild is not a restore (measured: 201 of 960 devices resolve differently,
1,111 identity conclusions frozen under RULE_VERSION 1, 1,316 observations
resting on input bytes no longer on disk). So the only honest rollback for a
batch was a hand-taken copy of the whole data directory -- 246 MB of
`corpus.sqlite` -- and HANDOFF.md's open list said plainly that nothing
*detects* the divergence.

SQLite's session extension records a changeset: a compact, invertible diff of
the rows a transaction actually changed. `ENABLE_SESSION` is compiled into this
box's libsqlite3, so the capability is present. What is NOT present is a Python
binding for it -- see `session_support()` -- so this module reaches the C API
through stdlib `ctypes`. No new dependency.

WHAT A CHANGESET IS NOT
-----------------------
Three limits, stated here because each one silently narrows what "reversible"
means and none of them is visible in the changeset itself:

1. **Rows, not schema.** A changeset records row changes. A migration that
   creates a table, adds a column or drops an index is not in it, and inverting
   will not undo it. A batch that migrates is only partially reversible, and
   `record_changes()` records the `user_version`/schema digest so the mismatch
   is detectable rather than assumed away.
2. **Only tables with a PRIMARY KEY.** The session extension identifies rows by
   primary key; a table without one is not tracked at all, and it is not
   tracked *silently*. On this corpus that is exactly one table --
   `identity_resolution_rationales` -- which is the table recording WHY each
   automated identity conclusion was reached. `tables_invisible_to_a_changeset()`
   names them and `integrity.check_corpus` reports them, so the blind spot is a
   measurement rather than a surprise.
3. **Net effect, not history.** Insert-then-update collapses to one insert of
   the final value. That is what makes it compact; it also means a changeset is
   not an audit log of the steps taken.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

# sqlite3.h
SQLITE_OK = 0
SQLITE_ROW = 100
SQLITE_DONE = 101
SQLITE_INSERT = 18
SQLITE_UPDATE = 23
SQLITE_DELETE = 9
# sqlite3session.h conflict dispositions
SQLITE_CHANGESET_OMIT = 0
SQLITE_CHANGESET_ABORT = 2

OP_NAMES = {SQLITE_INSERT: "insert", SQLITE_UPDATE: "update", SQLITE_DELETE: "delete"}

# The environment variable that forces the unavailable path. It exists so the
# degraded branch is exercised by a test rather than only reasoned about: a
# fallback nobody runs is a fallback that does not work.
DISABLE_ENV = "MOBILE_OBSERVATORY_NO_SESSION"


@dataclass(frozen=True)
class SessionSupport:
    """Whether a changeset can be recorded here, and if not, exactly why."""

    available: bool
    reason: str

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.available


# --------------------------------------------------------------------------
# reaching the C API
# --------------------------------------------------------------------------
#
# Python 3.12.3 exposes NO session binding:
#
#     >>> sqlite3.Connection.create_session
#     AttributeError: type object 'sqlite3.Connection' has no attribute
#     'create_session'
#
# and `sqlite3` has no `sqlite3session` attribute either. The symbols are
# nonetheless exported from the libsqlite3 that `_sqlite3` is linked against
# (`nm -D` lists sqlite3session_create, sqlite3changeset_invert,
# sqlite3changeset_apply and the rest), so ctypes can call them -- but only with
# the `sqlite3 *` handle, which Python also does not expose.
#
# That handle is the first member of CPython's `pysqlite_Connection` struct,
# immediately after PyObject_HEAD. Reading it is reading a private layout, so it
# is never ASSUMED here: `_probe_handle_offset()` verifies the candidate offset
# IN A SUBPROCESS against a database whose filename it already knows, and a
# wrong guess therefore costs a dead child process and a reported reason instead
# of taking the batch down with it. `sqlite3_db_filename` returning the expected
# path is the confirmation; anything else, including a crash, reads as
# unavailable.

_HANDLE_OFFSET = ctypes.sizeof(ctypes.c_void_p) * 2  # ob_refcnt, ob_type

_PROBE = r"""
import ctypes, os, sqlite3, sys
path = sys.argv[1]
offset = int(sys.argv[2])
lib = ctypes.CDLL(sys.argv[3])
lib.sqlite3_db_filename.restype = ctypes.c_char_p
lib.sqlite3_db_filename.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
conn = sqlite3.connect(path)
conn.execute("create table probe(id integer primary key)")
handle = ctypes.c_void_p.from_address(id(conn) + offset).value
if not handle:
    sys.exit("null handle")
seen = lib.sqlite3_db_filename(ctypes.c_void_p(handle), b"main")
if not seen or os.path.realpath(seen.decode()) != os.path.realpath(path):
    sys.exit("handle does not name the open database")
# and prove the session API accepts it
lib.sqlite3session_create.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                      ctypes.POINTER(ctypes.c_void_p)]
lib.sqlite3session_delete.argtypes = [ctypes.c_void_p]
session = ctypes.c_void_p()
if lib.sqlite3session_create(ctypes.c_void_p(handle), b"main",
                             ctypes.byref(session)) != 0:
    sys.exit("sqlite3session_create refused the handle")
lib.sqlite3session_delete(session)
print("ok")
"""

_library: ctypes.CDLL | None = None
_support: SessionSupport | None = None


def _library_path() -> str:
    """The libsqlite3 that `_sqlite3` is already linked against.

    Loading a DIFFERENT libsqlite3 would hand session functions from one build
    a handle allocated by another -- the same struct name and a different
    layout, which is the kind of mistake that corrupts rather than fails. The
    soname is what the extension module records, so it resolves to the copy
    already mapped into this process.
    """
    return "libsqlite3.so.0"


def _probe_handle_offset() -> str:
    """Empty string if the offset is confirmed, else the reason it was not."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "probe.sqlite")
        try:
            done = subprocess.run(
                [sys.executable, "-c", _PROBE, path, str(_HANDLE_OFFSET), _library_path()],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            return f"could not run the handle probe: {error}"
    if done.returncode != 0 or done.stdout.strip() != "ok":
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        return ("handle probe failed (rc=%d): %s"
                % (done.returncode, detail[-1] if detail else "no output"))
    return ""


def session_support() -> SessionSupport:
    """Measure, once per process, whether changesets can be recorded here."""
    global _library, _support
    if _support is not None:
        return _support
    if os.environ.get(DISABLE_ENV):
        _support = SessionSupport(False, f"disabled by {DISABLE_ENV}")
        return _support
    if sys.implementation.name != "cpython":
        _support = SessionSupport(
            False, f"needs CPython's connection layout, running on {sys.implementation.name}")
        return _support
    try:
        library = ctypes.CDLL(_library_path())
    except OSError as error:
        _support = SessionSupport(False, f"cannot load {_library_path()}: {error}")
        return _support
    try:
        library.sqlite3session_create
    except AttributeError:
        _support = SessionSupport(
            False, f"{_library_path()} exports no sqlite3session_create "
                   "(built without SQLITE_ENABLE_SESSION)")
        return _support
    reason = _probe_handle_offset()
    if reason:
        _support = SessionSupport(False, reason)
        return _support
    _bind(library)
    _library = library
    _support = SessionSupport(True, "sqlite3session via ctypes")
    return _support


def _bind(lib: ctypes.CDLL) -> None:
    v, i, cp, pv = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)
    pi = ctypes.POINTER(ctypes.c_int)
    lib.sqlite3_db_filename.restype = cp
    lib.sqlite3_db_filename.argtypes = [v, cp]
    lib.sqlite3_errmsg.restype = cp
    lib.sqlite3_errmsg.argtypes = [v]
    lib.sqlite3_free.argtypes = [v]
    lib.sqlite3session_create.argtypes = [v, cp, pv]
    lib.sqlite3session_attach.argtypes = [v, cp]
    lib.sqlite3session_changeset.argtypes = [v, pi, pv]
    lib.sqlite3session_delete.argtypes = [v]
    lib.sqlite3session_isempty.argtypes = [v]
    lib.sqlite3changeset_invert.argtypes = [i, v, pi, pv]
    lib.sqlite3changeset_apply.argtypes = [v, i, v, v, v, v]
    lib.sqlite3changeset_start.argtypes = [pv, i, v]
    lib.sqlite3changeset_next.argtypes = [v]
    lib.sqlite3changeset_op.argtypes = [v, ctypes.POINTER(cp), pi, pi, pi]
    lib.sqlite3changeset_finalize.argtypes = [v]


def _handle(connection) -> ctypes.c_void_p:
    """The `sqlite3 *` behind a Python connection, re-verified per use.

    The subprocess probe confirms the offset for this interpreter; this confirms
    the pointer for this connection, so a caller cannot hand us a closed or
    foreign object and have it read as a valid database.
    """
    support = session_support()
    if not support.available:
        raise ChangesetUnavailable(support.reason)
    assert _library is not None
    pointer = ctypes.c_void_p.from_address(id(connection) + _HANDLE_OFFSET).value
    if not pointer:
        raise ChangesetUnavailable("connection has no open database handle")
    if _library.sqlite3_db_filename(ctypes.c_void_p(pointer), b"main") is None:
        raise ChangesetUnavailable("connection handle does not name a main database")
    return ctypes.c_void_p(pointer)


class ChangesetUnavailable(RuntimeError):
    """The session API could not be reached. Carries the measured reason."""


class ChangesetConflict(RuntimeError):
    """A changeset did not apply cleanly, so NOTHING was applied."""


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------


@dataclass
class Recording:
    """What one recorded write changed."""

    changeset: bytes = b""
    tables_untracked: tuple[str, ...] = ()
    schema_digest_before: str = ""
    schema_digest_after: str = ""
    error: str = ""

    @property
    def bytes(self) -> int:
        return len(self.changeset)

    @property
    def schema_changed(self) -> bool:
        """True when the write also changed the schema, which a changeset cannot
        invert. Reported, never silently tolerated."""
        return self.schema_digest_before != self.schema_digest_after

    def as_dict(self) -> dict:
        summary = summarise(self.changeset) if self.changeset else {}
        return {"bytes": self.bytes,
                "tables_changed": len(summary),
                "changes": summary,
                "tables_untracked": list(self.tables_untracked),
                "schema_changed": self.schema_changed,
                "schema_digest_before": self.schema_digest_before,
                "schema_digest_after": self.schema_digest_after,
                "error": self.error}


def schema_digest(connection) -> str:
    """A digest of the schema, so a schema change is detectable at revert time."""
    digest = hashlib.sha256()
    for name, sql in connection.execute(
            "SELECT name, coalesce(sql,'') FROM sqlite_schema ORDER BY name"):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(sql.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def tables_invisible_to_a_changeset(connection) -> list[str]:
    """Tables the session extension cannot track: no declared PRIMARY KEY.

    Not an estimate and not a static list -- asked of the schema, so a table
    added later without a key is reported by the check that reads this rather
    than quietly joining the blind spot.

    VIRTUAL tables are excluded, and that exclusion is a measurement rather than
    an assumption. `PRAGMA table_info` reports no primary key for an FTS5 table,
    so a naive reading lists `firmware_release_search` here -- but an FTS5 table
    stores nothing itself: its content lives in `*_data`, `*_content`, `*_idx`,
    `*_docsize` and `*_config` shadow tables, every one of which HAS a primary
    key and is therefore tracked. Listing the virtual table would report covered
    data as a blind spot, and a check that cries wolf is a check that gets
    ignored. tests/test_changesets.py asserts the shadow rows really do travel
    in the changeset rather than taking this paragraph's word for it.
    """
    virtual = {name for (name,) in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' "
        "AND sql LIKE 'CREATE VIRTUAL TABLE%'")}
    invisible = []
    for (name,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if name in virtual:
            continue
        info = connection.execute(f'PRAGMA table_info("{name}")').fetchall()
        if info and not any(row[5] for row in info):
            invisible.append(name)
    return invisible


@contextmanager
def record_changes(connection) -> Iterator[Recording]:
    """Record every tracked row change made on `connection` inside this block.

    The recording is filled in on exit, so callers read `recording.changeset`
    after the block. Attaches ALL tables, including tables created inside the
    block -- `sqlite3session_attach(session, NULL)` is a standing instruction,
    not a snapshot of the table list.

    The changeset is captured even when the block raises, and the exception is
    then re-raised unchanged. A batch that dies half way is precisely the run
    somebody needs to undo: this ingest commits incrementally, so a crash leaves
    committed work behind, and a recorder that only produced a diff for runs
    that succeeded would be missing on the one occasion it was wanted.
    """
    handle = _handle(connection)
    assert _library is not None
    lib = _library
    session = ctypes.c_void_p()
    rc = lib.sqlite3session_create(handle, b"main", ctypes.byref(session))
    if rc != SQLITE_OK:
        raise ChangesetUnavailable(
            f"sqlite3session_create failed (rc={rc}): "
            f"{lib.sqlite3_errmsg(handle).decode(errors='replace')}")
    recording = Recording(tables_untracked=tuple(tables_invisible_to_a_changeset(connection)),
                          schema_digest_before=schema_digest(connection))
    try:
        rc = lib.sqlite3session_attach(session, None)
        if rc != SQLITE_OK:
            raise ChangesetUnavailable(f"sqlite3session_attach failed (rc={rc})")
        try:
            yield recording
        finally:
            # In a `finally`, so a raising block still yields its diff. Failures
            # to COLLECT are recorded on the Recording rather than raised: the
            # caller's own exception, if there is one, is the more important
            # news and must not be replaced by ours.
            size = ctypes.c_int()
            buffer = ctypes.c_void_p()
            rc = lib.sqlite3session_changeset(session, ctypes.byref(size), ctypes.byref(buffer))
            if rc != SQLITE_OK:
                recording.error = f"sqlite3session_changeset failed (rc={rc})"
            else:
                try:
                    recording.changeset = (
                        ctypes.string_at(buffer, size.value) if size.value else b"")
                finally:
                    lib.sqlite3_free(buffer)
                recording.schema_digest_after = schema_digest(connection)
    finally:
        lib.sqlite3session_delete(session)


# --------------------------------------------------------------------------
# reading, inverting, applying
# --------------------------------------------------------------------------


def summarise(changeset: bytes) -> dict[str, dict[str, int]]:
    """Per table, how many rows the changeset inserts, updates and deletes."""
    if not changeset:
        return {}
    session_support()
    assert _library is not None
    lib = _library
    iterator = ctypes.c_void_p()
    rc = lib.sqlite3changeset_start(ctypes.byref(iterator), len(changeset),
                                    ctypes.c_char_p(changeset))
    if rc != SQLITE_OK:
        raise ChangesetUnavailable(f"sqlite3changeset_start failed (rc={rc})")
    counts: dict[str, dict[str, int]] = {}
    try:
        while lib.sqlite3changeset_next(iterator) == SQLITE_ROW:
            table = ctypes.c_char_p()
            columns = ctypes.c_int()
            operation = ctypes.c_int()
            indirect = ctypes.c_int()
            rc = lib.sqlite3changeset_op(iterator, ctypes.byref(table), ctypes.byref(columns),
                                         ctypes.byref(operation), ctypes.byref(indirect))
            if rc != SQLITE_OK:
                raise ChangesetUnavailable(f"sqlite3changeset_op failed (rc={rc})")
            name = (table.value or b"?").decode(errors="replace")
            bucket = counts.setdefault(name, {"insert": 0, "update": 0, "delete": 0})
            bucket[OP_NAMES.get(operation.value, "insert")] += 1
    finally:
        lib.sqlite3changeset_finalize(iterator)
    return counts


def invert(changeset: bytes) -> bytes:
    """The changeset that undoes `changeset`: inserts become deletes and back."""
    if not changeset:
        return b""
    session_support()
    assert _library is not None
    lib = _library
    size = ctypes.c_int()
    buffer = ctypes.c_void_p()
    rc = lib.sqlite3changeset_invert(len(changeset), ctypes.c_char_p(changeset),
                                     ctypes.byref(size), ctypes.byref(buffer))
    if rc != SQLITE_OK:
        raise ChangesetUnavailable(f"sqlite3changeset_invert failed (rc={rc})")
    try:
        return ctypes.string_at(buffer, size.value) if size.value else b""
    finally:
        lib.sqlite3_free(buffer)


def apply_changeset(connection, changeset: bytes) -> None:
    """Apply `changeset`, or change nothing at all.

    The conflict handler ABORTs, which rolls the whole application back. That is
    the point: a revert against a corpus that has moved on since the changeset
    was recorded must refuse, not merge. Half a rollback is worse than none --
    it would leave a state no run ever produced.

    Foreign keys are deferred to commit rather than disabled. An inverted
    changeset deletes parents and children in table order, which transiently
    violates constraints that hold at both ends; deferring checks them where
    they matter, so a revert that really would orphan a row still fails.
    """
    if not changeset:
        return
    handle = _handle(connection)
    assert _library is not None
    lib = _library
    filter_cb = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p)(
        lambda context, name: 1)
    conflicts: list[int] = []

    def on_conflict(context, kind, iterator):
        conflicts.append(kind)
        return SQLITE_CHANGESET_ABORT

    conflict_cb = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                                   ctypes.c_void_p)(on_conflict)
    connection.execute("PRAGMA defer_foreign_keys = ON")
    try:
        rc = lib.sqlite3changeset_apply(handle, len(changeset), ctypes.c_char_p(changeset),
                                        ctypes.cast(filter_cb, ctypes.c_void_p),
                                        ctypes.cast(conflict_cb, ctypes.c_void_p), None)
    finally:
        connection.execute("PRAGMA defer_foreign_keys = OFF")
    if rc != SQLITE_OK:
        message = lib.sqlite3_errmsg(handle)
        raise ChangesetConflict(
            f"changeset did not apply and nothing was changed (rc={rc}, "
            f"conflicts={len(conflicts)}): "
            f"{message.decode(errors='replace') if message else 'no message'}")


# --------------------------------------------------------------------------
# the proof instrument
# --------------------------------------------------------------------------


def table_digests(connection, *, tables: Sequence[str] | None = None) -> dict[str, str]:
    """A content digest per table: the instrument for "the corpus came back".

    Compares CONTENT, not the file. Two SQLite files holding identical rows
    differ byte for byte -- page order, freelist, the WAL -- so a file hash
    would report a correct revert as a failure and teach whoever ran it to stop
    believing the check. Rows are ordered by their full value tuple so the
    digest does not depend on physical order either.
    """
    names = list(tables) if tables is not None else [
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    digests: dict[str, str] = {}
    for name in names:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]
        if not columns:
            continue
        quoted = ", ".join(f'"{column}"' for column in columns)
        digest = hashlib.sha256()
        for row in connection.execute(f'SELECT {quoted} FROM "{name}" ORDER BY {quoted}'):
            for value in row:
                digest.update(repr(value).encode())
                digest.update(b"\x1f")
            digest.update(b"\x1e")
        digests[name] = digest.hexdigest()
    return digests


# --------------------------------------------------------------------------
# storing them beside the corpus
# --------------------------------------------------------------------------


class ChangesetStore:
    """Changesets on disk, newest last, each beside its own metadata.

    Additive: `tools/backup_evidence.py` keeps doing what it does. A changeset
    reverses the LAST write; the evidence archive is what survives losing the
    directory. Neither replaces the other -- see docs/CHANGESETS.md.
    """

    SUFFIX = ".changeset"

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def write(self, recording: Recording, *, run: str, extra: dict | None = None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem = f"{stamp}-{run}"
        path = self.directory / f"{stem}{self.SUFFIX}"
        path.write_bytes(recording.changeset)
        meta = {"run": run, "recorded_at": stamp, "changeset": path.name,
                "sha256": hashlib.sha256(recording.changeset).hexdigest(),
                **recording.as_dict()}
        if extra:
            meta.update(extra)
        (self.directory / f"{stem}.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def entries(self) -> list[dict]:
        entries = []
        for path in sorted(self.directory.glob(f"*{self.SUFFIX}")):
            meta_path = path.with_suffix(".json")
            meta = {}
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except ValueError:
                    meta = {"error": "metadata is not readable JSON"}
            entries.append({"path": str(path), "bytes": path.stat().st_size, **meta})
        return entries

    def read(self, name: str) -> bytes:
        path = self.directory / name if not os.path.isabs(name) else Path(name)
        return path.read_bytes()
