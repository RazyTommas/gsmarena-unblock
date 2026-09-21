"""Audited repair of captured-source interpretation; raw observations stay intact."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .collectors.adapters.xiaomi_tracker import _region_from_codename_and_name
from .enrichment import _id, promote_approved_product_observations


def _audit(c, entity_type, entity_id, reason, before, after, now, evidence_id=None):
    c.execute('INSERT OR IGNORE INTO source_data_corrections VALUES(?,?,?,?,?,?,?,?)',
        (_id('source-correction',entity_type,entity_id,reason),entity_type,entity_id,reason,
         json.dumps(before,sort_keys=True),json.dumps(after,sort_keys=True),evidence_id,now))


def correct_source_interpretations(c: sqlite3.Connection) -> dict[str,int]:
    now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    totals={'samsung_dates_cleared':0,'xiaomi_regions_corrected':0,'upgrade_events_superseded':0}
    if not c.in_transaction:
        c.execute('BEGIN IMMEDIATE')
    with c:
        # Scope to the known faulty parser and exact old assertion. Independent
        # vendor-date evidence, if present, prevents clearing a legitimate date.
        rows=c.execute('''SELECT DISTINCT fr.id,fr.vendor_released_at,e.id evidence_id,
            json_extract(o.payload_json,'$.data.release_time') old_date
            FROM firmware_releases fr JOIN firmware_release_evidence fre ON fre.firmware_release_id=fr.id
            JOIN evidence e ON e.id=fre.evidence_id JOIN observations o ON o.id=e.observation_id
            JOIN ingestion_runs ir ON ir.id=o.run_id
            WHERE ir.parser_name='samsung_fota_history_csv' AND ir.parser_version='1.0.0'
              AND fr.vendor_released_at=json_extract(o.payload_json,'$.data.release_time')
              AND NOT EXISTS(SELECT 1 FROM firmware_release_evidence independent
                  WHERE independent.firmware_release_id=fr.id AND independent.role='release_date'
                    AND independent.evidence_id!=e.id)''').fetchall()
        for row in rows:
            if c.execute('SELECT vendor_released_at FROM firmware_releases WHERE id=?',(row['id'],)).fetchone()[0] is None:
                continue
            _audit(c,'firmware_release',row['id'],'samsung_build_month_not_release_date',
                {'vendor_released_at':row['vendor_released_at']},
                {'vendor_released_at':None,'build_derived_month':row['old_date'][:7]},now,row['evidence_id'])
            c.execute('UPDATE firmware_releases SET vendor_released_at=NULL WHERE id=?',(row['id'],))
            totals['samsung_dates_cleared']+=1
        rows=c.execute('''SELECT pfr.id,pfr.product_id,pfr.region_code,pfr.build_id,o.payload_json
            FROM product_firmware_releases pfr JOIN observations o ON o.id=pfr.observation_id
            WHERE pfr.source_id='xiaomi.community.firmware_tracker' ''').fetchall()
        changed=set()
        for row in rows:
            data=json.loads(row['payload_json'])['data']
            region=_region_from_codename_and_name(data['model_code'],data['source_device_name'])
            if region==row['region_code']:
                continue
            _audit(c,'product_firmware_release',row['id'],'xiaomi_explicit_regional_suffix',
                {'region_code':row['region_code']}, {'region_code':region,'source_codename':data['model_code'],
                'source_name':data['source_device_name']},now)
            c.execute('UPDATE product_firmware_releases SET region_code=? WHERE id=?',(region,row['id']))
            changed.add(row['product_id'])
            totals['xiaomi_regions_corrected']+=1
        # Preserve invalid historical events and append an explicit withdrawal.
        # Only old/new builds involving a changed market are superseded. A new
        # projection below emits correctly scoped upgrades when evidence supports it.
        for pid in changed:
            events=c.execute("SELECT * FROM domain_events WHERE subject_type='source_product' AND subject_id=? AND event_type='android_version_changed'",(pid,)).fetchall()
            for event in events:
                before=json.loads(event['before_json'] or '{}'); after=json.loads(event['after_json'] or '{}')
                valid=True
                for endpoint in (before,after):
                    matches=c.execute('''SELECT DISTINCT region_code FROM product_firmware_releases
                        WHERE product_id=? AND build_id=?''',(pid,endpoint.get('build'))).fetchall()
                    if not any(r[0]==endpoint.get('region') for r in matches):valid=False
                if valid:continue
                dedupe='region-correction:'+event['id']
                before_changes=c.total_changes
                c.execute('''INSERT OR IGNORE INTO domain_events VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (_id('domain-event',dedupe),'identity_corrected','source_product',pid,dedupe,now,now,
                     event['after_json'],json.dumps({'withdrawn':True,'reason':'Region parser correction; previous Android upgrade scope invalid'}),
                     event['evidence_id'],event['id']))
                totals['upgrade_events_superseded']+=c.total_changes>before_changes
    totals['corrected_upgrade_events_created']=promote_approved_product_observations(c)['android_upgrade_events']
    return totals


def firmware_date_evidence(c: sqlite3.Connection, release_id: str) -> dict:
    correction=c.execute('''SELECT before_json,after_json,evidence_id,recorded_at FROM source_data_corrections
        WHERE entity_type='firmware_release' AND entity_id=? AND reason='samsung_build_month_not_release_date' ''',(release_id,)).fetchone()
    if correction:
        return {'build_derived_month':json.loads(correction['after_json'])['build_derived_month'],
                'date_basis':'build_identifier_month_not_vendor_release','evidence_id':correction['evidence_id'],
                'corrected_at':correction['recorded_at']}
    row=c.execute('''SELECT json_extract(o.payload_json,'$.data.build_derived_month') build_month,e.id evidence_id
        FROM firmware_release_evidence fre JOIN evidence e ON e.id=fre.evidence_id
        JOIN observations o ON o.id=e.observation_id WHERE fre.firmware_release_id=?
          AND json_extract(o.payload_json,'$.data.build_derived_month') IS NOT NULL LIMIT 1''',(release_id,)).fetchone()
    return {'build_derived_month':row['build_month'],'date_basis':'build_identifier_month_not_vendor_release',
            'evidence_id':row['evidence_id']} if row else {}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    args=parser.parse_args()
    from .database import Database
    db=Database.migrated(args.data_dir/'corpus.sqlite')
    try:print(json.dumps(correct_source_interpretations(db.connection),indent=2,sort_keys=True))
    finally:db.close()


if __name__=='__main__':main()
