# Changesets — undoing one batch without copying 246 MB

Added 2026-09-30. Additive: `tools/backup_evidence.py` is unchanged and still
does what `docs/BACKUP.md` describes. This is a different job.

## The problem it addresses

`docs/BACKUP.md` establishes that **a rebuild is not a restore** — 201 of 960
devices resolve differently, 1,111 identity conclusions are frozen under
`RULE_VERSION 1`, and 1,316 observations rest on input bytes no longer on disk.
HANDOFF.md's open list then said, of that divergence, that *nothing detects it*.

So before a batch the only available rollback was a hand-taken copy of
`.observatory-data` — 246 MB of `corpus.sqlite`, 435 MB of directory — and after
a batch there was no record of what the run had actually changed.

## What a changeset is

SQLite's session extension records a **changeset**: a compact, invertible diff of
the rows a transaction changed. The batch now records one per run into
`<data-dir>/changesets/`, beside a `.json` naming what it touched.

Measured on a copy of the live corpus, a full steady-state batch re-run:

| | bytes | |
|---|---|---|
| `corpus.sqlite` | 265,842,688 | the snapshot ritual it supplements |
| one batch's changeset | 289,448 | **918× smaller** |

Inverting that changeset restores every tracked table to a byte-identical
content digest — verified over all 57 tables, not sampled.

## The three things it cannot do

Each is a measurement, not a caveat added for safety.

**1. Rows, not schema.** A changeset holds row changes. A migration that creates
a table or drops an index is not in it and inverting will not undo it. Every
recording carries a schema digest from before and after, so `schema_changed` is
reported; `tools/corpus_changeset.py revert` refuses such a run unless
`--schema-moved` is passed.

**2. Only tables with a PRIMARY KEY.** The session extension identifies rows by
primary key. A table without one is not recorded — and not complained about. On
this corpus that is exactly one table: **`identity_resolution_rationales`**,
which is where every automated identity conclusion records *why* it was reached.
A revert leaves it untouched. `check_corpus` reports this as the warning
`table_absent_from_every_changeset` so the blind spot is visible rather than
discovered during a rollback.

Virtual tables are **not** in that list even though `PRAGMA table_info` shows no
primary key for them: an FTS5 table stores its content in `*_data`, `*_content`,
`*_idx`, `*_docsize` and `*_config` shadow tables, all of which have primary keys
and are tracked. `tests/test_changesets.py` asserts the shadow rows really do
travel in the changeset.

**3. Net effect, not history.** Insert-then-update collapses into one insert of
the final value. That is what makes it compact; it also means a changeset is not
an audit log of the steps a run took.

## Does this replace the snapshot ritual?

**No, and it is not trying to.** They answer different questions:

| | a changeset | `backup_evidence.py` | a directory copy |
|---|---|---|---|
| undo the last batch | yes, 0.28 MB | no | yes, 435 MB |
| survive losing the directory | **no** | yes, 2.0 MB | yes |
| rebuild from captured evidence | no | yes | yes |

A changeset only has meaning against the exact corpus it was recorded on. Lose
`corpus.sqlite` and every changeset beside it becomes unusable, which is why
`backup_evidence.py` remains the thing that matters for durability.

What it **can** replace is the *pre-batch* 246 MB copy taken only so the run
could be undone. For that specific purpose it is 918× cheaper and strictly more
informative, because it also says what changed. Keep the evidence archive.

## Using it

```sh
python3 tools/corpus_changeset.py list   --data-dir .observatory-data
python3 tools/corpus_changeset.py show   --data-dir .observatory-data <name>
python3 tools/corpus_changeset.py revert --data-dir .observatory-data <name>
```

`revert` applies the inverse and **refuses unless it applies cleanly and in
full**. A conflict — the corpus has moved on since the changeset was recorded —
rolls the whole application back and changes nothing, because half a rollback
leaves a state no run ever produced.

## How the C API is reached, and why that is not a dependency

Python 3.12.3 binds no session API:

```
>>> sqlite3.Connection.create_session
AttributeError: type object 'sqlite3.Connection' has no attribute 'create_session'
```

The symbols are nonetheless exported from the `libsqlite3.so.0` that `_sqlite3`
is already linked against, because this build sets `SQLITE_ENABLE_SESSION`. So
`src/mobile_observatory/changesets.py` calls them through stdlib `ctypes`. No
package is installed and no second SQLite is loaded — it is the same library the
`sqlite3` module is already using.

The one private thing it relies on is where CPython keeps the `sqlite3 *` handle
inside its connection object. That is never assumed: `session_support()` verifies
the offset **in a subprocess** against a database whose filename it already
knows, so a wrong guess costs a dead child process and a reported reason rather
than taking the batch down. If anything about that check fails, the batch runs
exactly as it did before and says why it recorded nothing.

If CPython ever binds the session API properly, `tests/test_changesets.py`
fails on purpose, and the ctypes layer should be deleted rather than kept.
