"""Where the bytes actually are, measured rather than estimated.

`PRAGMA compile_options` reports ENABLE_DBSTAT_VTAB, so `dbstat` is available:
one row per database page, naming the btree that owns it. That makes per-table
and per-index size a MEASUREMENT. Two of HANDOFF.md's open items were judged on
estimates instead:

  - "no VACUUM anywhere" was weighed against a guess at what VACUUM would
    reclaim. It is 266 free pages of 63,003 -- 1.04 MB of 246.1 MB, 0.42%.
  - "ledger/raw grows without bound" is about the filesystem, not the database,
    and the two were being discussed as one growth problem. They are different
    sizes with different causes, so `directory_sizes()` reports the tree beside
    `measure()`'s pages.

The instrument is checked, not trusted: `measure()` returns `unaccounted_bytes`,
the residual between the file's own page count and what dbstat attributes to a
btree. A residual larger than the freelist means dbstat is not seeing part of
the file and the numbers below it are not a complete account -- which is the
failure a size report must never hide, because a partial total looks exactly
like a small one.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass(frozen=True)
class StorageObject:
    """One btree: a table, an index, or SQLite's own bookkeeping."""

    name: str
    kind: str          # 'table' | 'index' | 'internal'
    table: str         # the table an index belongs to; its own name for a table
    bytes: int
    pages: int
    payload_bytes: int
    unused_bytes: int

    @property
    def fill_ratio(self) -> float:
        """Payload over allocated. A low ratio is fragmentation, not content."""
        return (self.payload_bytes / self.bytes) if self.bytes else 0.0

    def as_dict(self) -> dict:
        return {**asdict(self), "fill_ratio": round(self.fill_ratio, 4)}


def dbstat_available(connection) -> bool:
    """Whether this build actually answers a dbstat query.

    Asks the database, not `compile_options`. A compile option is a claim about
    the library; a successful query is the capability.
    """
    try:
        connection.execute("SELECT name, pgsize FROM dbstat LIMIT 1").fetchall()
    except Exception:
        return False
    return True


def measure(connection) -> dict:
    """Real per-object sizes for the main database.

    `dbstat` is a virtual table that walks every page, so this is O(file) --
    ~0.9s on the 246 MB corpus. A reporting path, never a request path.
    """
    if not dbstat_available(connection):
        return {"available": False,
                "reason": "this SQLite build does not answer a dbstat query "
                          "(needs SQLITE_ENABLE_DBSTAT_VTAB)"}

    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    page_count = connection.execute("PRAGMA page_count").fetchone()[0]
    freelist = connection.execute("PRAGMA freelist_count").fetchone()[0]

    # type and owning table per btree name. Implicit UNIQUE indexes
    # (sqlite_autoindex_*) are in sqlite_schema with a NULL sql, so they are
    # attributed to their table like any other index rather than lumped into an
    # "internal" bucket -- they are 20.6 MB on this corpus, the second largest
    # object in the file, and calling that "internal" would hide it.
    catalogue: dict[str, tuple[str, str]] = {}
    for name, kind, table in connection.execute(
            "SELECT name, type, tbl_name FROM sqlite_schema WHERE type IN ('table','index')"):
        catalogue[name] = (kind, table or name)

    objects: list[StorageObject] = []
    total = 0
    for name, size, pages, payload, unused in connection.execute("""
            SELECT name, sum(pgsize), count(*), sum(payload), sum(unused)
              FROM dbstat GROUP BY name"""):
        kind, table = catalogue.get(name, ("internal", name))
        objects.append(StorageObject(name=name, kind=kind, table=table, bytes=size or 0,
                                     pages=pages, payload_bytes=payload or 0,
                                     unused_bytes=unused or 0))
        total += size or 0
    objects.sort(key=lambda item: -item.bytes)

    file_bytes = page_size * page_count
    return {
        "available": True,
        "page_size": page_size,
        "page_count": page_count,
        "file_bytes": file_bytes,
        "measured_bytes": total,
        "freelist_pages": freelist,
        # What VACUUM would give back, and nothing more. Free pages are the whole
        # of it: VACUUM also repacks, which shrinks `unused` inside live pages,
        # so this is a floor rather than an estimate of the total effect.
        "reclaimable_bytes": freelist * page_size,
        # The instrument's own check. Free pages belong to no btree, so dbstat
        # cannot see them; anything beyond them is unexplained.
        "unaccounted_bytes": file_bytes - total - freelist * page_size,
        "table_bytes": sum(o.bytes for o in objects if o.kind == "table"),
        "index_bytes": sum(o.bytes for o in objects if o.kind == "index"),
        "internal_bytes": sum(o.bytes for o in objects if o.kind == "internal"),
        "objects": [o.as_dict() for o in objects],
    }


def largest(measurement: dict, limit: int = 10) -> list[dict]:
    """The top `limit` objects by bytes. Never a truncated list presented whole:
    callers get `objects` too, and the CLI prints how many were not shown."""
    return measurement.get("objects", [])[:limit]


def by_table(measurement: dict, limit: int = 10) -> list[dict]:
    """Rolled up per table: its own pages plus every index over it.

    "Is this table's growth a problem" is a question about the table AND what
    indexing it costs, which the per-btree list splits apart. On this corpus
    `observations` is 102 MB of rows carrying 39 MB of indexes; reading either
    number alone understates it.
    """
    rolled: dict[str, dict] = {}
    for item in measurement.get("objects", []):
        bucket = rolled.setdefault(item["table"], {"table": item["table"], "bytes": 0,
                                                   "table_bytes": 0, "index_bytes": 0,
                                                   "pages": 0, "indexes": 0})
        bucket["bytes"] += item["bytes"]
        bucket["pages"] += item["pages"]
        if item["kind"] == "index":
            bucket["index_bytes"] += item["bytes"]
            bucket["indexes"] += 1
        else:
            bucket["table_bytes"] += item["bytes"]
    return sorted(rolled.values(), key=lambda item: -item["bytes"])[:limit]


def directory_sizes(data_dir: str | Path) -> list[dict]:
    """On-disk bytes per immediate child of the data directory.

    The other half of "is this growth a problem": `ledger/` is 109 MB of captured
    artifacts that no `dbstat` query can see. Reported beside the pages so the
    two are not confused for one number, and so an answer about the corpus file
    is not mistaken for an answer about the directory.
    """
    root = Path(data_dir)
    if not root.is_dir():
        return []
    entries = []
    for child in sorted(root.iterdir()):
        if child.is_file():
            total, files = child.stat().st_size, 1
        else:
            total, files = 0, 0
            for path in child.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
                    files += 1
        entries.append({"name": child.name, "bytes": total, "files": files,
                        "kind": "file" if child.is_file() else "directory"})
    return sorted(entries, key=lambda item: -item["bytes"])
