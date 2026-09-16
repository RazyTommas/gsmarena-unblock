"""Validate captured enrichment on an isolated SQLite backup of a real snapshot."""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mobile_observatory.database import Database
from mobile_observatory.product_specs import enrich_product_specs
from mobile_observatory.server import ObservatoryService


def validate(source: Path, output: Path, legacy: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'corpus.sqlite').exists():
        raise ValueError('Output must be a fresh directory; existing snapshots are preserved')
    for name in ('corpus.sqlite', 'local.sqlite'):
        if (source/name).exists():
            with sqlite3.connect(source/name) as original, sqlite3.connect(output/name) as copy:
                original.backup(copy)
    db = Database.migrated(output/'corpus.sqlite')
    service = ObservatoryService(db, output/'local.sqlite', demonstration=False)
    protected = ('hardware_models','hardware_silicon','firmware_releases','product_firmware_releases',
                 'product_security_publications','identity_conclusions','domain_events','applicability_claims','fix_claims')
    def state():
        return {table: [tuple(r) for r in db.connection.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in protected}
    before = state()
    initial = db.connection.execute('SELECT count(*) FROM observed_product_silicon').fetchone()[0]
    kwargs = dict(specs_csv=legacy/'T004-gsmarena-slugs/gsm_specs.csv',
                  devices_yml=legacy/'xiaomi-tracker/devices.yml',
                  google_play_csv=legacy/'google-play-devices/supported_devices.csv',
                  decisions=service.identity_decisions())
    result = enrich_product_specs(db.connection, **kwargs)
    assert state() == before, 'Enrichment changed protected canonical/history/conclusion records'
    rows = [tuple(r) for r in db.connection.execute('SELECT * FROM observed_product_silicon ORDER BY product_id')]
    enrich_product_specs(db.connection, **kwargs)
    assert rows == [tuple(r) for r in db.connection.execute('SELECT * FROM observed_product_silicon ORDER BY product_id')], 'Enrichment must be idempotent'
    assert db.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not db.connection.execute('PRAGMA foreign_key_check').fetchall()
    start = time.perf_counter()
    chips = service.chips_page({'limit':['100']})
    chip_ms = round((time.perf_counter()-start)*1000)
    checked = 0
    for product_id, in db.connection.execute('SELECT id FROM source_products'):
        detail = service.product_detail(product_id)
        for key, endpoint in (('firmware',service.product_releases_page),('security',service.product_security_page)):
            page = detail[key]; seen={r['id'] for r in page['items']}; cursor=page['meta']['page']['nextCursor']
            while cursor:
                extra=endpoint({'product':[product_id],'cursor':[cursor],'limit':['50']})
                new={r['id'] for r in extra.items}
                assert not seen & new
                seen |= new; cursor=extra.metadata()['nextCursor']
            assert len(seen)==page['meta']['page']['total']
        checked += 1
    report = {'before_silicon_observations':initial, 'enrichment':result,
              'protected_counts':{k:len(v) for k,v in before.items()},
              'product_details_checked':checked, 'chip_rows':chips.total, 'chip_query_ms':chip_ms,
              'integrity':'ok', 'idempotent':True, 'protected_records_unchanged':True,
              'snapshot':str(output)}
    (output/'product-batch-validation.json').write_text(json.dumps(report,indent=2)+'\n')
    service.local.close(); db.close()
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--legacy',type=Path,default=ROOT.parent/'crawler/relay/results')
    args=parser.parse_args()
    print(json.dumps(validate(args.source,args.output,args.legacy),indent=2))
