#!/usr/bin/env python3
"""Offline catalog launcher, exports, and local-only operator web application."""
from __future__ import annotations

import argparse
import hmac
import json
import secrets
import shutil
import sqlite3
import tempfile
import threading
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import catalog_lock, session as connect, encoded, export_csv, now, sha256, summary
from import_corpus import import_corpus
from catalog import list_rows, quality, prompt_for, get_record
from research import enrich, seed_targets, import_assertions
from updater import PRESETS, apply_candidate, config, run_update, save_config, test_command

ROOT = Path(__file__).resolve().parent


def bundle(data, output):
    """SQLite backup API gives a consistent copy, including any journaled writes."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'device-catalog'; root.mkdir()
        for name in ('core.py', 'import_corpus.py', 'updater.py', 'xiaomi_format.py', 'run.py', 'catalog.py', 'research.py', 'README.md'):
            shutil.copy2(ROOT / name, root / name)
        shutil.copytree(ROOT / 'web', root / 'web')
        (root / 'data').mkdir()
        with connect(data) as source, sqlite3.connect(root / 'data/catalog.sqlite') as target:
            source.backup(target)
        with connect(root / 'data') as c:
            export_csv(c, root / 'csv')
            counts = summary(c)
            # Keep evidence needed to review pending proposals after unpacking.
            for row in c.execute('SELECT DISTINCT path FROM captures'):
                source = data / row[0]
                if source.is_file():
                    dest = root / 'data' / row[0]; dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
        (root / 'data/settings.json').write_text(encoded({k: config(data)[k] for k in ('firmware_regions', 'page_size')}))
        files = {str(p.relative_to(root)): sha256(p) for p in root.rglob('*') if p.is_file()}
        (root / 'manifest.json').write_text(json.dumps({'created_at': now(), 'counts': {k: counts[k] for k in ('devices', 'models', 'firmwares')},
            'files_sha256': files, 'requires': 'Python 3.11+; no pip install; network only for updates'}, indent=2) + '\n')
        pending = output.with_suffix('.zip.tmp')
        with zipfile.ZipFile(pending, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in root.rglob('*'):
                if p.is_file():
                    z.write(p, p.relative_to(root.parent))
        pending.replace(output)
    return {'path': str(output), 'bytes': output.stat().st_size, 'sha256': sha256(output)}


def serve(data, port):
    token = secrets.token_urlsafe(32)
    lock = threading.Lock()
    job = {'busy': False, 'action': None, 'result': None, 'error': None}
    with connect(data) as c:
        c.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'", (now(),))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, body, kind='application/json', status=200, filename=None):
            if not isinstance(body, bytes):
                body = encoded(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers(); self.wfile.write(body)

        def valid_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self.valid_host():
                return self.send({'error': 'Local host required'}, status=403)
            parsed = urllib.parse.urlparse(self.path); path = parsed.path
            try:
                if path in ('/', '/app.js', '/style.css'):
                    name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                    kind = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript', '/style.css': 'text/css'}[path]
                    return self.send((ROOT / 'web' / name).read_bytes(), kind)
                if path == '/api/session':
                    return self.send({'token': token, 'job': job, 'presets': PRESETS})
                if path == '/api/settings':
                    return self.send(config(data))
                if path == '/api/regions':
                    with connect(data) as c:
                        return self.send([dict(r) for r in c.execute('SELECT region,count(*) AS count FROM firmwares GROUP BY region ORDER BY region')])
                if path == '/api/status':
                    with connect(data) as c:
                        return self.send({**summary(c), 'job': job})
                if path in ('/api/rows', '/api/quality', '/api/prompt', '/api/record'):
                    args = urllib.parse.parse_qs(parsed.query)
                    get = lambda key, default='': args.get(key, [default])[0]
                    with connect(data) as c:
                        if path == '/api/quality':
                            return self.send(quality(c))
                        if path == '/api/prompt':
                            return self.send(prompt_for(c, get('table'), get('id'), get('field')))
                        if path == '/api/record':
                            return self.send(get_record(c, get('table'), get('id')))
                        return self.send(list_rows(c, config(data), table=get('table','devices'), query=get('q'),
                            offset=get('offset',0), limit=get('limit',config(data)['page_size']), sort=get('sort'),
                            direction=get('direction'), missing=get('missing'), manufacturer=get('manufacturer'),
                            related_device=get('related_device'),related_model=get('related_model')))
                if path.startswith('/download/'):
                    filename = path.removeprefix('/download/')
                    if filename == 'device-catalog.zip':
                        p = data / 'exports' / filename; kind = 'application/zip'
                    elif filename in ('devices.csv', 'models.csv', 'device_models.csv', 'firmwares.csv'):
                        p = data / 'exports' / filename; kind = 'text/csv; charset=utf-8'
                    else:
                        return self.send({'error': 'Not found'}, status=404)
                    if not p.is_file():
                        return self.send({'error': 'Build exports or bundle first'}, status=404)
                    return self.send(p.read_bytes(), kind, filename=filename)
                return self.send({'error': 'Not found'}, status=404)
            except (ValueError, OSError, sqlite3.Error) as e:
                return self.send({'error': str(e)}, status=400)

        def do_POST(self):
            origin = self.headers.get('Origin')
            allowed_origins = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
            if not self.valid_host() or (origin and origin not in allowed_origins) or not hmac.compare_digest(self.headers.get('X-Catalog-Token', ''), token):
                return self.send({'error': 'Local session token required'}, status=403)
            if self.headers.get('Content-Type') != 'application/json':
                return self.send({'error': 'JSON required'}, status=400)
            acquired = False
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 100000:
                    raise ValueError('Invalid request size')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('Expected JSON object')
                acquired = lock.acquire(blocking=False)
                if not acquired:
                    return self.send({'error': 'Another operation is running'}, status=409)
                if self.path == '/api/preferences':
                    if not set(body) <= {'page_size', 'firmware_regions'}:
                        raise ValueError('Unknown display preference')
                    save_config(data, {**config(data), **body})
                    return self.send({'saved': True})
                if self.path == '/api/research-import':
                    return self.send(import_assertions(data, body.get('assertions')))
                if self.path == '/api/settings':
                    save_config(data, body)
                    return self.send({'saved': True})
                if self.path == '/api/decision':
                    with connect(data) as c:
                        if body.get('decision') == 'apply':
                            changed = apply_candidate(c, body.get('id'))
                            if changed:
                                c.execute("UPDATE candidates SET rationale=rationale || ' Applied by local operator.' WHERE id=?", (body['id'],))
                        elif body.get('decision') == 'reject':
                            c.execute("UPDATE candidates SET status='rejected',rationale=rationale || ' Rejected by local operator.' WHERE id=? AND status IN ('pending','review','approved')", (body.get('id'),))
                            changed = 0
                        else:
                            raise ValueError('Unknown review decision')
                    return self.send({'applied': changed})
                actions = {'/api/update', '/api/review', '/api/test-command', '/api/export', '/api/bundle', '/api/enrich'}
                if self.path not in actions:
                    return self.send({'error': 'Not found'}, status=404)
                action = self.path
                job.update(busy=True, action=action, result=None, error=None)

                def work():
                    try:
                        if action in ('/api/update', '/api/review'):
                            result = run_update(data, discover=action == '/api/update')
                        elif action == '/api/enrich':
                            result = enrich(data, 50)
                        elif action == '/api/test-command':
                            result = test_command(config(data))
                        elif action == '/api/bundle':
                            result = bundle(data, data / 'exports/device-catalog.zip')
                        else:
                            with connect(data) as c:
                                export_csv(c, data / 'exports')
                            result = {'exported_at': now(), 'files': ['devices.csv', 'firmwares.csv', 'models.csv', 'device_models.csv']}
                        job['result'] = result
                    except Exception as e:
                        job['error'] = str(e)[:500]
                    finally:
                        job['busy'] = False
                        lock.release()
                threading.Thread(target=work, daemon=True).start()
                acquired = False  # Worker owns the lock now.
                return self.send({'started': True}, status=202)
            except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as e:
                return self.send({'error': str(e)}, status=400)
            finally:
                if acquired:
                    lock.release()

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'Device Catalog: http://127.0.0.1:{server.server_port} (Ctrl+C to stop)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    sub = parser.add_subparsers(dest='action')
    serving = sub.add_parser('serve'); serving.add_argument('--port', type=int, default=8130)
    importing = sub.add_parser('import'); importing.add_argument('corpus', type=Path)
    exporting = sub.add_parser('export'); exporting.add_argument('--output', type=Path, default=ROOT / 'dist/csv')
    packing = sub.add_parser('bundle'); packing.add_argument('--output', type=Path, default=ROOT / 'dist/device-catalog.zip')
    researching = sub.add_parser('enrich'); researching.add_argument('--limit', type=int, default=50)
    seeding = sub.add_parser('research-seed'); seeding.add_argument('corpus', type=Path)
    assertions = sub.add_parser('research-import'); assertions.add_argument('file', type=Path)
    sub.add_parser('status'); sub.add_parser('update'); sub.add_parser('review'); sub.add_parser('test-command')
    args = parser.parse_args(); data = args.data_dir.resolve()
    if args.action == 'status':
        return execute(args, data)
    try:
        with catalog_lock(data):
            execute(args, data)
    except ValueError as e:
        parser.exit(1, str(e) + '\n')


def execute(args, data):
    if args.action == 'import':
        import_corpus(args.corpus, data)
    elif args.action == 'research-seed':
        print(encoded(seed_targets(data, args.corpus)))
    elif args.action == 'research-import':
        print(encoded(import_assertions(data, json.loads(args.file.read_text()))))
    elif args.action == 'enrich':
        print(encoded(enrich(data, args.limit)))
    elif args.action == 'export':
        with connect(data) as c:
            export_csv(c, args.output)
        print(args.output)
    elif args.action == 'bundle':
        print(encoded(bundle(data, args.output)))
    elif args.action == 'status':
        with connect(data) as c:
            print(json.dumps(summary(c), indent=2))
    elif args.action in ('update', 'review'):
        print(encoded(run_update(data, args.action == 'update')))
    elif args.action == 'test-command':
        print(encoded(test_command(config(data))))
    else:
        serve(data, getattr(args, 'port', 8130))


if __name__ == '__main__':
    main()
