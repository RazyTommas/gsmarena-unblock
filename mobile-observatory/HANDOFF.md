# Mobile Observatory — handoff

Updated 2026-09-30. Supersedes the 2026-09-17 handoff entirely — that one
described port 8124 and a `/tmp` snapshot that no longer exists.

State: `main` at `f5da9a7`, clean, pushed. 347 tests pass under BOTH
`python3 -m unittest discover -s tests` and `pytest` (291 before the robustness
round below). Corpus: 0 errors, 1 warning.

**Two data-honesty rounds since, on a branch: 478 tests under both runners,
corpus 0 errors / 3 warnings. Migrations 0030 and 0031 have NOT been applied to
`.observatory-data`; a human does that.** See below.

## Three compiled-in SQLite capabilities, 2026-09-30

`ENABLE_SESSION`, `ENABLE_FTS5` and `ENABLE_DBSTAT_VTAB` are all in this box's
libsqlite3 and none was used. **521 tests under both runners** (479 before). No
new dependency: `ctypes` and `sqlite3` only.

**Migration 0032 has NOT been applied to `.observatory-data`; a human does that.**
Until it is, search runs the scan exactly as before — see "cannot go stale".

### a batch is now reversible from a 0.27 MB diff, not a 246 MB copy

The open item below said nothing detects the rebuild/restore divergence. The
batch now records a SQLite **changeset** per run into `<data-dir>/changesets/`.
Measured on a copy of the live corpus, steady-state re-run:

| | bytes | |
|---|---|---|
| `corpus.sqlite` | 265,809,920 | |
| one batch's changeset | **285,184** | **932× smaller** |

Inverted and applied in 0.02s; **64 table content digests compared, 3 changed by
the run, 0 still differing afterwards.** `docs/CHANGESETS.md` has the contract,
`tools/corpus_changeset.py` the `list` / `show` / `revert` commands.

Three limits, each measured rather than asserted:

- **Rows, not schema.** A migration is not in it. `schema_changed` is reported
  and `revert` refuses such a run without `--schema-moved`.
- **Only tables with a PRIMARY KEY.** Exactly one here —
  `identity_resolution_rationales`, the table recording *why* each identity was
  concluded. `check_corpus` reports it as `table_absent_from_every_changeset`
  (warning) so a revert's blind spot is known before it is relied on.
- **Net effect, not history.** Insert-then-update collapses to one insert.

It does **not** replace `tools/backup_evidence.py`: a changeset only has meaning
against the corpus it was recorded on. It replaces the *pre-batch copy taken only
so the run could be undone*.

Python 3.12.3 binds no session API (`Connection.create_session` raises
`AttributeError`), so the C API is reached through `ctypes` against the
libsqlite3 `_sqlite3` already links. The connection-handle offset is verified in
a **subprocess** first, so a wrong guess costs a dead child and a reported reason
rather than the batch.

### an immutable table was being rewritten nightly — found by the revert refusing

The first revert against the live corpus **refused**, naming
`source_data_corrections`. It was right to.

`collectors/device_promotion._record_unresolved` wrote with `INSERT OR REPLACE`
and a random `new_id()`. The table has `UNIQUE(entity_type, entity_id, reason)`,
so every batch collided with itself; REPLACE then deleted the stored row and
inserted a new one. `source_data_corrections_no_delete` exists to forbid exactly
that and **never fired**: SQLite runs a REPLACE's implicit delete *without* delete
triggers unless `PRAGMA recursive_triggers` is on, and it is not.

Measured: **6 of 7 stored refusals deleted and re-inserted on every run**, each
time with a new primary key and a new `recorded_at` — so "when was this refusal
first reached" answered with last night's clock. Now `ON CONFLICT … DO NOTHING`
with a derived id, matching `source_corrections._audit`, the other writer to the
same table. `tests/test_corrections_are_append_only.py` also scans the source so
no writer can spell it `REPLACE` again.

### search: FTS5 narrows, LIKE still decides

`/api/v1/search` was ~53ms of releases in a ~68ms payload, and `q=%` — matching
nothing — still cost 51ms, so the cost was scanning 21,186 releases through the
view's four joins, not evaluating the LIKE.

An FTS5 **trigram** index now proposes candidates; every `database.like_clause`
predicate is unchanged and still decides every match, so results are identical
**by construction**. Compared against the scan over 58 queries on the live
corpus, totals *and* rows: **0 disagreements**, including all five from the
wildcard round (`_` → 60/4/0, `%` → 0/0/0, `TECNO_W4` → 1 device, `5G` →
99/28/1357, `i3` → 1/0/39).

Releases part, sum over those 58: **3,251ms → 1,221ms (−62%)**; whole payload
3,952ms → 1,900ms (−52%). Index costs **5.59 MB, 2.23% of the corpus**.

Where it does not help, and this is not a corner:

- **1–2 character queries get nothing.** A trigram index cannot answer shorter
  than a trigram, and FTS5 returns **no rows** rather than an error — so without
  the length gate `5G` reads as "nothing matches" and is believed. Four of the
  five queries above are shorter than three characters.
- **Very broad queries are slower through an index**, so they are not sent
  through one. `Galaxy` matches all 21,186 rows; 68.7ms → 69.9ms via the scan.
- **Devices and chips are deliberately not indexed.** Devices is 865 rows and
  costs 0.7ms with a query and 0.7ms with none; chips is 11.6ms with no query and
  11.7ms with one. `q` was never the cost outside releases.

It cannot go stale silently: the batch rebuilds it when its basis digest moves,
`check_corpus` re-derives the whole basis and reports
`search_index_does_not_match_the_corpus` (error), and the request path checks a
0.01ms row-count tripwire and **falls back to the scan** rather than answer from
a stale index. The cheap check catches rows arriving or leaving, not a same-count
edit; the digest catches that, once per batch.

### sizes are measured now, and two open items were judged on the wrong number

`tools/corpus_sizes.py` reports real bytes from `dbstat`. It states its own
residual, so a partial account cannot pass as a small one (currently 0.000 MB).

- File **246.105 MB**; tables 155.062 MB; **indexes 89.938 MB — 36.5% of it**.
- `VACUUM` reclaims **1.039 MB, 0.42%** (266 free pages). The estimate in the
  open list below was right, and is now a measurement.
- `observations` is **139.75 MB — 57% of the corpus** (102.1 MB of rows plus
  37.6 MB across 5 indexes). `sqlite_autoindex_observations_2`, an implicit
  UNIQUE index, is 20.6 MB on its own and the second largest object in the file.
- `firmware_release_evidence` and its index are only 47% and 53% full, so VACUUM
  would repack more than the freelist figure suggests; 1.039 MB is a floor.
- **`ledger/raw` is 18.3 MB. `ledger/staging` beside it is 89.9 MB** — 4.9×
  larger, and not mentioned anywhere. The growth item below names the smaller of
  the two. `history/review-20260916` is another 65.8 MB in 2 files.

## Data honesty, 2026-09-30

### 626 products had a terminal state, and it was spelled "pending"

`automate_identity_review` runs to a fixed point and leaves 626 of 2,367 products
at `review_state='proposed'` — every one already concluded, and concluded as
unresolvable: **465** with no independent identifier in any captured source
(including all 66 Apple products, for the reason `integrity.review_queue`'s
docstring gives) and **161** naming several candidates with nothing to discriminate
between them. `proposed` means "waiting for a reviewer", so those 626 presented
**20,955 observation links** as a queue no reviewer could ever clear.

They now rest in `unresolvable_on_captured_evidence` — neither `approved` (which
would assert the identity) nor `rejected` (which would assert it was judged wrong,
withdraw the product's hardware claim and block the rules forever). Nothing is
approved, asserted, promoted, deleted or hidden; the products go on serving from
the evidence layer. It is **reopenable**: the adjudication fingerprints the basis
it decided on and `reopen_stale_adjudications` withdraws it, dropping the frozen
conclusion, the moment a capture changes that basis — never on the clock, so a
rerun over unchanged inputs reopens nothing. Provenance goes in
`identity_resolution_rationales` beside every other automated identity decision.

Measured on a COPY of the live corpus: 626 → 0 `proposed`;
`observations_awaiting_review` Xiaomi 14,996 → 0, Apple 4,450 → 0, TECNO 846 → 0,
itel 573 → 0, Infinix 90 → 0, with the same observations reported as
`observations_adjudicated_unresolvable`. `observations_not_serving` unchanged
(they still do not serve), 845 devices served either way. The agent handoff bundle
goes from 626 candidates to 0.

`docs/AUTOMATED_IDENTITY_ENRICHMENT.md` has the contract;
`src/mobile_observatory/adjudication.py` has the argument.

### the corpus stored the four-character string 'null'

Six `xiaomi.community.firmware_tracker` observations carry `"null"` as text in
`$.data.release_date` — the only non-date value in that field across 96,319
observations. `coalesce` treated it as present, so `observations.effective_at`
published the word `null` as a date and it propagated to
`product_firmware_releases.vendor_released_at`.

The payload is untouched; the archive still says what the source said. The derived
values now read a non-date as absence, through ONE rule
(`src/mobile_observatory/source_dates.py`) applied where each value is produced —
which replaced five reader-side `nullif(…,'null')` patches that had accumulated
while both writers went on storing the sentinel. It is a shape test, not a sentinel
list: measured, every value any source states in those three fields is either JSON
null or ISO-date-prefixed, so it discards those six and nothing else.

`check_corpus` now reports `source_states_a_non_date_where_a_date_belongs`
(warning, 6 — the source's defect, still in the archive) and
`derived_date_column_holds_a_non_date` (error, 0 — a write that bypassed the rule).
The first is the third warning. `docs/SOURCE_INTERPRETATION_CORRECTIONS.md` has it.

## Robustness round, 2026-09-30

Seven defects found by executing ~2,800 real requests against the running
server, all now fixed with a guard that fails against the planted defect.

Four of them were one family — **a value from the request reaching SQLite
unbindable, or a clause reaching it unbound** — so they are fixed at the shared
helper, not per route:

- `_query_int` clamps every query-string integer into SQLite's signed 64-bit
  range. `offset=2**63` parsed fine, clamped fine, and raised `OverflowError` at
  BIND time; `OverflowError` is not `ValueError`, so the handler thread died and
  the client got **no HTTP response at all**. It was live at all twelve
  `_pagination` call sites. A path-segment id has the same bound.
- `devices_page` parses `max_android` **before** appending its clause. It used to
  append first, so a non-numeric value left one `?` unbound and dropped the
  connection — and the "matches nothing" intent its comment describes had never
  once executed.
- `database.like_clause` / `like_contains` are now the only way a LIKE is built.
  Unescaped, `?q=_` reported 865 devices / 452 chips / 3,443 releases as
  "matching" and `?q=%` the same; now 60 / 4 / 0 and 0 / 0 / 0, with `q=5G`
  unchanged at 99 / 28 / 1,357.
- `_require_id_list` refuses a str where a JSON array is expected. A str is
  iterable, so `{"ids": "abcdef"}` acknowledged six single-character ids and
  answered `404 update_not_found` — a type error reported as a fact about the
  corpus.

And in the request lifecycle:

- `Handler._body` bounds every body read. `Content-Length: 5000` in front of a
  2-byte body parked a handler thread **forever**, holding its per-thread SQLite
  handles; there was no read timeout anywhere. The bound is on IDLE time, so a
  slow honest upload is not cut off.
- `Handler._guarded` answers `500 {"error":"internal_error"}` instead of dropping
  the connection, and `log_message` writes an access line again (both were listed
  under "Open" here). A fault that leaves neither a response nor a log entry can
  only be found by reproducing it.
- `_refusal` stops an `OSError`'s filesystem path reaching a caller, and the
  collection-request routes answer 404 rather than `409` carrying
  `invalid literal for int() with base 10: 'abc'` as API prose.

Presentation, and the one thing deliberately NOT decided:

- `_firmware_holdings` is the single answer behind both the device grid and the
  device detail view, so one device cannot describe itself two ways. TECNO i3
  said `firmware_count: 0` / "firmware not observed" on the grid while its own
  detail view listed "Captured ROM history · 1". 7 of 865 devices diverged; the
  new `held_not_current` coverage state says "Captured releases; none establishes
  current firmware" instead of claiming absence. 0 of 865 disagree now.
- **Whether that `i3Pro-…` build belongs to the TECNO i3 is an identity
  judgement and was not made.** Nothing was reattached, deleted or re-reviewed.
  The `firmware_build_names_a_sibling_model` invariant was checked for
  under-reporting and does NOT under-report: its count of 8 already includes the
  i3Pro row, alongside L9Plus (×2), W3Pro, i5Pro, S11Plus, W5Lite and DP10APro.
  It is 8 and not 9 because that row was never missing from it.

## What this is

A local-first firmware/silicon/CVE observatory over 865 phones. Stdlib-only
Python 3.11+: `http.server` + SQLite. No pip, no Node, no container, no network
at runtime. 13 publishers, 96,319 captured observations.

Its defining property is what it refuses to do: it will not state a value it
cannot source, it records absence AS absence, and it never compares dates
across publishers as though they measured the same event.

## Run it on a new box

Verified end to end on a real clone on 2026-09-29; timings measured.

```sh
git clone git@github.com:RazyTommas/gsmarena-unblock.git obs   # 12s, 120 MB
cd obs/mobile-observatory
python3 -m mobile_observatory.batch                            # 9m50s, no flags needed
PYTHONPATH=src python3 -m mobile_observatory.server --port 8000 --data-dir .observatory-data
```

The batch needs no flags: its default `--legacy-root` resolves to
`../crawler/relay/results`, and all 16 required inputs are in git (24 MB).

**Do not start from `python3 run.py`** even though the README leads with it. It
works, but extracts the 2026-09-17 bundle: measured at **83 devices / 26,961
observations** against 854 / 94,969 from the batch. The README is wrong to lead
with it and has not been fixed.

## The single most important caveat

**A rebuild is not a restore.** Rebuilding from the same inputs does not
reproduce this corpus. Measured: 759 devices identical, **106 only in the live
corpus, 95 only in a rebuild** — 201 resolve differently. Two causes, both
deliberate behaviour:

- **1,111 identity conclusions are frozen under `RULE_VERSION 1`.** Remembered
  conclusions are final by design, so the corpus does not depend on *when* it
  last ran — which makes it depend on the *order* it ran in.
- **1,316 observations rest on an input version no longer on disk.** The
  capture tree is overwritten in place; the corpus accumulates. Six artifact
  digests exist only in the live corpus.

The corpus is an archive whose inputs are a moving window, not a cache of them.
If you need *this* corpus, copy `.observatory-data/`; do not rebuild.
Documented in `docs/BACKUP.md` and in the portable bundle's own README.

## Backup

See also `docs/CHANGESETS.md`, which is a different job: undoing the last batch,
not surviving the loss of the directory. Neither replaces the other.

`tools/backup_evidence.py` — **2.0 MB compressed, not 243 MB.** It asks the
corpus which files it cites rather than listing directories, so a new source
writing somewhere new is covered without editing the tool. `corpus.sqlite` is
deliberately NOT backed up (derived, rebuildable). `local.sqlite` goes through
sqlite3's backup API, never `cp`.

Exit 2 = archive written and verifies but INCOMPLETE. Drill exercised: deleted
every irreplaceable byte, restored, 21 artifacts resolvable, 5,785
acknowledgements back, served 865 devices.

## Access control

Added 2026-09-28; before that there was none. A live CSRF was demonstrated
(`Content-Type: text/plain` + `Origin: https://evil.example` → 200, row
written) and then refused. See `docs/ACCESS_CONTROL.md`.

- Loopback + no token = open. That is the dev machine, deliberately.
- Non-loopback bind with no token **mints one** at `<data-dir>/auth-token`
  (0600) and prints the PATH, never the value.
- Token proves *who*; the Origin check proves *where the request was composed*.
  Both are needed — the browser sends the cookie automatically, which is what
  CSRF abuses.

**Two gaps that cannot be closed in code** and must be handled at deploy time:
a loopback-bound server behind a reverse proxy or SSH tunnel is remotely
reachable while still counting as "open", and plain HTTP exposes a permanent
bearer token. The online box must set `MOBILE_OBSERVATORY_TOKEN` explicitly
even behind a proxy, and must terminate TLS.

## Open, none blocking

- The ledger grows without bound — and it is **`ledger/staging` at 89.9 MB**,
  not the `ledger/raw` (18.3 MB) this line used to name. `batch.log` unrotated.
- `VACUUM` still runs nowhere. Now measured rather than estimated: it reclaims
  1.039 MB of 246.105 MB (0.42%), plus some repacking of two 47%-full btrees.
  Still not worth a batch step.
- The batch emits two log lines for a ten-minute run.
- `run.py` hardcodes the 2026-09-17 zip; the 09-22 bundle has a manifest but no
  zip beside it.
- The rebuild/restore divergence is still not detected *as such*. What exists now
  is narrower and worth not confusing with it: every batch records exactly what
  it changed, invertibly. That makes a **write** reversible and visible; it does
  not compare a rebuild against this corpus.
- 6 `identity_resolution_rationales` rows and the rest of that table are outside
  every changeset, because it has no PRIMARY KEY. Giving it one would close the
  last blind spot in a revert; it is a schema change on a 721-row table and was
  not made here.

## Hard rules (from the original brief — still binding)

- No account creation, no password entry, no CAPTCHA solving, no login or
  bot-detection bypass, ever. **samfw.com is off limits.**
- robots.txt is binding even when a fetch is trivial. Cheapness is not
  permission. **dl.google.com** and **romprovider.com** are off limits;
  **deviceinfohw.ru** is disabled (403s honest crawler UAs).
- Authorised with limits: Samsung fota-cloud (UA `Kies2.0_FUS`, metadata only,
  no credentials) and Google OTA check-in device impersonation (metadata only,
  no IMEI, strip `update_token`/`androidId`, record `update_url` but never
  fetch it).
- Rate limits are a budget **per host per day, summed across every run** — not
  per run.
- **gsmarena belongs to `collector@field` exclusively. Do not touch it.**
  `.claude/` and `crawler/relay/results/*.csv` are theirs — read, never modify.
- Never commit credentials anywhere, including messages.
- Data honesty: never invent a value to fill a blank; absence is recorded as
  absence; never present a truncated list as complete; never compare dates
  across publishers as if they mean the same thing.
- Git: `--ff-only`, merge, **never rebase**. Never `git add -A` after a merge
  without scanning for conflict markers. Delegates must not `git stash` in a
  shared worktree. Leave the repo on `main`.
- Do not undo `crawler/relay/tests/test_relay_never_rewrites.py`.
- **Never self-report success.** No model-judged results. Every check must both
  pass clean AND fail against a planted defect — a test that cannot fail proves
  nothing. Before believing a number, ask whether the instrument is broken.

## Lessons this codebase has already paid for

Written down because each was found the expensive way.

1. **Run the thing.** Every serious defect this month was found by executing a
   path end to end, never by reading code. A delegated agent's bundle test
   stayed green through three crashes because its fixture never reached those
   paths.
2. **Fix the pattern, not the instance.** One bug lived in five call sites. It
   was fixed three times, once per crash, each at whichever site the batch
   reached first. The commit message for fix #2 even said "the same rule in two
   places is how the second one survived" — and shipped with three more copies.
   `resolve_silicon_vendor`/`resolve_silicon_family` are now the only route,
   with a test that fails if the raw insert returns.
3. **The dev corpus is a narrower world than production.** Three crashes were
   invisible on the populated dev corpus and fatal on a first ingest.
4. **A success line proves nothing about the payload.** `msg send -m -` printed
   `sent` and delivered a one-hyphen body. The backup tool printed success with
   `local.sqlite` missing from the archive. Read back what was actually written.
5. **`200×` is a rule about a growing axis.** It does not apply here: the phone
   catalogue is a fixed universe and we crawl the same internet a production box
   would. Real ceiling ~6× (every model of the brands we can source firmware
   for), ~62× absolute. A `~51 GB` figure derived from 200× was withdrawn.
