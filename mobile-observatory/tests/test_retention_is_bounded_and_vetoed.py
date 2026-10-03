"""Retention must be bounded by BOTH age and bytes, and must never take evidence.

Count is not a bound: ten files is meaningless when one of them is 48 MB, which
is the actual shape of `ledger/staging` on the live corpus. So every test here
asserts bytes or ages, never "how many are left".

The hazards these guard are all specific to this tree and all measured:

  * `ledger/raw` is content-addressed and write-once, so **mtime is not a
    liveness signal** -- a raw file's mtime is when its digest was first seen.
    Every raw file on the live corpus has a September mtime while today's run
    cites all 13 of its digests. An age-only rule deletes the whole tree.
  * The batch **rewrites staging with fixed run ids**, so 97.5% of that tree was
    written by the most recent run. A byte cap that may take a current-run file
    is churn: the next run writes it again and the cap is still missed.
  * A raw `.bin` and its `.json` sidecar are **indivisible** --
    `importer.import_run` opens the sidecar first (importer.py:52) and the body
    second, so losing either half makes a replay raise FileNotFoundError.

Each test is written so a specific defect breaks it: dropping the veto, dropping
the minimum age, letting the byte cap ignore eligibility, or pruning half a pair.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))      # as tests/test_backup_evidence.py does

from mobile_observatory import retention      # noqa: E402

NOW = 1_800_000_000.0          # a fixed clock: ages are chosen, never slept for
DAY = 86400.0
HOUR = 3600.0


def digest_of(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class Fixture:
    """A data directory with files at chosen ages and a corpus citing chosen rows."""

    def __init__(self, root: Path):
        self.root = root
        self.artifacts: list[tuple[str | None, str | None]] = []

    def file(self, relative: str, content: bytes, *, age_days: float) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        stamp = NOW - age_days * DAY
        os.utime(path, (stamp, stamp))
        return path

    def staging(self, run_id: str, digest: str, *, age_days: float, pad: int = 0) -> Path:
        row = json.dumps({"artifact_sha256": digest, "kind": "firmware_release",
                          "source_record_id": run_id, "data": {"pad": "x" * pad}},
                         sort_keys=True) + "\n"
        return self.file(f"ledger/staging/{run_id}.jsonl", row.encode(), age_days=age_days)

    def raw_pair(self, source: str, digest: str, *, age_days: float,
                 body: bytes = b"ARTIFACT-BODY") -> tuple[Path, Path]:
        stem = f"ledger/raw/{source}/{digest[:2]}/{digest}"
        return (self.file(f"{stem}.bin", body, age_days=age_days),
                self.file(f"{stem}.json", b'{"media_type":"text/csv"}', age_days=age_days))

    def cite(self, *, storage_uri: Path | str | None = None, sha256: str | None = None) -> None:
        self.artifacts.append((str(storage_uri) if storage_uri else None, sha256))

    def corpus(self) -> Path:
        """Only the two columns the veto reads. See the instrument check below for
        why a hand-made table is safe here: the real migrated schema is asserted
        to answer the same query."""
        path = self.root / "corpus.sqlite"
        connection = sqlite3.connect(path)
        connection.execute("DROP TABLE IF EXISTS artifacts")
        connection.execute("CREATE TABLE artifacts(id TEXT, source_id TEXT, "
                           "storage_uri TEXT, sha256 TEXT)")
        connection.executemany(
            "INSERT INTO artifacts(id, source_id, storage_uri, sha256) VALUES(?,?,?,?)",
            [(f"a{n}", "src", uri, sha) for n, (uri, sha) in enumerate(self.artifacts)])
        connection.commit()
        connection.close()
        return path

    def plan(self, **kwargs) -> retention.Plan:
        self.corpus()
        kwargs.setdefault("max_bytes", 1 << 40)
        kwargs.setdefault("policy", retention.default_policy(
            max_age_days=kwargs.pop("max_age_days", 14.0),
            min_age_hours=kwargs.pop("min_age_hours", 48.0)))
        return retention.plan(self.root, now=NOW, **kwargs)


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(Path(self._tmp.name))

    # -- helpers that make the assertions read as the claim they are ---------
    def pruned(self, plan: retention.Plan) -> dict[str, str]:
        return {c.relative: c.reason for c in plan.prune}

    def vetoed(self, plan: retention.Plan) -> dict[str, str]:
        return {c.relative: c.reason for c in plan.vetoed}

    def kept(self, plan: retention.Plan) -> dict[str, str]:
        return {c.relative: c.reason for c in plan.kept}

    def assertAccounted(self, plan: retention.Plan) -> None:
        """Every managed file is in exactly one list.

        `storage.measure` reports `unaccounted_bytes` for the same reason: a
        partial account of a tree reads exactly like a small one.
        """
        self.assertEqual(plan.managed_files, plan.accounted_files,
                         "a managed file appears in none of pruned/vetoed/kept")
        names = [c.relative for c in plan.prune + plan.vetoed + plan.kept]
        self.assertEqual(len(names), len(set(names)), "a file is in two lists at once")


class AgeBound(RetentionCase):
    def test_the_age_bound_prunes_past_it_keeps_inside_it_and_refuses_below_min_age(self):
        f = self.fixture
        f.staging("retired", digest_of("d-retired"), age_days=20.0, pad=500)
        f.staging("idle", digest_of("d-idle"), age_days=5.0, pad=400)
        f.staging("current", digest_of("d-current"), age_days=1.0 / 24.0, pad=300)
        plan = f.plan(max_age_days=14.0, min_age_hours=48.0)

        self.assertEqual(retention.PRUNE_TOO_OLD,
                         self.pruned(plan)[str(Path("ledger/staging/retired.jsonl"))])
        self.assertEqual(retention.KEPT_INSIDE_BOUNDS,
                         self.kept(plan)[str(Path("ledger/staging/idle.jsonl"))])
        self.assertEqual(retention.VETO_TOO_YOUNG,
                         self.vetoed(plan)[str(Path("ledger/staging/current.jsonl"))])
        self.assertNotIn(str(Path("ledger/staging/idle.jsonl")), self.pruned(plan))
        self.assertNotIn(str(Path("ledger/staging/current.jsonl")), self.pruned(plan))
        self.assertAccounted(plan)

    def test_the_minimum_age_is_what_protects_the_current_run_not_the_maximum_age(self):
        """The defect this catches: min_age dropped, or folded into max_age.

        With max_age 0 every file is "too old", so only the minimum age can keep
        the run's own output. On the live corpus that is 91,902,560 B -- 97.5% of
        ledger/staging -- which the next run would rewrite byte for byte.
        """
        f = self.fixture
        f.staging("current", digest_of("d-current"), age_days=1.0 / 24.0, pad=1000)
        f.staging("yesterday", digest_of("d-yesterday"), age_days=1.0, pad=1000)
        f.staging("older", digest_of("d-older"), age_days=3.0, pad=1000)
        plan = f.plan(max_age_days=0.0, min_age_hours=48.0)

        self.assertEqual(retention.VETO_TOO_YOUNG,
                         self.vetoed(plan)[str(Path("ledger/staging/current.jsonl"))])
        self.assertEqual(retention.VETO_TOO_YOUNG,
                         self.vetoed(plan)[str(Path("ledger/staging/yesterday.jsonl"))])
        self.assertIn(str(Path("ledger/staging/older.jsonl")), self.pruned(plan))
        self.assertAccounted(plan)


class ByteBound(RetentionCase):
    def _three(self):
        f = self.fixture
        f.staging("oldest", digest_of("d1"), age_days=10.0, pad=1000)
        f.staging("middle", digest_of("d2"), age_days=8.0, pad=1000)
        f.staging("current", digest_of("d3"), age_days=1.0 / 24.0, pad=20000)
        return f

    def test_the_byte_bound_takes_the_oldest_eligible_file_first(self):
        f = self._three()
        total = sum(p.stat().st_size for p in (f.root / "ledger/staging").iterdir())
        # A budget one byte under the smallest single file's worth of slack, so
        # exactly one file has to go.
        oldest = (f.root / "ledger/staging/oldest.jsonl").stat().st_size
        plan = f.plan(max_age_days=365.0, min_age_hours=48.0, max_bytes=total - 1)

        self.assertEqual([str(Path("ledger/staging/oldest.jsonl"))],
                         [c.relative for c in plan.prune])
        self.assertEqual(retention.PRUNE_OVER_BUDGET, plan.prune[0].reason)
        self.assertEqual(total - oldest, plan.managed_bytes_after)
        self.assertEqual(0, plan.residual_bytes)
        self.assertAccounted(plan)

    def test_the_byte_bound_never_takes_an_ineligible_file_however_big_it_is(self):
        """The defect this catches: the cap sorting ALL files by size or age.

        `current.jsonl` is the biggest file here and the oldest-first order would
        reach it second. It is younger than the minimum age, so it must be out of
        the cap's reach entirely -- and the shortfall must be reported, not
        covered by deleting it.
        """
        f = self._three()
        plan = f.plan(max_age_days=365.0, min_age_hours=48.0, max_bytes=1000)
        current = str(Path("ledger/staging/current.jsonl"))

        self.assertNotIn(current, self.pruned(plan))
        self.assertEqual(retention.VETO_TOO_YOUNG, self.vetoed(plan)[current])
        self.assertEqual([str(Path("ledger/staging/oldest.jsonl")),
                          str(Path("ledger/staging/middle.jsonl"))],
                         [c.relative for c in plan.prune])
        self.assertGreater(plan.residual_bytes, 0,
                           "a cap that cannot be met must say so, not go quiet")
        self.assertEqual(plan.managed_bytes_after - 1000, plan.residual_bytes)
        self.assertIn("could not be met from eligible files", plan.residual_reason)
        self.assertAccounted(plan)

    def test_a_cap_that_is_met_reports_no_residual(self):
        """The paired negative: `residual_bytes` must not be a constant."""
        f = self._three()
        plan = f.plan(max_age_days=365.0, min_age_hours=48.0, max_bytes=1 << 30)
        self.assertEqual(0, plan.residual_bytes)
        self.assertIsNone(plan.residual_reason)
        self.assertEqual([], plan.prune)


class CitedEvidenceIsNeverPruned(RetentionCase):
    """The veto, against the age AND the byte rule, both of which would take it."""

    def _three_pairs(self, *, age_days: float = 400.0):
        f = self.fixture
        by_digest = digest_of("cited-by-digest")
        by_uri = digest_of("cited-by-uri")
        orphan = digest_of("cited-by-nothing")
        f.raw_pair("src.a", by_digest, age_days=age_days, body=b"B" * 4000)
        uri_bin, _ = f.raw_pair("src.b", by_uri, age_days=age_days, body=b"U" * 4000)
        f.raw_pair("src.c", orphan, age_days=age_days, body=b"O" * 4000)
        # Cited by digest ONLY, and the stored path deliberately names a data
        # directory this one is not: that is what a restored or relocated corpus
        # looks like, and a path-only veto misses it while the bytes are the same.
        f.cite(storage_uri=Path("/somewhere/else/entirely") / f"{by_digest}.bin",
               sha256=by_digest)
        # Cited by path only, with no recorded sha256 at all.
        f.cite(storage_uri=uri_bin, sha256=None)
        return f, by_digest, by_uri, orphan

    def test_an_artifact_the_corpus_cites_survives_an_age_rule_that_would_take_it(self):
        f, by_digest, by_uri, orphan = self._three_pairs()
        plan = f.plan(max_age_days=1.0, min_age_hours=48.0)
        vetoed, pruned = self.vetoed(plan), self.pruned(plan)

        self.assertEqual(retention.VETO_CITED_DIGEST,
                         vetoed[str(Path(f"ledger/raw/src.a/{by_digest[:2]}/{by_digest}.bin"))])
        self.assertEqual(retention.VETO_CITED_URI,
                         vetoed[str(Path(f"ledger/raw/src.b/{by_uri[:2]}/{by_uri}.bin"))])
        for relative in list(vetoed):
            self.assertNotIn(relative, pruned)
        # and the one nothing cites IS taken, so the veto is not just "keep all"
        self.assertIn(str(Path(f"ledger/raw/src.c/{orphan[:2]}/{orphan}.bin")), pruned)
        self.assertAccounted(plan)

    def test_an_artifact_the_corpus_cites_survives_a_byte_cap_that_would_take_it(self):
        f, by_digest, by_uri, orphan = self._three_pairs()
        plan = f.plan(max_age_days=100000.0, min_age_hours=48.0, max_bytes=1)
        pruned = self.pruned(plan)

        for digest, source in ((by_digest, "src.a"), (by_uri, "src.b")):
            for suffix in (".bin", ".json"):
                relative = str(Path(f"ledger/raw/{source}/{digest[:2]}/{digest}{suffix}"))
                self.assertNotIn(relative, pruned,
                                 f"the byte cap took cited evidence: {relative}")
        self.assertIn(str(Path(f"ledger/raw/src.c/{orphan[:2]}/{orphan}.bin")), pruned)
        self.assertGreater(plan.residual_bytes, 0)
        self.assertAccounted(plan)

    def test_the_veto_reads_the_raw_columns_so_a_corrupt_citation_still_protects(self):
        """`irreplaceable_files` DROPS a row whose bytes mismatch their sha256.

        That is right for a backup manifest and exactly wrong as a veto basis: a
        cited-but-corrupt artifact is the file that must not be deleted, and it
        is the one missing from that output. Here the recorded sha256 does not
        match the bytes on disk, so `irreplaceable_files` reports a problem and
        omits the file -- and the veto must still hold.
        """
        from backup_evidence import irreplaceable_files

        f = self.fixture
        claimed = digest_of("what-the-corpus-thinks-is-there")
        path, _ = f.raw_pair("src.a", claimed, age_days=400.0, body=b"DIFFERENT BYTES")
        f.cite(storage_uri=path, sha256=claimed)
        corpus = f.corpus()

        files, problems = irreplaceable_files(corpus, f.root)
        self.assertEqual([], files, "the fixture must reproduce the drop, or it proves nothing")
        self.assertTrue(any("no longer matches" in p for p in problems))

        plan = retention.plan(f.root, now=NOW, max_bytes=1,
                              policy=retention.default_policy(max_age_days=1.0))
        self.assertNotIn(str(path.relative_to(f.root)), self.pruned(plan))
        self.assertEqual(retention.VETO_CITED_DIGEST,
                         self.vetoed(plan)[str(path.relative_to(f.root))])

    def test_an_unreadable_corpus_refuses_the_whole_prune_rather_than_citing_nothing(self):
        """With no citation basis every file looks uncited. Absence is not an answer."""
        f = self.fixture
        orphan = digest_of("would-be-pruned")
        f.raw_pair("src.c", orphan, age_days=400.0, body=b"O" * 4000)
        f.staging("retired", orphan, age_days=400.0, pad=100)
        plan = retention.plan(f.root, now=NOW, max_bytes=1,       # no corpus written at all
                              policy=retention.default_policy(max_age_days=1.0))

        self.assertEqual([], plan.prune)
        self.assertIsNotNone(plan.refused)
        self.assertIn(retention.VETO_CORPUS_UNREADABLE, plan.refused)
        self.assertEqual(set(self.vetoed(plan).values()), {retention.VETO_CORPUS_UNREADABLE})
        self.assertGreater(plan.residual_bytes, 0)
        self.assertAccounted(plan)
        # A FIRST BUILD, not a fault: run_batch creates the corpus after this
        # runs, and batch.py must not raise an ALARM for the normal first-run
        # state (tests/test_batch_logging.py asserts a healthy run logs none).
        self.assertEqual(retention.REFUSED_NO_CORPUS, plan.refused_code)

    def test_a_corpus_that_exists_and_will_not_answer_is_a_fault_not_a_first_build(self):
        """The paired positive. One refusal code for both would make the ALARM
        batch.py raises either always-on or always-off."""
        f = self.fixture
        orphan = digest_of("would-be-pruned")
        f.raw_pair("src.c", orphan, age_days=400.0, body=b"O" * 4000)
        (f.root / "corpus.sqlite").write_bytes(b"this is not a database")
        plan = retention.plan(f.root, now=NOW, max_bytes=1,
                              policy=retention.default_policy(max_age_days=1.0))

        self.assertEqual([], plan.prune)
        self.assertEqual(retention.REFUSED_CORPUS_UNREADABLE, plan.refused_code)
        self.assertFalse(plan.corpus_readable)


class ThePairIsIndivisible(RetentionCase):
    def test_a_cited_bin_protects_its_sidecar_and_the_sidecar_says_why(self):
        f = self.fixture
        digest = digest_of("cited")
        body, sidecar = f.raw_pair("src.a", digest, age_days=400.0, body=b"B" * 9000)
        f.cite(storage_uri=body, sha256=None)
        plan = f.plan(max_age_days=1.0, min_age_hours=48.0, max_bytes=1)
        vetoed = self.vetoed(plan)

        self.assertEqual(retention.VETO_CITED_URI, vetoed[str(body.relative_to(f.root))])
        # Reported, never silently skipped: the sidebar is cited by nothing, and
        # `import_run` opens it before the body.
        self.assertEqual(retention.VETO_PAIRED_WITH_PROTECTED,
                         vetoed[str(sidecar.relative_to(f.root))])
        self.assertEqual([], plan.prune)

    def test_every_group_is_pruned_whole_or_not_at_all(self):
        f = self.fixture
        cited, orphan = digest_of("cited"), digest_of("orphan")
        body, _ = f.raw_pair("src.a", cited, age_days=400.0, body=b"B" * 9000)
        f.raw_pair("src.c", orphan, age_days=400.0, body=b"O" * 9000)
        f.cite(storage_uri=body, sha256=None)
        plan = f.plan(max_age_days=1.0, min_age_hours=48.0)

        groups: dict[str, set[bool]] = {}
        for candidate in plan.prune:
            groups.setdefault(Path(candidate.relative).stem, set()).add(True)
        for candidate in plan.vetoed:
            groups.setdefault(Path(candidate.relative).stem, set()).add(False)
        for stem, verdicts in groups.items():
            self.assertEqual(1, len(verdicts),
                             f"{stem}: one half of an indivisible pair was treated differently")
        self.assertEqual({str(Path(f"ledger/raw/src.c/{orphan[:2]}/{orphan}.bin")),
                          str(Path(f"ledger/raw/src.c/{orphan[:2]}/{orphan}.json"))},
                         set(self.pruned(plan)))


class StagingDecidesWhichRawSurvives(RetentionCase):
    def test_a_digest_a_surviving_staging_file_still_references_is_vetoed(self):
        """And the one whose only referrer is being pruned is not.

        This is why `plan()` does staging before raw. Both raw pairs here are 400
        days old and cited by nothing, so age alone takes both; what separates
        them is whether a staging file that SURVIVES this plan still names them.
        """
        f = self.fixture
        retired, live = digest_of("retired-run"), digest_of("live-run")
        f.staging("retired-run", retired, age_days=400.0, pad=100)
        f.staging("live-run", live, age_days=1.0 / 24.0, pad=100)
        f.raw_pair("src.a", retired, age_days=400.0, body=b"R" * 5000)
        f.raw_pair("src.b", live, age_days=400.0, body=b"L" * 5000)
        plan = f.plan(max_age_days=14.0, min_age_hours=48.0)
        pruned, vetoed = self.pruned(plan), self.vetoed(plan)

        self.assertEqual(retention.PRUNE_TOO_OLD,
                         pruned[str(Path("ledger/staging/retired-run.jsonl"))])
        self.assertEqual(retention.VETO_TOO_YOUNG,
                         vetoed[str(Path("ledger/staging/live-run.jsonl"))])
        for suffix in (".bin", ".json"):
            self.assertIn(str(Path(f"ledger/raw/src.a/{retired[:2]}/{retired}{suffix}")), pruned)
            self.assertEqual(
                retention.VETO_STAGED,
                vetoed[str(Path(f"ledger/raw/src.b/{live[:2]}/{live}{suffix}"))])
        self.assertEqual(1, plan.staged_digests, "only the surviving file's digest counts")
        self.assertAccounted(plan)


class LogRotation(RetentionCase):
    def _log(self, size: int, generations: dict[int, int]) -> Path:
        log = self.fixture.file("batch.log", b"L" * size, age_days=0.0)
        for number, length in generations.items():
            self.fixture.file(f"batch.log.{number}", bytes([number]) * length, age_days=0.0)
        return log

    def test_rotation_keeps_the_rotated_generation_and_drops_the_oldest(self):
        f = self.fixture
        log = self._log(5000, {1: 11, 2: 22})
        plan = f.plan(log_max_bytes=1000, log_keep=2)
        self.assertTrue(plan.log["rotate"])
        self.assertEqual(["batch.log.2"], plan.log["remove"])

        report = retention.apply(plan, f.root)
        self.assertTrue(report["log"]["rotated"])
        self.assertFalse(log.exists(), "batch.log must be out of the way for a fresh handle")
        self.assertEqual(b"L" * 5000, (f.root / "batch.log.1").read_bytes())
        self.assertEqual(bytes([1]) * 11, (f.root / "batch.log.2").read_bytes())
        self.assertFalse((f.root / "batch.log.3").exists())

    def test_a_log_under_its_cap_is_not_rotated(self):
        f = self.fixture
        log = self._log(500, {})
        plan = f.plan(log_max_bytes=1000, log_keep=2)
        self.assertFalse(plan.log["rotate"])
        retention.apply(plan, f.root)
        self.assertEqual(b"L" * 500, log.read_bytes())
        self.assertFalse((f.root / "batch.log.1").exists())

    def test_lowering_keep_drops_generations_past_it_without_a_rotation(self):
        f = self.fixture
        self._log(500, {1: 11, 2: 22, 3: 33, 4: 44})
        plan = f.plan(log_max_bytes=1000, log_keep=2)
        self.assertFalse(plan.log["rotate"])
        self.assertEqual(["batch.log.3", "batch.log.4"], sorted(plan.log["remove"]))
        retention.apply(plan, f.root)
        self.assertTrue((f.root / "batch.log.2").exists())
        self.assertFalse((f.root / "batch.log.3").exists())
        self.assertFalse((f.root / "batch.log.4").exists())

    def test_rotated_tracks_the_live_file_moving_and_not_the_error_list(self):
        """`rotated` is what makes the caller reattach its log handle.

        `batch_logging` holds `batch.log` open for the whole run, so a rotation
        the caller is not told about leaves it appending to `batch.log.1` for the
        next ten minutes, silently. Driving the flag off the whole error list
        would report False after a successful rename whenever anything else
        failed -- here, a ledger file that vanished between plan and apply.
        """
        f = self.fixture
        log = self._log(5000, {})
        f.staging("retired", digest_of("d1"), age_days=400.0, pad=100)
        plan = f.plan(max_age_days=14.0, log_max_bytes=1000, log_keep=2)
        self.assertEqual(1, len(plan.prune))
        (f.root / plan.prune[0].relative).unlink()        # vanishes under apply()

        report = retention.apply(plan, f.root)
        self.assertTrue(report["errors"], "the fixture must produce an unrelated error")
        self.assertTrue(report["log"]["rotated"],
                        "the live log moved; the caller MUST be told to reattach")
        self.assertFalse(log.exists())

    def test_keeping_zero_generations_refuses_to_rotate_rather_than_delete_the_log(self):
        f = self.fixture
        log = self._log(5000, {1: 11})
        plan = f.plan(log_max_bytes=1000, log_keep=0)
        self.assertFalse(plan.log["rotate"])
        self.assertEqual(["batch.log.1"], plan.log["remove"])
        retention.apply(plan, f.root)
        self.assertEqual(b"L" * 5000, log.read_bytes(), "the live log must survive keep=0")
        self.assertFalse((f.root / "batch.log.1").exists())

    def test_the_log_still_rotates_when_the_corpus_refuses_the_file_prune(self):
        """They are independent, and the refusal case is when the log matters most.

        A box whose corpus will not open is a box whose log is the only thing
        left to read. `batch.log` cites no evidence, so gating its rotation on
        the citation basis couples two unrelated things and leaves the log
        growing exactly when it is needed.
        """
        f = self.fixture
        log = self._log(5000, {})
        f.staging("retired", digest_of("d1"), age_days=400.0, pad=100)
        (f.root / "corpus.sqlite").write_bytes(b"not a database")
        plan = retention.plan(f.root, now=NOW, log_max_bytes=1000, log_keep=2,
                              policy=retention.default_policy(max_age_days=14.0))
        self.assertEqual(retention.REFUSED_CORPUS_UNREADABLE, plan.refused_code)

        report = retention.apply(plan, f.root)
        self.assertEqual(0, report["deleted_files"], "a refusal must prune no file")
        self.assertTrue((f.root / "ledger/staging/retired.jsonl").exists())
        self.assertTrue(report["log"]["rotated"])
        self.assertFalse(log.exists())
        self.assertEqual(b"L" * 5000, (f.root / "batch.log.1").read_bytes())

    def test_rotation_is_bounded_by_bytes_not_by_runs(self):
        """A generation count alone bounds nothing: `keep` files of any size."""
        f = self.fixture
        self._log(999, {})
        self.assertFalse(f.plan(log_max_bytes=1000, log_keep=4).log["rotate"])
        self._log(1000, {})
        self.assertTrue(f.plan(log_max_bytes=1000, log_keep=4).log["rotate"])


class DryRun(RetentionCase):
    def test_a_dry_run_deletes_nothing_and_rotates_nothing(self):
        f = self.fixture
        f.staging("retired", digest_of("d1"), age_days=400.0, pad=5000)
        f.staging("also-retired", digest_of("d2"), age_days=300.0, pad=5000)
        f.file("batch.log", b"L" * 9000, age_days=0.0)
        f.file("batch.log.1", b"1" * 10, age_days=0.0)

        plan = f.plan(max_age_days=14.0, log_max_bytes=1000, log_keep=1)
        # Snapshot AFTER planning: plan() is read-only over the trees but writes
        # the fixture's corpus, and a dry run has to be compared against the
        # state apply() is handed, not against the state before it was built.
        before = {p: p.read_bytes() for p in sorted(f.root.rglob("*")) if p.is_file()}
        self.assertEqual(2, len(plan.prune), "a dry run over nothing proves nothing")
        self.assertTrue(plan.log["rotate"])

        report = retention.apply(plan, f.root, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(0, report["deleted_files"])
        self.assertEqual(0, report["deleted_bytes"])
        self.assertFalse(report["log"]["rotated"])
        self.assertEqual(before, {p: p.read_bytes() for p in sorted(f.root.rglob("*"))
                                 if p.is_file()})
        # and the same plan applied for real DOES delete, so the dry run is the
        # flag doing something rather than the plan being empty
        report = retention.apply(f.plan(max_age_days=14.0, log_max_bytes=1000, log_keep=1),
                                 f.root)
        self.assertEqual(2, report["deleted_files"])
        self.assertFalse((f.root / "ledger/staging/retired.jsonl").exists())


class TheInstrumentItself(RetentionCase):
    def test_the_real_migrated_schema_answers_the_query_the_veto_asks(self):
        """The fixture above hand-builds `artifacts`. This is what stops that
        from passing against a schema that has moved underneath it."""
        from mobile_observatory.database import Database

        with TemporaryDirectory() as tmp:
            database = Database.migrated(Path(tmp) / "corpus.sqlite")
            try:
                database.connection.execute(
                    "SELECT storage_uri, sha256 FROM artifacts LIMIT 1").fetchall()
            finally:
                database.close()

    def test_the_policy_plans_staging_before_raw(self):
        """Load-bearing order, asserted rather than left to a comment."""
        names = [rule.name for rule in retention.default_policy()]
        self.assertLess(names.index("ledger/staging"), names.index("ledger/raw"))
        staging = next(r for r in retention.default_policy() if r.name == "ledger/staging")
        raw = next(r for r in retention.default_policy() if r.name == "ledger/raw")
        self.assertTrue(staging.carries_artifact_digests)
        self.assertFalse(raw.carries_artifact_digests)

    def test_the_report_line_survives_a_real_logger_and_names_every_number(self):
        """Through `logging`, not through the `%` operator.

        This test used to assert `template % args` and passed while the batch's
        own `logger.info(*log_fields(report))` raised inside the handler for
        every single run: logging unwraps a single mapping argument and nothing
        else, so the tuple arrived whole and `msg % self.args` saw one argument
        for fourteen `%s`. logging reports that as a "Logging error" on stderr
        and carries on, so the run looked fine and the line was simply absent
        from `batch.log`. A formatter is the only thing that can prove this.
        """
        import logging

        f = self.fixture
        f.staging("retired", digest_of("d1"), age_days=400.0, pad=100)
        report = retention.apply(f.plan(max_age_days=14.0), f.root, dry_run=True)
        template, fields = retention.log_fields(report)
        self.assertEqual(template.count("%s"), len(fields))

        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))      # raises if it cannot format

        logger = logging.getLogger("test_retention_line")
        logger.handlers = [Capture()]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logging.raiseExceptions = True          # do not let logging swallow it
        try:
            logger.info(template, *fields)      # the batch's own spelling
        finally:
            logger.handlers = []

        self.assertEqual(1, len(records))
        for field in ("pruned=", "vetoed=", "kept=", "managed_before=", "managed_after=",
                      "budget=", "residual=", "accounted_files="):
            self.assertIn(field, records[0])
        self.assertNotIn("%s", records[0], "a field was never interpolated")

    def test_batch_logs_the_retention_line_into_batch_log(self):
        """End to end through `batch._prune`, because that is the caller that broke.

        `run_batch` is not involved: the line has to be in the file on its own.
        """
        from unittest.mock import patch
        from mobile_observatory import batch

        f = self.fixture
        f.staging("retired", digest_of("d1"), age_days=400.0, pad=100)
        f.corpus()
        argv = ["mobile_observatory.batch", "--data-dir", str(f.root),
                "--legacy-root", str(f.root)]
        with patch.object(batch, "run_batch", return_value={"totals": {}, "silence": []}), \
                patch.object(sys, "argv", argv):
            batch.main()

        text = (f.root / "batch.log").read_text()
        self.assertIn("retention pruned=1", text)
        self.assertIn("accounted_files=", text)
        self.assertNotIn("%s", text)
        self.assertFalse((f.root / "ledger/staging/retired.jsonl").exists(),
                         "the batch must actually prune, not only report")


if __name__ == "__main__":
    unittest.main()
