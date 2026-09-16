#!/usr/bin/env python3
"""Restore the bundled real snapshot once and serve it using Python 3.11+."""
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
ARCHIVE=ROOT/'portable/mobile-observatory-2026-09-17.zip'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    """Keep copied runtime directories usable after moving to another computer."""
    mapping=data/'provenance-paths.json'
    if not mapping.is_file():return
    with sqlite3.connect(data/'corpus.sqlite') as db:
        for record in json.loads(mapping.read_text()):
            target=(data/record['portable_storage_uri']).resolve()
            if not target.is_relative_to(data.resolve()) or not target.is_file():
                raise ValueError('Missing or unsafe portable evidence path')
            current=db.execute('SELECT storage_uri,sha256 FROM artifacts WHERE id=?',(record['artifact_id'],)).fetchone()
            if current and current[0]!=str(target):
                if sha(target)!=current[1]:raise ValueError('Evidence checksum mismatch after move')
                db.execute('UPDATE artifacts SET storage_uri=? WHERE id=?',(str(target),record['artifact_id']))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=ROOT/'.observatory-data')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8124)
    parser.add_argument('--restore-only',action='store_true')
    args=parser.parse_args()
    data=args.data_dir.expanduser().resolve()
    created=restore(data)
    rebase_evidence(data)
    print(f"{'Restored bundled snapshot to' if created else 'Using existing data in'} {data}",flush=True)
    if args.restore_only:return
    sys.path.insert(0,str(ROOT/'src'))
    from mobile_observatory.server import main as serve
    sys.argv=[sys.argv[0],'--data-dir',str(data),'--host',args.host,'--port',str(args.port)]
    serve()

if __name__=='__main__':main()
