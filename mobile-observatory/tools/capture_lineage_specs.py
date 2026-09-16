#!/usr/bin/env python3
"""Capture immutable official LineageOS wiki data; capture-time dependency: PyYAML.

The application/importer remains Python-standard-library only. This command
performs bounded, read-only HTTP requests and never writes a corpus database.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import urllib.request


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent':'MobileObservatory/1.0 source-capture'})
    with urllib.request.urlopen(req, timeout=45) as response:
        data = response.read(4_000_001)
    if len(data)>4_000_000:
        raise ValueError('Unexpectedly large source response')
    return data


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit',required=True,help='Immutable 40-character upstream Git commit')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not re.fullmatch('[a-f0-9]{40}',args.commit):
        parser.error('--commit must be an immutable Git SHA')
    import yaml  # only this optional capture command requires PyYAML
    args.output.mkdir(parents=True,exist_ok=True)
    now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    base=f'https://raw.githubusercontent.com/LineageOS/lineage_wiki/{args.commit}/'
    tree=json.loads(fetch(f'https://api.github.com/repos/LineageOS/lineage_wiki/git/trees/{args.commit}?recursive=1'))
    if tree.get('truncated'):
        raise ValueError('Upstream source tree was truncated')
    paths=sorted(x['path'] for x in tree['tree'] if x['path'].startswith('_data/devices/') and x['path'].endswith('.yml'))
    def one(path):
        data=fetch(base+path)
        obj=yaml.safe_load(data)
        if obj.get('type') not in ('phone','tablet','foldable') or not isinstance(obj.get('soc'),str):
            return None
        rel='raw/'+path.rsplit('/',1)[1]
        target=args.output/rel
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
        return dict(key=target.stem,vendor=obj.get('vendor'),name=obj.get('name'),codename=obj.get('codename'),
            soc=obj['soc'],models=obj.get('models') or [],type=obj['type'],source_url=base+path,
            raw_path=rel,sha256=hashlib.sha256(data).hexdigest(),observed_at=now)
    with ThreadPoolExecutor(max_workers=6) as pool:
        records=[r for r in pool.map(one,paths) if r]
    for upstream,local in [('licenses/LICENSE','LICENSE'),('_includes/layout/footer.html','LICENSE-footer.html')]:
        (args.output/local).write_bytes(fetch(base+upstream))
    capture=dict(format_version=1,source='LineageOS/lineage_wiki',commit=args.commit,observed_at=now,
        license='CC-BY-SA-3.0',attribution='LineageOS contributors',records=records)
    (args.output/'capture.json').write_text(json.dumps(capture,indent=2,sort_keys=True)+'\n')
    print(f'Captured {len(records)} immutable source targets; no corpus changes.')


if __name__=='__main__':
    main()
