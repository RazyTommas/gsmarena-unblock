"""Evidence-preserving security catalog queries; no applicability adjudication."""
from __future__ import annotations

import json
import sqlite3

# Aggregate each relation independently: joining claims to fixes multiplies rows
# and can misleadingly imply that every fix belongs to every affected part.
INDEX = """WITH bulletin AS (
 SELECT av.vulnerability_id, min(a.title) bulletin,max(a.published_at) bulletin_date,
 group_concat(DISTINCT s.name) evidence,
 group_concat(DISTINCT coalesce(aa.source_url,s.base_url)) source_urls
 FROM advisory_vulnerabilities av JOIN advisories a ON a.id=av.advisory_id
 JOIN sources s ON s.id=a.source_id LEFT JOIN evidence ae ON ae.id=a.evidence_id
 LEFT JOIN artifacts aa ON aa.id=ae.artifact_id GROUP BY av.vulnerability_id
), claim AS (
 SELECT ac.vulnerability_id,count(DISTINCT ac.id) claim_count,
 group_concat(DISTINCT CASE WHEN ac.relationship='affected' THEN sp.part_number END) affected_parts,
 count(DISTINCT CASE WHEN ac.relationship='affected' THEN hs.hardware_model_id END) device_count
 FROM applicability_claims ac LEFT JOIN silicon_parts sp
 ON ac.subject_type='silicon_part' AND sp.id=ac.subject_id
 LEFT JOIN hardware_silicon hs ON hs.part_id=sp.id GROUP BY ac.vulnerability_id
), fix AS (
 SELECT vulnerability_id,count(*) fix_count FROM fix_claims GROUP BY vulnerability_id
), catalog AS (
 SELECT v.id vulnerability_id,v.cve_id cve,b.bulletin,
 coalesce(v.published_at,b.bulletin_date) published_at,
 coalesce(v.summary,'Component not specified') component,b.evidence,b.source_urls,
 coalesce(c.claim_count,0) claim_count,coalesce(c.device_count,0) device_count,
 c.affected_parts,coalesce(f.fix_count,0) fix_count
 FROM vulnerabilities v JOIN bulletin b ON b.vulnerability_id=v.id
 LEFT JOIN claim c ON c.vulnerability_id=v.id LEFT JOIN fix f ON f.vulnerability_id=v.id
) """


def present(row: sqlite3.Row) -> dict:
    item = dict(row)
    part, fixes = bool(item['affected_parts']), bool(item['fix_count'])
    item.update(severity='Unscored', score='—', cve_url=f"https://nvd.nist.gov/vuln/detail/{item['cve']}",
                chip=item['affected_parts'] or 'Applicability unresolved', devices=item['device_count'],
                state=('Part applicability + fix coordinate' if part and fixes else
                       'Part applicability' if part else 'Component applicability' if item['claim_count'] else 'Catalog only'),
                reasoning=('Affected-part claims and CVE fix coordinates are captured separately; applicability of a fix to a device is not established.' if part and fixes else
                           'An exact affected silicon part is captured; no CVE fix coordinate is captured.' if part else
                           'Applicability claims are captured, but no exact affected silicon part is established.' if item['claim_count'] else
                           'The CVE appears in a captured bulletin only; device impact is not established.'))
    return item


def query_catalog(connection: sqlite3.Connection, query: dict, limit: int, offset: int):
    value = lambda key: (query.get(key) or [''])[0].strip()
    clauses, params = ['1=1'], []
    if value('q'):
        clauses.append('(cve LIKE ? COLLATE NOCASE OR component LIKE ? COLLATE NOCASE OR bulletin LIKE ? COLLATE NOCASE OR evidence LIKE ? COLLATE NOCASE)')
        params.extend([f"%{value('q')}%"] * 4)
    for key, column, op in [('cve','cve','='),('date_from','substr(published_at,1,10)','>='),('date_to','substr(published_at,1,10)','<=')]:
        if value(key):
            clauses.append(f'{column} {op} ? COLLATE NOCASE');params.append(value(key))
    if value('vendor'):
        clauses.append('evidence LIKE ? COLLATE NOCASE');params.append(f"%{value('vendor')}%")
    if value('mobile_linked') in ('1','true'):
        clauses.append('device_count>0')
    if value('exact_part') in ('1','true'):
        clauses.append('affected_parts IS NOT NULL')
    if value('fix_status') in ('with_coordinate','without_coordinate'):
        clauses.append('fix_count>0' if value('fix_status')=='with_coordinate' else 'fix_count=0')
    if value('part'):
        clauses.append("""EXISTS (SELECT 1 FROM applicability_claims ac JOIN silicon_parts sp
          ON ac.subject_type='silicon_part' AND sp.id=ac.subject_id
          WHERE ac.vulnerability_id=catalog.vulnerability_id AND ac.relationship='affected'
          AND sp.part_number=? COLLATE NOCASE)""")
        params.append(value('part'))
    if value('model'):
        clauses.append("""EXISTS (SELECT 1 FROM applicability_claims ac JOIN hardware_silicon hs
          ON ac.subject_type='silicon_part' AND hs.part_id=ac.subject_id
          JOIN hardware_models hm ON hm.id=hs.hardware_model_id
          WHERE ac.vulnerability_id=catalog.vulnerability_id AND ac.relationship='affected'
          AND hm.model_code=? COLLATE NOCASE)""")
        params.append(value('model'))
    where = ' AND '.join(clauses)
    total=connection.execute(INDEX+f'SELECT count(*) FROM catalog WHERE {where}',params).fetchone()[0]
    order='published_at IS NULL,published_at '+('ASC' if value('sort')=='oldest_asc' else 'DESC')+',cve DESC'
    rows=connection.execute(INDEX+f'SELECT * FROM catalog WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?',[*params,limit,offset]).fetchall()
    return [present(row) for row in rows],total


def detail(connection: sqlite3.Connection, cve: str) -> dict:
    rows,_=query_catalog(connection,{'cve':[cve]},1,0)
    if not rows:
        raise KeyError(cve)
    result=rows[0];identifier=result['vulnerability_id']
    evidence_columns='e.locator,e.excerpt,ar.source_url,ar.sha256,ar.retrieved_at observed_at,s.name source'
    evidence_joins='LEFT JOIN evidence e ON e.id=x.evidence_id LEFT JOIN artifacts ar ON ar.id=e.artifact_id LEFT JOIN sources s ON s.id=ar.source_id'
    result['bulletins']=[dict(row) for row in connection.execute(f'''SELECT x.id,x.title,x.advisory_key,x.published_at,x.evidence_id,
      e.locator,e.excerpt,coalesce(ar.source_url,bs.base_url) source_url,ar.sha256,ar.retrieved_at observed_at,bs.name source,
      CASE WHEN ar.source_url IS NOT NULL THEN 'captured_artifact' ELSE 'source_homepage' END source_url_kind
      FROM advisories x JOIN advisory_vulnerabilities av ON av.advisory_id=x.id JOIN sources bs ON bs.id=x.source_id {evidence_joins}
      WHERE av.vulnerability_id=? ORDER BY x.published_at DESC,x.id''',(identifier,))]
    result['claims']=[dict(row) for row in connection.execute(f'''SELECT x.*,sp.part_number,sp.marketing_name,{evidence_columns}
      FROM applicability_claims x {evidence_joins} LEFT JOIN silicon_parts sp ON x.subject_type='silicon_part' AND sp.id=x.subject_id
      WHERE x.vulnerability_id=? ORDER BY x.relationship,x.subject_type,x.subject_id''',(identifier,))]
    result['fixes']=[dict(row) for row in connection.execute(f'''SELECT x.*,{evidence_columns}
      FROM fix_claims x {evidence_joins} WHERE x.vulnerability_id=? ORDER BY x.fix_kind,x.id''',(identifier,))]
    result['verdicts']=[dict(row) for row in connection.execute('SELECT * FROM security_verdicts WHERE vulnerability_id=? AND is_current=1 ORDER BY subject_type,subject_id',(identifier,))]
    for key,field in [('claims','constraint_json'),('fixes','coordinate_json'),('verdicts','inputs_json')]:
        for item in result[key]:
            item[field.removesuffix('_json')]=json.loads(item.pop(field))
    result['mappedHardware']=[dict(row) for row in connection.execute('''SELECT DISTINCT hm.model_code,hm.id,sp.part_number
      FROM applicability_claims ac JOIN hardware_silicon hs ON ac.subject_type='silicon_part' AND hs.part_id=ac.subject_id
      JOIN hardware_models hm ON hm.id=hs.hardware_model_id JOIN silicon_parts sp ON sp.id=hs.part_id
      WHERE ac.vulnerability_id=? AND ac.relationship='affected' ORDER BY hm.model_code,sp.part_number''',(identifier,))]
    result['boundaries']={'hardwareLinks':'Reviewed hardware containing a claimed affected part; constraints and actual firmware verdicts must still be evaluated.',
      'fixCoordinates':'CVE-level coordinates are not proof that a particular part, product, or installed firmware is fixed.',
      'productApplicability':'Product-level specification matches do not establish exact hardware or CVE applicability.',
      'missingEvidence':'No captured claim or verdict is not proof of absence or safety.'}
    return result
