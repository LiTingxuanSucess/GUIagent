"""Join every retained CLI invocation to business, page and SMS evidence."""
import argparse
import collections
import csv
import datetime as dt
import json
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))
REASONS = {
    'cli_billing_required': 'Cursor账单阻断，本次未完成修复；不能推断当前仍欠费',
    'cli_timeout': '执行预算耗尽，页面修复未完成',
    'cli_no_output': '等待新输出超时；工具调用次数另列，不能推断完全没执行',
    'sms_required': '当次转交短信；需结合页面验证与同平台短信判断',
    'sms_timeout': '当次短信接续窗口超时，不能仅凭此断定回调接口故障',
    'no_progress': '当前页面操作无进展',
    'target_missing': '原页面目标丢失，不能直接重新发送业务问题',
    'cli_verification_failed': 'CLI结束后的独立页面复核未通过',
    'service_interrupted': '服务停止中断本次调用',
    'cli_account_wait_timeout': '等待其他账号恢复结束超时，本次未启动',
    'cli_resume_failed': '页面复核通过，但原网页续跑未成功',
}


def site_from_text(text):
    for site, labels in [('doubao', ['豆包']), ('kimi', ['Kimi', '月之暗面']),
                         ('wenxinyiyan', ['文心', '百度']), ('yuanbao', ['元宝']),
                         ('qianwen', ['千问', '通义']), ('deepseek', ['DeepSeek', '深度求索'])]:
        if any(label in text for label in labels):
            return site
    return ''


def timestamp(value):
    if isinstance(value, (int, float)):
        return value
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)).timestamp()
    except ValueError:
        return 0


def audit(root, date):
    data = root / 'data'
    index = json.loads((data / 'monitor-requests.json').read_text(encoding='utf-8'))
    start = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(), TZ).timestamp()
    calls = [c for c in index['cursor_calls'] if start <= c['started_at'] < start + 86400]
    accounts = {a['id']: a for a in json.loads((data / 'accounts.json').read_text(encoding='utf-8'))['accounts']}
    parents = {r['id']: r for r in index['rows']}
    messages = [json.loads(line) for line in (data / 'sms.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    rows = []
    for call in sorted(calls, key=lambda c: (c['started_at'], c['id'])):
        raw_path = data / 'cursor-calls' / (call['id'] + '.json')
        raw = json.loads(raw_path.read_text(encoding='utf-8')) if raw_path.exists() else {}
        account = accounts.get(call['account_id'], {})
        internal = 'yiyan' if call['site'] == 'wenxinyiyan' else call['site']
        parent = parents.get(call['job_id'], {})
        result = next((r for r in parent.get('results', []) if r['site'] == call['site']), {})
        phone = account.get('phone', '')
        # A nearby callback is correlation, not proof it was for this invocation.
        related_sms = [m for m in messages if phone and ''.join(filter(str.isdigit, m.get('receiver', '')))[-11:] == phone[-11:]
                       and site_from_text(m.get('result', '')) == call['site']
                       and call['started_at'] - 2 <= timestamp(m.get('received_at')) <= (call.get('finished_at') or call['started_at']) + 180]
        probe = raw.get('probe_reason') or ''
        reason = REASONS.get(call.get('error_code'), '页面复核成功' if call['status'] == 'success' else '未保留足够证据进一步定位')
        if call.get('error_code') in ('sms_required', 'sms_timeout'):
            reason += '；观察到验证层仍在' if probe == 'captcha_visible' else '；观察到仍未登录' if probe == 'logged_out' else ''
            reason += '；窗口内有匹配平台回调（关联证据，非提交成功证明）' if related_sms else '；保留记录中未见窗口内匹配平台回调'
        row = {key: call.get(key, '') for key in ['id', 'job_id', 'site', 'account_id', 'account_label', 'task_type', 'status', 'error_code', 'process_status', 'process_error_code', 'verified', 'resume_ok', 'cli_assisted', 'tool_calls', 'elapsed_ms', 'build_id']}
        row.update(started_at=dt.datetime.fromtimestamp(call['started_at'], TZ).isoformat(),
                   assessment=reason, probe_reason=probe, probe_error_code=raw.get('probe_error_code', ''),
                   parent_status=parent.get('status', 'account_maintenance' if call['job_id'] in ('login-check', 'manual-login') else 'unrecorded'),
                   final_platform_source=result.get('source', ''), final_platform_ok=result.get('ok'),
                   current_account_status=account.get('sites', {}).get(internal, {}).get('status', 'unrecorded'),
                   nearby_sms_callbacks=len(related_sms))
        rows.append(row)
    waiting = [r for r in rows if r['status'] in ('waiting_sms', 'action_required')]
    summary = {'date': date, 'generated_at': dt.datetime.now(TZ).isoformat(), 'calls': len(rows),
               'status_counts': dict(collections.Counter(r['status'] for r in rows)),
               'failed_reasons': dict(collections.Counter(r['error_code'] for r in rows if r['status'] == 'failed')),
               'waiting_unique_account_sites': len({(r['account_id'], r['site']) for r in waiting}),
               'waiting_parent_outcomes': dict(collections.Counter(r['parent_status'] for r in waiting)),
               'by_site': {site: dict(collections.Counter(r['status'] for r in rows if r['site'] == site)) for site in sorted({r['site'] for r in rows})},
               'note': '逐条历史证据；当前账号状态不等于当时状态。窗口内短信仅为时间/账号/平台关联，不证明已提交或登录成功。未修改任务或发起外部调用。'}
    return rows, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--date', default=str(dt.datetime.now(TZ).date()))
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    rows, summary = audit(args.root, args.date)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.output_dir / 'calls.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    with (args.output_dir / 'calls.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ['id'])
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("'" + str(v) if str(v).lstrip().startswith(('=', '+', '-', '@')) else v) for k, v in row.items()})
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
