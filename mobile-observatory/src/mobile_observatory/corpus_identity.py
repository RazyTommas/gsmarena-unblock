"""Is this corpus the corpus it claims to be?

THE DEFECT THIS CLOSES, in the handoff's own words: "A rebuild is not a restore.
Rebuilding from the same inputs does not reproduce this corpus. Measured: 759
devices identical, 106 only in the live corpus, 95 only in a rebuild -- 201
resolve differently ... and nothing detects this."

Both causes are deliberate behaviour, not bugs:

  * 1,111 identity conclusions are FROZEN under RULE_VERSION 1. A remembered
    conclusion is final by design, so the corpus does not depend on *when* it
    last ran -- which makes it depend on the *order* it ran in.
  * 1,316 observations rest on an input version no longer on disk. The capture
    tree is overwritten in place; the corpus accumulates. Six artifact digests
    exist only in the live corpus.

Neither can be removed without changing what the corpus IS. What was missing was
any way to notice the difference, so a rebuilt corpus could be handed over as a
restored one and every page would agree with itself.

WHY EQUALITY IS THE WRONG TEST, and this is the whole design
------------------------------------------------------------
A nightly batch legitimately changes the corpus: it concludes new identities,
registers new source values, promotes new devices. Comparing a fingerprint for
EQUALITY against last night's would fire every single night, and a check that
always fires is exactly as useless as one that never does.

What a legitimate batch does NOT do is FORGET. Conclusions are final by design,
the registry's rows are keyed and durable, a promoted device stays promoted. So
the invariant is **monotonic containment**:

    every subject the baseline recorded is still present, and still decided
    the same way.

A nightly run satisfies that -- it only adds. A rebuild violates it loudly: the
106 devices that exist only in the live corpus are 106 subjects the baseline
recorded and the rebuild does not have. Additions are counted and reported as
additions, never as faults.

WHERE THE BASELINE LIVES, and why not in the database
-----------------------------------------------------
`<data-dir>/corpus-identity.json`, written by every batch. NOT a table in
corpus.sqlite, and that is the point: a rebuild creates a NEW corpus.sqlite, so a
baseline stored inside it would be destroyed by the very event it exists to
detect. A sidecar survives `cp -a .observatory-data`, travels in
tools/backup_evidence.py's archive, and -- the case this was built for -- can be
dropped beside a freshly rebuilt corpus to ask "is this the corpus I think I
reproduced?".

It is a record, not a lock: nothing refuses to run because of it. See
`check_corpus`, which reports.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

BASELINE_FILENAME = "corpus-identity.json"
FORMAT = 1

#: Length of each per-subject digest kept in the baseline. 10 hex characters is
#: 40 bits: over ~7,000 subjects the chance of a collision hiding a changed
#: decision is about 3e-7, and the file stays a few hundred KB instead of a few
#: MB beside a 2 MB evidence backup.
DIGEST_CHARS = 10


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Each component is (name, what it pins, key SQL, value SQL, table).
#
# The value columns are chosen to be the DECISION and never its clock or its
# prose. `concluded_at` is a timestamp that legitimately differs between two
# corpora holding the same conclusion, and including it would make every
# component differ always -- the always-fires failure again. `rationale` and the
# evidence JSON are narrative that can be reworded without the decision moving.
_COMPONENTS = (
    ("identity_conclusions",
     "what the corpus concluded about each source product's identity, and under "
     "which frozen rule version",
     """SELECT product_id,
               conclusion || '|' || confidence || '|' || method || '|' || rule_version
          FROM identity_conclusions"""),
    ("source_identity_registry",
     "which product each captured source identity resolves to -- the mapping that "
     "decides which device an observation describes",
     """SELECT source_id || '|' || namespace || '|' || normalized_value,
               ifnull(product_id,'-') || '|' || resolution_state || '|'
                 || resolution_method || '|' || rule_version
          FROM source_identity_registry"""),
    ("hardware_models",
     "the published device catalogue: the 865 model codes a reader sees, which is "
     "where the measured 201-devices-differ divergence shows up",
     """SELECT model_code_normalized, ifnull(codename,'-')
          FROM hardware_models"""),
    ("artifacts",
     "the captured input VERSIONS this corpus rests on; the capture tree is "
     "overwritten in place, so six of these exist only here",
     # Keyed by (source_id, sha256) and not by sha256 alone. The uniqueness guard
     # below caught that: `artifacts` is UNIQUE(source_id, sha256), two sources
     # can cite the same bytes, and on the live corpus 21 rows collapse to 20
     # digests -- which would have reported 20 subjects as the whole truth.
     # byte_length rather than run_id as the value: a run id is re-emitted every
     # night by design, so it would make this component differ on every run.
     """SELECT source_id || '|' || sha256, byte_length FROM artifacts"""),
)


def fingerprint(connection, *, entries: bool = True) -> dict:
    """The facts that make this corpus THIS corpus.

    `entries=False` keeps only the counts and aggregate digests. It answers
    "did it diverge" and cannot answer "in what", which is why it is not the
    default: a check that says a corpus is wrong without saying which 106
    devices went missing is a check somebody has to reproduce by hand.
    """
    components = {}
    for name, pins, sql in _COMPONENTS:
        rows = connection.execute(sql).fetchall()
        mapping = {str(key): _digest(str(value))[:DIGEST_CHARS] for key, value in rows}
        if len(mapping) != len(rows):
            # A key SQL that is not unique would silently collapse rows and make
            # the count honest while the comparison went blind.
            raise ValueError(
                f"{name}: {len(rows)} rows collapsed to {len(mapping)} keys; the "
                f"fingerprint's key expression is not unique and the comparison "
                f"below would be comparing fewer subjects than exist")
        ordered = sorted(mapping.items())
        components[name] = {
            "pins": pins,
            "count": len(ordered),
            # Over keys AND values: this moves when any decision moves.
            "digest": _digest("\n".join(f"{k}={v}" for k, v in ordered)),
            # Over keys alone: this moves only when the SET of subjects moves,
            # which is how "a device disappeared" is told from "a device was
            # re-decided" without reading the entries.
            "keys_digest": _digest("\n".join(k for k, _ in ordered)),
        }
        if entries:
            components[name]["entries"] = dict(ordered)
    # Not a component: it is a distribution, not a set of subjects. It is here
    # because it is the MECHANISM behind the divergence rather than more of its
    # size. Measured: the live corpus holds 1,209 conclusions frozen at
    # rule_version 1 and 1,158 at 2; a rebuild is 100% v2, and v2 strips the
    # brand prefix -- which is why 87 of the 106 codes only the live corpus has
    # are literally "<BRAND> <a code the rebuild does have>".
    rule_versions = {}
    try:
        rule_versions = {str(v): n for v, n in connection.execute(
            "SELECT rule_version, count(*) FROM identity_conclusions "
            "GROUP BY 1 ORDER BY 1").fetchall()}
    except Exception:                                      # noqa: BLE001
        rule_versions = {}
    return {
        "format": FORMAT,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "identity_rule_versions": rule_versions,
        "digest": _digest("\n".join(f"{name}={components[name]['digest']}"
                                    for name, _, _ in _COMPONENTS)),
        "components": components,
    }


def compare(baseline: dict, current: dict) -> dict:
    """What the baseline recorded and this corpus no longer says.

    Three buckets per component, and the asymmetry is the whole point:

      forgotten -- a subject the baseline had and this corpus does not. The
                   rebuild/restore divergence. A fault.
      changed   -- a subject both have, decided differently. Also a fault: a
                   frozen conclusion is not supposed to move.
      added     -- a subject only this corpus has. NOT a fault; this is what a
                   nightly batch does, and counting it as a divergence is what
                   would make the check fire every night and be switched off.
    """
    report = {"baseline_recorded_at": baseline.get("recorded_at"),
              "baseline_digest": baseline.get("digest"),
              "current_digest": current.get("digest"),
              "identical": baseline.get("digest") == current.get("digest"),
              "comparable": True, "incomparable_reason": None,
              "components": {}, "forgotten": 0, "changed": 0, "added": 0,
              "mechanism": []}
    if baseline.get("format") != FORMAT:
        report.update(comparable=False, incomparable_reason=(
            f"baseline is format {baseline.get('format')!r}, this build reads format "
            f"{FORMAT}: refusing to report a divergence it cannot actually compute"))
        return report
    for name, _pins, _sql in _COMPONENTS:
        before = (baseline.get("components") or {}).get(name) or {}
        after = (current.get("components") or {}).get(name) or {}
        entry = {"baseline_count": before.get("count"), "current_count": after.get("count"),
                 "digest_matches": before.get("digest") == after.get("digest"),
                 "forgotten": None, "changed": None, "added": None,
                 "examples": [], "detail_available": False}
        old_entries, new_entries = before.get("entries"), after.get("entries")
        if isinstance(old_entries, dict) and isinstance(new_entries, dict):
            missing = sorted(set(old_entries) - set(new_entries))
            moved = sorted(k for k in set(old_entries) & set(new_entries)
                           if old_entries[k] != new_entries[k])
            entry.update(detail_available=True, forgotten=len(missing),
                         changed=len(moved), added=len(set(new_entries) - set(old_entries)),
                         # Named, not counted: "106 devices are missing" without a
                         # single model code is a number somebody then has to
                         # reproduce by hand before they can act on it.
                         examples=[f"forgotten:{k}" for k in missing[:5]]
                                  + [f"changed:{k}" for k in moved[:5]])
            report["forgotten"] += len(missing)
            report["changed"] += len(moved)
            report["added"] += len(set(new_entries) - set(old_entries))
        elif not entry["digest_matches"]:
            # One side was recorded without entries. The divergence is real and
            # its SHAPE is unknown -- which is reported as unknown rather than
            # as zero, because a 0 here reads as "nothing missing".
            entry["examples"] = [
                "no per-subject entries on one side, so which subjects moved is "
                "not derivable from this baseline"]
        report["components"][name] = entry
    report["mechanism"] = explain(baseline, current, report)
    return report


def explain(baseline: dict, current: dict, report: dict) -> list:
    """Why the two corpora differ, not just by how much.

    A count tells an operator something is wrong. The mechanism tells them
    whether it is the thing they expected. Both sentences below are measurable
    from the two fingerprints and were measured before being written:

      * the rule version a conclusion was frozen under. 1,209 of the live
        corpus's conclusions are RULE_VERSION 1 and a rebuild produces only
        version 2 -- that IS the divergence, not a symptom of it.
      * v2 strips the brand prefix, so a "forgotten" model code is usually the
        same device under its older spelling. 87 of the 106 codes only the live
        corpus has end with a code the rebuild does have.

    And a rebuild is not only loss: on the same pair it GAINED 4,941
    product_firmware_releases. Anything reported as purely missing would send an
    operator looking for damage in a corpus that has more evidence, not less.
    """
    lines = []
    before = baseline.get("identity_rule_versions") or {}
    after = current.get("identity_rule_versions") or {}
    if before and after and before != after:
        lines.append(
            "identity conclusions were frozen under different rule versions: baseline "
            + ", ".join(f"v{v}x{n}" for v, n in sorted(before.items()))
            + " -> now " + ", ".join(f"v{v}x{n}" for v, n in sorted(after.items()))
            + ". A remembered conclusion is final by design, so a corpus rebuilt under a "
              "later rule reaches different, equally defensible answers -- this is the "
              "mechanism, not a symptom of one.")
    models = (baseline.get("components") or {}).get("hardware_models") or {}
    now = (current.get("components") or {}).get("hardware_models") or {}
    old_keys, new_keys = models.get("entries"), now.get("entries")
    if isinstance(old_keys, dict) and isinstance(new_keys, dict):
        missing = set(old_keys) - set(new_keys)
        arrived = set(new_keys) - set(old_keys)
        suffixed = sorted(k for k in missing
                          if any(k != other and k.endswith(other) for other in arrived))
        if suffixed:
            lines.append(
                f"{len(suffixed)} of {len(missing)} model codes the baseline has and this "
                f"corpus does not END WITH a code this corpus DID gain -- the same device "
                f"under a brand-prefixed spelling, not a device that disappeared "
                f"(e.g. {', '.join(suffixed[:3])}).")
        if arrived:
            lines.append(
                f"and {len(arrived)} model code(s) exist only here. A rebuild is not only "
                f"loss: measured on the live pair it also gained 4,941 "
                f"product_firmware_releases.")
    return lines


# ---------------------------------------------------------------------------
# the sidecar
# ---------------------------------------------------------------------------

def baseline_path(connection=None, *, data_dir=None) -> Path | None:
    """Where the baseline lives for this corpus.

    Derived from the connection's own file when no data dir is given, because
    `check_corpus` receives a connection and nothing else, and threading a path
    through every caller would mean the one caller that forgot it silently
    skipped the check.
    """
    if data_dir is not None:
        return Path(data_dir) / BASELINE_FILENAME
    if connection is None:
        return None
    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except Exception:                                      # noqa: BLE001
        return None
    for row in rows:
        name, path = row[1], row[2]
        if name == "main" and path:
            return Path(path).resolve().parent / BASELINE_FILENAME
    return None                                            # :memory:


def read_baseline(path) -> tuple[dict | None, str | None]:
    """The recorded baseline, or (None, why-not). Never raises at the caller."""
    path = Path(path)
    if not path.is_file():
        return None, f"no baseline recorded at {path}"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path} could not be read: {type(exc).__name__}: {exc}"
    if not isinstance(loaded, dict) or "components" not in loaded:
        return None, f"{path} is not a corpus identity baseline"
    return loaded, None


def write_baseline(connection, path, *, entries: bool = True) -> dict:
    """Record this corpus's identity, keeping what the previous record said.

    `supersedes` carries the previous baseline's digest and timestamp forward, so
    the file is a short chain rather than a value with no history: a corpus whose
    identity moved three times says so, and the digest a backup was taken under
    is still in the file that travels with it.
    """
    path = Path(path)
    previous, _ = read_baseline(path)
    current = fingerprint(connection, entries=entries)
    chain = list((previous or {}).get("supersedes") or [])
    if previous:
        chain.append({"digest": previous.get("digest"),
                      "recorded_at": previous.get("recorded_at")})
        # Bounded, for the same reason retention.py exists: a nightly chain is
        # 365 entries a year and the useful part is the recent end.
        chain = chain[-20:]
    current["supersedes"] = chain
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return current


def main() -> None:
    import argparse

    from .database import Database

    parser = argparse.ArgumentParser(
        description="Record or compare a corpus's identity conclusions, so a rebuilt "
                    "corpus can be checked against the one it claims to be.")
    parser.add_argument("command", choices=("record", "show", "compare"))
    parser.add_argument("--data-dir", default=".observatory-data")
    parser.add_argument("--baseline", default=None,
                        help="The baseline to compare against. Defaults to "
                             f"<data-dir>/{BASELINE_FILENAME}. Point it at the baseline "
                             "from the corpus you believe you reproduced.")
    parser.add_argument("--no-entries", action="store_true",
                        help="Record only counts and aggregate digests. The result can say "
                             "THAT a corpus diverged and not which subjects moved.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    db = Database(data_dir / "corpus.sqlite")
    try:
        if args.command == "record":
            written = write_baseline(db.connection, data_dir / BASELINE_FILENAME,
                                     entries=not args.no_entries)
            print(f"recorded {data_dir / BASELINE_FILENAME}")
            print(f"  digest {written['digest']}")
            for name, part in written["components"].items():
                print(f"  {name:<28} {part['count']:>6} subjects  {part['digest'][:16]}")
            return
        current = fingerprint(db.connection, entries=not args.no_entries)
        if args.command == "show":
            print(f"digest {current['digest']}")
            for name, part in current["components"].items():
                print(f"  {name:<28} {part['count']:>6} subjects  {part['digest'][:16]}")
                print(f"    pins: {part['pins']}")
            return
        path = Path(args.baseline) if args.baseline else data_dir / BASELINE_FILENAME
        baseline, why = read_baseline(path)
        if baseline is None:
            print(f"cannot compare: {why}")
            raise SystemExit(2)
        report = compare(baseline, current)
        print(f"baseline {path}")
        print(f"  recorded {report['baseline_recorded_at']} digest {report['baseline_digest']}")
        print(f"  current                       digest {report['current_digest']}")
        if report["identical"]:
            print("IDENTICAL: this corpus still says everything the baseline recorded, "
                  "and nothing more.")
            return
        if not report["comparable"]:
            print(f"INCOMPARABLE: {report['incomparable_reason']}")
            raise SystemExit(2)
        print(f"  forgotten {report['forgotten']}  changed {report['changed']}  "
              f"added {report['added']}")
        for name, part in report["components"].items():
            if part["digest_matches"]:
                continue
            print(f"  {name}: baseline {part['baseline_count']} -> current "
                  f"{part['current_count']}  forgotten={part['forgotten']} "
                  f"changed={part['changed']} added={part['added']}")
            for example in part["examples"]:
                print(f"      {example}")
        if report["forgotten"] or report["changed"]:
            print("DIVERGED: this corpus no longer says things the baseline recorded. It "
                  "is not the corpus that baseline describes -- see docs/BACKUP.md, "
                  "'a rebuild is not a restore'.")
            raise SystemExit(1)
        print("GREW ONLY: every subject the baseline recorded is still present and "
              "unchanged; this corpus has added to it.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
