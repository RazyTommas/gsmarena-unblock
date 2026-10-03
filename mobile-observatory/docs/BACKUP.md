# Backup

## What is not backed up, and why

`corpus.sqlite` is 243 MB and almost all of it is derived. The captured inputs
under `crawler/relay/results` are in git (67 files, 24 MB). The code is in git.
A batch rebuilds the database from them in about thirteen minutes — verified by
unpacking a bundle into a directory that had never seen this repository and
running the batch there with `env -i`.

Copying 243 MB nightly to protect something regenerable is waste. So it isn't.

## A rebuild is not a restore

That distinction is the whole reason this document exists, and the difference
was measured rather than assumed.

Rebuilding the live corpus from the current inputs gives **759 of the same
devices, 106 that only the live corpus has, and 95 that only the rebuild has**
— 201 devices resolving differently. Two causes:

**Identity conclusions are remembered.** 1,111 of them are recorded under
`RULE_VERSION 1`. The rule is deliberate: re-deciding every identity on every
run would make the corpus depend on *when* it last ran. The side effect is that
it depends on *the order* it ran in. A rebuild applies today's rules to
everything and reaches different, equally defensible answers.

**The inputs are a moving window; the corpus accumulates.** 1,316 observations
rest on an input version that is no longer on disk — the capture tree is
overwritten in place. Six artifact digests exist in the live corpus and nowhere
in a fresh build.

So the corpus is an *archive* whose inputs move, not a *cache* of them. Losing
it costs those 201 devices and those 1,111 frozen decisions. That is survivable.
It should be a decision someone makes knowingly, not one they discover during an
incident.

## What is backed up

Asked of the corpus, not hardcoded: **every file an `artifacts` row points at
that lives inside the data directory**, plus `local.sqlite`.

| | live measurement |
| --- | --- |
| artifact files inside the data dir | 20 files, 20.6 MB (11 under `evidence/`, 9 under `ledger/raw/`) |
| `local.sqlite` | 0.7 MB — 5,785 acknowledgements, the watch list, collection requests |
| **archive** | **2.0 MB compressed** |
| (not included) `corpus.sqlite` | 243 MB |

Artifact files *outside* the data directory are the repository's own captured
inputs. Git is their backup; copying them would add 24 MB to duplicate
something already versioned.

Deriving the list from the `artifacts` table rather than listing directories is
what keeps this correct when a new source starts writing somewhere new. A backup
that silently stops covering a source is worse than no backup, because it
reports success either way.

### Retention asks the same question, and asks it differently on purpose

The batch prunes `ledger/` on an age and a byte budget now
(`src/mobile_observatory/retention.py`, `docs/SCHEDULING.md`). It never prunes a
file this tool would back up, and it does **not** reuse
`irreplaceable_files()` to decide that.

`irreplaceable_files()` drops a row whose bytes are absent or whose `sha256` no
longer matches -- it records a problem and moves on. That is right for a
manifest and exactly wrong as a veto basis: a cited-but-corrupt artifact is the
file that must not be deleted and is the one missing from this function's
output. Retention builds its veto from the raw `artifacts.storage_uri` and
`artifacts.sha256` columns, a strict superset, and uses this function only to
report.

Measured across a real prune on a copy of the live corpus, with retention set as
aggressively as it can be set (`--retention-days 0 --retention-max-mb 0.000001`):
the cited set is **identical before and after** -- 20 files, 20,630,209 B,
`problems == []` both sides -- while 8 staging/quarantine files and 2,370,293 B
went.

## Running it

```sh
python3 tools/backup_evidence.py --data-dir .observatory-data --output backups/evidence.tar.gz
python3 tools/backup_evidence.py --verify backups/evidence.tar.gz
python3 tools/backup_evidence.py --restore backups/evidence.tar.gz --data-dir /new/box/data
```

`scheduling/run-backup.sh` wraps it with timestamping, a verify of what it just
wrote, and pruning by count (`MOBILE_OBSERVATORY_BACKUP_KEEP`, default 14).
Pruning happens only *after* a successful verify, so a run that produced a bad
archive never deletes a good one. A cron line is in
`scheduling/crontab.example`.

`local.sqlite` is copied through sqlite3's backup API, never `cp`. A plain file
copy of an open database omits its `-wal`, and the copy is then missing every
committed transaction that has not been checkpointed — silently, producing a
file that opens fine and is merely out of date.

## Exit codes

| code | meaning |
| --- | --- |
| 0 | archive written and complete |
| 2 | archive written and verifies, but **does not cover everything** — e.g. `local.sqlite` was absent. Valid, not complete. |
| other | the backup itself failed |

Exit 2 exists because of a defect the restore drill found in this very tool: the
first archive contained no `local.sqlite`, and both backup and restore reported
plain success. Half the thing being protected was missing and the exit code said
fine. A scheduler reads the exit code, not the prose.

## The drill

Run it. A backup nobody has restored is a hypothesis.

```sh
# on a COPY of the data directory, never the live one
python3 tools/backup_evidence.py --data-dir /tmp/drill --output /tmp/drill.tar.gz
find /tmp/drill/evidence /tmp/drill/ledger -type f -delete && rm /tmp/drill/local.sqlite
python3 tools/backup_evidence.py --restore /tmp/drill.tar.gz --data-dir /tmp/drill
```

Exercised on 2026-09-28 against a mirror of the live data directory. Before
loss: 21 artifacts resolvable. After deleting every irreplaceable byte: 20
missing. After restore: **21 resolvable, 0 missing, 0 corrupt, 5,785
acknowledgements and 2 watches back**, and the restored directory served 865
devices.

`tests/test_backup_evidence.py` runs the same round trip against a real loss
rather than only verifying an archive's internal consistency — which would prove
the archive is self-consistent, not that anyone can recover with it.
