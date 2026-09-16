"""Durable local watch preferences; never assert vendor support or applicability."""
import sqlite3


def migrate_watches(local: sqlite3.Connection):
    local.execute('''CREATE TABLE IF NOT EXISTS watches (
      subject_type TEXT NOT NULL CHECK(subject_type IN ('hardware_model','source_product')),
      subject_id TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(subject_type,subject_id))''')
    local.commit()


def list_watches(local: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in local.execute('SELECT * FROM watches ORDER BY created_at DESC,subject_type,subject_id')]


def save_watch(local: sqlite3.Connection, corpus: sqlite3.Connection, value: dict) -> dict:
    if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
        raise ValueError('enabled must be a boolean')
    kind, identifier = value.get('subjectType'), value.get('subjectId')
    table = {'hardware_model':'hardware_models', 'source_product':'source_products'}.get(kind)
    if not table or not isinstance(identifier,str) or not identifier or len(identifier)>200:
        raise ValueError('invalid watch target')
    if value['enabled'] and corpus.execute(f'SELECT 1 FROM {table} WHERE id=?',(identifier,)).fetchone() is None:
        raise ValueError('unknown watch target')
    with local:
        if value['enabled']:
            local.execute('INSERT OR IGNORE INTO watches(subject_type,subject_id) VALUES(?,?)',(kind,identifier))
        else:
            # Removing a stale watch remains possible after a corpus replacement.
            local.execute('DELETE FROM watches WHERE subject_type=? AND subject_id=?',(kind,identifier))
    return {'subjectType':kind,'subjectId':identifier,'enabled':value['enabled']}
