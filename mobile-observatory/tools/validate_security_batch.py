#!/usr/bin/env python3
"""Read-only validation of the security read model on a migrated snapshot."""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from mobile_observatory.security_read import query_catalog,detail

parser=argparse.ArgumentParser();parser.add_argument('corpus',type=Path);args=parser.parse_args()
c=sqlite3.connect(f'file:{args.corpus.resolve()}?mode=ro',uri=True);c.row_factory=sqlite3.Row
expected=c.execute('SELECT count(DISTINCT vulnerability_id) FROM advisory_vulnerabilities').fetchone()[0]
seen=[];timings=[]
for offset in range(0,expected,200):
    start=time.perf_counter();rows,total=query_catalog(c,{},200,offset);timings.append(time.perf_counter()-start)
    assert total==expected
    seen.extend(row['cve'] for row in rows)
assert len(set(seen))==expected
cases=[{}, {'mobile_linked':['1']},{'exact_part':['1']},{'fix_status':['with_coordinate']},{'fix_status':['without_coordinate']},{'vendor':['MediaTek']},{'date_from':['2025-01-01'],'date_to':['2025-12-31']}]
counts={}
for query in cases:
    rows,total=query_catalog(c,query,1,0);counts[str(query)]=total
    if rows:
        item=detail(c,rows[0]['cve'])
        assert len(item['claims'])==item['claim_count']
        assert len(item['fixes'])==item['fix_count']
        assert len({x['id'] for x in item['mappedHardware']})==item['device_count']
assert counts[str(cases[3])]+counts[str(cases[4])]==expected
for cve in [seen[0],seen[100],seen[-1]]:
    assert detail(c,cve)['cve']==cve
assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
assert not c.execute('PRAGMA foreign_key_check').fetchall()
print(json.dumps({'cves':expected,'pages':len(timings),'max_page_seconds':round(max(timings),3),'filters':counts,'integrity':'ok'},indent=2))
