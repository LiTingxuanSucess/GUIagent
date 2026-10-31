"""Classify retained webpage attempts and join exact diagnostic log evidence."""
from __future__ import annotations
import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import re

TZ = dt.timezone(dt.timedelta(hours=8))
CATEGORIES = {
    'platform_risk_control': ('安全验证', '平台触发验证码或安全验证，原页面未完成恢复'),
    'platform_quota': ('额度或账号限制', '网页账号的额度或发言权限受限；需区分免费额度耗尽与账号禁言'),
    'platform_auth_unavailable': ('登录不可用', '账号未通过登录复核或原登录入口不可用'),
    'generation_timeout': ('生成超时', '问题已发送，但预算内未确认完整回答；不能仅凭错误码判断平台慢还是完成检测误判'),
    'answer_extract_timeout': ('正文提取超时', '正文提取阶段未取得可验证的完整回答，需检查DOM和完成检测'),
    'site_timeout': ('网页流程超时或异常', '流程未在预算内完成；需要区分真实等待超时和被统一包装的浏览器异常'),
    'send_timeout': ('发送超时', '输入或发送确认阶段超时；必须核实原页面，不应直接重新发问'),
    'recovery_unconfirmed': ('发送或恢复未确认', '不能确认原问题发送或页面恢复状态，保留原页面避免重复发送'),
    'site_request_failed': ('通用采集失败', '通用错误，不足以直接判断网络故障；检查页面崩溃、空回答及具体诊断'),
    'platform_busy': ('网页繁忙或加载未完成', '网页提示繁忙、页面仍在加载或未开始生成'),
    'browser_unresponsive': ('浏览器无进展', '浏览器持续无响应或页面在观察窗口内无进展'),
    'recovery_page_missing': ('原页面丢失', '原账号的目标页面已丢失，不能直接重新发送已发送的问题'),
    'sms_timeout': ('短信接续超时', '短信接续或登录复核未在窗口内完成，不等于回调接口故障'),
}


def canonical_site(site):
    return 'wenxinyiyan' if site == 'yiyan' else site


def signal_label(text):
    if '禁言' in text: return '日志明确记录账号被平台禁言，不能当作免费额度耗尽'
    if re.search(r'page crashed|target crashed', text, re.I): return '日志明确记录页面崩溃，具体崩溃原因尚未证实'
    if '未返回有效回答' in text or '未取得有效回答' in text: return '返回或提取结果为空，不代表HTTP网络请求失败'
    if '发送点击结果未知' in text: return '点击后发送状态未知，代码选择不再次回车'
    if 'intercepts pointer events' in text: return '交互控件被其他页面元素遮挡'
    if 'strict mode violation' in text: return '页面定位器匹配到多个元素'
    return ''


def read_log_evidence(path):
    diagnostics = collections.defaultdict(list)
    failures = collections.defaultdict(list)
    if not path.exists(): return diagnostics, failures
    diagnostic = re.compile(r'scrape diagnostic job=(\w+) site=(\w+) diagnostic_id=([\w-]+) status=(\w+) error_code=(\w*) elapsed_ms=(\d+)')
    failure = re.compile(r'scrape failed job=(\w+) site=(\w+) account=([\w-]+) elapsed_ms=(\d+): (.*)')
    with path.open(encoding='utf-8', errors='replace') as stream:
        for line_number, line in enumerate(stream, 1):
            try: at = dt.datetime.strptime(line[:19], '%Y/%m/%d %H:%M:%S').replace(tzinfo=TZ).timestamp()
            except ValueError: continue
            if found := diagnostic.search(line):
                job, site, identity, status, code, elapsed = found.groups()
                started = re.search(r'-(\d{18,20})$', identity)
                if started:
                    diagnostics[(job, canonical_site(site))].append({'id': identity, 'started': int(started[1])/1e9,
                        'status': status, 'error_code': code, 'line': line_number})
            elif found := failure.search(line):
                job, site, account, elapsed, detail = found.groups()
                failures[(job, canonical_site(site), account)].append({'at': at, 'signal': signal_label(detail), 'line': line_number})
    return diagnostics, failures


def csv_write(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ['job_id'])
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "'"+str(v) if str(v).lstrip().startswith(('=', '+', '-', '@')) else v for k,v in row.items()})


def analyze(root, start_date, end_date):
    raw_index = (root/'data/monitor-requests.json').read_bytes()
    index = json.loads(raw_index)
    start = dt.datetime.combine(dt.date.fromisoformat(start_date), dt.time(), TZ).timestamp()
    end = dt.datetime.combine(dt.date.fromisoformat(end_date), dt.time(), TZ).timestamp()
    if end <= start: raise ValueError('end-date 必须晚于 start-date，结束日期不包含当天')
    cutoff = min(end, index['updated_at'])
    diagnostics, log_failures = read_log_evidence(root/'geo-server.out.log')
    failures, skipped = [], []
    daily = collections.defaultdict(collections.Counter)
    result_sources, unique_jobs = {}, {}
    dropped = 0
    for job in index['rows']:
        for result in job.get('results', []):
            dropped += result.get('attempts_dropped', 0)
            for number, attempt in enumerate(result.get('attempts', [])):
                at = attempt.get('started_at') or job['created']
                if attempt.get('channel') != 'web' or not start <= at < cutoff: continue
                day = str(dt.datetime.fromtimestamp(at, TZ).date())
                status = attempt.get('status', 'unknown')
                if attempt.get('skipped'): status = 'skipped'
                daily[day][status] += 1
                if status not in ('failed', 'skipped'): continue
                code = attempt.get('error_code') or 'unknown'
                category, reason = CATEGORIES.get(code, ('未分类', '现有记录不足以确定更具体原因'))
                row = {'job_id': job['id'], 'site': result['site'], 'attempt_index': number,
                       'account_label': attempt.get('account_label', ''), 'started_at': dt.datetime.fromtimestamp(at, TZ).isoformat(),
                       'elapsed_ms': attempt.get('elapsed_ms', 0), 'status': status, 'error_code': code,
                       'category': category, 'analysis': reason, 'inferred': attempt.get('inferred', False),
                       'parent_status': job['status'], 'final_platform_source': result.get('source'),
                       'final_platform_ok': result.get('ok'), 'diagnostic_id': '', 'diagnostic_error_code': '',
                       'failed_phase': '', 'diagnostic_log_line': '', 'secondary_evidence': '', 'correlated_log_line': ''}
                if code == 'platform_quota' and result['site'] == 'kimi':
                    row['analysis'] = 'Kimi网页免费额度已用完，CLI页面修复不能补充额度，应等待额度重置或使用仍有额度的账号'
                if status == 'skipped':
                    row.update(category='未实际调用（跳过）', analysis='按原记录单列，不作为实际网页失败计入分母')
                    skipped.append(row); continue
                candidates = [e for e in diagnostics.get((job['id'], result['site']), []) if abs(e['started']-at)<.001]
                if candidates:
                    event = min(candidates, key=lambda e: abs(e['started']-at))
                    row.update(diagnostic_id=event['id'], diagnostic_error_code=event['error_code'], diagnostic_log_line=event['line'])
                    manifest = root/'data/diagnostics'/event['id']/'manifest.json'
                    if manifest.exists():
                        detail = json.loads(manifest.read_text(encoding='utf-8'))
                        row['failed_phase'] = detail.get('failed_phase') or detail.get('phase', '')
                        row['secondary_evidence'] = signal_label(str(detail.get('error', '')))
                expected_end = at + attempt.get('elapsed_ms', 0)/1000
                related = [e for e in log_failures.get((job['id'], result['site'], attempt.get('account_id')), []) if abs(e['at']-expected_end)<5]
                if related:
                    event = min(related, key=lambda e: abs(e['at']-expected_end))
                    row['correlated_log_line'] = event['line']
                    if event['signal']: row['secondary_evidence'] = event['signal']
                failures.append(row)
                unique_jobs[job['id']] = job['status']
                result_sources[(job['id'], result['site'])] = (result.get('source'), result.get('ok'))
    summary = {'start': dt.datetime.fromtimestamp(start, TZ).isoformat(), 'end_exclusive': dt.datetime.fromtimestamp(end,TZ).isoformat(),
        'snapshot_at': dt.datetime.fromtimestamp(index['updated_at'], TZ).isoformat(), 'index_sha256': hashlib.sha256(raw_index).hexdigest(),
        'failures': len(failures), 'skipped': len(skipped), 'unique_affected_jobs': len(unique_jobs),
        'affected_job_status': dict(collections.Counter(unique_jobs.values())), 'affected_job_site_pairs': len(result_sources),
        'final_sources_by_job_site': dict(collections.Counter(source if ok else 'not_delivered' for source,ok in result_sources.values())),
        'error_codes': dict(collections.Counter(row['error_code'] for row in failures)),
        'categories': dict(collections.Counter(row['category'] for row in failures)),
        'daily': {day: dict(counts) for day,counts in sorted(daily.items())},
        'by_site': {site: dict(collections.Counter(row['error_code'] for row in failures if row['site']==site)) for site in sorted({row['site'] for row in failures})},
        'skipped_reasons': dict(collections.Counter(row['error_code'] for row in skipped)),
        'exact_diagnostic_matches': sum(bool(row['diagnostic_id']) for row in failures),
        'inferred_failures': sum(row['inferred'] is True for row in failures),
        'secondary_evidence': dict(collections.Counter(row['secondary_evidence'] for row in failures if row['secondary_evidence'])),
        'retained_index_attempts_dropped_all_dates': dropped,
        'method': '按尝试开始时间（北京时间）统计。每条失败保留；重试不去重；日志复核/恢复输出不另加一次失败。跳过和历史未知单列。诊断ID仅在任务/平台/启动时间1毫秒内一致时精确关联；其他日志只在任务/平台/账号/结束时间5秒内关联。未修改任务，不提交真实问题。'}
    return failures, skipped, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--start-date', required=True)
    parser.add_argument('--end-date', required=True, help='结束日期不包含当天')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    failures, skipped, summary = analyze(args.root, args.start_date, args.end_date)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_write(args.output_dir/'failures.csv', failures)
    csv_write(args.output_dir/'skipped.csv', skipped)
    (args.output_dir/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__': main()
