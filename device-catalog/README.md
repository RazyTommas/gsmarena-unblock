# Device Catalog — Mobile Observatory, Part 1

An independent, local catalog of devices, exact hardware models, technical specifications,
and firmware. Python 3.11+ and its standard library are the entire runtime. No pip install,
Node, hosted service, external assets, or model access is needed to browse or export.

## Run

From this directory, run `python3 run.py` (Windows: `py -3 run.py`) and open
<http://127.0.0.1:8130>. A downloaded bundle already contains its database and CSVs.

From a source checkout, initialize once from the existing corpus:

```sh
python3 run.py import ../mobile-observatory/.observatory-data/corpus.sqlite
python3 run.py
```

The importer opens the original database **read-only**, refuses to overwrite a populated
catalog, and preserves stable device/model IDs. It has no runtime dependency on the
old product, crawler, or its schema. It retains firmware observations excluded from
canonical promotion as `unresolved_observation`; these do not become verified facts.
The two source Samsung observations represented by existing canonical releases are not
duplicated. Different publishers/layers may still report the same build: row counts are
record counts, not a count of unique firmware binaries.

The older Atlas database has **not** been merged or deleted. Its reconciliation is a
separate migration task. Neither the old applications nor their databases are modified.

## What is included

- Devices with original identity status and technical specifications.
- Exact hardware models and explicit device/model links; a product can have multiple models.
- Firmware builds, version, region/channel, known release date, patch level, baseband,
  download/source URLs, observation date, and record layer.
- Search, sortable columns, custom 1–500 rows per page, CSV downloads, ZIP packaging, update history, and a review queue.

The interface defaults to dark mode with compact, single-line table rows. Chipsets are
visible and searchable in Devices, Models and Firmware, including chipset information
from linked hardware models. Hover truncated cells or open View for the full value.

Model release and firmware release dates are separate visible, searchable columns and
CSV fields (`released_at`, plus `model_released_at` in the firmware export). Model release
means commercial availability, not announcement or first observation. Unknown dates show
as clickable “Find…” actions. The Data quality tab reports current coverage. A normalized feed can propose model `released_at` as `YYYY`,
`YYYY-MM`, or `YYYY-MM-DD` through the evidence/reasoning workflow. Partial dates retain
their precision; the application never invents a day or derives a model launch from firmware.

**Settings → Firmware shown in the catalog** controls visible firmware regions. The
default is `ILO` (Israel), `MID`, `GLOBAL`, and `GL`. Select other codes or Show all regions,
then Save settings. This is a presentation filter: stored firmware and CSV exports retain
every region. The selected regions travel with the offline bundle.

Security advisories, CVEs, security verdicts, subscriptions, legacy dashboards, and old
crawler runtimes are outside this component. Unknown fields stay empty. Firmware patch
level remains ordinary technical metadata.

## CSV and offline bundle

```sh
python3 run.py export                         # dist/csv/
python3 run.py bundle                         # dist/device-catalog.zip
python3 run.py status
```

Both actions also appear under **Update & export** in the web UI. CSVs have a UTF-8 BOM,
CRLF records, quoted fields, headers, and formula-injection protection. `devices.csv`
contains flattened common specifications plus the complete `specs_json` column;
`firmwares.csv` contains firmware and its device/model IDs. `models.csv` and
`device_models.csv` preserve exact hardware and many-to-many relationships without
forcing a firmware row onto a guessed device. When `device_id` is empty in firmware,
use `model_id` to join the models table and device/model links. For Excel to preserve
model codes and version strings exactly, use **Data → From Text/CSV** with those columns
typed as Text (double-click opening uses Excel's own automatic type detection).

The ZIP contains the app, SQLite snapshot, CSVs, captured update evidence, and a SHA-256
manifest. It includes regional display preferences, but excludes machine-specific reasoning
commands, source settings, and credentials. Extract it on
another machine and run `python3 run.py`. It does not embed a Python interpreter.

## On-demand updates and LLM reasoning

Nothing crawls on a timer. Click **Check for updates** to fetch enabled sources, stage
changes, and review one configurable batch. Click **Review next batch** to process more
without fetching again. You can also use `python3 run.py update` or `review`.

Built-in live collectors:

| Source | Coverage | Limitations |
|---|---|---|
| IPSW API | iPhone discovery and firmware history | iPhones only; source-reported records, not hardware identity verification |
| Samsung FOTA | Latest firmware for known model/region targets | Rotating target batches; does not discover Samsung models itself |
| Xiaomi Firmware Updater | Xiaomi/Redmi/POCO tracker devices and releases | Community publisher; ambiguous codenames and regional variants remain reviewable |
| Google Play supported devices | Android manufacturer/model/codename discovery | Does not supply complete specifications or firmware; potential duplicate commercial names go to review |
| Configured HTTPS JSON feed | Additional devices, models and firmware | Publisher must supply the documented normalized schema |

Tecno, Infinix and itel **historical data is bundled**, but dedicated live firmware
collectors for those vendors are not implemented here yet. Google Play can discover their
models; additional firmware feeds can be configured. Run reports explicitly show per-source
errors, checked target counts and available target counts; success never means every vendor
was checked. A replay/import date is not a fresh source capture date.

The updater stores source bytes and hashes, compares records idempotently, and gives the
reasoner the proposed record, current value, nearby names, evidence URL/hash and an excerpt.
The reasoner must return one of `approve`, `review`, or `reject` with a concise evidence-based
rationale and matching capture ID. It cannot rewrite facts or make up missing values.
The application checks the complete response before applying any decisions, then applies
approved records transactionally after checking references and concurrent changes.
Ambiguous proposals remain in the review queue. **Apply** there is an explicit local
operator decision. Every reviewed proposal records the command/model arguments and rationale.
Source-level identities are scoped to their publisher and never imply canonical hardware matches.

## Configure the installed CLI from the web

**Settings** provides Claude and Codex presets, an editable JSON argument array, timeout,
batch size, source configuration, and **Test saved command**. The process inherits the
server user's environment/login; this app does not read or store CLI credentials.

Claude defaults to the current `opus` alias with high effort. Change `--model` to another
alias/full model supported by your installation (for example `fable`, also a preset).
Codex presets select `gpt-6.1-sol` or `gpt-6-astra` with high reasoning effort, verified in
the installed CLI's model catalog on 2026-10-23. Edit `-m` as newer models become available,
or remove it to use the CLI's configured default. There is no automatic downgrade
when a requested model is unavailable: the run fails visibly and leaves candidates pending.

The [Codex CLI documentation](https://learn.chatgpt.com/docs/non-interactive-mode) and
[Claude CLI documentation](https://code.claude.com/docs/en/headless) describe these
non-interactive interfaces. Presets were checked against the installed CLI help.
Commands receive the prompt on stdin and must emit the final JSON object on stdout.
Commands execute as argument lists, never through a shell, in a temporary working directory.
The default Claude command has tools disabled; the Codex preset requests a read-only sandbox.
Custom commands are trusted local operator configuration and inherit that user's permissions.

The web server binds to loopback only, validates the Host header and Origin, and requires
a session token for mutations. Do not expose it through a public reverse proxy. Updates,
settings writes and packaging are serialized in the web process. An operating-system lock
prevents another server or CLI writer from opening the same catalog concurrently; use the
web export/bundle buttons while its server is running. Read-only status remains available.

### Additional JSON feeds

Add `{"id":"my-feed","kind":"json_feed","enabled":true,"url":"https://example.com/catalog.json"}`
to Sources. The response is an array of `{ "kind": "device|firmware|model_device", "record": {...} }`.
Stable IDs must persist across fetches. `device` requires `id`, `manufacturer`, `name`,
`identity_state`, `specs_json` (a JSON object encoded as text), `source_url`, and `observed_at`.
`firmware` requires `id`, `device_id`, `model_id` (one may be null), `source`, `source_key`,
`build`, and `record_layer`; optional technical columns match the SQLite `firmwares` table
in `core.py`. `model_device` requires `id`, `manufacturer`, `name`, `model_code`, `codename`,
`source_url`, and `observed_at`. It creates a source-reported device and exact model together.
All records go through evidence capture, validation and reasoning review.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 run.py test-command   # Uses the saved CLI and its existing authenticated account
```

Database, evidence, exports and local settings live in ignored `data/`; generated bundles
live in ignored `dist/`. Back up `data/` to retain this component's working state.

## Missing-data research workflow

Click **Find…** in a table or record detail to copy/download a task for your agent.
It includes exact record identity, current facts, evidence requirements, and the import/review steps.
**Data quality** shows per-field coverage, filters records with gaps, and accepts researched JSON assertions.
Imports verify an exact supporting excerpt in a captured HTTPS source before staging changes for
the configured reasoning CLI. Existing values cannot be silently overwritten.

For a source checkout, seed known specification pages once and research on demand:

```sh
python3 run.py research-seed ../mobile-observatory/.observatory-data/corpus.sqlite
python3 run.py enrich --limit 50
python3 run.py research-import research-results.json
python3 run.py review
```

The mapped-source parser only fills empty fields when the fetched product title exactly matches
the original mapping. Source renames and mismatches stay flagged for review. Announcements
are never treated as releases. Model-variant dates require evidence for that exact variant.
No periodic research runs automatically. See QA_REPORT.md for the latest verification and limits.
