#!/usr/bin/env python3
"""Independent request monitor. Private config/state; public summaries omit prompts and keys."""
from __future__ import annotations
import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import os
import re
import signal
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SITES = {'deepseek':'DeepSeek','doubao':'豆包','yuanbao':'元宝','kimi':'Kimi','qianwen':'千问','wenxinyiyan':'文心'}
LABELS = {'platform_busy':'网页繁忙或尚未生成回答','site_timeout':'网页超时','platform_auth_unavailable':'网页登录不可用','platform_risk_control':'需要安全验证','platform_quota':'网页额度不足','sms_timeout':'登录短信超时','api_timeout':'API 超时','api_quota_exhausted':'API 余额或额度不足','api_auth_failed':'API 认证失败','api_risk_control':'API 风控拒绝','api_rate_limited':'API 限流','api_request_failed':'API 调用失败','search_request_failed':'联网搜索失败','search_quota_exhausted':'搜索余额或额度不足','search_not_confirmed':'搜索已开启但未返回来源','search_risk_control':'搜索风控拒绝','search_no_sources':'搜索未返回来源','search_not_configured':'搜索未配置','search_auth_failed':'搜索认证失败','search_rate_limited':'搜索限流','callback_failed':'结果回调失败','job_timeout':'请求总预算超时','service_down':'业务服务不可达','failure_rate':'近五分钟失败率过高','share_failed':'分享链接生成失败','test':'飞书接入验证'}
LABELS.update({'cli_result_unconfirmed':'CLI 任务结果未确认','cli_unavailable':'CLI 程序不可用','cli_start_failed':'CLI 启动失败','cli_process_failed':'CLI 进程异常退出','cli_task_stopped':'CLI 任务停止','cli_timeout':'CLI 执行超时','cli_no_output':'CLI 无输出超时','no_progress':'页面连续无进展','target_missing':'原页面已丢失'})
LABELS.update({'cli_disabled':'CLI 恢复未启用','cli_unsupported_site':'平台尚未接入 CLI 恢复','cli_context_missing':'缺少原网页接管上下文','cli_canceled':'CLI 恢复已取消','cli_busy':'该账号已有 CLI 恢复任务','cli_instructions_unavailable':'CLI 恢复指令不可用','cli_verification_failed':'页面复核未通过','cli_resume_failed':'页面已恢复，网页续跑失败'})
LABELS.update({'cli_account_wait_timeout':'等待账号 CLI 恢复任务超时，未启动本次 CLI'})
LABELS.update({'cli_billing_required':'Cursor 账单未支付，已暂停辅助','cli_auth_failed':'Cursor 认证失效','cli_quota_exhausted':'Cursor 额度不足','cli_rate_limited':'Cursor 限流冷却中','cli_transport_failed':'Cursor 上游连接失败','cli_provider_probing':'Cursor 正在复查可用性','cli_health_unavailable':'Cursor 健康状态不可用','recovery_unstable':'验证层消失后状态尚未稳定'})
LABELS['cli_page_cooldown']='当前验证冷却中，等待页面变化'
LABELS.update({'cli_resume_interrupted':'页面已恢复，网页续读因服务重启中断','service_interrupted':'服务重启导致执行中断'})
LABELS.update({'recovery_state_missing':'缺少原任务恢复记录','recovery_state_unavailable':'无法保存原任务恢复记录','recovery_account_busy':'原网页正在被其他操作占用','recovery_context_unconfirmed':'无法确认原网页上下文','recovery_probe_timeout':'原网页复核超时','recovery_probe_failed':'原网页复核执行失败','recovery_page_missing':'原网页标签已丢失','recovery_unconfirmed':'原问题或网页恢复状态未确认','web_resume_failed':'原网页续跑未取得回答','request_cancelled':'请求已取消'})
LABELS.update({'insufficient_web_budget':'本地网页预算不足，未开始调用','browser_admission_timeout':'本地浏览器调度等待超时，未开始调用','session_setup_timeout':'本地浏览器会话准备超时，未开始调用','api_admission_timeout':'本地 API 调度等待超时，未开始调用','insufficient_api_budget':'本地 API 预算不足，未开始调用','request_deadline_exceeded':'本地请求总预算已用完','generation_timeout':'回答生成超时','answer_extract_timeout':'正文提取超时','share_timeout':'分享超时'})
LABELS.update({'browser_start_failed':'本地浏览器进程启动或连接失败，未开始调用','browser_unresponsive':'浏览器页面持续无进展'})
LABELS.update({'session_setup_failed':'本地网页准备失败，未开始调用','empty_answer':'尚未取得有效回答','api_disabled':'API 兜底未启用','api_not_configured':'API 兜底尚未配置','operator_required':'等待人工处理','target_missing':'原网页标签已丢失'})
LABELS.update({'send_timeout':'发送阶段超时，已保留原页面等待核实','recovery_target_missing':'原账号的网页目标已丢失'})
LABELS.update({'share_generation_failed':'分享链接生成失败'})
LABELS.update({'share_not_attempted':'回答已保留，分享未执行','answer_extract_failed':'回答已保留，补充提取失败','postprocess_failed':'回答已保留，后续处理失败'})
LOCAL_SCHEDULING_CODES=frozenset(('insufficient_web_budget','browser_admission_timeout','session_setup_timeout','session_setup_failed','browser_start_failed','api_admission_timeout','insufficient_api_budget','request_deadline_exceeded'))
STAGE_TIMING_KEYS=frozenset(('pool_wait_ms','site_gate_wait_ms','cloud_slot_wait_ms','web_slot_wait_ms','session_acquire_ms','session_setup_ms','web_execution_ms','api_wait_ms','api_execution_ms','cli_wait_ms','cli_preflight_ms','cli_execution_ms','cli_first_output_ms','cli_first_tool_ms','cli_verify_ms','cli_resume_ms'))
RECOVERY_STATUSES=frozenset(('succeeded','failed','cancelled','skipped'))
RECOVERY_PHASES=frozenset(('waiting_cli','waiting_sms','verifying','resuming','succeeded','failed'))
CURSOR_PHASES=frozenset(('running','candidate','verify','resume','completed'))
DELIVERY_RETRY_STATES=frozenset(('retry_wait','operator_wait','awaiting_confirmation'))
TZ = dt.timezone(dt.timedelta(hours=8))

def stamp(value):
    # Go writes RFC3339Nano; Python 3.10 only accepts up to six fraction digits.
    value=re.sub(r'\.(\d+)',lambda m:'.'+m.group(1)[:6].ljust(6,'0'),str(value).replace('Z','+00:00'))
    try: return dt.datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError): return 0

def display_time(value): return dt.datetime.fromtimestamp(value,TZ).strftime('%m-%d %H:%M:%S')

def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+'.tmp')
    fd = os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f: json.dump(data,f,ensure_ascii=False)
    os.chmod(temp,0o600)
    os.replace(temp,path)

def notify_watchdog():
    """Optional systemd heartbeat; never let notification delay a monitor tick."""
    address=os.environ.get('NOTIFY_SOCKET','')
    if not address:return False
    if address.startswith('@'):address='\0'+address[1:]
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as notifier:
            notifier.setblocking(False)
            notifier.sendto(b'WATCHDOG=1',address)
        return True
    except (OSError,ValueError):return False

def tick_delay(started,now=None,interval=10):
    """Start-to-start cadence; wall-clock adjustments cannot extend the wait."""
    return max(0,started+interval-(time.monotonic() if now is None else now))

def safe_code(value):
    value = str(value or '')
    return value if re.fullmatch(r'[a-z][a-z0-9_]{0,63}',value) else 'unknown'

def percentile(values, fraction):
    if not values:return 0
    values=sorted(values)
    return values[max(0,math.ceil(len(values)*fraction)-1)]

def nonnegative_int(value):
    try:return max(0,int(value or 0))
    except (ValueError,TypeError,OverflowError):return 0

def account_id(value):
    value=str(value or '')
    return value if re.fullmatch(r'acct_[a-zA-Z0-9_-]{1,59}',value) else ''

def account_label(identity,site,channel,accounts):
    if channel=='api':return SITES[site]+' API'
    if not identity:return '历史账号未知'
    return accounts.get(identity,identity)

def error_label(code):return LABELS.get(code,'未知错误' if code else '')

def sanitize_stage_timings(raw):
    if not isinstance(raw,dict):return {}
    result={}
    for key,value in raw.items():
        if key not in STAGE_TIMING_KEYS or isinstance(value,bool) or not isinstance(value,(int,float)):continue
        try:
            if math.isfinite(value) and value>=0:result[key]=value
        except (OverflowError,ValueError):pass
    return result

def local_skip_code(result,channel):
    # Only explicit skip evidence changes attribution; keep historical real
    # attempt failures intact even if they carry a local-looking error code.
    for attempt in result.get('attempts',[]):
        if attempt.get('channel')==channel and attempt.get('skipped') and attempt.get('error_code') in LOCAL_SCHEDULING_CODES:return attempt['error_code']
    code=(result.get('web_error_code') or result.get('error_code')) if channel=='web' else result.get('api_error_code')
    return code if channel=='web' and result.get('web_skipped') and code in LOCAL_SCHEDULING_CODES else ''

def only_local_skips(result):
    attempts=[a for a in result.get('attempts',[]) if a.get('channel') in ('web','api')]
    return bool(attempts) and all(a.get('skipped') for a in attempts) and any(local_skip_code(result,c) for c in ('web','api'))

def event_stamp(value):
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        try:return float(value) if math.isfinite(value) and value>=0 else 0
        except (OverflowError,ValueError):return 0
    return stamp(value)

def recovery_identifier(value):
    return value if isinstance(value,str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,160}',value) else ''

def sanitize_web_recovery(raw):
    if not isinstance(raw,dict) or raw.get('status') not in RECOVERY_STATUSES:return None
    number=raw.get('attempts')
    attempts=nonnegative_int(number) if isinstance(number,(int,float)) and not isinstance(number,bool) else 0
    elapsed=raw.get('elapsed_ms')
    return {'status':raw['status'],'attempts':attempts,'cli_call_id':recovery_identifier(raw.get('cli_call_id')),
        'error_code':safe_code(raw.get('error_code')) if raw.get('error_code') else '',
        'verified':raw.get('verified') if isinstance(raw.get('verified'),bool) else None,
        'resumed':raw.get('resumed') if isinstance(raw.get('resumed'),bool) else None,
        'cli_assisted':raw.get('cli_assisted') if isinstance(raw.get('cli_assisted'),bool) else None,
        'elapsed_ms':nonnegative_int(elapsed) if isinstance(elapsed,(int,float)) and not isinstance(elapsed,bool) else 0}

def sanitize_recovery_states(raw,accounts):
    if not isinstance(raw,dict):return {}
    states={}
    for key,value in raw.items():
        site='wenxinyiyan' if key=='yiyan' else key
        if site not in SITES or not isinstance(value,dict) or value.get('phase') not in RECOVERY_PHASES:continue
        identity=account_id(value.get('account_id'))
        states[site]={'phase':value['phase'],'account_id':identity,'account_label':account_label(identity,site,'web',accounts),
            'cli_call_id':recovery_identifier(value.get('cli_call_id')),'error_code':safe_code(value.get('error_code')) if value.get('error_code') else '',
            'updated_at':event_stamp(value.get('updated_at'))}
    return states

def sanitize_cursor_call(raw,accounts=None,now=None,inferred=False):
    site='wenxinyiyan' if raw.get('site')=='yiyan' else raw.get('site')
    if site not in SITES:return None
    started=event_stamp(raw.get('started_at'))
    if not started:return None
    identity=account_id(raw.get('account_id'));job=str(raw.get('job_id') or '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',job):job=''
    call_id=str(raw.get('id') or '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,160}',call_id):return None
    status=raw.get('status')
    if status not in ('running','success','failed','unknown','skipped','waiting_sms','action_required'):status='success' if raw.get('ok') is True else ('failed' if raw.get('ok') is False else 'unknown')
    finished=event_stamp(raw.get('finished_at'));finished=finished if finished>=started else 0
    code=safe_code(raw.get('error_code')) if raw.get('error_code') else ''
    if status=='running' and now is not None and now-started>540:status='unknown';code='cli_result_unconfirmed'
    if status=='unknown' and not code:code='cli_result_unconfirmed'
    verified=raw.get('verified') if isinstance(raw.get('verified'),bool) else None
    resume_ok=raw.get('resume_ok') if isinstance(raw.get('resume_ok'),bool) else None
    task_type=raw.get('task_type') if raw.get('task_type') in ('captcha_recovery','web_recovery','manual_page_assist','login_check_assist') else 'captcha_recovery'
    phase=raw.get('phase') if raw.get('phase') in CURSOR_PHASES else ''
    if task_type in ('web_recovery','manual_page_assist','login_check_assist') and status=='success' and verified is not True:status='unknown';code='cli_result_unconfirmed'
    if status=='success' and resume_ok is not False:code=''
    call={'id':call_id,'job_id':job,'site':site,'channel':'cursor','account_id':identity,'account_label':account_label(identity,site,'cursor',accounts or {}),'started_at':started,'finished_at':finished,'elapsed_ms':nonnegative_int(raw.get('elapsed_ms')),'ok':True if status=='success' else (False if status=='failed' else None),'status':status,'skipped':status=='skipped','skip_reason':code if status=='skipped' else '','error_code':code,'error_label':error_label(code),'task_type':task_type,'phase':phase,'verified':verified,'resume_ok':resume_ok,'inferred':inferred or raw.get('inferred') is True}
    for key in ('process_status','process_error_code','recovery_origin','resume_error_code','build_id','config_hash','model'):
        value=raw.get(key)
        if isinstance(value,str) and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,100}',value):call[key]=value
    call['cli_assisted']=raw.get('cli_assisted') if isinstance(raw.get('cli_assisted'),bool) else None
    call['tool_calls']=min(10000,nonnegative_int(raw.get('tool_calls')))
    timings=raw.get('stage_timings')
    if isinstance(timings,dict):call['stage_timings']={k:min(86400000,nonnegative_int(v)) for k,v in timings.items() if k in STAGE_TIMING_KEYS and isinstance(v,(int,float)) and math.isfinite(v) and v>=0}
    if call.get('process_status')=='failed' and status=='success':
        call.update(status='failed',ok=False,error_code=call.get('process_error_code') or 'cli_process_failed',cli_assisted=False)
        call['error_label']=error_label(call['error_code'])
    return call

def cursor_counts():return {**stage_counts(),'running':0,'waiting_sms':0,'action_required':0,'provider_blocked':0,'verified_success':0,'resumed_success':0,'attributed_success':0,'by_build':{},'by_task':{},'coverage':'structured_and_server_logs','coverage_note':'仅统计结构化记录与服务日志中的自动调用；历史手工调用未记录，无法计数。'}

def add_cursor_summary(summary,calls,start,now):
    counts=cursor_counts();site_counts={site:cursor_counts() for site in SITES};site_latencies={site:[] for site in SITES};accounts={};latencies=[]
    for call in calls or []:
        if not start<=call['started_at']<=now:continue
        site=call['site'];key=(call['account_id'],site)
        if key not in accounts:accounts[key]={'account_id':call['account_id'],'account_label':call['account_label'],'site':site,'channel':'cursor',**cursor_counts(),'last_error_code':'','last_error_label':'','last_at':0,'_latencies':[],'_error_at':0}
        account=accounts[key];status=call['status']
        for target in (counts,site_counts[site],account):
            target[status]+=1
            if status!='skipped':target['total']+=1
            if call.get('verified') is True:target['verified_success']+=1
            if call.get('resume_ok') is True:target['resumed_success']+=1
            if call.get('cli_assisted') is True and call.get('resume_ok') is True:target['attributed_success']+=1
            if call.get('error_code') in ('cli_billing_required','cli_auth_failed','cli_quota_exhausted','cli_rate_limited','cli_transport_failed','cli_provider_probing'):target['provider_blocked']+=1
            for field,key in [('by_build','build_id'),('by_task','task_type')]:
                label=call.get(key) or 'historical_unknown'
                bucket=target[field].setdefault(label,{'total':0,'success':0,'failed':0,'waiting':0,'skipped':0})
                bucket['total']+=1
                outcome=status if status in ('success','failed','skipped') else 'waiting'
                bucket[outcome]+=1
        account['last_at']=max(account['last_at'],call['finished_at'] or call['started_at'])
        if status in ('failed','unknown') and call['error_code'] and call['started_at']>=account['_error_at']:
            account.update(last_error_code=call['error_code'],last_error_label=call['error_label'],_error_at=call['started_at'])
        if status in ('success','failed') and call['elapsed_ms']>0:
            latencies.append(call['elapsed_ms']);site_latencies[site].append(call['elapsed_ms']);account['_latencies'].append(call['elapsed_ms'])
    def finish(target,values=None):
        known=target['success']+target['failed'];target['success_rate']=round(100*target['success']/known,1) if known else None
        if values is not None:target.update(p50_ms=percentile(values,.5),p95_ms=percentile(values,.95),avg_ms=round(sum(values)/len(values),1) if values else 0,latency_samples=len(values))
    finish(counts,latencies);summary['channels']['cursor']=counts
    for site in summary['sites']:
        metrics=site_counts[site['site']];finish(metrics,site_latencies[site['site']]);site.update({'cursor_'+key:value for key,value in metrics.items()})
    for account in accounts.values():
        finish(account,account.pop('_latencies'));account.pop('_error_at');summary['accounts'].append(account)
    summary['accounts'].sort(key=lambda a:(a['failed'],a['last_at']),reverse=True)

def attach_cursor_calls(rows,calls):
    by_job={}
    for call in calls:
        if call['job_id']:by_job.setdefault(call['job_id'],[]).append(call)
    for row in rows:
        related=by_job.get(row['id'],[])
        result_sites={r['site'] for r in row['results']};unmerged=[call for call in related if call['site'] not in result_sites]
        if unmerged:row['cursor_calls']=unmerged
        else:row.pop('cursor_calls',None)
        for result in row['results']:
            result['attempts']=[a for a in result['attempts'] if a['channel']!='cursor']
            result['attempts'].extend(call for call in related if call['site']==result['site'])

def sanitize_attempt(raw,site,accounts,historical=False):
    channel=raw.get('channel')
    if channel not in ('web','api'):return None
    identity='api:'+site if channel=='api' else account_id(raw.get('account_id'))
    skipped=raw.get('skipped') is True
    ok=raw.get('ok') if isinstance(raw.get('ok'),bool) and not skipped else None
    code=safe_code(raw.get('error_code')) if raw.get('error_code') else ''
    started=raw.get('started_at')
    started=started if isinstance(started,(int,float)) and not isinstance(started,bool) and math.isfinite(started) and started>=0 else stamp(started)
    local=skipped and code in LOCAL_SCHEDULING_CODES
    if local:identity=''
    result={'channel':channel,'account_id':identity,'account_label':'本地调度 / 预算（未调用平台）' if local else account_label(identity,site,channel,accounts),'ok':ok,'status':'skipped' if skipped else ('unknown' if ok is None else ('success' if ok else 'failed')),'error_code':code,'error_label':error_label(code),'started_at':started,'elapsed_ms':nonnegative_int(raw.get('elapsed_ms')),'skipped':skipped,'skip_reason':safe_code(raw.get('skip_reason')) if skipped and raw.get('skip_reason') else '','inferred':historical}
    if raw.get('failure_reason'): result['failure_reason'] = safe_code(raw['failure_reason'])
    stage = str(raw.get('failure_stage') or '')
    if re.fullmatch(r'[a-z0-9_]{1,64}', stage): result['failure_stage'] = stage
    diagnostic = str(raw.get('diagnostic_id') or '')
    if re.fullmatch(r'[a-zA-Z0-9_-]{1,160}', diagnostic): result['diagnostic_id'] = diagnostic
    return result

def result_attempts(raw,site,accounts):
    # The new attempt ledger is authoritative, including failed accounts before
    # failover. Never replace those observations with the final delivery source.
    attempts=[]
    if isinstance(raw.get('attempts'),list):
        for entry in raw['attempts']:
            if isinstance(entry,dict):
                attempt=sanitize_attempt(entry,site,accounts)
                if attempt:attempts.append(attempt)
    if attempts:return attempts
    api_source=raw.get('source')=='official_api'
    web_code=raw.get('web_error_code') or (raw.get('error_code') if not api_source and raw.get('ok') is not True else '')
    web_ms=nonnegative_int(raw.get('web_elapsed_ms') if 'web_elapsed_ms' in raw else (raw.get('elapsed_ms') if not api_source else 0))
    skipped=raw.get('web_skipped') is True
    recovery=raw.get('search_recovery_status')
    completed_web=recovery in ('succeeded','failed','not_confirmed')
    web_ok=None
    if raw.get('delivery_pending') is True and raw.get('retry_state')=='awaiting_confirmation' and not web_ms and not raw.get('web_error_code'):
        # Restart/absent-journal metadata is not evidence of an upstream call.
        web_code=''
    if not skipped:
        if completed_web or (not api_source and raw.get('ok') is True and (raw.get('source') or web_ms>0)):web_ok=True
        elif web_code or web_ms>0:web_ok=False
    if web_ok is True:web_code=''
    # Old records retain at most the last browser account. Earlier retry
    # identities and outcomes have no attempt-level evidence and stay unknown.
    for _ in range(min(100,nonnegative_int(raw.get('account_retries')))):
        attempts.append(sanitize_attempt({'channel':'web'},site,accounts,True))
    attempts.append(sanitize_attempt({'channel':'web','account_id':raw.get('account_id'),'ok':web_ok,'error_code':web_code,'elapsed_ms':web_ms,'skipped':skipped,'skip_reason':raw.get('web_skip_reason')},site,accounts,True))
    api_code=raw.get('api_error_code') or (raw.get('search_recovery_error_code') if recovery=='failed' else '') or (raw.get('error_code') if api_source and raw.get('ok') is not True else '')
    direct_api_ms=nonnegative_int(raw.get('api_elapsed_ms') if 'api_elapsed_ms' in raw else (raw.get('elapsed_ms') if api_source else 0))
    api_ms=direct_api_ms or (nonnegative_int(raw.get('search_recovery_elapsed_ms')) if completed_web else 0)
    if api_source or api_code or raw.get('api_fallback_failed') is True or api_ms>0 or completed_web:
        api_skipped=raw.get('api_skipped') is True or (api_code in ('api_admission_timeout','insufficient_api_budget','request_deadline_exceeded') and not direct_api_ms and raw.get('api_fallback_failed') is not True)
        # Recovery can keep the browser answer after a successful API response
        # with no sources. That is a search-quality issue, not an API failure.
        api_ok=True if recovery in ('succeeded','not_confirmed') or (api_source and raw.get('ok') is True) else (False if api_code or raw.get('api_fallback_failed') is True or recovery=='failed' else None)
        if api_ok is True:api_code=''
        attempts.append(sanitize_attempt({'channel':'api','ok':api_ok,'error_code':api_code,'elapsed_ms':api_ms,'skipped':api_skipped,'skip_reason':raw.get('api_skip_reason') or api_code if api_skipped else ''},site,accounts,True))
    return attempts

def sanitize_job(data,accounts=None):
    if not isinstance(data,dict):return None
    accounts=accounts or {}
    results=[]
    for r in data.get('results',[]):
        if not isinstance(r,dict):continue
        site='wenxinyiyan' if r.get('site_id')=='yiyan' else r.get('site_id')
        if site not in SITES:continue
        result={k:r.get(k) is True for k in ['ok','web_skipped','api_fallback_failed']}
        if r.get('delivery_pending') is True and not result['ok']:
            result['delivery_pending']=True
            if r.get('retry_state') in DELIVERY_RETRY_STATES:result['retry_state']=r['retry_state']
        result.update({k:safe_code(r.get(k)) if r.get(k) else '' for k in ['error_code','web_error_code','api_error_code','web_search_error_code','web_search_status','web_search_mode','web_search_provider','share_status','optional_stage_error_code','share_error_code']})
        web_ms=r.get('web_elapsed_ms') if 'web_elapsed_ms' in r else (0 if r.get('source')=='official_api' else r.get('elapsed_ms'))
        result.update({'site':site,'source':'api' if r.get('source')=='official_api' else 'web','citations':len(r.get('citations') or []),'api_ms':nonnegative_int(r.get('api_elapsed_ms')),'web_ms':nonnegative_int(web_ms)})
        result['attempts']=result_attempts(r,site,accounts)
        if r.get('attempts_truncated') is True:
            result.update(attempts_truncated=True,attempts_dropped=nonnegative_int(r.get('attempts_dropped')))
        if r.get('delivery_checkpoint_recovered') is True:result['delivery_checkpoint_recovered']=True
        timings=sanitize_stage_timings(r.get('stage_timings'))
        if timings:result['stage_timings']=timings
        recovery=sanitize_web_recovery(r.get('web_recovery'))
        if recovery is not None:result['web_recovery']=recovery
        identity='api:'+site if result['source']=='api' else account_id(r.get('account_id'))
        local=only_local_skips(result)
        if local:identity=''
        result.update({'account_id':identity,'account_label':'本地调度 / 预算（未调用平台）' if local else account_label(identity,site,result['source'],accounts),'error_label':error_label(result['error_code'])})
        diagnostic=str(r.get('diagnostic_id') or '')
        if re.fullmatch(r'[a-zA-Z0-9_-]{1,160}',diagnostic):result['diagnostic_id']=diagnostic
        results.append(result)
    job_id=str(data.get('job_id',''))
    if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',job_id):return None
    row={'id':job_id,'status':data.get('status') if data.get('status') in ['queued','running','succeeded','partial','failed'] else 'unknown','created':stamp(data.get('created_at')),'updated':stamp(data.get('updated_at')),'finished':stamp(data.get('finished_at')),'deadline':stamp(data.get('deadline_at')),'deadline_policy':'web_first' if data.get('deadline_policy')=='web_first' else '','total_ms':nonnegative_int(data.get('total_ms')),'queued_ms':nonnegative_int(data.get('queued_ms')),'sites':[s for s in data.get('sites',[]) if s in SITES or s=='yiyan'],'results':results,'callback_failed':data.get('callback_status')=='failed','error_code':safe_code(data.get('error_code')) if data.get('error_code') else ''}
    if data.get('status')=='cancelled':row['status']='cancelled'
    if data.get('delivery_policy')=='eventual':
        retry_at=stamp(data.get('retry_at'))
        row.update(delivery_policy='eventual',retry_at=dt.datetime.fromtimestamp(retry_at,dt.timezone.utc).isoformat() if retry_at>0 else '',retry_count=nonnegative_int(data.get('retry_count')),wait_reason=safe_code(data.get('wait_reason')) if data.get('wait_reason') else '')
    states=sanitize_recovery_states(data.get('recovery_states'),accounts)
    if states:row['recovery_states']=states
    return row

def delivery_counts():return {'total':0,'pending':0,'web_success':0,'api_success':0,'web_share':None,'api_fallback_rate':None}

def recovery_counts():return {'attempted':0,'completed':0,'known_completed':0,'pending':0,'cancelled':0,'skipped':0,'unknown':0,'web_success':0,'web_success_rate':None}

def count_web_recovery(counts,result):
    evidence=result.get('web_recovery')
    if not evidence or evidence['attempts']<=0:return
    counts['attempted']+=1
    if evidence['status'] in ('cancelled','skipped'):counts[evidence['status']]+=1
    elif isinstance(evidence.get('resumed'),bool):
        counts['completed']+=1
        if isinstance(evidence.get('cli_assisted'),bool):
            counts['known_completed']+=1
            if evidence['resumed'] and evidence['cli_assisted'] and result['ok'] and result['source']=='web':counts['web_success']+=1
        else:counts['unknown']+=1
    else:counts['unknown']+=1

def finish_delivery_counts(delivery,recovery):
    if delivery['total']:
        delivery['web_share']=round(100*delivery['web_success']/delivery['total'],1)
        delivery['api_fallback_rate']=round(100*delivery['api_success']/delivery['total'],1)
    if recovery['known_completed']:recovery['web_success_rate']=round(100*recovery['web_success']/recovery['known_completed'],1)

def stage_counts():return {'total':0,'success':0,'failed':0,'skipped':0,'unknown':0,'success_rate':None}

def count_stage(counts,attempt):
    if attempt['skipped']:counts['skipped']+=1
    elif attempt['ok'] is None:counts['unknown']+=1
    else:
        counts['total']+=1
        counts['success' if attempt['ok'] else 'failed']+=1

def stage_rate(counts):counts['success_rate']=round(100*counts['success']/counts['total'],1) if counts['total'] else None

def summarize(rows, now, seconds, start_at=None, include_recent=True, cursor_calls=None):
    start=now-seconds if start_at is None else start_at
    selected=[r for r in rows if start<=r['created']<=now]
    counts={s:0 for s in ['queued','running','succeeded','partial','failed','cancelled']}
    sites={s:{'site':s,'name':name,'total':0,'success':0,'failed':0,'pending':0,'web_delivery_success':0,'api_delivery_success':0,'search_used':0,'search_failed':0,'missing_citations':0,'latencies':[],'last_at':0,'last_status':'no_data','delivery':delivery_counts(),'recovery':recovery_counts()} for s,name in SITES.items()}
    delivery=delivery_counts();recovery=recovery_counts()
    channels={channel:stage_counts() for channel in ('web','api')};channel_latencies={c:[] for c in channels};accounts={};site_stages={s:{c:stage_counts() for c in channels} for s in SITES}
    durations=[];recent=[];attempts_dropped=0;step=max(60,max(0,now-start)//12);trend=[{'at':start+i*step,'total':0,'failed':0,'partial':0} for i in range(12)]
    for row in selected:
        if row['status'] in counts:counts[row['status']]+=1
        if row['status'] in ['succeeded','partial','failed']:durations.append(row['total_ms'])
        bucket=min(11,max(0,int((row['created']-start)/step)))
        trend[bucket]['total']+=1
        if row['status'] in ['failed','partial']:trend[bucket][row['status']]+=1
        for r in row['results']:
            attempts_dropped+=r.get('attempts_dropped',0)
            s=sites[r['site']];pending=row['status'] in ('queued','running') and r.get('delivery_pending') is True and not r['ok']
            if pending:s['pending']+=1
            else:s['total']+=1;s['success' if r['ok'] else 'failed']+=1
            if r['ok']:s[r['source']+'_delivery_success']+=1
            for target in (delivery,s['delivery']):
                target['pending' if pending else 'total']+=1
                if r['ok']:target[r['source']+'_success']+=1
            for target in (recovery,s['recovery']):count_web_recovery(target,r)
            for attempt in r.get('attempts',[]):
                if attempt['channel'] not in ('web','api'):continue
                channel=attempt['channel'];count_stage(channels[channel],attempt);count_stage(site_stages[r['site']][channel],attempt)
                if not attempt['skipped'] and isinstance(attempt['ok'],bool) and attempt['elapsed_ms']>0:channel_latencies[channel].append(attempt['elapsed_ms'])
                key=(attempt['account_id'],r['site'],channel)
                if key not in accounts:accounts[key]={'account_id':attempt['account_id'],'account_label':attempt['account_label'],'site':r['site'],'channel':channel,**stage_counts(),'last_error_code':'','last_error_label':'','last_at':0,'last_error_at':0}
                account=accounts[key];count_stage(account,attempt)
                at=attempt['started_at'] or row['updated'] or row['created'];account['last_at']=max(account['last_at'],at)
                if attempt['ok'] is False and at>=account['last_error_at']:
                    account.update(last_error_code=attempt['error_code'] or 'unknown',last_error_label=error_label(attempt['error_code'] or 'unknown'),last_error_at=at)
            if r['web_search_status']=='used':s['search_used']+=1
            if r['web_search_status']=='failed':s['search_failed']+=1
            if r['ok'] and r['citations']==0:s['missing_citations']+=1
            if not pending:s['latencies'].append(r['api_ms']+r['web_ms'])
            if row['updated']>=s['last_at']:
                s['last_at']=row['updated'];s['last_status']='pending' if pending else ('success' if r['ok'] else 'failed')
        if row['status'] in ('queued','running'):
            terminal_sites={r['site'] for r in row['results'] if r.get('web_recovery')}
            for site,state in row.get('recovery_states',{}).items():
                if site in sites and site not in terminal_sites and state['phase'] in ('waiting_cli','waiting_sms','verifying','resuming') and state['cli_call_id']:
                    for target in (recovery,sites[site]['recovery']):target['attempted']+=1;target['pending']+=1
        if include_recent:recent.append(row)
    for s in sites.values():
        s['success_rate']=round(100*s['success']/s['total'],1) if s['total'] else None
        s['p95_ms']=percentile(s.pop('latencies'),.95)
        for channel,stages in site_stages[s['site']].items():
            stage_rate(stages);s.update({channel+'_'+k:v for k,v in stages.items()})
        finish_delivery_counts(s['delivery'],s['recovery'])
    for channel,stages in channels.items():
        stage_rate(stages);latencies=channel_latencies[channel]
        stages.update(p50_ms=percentile(latencies,.5),p95_ms=percentile(latencies,.95),avg_ms=round(sum(latencies)/len(latencies),1) if latencies else 0,latency_samples=len(latencies))
    for account in accounts.values():stage_rate(account);account.pop('last_error_at')
    finished=counts['succeeded']+counts['partial']+counts['failed']
    finish_delivery_counts(delivery,recovery)
    summary={'total':len(selected),'counts':counts,'success_rate':round(100*counts['succeeded']/finished,1) if finished else None,'p50_ms':percentile(durations,.5),'p95_ms':percentile(durations,.95),'channels':channels,'delivery':delivery,'recovery':recovery,'attempts_dropped':attempts_dropped,'accounts':sorted(accounts.values(),key=lambda a:(a['failed'],a['last_at']),reverse=True),'sites':list(sites.values()),'trend':trend}
    if include_recent:summary['recent']=sorted(recent,key=lambda r:r['created'],reverse=True)[:100]
    add_cursor_summary(summary,cursor_calls,start,now)
    return summary

def summarize_days(rows,now,cursor_calls=None):
    # Group once instead of rescanning all historical rows for every date.
    # Summaries hold counters only; all per-request detail lives in the index.
    grouped={}
    for row in rows:
        if not 0<row['created']<=now:continue
        date=dt.datetime.fromtimestamp(row['created'],TZ).strftime('%Y-%m-%d')
        grouped.setdefault(date,[]).append(row)
    calls_by_date={}
    for call in cursor_calls or []:
        if not 0<call['started_at']<=now:continue
        date=dt.datetime.fromtimestamp(call['started_at'],TZ).strftime('%Y-%m-%d');calls_by_date.setdefault(date,[]).append(call)
    days={}
    for date in sorted(set(grouped)|set(calls_by_date),reverse=True):
        start=dt.datetime.strptime(date,'%Y-%m-%d').replace(tzinfo=TZ).timestamp()
        days[date]=summarize(grouped.get(date,[]),min(now,start+86400),86400,start_at=start,include_recent=False,cursor_calls=calls_by_date.get(date,[]))
    return days

def send_feishu(config, text):
    address=config.get('webhook','')
    u=urllib.parse.urlparse(address)
    if u.scheme!='https' or u.hostname!='open.feishu.cn' or not u.path.startswith('/open-apis/bot/v2/hook/') or u.username or u.query:
        raise RuntimeError('飞书地址未配置或格式错误')
    payload={'msg_type':'text','content':{'text':text}}
    if config.get('sign_secret'):
        import base64,hashlib,hmac
        timestamp=str(int(time.time()));key=(timestamp+'\n'+config['sign_secret']).encode()
        payload.update({'timestamp':timestamp,'sign':base64.b64encode(hmac.new(key,b'',hashlib.sha256).digest()).decode()})
    req=urllib.request.Request(address,json.dumps(payload,ensure_ascii=False).encode(),{'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=8) as response:body=json.loads(response.read(32768))
    except urllib.error.HTTPError as exc:raise RuntimeError('飞书 HTTP '+str(exc.code)) from None
    except Exception:raise RuntimeError('飞书连接失败') from None
    code=body.get('code',body.get('StatusCode'))
    if not isinstance(code,int) or isinstance(code,bool) or code!=0:raise RuntimeError('飞书返回错误码 '+str(code))

class Monitor:
    def __init__(self,root,config,sender=send_feishu):
        self.root=Path(root);self.data_dir=Path(os.environ.get('GEO_DATA_DIR') or self.root/'data');self.config=config;self.sender=sender;self.cache={};self.state_path=self.data_dir/'monitor-state.json'
        self.accounts={}
        self.cursor_cache={};self.cursor_log_offset=0;self.cursor_log_identity=None;self.cursor_log_calls={};self.cursor_log_pending={}
        try:self.state=json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.state={'seen':{},'incidents':{},'outbox':[],'initialized':False,'health_failures':0,'delivery':{}}
        for k,default in [('seen',{}),('incidents',{}),('outbox',[]),('delivery',{})]:self.state.setdefault(k,default)
        self.snapshot={}

    def load_rows(self,now):
        # Only masked phone tails are retained. Other account state, cookies,
        # profiles and authentication data never enter either public index.
        accounts={}
        try:
            store=json.loads((self.data_dir/'accounts.json').read_text(encoding='utf-8'))
            for entry in store.get('accounts',[]):
                if not isinstance(entry,dict):continue
                identity=account_id(entry.get('id'));phone=str(entry.get('phone') or '')
                if identity and re.fullmatch(r'\+?[0-9]{7,15}',phone):accounts[identity]='手机尾号 '+phone[-4:]
        except (OSError,ValueError,TypeError,AttributeError):pass
        if accounts!=self.accounts:self.cache.clear();self.accounts=accounts
        paths=set()
        for p in (self.data_dir/'jobs').glob('*.json'):
            paths.add(str(p))
            try:
                mod=p.stat().st_mtime_ns
                if str(p) not in self.cache or self.cache[str(p)][0]!=mod:
                    row=sanitize_job(json.loads(p.read_text(encoding='utf-8')),self.accounts)
                    self.cache[str(p)]=(mod,row)
            except (OSError,ValueError,TypeError):continue
        for p in list(self.cache):
            if p not in paths:del self.cache[p]
        return [r for _,r in self.cache.values() if r and 0<r['created']<=now]

    def load_cursor_calls(self,now):
        # Read only structured allowlisted events and server lifecycle lines.
        # The agent's transcript can include phone numbers and is never opened.
        paths=set()
        for path in (self.data_dir/'cursor-calls').glob('*.json'):
            key=str(path);paths.add(key)
            try:
                mod=path.stat().st_mtime_ns
                if key not in self.cursor_cache or self.cursor_cache[key][0]!=mod:
                    raw=json.loads(path.read_text(encoding='utf-8'))
                    call=sanitize_cursor_call(raw) if isinstance(raw,dict) else None
                    self.cursor_cache[key]=(mod,call)
            except (OSError,ValueError,TypeError):continue
        for key in list(self.cursor_cache):
            if key not in paths:del self.cursor_cache[key]
        log_path=self.root/'geo-server.out.log'
        try:
            info=log_path.stat();identity=(info.st_dev,info.st_ino)
            if identity!=self.cursor_log_identity or info.st_size<self.cursor_log_offset:self.cursor_log_offset=0;self.cursor_log_identity=identity
            with log_path.open('rb') as stream:
                stream.seek(self.cursor_log_offset)
                while True:
                    before=stream.tell();line=stream.readline()
                    if not line:break
                    if not line.endswith(b'\n'):self.cursor_log_offset=before;break
                    self.cursor_log_offset=stream.tell()
                    if b'cursor captcha agent ' not in line:continue
                    text=line.decode('utf-8',errors='replace')
                    match=re.search(r'cursor captcha agent (start|done|exit|timeout) job=([a-zA-Z0-9_-]{1,64}) account=(acct_[a-zA-Z0-9_-]{1,59})(?:\s|:|$)',text)
                    if not match:continue
                    try:at=dt.datetime.strptime(text[:19],'%Y/%m/%d %H:%M:%S').replace(tzinfo=TZ).timestamp()
                    except ValueError:continue
                    action,job,account=match.groups();pair=(job,account)
                    id_match=re.search(r'(?:\s)call=([a-zA-Z0-9_-]{1,160})(?:\s|:|$)',text)
                    explicit_id=id_match.group(1) if id_match else ''
                    if action=='start':
                        call_key=('id',explicit_id) if explicit_id else ('legacy',job,account,int(at));digest=hashlib.sha256((job+'/'+account).encode()).hexdigest()[:12]
                        self.cursor_log_calls[call_key]={'id':explicit_id or 'cursor-log-'+str(int(at))+'-'+digest,'job_id':job,'site':'doubao','account_id':account,'started_at':at,'status':'running','inferred':True,'_explicit_id':bool(explicit_id)}
                        self.cursor_log_pending[pair]=call_key
                    else:
                        call_key=('id',explicit_id) if explicit_id else self.cursor_log_pending.get(pair)
                        if call_key not in self.cursor_log_calls:continue
                        call=self.cursor_log_calls[call_key]
                        if at<call['started_at']:continue
                        call.update(finished_at=at,elapsed_ms=int((at-call['started_at'])*1000),status='unknown' if action=='done' else 'failed',error_code={'done':'cli_result_unconfirmed','exit':'cli_process_failed','timeout':'cli_timeout'}[action])
                        self.cursor_log_pending.pop(pair,None)
        except OSError:pass
        merged={};structured_by_pair={}
        # Keep distinct structured IDs even if they start in the same second.
        for _,raw in self.cursor_cache.values():
            if not raw:continue
            call=sanitize_cursor_call(raw,self.accounts,now)
            if call and call['started_at']<=now:
                merged[call['id']]=call
                structured_by_pair.setdefault((call['job_id'],call['account_id'],call['site']),[]).append(call)
        for raw in self.cursor_log_calls.values():
            call=sanitize_cursor_call(raw,self.accounts,now,inferred=True)
            if not call or call['started_at']>now or call['id'] in merged:continue
            if not raw.get('_explicit_id'):
                candidates=structured_by_pair.get((call['job_id'],call['account_id'],call['site']),[])
                if any(abs(candidate['started_at']-call['started_at'])<=5 for candidate in candidates):continue
            merged[call['id']]=call
        return sorted(merged.values(),key=lambda call:call['started_at'],reverse=True)

    def issue(self,site,channel,code,job,now,severity='critical',count=None):
        key=site+'/'+channel+'/'+code
        item=self.state['incidents'].get(key)
        if not item:item={'key':key,'site':site,'channel':channel,'code':code,'severity':severity,'first_at':now,'last_sent':0,'count':0,'active':False};self.state['incidents'][key]=item
        was_active=item['active']
        escalated=item.get('severity')!='critical' and severity=='critical' and was_active
        for previous in self.state['incidents'].values():
            if previous['key']!=key and previous['active'] and previous['site']==site and previous['channel']==channel:
                was_active=True
                escalated=escalated or (previous.get('severity')!='critical' and severity=='critical')
                item['last_sent']=max(item['last_sent'],previous.get('last_sent',0))
                self.retire(previous,'superseded_by_current_reason',now)
        item.update({'active':True,'severity':severity,'last_at':now,'job':job,'count':count if count is not None else item['count']+1})
        interval=self.config.get('dedup_seconds',900) if severity=='critical' else self.config.get('warning_dedup_seconds',3600)
        if not any(e['key']==key and e['kind']=='problem' and e['status']=='pending' for e in self.state['outbox']) and (not was_active or escalated or not item['last_sent'] or now-item['last_sent']>=interval):
            self.enqueue(item,'problem',now)

    def retire(self,item,reason,now):
        # A superseded or stale diagnosis is not a verified recovery.
        item.update(active=False,retired_at=now,retired_reason=reason)
        for event in self.state['outbox']:
            if event['key']==item['key'] and event['status']=='pending':event['status']='superseded'

    def retire_channels(self,site,channels,reason,now):
        for item in self.state['incidents'].values():
            if item['active'] and item['site']==site and item['channel'] in channels:self.retire(item,reason,now)

    def recover(self,site,channel,now):
        for item in self.state['incidents'].values():
            if item['active'] and item['site']==site and item['channel']==channel:
                item['active']=False;item['last_at']=now
                for e in self.state['outbox']:
                    if e['key']==item['key'] and e['status']=='pending' and e['kind']=='problem':e['status']='superseded'
                if item['last_sent']:self.enqueue(item,'recovery',now)

    def enqueue(self,item,kind,now):
        self.state['outbox'].append({k:item.get(k) for k in ['key','site','channel','code','severity','job','count'] }|{'kind':kind,'at':now,'status':'pending','attempts':0,'next_at':now})

    def result_event(self,row,r,now):
        site=r['site'];job=row['id']
        local_web=local_skip_code(r,'web');local_api=local_skip_code(r,'api')
        if r['ok']:
            self.recover(site,'request',now)
            self.recover(site,r['source'],now)
            if not local_web and not local_api:self.recover(site,'local',now)
        elif r['api_error_code'] and not local_api:
            self.issue(site,'api',r['api_error_code'],job,now)
            self.retire_channels(site,('web','search','request','rate'),'covered_by_api_failure',now)
            return
        elif not r.get('delivery_pending') and not only_local_skips(r):self.issue(site,'request',r['error_code'] or 'unknown',job,now)
        for code in dict.fromkeys(c for c in (local_web,local_api) if c):self.issue(site,'local',code,job,now,'warning')
        web=r['web_error_code'] or (next((a.get('error_code') for a in reversed(r.get('attempts',[])) if a.get('channel')=='web' and a.get('ok') is False and not a.get('skipped')),'') if r.get('delivery_pending') else (r['error_code'] if not r['ok'] else ''))
        if web and not local_web:self.issue(site,'web',web,job,now,'warning' if r['ok'] else 'critical')
        if r['ok'] and r['source']=='api' and r['web_search_status']=='not_confirmed' and r['citations']==0:self.issue(site,'search','search_not_confirmed',job,now,'warning')
        elif r['web_search_status']=='failed':self.issue(site,'search',r['web_search_error_code'] or 'search_request_failed',job,now,'warning')
        elif r['web_search_status']=='used':self.recover(site,'search',now)
        quality=r.get('optional_stage_error_code') or r.get('share_error_code') or ('share_failed' if r['source']=='web' and r['share_status'] in ['generation_failed','verification_failed'] else '')
        if quality and (r['ok'] or r['source']=='web' and r['share_status'] in ['generation_failed','verification_failed']):self.issue(site,'quality',quality,job,now,'warning')
        elif r['share_status']=='verified':self.recover(site,'quality',now)

    def process(self,rows,now):
        first=not self.state.get('initialized')
        for row in sorted(rows,key=lambda r:r['updated']):
            for r in row['results']:
                key=row['id']+'/'+r['site']
                if r.get('delivery_pending'):
                    # A persistent job can publish several attempt ledgers and
                    # later deliver successfully. Do not let its first pending
                    # observation consume the final result's dedup identity.
                    evidence=[(a.get('channel'),a.get('account_id'),a.get('started_at'),a.get('ok'),a.get('error_code'),a.get('skipped')) for a in r.get('attempts',[])]
                    key+='/pending/'+hashlib.sha256(json.dumps([evidence,r.get('error_code'),r.get('api_error_code')],sort_keys=True).encode()).hexdigest()[:16]
                if key not in self.state['seen']:
                    self.state['seen'][key]=now
                    if not first:self.result_event(row,r,now)
            if row['callback_failed']:
                key=row['id']+'/callback'
                if key not in self.state['seen']:
                    self.state['seen'][key]=now
                    if not first:self.issue('service','callback','callback_failed',row['id'],now,'warning')
            if row.get('deadline_policy')!='web_first' and row['status'] in ['queued','running'] and row['deadline'] and now>row['deadline']+15:self.issue('service','request','job_timeout',row['id'],now)
        self.state['initialized']=True
        if first:
            latest={}
            for row in sorted(rows,key=lambda r:r['updated']):
                if row['updated']>=now-300:
                    for r in row['results']:latest[r['site']]=(row,r)
            for row,r in latest.values():self.result_event(row,r,now)
        # A request rejected before either platform call is a local capacity
        # observation, not evidence for a platform failure-rate incident.
        rates=summarize([{**r,'results':[result for result in r['results'] if not only_local_skips(result)]} for r in rows],now,300)
        for s in rates['sites']:
            root_api=any(i['active'] and i['site']==s['site'] and i['channel']=='api' and i['severity']=='critical' for i in self.state['incidents'].values())
            if root_api:self.retire_channels(s['site'],('rate',),'covered_by_api_failure',now)
            elif s['total']>=5 and s['failed']/s['total']>=.4:self.issue(s['site'],'rate','failure_rate','',now,count=s['failed'])
            elif s['total']>=5 and s['failed']/s['total']<.2:self.recover(s['site'],'rate',now)
        self.reconcile(rows,now)
        self.state['seen']={k:v for k,v in self.state['seen'].items() if v>=now-172800}

    def reconcile(self,rows,now):
        # Reconcile persisted pre-fix incidents against real channel
        # observations, including results already seen before a restart.
        observations={};latest={}
        estimates={r['id'] for r in rows if r.get('deadline_policy')=='web_first'}
        pending_deliveries={(row['id'],result['site']) for row in rows for result in row['results'] if result.get('delivery_pending') and not result['ok']}
        for item in self.state['incidents'].values():
            if item['active'] and item['site']=='service' and item['channel']=='request' and item['code']=='job_timeout' and item.get('job') in estimates:self.retire(item,'deadline_is_estimate',now)
            elif item['active'] and item['channel']=='request' and (item.get('job'),item['site']) in pending_deliveries:self.retire(item,'delivery_pending',now)
        for row in sorted(rows,key=lambda r:r['updated']):
            for r in row['results']:
                latest[r['site']]=(row,r)
                if r.get('api_error_code') or (r['ok'] and r['source']=='api'):observations[(r['site'],'api')]=(row,r)
                if not r.get('web_skipped'):observations[(r['site'],'web')]=(row,r)
                if r.get('web_search_status')=='used':observations[(r['site'],'search')]=(row,r)
                elif r.get('web_search_status') in ('failed','not_confirmed'):observations.pop((r['site'],'search'),None)
        for (site,channel),(row,r) in observations.items():
            if row['updated']<now-21600:continue
            recovered=(channel=='api' and r['ok'] and r['source']=='api') or (channel=='web' and r['ok'] and r['source']=='web') or channel=='search'
            if recovered:
                for item in self.state['incidents'].values():
                    if item['active'] and item['site']==site and item['channel']==channel and item.get('last_at',0)<=row['updated']:self.recover(site,channel,now);break
        for site,(row,r) in latest.items():
            if not r['ok'] and r.get('api_error_code') and not local_skip_code(r,'api'):self.retire_channels(site,('web','search','request','rate'),'covered_by_api_failure',now)
        # Keep the latest reason per channel; preserve retired evidence.
        active=sorted([i for i in self.state['incidents'].values() if i['active']],key=lambda i:i.get('last_at',0),reverse=True)
        seen=set()
        for item in active:
            channel=(item['site'],item['channel'])
            if now-item.get('last_at',0)>self.config.get('incident_stale_seconds',21600):self.retire(item,'stale_without_recent_observation',now)
            elif channel in seen:self.retire(item,'superseded_by_current_reason',now)
            else:seen.add(channel)

    def health(self,now):
        ok=False
        try:
            with urllib.request.urlopen(self.config.get('health_url','http://127.0.0.1:8080/admin'),timeout=3) as r:ok=r.status==200
        except Exception:pass
        self.state['health_failures']=0 if ok else self.state.get('health_failures',0)+1
        if ok:self.recover('service','health',now)
        elif self.state['health_failures']>=3:self.issue('service','health','service_down','',now)
        return ok

    def deliver(self,now):
        pending=[e for e in self.state['outbox'] if e['status']=='pending' and e['next_at']<=now][:8]
        if not pending or not self.config.get('enabled',True) or not self.config.get('webhook'):return
        text=['GEO 请求监控告警']
        for e in pending:
            heading='恢复' if e['kind']=='recovery' else ('告警' if e['severity']=='critical' else '提醒')
            context='；API 已兜底返回回答' if e['kind']=='problem' and e['channel']=='web' and e['severity']=='warning' else ''
            text.append(f"【{heading}】{SITES.get(e['site'],'业务服务')} · {LABELS.get(e['code'],e['code'])}{context}\n时间：{display_time(e['at'])}；累计：{e['count']} 次"+(f"；任务：{e['job']}" if e['job'] else ''))
        text.append('看板：'+self.config.get('dashboard_url',''))
        atomic_json(self.state_path,self.state)
        try:
            self.sender(self.config,'\n\n'.join(text))
            for e in pending:
                e['status']='sent';e['sent_at']=now
                if e['kind']=='problem' and e['key'] in self.state['incidents']:self.state['incidents'][e['key']]['last_sent']=now
            self.state['delivery']={'at':now,'status':'sent','error':''}
        except Exception as err:
            for e in pending:e['attempts']+=1;e['next_at']=now+min(600,10*(2**min(e['attempts'],6)))
            self.state['delivery']={'at':now,'status':'failed','error':str(err) if isinstance(err,RuntimeError) else '推送连接失败'}
        sent=[e for e in self.state['outbox'] if e['status']!='pending'][-200:]
        self.state['outbox']=sent+[e for e in self.state['outbox'] if e['status']=='pending']

    def tick(self,now=None,health_override=None):
        started=time.monotonic()
        now=time.time() if now is None else now
        rows=self.load_rows(now);cursor_calls=self.load_cursor_calls(now)
        # Historical drilldown must not replay old incidents after deployment
        # or restart. Preserve the monitor's existing 24-hour alert input.
        alert_rows=[r for r in rows if r['created']>=now-86400 or r['status'] in ['queued','running']]
        self.process(alert_rows,now)
        healthy=self.health(now) if health_override is None else health_override
        self.deliver(now)
        self.snapshot={'updated_at':now,'service_healthy':healthy,'alerts_enabled':bool(self.config.get('enabled',True) and self.config.get('webhook')),'dedup_seconds':self.config.get('dedup_seconds',900),'delivery':self.state['delivery'],'active_alerts':sorted([v for v in self.state['incidents'].values() if v['active']],key=lambda x:x['last_at'],reverse=True),'notifications':self.state['outbox'][-40:],'windows':{k:summarize(rows,now,v,cursor_calls=cursor_calls) for k,v in [('15m',900),('1h',3600),('6h',21600),('24h',86400)]},'labels':LABELS}
        queue_counts=collections.Counter(row['status'] for row in rows)
        self.snapshot['queue_counts']={status:queue_counts[status] for status in ('queued','running')}
        midnight=dt.datetime.fromtimestamp(now,TZ).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        self.snapshot['windows']['today']=summarize(rows,now,now-midnight,start_at=midnight,cursor_calls=cursor_calls)
        self.snapshot['windows']['afternoon']=summarize(rows,now,max(0,now-midnight-43200),start_at=midnight+43200,cursor_calls=cursor_calls)
        self.snapshot['warning_dedup_seconds']=self.config.get('warning_dedup_seconds',3600)
        days=summarize_days(rows,now,cursor_calls);available_dates=list(days)
        self.snapshot['available_dates']=available_dates
        self.snapshot['daily']=[{'date':date,**{k:day[k] for k in ('total','success_rate','p50_ms','p95_ms','channels','delivery','recovery')}} for date,day in days.items()]
        atomic_json(self.state_path,self.state)
        attach_cursor_calls(rows,cursor_calls)
        atomic_json(self.data_dir/'monitor-requests.json',{'updated_at':now,'rows':sorted(rows,key=lambda r:r['created'],reverse=True),'cursor_calls':cursor_calls})
        atomic_json(self.data_dir/'monitor-daily.json',{'updated_at':now,'timezone':'Asia/Shanghai','available_dates':available_dates,'days':days})
        self.snapshot.update(generated_at=time.time(),tick_ms=nonnegative_int((time.monotonic()-started)*1000))
        atomic_json(self.data_dir/'monitor-dashboard.json',self.snapshot)
        # A blocked health check, sender or failed publication must not renew
        # the watchdog. systemd restarts us if progress stops for 45 seconds.
        notify_watchdog()
        return self.snapshot

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);ap.add_argument('--once',action='store_true');ap.add_argument('--test',action='store_true');args=ap.parse_args()
    config=json.loads((args.root/'.monitoring.json').read_text(encoding='utf-8'))
    if args.test:
        send_feishu(config,'GEO 请求监控告警：接入验证成功。请求失败、额度耗尽、超时、搜索失败和服务不可达将自动推送；同类异常15分钟内去重，恢复后通知。\n看板：'+config.get('dashboard_url',''))
        monitor=Monitor(args.root,config);now=time.time()
        monitor.state['delivery']={'at':now,'status':'sent','error':''}
        monitor.state['outbox'].append({'key':'service/test/test','site':'service','channel':'test','code':'test','severity':'info','job':'','count':1,'kind':'test','at':now,'status':'sent','sent_at':now,'attempts':1,'next_at':now})
        atomic_json(monitor.state_path,monitor.state)
        print('Feishu delivery accepted (code=0)',flush=True);return
    monitor=Monitor(args.root,config);running=True
    def stop(*_):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    notify_watchdog()
    while running:
        started=time.monotonic()
        try:monitor.tick()
        except Exception as exc:print('monitor tick failed:',type(exc).__name__,flush=True)
        if args.once:break
        while running:
            delay=tick_delay(started)
            if delay<=0:break
            time.sleep(min(1,delay))

if __name__=='__main__':main()
