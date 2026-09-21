"""Public AOSP source build evidence, separate from OTA/firmware availability."""
from __future__ import annotations
import argparse
from datetime import date
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3

from .enrichment import _capture_evidence, _id
from .product_specs import name_key

SOURCE='google.aosp.source_builds'
URL='https://source.android.com/docs/setup/reference/build-numbers'
HEADERS=['Build ID','Tag','Version','Supported devices','Security patch level']


class BuildTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables=[];self.table=None;self.row=None;self.cell=None

    def handle_starttag(self,tag,attrs):
        if tag=='table':self.table=[]
        elif tag=='tr' and self.table is not None:self.row=[]
        elif tag in ('th','td') and self.row is not None:self.cell=[]
        elif tag=='br' and self.cell is not None:self.cell.append(' ')

    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)

    def handle_endtag(self,tag):
        if tag in ('th','td') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()));self.cell=None
        elif tag=='tr' and self.row is not None:
            self.table.append(self.row);self.row=None
        elif tag=='table' and self.table is not None:
            self.tables.append(self.table);self.table=None


def parse_builds(html: str) -> list[dict]:
    parser=BuildTableParser();parser.feed(html)
    tables=[t for t in parser.tables if t and t[0]==HEADERS]
    if len(tables)!=1:
        raise ValueError('Expected exactly one explicit AOSP build table')
    rows=[]
    for position,row in enumerate(tables[0][1:],2):
        if len(row)!=5 or not row[0] or not row[1]:
            raise ValueError('Malformed AOSP build row')
        build,tag,label,names,patch=row
        if patch:patch=date.fromisoformat(patch).isoformat()
        # Tags themselves explicitly encode the source Android release; this is
        # not an inference from a firmware build ID or marketing name.
        version=re.fullmatch(r'android-(?:security-)?(\d+\.\d+\.\d+)_r\d+(?:\.\d+)?',tag)
        rows.append(dict(build=build,source_tag=tag,version_label=label,
            android_version=version[1] if version else None,
            supported_products=[v.strip() for v in re.split('[,，]',names) if v.strip()],
            security_patch_level=patch or None,locator=f'html:source-build-table:row={position}'))
    return rows


def import_google_builds(c: sqlite3.Connection, capture_path: Path,
                         decisions: list[dict] | None=None) -> dict[str,int]:
    capture=json.loads(capture_path.read_text())
    root=capture_path.parent.resolve();raw=(root/capture['raw_path']).resolve()
    if not raw.is_relative_to(root) or capture.get('source_url')!=URL or capture.get('format_version')!=1:
        raise ValueError('Invalid source capture manifest')
    content=raw.read_bytes()
    if hashlib.sha256(content).hexdigest()!=capture['sha256']:
        raise ValueError('Source artifact hash mismatch')
    rows=parse_builds(content.decode('utf-8'));now=capture['observed_at']
    dbfile=c.execute('PRAGMA database_list').fetchone()[2]
    if dbfile:
        target=Path(dbfile).parent/'evidence'/(capture['sha256']+'.html')
        target.parent.mkdir(exist_ok=True)
        if not target.exists():target.write_bytes(content)
        raw=target
    totals=dict(captured_builds=len(rows),pixel_product_links=0,products_created=0,unassigned_builds=0,blocked_product_links=0)
    if not c.in_transaction:c.execute('BEGIN IMMEDIATE')
    with c:
        header=_capture_evidence(c,source_id=SOURCE,source_name='Google AOSP source build catalog',
            source_url=URL,path=raw,locator='html:source-build-table',excerpt='AOSP source tags and supported-device statements',now=now)
        artifact=c.execute('SELECT artifact_id FROM evidence WHERE id=?',(header,)).fetchone()[0]
        run=c.execute('SELECT run_id FROM artifacts WHERE id=?',(artifact,)).fetchone()[0]
        for row in rows:
            eid=_id('google-source-build-evidence',artifact,row['locator'])
            payload=json.dumps({'data':row,'identity_hints':{'source_product_names':row['supported_products']},
                'evidence':{'source_url':URL,'artifact_pointer':row['locator'],'authority':'aosp-source-build-catalog'}},sort_keys=True)
            oid=_id('google-source-build-observation',artifact,row['locator'])
            c.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (oid,SOURCE,run,artifact,'source_code_build',row['build']+':'+row['source_tag'],now,payload,
                 hashlib.sha256(payload.encode()).hexdigest(),'valid',None))
            c.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,NULL,?,?,?)',
                (eid,artifact,row['locator'],json.dumps(row,sort_keys=True),now))
            c.execute('UPDATE evidence SET observation_id=? WHERE id=? AND observation_id IS NULL',(oid,eid))
            bid=_id('google-source-build',row['build'],row['source_tag'],capture['sha256'])
            c.execute('INSERT OR IGNORE INTO source_build_catalog VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (bid,SOURCE,row['build'],row['source_tag'],row['version_label'],row['android_version'],
                 json.dumps(row['supported_products']),row['security_patch_level'],eid,URL,now))
            if not row['supported_products']:totals['unassigned_builds']+=1
            for name in row['supported_products']:
                # Nexus manufacturer attribution is intentionally left unresolved.
                if not (name=='Pixel' or name.startswith('Pixel ')):continue
                candidates=[p for p in c.execute("SELECT * FROM source_products WHERE manufacturer='Google'")
                            if name_key(p['canonical_name'],'Google')==name_key(name,'Google')]
                pid=candidates[0]['id'] if len(candidates)==1 else _id('google-source-product',name)
                blocked=len(candidates)>1 or any(p['review_state']!='approved' for p in candidates)
                blocked=blocked or bool(c.execute("SELECT 1 FROM source_identity_registry WHERE product_id=? AND resolution_state!='approved'",(pid,)).fetchone())
                blocked=blocked or any(d.get('decision') in ('different','defer') and
                    (d.get('canonical_id')==pid or d.get('source_namespace')==SOURCE and d.get('source_value')==name)
                    for d in (decisions or []))
                if blocked:
                    totals['blocked_product_links']+=1;continue
                if not candidates:
                    before=c.total_changes
                    c.execute('INSERT OR IGNORE INTO source_products VALUES(?,?,?,?,?,NULL,?,?)',
                        (pid,'Google',name,name_key(name,'Google'),'approved',now,now))
                    totals['products_created']+=c.total_changes>before
                c.execute('INSERT OR IGNORE INTO source_identity_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (_id('google-source-product-identity',name),SOURCE,'product_name',name,name.casefold(),pid,
                     'approved','exact_aosp_supported_product_name','1','high',now,now))
                c.execute('INSERT OR IGNORE INTO source_build_product_links VALUES(?,?,?,?)',
                    (bid,pid,'exact_aosp_supported_product_name','high'))
                totals['pixel_product_links']+=1
        c.execute("UPDATE artifacts SET media_type='text/html' WHERE id=?",(artifact,))
        c.execute("UPDATE ingestion_runs SET parser_name='google-aosp-source-build-table',fetched_count=?,accepted_count=? WHERE source_id=?",
                  (len(rows),len(rows),SOURCE))
    return totals


def product_source_builds(c: sqlite3.Connection, product_id: str, *, limit: int=50, offset: int=0) -> tuple[list[dict],int]:
    latest='''WITH current_builds AS(SELECT *,row_number() OVER(
        PARTITION BY source_id,build_id,source_tag ORDER BY observed_at DESC,id DESC) current_rank
        FROM source_build_catalog) '''
    total=c.execute(latest+'''SELECT count(*) FROM source_build_product_links link
        JOIN current_builds sb ON sb.id=link.build_id WHERE link.product_id=? AND sb.current_rank=1''',(product_id,)).fetchone()[0]
    rows=c.execute(latest+'''SELECT sb.*,link.confidence,link.match_method,e.locator,a.sha256 artifact_sha256
        FROM source_build_product_links link JOIN current_builds sb ON sb.id=link.build_id
        JOIN evidence e ON e.id=sb.evidence_id JOIN artifacts a ON a.id=e.artifact_id
        WHERE link.product_id=? AND sb.current_rank=1 ORDER BY sb.security_patch_level IS NULL,sb.security_patch_level DESC,sb.source_tag,sb.id
        LIMIT ? OFFSET ?''',(product_id,limit,offset)).fetchall()
    return [{**{k:v for k,v in dict(r).items() if k!='supported_products_json'},
             'supported_products':json.loads(r['supported_products_json']),
             'scope':'AOSP source build; not an OTA deployment, OEM support status or device security assessment'} for r in rows],total


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--capture',type=Path,required=True)
    args=parser.parse_args()
    from .database import Database
    db=Database.migrated(args.data_dir/'corpus.sqlite')
    decisions=[];local=args.data_dir/'local.sqlite'
    if local.exists():
        with sqlite3.connect(local) as c:
            c.row_factory=sqlite3.Row
            if c.execute("SELECT 1 FROM sqlite_schema WHERE name='identity_decisions'").fetchone():
                decisions=[dict(r) for r in c.execute('SELECT * FROM identity_decisions')]
    try:print(json.dumps(import_google_builds(db.connection,args.capture,decisions),indent=2,sort_keys=True))
    finally:db.close()


if __name__=='__main__':main()
