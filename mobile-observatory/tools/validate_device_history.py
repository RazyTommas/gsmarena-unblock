#!/usr/bin/env python3
"""Validate every exact-model detail and ROM page on an isolated snapshot copy.

The supplied corpus is backed up read-only; migrations and local state are created
only in an automatically removed temporary directory.
"""
import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from mobile_observatory import Database
from mobile_observatory.server import ObservatoryService

parser=argparse.ArgumentParser();parser.add_argument('corpus',type=Path);args=parser.parse_args()
with tempfile.TemporaryDirectory(prefix='observatory-device-validation-') as temp:
    root=Path(temp)
    with sqlite3.connect(f'file:{args.corpus.resolve()}?mode=ro',uri=True) as source:
        with sqlite3.connect(root/'corpus.sqlite') as destination:source.backup(destination)
    db=Database.migrated(root/'corpus.sqlite');service=ObservatoryService(db,root/'local.sqlite',demonstration=False)
    models=[row[0] for row in db.connection.execute('SELECT model_code FROM hardware_models')]
    counts=[]
    for model in models:
        detail=service.device_detail(model);seen=set();cursor=0
        while cursor is not None:
            page=service.releases_page({'model_exact':[model],'cursor':[str(cursor)],'limit':['200']})
            assert all(row['model']==model for row in page.items),model
            new={row['id'] for row in page.items};assert not seen&new,model
            seen|=new;cursor=page.metadata()['nextCursor']
        assert len(seen)==detail['firmware']['meta']['page']['total'],model
        assert sum(row['count'] for row in detail['regions'])==len(seen),model
        counts.append((len(seen),model))
    assert sum(count for count,_ in counts)==db.connection.execute('SELECT count(*) FROM firmware_releases').fetchone()[0]
    print(json.dumps({'models':len(models),'releases':sum(count for count,_ in counts),'largest':sorted(counts,reverse=True)[:5]},indent=2))
    service.local.close();db.close()
