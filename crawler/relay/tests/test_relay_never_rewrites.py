"""relay.sync() must never rewrite a commit it did not make.

WHY THIS TEST EXISTS
relay.py used to run `git pull --rebase --autostash` on every send. Two guards
already stood in front of it -- a collector-mid-write check and a dirty-tree
check -- and neither covered the case that actually cost us: a CLEAN tree that
carries local COMMITS. A rebase there replays and flattens them.

On 2026-09-22 that discarded a five-branch integration six separate times while
another agent was working in the same tree. Twice it was invisible rather than
loud: a suite read 132 passed, then 100 passed with 23 test files silently gone;
later, 13 "failures" that were only files being rewritten underneath pytest.
Committing is what you do to PROTECT work, so the one unguarded path was the one
a careful person walks straight into.

WHY THE TEST IS BUILT THE WAY IT IS
relay.py derives REPO from its OWN location (`Path(__file__).parent.parent.parent`).
The first version of this test ran it via runpy from the source tree, so every git
command executed against the SOURCE repo, hit its dirty-tree guard, returned False,
and passed against the old rebase code -- a test that proved nothing. It is now
COPIED into the fixture repo at crawler/relay/relay.py so REPO resolves to the
fixture. Verified by planting the old implementation and watching this fail.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RELAY_SRC = Path(__file__).resolve().parents[1] / "relay.py"


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True).stdout.strip()


class RelayNeverRewritesHistory(unittest.TestCase):

    def _fixture(self, tmp):
        """A bare origin plus a clone that really contains crawler/relay/relay.py."""
        origin = tmp / "origin.git"
        origin.mkdir()
        git(origin, "init", "--bare", "--initial-branch=main", "--quiet")

        work = tmp / "work"
        (work / "crawler" / "relay").mkdir(parents=True)
        git(work, "init", "--initial-branch=main", "--quiet")
        git(work, "config", "user.name", "test")
        git(work, "config", "user.email", "test@example.invalid")
        git(work, "remote", "add", "origin", str(origin))
        shutil.copy(RELAY_SRC, work / "crawler" / "relay" / "relay.py")
        git(work, "add", "-A")
        git(work, "commit", "--quiet", "-m", "base")
        git(work, "push", "--quiet", "-u", "origin", "main")
        return origin, work

    def _run_sync(self, work):
        relay = work / "crawler" / "relay" / "relay.py"
        script = ("import sys, runpy;"
                  "sys.argv=['relay.py'];"
                  f"m=runpy.run_path({str(relay)!r});"
                  "print('SYNC', m['sync']())")
        return subprocess.run([sys.executable, "-c", script], cwd=work,
                              capture_output=True, text=True)

    def test_local_commits_are_not_rewritten_when_origin_moves_ahead(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            origin, work = self._fixture(tmp)

            # a peer delivers to origin
            other = tmp / "other"
            git(tmp, "clone", "--quiet", str(origin), str(other))
            git(other, "config", "user.name", "peer")
            git(other, "config", "user.email", "peer@example.invalid")
            (other / "delivery.csv").write_text("rows\n")
            git(other, "add", "delivery.csv")
            git(other, "commit", "--quiet", "-m", "relay: delivery")
            git(other, "push", "--quiet", "origin", "main")

            # we commit locally: the integration a rebase would flatten
            (work / "integration.py").write_text("value = 1\n")
            git(work, "add", "integration.py")
            git(work, "commit", "--quiet", "-m", "integration: five branches merged")
            before = git(work, "rev-parse", "HEAD")

            # tree is CLEAN and carries a local commit -- the unguarded case
            self.assertEqual(git(work, "status", "--porcelain"), "",
                             "fixture must be clean, or the dirty guard hides the bug")

            proc = self._run_sync(work)
            after = git(work, "rev-parse", "HEAD")

            self.assertEqual(
                before, after,
                "sync() rewrote a local commit.\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}\n"
                "A refusal is correct here; a rewrite is not.")
            self.assertIn("integration: five branches merged",
                          git(work, "log", "--oneline", "-5"))

    def test_no_rebase_or_autostash_call_survives_in_the_source(self):
        """Belt and braces: the behavioural test can be satisfied by a guard someone
        later deletes. This asserts the dangerous call simply is not there."""
        offenders = [l.strip() for l in RELAY_SRC.read_text().splitlines()
                     if ('"--rebase"' in l or '"--autostash"' in l)]
        self.assertEqual(offenders, [],
                         f"relay.py must not invoke git rebase/autostash; found: {offenders}")


if __name__ == "__main__":
    unittest.main()
