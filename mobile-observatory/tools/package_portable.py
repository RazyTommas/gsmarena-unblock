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


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f'{source.resolve().as_uri()}?mode=ro', uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
        dst.execute('PRAGMA journal_mode=DELETE')
        assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not dst.execute('PRAGMA foreign_key_check').fetchall()


def build(data, baseline, ledger, output):
    if output.exists():
        raise FileExistsError(output)
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
                  'databases':[name for name in
                               ('corpus.sqlite','local.sqlite',
                                'history/review-20260916/corpus.sqlite',
                                'history/review-20260916/local.sqlite','legacy/devices.db')
                               if (root/name).is_file()],
                  'notes':'Reviewed captured data, not live. Original absolute paths retained in provenance-paths.json; active artifact paths are portable.'}
        (root/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file(): archive.write(path,path.relative_to(root).as_posix())
        record={'archive':output.name,'sha256':sha(output),'bytes':output.stat().st_size,
                'packaged_at':manifest['packaged_at'],'data_as_of':data_as_of,
                'databases':manifest['databases'],'corpus_row_counts':counts}
        output.with_suffix('.json').write_text(json.dumps(record,indent=2,sort_keys=True)+'\n')
        return record

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--ledger',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(build(a.data_dir,a.baseline,a.ledger,a.output),indent=2))
