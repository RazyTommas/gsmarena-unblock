"""Conservative missing-data enrichment with source captures and per-field provenance."""
from __future__ import annotations
import calendar
import html
import json
import re
import sqlite3
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from core import connect, encoded, now, stable
from updater import fetch, stage

SPEC_FIELDS = {'chipset':'chipset', 'cpu':'cpu', 'gpu':'gpu', 'internalmemory':'memory',
 'displaysize':'display', 'batdescription1':'battery', 'wlan':'wifi', 'bluetooth':'bluetooth', 'nfc':'nfc',
 'net2g':'bands_2g','net3g':'bands_3g','net4g':'bands_4g','net5g':'bands_5g','os':'os_at_capture'}
SPEC_KEYS = {v:k for k,v in SPEC_FIELDS.items()}


def clean(text):
    return ' '.join(html.unescape(text).split())


class SpecsParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.fields={}; self.title=''; self.active=[]; self.title_depth=0

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='h1': self.title_depth+=1
        if tag not in ('br','img','meta','link','input','hr','source','wbr'):
            for item in self.active: item['depth']+=1
        if attrs.get('data-spec'):
            self.active.append({'key':attrs['data-spec'],'depth':1,'text':[]})
        if tag=='br':
            for item in self.active:item['text'].append(' ')

    def handle_endtag(self, tag):
        if tag=='h1': self.title_depth=0
        if tag in ('br','img','meta','link','input','hr','source','wbr'): return
        for item in self.active: item['depth']-=1
        finished=[x for x in self.active if x['depth']==0]
        for item in finished:
            self.fields[item['key']]=clean(''.join(item['text']))
            self.active.remove(item)

    def handle_data(self, text):
        for item in self.active: item['text'].append(text)
        if self.title_depth:self.title+=text


def release_date(text):
    # Only explicit Released clauses; never infer a release from announcement/year alone.
    match=re.search(r'\bReleased\s+(\d{4})(?:[,\s]+([A-Za-z]+)(?:\s+(\d{1,2}))?)?',text,re.I)
    if not match:return ''
    year,month,day=match.groups()
    if not month:return year
    names={name.lower():i for i,name in enumerate(calendar.month_name) if name}
    if month.lower() not in names:return ''
    value=f'{year}-{names[month.lower()]:02}'
    if day:
        from datetime import date
        value+=f'-{int(day):02}'
        date.fromisoformat(value)
    return value


def norm(text):
    return re.sub(r'[^a-z0-9]', '',text.casefold())


def fact(c, table, record_id, field, value, url, quote, capture, method, candidate_id=None):
    c.execute('''INSERT OR IGNORE INTO research_facts
        (id,entity_table,entity_id,field,value,source_url,quote,capture_id,method,recorded_at,candidate_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (stable(table,record_id,field,value,url),table,record_id,field,value,url,quote,capture,method,now(),candidate_id))


def seed_targets(data, original):
    c=connect(data)
    old=sqlite3.connect(original.resolve().as_uri()+'?mode=ro',uri=True)
    old.row_factory=sqlite3.Row
    seeded=0
    with c:
        for row in old.execute("SELECT product_id,name,source_url FROM source_specifications WHERE source_url LIKE 'https://www.gsmarena.com/%'"):
            if c.execute('SELECT 1 FROM devices WHERE id=?',(row['product_id'],)).fetchone():
                seeded+=c.execute('''INSERT INTO research_targets(device_id,url,expected_name) VALUES (?,?,?)
                    ON CONFLICT(device_id) DO UPDATE SET url=excluded.url,expected_name=excluded.expected_name,
                    status=CASE WHEN research_targets.url<>excluded.url THEN 'pending' ELSE research_targets.status END,
                    error=CASE WHEN research_targets.url<>excluded.url THEN '' ELSE research_targets.error END''',
                    (row['product_id'],row['source_url'],row['name'])).rowcount
        # Restore technical columns already captured in original evidence, without guessing new facts.
        restored=0
        for row in c.execute('SELECT id,specs_json FROM devices').fetchall():
            specs=json.loads(row['specs_json'])
            for obs in specs.get('chipset_observations',[]):
                for evidence in json.loads(obs.get('evidence_json') or '[]'):
                    source=evidence.get('specification',{})
                    for key,field in SPEC_FIELDS.items():
                        if not specs.get(key) and source.get(key):
                            specs[key]=source[key];restored+=1
                            fact(c,'devices',row['id'],field,str(source[key]),evidence.get('source_url',''),str(source[key]),None,'restored_capture')
            c.execute('UPDATE devices SET specs_json=? WHERE id=?',(encoded(specs),row['id']))
    old.close();c.close()
    return {'targets_added':seeded,'captured_fields_restored':restored}


def enrich(data, limit=50):
    c=connect(data); run_id=stable('research',now(),time.monotonic_ns())
    report={'checked':0,'fields_filled':0,'errors':[],'scope':'previously mapped specification pages; missing fields only'}
    c.execute('INSERT INTO runs(id,started_at,status,summary_json) VALUES (?,?,?,?)',(run_id,now(),'running',encoded(report)));c.commit()
    targets=list(c.execute("SELECT * FROM research_targets WHERE status='pending' ORDER BY device_id LIMIT ?",(limit,)))
    state='succeeded'
    for target in targets:
        try:
            body,capture=fetch(c,data,'specification-research',target['url'])
            parser=SpecsParser();parser.feed(body.decode('utf-8',errors='replace'))
            if norm(clean(parser.title))!=norm(target['expected_name']):
                raise ValueError('Source title does not exactly match the previously mapped product; manual review required')
            p=c.execute('SELECT * FROM devices WHERE id=?',(target['device_id'],)).fetchone()
            specs=json.loads(p['specs_json'])
            with c:
                for key,field in SPEC_FIELDS.items():
                    value=parser.fields.get(key,'')
                    if value and not specs.get(key):
                        specs[key]=value
                        fact(c,'devices',p['id'],field,value,target['url'],value,capture,'exact_source_parser')
                        report['fields_filled']+=1
                c.execute('UPDATE devices SET specs_json=? WHERE id=?',(encoded(specs),p['id']))
                if not p['source_url']:
                    c.execute('UPDATE devices SET source_url=? WHERE id=?',(target['url'],p['id']))
                    fact(c,'devices',p['id'],'source_url',target['url'],target['url'],clean(parser.title),capture,'exact_source_parser')
                    report['fields_filled']+=1
                release_text=next((parser.fields.get(k,'') for k in ('released-hl','status','year') if 'Released ' in parser.fields.get(k,'')),'')
                date=release_date(release_text)
                if date and not p['released_at']:
                    c.execute('UPDATE devices SET released_at=? WHERE id=?',(date,p['id']))
                    fact(c,'devices',p['id'],'released_at',date,target['url'],release_text,capture,'exact_source_parser')
                    report['fields_filled']+=1
                c.execute("UPDATE research_targets SET status='checked',checked_at=?,error='' WHERE device_id=?",(now(),p['id']))
        except Exception as e:
            c.rollback();state='partial'
            report['errors'].append({'device_id':target['device_id'],'url':target['url'],'error':str(e)[:250]})
            with c:c.execute("UPDATE research_targets SET status='needs_review',checked_at=?,error=? WHERE device_id=?",(now(),str(e)[:250],target['device_id']))
            if isinstance(e,HTTPError) and e.code in (403,429):
                report['stopped_reason']='Source access restricted; remaining targets left pending.'
                break
        report['checked']+=1
        c.execute('UPDATE runs SET summary_json=? WHERE id=?',(encoded(report),run_id));c.commit()
        time.sleep(.3)
    report['remaining_targets']=c.execute("SELECT count(*) FROM research_targets WHERE status='pending'").fetchone()[0]
    c.execute('UPDATE runs SET finished_at=?,status=?,summary_json=? WHERE id=?',(now(),state,encoded(report),run_id));c.commit();c.close()
    return {'id':run_id,'status':state,**report}


def import_assertions(data, assertions):
    """Agent results enter the normal LLM review queue, never direct SQL writes."""
    if not isinstance(assertions,list) or not 1<=len(assertions)<=500:
        raise ValueError('Provide an array of 1–500 researched assertions')
    c=connect(data); cache={}; groups={}
    try:
        for a in assertions:
            table=a.get('table');field=a.get('field')
            fields={'devices':{'released_at','source_url',*SPEC_KEYS},'models':{'released_at','chipset','model_code','codename','source_url'},
                    'firmwares':{'released_at','version','region','download_url','source_url','baseband','patch_level'}}
            if table not in fields or field not in fields[table]:raise ValueError('Unsupported research target')
            if not isinstance(a.get('value'),str) or not a['value'].strip() or len(a['value'])>2000:raise ValueError('A bounded nonempty value is required')
            if not isinstance(a.get('quote'),str) or not 5<=len(a['quote'])<=1000:raise ValueError('A short exact source excerpt is required')
            url=a.get('source_url','')
            if url not in cache:cache[url]=fetch(c,data,'agent-research',url)
            body,capture=cache[url]
            text=clean(re.sub('<[^>]+>',' ',body.decode('utf-8',errors='replace')))
            if clean(a['quote']) not in text:raise ValueError('Evidence excerpt is not present in the fetched source')
            key=(table,a.get('id'),capture)
            if key not in groups:
                existing=c.execute(f'SELECT * FROM {table} WHERE id=?',(a.get('id'),)).fetchone()
                if not existing:raise ValueError('Research record not found')
                groups[key]={'row':dict(existing),'facts':[]}
            group=groups[key];row=group['row']
            if not row.get('source_url'):
                row['source_url']=url
            if table=='devices' and field in SPEC_KEYS:
                specs=json.loads(row['specs_json']);spec_key=SPEC_KEYS[field]
                if specs.get(spec_key) and specs[spec_key]!=a['value']:raise ValueError('Research import fills gaps only; existing facts require a reviewed correction')
                specs[spec_key]=a['value'];row['specs_json']=encoded(specs)
            else:
                if row.get(field) and row[field]!=a['value']:raise ValueError('Research import fills gaps only; existing facts require a reviewed correction')
                row[field]=a['value']
            group['facts'].append(a)
        staged=0
        with c:
            for (table,record_id,capture),group in groups.items():
                row=group['row'];kind={'devices':'device','models':'model','firmwares':'firmware'}[table]
                added=stage(c,kind,row,capture);staged+=added
                candidate=c.execute('SELECT id FROM candidates WHERE kind=? AND target_id=? AND payload_json=?',(kind,record_id,encoded(row))).fetchone()
                if candidate:
                    c.execute("UPDATE candidates SET rationale=? WHERE id=? AND status='pending'",
                        (' '.join(str(a.get('reason','')) for a in group['facts']),candidate[0]))
                    for a in group['facts']:fact(c,table,record_id,a['field'],a['value'],a['source_url'],a['quote'],capture,'pending_reasoning',candidate[0])
        return {'staged':staged,'assertions':len(assertions)}
    finally:c.close()
