"""Exercise queued-job handover using an isolated server and fixture scraper."""
import argparse
import importlib.util
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--binary', type=Path, default=Path(__file__).resolve().parents[1] / 'bin/geo-server')
args = parser.parse_args()
BINARY = args.binary.resolve()
ROOT = Path(tempfile.mkdtemp(prefix='geo-handover-'))
SERVER = ROOT / 'server'
(SERVER / 'scripts').mkdir(parents=True)
(SERVER / 'web').mkdir()
(SERVER / 'data').mkdir()
script = '''import json
from pathlib import Path
with Path('dispatched.txt').open('a',encoding='utf-8') as stream:stream.write('sent\\n')
print(json.dumps({'ok':True,'answer':'The accepted question completed after maintenance.','citations':['https://example.org/source-a','https://example.org/source-b'],'citation_details':[]}))
'''
(SERVER / 'scripts/run_scrape.py').write_text(script, encoding='utf-8')
barrier = SERVER / 'data/.deployment-drain.json'
barrier.write_text(json.dumps({'expires_at': time.time() + 60, 'allow': []}), encoding='utf-8')
with socket.socket() as reserve:
    reserve.bind(('127.0.0.1', 0))
    port = reserve.getsockname()[1]
BASE = 'http://127.0.0.1:' + str(port)
env = {**os.environ, 'GEO_SERVER_ROOT': str(SERVER), 'GEO_HTTP_ADDR': '127.0.0.1:' + str(port),
       'GEO_SKIP_BROWSER': '1', 'GEO_DOUBAO_BACKEND': 'agentbay',
       'GEO_DIAGNOSTICS_ENABLED': '0', 'GEO_SITE_COOLDOWN_ENABLED': '0',
       'GEO_SINGLE_TOTAL_TIMEOUT_SEC': '30', 'GEO_DOUBAO_SINGLE_TOTAL_TIMEOUT_SEC': '30',
       'GEO_QUEUE_WORKERS': '2', 'GEO_SITE_CONCURRENCY': '2', 'GEO_API_FALLBACK': '0'}
spec = importlib.util.spec_from_file_location('client', Path(__file__).with_name('geo_ask.py'))
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
log_path = ROOT / 'server.log'
log = log_path.open('wb')
log_path.chmod(0o600)
process = None

def start():
    global process, ak, sk
    process = subprocess.Popen([str(BINARY)], cwd=ROOT, env=env, stdout=log, stderr=log)
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if process.poll() is not None:
            raise RuntimeError('isolated server exited; see private log')
        try:
            ak, sk = client.load_aksk(SERVER / '.aksk.json')
            if call('GET', '/api/v1/sites').get('http_status') == 200:
                return
        except (OSError, ValueError):
            pass
        time.sleep(.1)
    raise RuntimeError('isolated startup timeout')

def call(method, path, payload=None):
    return client.call(BASE, method, path, ak, sk, payload)

def stop():
    global process
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

try:
    start()
    payload = {'sites': ['doubao'], 'prompt': 'maintenance acceptance fixture', 'request_id': 'same-business-request'}
    submitted = call('POST', '/api/v1/ask', payload)
    assert submitted.get('http_status') == 202
    job_id = submitted['job_id']
    time.sleep(.3)
    before = call('GET', '/api/v1/jobs/' + job_id)
    assert before['status'] == 'queued' and not (ROOT / 'dispatched.txt').exists()
    cancel_payload = {**payload, 'request_id': 'cancelled-business-request'}
    cancel_submitted = call('POST', '/api/v1/ask', cancel_payload)
    assert cancel_submitted.get('http_status') == 202
    cancel_id = cancel_submitted['job_id']
    for _ in range(2):
        cancelled = call('POST', '/api/v1/jobs/' + cancel_id + '/cancel')
        assert cancelled['http_status'] == 200 and cancelled['status'] == 'cancelled'
        assert cancelled['error_code'] == 'request_cancelled'
    stop()
    start()
    cancelled_after = call('GET', '/api/v1/jobs/' + cancel_id)
    assert cancelled_after['status'] == 'cancelled'
    assert cancelled_after['finished_at'] == cancelled['finished_at']
    cancel_replay = call('POST', '/api/v1/ask', cancel_payload)
    assert cancel_replay['http_status'] == 200 and cancel_replay['job_id'] == cancel_id
    assert cancel_replay['status'] == 'cancelled'
    after = call('GET', '/api/v1/jobs/' + job_id)
    assert after['status'] == 'queued' and after.get('deadline_at') == before.get('deadline_at')
    assert after['request_id'] == payload['request_id'] and not (ROOT / 'dispatched.txt').exists()
    replay = call('POST', '/api/v1/ask', payload)
    assert replay['job_id'] == job_id and replay['http_status'] == 200
    barrier.unlink()
    until = time.monotonic() + 10
    while time.monotonic() < until:
        finished = call('GET', '/api/v1/jobs/' + job_id)
        if finished['status'] in ('succeeded', 'partial', 'failed'):
            break
        time.sleep(.1)
    assert finished['status'] == 'succeeded'
    refs = finished['results'][0]['citations']
    assert len(refs) == 2 and refs == finished['results'][0]['citation_details']
    assert (ROOT / 'dispatched.txt').read_text(encoding='utf-8').splitlines() == ['sent']
    summary = {'passed': True, 'queued_before_and_after_real_restart': True,
               'signed_cancel_idempotent_and_preserved_after_restart': True,
               'request_id_and_deadline_preserved': True, 'executed_once': True,
               'citations_preserved': 2, 'isolated_artifacts': str(ROOT)}
    (ROOT / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)
finally:
    stop()
    log.close()
