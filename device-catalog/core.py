"""Small, independent catalog. Python standard library only."""
from __future__ import annotations

import csv
import contextlib
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def stable(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, encoded(parts)))


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices(
 id TEXT PRIMARY KEY, manufacturer TEXT NOT NULL, name TEXT NOT NULL,
 identity_state TEXT NOT NULL, specs_json TEXT NOT NULL DEFAULT '{}',
 released_at TEXT NOT NULL DEFAULT '',
 source_url TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS models(
 id TEXT PRIMARY KEY, manufacturer TEXT NOT NULL, name TEXT NOT NULL,
 model_code TEXT NOT NULL DEFAULT '', codename TEXT NOT NULL DEFAULT '',
 chipset TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
 released_at TEXT NOT NULL DEFAULT '',
 observed_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS device_models(
 device_id TEXT REFERENCES devices(id), model_id TEXT REFERENCES models(id),
 PRIMARY KEY(device_id,model_id));
CREATE TABLE IF NOT EXISTS identities(
 source TEXT, external_id TEXT, device_id TEXT REFERENCES devices(id),
 PRIMARY KEY(source,external_id));
CREATE TABLE IF NOT EXISTS firmwares(
 id TEXT PRIMARY KEY, device_id TEXT REFERENCES devices(id), model_id TEXT REFERENCES models(id),
 source TEXT NOT NULL, source_key TEXT NOT NULL, build TEXT NOT NULL,
 version TEXT NOT NULL DEFAULT '', os TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '',
 channel TEXT NOT NULL DEFAULT '', delivery TEXT NOT NULL DEFAULT '',
 released_at TEXT NOT NULL DEFAULT '', patch_level TEXT NOT NULL DEFAULT '',
 baseband TEXT NOT NULL DEFAULT '', download_url TEXT NOT NULL DEFAULT '',
 source_url TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL DEFAULT '',
 record_layer TEXT NOT NULL, UNIQUE(source,source_key),
 CHECK(device_id IS NOT NULL OR model_id IS NOT NULL));
CREATE INDEX IF NOT EXISTS fw_device ON firmwares(device_id);
CREATE INDEX IF NOT EXISTS fw_model ON firmwares(model_id);
CREATE INDEX IF NOT EXISTS fw_release ON firmwares(released_at);
CREATE INDEX IF NOT EXISTS dm_model ON device_models(model_id);
CREATE VIEW IF NOT EXISTS device_details AS
 SELECT d.*,coalesce((SELECT group_concat(chipset,' | ') FROM (
   SELECT trim(CAST(json_extract(d.specs_json,'$.chipset') AS TEXT)) AS chipset
   UNION SELECT trim(CAST(json_extract(d.specs_json,'$."Platform — Chipset"') AS TEXT))
   UNION SELECT trim(json_extract(value,'$.raw_chipset')) FROM json_each(d.specs_json,'$.chipset_observations')
   UNION SELECT trim(m.chipset) FROM device_models dm JOIN models m ON m.id=dm.model_id WHERE dm.device_id=d.id
 ) WHERE chipset IS NOT NULL AND chipset<>''),'') AS chipset FROM devices d;
CREATE TABLE IF NOT EXISTS captures(
 id TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL, fetched_at TEXT NOT NULL,
 sha256 TEXT NOT NULL, path TEXT NOT NULL, byte_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS candidates(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_id TEXT NOT NULL,
 payload_json TEXT NOT NULL, before_json TEXT NOT NULL, capture_id TEXT REFERENCES captures(id),
 status TEXT NOT NULL DEFAULT 'pending', rationale TEXT NOT NULL DEFAULT '',
 command_json TEXT NOT NULL DEFAULT '[]', reviewed_at TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS candidate_status ON candidates(status);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS research_facts(
 id TEXT PRIMARY KEY,entity_table TEXT NOT NULL,entity_id TEXT NOT NULL,field TEXT NOT NULL,
 value TEXT NOT NULL,source_url TEXT NOT NULL,quote TEXT NOT NULL,capture_id TEXT,
 method TEXT NOT NULL,recorded_at TEXT NOT NULL,candidate_id TEXT);
CREATE INDEX IF NOT EXISTS research_entity ON research_facts(entity_table,entity_id);
CREATE TABLE IF NOT EXISTS research_targets(
 device_id TEXT PRIMARY KEY REFERENCES devices(id),url TEXT NOT NULL,expected_name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',checked_at TEXT,error TEXT NOT NULL DEFAULT '');
"""


def connect(data: Path):
    data.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(data / "catalog.sqlite", timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    for table in ('devices', 'models'):
        if 'released_at' not in {r[1] for r in c.execute(f'PRAGMA table_info({table})')}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN released_at TEXT NOT NULL DEFAULT ''")
    return c


def put(c, table, row):
    keys = list(row)
    # Table and keys are internal constants, never interpolated from an HTTP request.
    c.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)}) "
              f"ON CONFLICT(id) DO UPDATE SET " + ','.join(f'{k}=excluded.{k}' for k in keys if k != 'id'),
              [row[k] for k in keys])


@contextlib.contextmanager
def session(data):
    c = connect(data)
    try:
        with c:
            yield c
    finally:
        c.close()


def summary(c):
    result = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ('devices', 'models', 'firmwares')}
    result['last_observation'] = c.execute('SELECT max(observed_at) FROM firmwares').fetchone()[0]
    result['candidate_states'] = dict(c.execute('SELECT status,count(*) FROM candidates GROUP BY status'))
    result['manufacturers'] = [dict(r) for r in c.execute(
        'SELECT manufacturer,count(*) AS count FROM devices GROUP BY manufacturer ORDER BY count DESC')]
    result['sources'] = [dict(r) for r in c.execute(
        'SELECT source,count(*) AS count,max(observed_at) AS last_observation FROM firmwares GROUP BY source')]
    result['runs'] = [dict(r) for r in c.execute('SELECT * FROM runs ORDER BY started_at DESC LIMIT 10')]
    result['metadata'] = dict(c.execute('SELECT key,value FROM metadata'))
    return result


DEVICE_EXPORT = """SELECT d.id AS device_id,d.manufacturer,d.name,d.identity_state,
 (SELECT group_concat(m.model_code,' | ') FROM device_models dm JOIN models m ON m.id=dm.model_id WHERE dm.device_id=d.id) AS model_codes,
 (SELECT group_concat(m.id,' | ') FROM device_models dm JOIN models m ON m.id=dm.model_id WHERE dm.device_id=d.id) AS model_ids,
 d.specs_json,d.source_url,d.observed_at,d.chipset,d.released_at FROM device_details d ORDER BY d.manufacturer,d.name,d.id"""
FIRMWARE_EXPORT = """SELECT f.id AS firmware_id,f.device_id,f.model_id,
 coalesce(d.manufacturer,m.manufacturer) AS manufacturer,coalesce(d.name,m.name) AS device_name,
 m.model_code,CASE WHEN f.model_id IS NOT NULL THEN m.released_at ELSE d.released_at END AS model_released_at,
 f.build,f.version,f.os,f.region,f.channel,f.delivery,f.released_at,
 f.patch_level,f.baseband,f.download_url,f.source,f.source_url,f.observed_at,f.record_layer
 FROM firmwares f LEFT JOIN devices d ON d.id=f.device_id LEFT JOIN models m ON m.id=f.model_id
 ORDER BY manufacturer,device_name,f.released_at DESC,f.id"""
SPEC_COLUMNS = {'chipset': ['chipset', 'Platform — Chipset'], 'cpu': ['cpu', 'Platform — CPU'],
 'gpu': ['gpu', 'Platform — GPU'], 'memory': ['internalmemory', 'Memory — Internal'],
 'display': ['displaysize', 'Display — Size'], 'battery': ['batdescription1', 'Battery — Type'],
 'os_at_capture': ['os', 'Platform — OS'], 'wifi': ['wlan'], 'bluetooth': ['bluetooth'],
 'nfc': ['nfc'], 'bands_2g': ['net2g'], 'bands_3g': ['net3g'], 'bands_4g': ['net4g'], 'bands_5g': ['net5g']}


def safe_cell(value):
    value = '' if value is None else str(value)
    # Prevent formulas from source-controlled strings executing in Excel.
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value


def export_csv(c, directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name, query in [('devices', DEVICE_EXPORT), ('models', 'SELECT * FROM models ORDER BY manufacturer,name,id'),
                        ('device_models', 'SELECT * FROM device_models ORDER BY device_id,model_id'),
                        ('firmwares', FIRMWARE_EXPORT)]:
        cursor = c.execute(query)
        fields = [d[0] for d in cursor.description]
        if name == 'devices':
            fields += [k for k in SPEC_COLUMNS if k not in fields]
        path = directory / (name + '.csv')
        temp = path.with_suffix('.csv.tmp')
        with temp.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in cursor:
                row = dict(row)
                if name == 'devices':
                    specs = json.loads(row['specs_json'])
                    row.update({k: next((specs[a] for a in aliases if specs.get(a)), '')
                                for k, aliases in SPEC_COLUMNS.items() if k != 'chipset'})
                writer.writerow({k: safe_cell(v) for k, v in row.items()})
        temp.replace(path)


def sha256(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


@contextlib.contextmanager
def catalog_lock(data):
    """One server/writer per data directory; the OS releases locks after crashes."""
    data.mkdir(parents=True, exist_ok=True)
    with (data / '.writer.lock').open('a+b') as f:
        if os.name == 'nt':
            import msvcrt
            f.write(b'0'); f.flush(); f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError('Catalog is already open in another server or writer') from None
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Catalog is already open in another server or writer') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)
