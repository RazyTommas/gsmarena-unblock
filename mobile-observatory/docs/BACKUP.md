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

So the corpus is an *archive* whose inputs move, not a *cache* of them.

**And a rebuild is not only loss, which this document used to imply.** Measured
2026-10-03 on this corpus against a full cold rebuild from the same inputs:

| | live | rebuilt |
|---|---|---|
| devices | 865 | 854 (759 identical, 106 only live, 95 only rebuilt) |
| identity conclusions by rule version | **v1 1,209** + v2 1,158 | v2 2,335, none at v1 |
| product_firmware_releases | 29,921 | **34,862** (+4,941) |
| artifact digests the other does not have | 7 | — |

The mechanism, not just the size: **1,209 conclusions are frozen at
`RULE_VERSION 1` and a rebuild produces only v2**, and v2 strips the brand
prefix -- so **87 of the 106 codes only the live corpus has are literally
`<BRAND> <a code the rebuild does have>`**, the same device under its older
spelling rather than a device that vanished. (The earlier figure of 1,111 frozen
conclusions was measured before later batches; 1,209 is today's.)

Losing this corpus costs those frozen decisions and the inputs that are no longer
on disk; it also means giving up 4,941 product firmware releases a rebuild would
have added. Either way it should be a decision someone makes knowingly, not one
they discover during an incident.

### And now something detects it

Until 2026-10-03 nothing did, so a rebuilt corpus could be handed over as a
restored one and every page would agree with itself.

Every batch records `<data-dir>/corpus-identity.json`: a digest per subject over
the four things that make this corpus *this* corpus —

| component | what it pins | on the live corpus |
| --- | --- | --- |
| `identity_conclusions` | what was concluded about each product, under which frozen rule version | 2,367 subjects |
| `source_identity_registry` | which product each captured source identity resolves to | 3,733 |
| `hardware_models` | the published device catalogue, where the 201 shows up | 865 |
| `artifacts` | the captured input *versions* it rests on | 21 |

Measured: 19ms to compute, 0.44 MB on disk, 3ms to compare.

`check_corpus` compares the corpus against that record and reports
`corpus_no_longer_matches_its_recorded_identity` (**error**) naming the subjects
that went missing. `PYTHONPATH=src python3 -m mobile_observatory.corpus_identity compare
--identity-baseline <path>` asks the question directly — point it at the baseline
from the corpus you believe you reproduced.

Keyed by **what a product IS** -- `(manufacturer, normalized_name)`, which is
`source_products`' own UNIQUE constraint -- and not by its row id. The row id was
the obvious key and was wrong: `merge_confirmed_duplicates` runs every batch and
repoints duplicates at a survivor, so a conclusion about the same device moves to
a different id. Measured on two consecutive batches over one fresh corpus, keyed
by id: **464 conclusions "forgotten" and 464 "added" with the total unchanged** --
an ordinary night reading as mass amnesia. Keyed by identity: **0 errors**.

**The test is containment, not equality,** and that is the whole design. A
nightly batch legitimately adds conclusions, registry rows and devices; an
equality test would fire every night and be switched off within a week. What a
batch never does is *forget*, because a concluded identity is final by design. So
a subject the baseline recorded and this corpus no longer has is the divergence;
an addition is counted and reported as an addition.

Measured on copies of the live corpus:

| posture | finding |
| --- | --- |
| the same corpus, against its own baseline | none |
| 40 devices added, nothing forgotten (a nightly batch) | none |
| 106 recorded devices gone, 95 new, 7 conclusions re-decided, 11 dropped, 6 input digests gone | **error**, 130 subjects, naming model codes `21091116UI`, `2210129SG`, `24053PY09C`, … |
| no baseline recorded at all | warning — *reported*, never silence |

The baseline is a sidecar and not a table in `corpus.sqlite` on purpose: a
rebuild creates a new `corpus.sqlite`, so a baseline stored inside it would be
destroyed by the very event it exists to detect. It travels with `cp -a
.observatory-data` and inside this tool's archive.

`docs/CHANGESETS.md` is a different and narrower job and the two must not be
confused: a changeset makes a *write* reversible. It never compares a rebuild
against this corpus.

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

**Restore into a DIFFERENT directory from the one you backed up.** The drill
here used to restore `/tmp/drill` back into `/tmp/drill`, and that version of it
could never fail the way a real recovery does: `artifacts.storage_uri` is an
absolute path, so restoring into the directory it already names hides the fact
that nothing repoints it. Measured on the old code, restoring into a new
directory: **1 of 1 artifact file restored, and the corpus still citing
`/tmp/.../data/evidence/artifacts/a.bin` on a box that no longer has it.** A
drill that restores in place is a drill that tests the happy path of the one
disaster it exists for.

```sh
# on a COPY of the data directory, never the live one
python3 tools/backup_evidence.py --data-dir /tmp/drill --output /tmp/drill.tar.gz

# a DIFFERENT directory, standing in for a different box
mkdir -p /tmp/drill-elsewhere
cp /tmp/drill/corpus.sqlite /tmp/drill-elsewhere/     # you carried the corpus; the
rm -rf /tmp/drill                                     # original box is gone
python3 tools/backup_evidence.py --restore /tmp/drill.tar.gz --data-dir /tmp/drill-elsewhere
```

Read `evidence_paths` in the restore's own report: `rebased` is how many artifact
rows were repointed at where the bytes now are, and **`unresolved_count` must be
0**. A nonzero count is a corpus citing bytes it cannot produce, and it is named
rather than summarised so you can see which.

A data directory that was **moved rather than restored** has the same problem and
no archive to fix it from. `python3 tools/backup_evidence.py --rebase --data-dir
<the moved directory>` runs the same rule over what is already on disk, and exits
nonzero if anything is still unresolved.

Exercised on 2026-09-28 against a mirror of the live data directory. Before
loss: 21 artifacts resolvable. After deleting every irreplaceable byte: 20
missing. After restore: **21 resolvable, 0 missing, 0 corrupt, 5,785
acknowledgements and 2 watches back**, and the restored directory served 865
devices. That drill restored in place and so did not exercise the rebase; the
different-directory form above was added 2026-10-03 with the rebase itself.

`tests/test_backup_evidence.py` runs the same round trip against a real loss
rather than only verifying an archive's internal consistency — which would prove
the archive is self-consistent, not that anyone can recover with it.
