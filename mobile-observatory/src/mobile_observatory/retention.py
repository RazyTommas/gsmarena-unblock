"""Bounded retention for the trees the ingest writes and nothing ever deleted.

WHY THIS IS A DISK PROBLEM AND NOT A LATENCY ONE

None of these files is on a request path. `ledger/` is read only by
`collectors/importer.import_run` replaying a run, and `batch.log` only by a
human. So the question is never "is this slow" -- it is "what is this costing,
and what would deleting it lose".

WHAT WAS ACTUALLY MEASURED (copy of the live corpus, 2026-10-03)

HANDOFF.md's open item named `ledger/raw` as the tree that grows without bound.
Two things are wrong with that, both measured here:

  * `ledger/staging` is 94,272,853 B (89.9 MiB) against `ledger/raw`'s
    19,202,294 B (18.3 MiB) -- 4.9x larger, and it was unmentioned.
  * `ledger/raw` has **nothing prunable in it at all**. All 13 of its distinct
    digests are in `artifacts.sha256` AND referenced by a surviving staging
    file. Pruning it is not "unfinished", it is refused: 28 files, 19.2 MB,
    100% vetoed. Naming the wrong directory also named the only one of the two
    that this policy can never free a byte from.

And `ledger/staging` is mostly not garbage either. The batch rewrites it with
fixed run ids (batch.py's adapter list), so 11 of its 15 files -- 91,902,560 B,
97.5% of the tree -- were written by the most recent run via `os.replace`, with
a fresh mtime. A total-size cap below ~88 MiB would delete a file the run just
wrote, which the next run regenerates: churn, not retention. Only 4 files /
2,370,293 B are stale orphans, from retired run ids and from the manual
collection worker (which mints unique timestamped run ids, so its staging file
is never rewritten and goes stale by design).

THE THREE RULES THAT MAKE THIS SAFE

1. **mtime is not a liveness signal under `ledger/raw`.** `IngestionStore.
   save_artifact` is content-addressed and guarded by `if not body.exists()`, so
   a raw file's mtime is when its digest was FIRST seen and never when it was
   last used. Every raw file here has an mtime from 2026-09-18..09-23 while
   today's run cites all of them. Age alone would delete the whole tree.
   Eligibility under `raw` is therefore REFERENCE, not age; age only sets a
   floor.

2. **A veto, computed from `artifacts.storage_uri` and `artifacts.sha256`.**
   `tools/backup_evidence.irreplaceable_files` is the right question asked the
   wrong way round for this purpose: it DROPS a row whose bytes are missing or
   whose sha256 mismatches (it records a problem and continues), so a
   cited-but-corrupt artifact is absent from its output and would look prunable.
   The veto is built from the raw columns, a strict superset, and
   `irreplaceable_files` is used for reporting only.

   The digest half matters beyond robustness. By path, 9 raw `.bin` files are
   cited (8 distinct digests). By digest, all 13 are: the other 5 are cited at
   an `evidence/artifacts/...` path holding the same bytes. A path-only veto
   would have left 10 raw files (5 pairs) prunable, and relocating a data
   directory -- which is what a restore is -- makes every stored absolute path
   miss while the digests still match.

3. **A per-tree minimum age, so the current run's files can never be
   eligible.** 48 hours is two nightly cadences, so neither the most recent run
   nor the one before it can be taken by the size cap, whatever the cap says.
   Measured: that protects the 91,902,560 B the last run wrote.

WHEN THE SIZE CAP CANNOT BE MET

It says so. `residual_bytes` and `residual_reason` report how far over budget
the trees still are and how many bytes are held by veto. Deleting a vetoed or
current-run file to satisfy a number would be the worst available outcome: the
next run writes it again and the cap is still missed.

NOT IN THIS POLICY, AND WHY

  * `changesets/` -- 7,129,972 B, growing ~285 KB per batch (~104 MB/year).
    Adding it is one `TreeRule` below. It is deliberately not added: a changeset
    is the only way to revert a run (docs/CHANGESETS.md), and how far back a
    revert must be able to reach is a policy call for whoever relies on it, not
    a default.
  * `ledger/runs/` -- 9,518 B, 0.008% of the ledger, and `latest.json` is live
    state. A run's own record (counts, state, messages) is the only evidence the
    run happened; keeping it after its payload is pruned leaves the prune
    attributable. The bytes are not there.
  * `history/review-20260916/` -- 69 MB of a human's frozen snapshot. Nothing in
    `src/` writes it, so nothing in `src/` gets to delete it.
  * `cron.log` (109 KB) and `server.log` -- shell redirects, not written by this
    process. A `>>` redirect holds the inode open, so renaming the file would
    silently keep writing to the renamed one -- exactly the hazard
    `batch_logging` has, but for a handle this process does not own. See
    docs/SCHEDULING.md for the `logrotate copytruncate` rule those two want.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# Named so a log line can report WHICH knob took effect, the way access.py's
# ENV_VAR and changesets.py's DISABLE_ENV do.
DEFAULT_MAX_AGE_DAYS = 14.0
DEFAULT_MIN_AGE_HOURS = 48.0
DEFAULT_MAX_MB = 256.0
DEFAULT_LOG_MAX_MB = 4.0
DEFAULT_LOG_KEEP = 4

KIND_RUN_KEYED = "run_keyed"
KIND_CONTENT_ADDRESSED = "content_addressed"

# Every veto reason is a named constant so a report can be grepped and a test
# can assert WHICH rule saved a file rather than only that it survived.
VETO_CITED_URI = "cited_by_artifacts_storage_uri"
VETO_CITED_DIGEST = "cited_by_artifacts_sha256"
VETO_PAIRED_WITH_PROTECTED = "indivisible_from_a_protected_sibling"
VETO_STAGED = "referenced_by_a_surviving_staging_file"
VETO_TOO_YOUNG = "younger_than_the_minimum_age"
VETO_CORPUS_UNREADABLE = "corpus_unreadable_so_no_citation_can_be_established"

# Two ways to have no citation basis, and they are not the same event. A first
# build legitimately has no corpus yet -- `run_batch` creates it after this runs
# -- and raising an alarm for the normal first-run state is a false alarm that
# teaches a reader to ignore the real one (tests/test_batch_logging.py asserts a
# healthy run logs no ALARM, and it is right to). A corpus that EXISTS and will
# not answer for its artifacts is a fault. `batch.py` branches on these.
REFUSED_NO_CORPUS = "no_corpus_yet"
REFUSED_CORPUS_UNREADABLE = "corpus_will_not_answer_for_its_artifacts"

PRUNE_TOO_OLD = "older_than_the_maximum_age"
PRUNE_OVER_BUDGET = "over_the_total_byte_budget"

# Not a veto. Inside both bounds, so nothing asked for it -- and available to
# the size cap the moment the cap moves.
KEPT_INSIDE_BOUNDS = "inside_both_bounds_and_available_to_the_size_cap"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TreeRule:
    """One managed tree. The policy is this list and nothing else."""

    name: str                       # relative to the data directory
    kind: str                       # KIND_RUN_KEYED | KIND_CONTENT_ADDRESSED
    pattern: str = "*"              # which files under it this rule owns
    max_age_days: float = DEFAULT_MAX_AGE_DAYS
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS
    # Whether a surviving file in this tree protects a content-addressed digest.
    # Declared rather than inferred from the directory name: this is the fact
    # that makes the plan order load-bearing, so it belongs in the policy where
    # a reader can see it.
    carries_artifact_digests: bool = False
    # Which member of a content-addressed group holds the bytes the digest names.
    # `IngestionStore.save_artifact` writes `<sha256>.bin` beside a `<sha256>.json`
    # sidecar; declaring the suffix here keeps that fact in the policy instead of
    # hardcoding storage.py's layout into the planner.
    content_suffix: str = ".bin"

    @property
    def max_age_seconds(self) -> float:
        return self.max_age_days * 86400.0

    @property
    def min_age_seconds(self) -> float:
        return self.min_age_hours * 3600.0


def default_policy(*, max_age_days: float = DEFAULT_MAX_AGE_DAYS,
                   min_age_hours: float = DEFAULT_MIN_AGE_HOURS) -> tuple[TreeRule, ...]:
    """The managed trees, in the order they must be planned.

    `staging` BEFORE `raw`, and that order is load-bearing: a raw digest is
    protected by any staging file that SURVIVES this plan, so which staging
    files survive has to be decided first.
    """
    return (
        TreeRule("ledger/staging", KIND_RUN_KEYED, "*.jsonl",
                 max_age_days=max_age_days, min_age_hours=min_age_hours,
                 carries_artifact_digests=True),
        TreeRule("ledger/quarantine", KIND_RUN_KEYED, "*.jsonl",
                 max_age_days=max_age_days, min_age_hours=min_age_hours),
        TreeRule("ledger/raw", KIND_CONTENT_ADDRESSED, "*",
                 max_age_days=max_age_days, min_age_hours=min_age_hours),
        # One line adds changesets/ here. See the module docstring for why it is
        # deliberately absent.
    )


@dataclass(frozen=True)
class Candidate:
    relative: str
    tree: str
    bytes: int
    age_seconds: float
    reason: str

    def as_dict(self) -> dict:
        return {"relative": self.relative, "tree": self.tree, "bytes": self.bytes,
                "age_days": round(self.age_seconds / 86400.0, 3), "reason": self.reason}


@dataclass
class Plan:
    """What WOULD happen. Nothing here has touched the filesystem."""

    prune: list[Candidate] = field(default_factory=list)
    vetoed: list[Candidate] = field(default_factory=list)
    # The third state, and it has to exist. "Pruned" and "vetoed" alone would
    # report a file that is simply inside both bounds as protected, which it is
    # not -- it is available to the size cap the moment the cap moves. Every
    # managed file appears in exactly one of these three lists, and
    # `accounted_files` checks that.
    kept: list[Candidate] = field(default_factory=list)
    log: dict = field(default_factory=dict)
    managed_bytes_before: int = 0
    managed_files: int = 0
    budget_bytes: int = 0
    residual_bytes: int = 0
    residual_reason: str | None = None
    corpus_readable: bool = False
    cited_uris: int = 0
    cited_digests: int = 0
    staged_digests: int = 0
    refused: str | None = None
    refused_code: str | None = None

    @property
    def prune_bytes(self) -> int:
        return sum(c.bytes for c in self.prune)

    @property
    def vetoed_bytes(self) -> int:
        return sum(c.bytes for c in self.vetoed)

    @property
    def managed_bytes_after(self) -> int:
        return self.managed_bytes_before - self.prune_bytes

    @property
    def accounted_files(self) -> int:
        """Every managed file lands in exactly one of the three lists.

        The instrument's own check, in the spirit of `storage.measure`'s
        `unaccounted_bytes`: a file in none of them has been silently dropped
        from the plan, and a partial account of a tree reads exactly like a
        small one.
        """
        return len(self.prune) + len(self.vetoed) + len(self.kept)

    def as_dict(self) -> dict:
        return {
            "refused": self.refused,
            "refused_code": self.refused_code,
            "corpus_readable": self.corpus_readable,
            "cited_storage_uris": self.cited_uris,
            "cited_sha256": self.cited_digests,
            "digests_referenced_by_surviving_staging": self.staged_digests,
            "managed_bytes_before": self.managed_bytes_before,
            "managed_bytes_after": self.managed_bytes_after,
            "prune_files": len(self.prune),
            "prune_bytes": self.prune_bytes,
            "vetoed_files": len(self.vetoed),
            "vetoed_bytes": self.vetoed_bytes,
            "kept_files": len(self.kept),
            "kept_bytes": sum(c.bytes for c in self.kept),
            "accounted_files": self.accounted_files,
            "managed_files": self.managed_files,
            "budget_bytes": self.budget_bytes,
            "residual_bytes": self.residual_bytes,
            "residual_reason": self.residual_reason,
            "pruned": [c.as_dict() for c in self.prune],
            # Never truncated and never silent: a vetoed file is the policy
            # working, and the only way to know it worked is to see it named.
            "vetoed": [c.as_dict() for c in self.vetoed],
            "kept": [c.as_dict() for c in self.kept],
            "log": dict(self.log),
        }


def _citation_basis(corpus: Path) -> tuple[set[str], set[str], tuple[str, str] | None]:
    """Absolute paths and digests the corpus cites, from the RAW columns.

    Not `irreplaceable_files`: that drops a row whose bytes are absent or whose
    sha256 no longer matches. A cited-but-corrupt artifact is exactly the file
    that must not be deleted, and it is exactly the one missing from that
    output. This is a strict superset of it.
    """
    if not corpus.is_file():
        # A first build. run_batch creates the corpus AFTER retention runs.
        return set(), set(), (REFUSED_NO_CORPUS, f"no corpus at {corpus} yet")
    try:
        connection = sqlite3.connect(f"file:{corpus}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return set(), set(), (REFUSED_CORPUS_UNREADABLE,
                              f"{corpus} did not open read-only: {exc}")
    try:
        rows = connection.execute("SELECT storage_uri, sha256 FROM artifacts").fetchall()
    except sqlite3.Error as exc:
        return set(), set(), (REFUSED_CORPUS_UNREADABLE,
                              f"{corpus} has no readable artifacts table: {exc}")
    finally:
        connection.close()

    uris: set[str] = set()
    digests: set[str] = set()
    for storage_uri, sha256 in rows:
        raw = storage_uri or ""
        if raw and not ("://" in raw and not raw.startswith("file://")):
            try:
                uris.add(str(Path(raw.removeprefix("file://")).resolve()))
            except OSError:
                pass            # an unreadable path cites nothing we can match
        if sha256:
            digests.add(sha256)
    return uris, digests, None


def _staged_digests(paths: list[Path]) -> set[str]:
    """Every `artifact_sha256` on a row of these staging files.

    Streamed line by line: one of these files is 48 MB and reading it into a
    list of dicts (the way `importer._jsonl` does, correctly, for one run) would
    be gigabytes. Measured on the live tree: 94 MB, 0.73s -- noise against a
    ten-minute batch, so it is not worth a cheaper-but-approximate scan.
    """
    digests: set[str] = set()
    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line).get("artifact_sha256")
                    except (ValueError, AttributeError):
                        continue
                    if value:
                        digests.add(value)
        except OSError:
            continue
    return digests


def _files(root: Path, rule: TreeRule) -> list[Path]:
    tree = root / rule.name
    if not tree.is_dir():
        return []
    return sorted(p for p in tree.rglob(rule.pattern) if p.is_file())


def plan(data_dir: Path | str, *, policy: tuple[TreeRule, ...] | None = None,
         max_bytes: int | None = None, log_path: Path | str | None = None,
         log_max_bytes: int | None = None, log_keep: int = DEFAULT_LOG_KEEP,
         now: float, corpus: Path | str | None = None) -> Plan:
    """What retention WOULD do. Pure: it reads, it never writes.

    `now` is required rather than defaulted to `time.time()` so a test can place
    a file at a chosen age instead of sleeping, and so one plan uses one clock.
    """
    root = Path(data_dir)
    policy = default_policy() if policy is None else policy
    budget = int(DEFAULT_MAX_MB * 1024 * 1024) if max_bytes is None else int(max_bytes)
    corpus_path = Path(corpus) if corpus else root / "corpus.sqlite"
    log_file = Path(log_path) if log_path else root / "batch.log"
    log_cap = int(DEFAULT_LOG_MAX_MB * 1024 * 1024) if log_max_bytes is None else int(log_max_bytes)

    result = Plan(budget_bytes=budget)
    uris, digests, failure = _citation_basis(corpus_path)
    result.corpus_readable = failure is None
    result.cited_uris, result.cited_digests = len(uris), len(digests)

    # The log is this process's own file and cites nothing, so its rotation does
    # not depend on the corpus and is planned either way.
    result.log = _plan_log(log_file, log_cap, log_keep)

    managed: list[tuple[TreeRule, Path, int, float]] = []
    for rule in policy:
        for path in _files(root, rule):
            stat = path.stat()
            managed.append((rule, path, stat.st_size, max(0.0, now - stat.st_mtime)))
    result.managed_bytes_before = sum(size for _, _, size, _ in managed)
    result.managed_files = len(managed)

    if failure is not None:
        # Refuse the whole prune. With no citation basis every file under
        # `raw` looks uncited, which is the one mistake this module exists to
        # make impossible -- and reporting "nothing to prune" would be a
        # measurement reported as a fact about the tree.
        result.refused_code, detail = failure
        result.refused = f"{VETO_CORPUS_UNREADABLE}: {detail}"
        result.vetoed = [Candidate(str(p.relative_to(root)), r.name, size, age,
                                  VETO_CORPUS_UNREADABLE)
                         for r, p, size, age in managed]
        if result.managed_bytes_before > budget:
            result.residual_bytes = result.managed_bytes_before - budget
            result.residual_reason = (
                "the byte budget was not applied at all: no citation basis, so no file "
                "could be shown to be unreferenced")
        return result

    pruned: list[Candidate] = []
    vetoed: list[Candidate] = []
    eligible: list[tuple[Path, Candidate]] = []     # may be taken by the size cap

    # --- run-keyed trees first: which staging files survive decides raw ----
    run_keyed = [(r, p, s, a) for r, p, s, a in managed if r.kind == KIND_RUN_KEYED]
    for rule, path, size, age in run_keyed:
        relative = str(path.relative_to(root))
        if age < rule.min_age_seconds:
            vetoed.append(Candidate(relative, rule.name, size, age, VETO_TOO_YOUNG))
        elif age > rule.max_age_seconds:
            pruned.append(Candidate(relative, rule.name, size, age, PRUNE_TOO_OLD))
        else:
            eligible.append((path, Candidate(relative, rule.name, size, age, PRUNE_OVER_BUDGET)))

    doomed = {c.relative for c in pruned}
    surviving = [path for rule, path, _, _ in run_keyed
                 if rule.carries_artifact_digests
                 and str(path.relative_to(root)) not in doomed]
    staged = _staged_digests(surviving)
    result.staged_digests = len(staged)

    # --- content-addressed trees: grouped, because a pair is indivisible ----
    content = [(r, p, s, a) for r, p, s, a in managed if r.kind == KIND_CONTENT_ADDRESSED]
    groups: dict[tuple[str, str], list[tuple[TreeRule, Path, int, float]]] = {}
    for rule, path, size, age in content:
        groups.setdefault((rule.name, str(path.parent / path.stem)), []).append(
            (rule, path, size, age))

    for (tree_name, _), members in sorted(groups.items()):
        rule = members[0][0]
        stems = {path.stem for _, path, _, _ in members}
        digest = next(iter(stems)) if len(stems) == 1 and _DIGEST.match(next(iter(stems))) else None
        paths = [path for _, path, _, _ in members]
        cited_members = {path for path in paths if str(path.resolve()) in uris}
        content_members = {path for path in paths if path.suffix == rule.content_suffix}
        group_age = min(age for _, _, _, age in members)

        # The group's verdict, not the file's. A `.bin` is cited and its `.json`
        # sidecar never is, but deleting either half makes
        # `importer.import_run` raise FileNotFoundError on a replay (it reads the
        # sidecar FIRST, importer.py:52) -- so the whole group lives or dies
        # together, and the sidecar is REPORTED as vetoed under its own reason
        # rather than quietly surviving.
        group_reason: str | None = None
        protected: set[Path] = set()
        if digest and digest in digests:
            group_reason, protected = VETO_CITED_DIGEST, content_members
        elif cited_members:
            group_reason, protected = VETO_CITED_URI, cited_members
        elif digest and digest in staged:
            # Both halves, not just the bytes: a replay opens the sidecar for its
            # media type and retrieved_at before it ever touches the body.
            group_reason, protected = VETO_STAGED, set(paths)
        elif group_age < rule.min_age_seconds:
            group_reason, protected = VETO_TOO_YOUNG, set(paths)

        for _, path, size, age in members:
            relative = str(path.relative_to(root))
            if group_reason is None:
                if group_age > rule.max_age_seconds:
                    pruned.append(Candidate(relative, tree_name, size, age, PRUNE_TOO_OLD))
                else:
                    eligible.append((path, Candidate(relative, tree_name, size, age,
                                                     PRUNE_OVER_BUDGET)))
            else:
                vetoed.append(Candidate(
                    relative, tree_name, size, age,
                    group_reason if path in protected else VETO_PAIRED_WITH_PROTECTED))

    # --- the byte budget, oldest eligible first ---------------------------
    after = result.managed_bytes_before - sum(c.bytes for c in pruned)
    eligible.sort(key=lambda item: -item[1].age_seconds)      # oldest first
    taken = 0
    for _, candidate in eligible:
        if after <= budget:
            break
        pruned.append(candidate)
        after -= candidate.bytes
        taken += 1
    result.kept = [
        Candidate(candidate.relative, candidate.tree, candidate.bytes,
                  candidate.age_seconds, KEPT_INSIDE_BOUNDS)
        for _, candidate in eligible[taken:]]

    if after > budget:
        held = sum(c.bytes for c in vetoed)
        result.residual_bytes = after - budget
        result.residual_reason = (
            f"the byte budget could not be met from eligible files: {after} B remain against a "
            f"{budget} B budget, {held} B of which is vetoed (cited evidence, an indivisible "
            f"sibling, or younger than the minimum age). Nothing live was deleted to reach a "
            f"number -- the next run would write it again and the budget would still be missed. "
            f"Raise --retention-max-mb deliberately, or lower --retention-days.")

    result.prune = pruned
    result.vetoed = vetoed
    return result


def _generations(log_file: Path) -> dict[int, Path]:
    """`batch.log.1`, `batch.log.2`, ... keyed by generation number."""
    found: dict[int, Path] = {}
    if not log_file.parent.is_dir():
        return found
    prefix = log_file.name + "."
    for sibling in log_file.parent.iterdir():
        if not sibling.is_file() or not sibling.name.startswith(prefix):
            continue
        tail = sibling.name[len(prefix):]
        if tail.isdigit():
            found[int(tail)] = sibling
    return found


def _plan_log(log_file: Path, max_bytes: int, keep: int) -> dict:
    """Rotate `batch.log` at `max_bytes`, keeping `keep` generations.

    `batch.log` is 30,133 B / 130 lines after ~65 runs -- about 232 B per run,
    so roughly 85 KB a year on a nightly timer. A size cap will essentially
    never fire at today's verbosity, and that is stated rather than hidden: this
    bound exists against a verbosity regression (HANDOFF.md's open item "the
    batch emits two log lines for a ten-minute run" is an invitation to one),
    not against today's growth.
    """
    generations = _generations(log_file)
    size = log_file.stat().st_size if log_file.is_file() else 0
    # keep < 1 would make rotation mean "delete the log", which is data loss with
    # no reason attached. It is refused: generations past the (empty) keep set are
    # still removed, and the live file is left alone and goes on growing. argparse
    # states the same bound.
    rotate = keep >= 1 and log_file.is_file() and max_bytes > 0 and size >= max_bytes
    shift = 1 if rotate else 0
    # A generation that would land past `keep` goes. This also catches `keep`
    # being lowered between runs, with no rotation of its own.
    remove = sorted(n for n in generations if n + shift > keep)
    renames: list[tuple[str, str]] = []
    if rotate:
        for n in sorted((n for n in generations if n + 1 <= keep), reverse=True):
            renames.append((generations[n].name, f"{log_file.name}.{n + 1}"))
        renames.append((log_file.name, f"{log_file.name}.1"))
    return {
        "path": str(log_file),
        "bytes": size,
        "max_bytes": max_bytes,
        "keep": keep,
        "rotate": rotate,
        "rotated": False,
        "generations_present": sorted(generations),
        "remove": [generations[n].name for n in remove],
        "remove_bytes": sum(generations[n].stat().st_size for n in remove),
        "renames": renames,
    }


def apply(plan_result: Plan, data_dir: Path | str, *, dry_run: bool = False) -> dict:
    """Perform the plan. Returns the plan's own report plus what actually ran.

    A dry run deletes nothing and renames nothing; the report is otherwise
    identical, so "what would it do" and "what did it do" are read the same way.
    """
    root = Path(data_dir)
    report = plan_result.as_dict()
    report["dry_run"] = bool(dry_run)
    report["deleted_files"] = 0
    report["deleted_bytes"] = 0
    report["errors"] = []
    if dry_run:
        return report

    # A refusal suppresses the FILE prune and nothing else. `batch.log` cites no
    # evidence, so whether the corpus can be read has no bearing on rotating it --
    # and a box whose corpus will not open is a box whose log is about to be the
    # only thing anyone can read. `plan_result.prune` is empty under a refusal
    # anyway; the check is here so that stays true by construction.
    for candidate in (() if plan_result.refused else plan_result.prune):
        target = root / candidate.relative
        try:
            target.unlink()
        except OSError as exc:
            report["errors"].append(f"{candidate.relative}: {exc}")
            continue
        report["deleted_files"] += 1
        report["deleted_bytes"] += candidate.bytes

    log = report["log"]
    directory = Path(log["path"]).parent
    for name in log["remove"]:
        try:
            (directory / name).unlink()
        except OSError as exc:
            report["errors"].append(f"{name}: {exc}")
    # Descending, so .2 is out of the way before .1 becomes .2, and the base
    # file moves last.
    moved = False
    for source, destination in log["renames"]:
        try:
            (directory / source).replace(directory / destination)
        except OSError as exc:
            report["errors"].append(f"{source} -> {destination}: {exc}")
            continue
        if source == Path(log["path"]).name:
            moved = True
    # `rotated` answers exactly one question: did the live log file move out from
    # under the handle the caller is holding? It must therefore be driven by that
    # rename alone. Anding in the whole error list -- which an unrelated failed
    # unlink under ledger/ can fill -- would report False after a successful
    # rename, and the caller would then NOT reattach its logger and would spend
    # the rest of the run appending to batch.log.1 with nothing saying so.
    log["rotated"] = moved
    return report


def log_fields(report: dict) -> tuple[str, tuple]:
    """`(template, fields)` -- call it as `logger.info(template, *fields)`.

    A pair rather than a finished string so the caller interpolates with %s on
    the logger, which is what every other report in this codebase does -- an
    f-string formats even when the record is never emitted, and it is the one
    spelling the house style does not use.

    SPLAT THE FIELDS. `logger.info(*log_fields(report))` passes the tuple as ONE
    argument; logging unwraps a single mapping and nothing else, so
    `msg % self.args` then sees one argument for fourteen %s and raises inside
    the handler. logging swallows that as a "Logging error" on stderr, so the
    run continues and the line is simply absent from batch.log. That is how this
    shipped once and was found by running the batch.
    """
    log = report.get("log", {})
    return (
        "retention pruned=%s pruned_bytes=%s vetoed=%s vetoed_bytes=%s kept=%s "
        "managed_files=%s accounted_files=%s managed_before=%s managed_after=%s budget=%s "
        "residual=%s log_rotated=%s log_generations_removed=%s dry_run=%s",
        (report["prune_files"], report["prune_bytes"], report["vetoed_files"],
         report["vetoed_bytes"], report["kept_files"], report["managed_files"],
         report["accounted_files"], report["managed_bytes_before"],
         report["managed_bytes_after"], report["budget_bytes"], report["residual_bytes"],
         log.get("rotated"), len(log.get("remove", [])), report["dry_run"]),
    )
