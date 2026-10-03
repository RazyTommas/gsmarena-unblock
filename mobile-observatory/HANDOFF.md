# Mobile Observatory — handoff

Updated 2026-10-04. Supersedes the 2026-09-17 handoff entirely — that one
described port 8124 and a `/tmp` snapshot that no longer exists.

## 5,643 of 5,804 "this build came after that one" claims were ordered by a hash (2026-10-04)

**MIGRATION 0034 IS NOT YET APPLIED TO `.observatory-data`.** The batch and the
server both apply it, inside the batch lock, and the server names what it is
about to apply before applying it. Take a backup first
(`tools/backup_evidence.py`): 0034 rewrites 21,845 rows of a derived read model
and records 21,845 `source_data_corrections` rows. **It is not reversible from a
changeset** — a migration is schema, so `tools/corpus_changeset.py revert`
refuses the run without `--schema-moved`. Until it is applied, `check_corpus`
reports `event_order_basis_table_absent` (warning, 5,804) and the feed reports
every basis as `null`.

`promote_approved_product_observations` read `$.data.release_date` and
`$.data.branch` — one publisher's vocabulary.
`mifirm.community.firmware_archive` publishes the identical two facts as
`$.data.vendor_released_at` and `$.data.channel`, so all **21,845** of its
promoted releases stored `vendor_released_at IS NULL` and `channel='unknown'`
while a real date sat in the payload beside them. Nothing errored.

`android_version_changed` orders its before/after pair by
`ORDER BY … vendor_released_at, id`, and with every date NULL the sort collapses
onto `id` — `uuid5(_NS,'product-firmware'||observation_id)`. Measured on a
read-only copy of the live corpus, two independent instruments agreeing (the
pair's dates via `dedupe_key`; the shape of `occurred_at`):

| | |
|---|---|
| `android_version_changed` events published | **5,804** |
| ordered on a pair where BOTH releases are undated | **5,643** |
| ordered on two stated dates | 56 |
| citing release rows the corpus no longer holds | 105 |

**Replayed with the dates the sources stated all along: of the 5,690 events whose
inputs still exist, 5,588 would not have been produced and 1,064 that should
exist never were.** 102 are the same claim either way. 278 of the survivors
assert a direction the real dates reverse. The replay was validated against the
stored events first — 0 events it derives from the corpus as-is are missing from
`domain_events` — and the batch then produced **exactly the 1,064 it predicted**.

**Nothing is retracted.** `domain_events` is append-only by trigger and deleting
published facts is the larger harm. `domain_event_ordering` (migration 0034)
records what decided each claim, **measured before** the backfill and frozen
afterwards, in `software_state_basis`'s own vocabulary —
`vendor_release_date` / `observation_order_only` / `mixed_dated_and_undated`,
plus `cited_releases_absent` for the 105. The two halves of 0034 are ordered and
that order is load-bearing: reversed, it stamps `vendor_release_date` onto 5,643
hash-ordered events, and a test plants exactly that. `GET /updates` and the
product detail carry `orderingBasis`, never defaulted; the Radar card prints it.

Blast radius, measured on a copy — live was never written and the batch never ran
against it:

| | before | after |
|---|---|---|
| `product_firmware_releases` with a date | 4,880 | **26,729** (+21,849) |
| `channel='unknown'` | 25,035 | **3,190** (honest absence: naijarom, google, frbox) |
| `android_version_changed` events | 5,804 | **6,868** (+1,064, all `vendor_release_date`) |
| events removed | — | **0** |
| `devices_with_current_firmware` | 845 | **845** |
| devices / observations / `firmware_releases` | 865 / 96,319 / 21,186 | unchanged |
| `check_corpus` deep | 0 errors, 4 warnings | **0 errors, 7 warnings** |

A second batch over the migrated copy adds 0 events and 0 releases, and
`corpus-identity.json` reports no divergence.

**42 devices' headline build changes, and that is user-visible.** 3 are the plain
repair (capture order → real dates, inside mifirm). The other **39 flip publisher
from `xiaomi.community.firmware_tracker` to `mifirm.community.firmware_archive`,
and 7 of those now show a build with an EARLIER date than the one it replaced.**
The mechanism is pre-existing and deliberate: `current_firmware`'s device pick
ranks by `latest_basis`, then `currency_rank`, then `source_id` — so every date
comparison after it is between rows of one publisher. mifirm used to lose on
basis alone; now both are `vendor_release_date`, both are rank 50, and `mifirm.…`
sorts before `xiaomi.…` alphabetically. `check_corpus` now names the population:
`headline_build_decided_by_publisher_name`, **243 devices (181 before this
change)**. **Deciding that one of these publishers outranks the other is an
authority judgement and was NOT made.**

**One instance of the same mismatch is left open on purpose.**
`source_dates.STATED_DATE_FIELDS` drives `observations.effective_at` (migration
0031) and does not list `$.data.vendor_released_at`, so that generated column
still reports the capture time for **44,351** mifirm observations.
`product_firmware_releases.vendor_released_at` is unaffected. Not closed because
`effective_at` already mixes a vendor release date, a bulletin publication date
and a capture time in one sortable column that the Explore observations tab sorts
on; the honest repair is the banding decision `devices_page` already had to make,
which is a judgement, not a rename. `check_corpus` reports it
(`source_stated_release_date_not_read_by_the_generated_column`, warning, 44,351)
on the **deep** scan only — it is a 222ms 96,319-row scan and `deep=False` runs
on every page load. Measured: the fast set costs 627.6ms at HEAD and 627.0ms with
this change.

Every adapter was checked for the same shape, by a scan over the source rather
than by reading: `mifirm_archive` (the defect), `xiaomi_tracker`, `apple_ipsw`,
`samsung_fota`, `samsung_history`, `samsung_aspl`, `tecno_security`,
`tecno_ota_checkin`, `frbox_transsion`, `naijarom_transsion`, `fixture_catalog`.
`frbox`'s `build_date`, `naijarom`'s `date_token` and `samsung_fota`'s
`build_derived_month` are deliberately NOT read as release dates — each adapter
says so in its own payload `date_basis`. Three further writers were routed
through the one rule, found by the scan and not by reading:
`collectors.promotion.SamsungFirmwarePromoter` held a third spelling of the date
and a second of the channel whose default was `"stable"` (an assertion, not an
absence), and `hmd_updates` a fourth. Both measured byte-identical for their own
sources first — the Samsung channel is an input to `release_id`, so a different
value would have re-keyed all 21,186 `firmware_releases`.

Planted and caught (**16 of 16**, listed in the harness output): the field-name
fix reverted · an adapter gains an undeclared date field · 0034's two halves
swapped · a recorded basis overwritten by a later run · a basis written for an
event that already existed · the feed defaulting a missing basis · the mislabel
CHECK constraints dropped · `check_corpus` losing its table guard · the 222ms
scan back on the request path · the client losing a label · an absent channel
borrowing another publisher's default · the field-name rule used to bypass the
shape rule · the adapter scan pointed at nothing (the guard's own guard) · the
basis vocabulary becoming two lists · the migration retracting the events · the
feed hard-joining the table and 500-ing below 0034.

**Three of those plants were not caught on the first pass**, and each exposed a
test that could not fail. `test_the_constraints_refuse_a_mislabelled_basis` used
invented event ids, so every insert raised `IntegrityError` from the
`REFERENCES domain_events(id)` foreign key and the case passed with the CHECK
constraints removed entirely. And the frozen-basis case passed while EITHER of
two independent defences held (`INSERT OR IGNORE`, and only recording on the
branch that actually inserted), so planting either one left it green — the
AND-keeps-the-bug shape. All three are fixed and now isolate what they name.

`docs/SOURCE_INTERPRETATION_CORRECTIONS.md` has the full argument,
`src/mobile_observatory/firmware_order.py` the reasoning for a table rather than
a column, `docs/API.md` the `orderingBasis` contract.

## Deploy-rehearsal blockers, 2026-10-03 (latest) — three P1s and three adjacent

Found by a clean-clone deploy rehearsal and fixed here. Every one was reproduced
before it was fixed.

### P1-A — every documented `python3 -m mobile_observatory.*` failed on a fresh clone

```
$ python3 -m mobile_observatory.batch   # [FAILS] HANDOFF.md, verbatim
ModuleNotFoundError: No module named 'mobile_observatory'     EXIT=1
```

Twenty sites across docs, source error messages and **the Admin page itself**
(its "Build and verify snapshots with ..." line named the snapshots module
the same way)
omitted `PYTHONPATH=src`. All fixed.

**Why 677 tests never caught it:** every test file does
`sys.path.insert(0, ROOT/"src")`, so the suite manufactures the one condition an
operator does not have. `tests/test_documented_commands_actually_run.py` closes
that two ways — a scan over every doc and source string, and a SUBPROCESS with a
clean environment that runs the documented form (must succeed) **and** the bare
form (must fail, or the scan is guarding nothing). Three further sites were found
by the new guard, not by me.

### P1-B — the changeset lied, and the real cause was not the migrations

```
"changeset": {"error": "sqlite3session_changeset failed (rc=17)",
              "reason": "sqlite3session via ctypes", "recorded": true}
```
with no `changesets/` directory on disk. Reproduced on a cold build.

Two defects. **`recorded` was a hardcoded `True`** beside `**stored`, describing
the code path rather than the artifact — now derived (`changeset_result`).

And **rc=17 is `observations`, not the migrations.** Bisected phase by phase
(the session survives `apply_migrations()` at rc=0 / 1,930 bytes and breaks at
the first captured source) then table by table:
`attach=observations -> rc=17`, every other table fine.
`observations.effective_at` is a VIRTUAL GENERATED column and the session
extension cannot produce a changeset for such a table — attaching it fails the
WHOLE session.

**This was never only a cold-build problem.** `INSERT OR IGNORE` means a
steady-state night writes no new observation, so the changeset worked on exactly
the runs with nothing to undo. The 285 KB nightly changesets were no-op runs.

Fixed by attaching tables by name, excluding generated-column tables, and
REPORTING the gap: `check_corpus` now says
`table_cannot_be_in_a_changeset_generated_column` (warning, naming
`observations`, 57% of the corpus). A first build is skipped deliberately with a
stated reason. See `docs/CHANGESETS.md` limit **0**.

### P1-C — the backup did not survive a different box

After the documented `--restore ... --data-dir <new>`: the bytes arrive and
`artifacts.storage_uri` still points at the ORIGINAL directory. It "worked" in
the rehearsal only because that directory still existed on the same machine.

`run.py` already solved this (`rebase_evidence` + `provenance-paths.json`) and
the backup path did not — the same one-rule-in-one-place-only shape this codebase
keeps paying for. Both now go through
`src/mobile_observatory/evidence_paths.rebase`, keyed by **digest** (the only
thing that survives a move), which reports `unresolved_count` so a partial
relocation cannot read as a complete one. `--rebase` does it for a directory that
was moved rather than restored.

**And the drill in `docs/BACKUP.md` restored `/tmp/drill` into `/tmp/drill`**, so
it could never detect this. It now restores into a different directory.

### Adjacent, and closed while in there

- **The server wrote the corpus outside `batch.lock`.** `batch.py` takes it;
  `server.py` never referenced it, while running `apply_migrations()` and a
  projection rebuild. A nightly timer plus any restart policy is two writers on
  one SQLite file. Every startup write is now inside the lock, and a server
  started mid-batch refuses with a message naming the batch.
- **Migrations applied at startup, ungated and silent.** That is how
  `.observatory-data` reached schema **33** while this handoff still said a human
  had to apply 0033 — nobody did; a restart did. The server now names the count
  and the versions **before** applying, says there is no backup, and reports
  `schema=` in the posture banner. `run.py` says it too (the bundle is at schema
  8 and is carried **23** versions forward). Not a refusal: a server that will
  not start because its schema is behind is a worse outage than the one it
  prevents.
- **The rebuild/restore detector now reports the MECHANISM.** Independently
  reproduced: live holds **1,209** conclusions frozen at `RULE_VERSION 1`
  (not 1,111) against a rebuild that is 100% v2; v2 strips the brand prefix, so
  **87 of the 106** codes only the live corpus has are literally
  `<BRAND> <a code the rebuild does have>`; and a rebuild **GAINS 4,941**
  `product_firmware_releases`. `docs/BACKUP.md` framed the divergence purely as
  loss and no longer does.

## Production-hardening round, 2026-10-03 (later) — five items

Every number below was measured, on a **copy** of the live corpus and on real
servers on spare ports; the live directory was never written and the batch never
ran against it. Every guard passes clean **and** fails against a planted defect —
**28 defects planted, 28 caught**, listed per item.

### the two "un-closeable" deploy gaps are now detectable and loud

The previous round called them un-closeable in code. They are. They were also
**undetectable**, which is a different problem, and that one is fixed. Both
exposures leave evidence *in the request*: a relay header
(`X-Forwarded-For` / `-Proto` / `-Host` / `Forwarded`) or a non-loopback peer
means it came from beyond this machine, and the absence of `proto=https` means
nothing says the hop in front spoke TLS.

`src/mobile_observatory/posture.py` raises two named alarms —
`open_server_is_reachable_from_outside` and
`access_token_travelled_in_cleartext` — on stderr (re-stated every 300s while
true) **and** in `/api/v1/admin/health` under `posture`, where
`posture.alarming` is the one boolean a monitor can gate on.

It **warns and never refuses**, which was a decision: neither condition is
knowable at startup (both are properties of a request, so a refusal could only
fire on the thousandth one); the trigger is one header any client can send, so
refusing hands every stranger an off switch; and a proxy in front is a legitimate
deployment whose repair is a token in a unit file, applied by a human in
daylight. `access.token_for_binding` already holds the ground where refusing is
right — state known before the first byte.

The banner, one line on stderr, every start — never the token's value:

```
posture: bind=0.0.0.0:8000 exposure=all-interfaces auth=token(environment) origin-check=active tls-in-front=unknown-until-a-request-arrives insecure-ok=unset
```

`tls-in-front` is honestly unknown there and says so; health reports it as
measured once traffic exists. `MOBILE_OBSERVATORY_INSECURE_OK` takes **alarm
names**, not a boolean, so waiving the cleartext reality of a LAN box does not
also silence "this open server is reachable from the internet"; the environment
is the authority and `<data-dir>/insecure-acknowledged.json` is the append-only
record. A waived condition is still counted and dated (`status: acknowledged`,
`waived: true` — two fields because they are two facts).

Full posture table in `docs/ACCESS_CONTROL.md`. The dev posture (loopback, no
token, nothing in front) stays silent, measured — if it did not, nobody would
read the alarm on the box that matters.

Planted and caught (7): observe() removed from the handler · relay headers
ignored · the opt-out made a blanket boolean · `posture` dropped from health ·
any `https` hop in a chain read as TLS to the client · an unresolvable peer
treated as loopback · the banner stops naming the token's source.

### `python3 run.py` refuses, and the README no longer leads with it

It extracted a hardcoded 09-17 bundle: **83 devices / 26,961 observations**
against 854 / 94,969 from the batch, with nothing downstream saying so. It could
not be repointed — the only zip in `portable/` *is* the 09-17 one, and the 09-22
manifest (236 devices) has no zip beside it.

So: bare `python3 run.py` exits **2** with a refusal naming the two commands that
build the real corpus, the bundle's own stated counts (read from its manifest,
never hardcoded) and every manifest whose zip is missing. `--bundle` keeps the
snapshot path for a box with no captured inputs, picks the **newest** manifest
that has a zip rather than a typed-in date, and says what it chose and what it is
not. `--list-bundles` reports the inventory. README and `docs/PORTABLE_RUN.md`
both fixed; the README now also carries the rebuild-is-not-a-restore caveat,
which had never reached the one document a new deployer opens.

Proven by extraction, not by the manifest's word: `--bundle --restore-only` into
a temp dir gives **hardware_models 83, observations 26,961** — exactly the
numbers the refusal quotes.

Planted and caught (5): the refusal removed · selection hardcoded to a date ·
a manifest with no counts reported as `0 devices` · a manifest with no zip
silently skipped · the README leading with `run.py` again.

### the batch is no longer silent

2 lines for a ten-minute run → **39**, one per phase, with counts and elapsed
time. Plus a heartbeat for a phase still open after `--progress-heartbeat-seconds`
(default 60, which no normal phase reaches), because a completion line cannot
tell you the phase you are waiting on is still alive.

It immediately found something nobody knew: **`identity:bridge-registry` is
202.7s of a 218.1s run — 93% of the batch in one phase that had never once been
visible.**

No `4/37` denominator, deliberately: the total is not knowable when the first
phase logs, so it would be a hand-maintained number that can be wrong. The count
of phases run is reported once (`batch finished phases=36`). Before/after logs
and the knob are in `docs/SCHEDULING.md`.

Planted and caught (5): the sources stop being phases · the heartbeat never
speaks · a missing count printed as `0` · the heartbeat re-arm shortening the age
it reports · a failing phase logging nothing.

### the rebuild/restore divergence is detected

~~The rebuild/restore divergence is still not detected *as such*~~ — CLOSED.
`src/mobile_observatory/corpus_identity.py` records
`<data-dir>/corpus-identity.json` on every batch: a per-subject digest over
`identity_conclusions` (2,367), `source_identity_registry` (3,733),
`hardware_models` (865) and `artifacts` (21). 19ms to compute, 0.44 MB, 3ms to
compare.

Keyed by `(manufacturer, normalized_name)` and not by row id. That was caught by
running two consecutive batches rather than by reading: keyed by `product_id`,
`merge_confirmed_duplicates` made an ordinary night report **464 conclusions
forgotten and 464 added with the total unchanged**. Keyed by identity, a normal
batch reports **0 errors** (measured, batch N+1 against a baseline from N) and a
real rebuild still reports 3,542.

**The test is containment, not equality**, and that is the design. A nightly
batch adds; an equality test would fire every night and be switched off within a
week. What a batch never does is *forget* — a concluded identity is final by
design — so a subject the baseline recorded and this corpus no longer has is the
divergence, and an addition is counted as an addition.

Measured on copies of the live corpus:

| posture | finding |
|---|---|
| the same corpus, its own baseline | none |
| 40 devices added, nothing forgotten | none |
| 106 recorded devices gone, 95 new, 7 conclusions re-decided, 11 dropped, 6 input digests gone | **error**, 130 subjects, naming `21091116UI`, `2210129SG`, `24053PY09C`, … |
| no baseline recorded | warning — reported, never silence |

A sidecar and not a table, because a rebuild creates a new `corpus.sqlite` and a
baseline inside it would be destroyed by the event it exists to detect. It is now
in `tools/backup_evidence.py`'s archive too (optional manifest key, so older
archives still verify) — without that, a restore puts the evidence back and takes
the only record that could check the result. `check_corpus` reports it;
`PYTHONPATH=src python3 -m mobile_observatory.corpus_identity compare --identity-baseline <path>`
asks directly.

The uniqueness guard in `fingerprint()` earned itself on the first real run:
keying `artifacts` by `sha256` alone put 21 rows into 20 keys, which would have
left the count honest while the comparison went blind.

Planted and caught (6): additions counted as divergence · forgotten subjects not
counted · an absent baseline reading as a pass · a clock folded into the identity
· the batch recording before checking · the uniqueness guard removed.

### Explore tab counters state no number rather than a wrong one

`Devices (3) | Silicon (452)` — one right number and two for a query no longer on
screen. Each page now records the **scope** it was fetched under and a count
shows only while that scope is in force; otherwise the label stands alone with a
`title` saying why. No extra fetches: measured, filtering makes **zero**
`/api/v1/chips` and `/api/v1/releases` requests.

Why not "unfiltered": the stored total is not the unfiltered total, it is the
total for whatever query was last sent to *that* tab. Refine `S2` to `S26` and
Silicon holds the total for `S2`; calling that "unfiltered" states something
false. Omitting it cannot state a wrong number, and it returns the moment the
reader opens the tab.

The scope is per tab, which falls out for free and matters: the manufacturer
filter is not part of the silicon query, so selecting a manufacturer hides the
*releases* count and keeps the *silicon* one — a true number is not withheld.

Planted and caught (5): the stale count shown again · one global "is anything
filtered" flag · a stale scope falling back to the stored total · the boot's
totals never stamped · a hidden count explaining nothing.

## This round, 2026-10-03 — **570 tests under both runners** (521 before)

Four fixes, each at its root. Measured, not asserted; every guard was run against
a planted defect before it was believed.

### clicking a search result painted a view nobody had loaded

Typing `S26` and clicking "Galaxy S26" landed on Explore with **0 rows and 0 API
requests** while the server held 3; only a second click, on the Devices tab,
fetched anything. `render()` paints `state.data` and never fetches, so a caller
that moved the view and then called `render()` showed whatever the last unrelated
fetch had left there — on a fresh boot, the unfiltered first page, which the
grid's own client-side filter then reduced to nothing.

It was **five call sites**, not one. Measured identically (`calls=[] rows=0`) on
the search hit, the rail button, a `#explore` hash, Enter in the search box (which
had no handler at all, while the dropdown's own footer says "Press Enter to filter
the table"), and the boot's phase 2 overwriting a filtered table with the
unfiltered page it had asked for before the reader typed. There is now one rule —
`ROUTE_LOADERS` / `loadRoute()` / `showRoute()` in `apps/web/app.js` — and every
one of them goes through it. The rows-per-page handler held a **third** copy of
the same route→loader dispatch table; it is gone.

Two things found while auditing and fixed with it:

- The grid re-ran the query over each row's own values after the server had
  already answered it. The server matches a device's **codename**; the row it
  returns carries no codename column. Measured: 88 devices have a codename that
  appears nowhere else on their row, and `q=lisa_tw_global` answers 1 device which
  the client filter dropped to 0. The client-side `matches()` is gone.
- **Browser Back/Forward does not change views at all**, and never did: the hash
  is written with `history.replaceState`, so `history.length` stays 2 and Back
  leaves the page. Not fixed — `pushState` is a behaviour change nobody asked for,
  and the comment above the router explains why the replaceState/retry loop is
  shaped that way. The `hashchange` listener it *would* use is fixed and proven.

`tests/test_search_hit_opens_a_loaded_table.py` drives a real browser against a
real server. Its fixture is 121 devices on purpose: with four, the boot's first
page already holds the match and the test passes against the broken code.

### the search has no wildcards, and now says so only when asked

Measured: matching is case-insensitive substring matching. `S26`=`s26`=3 devices,
`SM-S94`=3 (mid-string), and `s26*`, `S26%`, `s26_`, `*`, `%` all return **0**,
because every character is literal. That is the correct behaviour and is not
touched — `%` and `_` were live LIKE wildcards until recently and `q=_` reported
865 devices / 452 chips / 3,443 releases as "matching".

What it cost was discoverability, so the rule is now reachable **on demand and
never before**: a `title=` for the mouse, a `:focus-within` line for the keyboard
(hover alone has no keyboard route), `aria-describedby` for a screen reader, and
nothing persistent in the chrome — the line is `display:none` until the field has
focus and yields to the results dropdown, which occupies the same 44px. And a zero
result whose query contains `*`, `%`, `_` or `?` adds one muted line inside the
empty state that is already there, in the four places a zero result can be
reached, from one function. No banner, no colour, no button. Works in light, dark
and high contrast; the stylesheet stays fully token-driven.

### `identity_resolution_rationales` is inside the changeset now

It had no PRIMARY KEY, so the session extension recorded nothing for it and a
revert silently skipped 721 rows. Migration **0033** gives it
`PRIMARY KEY (subject, rule)` over a stored `subject` column holding
`ifnull(identity_id,'product:'||product_id)` — the expression 0030's unique index
was built on, materialised, because SQLite permits neither an expression nor a
generated column in a primary key, and `PRIMARY KEY (identity_id, rule)` cannot
exist on a STRICT table whose `identity_id` must stay nullable. A table-level
`CHECK` ties `subject` to that derivation, so the database enforces it rather than
the writer.

Measured on a copy of the live corpus: 721 rows before and after, the collision set
is the **same set** (0 keys differ between the old expression and the new column),
`tables_invisible_to_a_changeset()` goes `['identity_resolution_rationales']` →
`[]`, `check_corpus` goes **0 errors / 4 warnings → 0 errors / 3 warnings**, and an
insert, an update and a delete inside a recorded block round-trip through
`invert()` back to a byte-identical table.

`src/mobile_observatory/identity_rationales.py` is now the only writer — the two
rules spelled the same nine-column INSERT by hand, and `subject` would have been a
tenth column for both to derive separately. A source scan fails if a second writer
appears.

### bounded retention, over the directory that is actually large

`src/mobile_observatory/retention.py`, run by the batch inside the batch lock,
bound by **age and bytes** (`--retention-days 14`, `--retention-max-mb 256`,
`--retention-min-age-hours 48`, `--log-max-mb 4`, `--log-keep 4`, plus
`--retention-dry-run` / `--retention-off`; `MOBILE_OBSERVATORY_RETENTION_*` in
`scheduling/run-batch.sh`). It prints what it pruned and puts it in
`results["retention"]`.

Three measurements decided the shape:

- **`ledger/raw` has nothing prunable in it.** All 13 distinct digests are cited
  by `artifacts.sha256` *and* referenced by a surviving staging file: 28 files,
  19.2 MB, **100% vetoed even at `--retention-days 0 --retention-max-mb 0`**. The
  open item below named the only one of the two trees this can never free a byte
  from.
- **mtime is not a liveness signal under `raw`** (content-addressed, written once
  behind `if not body.exists()`), and **97.5% of `ledger/staging` is rewritten
  every night** by fixed run ids — so a byte cap below ~88 MiB would delete a file
  the run just wrote. Hence the minimum age, and an honest residual line when the
  cap cannot be met rather than a deletion that the next run undoes.
- The veto is built from `artifacts.storage_uri` **and** `artifacts.sha256`, not
  from `backup_evidence.irreplaceable_files`, which drops a row whose bytes are
  missing or whose digest mismatches — so a cited-but-corrupt artifact would have
  looked prunable. The digest half is also what survives a **relocation**:
  verified on a copy, where 0 of 21 stored absolute paths resolve and all 28 raw
  files were still vetoed.

Verified independently of the implementer: at the harshest policy the flags allow,
30 files / 94,272,853 B of staging and quarantine were pruned, **0** raw files,
and the corpus's 20 cited artifacts were identical before and after with
`problems == []`.

State: see the production-hardening round at the top for the current test count
under BOTH `python3 -m unittest discover -s tests` and `pytest`; it was 570 before
that round (521 before the round below, 347 before the robustness round).
Corpus, as of 2026-10-04 and AFTER migration 0034: **0 errors, 6 warnings on the
FAST check set; 7 on the full scan.** Before 0034 the same code reports 0 errors
and 5 fast / 6 deep, the extra one being `event_order_basis_table_absent`. The
lines below said `3 fast / 4 full` and were measured on 2026-10-03; the fast set
was already 4 at HEAD before this round (`observation_link_identity_owned_by_
another_product`, 18), so take the numbers in this paragraph and not those.
Fast-set cost is unchanged: 627.6ms at HEAD, 627.0ms now, five runs, minimum.
Those are two different measurements of two different things and are not
comparable --
`check_corpus(deep=False)` is what `/api/v1/admin/health` runs on every page load,
and `deep=True` adds the whole-database page scans, the search-index
re-derivation and the corpus-identity comparison. Quoting one number for both is
how "3 warnings" came to look like a regression against a scan that reports 4.
Measured 2026-10-03 on a read-only backup of the live corpus. A corpus with no
`corpus-identity.json` yet reports one more, naming that absence; the first batch
after this round records one.

**The live corpus is at schema 33, not 32.** It got there because the server
applies migrations at startup, ungated and silently -- which is also how it
reached 33 while the lines below still said a human had to apply 0033. The server
now says what it is about to apply, before it applies it, and does it inside the
batch lock.

**Migrations 0030-0033 ARE ALL applied to `.observatory-data`.** The line here
previously said 0033 was not and that "a human does that". Nobody did: the server
applies every pending migration at startup, with no prompt, no count and no
backup, so the live corpus was carried to 33 by a restart. That is corrected both
ways -- the claim above is now true, and the server states what it is about to
apply before applying it (see the production-hardening round at the top).

## Three compiled-in SQLite capabilities, 2026-09-30

`ENABLE_SESSION`, `ENABLE_FTS5` and `ENABLE_DBSTAT_VTAB` are all in this box's
libsqlite3 and none was used. **521 tests under both runners** (479 before). No
new dependency: `ctypes` and `sqlite3` only.

Migration 0032 **is** applied to `.observatory-data` (verified 2026-10-03: the
live corpus reports schema version 32). The line here previously said it was not.

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
PYTHONPATH=src python3 -m mobile_observatory.batch                            # 9m50s, no flags needed
PYTHONPATH=src python3 -m mobile_observatory.server --port 8000 --data-dir .observatory-data
```

The batch needs no flags: its default `--legacy-root` resolves to
`../crawler/relay/results`, and all 16 required inputs are in git (24 MB).

`python3 run.py` **refuses** as of 2026-10-03, and the README no longer leads
with it. It used to extract the 2026-09-17 bundle: measured at **83 devices /
26,961 observations** against 854 / 94,969 from the batch, with nothing
downstream saying so. `--bundle` still reaches the snapshot for a box with no
captured inputs.

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

**Two gaps that cannot be CLOSED in code** — the proxy is somebody else's
config and the TLS terminator is somebody else's daemon — but as of 2026-10-03
they are **detected**: a loopback-bound server behind a reverse proxy or SSH
tunnel is remotely reachable while still counting as "open", and plain HTTP
exposes a permanent bearer token. Both now raise a named alarm on stderr and in
`/api/v1/admin/health` (`posture.alarming`), with a one-line startup banner and a
deliberate, recorded `MOBILE_OBSERVATORY_INSECURE_OK` opt-out per alarm. It warns
and never refuses, for reasons stated at the top and in
`docs/ACCESS_CONTROL.md`. The online box must still set
`MOBILE_OBSERVATORY_TOKEN` explicitly even behind a proxy, and must still
terminate TLS — what changed is that failing to is no longer invisible.

## Open, none blocking

- ~~The ledger grows without bound~~ — CLOSED 2026-10-03, see the retention
  section at the top. Worth keeping the correction visible: this line named
  `ledger/raw`, which turned out to be the one tree retention can never free a
  byte from (100% vetoed), while `ledger/staging` beside it was the 89.9 MB.
- `VACUUM` still runs nowhere. Now measured rather than estimated: it reclaims
  1.039 MB of 246.105 MB (0.42%), plus some repacking of two 47%-full btrees.
  Still not worth a batch step.
- ~~The batch emits two log lines for a ten-minute run~~ — CLOSED 2026-10-03,
  see the progress section at the top. 2 lines → 39, and it found that
  `identity:bridge-registry` is 93% of the run.
- ~~`run.py` hardcodes the 2026-09-17 zip~~ — CLOSED: it refuses by default and
  `--bundle` picks the newest manifest with a zip. **The 09-22 bundle still has a
  manifest and no zip** — that is unchanged and now legible rather than silent:
  `run.py --list-bundles` reports it, and the refusal names it. Packaging the
  09-22 zip, or deleting its manifest, is still somebody's call.
- ~~The rebuild/restore divergence is still not detected *as such*~~ — CLOSED
  2026-10-03, see the corpus-identity section at the top. The changeset work is
  still the narrower thing and still worth not confusing with it: a changeset
  makes a **write** reversible; it does not compare a rebuild against this
  corpus.
- **Migration 0034 is not applied to `.observatory-data`.** See the section at
  the top. Written here rather than left implicit because this exact line is
  what went stale for 0033: nobody applied it, a server restart did, and the
  handoff went on saying a human had to. The server and the batch both apply it
  now and both say what they are applying first — but take a backup, because it
  rewrites 21,845 rows of a derived read model and a changeset cannot revert a
  migration.
- **`observations.effective_at` does not read `$.data.vendor_released_at`**, so
  it reports the capture time for 44,351 mifirm observations. Measured and
  reported (`source_stated_release_date_not_read_by_the_generated_column`,
  warning, 44,351, deep scan). Closing it changes the Explore observations sort
  for 46% of the corpus and needs the banding decision `devices_page` already
  made — a judgement, deliberately left to a human.
- **39 devices' headline build now comes from mifirm rather than the Xiaomi
  tracker purely because `mifirm.…` sorts before `xiaomi.…`**, both being
  `currency_rank` 50 with the same `latest_basis`; 7 of them show an earlier date
  than the build they replaced. 243 devices are in that tiebreak state (181
  before this change), counted by
  `headline_build_decided_by_publisher_name`. Giving one publisher a higher
  `currency_rank` is an authority judgement and was not made.
- ~~`identity_resolution_rationales` is outside every changeset~~ — CLOSED by
  migration 0033, which IS applied to `.observatory-data` (verified 2026-10-03:
  the live corpus reports schema 33). It was applied by a server restart, not by
  a human, which was its own defect and is fixed above.

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
