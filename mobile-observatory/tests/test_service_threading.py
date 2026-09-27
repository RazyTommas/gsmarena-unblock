"""The OTHER shared connection: ObservatoryService.local.

bc54d25 gave every request thread its own corpus connection. It left
`ObservatoryService.local` as one connection opened with
check_same_thread=False, and a comment claiming "every one of its uses is
already inside that lock". That claim was false for five request-reachable
methods, which is the same defect bc54d25 fixed, one database over.

Two tests, deliberately different in kind:

* the static one is the durable guard. It reads the source and fails the
  moment a `self.local` use appears outside `with self.local_lock`, whether
  or not a race happens to trigger on the day CI runs.
* the dynamic one proves the defect is real rather than theoretical. Races
  are probabilistic, so it is written to be sensitive (many threads, many
  iterations, mixed read and write) and it asserts on zero failures, not on
  a rate.

Measured at 4a51462 with the five methods unguarded: the dynamic test failed
with InterfaceError('bad parameter or other API misuse') and with
TypeError from json.loads() on a row read mid-write. Both pass once the
missing `with self.local_lock` blocks are restored.
"""
import ast
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.server import ObservatoryService  # noqa: E402

SERVER_SOURCE = ROOT / "src" / "mobile_observatory" / "server.py"


class LocalConnectionLockCoverageTest(unittest.TestCase):
    """Every `self.local` touch outside __init__ must be under the lock."""

    def test_every_self_local_use_is_lock_guarded(self):
        source = SERVER_SOURCE.read_text()
        tree = ast.parse(source)
        lines = source.splitlines()

        guarded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.With, ast.AsyncWith)):
                header = " ".join(lines[node.lineno - 1:node.body[0].lineno - 1])
                if "local_lock" in header:
                    guarded.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

        # __init__ runs before the server accepts connections, so it is single
        # threaded by construction and does not need the lock.
        setup: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "__init__":
                setup.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

        offenders = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "local"
                    and isinstance(node.value, ast.Name) and node.value.id == "self"
                    and node.lineno not in guarded and node.lineno not in setup):
                offenders.append(f"server.py:{node.lineno}: {lines[node.lineno - 1].strip()}")

        self.assertEqual(
            [], sorted(set(offenders)),
            "self.local is a single connection shared by every request thread. "
            "These uses are outside `with self.local_lock`:\n  " + "\n  ".join(sorted(set(offenders))),
        )


class ServiceConcurrencyTest(unittest.TestCase):
    """Drive the service methods themselves from many threads at once."""

    def _service(self) -> ObservatoryService:
        corpus = Database.migrated(":memory:", check_same_thread=False)
        local = Path(self.enterContext(tempfile.TemporaryDirectory())) / "local.sqlite"
        return ObservatoryService(corpus, local, demonstration=True)

    def test_concurrent_reads_and_writes_do_not_corrupt_the_local_connection(self):
        service = self._service()
        failures: list[str] = []
        barrier = threading.Barrier(16)

        def worker(index: int) -> None:
            barrier.wait()
            for iteration in range(40):
                try:
                    # A read, a write, and a read-that-parses-what-the-write-wrote.
                    service.config()
                    service.identity_decisions()
                    service.collection_requests()
                    if index % 3 == 0:
                        service.save_config({"cadenceHours": 1 + (iteration % 23),
                                             "preferredRegions": [], "enabledSources": [],
                                             "supportedOnly": True})
                except Exception as error:  # noqa: BLE001 - the point is to catch anything
                    failures.append(f"thread {index} iteration {iteration}: {error!r}")
                    return

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], failures[:8],
                         f"{len(failures)} of 16 threads failed against the shared local connection")


if __name__ == "__main__":
    unittest.main()
