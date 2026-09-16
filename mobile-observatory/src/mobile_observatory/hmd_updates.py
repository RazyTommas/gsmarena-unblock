"""HMD vendor update statements: product scope, never invented hardware/rollouts."""
from __future__ import annotations
import argparse
import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .enrichment import _capture_evidence, _id
from .product_specs import name_key

SOURCE='hmd.vendor.security_updates'
URL='https://www.hmd.com/en_int/security-updates'
MONTHS={name:i for i,name in enumerate(('January','February','March','April','May','June','July','August','September','October','November','December'),1)}


def parse_update(row: dict[str,str]) -> dict:
    name=row['phone'].strip()
    if not name.startswith(('Nokia ','HMD ')):
        raise ValueError('Unsupported or missing explicit HMD/Nokia product brand')
    patch=date.fromisoformat(row['androidBulletinDate'].strip()).isoformat()
    match=re.fullmatch(r'([A-Za-z]+) (\d{1,2}), (\d{4})',row['dateOfFirstLiveRelease'].strip())
    if not match or match[1] not in MONTHS:
        raise ValueError('Unrecognized explicit vendor first-live-release date')
    released=date(int(match[3]),MONTHS[match[1]],int(match[2])).isoformat()
    if not row['screenId'].strip():
        raise ValueError('Missing vendor build variant')
    # Only numeric Android versions explicitly written by the vendor. Marketing
    # codenames (Nougat/Pie/Oreo) alone are deliberately not translated to numbers.
    versions=set(re.findall(r'\bAndroid\s+(\d{1,2}(?:\.\d{1,2})?)\b',row['comments'],re.I))
    android=next(iter(versions)) if len(versions)==1 else None
    return dict(device=name,manufacturer=name.split(' ',1)[0],build=row['screenId'].strip(),
        region_code='SOURCE_UNSPECIFIED',branch='unknown',android=android,release_date=released,
        release_scope='First approved markets only; exact market/variant rollout unspecified',
        security_patch_level=patch,aspl_month=patch[:7],comments=row['comments'].strip(),
        source_url=URL,identity_state='vendor_product_statement',cadence=row['cadence'].strip(),
        end_of_life_label=row['endOfLife'].strip(),cadence_comments=row['cadenceComments'].strip(),
        source_record=dict(row))


def import_hmd_updates(c: sqlite3.Connection, csv_path: Path, *, observed_at: str,
                       decisions: list[dict] | None=None) -> dict[str,int]:
    # Parse the whole capture before database mutation. Source dates are separate
    # from observation date; no source timestamp is synthesized from filesystem.
    with csv_path.open(encoding='utf-8-sig') as handle:
        rows=[(line,parse_update(row)) for line,row in enumerate(csv.DictReader(handle),2)]
    digest=hashlib.sha256(csv_path.read_bytes()).hexdigest()
    dbfile=c.execute('PRAGMA database_list').fetchone()[2]
    if dbfile:
        destination=Path(dbfile).parent/'evidence'/(digest+'.csv')
        destination.parent.mkdir(exist_ok=True)
        if not destination.exists():destination.write_bytes(csv_path.read_bytes())
        csv_path=destination
    totals={'captured_rows':len(rows),'products_created':0,'firmware_added':0,'security_publications_added':0,'blocked_rows':0}
    if not c.in_transaction:c.execute('BEGIN IMMEDIATE')
    with c:
        first=_capture_evidence(c,source_id=SOURCE,source_name='HMD official security and maintenance updates',
            source_url=URL,path=csv_path,locator='csv:header',excerpt='Captured vendor update statements',now=observed_at)
        artifact=c.execute('SELECT artifact_id FROM evidence WHERE id=?',(first,)).fetchone()[0]
        run=c.execute('SELECT run_id FROM artifacts WHERE id=?',(artifact,)).fetchone()[0]
        for line,data in rows:
            name,maker=data['device'],data['manufacturer']
            payload=json.dumps({'data':data,'identity_hints':{'manufacturer':maker,'source_device_name':name},
                'evidence':{'artifact_pointer':f'CSV line {line}','authority':'vendor-official','source_url':URL}},sort_keys=True)
            content_hash=hashlib.sha256(payload.encode()).hexdigest()
            key=':'.join((name,data['build'],data['security_patch_level'],data['release_date']))
            oid=_id('hmd-update',key,content_hash)
            c.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (oid,SOURCE,run,artifact,'vendor_update_publication',key,observed_at,payload,content_hash,'valid',None))
            eid=_id('hmd-update-evidence',oid)
            c.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?)',
                (eid,artifact,oid,f'CSV line {line}',json.dumps(data['source_record'],sort_keys=True),observed_at))
            candidates=[p for p in c.execute('SELECT * FROM source_products WHERE manufacturer=?',(maker,))
                        if name_key(p['canonical_name'],maker)==name_key(name,maker)]
            pid=candidates[0]['id'] if len(candidates)==1 else _id('hmd-product',maker,name)
            blocked=len(candidates)>1 or any(p['review_state']!='approved' for p in candidates)
            blocked=blocked or any(d.get('decision') in ('different','defer') and
                (d.get('canonical_id')==pid or d.get('source_namespace')==SOURCE and d.get('source_value')==name)
                for d in (decisions or []))
            blocked=blocked or bool(c.execute("SELECT 1 FROM source_identity_registry WHERE product_id=? AND resolution_state!='approved'",(pid,)).fetchone())
            if blocked:
                totals['blocked_rows']+=1
                continue
            if not candidates:
                before=c.total_changes
                c.execute('INSERT OR IGNORE INTO source_products VALUES(?,?,?,?,?,?,?,?)',
                    (pid,maker,name,name_key(name,maker),'approved',None,observed_at,observed_at))
                totals['products_created']+=c.total_changes>before
            iid=_id('hmd-product-identity',name)
            c.execute('INSERT OR IGNORE INTO source_identity_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (iid,SOURCE,'product_name',name,name.casefold(),pid,'approved','exact_vendor_product_name','1','high',observed_at,observed_at))
            c.execute('INSERT OR IGNORE INTO observation_product_links VALUES(?,?,?,?,?)',(oid,pid,iid,'approved',observed_at))
            # A vendor screenId is a build variant, not a hardware model. Do not
            # interpret its country-looking prefixes as market/codename identity.
            before=c.total_changes
            c.execute('INSERT OR IGNORE INTO product_firmware_releases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (_id('hmd-firmware',key),pid,iid,oid,SOURCE,'SOURCE_UNSPECIFIED',data['build'],'unknown',
                 data['android'],int(data['android'].split('.')[0]) if data['android'] else None,
                 data['release_date'],None,observed_at))
            totals['firmware_added']+=c.total_changes>before
            before=c.total_changes
            c.execute('INSERT OR IGNORE INTO product_security_publications VALUES(?,?,?,?,?,?,?,?,?)',
                (_id('hmd-security',key),pid,iid,oid,SOURCE,data['aspl_month'],None,
                 data['comments']+' · '+data['build'],observed_at))
            totals['security_publications_added']+=c.total_changes>before
        c.execute("UPDATE ingestion_runs SET parser_name='hmd-captured-vendor-updates',fetched_count=?,accepted_count=? WHERE id=?",
                  (len(rows),len(rows),run))
    return totals


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--capture',type=Path,required=True)
    parser.add_argument('--observed-at',required=True)
    args=parser.parse_args()
    from .database import Database
    db=Database.migrated(args.data_dir/'corpus.sqlite')
    decisions=[]
    local=args.data_dir/'local.sqlite'
    if local.exists():
        with sqlite3.connect(local) as c:
            c.row_factory=sqlite3.Row
            if c.execute("SELECT 1 FROM sqlite_schema WHERE name='identity_decisions'").fetchone():
                decisions=[dict(r) for r in c.execute('SELECT * FROM identity_decisions')]
    try:print(json.dumps(import_hmd_updates(db.connection,args.capture,observed_at=args.observed_at,decisions=decisions),indent=2,sort_keys=True))
    finally:db.close()


if __name__=='__main__':main()
