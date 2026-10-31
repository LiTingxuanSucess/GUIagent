#!/usr/bin/env python3
"""Read persisted job/CLI timings without exporting questions or credentials."""
import argparse
import collections
import csv
import datetime as dt
import json
import re
import statistics
from pathlib import Path


def stamp(value):
    try:
        # Python 3.10 rejects Go's RFC3339 timestamps with nanoseconds.
        return dt.datetime.fromisoformat(re.sub(r'(\.\d{6})\d+', r'\1', value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return 0


def distribution(values):
    values = sorted(v for v in values if isinstance(v, (int, float)) and v > 0)
    if not values:
        return {'count': 0}
    return {'count': len(values), 'median_seconds': round(statistics.median(values) / 1000, 1),
            'p90_seconds': round(values[int((len(values) - 1) * .9)] / 1000, 1),
            'max_seconds': round(values[-1] / 1000, 1)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    parser.add_argument('--start', required=True, help='Inclusive date, China time')
    parser.add_argument('--end', required=True, help='Exclusive date, China time')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    start, end = (stamp(day + 'T00:00:00+08:00') for day in (args.start, args.end))
    if not start or end <= start:
        parser.error('invalid date interval')
    jobs, calls, sites = [], [], collections.defaultdict(list)
    current = collections.Counter()
    for path in (args.data_dir / 'jobs').glob('*.json'):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        current[job.get('status', 'unknown')] += 1
        if start <= stamp(job.get('created_at')) < end:
            jobs.append(job)
            for result in job.get('results') or []:
                sites[result.get('site_id', 'unknown')].append(result)
    for path in (args.data_dir / 'cursor-calls').glob('*.json'):
        try:
            call = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if start <= stamp(call.get('started_at')) < end:
            calls.append(call)
    summary = {
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'start_inclusive': args.start, 'end_exclusive': args.end, 'timezone': 'Asia/Shanghai',
        'notes': ['Job status is the current snapshot, including later completion.',
                  'queued_ms is the initial wait; elapsed_ms accumulates execution rounds.',
                  'total_ms includes delivery retry/backoff; unfinished jobs are excluded from terminal totals.',
                  'Platform elapsed_ms describes the persisted latest result, and may include API fallback.',
                  'Historical CLI success is a recorded verdict, not a new audit of the original page.'],
        'jobs': len(jobs), 'statuses': dict(collections.Counter(j['status'] for j in jobs)),
        'current_statuses': dict(current),
        'initial_queue': distribution([j.get('queued_ms', 0) for j in jobs]),
        'execution_rounds': distribution([j.get('elapsed_ms', 0) for j in jobs]),
        'terminal_total': distribution([j.get('total_ms', 0) for j in jobs if j['status'] in ('succeeded', 'partial', 'failed')]),
        'sites': {site: {'latest_result_time': distribution([r.get('elapsed_ms', 0) for r in rows]),
                         'results': dict(collections.Counter('ok' if r.get('ok') is True else r.get('error_code', 'unknown') for r in rows)),
                         'answer_origins': dict(collections.Counter(r.get('answer_origin') or r.get('source') or 'unknown' for r in rows))}
                  for site, rows in sorted(sites.items())},
        'cli': {'count': len(calls), 'time': distribution([c.get('elapsed_ms', 0) for c in calls]),
                'outcomes': dict(collections.Counter(c.get('status', 'unknown') + ':' + (c.get('error_code') or '') for c in calls)),
                'types': dict(collections.Counter(c.get('task_type', 'unknown') for c in calls))},
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    fields = ['job_id', 'created_at', 'started_at', 'finished_at', 'status', 'sites',
              'initial_queue_seconds', 'execution_rounds_seconds', 'total_seconds', 'retry_count', 'wait_reason']
    with (args.output_dir / 'task-timings.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for job in sorted(jobs, key=lambda j: j.get('created_at', '')):
            row = {k: job.get(k, '') for k in fields[:5] + ['retry_count', 'wait_reason']}
            row['sites'] = ','.join(job.get('sites') or [])
            for name, key in [('initial_queue_seconds', 'queued_ms'), ('execution_rounds_seconds', 'elapsed_ms'), ('total_seconds', 'total_ms')]:
                row[name] = round(job.get(key, 0) / 1000, 1)
            writer.writerow(row)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
