#!/usr/bin/env python3
"""Measure signed GEO reads or explicitly requested real question workloads."""
import argparse
import collections
import concurrent.futures
import datetime
import json
import math
import statistics
import tempfile
import threading
import time
import uuid
from pathlib import Path

import geo_ask
from verify_job import canonical_site, valid_url, urls

QUESTIONS = ['仝人智能科技是做什么的？', '拓扑同构模型是什么意思？',
             '智能焊接机器人哪个品牌好？', '国内具身智能公司有哪些值得推荐？',
             '从哪获取行业研究报告？', '平价连锁咖啡推荐']


def write_private(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    path.chmod(0o600)


def percentile(values, p):
    return round(sorted(values)[max(0, math.ceil(len(values) * p) - 1)], 2) if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=['read', 'ask'], required=True)
    parser.add_argument('--job-id', help='Existing job for read pressure; read mode submits no questions')
    parser.add_argument('--count', type=int, default=6)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--sites', default='doubao', help='Comma-separated sites per question')
    parser.add_argument('--site-cycle', help='Instead send each question to one site, cycling through this list')
    parser.add_argument('--base', default=geo_ask.DEFAULT_BASE)
    parser.add_argument('--keys', type=Path, default=geo_ask.ROOT / '.aksk.json')
    parser.add_argument('--saved-dir', type=Path)
    parser.add_argument('--wait-sec',type=int,default=None,help='显式限制每个任务的客户端等待秒数；默认等到终态')
    args = parser.parse_args()
    if args.count < 1 or not 1 <= args.concurrency <= 128:
        parser.error('positive count and concurrency 1..128 required')
    if args.wait_sec is not None and args.wait_sec<=0:
        parser.error('--wait-sec must be positive')
    if args.scenario == 'read' and not args.job_id:
        parser.error('read scenario requires --job-id')
    ak, sk = geo_ask.load_aksk(args.keys)
    out = Path(tempfile.mkdtemp(prefix='geo-load-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-'))
    samples, rows, exceptions = [], [], []
    done = threading.Event()

    def monitor():
        while not done.is_set():
            row = {'at': time.time()}
            for key, file in [('browser_bytes', '/sys/fs/cgroup/geo-browsers/memory.current'),
                              ('browser_events', '/sys/fs/cgroup/geo-browsers/memory.events'),
                              ('memory_pressure', '/proc/pressure/memory')]:
                try:
                    value = Path(file).read_text(encoding='utf-8').strip()
                    row[key] = int(value) if key == 'browser_bytes' else value
                except (OSError, ValueError):
                    pass
            try:
                memory = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines() if line.startswith(('MemAvailable:', 'SwapFree:'))}
                row.update(memory)
            except (OSError, ValueError):
                pass
            samples.append(row)
            done.wait(2)

    def call(method, path, payload=None):
        # Retry only reads; POSTs have their own stable idempotency key but
        # submission failures are recorded, never hidden by fresh requests.
        for attempt in range(3):
            try:
                return geo_ask.call(args.base, method, path, ak, sk, payload)
            except OSError:
                if method != 'GET' or attempt == 2:
                    raise
                time.sleep(.2)

    def run(index):
        start = time.monotonic()
        if args.scenario == 'read':
            job = call('GET', '/api/v1/jobs/' + args.job_id)
            return {'index': index, 'http_status': job.get('http_status'),
                    'ok': job.get('http_status') == 200 and job.get('job_id') == args.job_id,
                    'client_ms': (time.monotonic() - start) * 1000}
        sites = args.sites.split(',')
        if args.site_cycle:
            cycle = args.site_cycle.split(',')
            sites = [cycle[index % len(cycle)]]
        payload = {'sites': sites, 'prompt': QUESTIONS[index % len(QUESTIONS)],
                   'request_id': 'pressure-' + uuid.uuid4().hex}
        submitted = call('POST', '/api/v1/ask', payload)
        jid = submitted.get('job_id')
        if not jid:
            return {'index': index, 'ok': False, 'http_status': submitted.get('http_status'),
                    'submission_failed': True, 'client_ms': (time.monotonic() - start) * 1000}
        write_private(out / (jid + '-submitted.json'), submitted)
        job=submitted
        while (remaining:=geo_ask.wait_remaining(start,args.wait_sec)) is None or remaining>0:
            job = call('GET', '/api/v1/jobs/' + jid)
            if geo_ask.job_read_unavailable(job):
                return {'index':index,'job_id':jid,'http_status':job.get('http_status'),'ok':False,
                        'client_read_error':True,'unfinished':True,'client_ms':(time.monotonic()-start)*1000}
            if geo_ask.classify_wait(str(job.get('status') or '')) == 'finished':
                break
            time.sleep(2 if remaining is None else min(2,remaining))
        write_private(out / (jid + '.json'), job)
        saved = {}
        if args.saved_dir:
            saved_job = json.loads((args.saved_dir / (jid + '.json')).read_text(encoding='utf-8'))
            saved = {canonical_site(r['site_id']): r for r in saved_job.get('results', [])}
        results = []
        for r in job.get('results') or []:
            site = canonical_site(r.get('site_id'))
            refs = r.get('citations') or []
            valid = bool(refs) and all(isinstance(v, dict) and valid_url(v.get('url')) for v in refs) and refs == r.get('citation_details')
            matches = True
            if args.saved_dir:
                raw = saved.get(site, {})
                matches = site in saved and urls(refs) == urls(raw.get('citations')) | urls(raw.get('citation_details'))
            results.append({'site': site, 'ok': r.get('ok') is True, 'answer_chars': len(r.get('answer') or ''),
                            'citations': len(refs), 'references_valid': valid, 'saved_matches': matches,
                            **{k: r.get(k) for k in ['answer_origin', 'browser_backend', 'account_retries', 'error_code',
                                                    'api_error_code', 'web_error_code', 'web_skip_reason', 'share_status']}})
        complete = sorted(canonical_site(s) for s in sites) == sorted(r['site'] for r in results)
        return {'index': index, 'job_id': jid, 'http_status': submitted.get('http_status'),
                'status': job.get('status'), 'total_ms': job.get('total_ms'), 'queued_ms': job.get('queued_ms'),
                'unfinished':geo_ask.classify_wait(str(job.get('status') or ''))!='finished',
                'client_ms': (time.monotonic() - start) * 1000, 'results': results,
                'ok': job.get('status') == 'succeeded' and complete and all(r['ok'] and r['answer_chars'] and r['references_valid'] and r['saved_matches'] for r in results)}

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    print(json.dumps({'event': 'started', 'scenario': args.scenario, 'count': args.count,
                      'concurrency': args.concurrency, 'private_artifacts': str(out)}), flush=True)
    started = last_report = time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            pending = {executor.submit(run, i): i for i in range(args.count)}
            while pending:
                finished, _ = concurrent.futures.wait(pending, timeout=2, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in finished:
                    index = pending.pop(future)
                    try:
                        row = future.result()
                    except Exception as exc:
                        row = {'index': index, 'ok': False, 'exception': type(exc).__name__}
                        exceptions.append(row)
                    rows.append(row)
                    if args.scenario == 'ask':
                        print(json.dumps({'event': 'completed', **row}, ensure_ascii=False), flush=True)
                if time.monotonic() - last_report >= 25:
                    print(json.dumps({'event': 'progress', 'finished': len(rows), 'total': args.count,
                                      'elapsed_sec': round(time.monotonic() - started, 1)}), flush=True)
                    last_report = time.monotonic()
    finally:
        done.set()
        thread.join(timeout=3)
        write_private(out / 'resources.json', samples)
    duration = time.monotonic() - started
    times = [r['client_ms'] for r in rows if 'client_ms' in r]
    platform = collections.defaultdict(collections.Counter)
    for row in rows:
        for r in row.get('results', []):
            c = platform[r['site']]
            c['calls'] += 1
            c['answers'] += r['ok'] and bool(r['answer_chars'])
            c['with_references'] += r['references_valid']
            if r.get('error_code'):
                c[r['error_code']] += 1
            if r.get('api_error_code'):
                c[r['api_error_code']] += 1
    summary = {'scenario': args.scenario, 'count': args.count, 'concurrency': args.concurrency,
               'passed': sum(r['ok'] for r in rows), 'failed': sum(not r['ok'] for r in rows),
               'duration_sec': round(duration, 2), 'requests_per_sec': round(len(rows) / duration, 2),
               'p50_ms': percentile(times, .5), 'p95_ms': percentile(times, .95), 'p99_ms': percentile(times, .99),
               'http_statuses': dict(collections.Counter(str(r.get('http_status')) for r in rows)),
               'peak_browser_mib': max((s.get('browser_bytes', 0) for s in samples), default=0) // 1048576,
               'platforms': platform, 'results': rows, 'exceptions': exceptions}
    write_private(out / 'summary.json', summary)
    print(json.dumps({'event': 'summary', **{k: v for k, v in summary.items() if k not in ('results', 'exceptions')}}, ensure_ascii=False), flush=True)
    return 0 if summary['failed'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
