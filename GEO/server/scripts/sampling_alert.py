#!/usr/bin/env python3
"""Private durable outbox for immediate recovery notifications."""
import argparse
import fcntl
import json
from pathlib import Path
import sys
import time

from request_monitor import SITES, LABELS, atomic_json, send_feishu


def deliver(config_path, event=None, *, sender=send_feishu, now=None):
    now = time.time() if now is None else now
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding='utf-8'))
    path = config_path.parent / 'recovery-alerts.json'
    with path.with_suffix('.lock').open('w', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {'pending': [], 'sent': {}}
        if event:
            key = '/'.join(str(event.get(k,'')) for k in ('job_id','site','stage','call_id'))
            if now-state['sent'].get(key, 0) >= 300 and not any(e['key']==key for e in state['pending']):
                state['pending'].append({'key': key, 'event': event})
                atomic_json(path, state)
        for row in list(state['pending']):
            e = row['event']
            text = (f"GEO 任务恢复通知\n平台：{SITES.get(e.get('site'),e.get('site','未知'))}"
                    f"\n任务：{e.get('job_id','')}\n阶段：{e.get('stage','')}"
                    f"\n原因：{LABELS.get(e.get('code'),e.get('code',''))} [{e.get('code','')}]"
                    f"\n{e.get('detail','')}" + (f"\nCursor 调用：{e['call_id']}" if e.get('call_id') else '')
                    + "\n人工处理：" + config.get('manual_url',''))
            sender(config, text)
            state['pending'].remove(row)
            state['sent'][row['key']] = now
            state['sent'] = {k:v for k,v in state['sent'].items() if now-v < 86400}
            atomic_json(path, state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--event', action='store_true')
    args = parser.parse_args()
    try:
        deliver(args.config, json.load(sys.stdin) if args.event else None)
        print('Recovery notification accepted (code=0)', flush=True)
    except Exception as error:
        print('Recovery notification deferred: ' + type(error).__name__, flush=True)
        raise SystemExit(1) from None
