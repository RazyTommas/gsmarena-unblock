"""Package reviewed SQLite snapshots and evidence without live WAL-file copies."""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from mobile_observatory.enrichment import write_agent_review_bundle
from mobile_observatory.batch_inputs import (BUNDLE_INPUTS_DIRECTORY, REQUIRED_LEGACY_INPUTS,
                                             missing_inputs)

SERVE_ONLY_REASON = (
    'No --legacy-root was given, so this bundle carries no inputs/ directory. It can SERVE the '
    'corpus it shipped with and it CANNOT re-ingest: mobile_observatory.batch reads '
    f'{len(REQUIRED_LEGACY_INPUTS)} captured files from --legacy-root, and a box holding only this '
    'bundle has none of them. Re-package with --legacy-root <captured results tree> to get a '
    'bundle that can run the batch.')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def readme(inputs):
    """The exact commands for the box that has nothing but this archive.

    Written into the bundle rather than into the repository, because the box
    that needs it is the one that unpacked the archive and may never have seen
    the repository's docs.
    """
    lines = ['# Mobile Observatory portable bundle', '',
             'Unpack this archive into a directory and point the application at it.', '',
             '## Serve', '',
             '    PYTHONPATH=<repo>/src python3 -m mobile_observatory.server --data-dir <unpacked>',
             '', '## Re-ingest (batch)', '']
    if inputs['packaged']:
        lines += [
            f"This bundle carries its own input set in `{BUNDLE_INPUTS_DIRECTORY}/` "
            f"({inputs['file_count']} captured files, {inputs['bytes']} bytes), so the batch can "
            'run with no network and no other checkout of the captured data:', '',
            '    PYTHONPATH=<repo>/src python3 -m mobile_observatory.batch \\',
            '        --data-dir <unpacked> \\',
            f"        --legacy-root <unpacked>/{BUNDLE_INPUTS_DIRECTORY}", '',
            'Both paths are inside the unpacked bundle. `<repo>` is the mobile-observatory source '
            'tree, which ships the code and the fixtures/ the batch also reads.', '',
            'The inputs are exactly the files the batch opens, not a mirror of the whole '
            'capture tree.']
    else:
        lines += ['**This bundle is SERVE-ONLY.**', '', SERVE_ONLY_REASON]
    lines += ['', '## Verify', '',
              'Every file in this archive is listed with its sha256 and byte length under `files` '
              'in `manifest.json`. `run.py` checks all of them on restore.', '']
    return '\n'.join(lines)


def backup(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f'{source.resolve().as_uri()}?mode=ro', uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
        dst.execute('PRAGMA journal_mode=DELETE')
        assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not dst.execute('PRAGMA foreign_key_check').fetchall()


def refuse_incomplete_inputs(legacy_root):
    """Raise unless every file the batch opens is present under `legacy_root`.

    Checked as a SET, before anything is copied. A partially packaged input set
    is the worst outcome available here: the batch would start, the adapters
    whose files are present would ingest, the ones missing would be recorded as
    `failed` runs, and the operator would get a smaller corpus and an exit code
    of 0. So a single absent file refuses the whole build, naming every file it
    could not find -- there is no honest way to substitute for one.
    """
    missing = missing_inputs(legacy_root)
    if missing:
        raise ValueError(
            f'Cannot package inputs from {legacy_root}: '
            f'{len(missing)} of {len(REQUIRED_LEGACY_INPUTS)} required files are absent '
            f'({", ".join(missing)}). The batch opens every one of them, so a bundle without '
            'them would either crash or silently ingest less. Point --legacy-root at a complete '
            'captured results tree, or omit it to build an explicitly serve-only bundle.')


def copy_inputs(legacy_root, root):
    """Copy the batch's required input set into the bundle."""
    refuse_incomplete_inputs(legacy_root)
    shipped = []
    for name in REQUIRED_LEGACY_INPUTS:
        destination = root/BUNDLE_INPUTS_DIRECTORY/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(legacy_root)/name, destination)
        shipped.append(name)
    return {'packaged': True, 'directory': BUNDLE_INPUTS_DIRECTORY,
            'source_legacy_root': str(Path(legacy_root).resolve()),
            'required': list(REQUIRED_LEGACY_INPUTS),
            'file_count': len(shipped),
            'bytes': sum((root/BUNDLE_INPUTS_DIRECTORY/name).stat().st_size for name in shipped),
            'batch_command': ('PYTHONPATH=<repo>/src python3 -m mobile_observatory.batch '
                              f'--data-dir <unpacked> --legacy-root <unpacked>/{BUNDLE_INPUTS_DIRECTORY}')}


def build(data, baseline, ledger, output, legacy_root=None):
    if output.exists():
        raise FileExistsError(output)
    # Refused before a single byte is written, so a rejected input set never
    # leaves a half-built archive behind for someone to ship by mistake.
    if legacy_root is not None:
        refuse_incomplete_inputs(legacy_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        for name in ('corpus.sqlite','local.sqlite'):
            backup(data/name,root/name)
            # The baseline is a point of comparison, not a requirement. A corpus
            # that has never been through a review round has none, and refusing
            # to package it would be refusing to ship a new deployment.
            if (baseline/name).is_file():
                backup(baseline/name,root/'history/review-20260916'/name)
        # The legacy crawler database. Looked for beside the corpus first --
        # that is where it actually lives -- with the historical path as a
        # fallback, and SKIPPED rather than fatal when neither exists.
        #
        # This line used to be an unconditional read of
        # <repo>/crawler/data/devices.db, a path that does not exist in the
        # repository. Packaging therefore failed on the first run every time,
        # which is why there was no working way to ship this at all: the ad hoc
        # script someone wrote on another machine was not a convenience, it was
        # the only path, and it was never committed.
        legacy = next((candidate for candidate in
                       (data/'legacy/devices.db', ROOT.parent/'crawler/data/devices.db')
                       if candidate.is_file()), None)
        if legacy is not None:
            backup(legacy, root/'legacy/devices.db')
        shutil.copytree(ledger,root/'ledger')
        if (data/'evidence').is_dir():
            shutil.copytree(data/'evidence',root/'evidence',dirs_exist_ok=True)
        if (data/'product-batch-validation.json').is_file():
            shutil.copy2(data/'product-batch-validation.json',root/'product-batch-validation.json')
        # The batch's own inputs. Without these the bundle serves and can never
        # be refreshed -- see batch_inputs.py. Optional, but never silently so:
        # `inputs.packaged` is false and `inputs.reason` says why, in the
        # manifest and in the bundle's README.
        inputs=(copy_inputs(legacy_root,root) if legacy_root is not None else
                {'packaged':False,'serve_only':True,'reason':SERVE_ONLY_REASON,
                 'required':list(REQUIRED_LEGACY_INPUTS),'file_count':0,'bytes':0})
        (root/'README.md').write_text(readme(inputs))
        db=sqlite3.connect(root/'corpus.sqlite');db.row_factory=sqlite3.Row
        paths=[]
        for artifact in db.execute('SELECT id,sha256,storage_uri FROM artifacts').fetchall():
            source=Path(artifact['storage_uri'])
            if not source.is_file() or sha(source)!=artifact['sha256']:
                raise ValueError(f"Missing or changed evidence: {artifact['id']}")
            relative=f"evidence/artifacts/{artifact['sha256']}.bin"
            destination=root/relative;destination.parent.mkdir(exist_ok=True)
            shutil.copy2(source,destination)
            paths.append({'artifact_id':artifact['id'],'original_storage_uri':str(source),'portable_storage_uri':relative})
            db.execute('UPDATE artifacts SET storage_uri=? WHERE id=?',(relative,artifact['id']))
        db.commit()
        write_agent_review_bundle(db,root/'agent-review')
        tables=[r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts={t:db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
        data_as_of=db.execute('SELECT max(observed_at) FROM observations').fetchone()[0]
        db.close()
        (root/'provenance-paths.json').write_text(json.dumps(paths,indent=2)+'\n')
        files={p.relative_to(root).as_posix():{'sha256':sha(p),'bytes':p.stat().st_size}
               for p in sorted(root.rglob('*')) if p.is_file()}
        manifest={'format':'mobile-observatory-portable-v1','packaged_at':datetime.now(timezone.utc).isoformat(),
                  'data_as_of':data_as_of,'corpus_row_counts':counts,'files':files,
                  'inputs':inputs,
                  'databases':[name for name in
                               ('corpus.sqlite','local.sqlite',
                                'history/review-20260916/corpus.sqlite',
                                'history/review-20260916/local.sqlite','legacy/devices.db')
                               if (root/name).is_file()],
                  'notes':('Reviewed captured data, not live. Original absolute paths retained in '
                           'provenance-paths.json; active artifact paths are portable. '
                           +('Carries the batch input set; see inputs.batch_command and README.md.'
                             if inputs['packaged'] else 'SERVE-ONLY: '+SERVE_ONLY_REASON))}
        (root/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file(): archive.write(path,path.relative_to(root).as_posix())
        record={'archive':output.name,'sha256':sha(output),'bytes':output.stat().st_size,
                'packaged_at':manifest['packaged_at'],'data_as_of':data_as_of,
                'databases':manifest['databases'],'corpus_row_counts':counts,
                'inputs':{k:inputs[k] for k in ('packaged','file_count','bytes') if k in inputs}}
        if not inputs['packaged']:
            record['inputs']['serve_only']=True
        output.with_suffix('.json').write_text(json.dumps(record,indent=2,sort_keys=True)+'\n')
        return record

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--ledger',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--legacy-root','--inputs',type=Path,default=None,dest='legacy_root',
                   help='Captured results tree the batch ingests from (the same path you would '
                        'pass to `mobile_observatory.batch --legacy-root`). Packaged into the '
                        f'bundle under {BUNDLE_INPUTS_DIRECTORY}/ so the unpacked bundle can '
                        're-ingest offline. Omit to build a SERVE-ONLY bundle, which is recorded '
                        'as such in the manifest.')
    a=p.parse_args()
    if a.legacy_root is None:
        print('WARNING: no --legacy-root given; building a SERVE-ONLY bundle. '+SERVE_ONLY_REASON,
              file=sys.stderr)
    print(json.dumps(build(a.data_dir,a.baseline,a.ledger,a.output,a.legacy_root),indent=2))
