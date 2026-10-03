#!/usr/bin/env python3
"""Restore a packaged historical snapshot and serve it.

READ THIS BEFORE CHANGING THE DEFAULT BACK.

This script used to be the README's first instruction and it extracted a
hardcoded `portable/mobile-observatory-2026-09-17.zip`. It worked. It also
produced a corpus a TENTH the size of the one this repository can build --
measured 83 devices / 26,961 observations against 854 / 94,969 from
`PYTHONPATH=src python3 -m mobile_observatory.batch` -- and nothing downstream said so. Every
page, every count, every export was internally consistent and answered from a
corpus that was missing 771 phones. A ten-times-wrong answer that looks
successful is the worst failure shape this product has, because the reader has
no way in and no reason to doubt it.

So the default is now a REFUSAL that names what to run instead, and the
snapshot path is still here behind `--bundle`, which:

  * picks the NEWEST manifest in portable/ that has a zip beside it, instead of
    hardcoding a date -- the hardcode is how 09-17 stayed the default after
    09-22 was packaged;
  * reports every manifest whose zip is MISSING rather than ignoring it, because
    `mobile-observatory-2026-09-22.json` has sat beside no zip for eleven days
    and the only place that was written down was a handoff's open-items list;
  * prints the chosen bundle's own stated row counts, read from its manifest
    rather than hardcoded here, so the number cannot go stale while the file
    moves on.

The bundle is not deleted and this is not a deprecation: an air-gapped box with
no captured inputs genuinely has no other route, `--restore-only` is documented,
and docs/PORTABLE_RUN.md describes the format. What changed is that choosing the
smaller corpus is now a decision somebody makes rather than the first thing the
README tells them to type.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent
BUNDLES=ROOT/'portable'
# The historical default. Kept as a module attribute because that is what it is
# -- a default -- and because tests/test_portable.py substitutes it to exercise
# restore() against a synthetic bundle. select_bundle() overrides it for a real
# run so the newest packaged snapshot wins over whichever date was typed here.
ARCHIVE=BUNDLES/'mobile-observatory-2026-09-17.zip'

# Measured on a real clone, 2026-09-29, by running the batch end to end. Dated
# rather than bare: it is a measurement of one corpus at one time, and a reader
# comparing it against their own run needs to know which.
BATCH_MEASUREMENT='measured 2026-09-29: 854 devices / 94,969 observations, 9m50s, no flags needed'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bundle_inventory(directory=BUNDLES):
    """Every packaged snapshot this checkout declares, newest manifest first.

    Returns a list of dicts carrying the manifest path, whether the zip it names
    is actually present, and whatever row counts the manifest states. A manifest
    with no zip is INCLUDED and flagged, never skipped -- that is the 09-22 case,
    and a silent skip is why nobody noticed.
    """
    found=[]
    for manifest_path in sorted(Path(directory).glob('*.json'),reverse=True):
        try:
            manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:
            found.append({'manifest':manifest_path,'archive':None,'present':False,
                          'error':f'{type(exc).__name__}: {exc}','counts':{}})
            continue
        archive=Path(directory)/manifest.get('archive',manifest_path.with_suffix('.zip').name)
        counts=manifest.get('corpus_row_counts') or {}
        found.append({'manifest':manifest_path,'archive':archive,
                      'present':archive.is_file(),'error':None,'counts':counts,
                      'packaged_at':manifest.get('packaged_at')})
    return found


def describe_counts(counts):
    """What a bundle says it holds, or an honest statement that it does not say.

    Never a zero and never a guess: a bundle whose manifest predates
    corpus_row_counts states nothing, and reporting that as 0 devices would be
    inventing a value to fill a blank.
    """
    if not counts:
        return 'its manifest states no row counts'
    devices=counts.get('hardware_models')
    observations=counts.get('observations')
    parts=[]
    if devices is not None: parts.append(f'{devices:,} devices')
    if observations is not None: parts.append(f'{observations:,} observations')
    return ' / '.join(parts) if parts else 'its manifest states no row counts'


def select_bundle(inventory=None):
    """The newest packaged snapshot that actually has a zip. Raises if none does."""
    inventory=bundle_inventory() if inventory is None else inventory
    usable=[b for b in inventory if b['present']]
    if not usable:
        raise ValueError(
            'no packaged snapshot in portable/ has a zip beside its manifest:\n'
            +'\n'.join(f"  {b['manifest'].name} -> {b['archive'].name if b['archive'] else '?'}"
                       ' (missing)' for b in inventory))
    return usable[0]


def bundle_refusal(inventory=None):
    """Why `python3 run.py` refuses, and the two lines that do the right thing."""
    inventory=bundle_inventory() if inventory is None else inventory
    usable=[b for b in inventory if b['present']]
    chosen=usable[0] if usable else None
    lines=[
        'run.py serves a PACKAGED HISTORICAL SNAPSHOT, not this repository\'s corpus.',
        '',
        'It is not what you want for a deployment, and it will not tell you so once',
        'it is running: every page will be internally consistent and answered from a',
        'corpus roughly a tenth the size.',
        '',
        '  packaged snapshot  '+(f"{chosen['archive'].name} -- {describe_counts(chosen['counts'])}"
                                 if chosen else 'none available (see below)'),
        '  this repository    PYTHONPATH=src python3 -m mobile_observatory.batch',
        '                     '+BATCH_MEASUREMENT,
        '',
        'Run this instead, from this directory:',
        '',
        '  PYTHONPATH=src python3 -m mobile_observatory.batch',
        '  PYTHONPATH=src python3 -m mobile_observatory.server --data-dir .observatory-data',
        '',
        'All 16 captured inputs the batch needs are in git; it takes no flags.',
    ]
    missing=[b for b in inventory if not b['present']]
    if missing:
        lines += ['',
                  'Packaged snapshots declared here with NO zip beside them (so --bundle',
                  'cannot reach them, and they are not what you would get):']
        lines += [f"  {b['manifest'].name}"
                  +(f" -> {b['archive'].name} missing" if b['archive'] else ' unreadable')
                  +(f" ({describe_counts(b['counts'])})" if b['counts'] else '')
                  for b in missing]
    lines += ['',
              'If the dated snapshot really is what you want -- an air-gapped box with no',
              'captured inputs, or a comparison against what that date held -- pass',
              '--bundle and it will say which one it chose and what that one holds.']
    return '\n'.join(lines)


def restore(destination):
    if destination.exists():
        if not (destination/'corpus.sqlite').is_file() or not (destination/'local.sqlite').is_file():
            raise ValueError('Existing data directory is incomplete. Choose a new --data-dir; it will not be overwritten.')
        return False
    expected=json.loads(ARCHIVE.with_suffix('.json').read_text())
    if sha(ARCHIVE)!=expected['sha256']:
        raise ValueError('Archive checksum mismatch. Download the repository again.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.observatory-restore-',dir=destination.parent))
    try:
        with zipfile.ZipFile(ARCHIVE) as archive:
            for info in archive.infolist():
                path=(temporary/info.filename).resolve()
                if not path.is_relative_to(temporary.resolve()):
                    raise ValueError('Unsafe archive member')
            archive.extractall(temporary)
        manifest=json.loads((temporary/'manifest.json').read_text())
        for name, expected_file in manifest['files'].items():
            path=temporary/name
            if path.stat().st_size!=expected_file['bytes'] or sha(path)!=expected_file['sha256']:
                raise ValueError(f'Extracted file checksum mismatch: {name}')
        for name in manifest['databases']:
            with sqlite3.connect(temporary/name) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                    raise ValueError(f'Database integrity failure: {name}')
        # Resolve active evidence paths for this installation. Originals remain
        # documented in provenance-paths.json and the historical database backup.
        with sqlite3.connect(temporary/'corpus.sqlite') as db:
            for aid,relative in db.execute('SELECT id,storage_uri FROM artifacts').fetchall():
                target=(destination/relative).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError('Unsafe evidence path')
                db.execute('UPDATE artifacts SET storage_uri=? WHERE id=?',(str(target),aid))
        os.replace(temporary,destination)
        return True
    except Exception:
        shutil.rmtree(temporary,ignore_errors=True)
        raise


def rebase_evidence(data):
    """Keep copied runtime directories usable after moving to another computer.

    Delegates to `mobile_observatory.evidence_paths.rebase`, which is the SAME
    rule tools/backup_evidence.py now runs on a restore. It used to exist only
    here, and the backup path -- the one that exists for moving to another box
    -- did not have it: measured, a restore into a different directory produced
    a corpus citing the original box's paths.

    The hard refusals this used to raise are now a REPORT. A missing or
    mismatched file is named and the rest are still rebased, because stopping at
    the first bad entry left the other twenty rows pointing at a directory that
    is also gone -- a half-moved corpus with no record of which half.
    """
    mapping=data/'provenance-paths.json'
    if not mapping.is_file():return None
    sys.path.insert(0,str(ROOT/'src'))
    from mobile_observatory import evidence_paths
    with sqlite3.connect(data/'corpus.sqlite') as db:
        records=json.loads(mapping.read_text())
        report=evidence_paths.rebase(
            db, data, evidence_paths.entries_from_provenance(records, db))
    for problem in report['missing']+report['mismatched']:
        print(f'  evidence: {problem}',flush=True)
    if report['unresolved_count']:
        print(f"  evidence: {report['unresolved_count']} artifact row(s) still point at a "
              f"path that does not exist; the corpus cites bytes it cannot produce. "
              f"First: {', '.join(report['unresolved'][:3])}",flush=True)
    return report


def announce_pending_migrations(data):
    """Say how far the server about to start will move this database.

    The packaged snapshot is at schema 8 and the application is at 33, so the
    first launch carries somebody's data forward 25 versions -- in place, with
    no prompt and no backup. It did that silently. The server says it too now;
    this says it here as well, because by the time the server speaks the
    operator has already typed the command that did it.
    """
    corpus=data/'corpus.sqlite'
    if not corpus.is_file():return []
    sys.path.insert(0,str(ROOT/'src'))
    from mobile_observatory.database import Database
    db=Database(corpus)
    try:
        pending=db.pending_migrations()
        if pending:
            print(f"  NOTE: this database is at schema {db.schema_version()} and the "
                  f"application is at {pending[-1]}. Starting the server applies "
                  f"{len(pending)} migration(s) IN PLACE, with no backup and no undo. "
                  f"Copy {data} first if you want to keep what you have.",flush=True)
        return pending
    finally:
        db.close()


def main():
    global ARCHIVE
    parser=argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir',type=Path,default=ROOT/'.observatory-data')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8124)
    parser.add_argument('--restore-only',action='store_true')
    parser.add_argument('--bundle',action='store_true',
                        help='Extract and serve the newest packaged historical snapshot. '
                             'Without this run.py refuses, because the snapshot is a tenth '
                             'of the corpus the batch builds from the inputs in this repo.')
    parser.add_argument('--list-bundles',action='store_true',
                        help='Report every packaged snapshot declared in portable/, '
                             'whether its zip is present, and what each states it holds.')
    args=parser.parse_args()

    inventory=bundle_inventory()
    if args.list_bundles:
        for item in inventory:
            state='present' if item['present'] else ('unreadable manifest' if item['error']
                                                     else 'ZIP MISSING')
            print(f"{item['manifest'].name:<40} {state:<20} {describe_counts(item['counts'])}")
        return
    if not args.bundle:
        # Refuse, on stderr, with a nonzero exit. A deployer following a stale
        # instruction gets the right instruction; a script that was piping this
        # into production gets a failure rather than a tenth of the corpus.
        print(bundle_refusal(inventory),file=sys.stderr)
        raise SystemExit(2)

    try:
        chosen=select_bundle(inventory)
    except ValueError as error:
        print(str(error),file=sys.stderr)
        raise SystemExit(2) from None
    ARCHIVE=chosen['archive']
    print(f"Using packaged snapshot {ARCHIVE.name}: {describe_counts(chosen['counts'])}."
          f" This is NOT the corpus `PYTHONPATH=src python3 -m mobile_observatory.batch` builds"
          f" ({BATCH_MEASUREMENT}).",flush=True)
    for item in inventory:
        if not item['present']:
            print(f"  note: {item['manifest'].name} declares"
                  f" {item['archive'].name if item['archive'] else 'an archive'} and it is not"
                  f" in portable/, so it was not a candidate.",flush=True)

    data=args.data_dir.expanduser().resolve()
    created=restore(data)
    rebase_evidence(data)
    print(f"{'Restored bundled snapshot to' if created else 'Using existing data in'} {data}",flush=True)
    announce_pending_migrations(data)
    if args.restore_only:return
    sys.path.insert(0,str(ROOT/'src'))
    from mobile_observatory.server import main as serve
    sys.argv=[sys.argv[0],'--data-dir',str(data),'--host',args.host,'--port',str(args.port)]
    serve()

if __name__=='__main__':main()
