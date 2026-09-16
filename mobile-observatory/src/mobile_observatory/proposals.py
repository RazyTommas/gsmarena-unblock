"""Local, inspectable agent proposals and append-only human review history.

Neither import nor approval writes to the replaceable canonical corpus.
"""
import hashlib
import json
import sqlite3
from urllib.parse import urlsplit


def migrate_proposals(local: sqlite3.Connection):
    local.executescript('''
      CREATE TABLE IF NOT EXISTS agent_proposals (
        id TEXT PRIMARY KEY, target_key TEXT NOT NULL, product_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        reviewed_at TEXT, reviewer TEXT, rationale TEXT);
      CREATE INDEX IF NOT EXISTS agent_proposals_target_idx ON agent_proposals(target_key,reviewed_at);
      CREATE TABLE IF NOT EXISTS agent_proposal_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL,
        decision TEXT NOT NULL, rationale TEXT NOT NULL, reviewer TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
      CREATE TABLE IF NOT EXISTS identity_decision_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_namespace TEXT NOT NULL,
        source_value TEXT NOT NULL, canonical_type TEXT NOT NULL, canonical_id TEXT,
        decision TEXT NOT NULL, decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        rationale TEXT, author TEXT NOT NULL DEFAULT 'local_user');
    ''')
    if not local.execute('SELECT 1 FROM identity_decision_history LIMIT 1').fetchone():
        local.execute('''INSERT INTO identity_decision_history
          (source_namespace,source_value,canonical_type,canonical_id,decision,decided_at,rationale,author)
          SELECT source_namespace,source_value,canonical_type,canonical_id,decision,decided_at,rationale,'legacy_local_memory'
          FROM identity_decisions''')
    local.executescript("""
      CREATE TRIGGER IF NOT EXISTS identity_history_no_update BEFORE UPDATE ON identity_decision_history
      BEGIN SELECT RAISE(ABORT,'identity decision history is immutable'); END;
      CREATE TRIGGER IF NOT EXISTS identity_history_no_delete BEFORE DELETE ON identity_decision_history
      BEGIN SELECT RAISE(ABORT,'identity decision history is immutable'); END;
      CREATE TRIGGER IF NOT EXISTS proposal_reviews_no_update BEFORE UPDATE ON agent_proposal_reviews
      BEGIN SELECT RAISE(ABORT,'proposal review history is immutable'); END;
      CREATE TRIGGER IF NOT EXISTS proposal_reviews_no_delete BEFORE DELETE ON agent_proposal_reviews
      BEGIN SELECT RAISE(ABORT,'proposal review history is immutable'); END;
    """)
    local.commit()


def _text(value, name, maximum=1000):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum:
        raise ValueError(f'{name} must be nonempty text, at most {maximum} characters')
    return value.strip()


def validate_proposal(corpus, value):
    required={'product_id','decision','canonical_name','model_codes','aliases','confidence','evidence','rationale'}
    if not isinstance(value,dict) or set(value)!=required:
        raise ValueError('Each proposal must contain exactly the documented identity proposal fields')
    product=_text(value['product_id'],'product_id',200)
    if not corpus.execute('SELECT 1 FROM source_products WHERE id=?',(product,)).fetchone():
        raise ValueError('Unknown product_id')
    if value['decision'] not in ('same','different','defer') or value['confidence'] not in ('low','medium','high'):
        raise ValueError('Invalid decision or confidence; agent confidence is never authoritative')
    cleaned={**value,'product_id':product,'canonical_name':_text(value['canonical_name'],'canonical_name',200),
             'rationale':_text(value['rationale'],'rationale',8000)}
    for key in ('model_codes','aliases'):
        if not isinstance(value[key],list) or len(value[key])>30:
            raise ValueError(f'{key} must be an array of at most 30 strings')
        cleaned[key]=sorted(set(_text(v,key,200) for v in value[key]))
    if not isinstance(value['evidence'],list) or not value['evidence'] or len(value['evidence'])>30:
        raise ValueError('At least one evidence reference is required (maximum 30)')
    evidence=[]
    for item in value['evidence']:
        if not isinstance(item,dict) or not set(item)<= {'url','artifact_id','observation_id','note'}:
            raise ValueError('Evidence accepts url, artifact_id, observation_id, and note only')
        entry={'note':_text(item.get('note'),'evidence note',4000)}
        if not any(item.get(key) for key in ('url','artifact_id','observation_id')):
            raise ValueError('Evidence needs an HTTP(S) URL or an existing captured reference')
        if item.get('url'):
            url=_text(item['url'],'url',2000);parsed=urlsplit(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Evidence URL must be HTTP(S), without embedded credentials')
            entry['url']=url
        for key,table in (('artifact_id','artifacts'),('observation_id','observations')):
            if item.get(key):
                identifier=_text(item[key],key,200)
                if not corpus.execute(f'SELECT 1 FROM {table} WHERE id=?',(identifier,)).fetchone():
                    raise ValueError(f'Unknown {key}')
                entry[key]=identifier
        evidence.append(entry)
    cleaned['evidence']=evidence
    return cleaned


def import_proposals(local,corpus,values):
    if not isinstance(values,list) or not 1<=len(values)<=100:
        raise ValueError('Import a JSON array containing 1–100 proposals')
    # Validate the whole batch before writing any proposal.
    validated=[validate_proposal(corpus,v) for v in values]
    imported=[]
    with local:
        for value in validated:
            payload=json.dumps(value,sort_keys=True,separators=(',',':'))
            identifier=hashlib.sha256(payload.encode()).hexdigest()
            target=hashlib.sha256(json.dumps([value['product_id'],' '.join(value['canonical_name'].casefold().split()),sorted(code.upper() for code in value['model_codes'])],sort_keys=True).encode()).hexdigest()
            previous=local.execute('SELECT status,reviewed_at,reviewer,rationale FROM agent_proposals WHERE target_key=? AND status!=\'pending\' ORDER BY reviewed_at DESC,rowid DESC LIMIT 1',(target,)).fetchone()
            local.execute('''INSERT OR IGNORE INTO agent_proposals
              (id,target_key,product_id,payload_json,status,reviewed_at,reviewer,rationale) VALUES(?,?,?,?,?,?,?,?)''',
              (identifier,target,value['product_id'],payload,previous['status'] if previous else 'pending',
               previous['reviewed_at'] if previous else None,previous['reviewer'] if previous else None,previous['rationale'] if previous else None))
            imported.append(identifier)
    return {'ids':imported,'count':len(imported),'canonicalChanges':0}


def list_proposals(local):
    items=[]
    for row in local.execute("SELECT * FROM agent_proposals ORDER BY status!='pending',created_at DESC,rowid DESC LIMIT 500"):
        item=dict(row);item['proposal']=json.loads(item.pop('payload_json'))
        item['reviews']=[dict(r) for r in local.execute('SELECT r.* FROM agent_proposal_reviews r JOIN agent_proposals p ON p.id=r.proposal_id WHERE p.target_key=? ORDER BY r.id',(row['target_key'],))]
        items.append(item)
    return items


def review_proposal(local,identifier,value):
    if not isinstance(value,dict) or value.get('decision') not in ('same','different','defer'):
        raise ValueError('Review decision must be same, different, or defer')
    rationale=_text(value.get('rationale'),'review rationale',4000)
    reviewer=_text(value.get('reviewer','local_user'),'reviewer',200)
    state={'same':'approved','different':'rejected','defer':'deferred'}[value['decision']]
    with local:
        row=local.execute('SELECT target_key FROM agent_proposals WHERE id=?',(identifier,)).fetchone()
        if row is None:raise KeyError(identifier)
        local.execute('INSERT INTO agent_proposal_reviews(proposal_id,decision,rationale,reviewer) VALUES(?,?,?,?)',(identifier,value['decision'],rationale,reviewer))
        local.execute('UPDATE agent_proposals SET status=?,reviewed_at=CURRENT_TIMESTAMP,reviewer=?,rationale=? WHERE target_key=?',(state,reviewer,rationale,row['target_key']))
    return {'id':identifier,'status':state,'canonicalChanges':0}


def save_decision(local,value):
    if not isinstance(value,dict):raise ValueError('Invalid identity decision')
    fields=[_text(value.get(k),k,500) for k in ('sourceNamespace','sourceValue','canonicalType')]
    identifier=value.get('canonicalId')
    if identifier is not None:identifier=_text(identifier,'canonicalId',200)
    if value.get('decision') not in ('same','different','defer'):raise ValueError('Invalid decision')
    rationale=value.get('rationale')
    if rationale is not None and (not isinstance(rationale,str) or len(rationale)>8000):raise ValueError('Invalid rationale')
    with local:
        # IS handles NULL targets, unlike a nullable composite primary key.
        local.execute('DELETE FROM identity_decisions WHERE source_namespace=? AND source_value=? AND canonical_type=? AND canonical_id IS ?',(*fields,identifier))
        local.execute('''INSERT INTO identity_decisions(source_namespace,source_value,canonical_type,canonical_id,decision,rationale)
                         VALUES(?,?,?,?,?,?)''',(*fields,identifier,value['decision'],rationale))
        local.execute('''INSERT INTO identity_decision_history(source_namespace,source_value,canonical_type,canonical_id,decision,rationale)
                         VALUES(?,?,?,?,?,?)''',(*fields,identifier,value['decision'],rationale))
    return {'ok':True,'decision':value['decision']}
