#!/usr/bin/env python3
"""Verify quota migration against a private, isolated copy of terminal jobs.

Never starts browsers, copies SMS messages, submits questions, or writes source
data. Refuses a snapshot containing unfinished jobs.
"""
import argparse
import collections
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    source = args.source.resolve()
    root = Path(tempfile.mkdtemp(prefix='geo-limit-preview-'))
    server = root / 'server'
    data = server / 'data'
    (data / 'jobs').mkdir(parents=True)
    (server / 'web').mkdir()
    original = {}
    for path in (source / 'data/jobs').glob('*.json'):
        raw = path.read_bytes()
        job = json.loads(raw)
        if job.get('status') not in ('succeeded', 'partial', 'failed', 'cancelled'):
            raise RuntimeError('snapshot has unfinished jobs; no candidate started')
        original[path.name] = hashlib.sha256(raw).hexdigest()
        (data / 'jobs' / path.name).write_bytes(raw)
    for name in ('accounts.json', 'jobs/account-protection/state.json'):
        destination = data / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if (source / 'data' / name).exists():
            shutil.copy2(source / 'data' / name, destination)
    shutil.copy2(source / 'geo-server.out.log', server / 'geo-server.out.log')
    shutil.copy2(source / 'web/monitor.html', server / 'web/monitor.html')
    config = server / 'config.local.yaml'
    config.write_text('phone: ""\nlisten: "127.0.0.1:0"\npublic_ip: "127.0.0.1"\n'
                      'chrome_path: "/nonexistent/chrome"\n', encoding='utf-8')
    password = uuid.uuid4().hex
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    base = 'http://127.0.0.1:' + str(port)
    env = {**os.environ, 'GEO_SERVER_ROOT': str(server), 'GEO_CONFIG': str(config),
           'GEO_DATA_DIR': str(data), 'GEO_SKIP_BROWSER': '1', 'GEO_PUBLIC_IP': '127.0.0.1',
           'GEO_HTTP_ADDR': f'127.0.0.1:{port}', 'GEO_LISTEN': '127.0.0.1:0',
           'GEO_ADMIN_USER': 'preview', 'GEO_ADMIN_PASSWORD': password,
           'GEO_API_FALLBACK': '0', 'GEO_CURSOR_CAPTCHA_AGENT': '0',
           'GEO_BROWSER_MEMORY_HIGH_MB': '0', 'GEO_BROWSER_MEMORY_MAX_MB': '0'}
    first = None
    with (root / 'candidate.log').open('wb') as log:
        for run in range(2):
            process = subprocess.Popen([str(args.binary.resolve())], cwd=root, env=env,
                                       stdout=log, stderr=log)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError('candidate exited; private log: ' + str(root / 'candidate.log'))
                    try:
                        request = urllib.request.Request(base + '/api/admin/login',
                            data=json.dumps({'user': 'preview', 'password': password}).encode(),
                            headers={'Content-Type': 'application/json'})
                        with opener.open(request, timeout=2) as response:
                            assert response.status == 200
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('candidate startup timeout') from None
                        time.sleep(.2)
                with opener.open(base + '/api/admin/monitor/actions', timeout=5) as response:
                    body = json.load(response)
                rows = body['account_limits']
                assert rows, 'no migrated quota records'
                accounts = json.loads((data / 'accounts.json').read_text(encoding='utf-8'))
                # Match full known phones instead of guessing from numeric timestamps.
                def phones(value):
                    if isinstance(value, dict):
                        if value.get('phone'): yield str(value['phone'])
                        for child in value.values(): yield from phones(child)
                    elif isinstance(value, list):
                        for child in value: yield from phones(child)
                encoded = json.dumps(body, ensure_ascii=False)
                assert all(phone not in encoded for phone in phones(accounts) if len(phone) >= 11)
                if first is None:
                    first = rows
                else:
                    assert rows == first, 'quota episode changed across restart'
                for name, digest in original.items():
                    assert hashlib.sha256((data / 'jobs' / name).read_bytes()).hexdigest() == digest
            finally:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    active = [row for row in first if row['status'] != 'recovered']
    print(json.dumps({'isolated_root': str(root), 'unchanged_terminal_jobs': len(original),
                      'restart_verified': True, 'active_limits': len(active),
                      'kinds': dict(collections.Counter(row['kind'] for row in active)),
                      'limits': [{k: row[k] for k in ('site', 'account_label', 'kind', 'status',
                                  'first_detected_at', 'last_detected_at', 'reset_at', 'next_probe_at')}
                                 for row in active]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
