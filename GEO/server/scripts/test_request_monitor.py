import importlib.util
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('request_monitor',Path(__file__).with_name('request_monitor.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class MonitorTests(unittest.TestCase):
    def test_attempt_failure_details_are_sanitized_and_preserved(self):
        row=m.sanitize_attempt({'channel':'web','ok':False,'error_code':'site_request_failed',
            'failure_reason':'browser_page_crashed','failure_stage':'02_wait','diagnostic_id':'job-kimi-123',
            'stderr':'private text','phone':'13900001234'},'kimi',{})
        self.assertEqual(row['failure_reason'],'browser_page_crashed')
        self.assertEqual(row['failure_stage'],'02_wait')
        self.assertEqual(row['diagnostic_id'],'job-kimi-123')
        self.assertNotIn('private text',json.dumps(row))
        self.assertNotIn('13900001234',json.dumps(row))
    def test_cli_provider_failure_cannot_become_a_page_success(self):
        base={'id':'billing','job_id':'job','site':'doubao','started_at':m.dt.datetime.fromtimestamp(self.now,m.TZ).isoformat(),'task_type':'web_recovery','status':'success','verified':True,'resume_ok':True,'process_status':'failed','process_error_code':'cli_billing_required','cli_assisted':False,'build_id':'recovery-v2'}
        failed=m.sanitize_cursor_call(base)
        self.assertEqual((failed['status'],failed['ok'],failed['error_code']),('failed',False,'cli_billing_required'))
        waiting=m.sanitize_cursor_call({**base,'id':'sms','status':'waiting_sms','process_status':'waiting_sms','process_error_code':'sms_required','error_code':'sms_required','verified':False,'resume_ok':None})
        view=m.summarize([],self.now,3600,cursor_calls=[failed,waiting])['channels']['cursor']
        self.assertEqual((view['success'],view['failed'],view['waiting_sms'],view['success_rate']),(0,1,1,0))
        self.assertEqual((view['provider_blocked'],view['verified_success'],view['attributed_success']),(1,1,0))
        self.assertEqual(view['by_build']['recovery-v2']['waiting'],1)

    def test_short_go_fractions_and_cli_timing_allowlist(self):
        self.assertEqual(m.stamp('2026-10-18T16:37:51.92687Z'),m.stamp('2026-10-18T16:37:51.926870Z'))
        c=m.sanitize_cursor_call({'id':'timing','job_id':'j','site':'kimi','started_at':'2026-10-18T16:37:51.92687Z','stage_timings':{'cli_wait_ms':12,'cli_resume_ms':5,'private_secret':123,'cli_verify_ms':float('nan')}})
        self.assertEqual(c['stage_timings'],{'cli_wait_ms':12,'cli_resume_ms':5})

    def test_cancelled_requests_are_terminal_and_not_pending(self):
        self.job('cancelled',[{'site_id':'yuanbao','ok':False,'error_code':'request_cancelled','delivery_pending':True}],status='cancelled')
        view=self.engine.tick(self.now,True)['windows']['1h']
        self.assertEqual(view['counts']['cancelled'],1)
        self.assertEqual(view['counts']['queued'],0)
        self.assertEqual(view['sites'][2]['pending'],0)

    def test_unconfirmed_search_warns_and_recovers_on_real_sources(self):
        self.job('no-sources',[{'site_id':'yuanbao','ok':True,'source':'official_api','web_search_status':'not_confirmed','citations':[]}])
        self.engine.tick(self.now,True)
        incident=self.engine.state['incidents']['yuanbao/search/search_not_confirmed']
        self.assertTrue(incident['active'])
        self.assertEqual(incident['severity'],'warning')
        self.assertIn('未返回来源',self.sent[0])
        self.job('sources',[{'site_id':'yuanbao','ok':True,'source':'official_api','web_search_status':'used','citations':[{'url':'https://example.com/real'}]}],at=self.now+10)
        self.engine.tick(self.now+10,True)
        self.assertFalse(self.engine.state['incidents']['yuanbao/search/search_not_confirmed']['active'])
    def test_go_nanosecond_timestamps_are_not_dropped(self):
        self.assertGreater(m.stamp('2026-10-13T10:38:03.287516837+08:00'),0)
        self.assertAlmostEqual(m.stamp('2026-10-13T10:38:03.287516837+08:00'),m.stamp('2026-10-13T10:38:03.287516+08:00'))
    def test_api_latency_is_not_counted_twice_when_web_was_skipped(self):
        row=m.sanitize_job({'job_id':'timing','results':[{'site_id':'kimi','ok':True,'source':'official_api','elapsed_ms':12000,'api_elapsed_ms':12000,'web_elapsed_ms':0}]})
        result=row['results'][0]
        self.assertEqual((result['api_ms'],result['web_ms']),(12000,0))
    def setUp(self):
        self.notify_environment=patch.dict(os.environ,{'NOTIFY_SOCKET':'','GEO_DATA_DIR':''});self.notify_environment.start();self.addCleanup(self.notify_environment.stop)
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'data/jobs').mkdir(parents=True)
        self.sent=[];self.cfg={'webhook':'https://open.feishu.cn/open-apis/bot/v2/hook/private-token','enabled':True,'dashboard_url':'https://example.com/admin/monitor','dedup_seconds':900}
        self.engine=m.Monitor(self.root,self.cfg,lambda c,t:self.sent.append(t));self.now=1791864000
    def tearDown(self):self.temp.cleanup()
    def job(self,job,results,status='succeeded',at=None):
        timestamp=m.dt.datetime.fromtimestamp(at or self.now,m.TZ).isoformat()
        data={'job_id':job,'status':status,'created_at':timestamp,'updated_at':timestamp,'finished_at':timestamp,'sites':['yuanbao'],'total_ms':4000,'prompt':'SECRET_PROMPT','ak':'SECRET_AK','callback_url':'https://private.example/token','results':results}
        (self.root/'data/jobs'/f'{job}.json').write_text(json.dumps(data),encoding='utf-8')
    def cursor_call(self,identity,at=None,**fields):
        folder=self.root/'data/cursor-calls';folder.mkdir(exist_ok=True)
        timestamp=m.dt.datetime.fromtimestamp(self.now if at is None else at,m.TZ).isoformat()
        data={'id':identity,'job_id':'cursor-job','site':'doubao','account_id':'acct_cursor','started_at':timestamp,'status':'running','task_type':'captcha_recovery',**fields}
        (folder/(identity+'.json')).write_text(json.dumps(data),encoding='utf-8')
    def test_pending_delivery_preserves_actual_attempts_without_final_failure(self):
        pending={'site_id':'yuanbao','ok':False,'source':'official_api','error_code':'api_request_failed','web_error_code':'site_timeout','api_error_code':'api_request_failed','delivery_pending':True,'retry_state':'retry_wait','attempts':[
            {'channel':'web','account_id':'acct_first','ok':False,'error_code':'site_timeout','elapsed_ms':100},
            {'channel':'api','ok':False,'error_code':'api_request_failed','elapsed_ms':200}]}
        self.job('pending',[pending],'queued')
        path=self.root/'data/jobs/pending.json';raw=json.loads(path.read_text(encoding='utf-8'))
        raw.update(delivery_policy='eventual',retry_at=m.dt.datetime.fromtimestamp(self.now+60,m.TZ).isoformat(),retry_count=3,wait_reason='api_request_failed')
        path.write_text(json.dumps(raw),encoding='utf-8')
        self.job('delivered',[{'site_id':'yuanbao','ok':True,'source':'web','attempts':[{'channel':'web','account_id':'acct_second','ok':True,'elapsed_ms':300}]}])
        view=self.engine.tick(self.now,True);summary=view['windows']['today'];site=next(s for s in summary['sites'] if s['site']=='yuanbao')
        self.assertEqual((summary['counts']['queued'],summary['success_rate']),(1,100))
        self.assertEqual((site['total'],site['success'],site['failed'],site['pending']),(1,1,0,1))
        self.assertEqual((summary['delivery']['total'],summary['delivery']['pending'],summary['delivery']['web_share']),(1,1,100))
        self.assertEqual((summary['channels']['web']['total'],summary['channels']['web']['success'],summary['channels']['web']['failed']),(2,1,1))
        self.assertEqual((summary['channels']['api']['total'],summary['channels']['api']['failed'],summary['channels']['api']['p95_ms']),(1,1,200))
        index=json.loads((self.root/'data/monitor-requests.json').read_text(encoding='utf-8'))
        row=next(r for r in index['rows'] if r['id']=='pending')
        self.assertEqual((row['delivery_policy'],row['retry_count'],row['wait_reason']),('eventual',3,'api_request_failed'))
        self.assertEqual(m.stamp(row['retry_at']),self.now+60)
        self.assertTrue(row['results'][0]['delivery_pending']);self.assertEqual(row['results'][0]['retry_state'],'retry_wait')
        day=next(iter(json.loads((self.root/'data/monitor-daily.json').read_text(encoding='utf-8'))['days'].values()))
        self.assertEqual(day['delivery'],summary['delivery'])
        self.assertFalse(any(i['channel']=='request' and i['site']=='yuanbao' for i in self.engine.state['incidents'].values()))
        for secret in ('SECRET_PROMPT','SECRET_ANSWER','SECRET_AK','private.example'):self.assertNotIn(secret,json.dumps(index)+json.dumps(view))

    def test_pending_final_success_is_processed_and_retires_old_final_failure(self):
        self.engine.state['initialized']=True
        self.engine.issue('yuanbao','request','site_timeout','persistent',self.now-1)
        self.job('persistent',[{'site_id':'yuanbao','ok':False,'error_code':'site_timeout','web_error_code':'site_timeout','delivery_pending':True,'retry_state':'retry_wait','attempts':[{'channel':'web','ok':False,'error_code':'site_timeout'}]}],'queued')
        self.engine.tick(self.now,True)
        self.assertFalse(self.engine.state['incidents']['yuanbao/request/site_timeout']['active'])
        self.assertEqual(self.engine.state['incidents']['yuanbao/request/site_timeout']['retired_reason'],'delivery_pending')
        self.assertTrue(self.engine.state['incidents']['yuanbao/web/site_timeout']['active'])
        self.job('persistent',[{'site_id':'yuanbao','ok':True,'source':'web','attempts':[{'channel':'web','ok':False,'error_code':'site_timeout'},{'channel':'web','ok':True}]}],at=self.now+10)
        summary=self.engine.tick(self.now+10,True)['windows']['today']
        self.assertFalse(self.engine.state['incidents']['yuanbao/web/site_timeout']['active'])
        self.assertEqual(summary['channels']['web']['total'],2)
        self.assertIn('persistent/yuanbao',self.engine.state['seen'])

    def test_unconfirmed_pending_without_journal_does_not_invent_web_failure(self):
        self.job('unknown',[{'site_id':'yuanbao','ok':False,'error_code':'recovery_state_missing','delivery_pending':True,'retry_state':'awaiting_confirmation','delivery_resume_required':True}],'queued')
        summary=self.engine.tick(self.now,True)['windows']['today']
        self.assertEqual((summary['channels']['web']['failed'],summary['channels']['web']['unknown']),(0,1))
        self.assertEqual((summary['delivery']['total'],summary['delivery']['pending']),(0,1))
        self.assertIsNone(summary['success_rate']);self.assertIsNone(summary['delivery']['web_share'])
        self.assertFalse(any(i['site']=='yuanbao' and i['channel'] in ('web','request','rate') for i in self.engine.state['incidents'].values()))

    def test_pending_metadata_uses_allowlisted_states_and_dates(self):
        raw={'job_id':'pending','created_at':m.dt.datetime.fromtimestamp(self.now,m.TZ).isoformat(),'delivery_policy':'eventual','retry_at':'javascript:SECRET_TOKEN','retry_count':-5,'wait_reason':'bad\nSECRET_TOKEN','results':[
            {'site_id':'yuanbao','ok':False,'delivery_pending':True,'retry_state':'SECRET_TOKEN','attempts_truncated':True,'attempts_dropped':4},
            {'site_id':'doubao','ok':True,'delivery_pending':True,'retry_state':'retry_wait'}]}
        row=m.sanitize_job(raw)
        self.assertEqual((row['retry_at'],row['retry_count'],row['wait_reason']),('',0,'unknown'))
        self.assertNotIn('retry_state',row['results'][0]);self.assertNotIn('delivery_pending',row['results'][1])
        self.assertEqual(row['results'][0]['attempts_dropped'],4)
        self.assertNotIn('SECRET_TOKEN',json.dumps(row))
    def test_counts_and_sensitive_content_never_leave_the_monitor(self):
        self.job('web',[{'site_id':'yuanbao','ok':True,'source':'api:conversation_detail','answer':'SECRET_ANSWER','citations':[]}])
        self.job('api',[{'site_id':'yuanbao','ok':True,'source':'official_api','web_search_status':'used','citations':[{'url':'https://example.com'}]}])
        self.job('fail',[{'site_id':'yuanbao','ok':False,'api_error_code':'api_quota_exhausted'}],'failed')
        view=self.engine.tick(self.now,True);s=view['windows']['1h']['sites'][2]
        self.assertEqual((s['total'],s['success'],s['web_success'],s['api_success'],s['search_used']),(3,2,1,1,1))
        self.assertEqual(view['windows']['1h']['success_rate'],66.7)
        raw=json.dumps(view)
        for private in ['SECRET_PROMPT','SECRET_AK','SECRET_ANSWER','private-token','private.example']:self.assertNotIn(private,raw)
        self.assertEqual((self.root/'data/monitor-state.json').stat().st_mode&0o777,0o600)
    def test_dedup_restart_recovery_and_new_incident(self):
        self.engine.issue('yuanbao','api','api_quota_exhausted','first',self.now)
        self.engine.tick(self.now,True)
        self.engine.issue('yuanbao','api','api_quota_exhausted','second',self.now+10)
        self.engine.tick(self.now+10,True)
        self.assertEqual(len(self.sent),1)
        engine=m.Monitor(self.root,self.cfg,lambda c,t:self.sent.append(t));engine.tick(self.now+20,True)
        self.assertEqual(len(self.sent),1)
        engine.recover('yuanbao','api',self.now+30);engine.tick(self.now+30,True)
        self.assertEqual(len(self.sent),2);self.assertIn('恢复',self.sent[-1])
        engine.issue('yuanbao','api','api_quota_exhausted','third',self.now+40);engine.tick(self.now+40,True)
        self.assertEqual(len(self.sent),3)
    def test_delivery_failure_retries_after_restart(self):
        self.engine.sender=lambda c,t:(_ for _ in ()).throw(RuntimeError('飞书 HTTP 503'))
        self.engine.issue('kimi','search','search_request_failed','test',self.now)
        self.engine.tick(self.now,True)
        self.assertEqual(self.engine.state['delivery']['status'],'failed')
        engine=m.Monitor(self.root,self.cfg,lambda c,t:self.sent.append(t));engine.tick(self.now+5,True)
        self.assertEqual(len(self.sent),0)
        engine.tick(self.now+21,True);self.assertEqual(len(self.sent),1)
        self.assertEqual(engine.state['outbox'][0]['status'],'sent')
    def test_partial_requests_do_not_count_as_complete_success(self):
        self.job('partial',[{'site_id':'yuanbao','ok':False,'error_code':'platform_busy'}],'partial')
        self.job('running',[],'running')
        view=self.engine.tick(self.now,True)['windows']['1h']
        self.assertEqual(view['success_rate'],0);self.assertEqual(view['counts']['running'],1)
    def test_healthy_request_clears_api_incident(self):
        self.engine.issue('yuanbao','api','api_quota_exhausted','first',self.now);self.engine.tick(self.now,True)
        row={'id':'success'};r={'site':'yuanbao','ok':True,'source':'api','web_error_code':'','error_code':'','web_search_status':'used','share_status':'not_supported'}
        self.engine.result_event(row,r,self.now+10);self.engine.tick(self.now+10,True)
        self.assertFalse(self.engine.state['incidents']['yuanbao/api/api_quota_exhausted']['active'])
    def test_service_outage_requires_three_observations_and_recovers(self):
        with patch.object(m.urllib.request,'urlopen',side_effect=OSError()):
            self.assertFalse(self.engine.health(self.now));self.assertFalse(self.engine.health(self.now+10));self.assertEqual(len(self.engine.state['incidents']),0)
            self.engine.health(self.now+20)
        self.engine.tick(self.now+20,False);self.assertIn('业务服务不可达',self.sent[0])
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*_):pass
        with patch.object(m.urllib.request,'urlopen',return_value=Response()):self.engine.health(self.now+30)
        self.engine.tick(self.now+30,True);self.assertIn('恢复',self.sent[-1])
    def test_feishu_http_success_with_business_error_is_not_delivery_success(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self,*_):return b'{"code":19024,"msg":"Key Words Not Found"}'
        with patch.object(m.urllib.request,'urlopen',return_value=Response()):
            with self.assertRaisesRegex(RuntimeError,'19024'):m.send_feishu(self.cfg,'test')
    def test_rate_alert_has_minimum_sample(self):
        for i in range(4):self.job('fail'+str(i),[{'site_id':'yuanbao','ok':False,'error_code':'site_timeout'}],'failed')
        self.engine.tick(self.now,True);self.assertNotIn('yuanbao/rate/failure_rate',self.engine.state['incidents'])
        self.job('fail4',[{'site_id':'yuanbao','ok':False,'error_code':'site_timeout'}],'failed')
        self.engine.tick(self.now+5,True);self.assertTrue(self.engine.state['incidents']['yuanbao/rate/failure_rate']['active'])

    def test_quota_failure_only_pushes_root_cause(self):
        self.engine.state['initialized']=True
        for i in range(5):self.job('fail'+str(i),[{'site_id':'doubao','ok':False,'error_code':'site_timeout','api_error_code':'api_quota_exhausted','web_search_status':'failed','web_search_error_code':'search_quota_exhausted'}],'failed')
        self.engine.tick(self.now,True)
        active=[i for i in self.engine.state['incidents'].values() if i['active']]
        self.assertEqual([i['key'] for i in active],['doubao/api/api_quota_exhausted'])
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0].count('【告警】'),1)

    def test_reason_changes_retire_old_error_without_false_recovery(self):
        self.engine.issue('doubao','api','api_auth_failed','old',self.now)
        self.engine.tick(self.now,True)
        self.engine.issue('doubao','api','api_quota_exhausted','new',self.now+10)
        self.engine.tick(self.now+10,True)
        old=self.engine.state['incidents']['doubao/api/api_auth_failed']
        self.assertFalse(old['active']);self.assertEqual(old['retired_reason'],'superseded_by_current_reason')
        self.assertEqual(len(self.sent),1)
        self.assertFalse(any('恢复' in x for x in self.sent))

    def test_successful_api_fallback_warns_as_degraded_and_limits_reminders(self):
        self.engine.state['initialized']=True
        result={'site_id':'yuanbao','ok':True,'source':'official_api','web_error_code':'platform_busy','web_search_status':'used','citations':[{'url':'https://example.org/real'}]}
        self.job('first',[result]);self.engine.tick(self.now,True)
        self.assertIn('API 已兜底返回回答',self.sent[0])
        result['web_error_code']='site_timeout'
        self.job('next',[result],at=self.now+1000);self.engine.tick(self.now+1000,True)
        self.assertEqual(len(self.sent),1)
        self.job('again',[result],at=self.now+3700);self.engine.tick(self.now+3700,True)
        self.assertEqual(len(self.sent),2)

    def test_old_incident_expires_without_claiming_recovery(self):
        self.engine.issue('deepseek','web','platform_busy','old',self.now)
        self.engine.tick(self.now,True)
        self.engine.tick(self.now+21601,True)
        item=self.engine.state['incidents']['deepseek/web/platform_busy']
        self.assertFalse(item['active']);self.assertEqual(item['retired_reason'],'stale_without_recent_observation')
        self.assertEqual(len(self.sent),1)

    def test_persisted_old_api_alert_recovers_from_already_seen_success(self):
        self.engine.issue('yuanbao','api','api_timeout','old',self.now)
        self.engine.tick(self.now,True)
        self.job('success',[{'site_id':'yuanbao','ok':True,'source':'official_api','web_search_status':'used'}],at=self.now+10)
        self.engine.state['seen']['success/yuanbao']=self.now+10
        self.engine.tick(self.now+10,True)
        self.assertFalse(self.engine.state['incidents']['yuanbao/api/api_timeout']['active'])

    def test_api_fallback_has_separate_actual_stage_success_rates(self):
        self.job('fallback',[{'site_id':'yuanbao','ok':True,'source':'official_api','web_error_code':'platform_quota','web_elapsed_ms':3000,'api_elapsed_ms':1500,'account_id':'acct_failed'}])
        window=self.engine.tick(self.now,True)['windows']['1h']
        self.assertEqual(window['success_rate'],100)
        self.assertEqual(window['channels']['web'],{'total':1,'success':0,'failed':1,'skipped':0,'unknown':0,'success_rate':0.0,'p50_ms':3000,'p95_ms':3000,'avg_ms':3000.0,'latency_samples':1})
        self.assertEqual(window['channels']['api'],{'total':1,'success':1,'failed':0,'skipped':0,'unknown':0,'success_rate':100.0,'p50_ms':1500,'p95_ms':1500,'avg_ms':1500.0,'latency_samples':1})
        site=next(s for s in window['sites'] if s['site']=='yuanbao')
        self.assertEqual((site['web_total'],site['web_success_rate'],site['api_total'],site['api_success_rate'],site['api_delivery_success']),(1,0.0,1,100.0,1))
        accounts={a['account_id']:a for a in window['accounts']}
        self.assertEqual(accounts['acct_failed']['last_error_code'],'platform_quota')
        self.assertEqual((accounts['api:yuanbao']['failed'],accounts['api:yuanbao']['success']),(0,1))

    def test_skipped_stages_never_count_as_failed_calls(self):
        self.job('skipped',[{'site_id':'doubao','ok':False,'error_code':'platform_risk_control','web_skipped':True,'web_skip_reason':'site_cooldown','api_error_code':'api_admission_timeout'}],'failed')
        window=self.engine.tick(self.now,True)['windows']['1h']
        for channel in ('web','api'):
            self.assertEqual(window['channels'][channel]['skipped'],1)
            self.assertEqual(window['channels'][channel]['total'],0)
            self.assertIsNone(window['channels'][channel]['success_rate'])

    def test_historical_stage_without_evidence_is_unknown(self):
        self.job('legacy',[{'site_id':'yuanbao','ok':True,'source':'official_api','account_id':'api:yuanbao','account_retries':1}])
        window=self.engine.tick(self.now,True)['windows']['1h']
        self.assertEqual(window['channels']['web']['unknown'],2)
        self.assertEqual(window['channels']['web']['total'],0)
        attempts=window['recent'][0]['results'][0]['attempts']
        self.assertTrue(all(a['account_id']=='' and a['ok'] is None and a['inferred'] for a in attempts if a['channel']=='web'))
        result=m.sanitize_job({'job_id':'no-evidence','results':[{'site_id':'doubao','ok':True}]})['results'][0]
        self.assertEqual(result['attempts'][0]['status'],'unknown')

    def test_attempt_ledger_preserves_failed_accounts_before_success(self):
        accounts={'accounts':[{'id':'acct_first','phone':'13800138351','secret':'SECRET_ACCOUNT'},{'id':'acct_second','phone':'13900137132'}]}
        (self.root/'data/accounts.json').write_text(json.dumps(accounts),encoding='utf-8')
        timestamp=m.dt.datetime.fromtimestamp(self.now,m.TZ).isoformat()
        self.job('switched',[{'site_id':'doubao','ok':True,'source':'dom','account_id':'acct_second','attempts':[
            {'channel':'web','account_id':'acct_first','ok':False,'error_code':'platform_risk_control','elapsed_ms':500,'started_at':timestamp},
            {'channel':'web','account_id':'acct_second','ok':True,'elapsed_ms':800,'started_at':timestamp}]}])
        window=self.engine.tick(self.now,True)['windows']['1h']
        self.assertEqual((window['channels']['web']['total'],window['channels']['web']['success_rate']),(2,50.0))
        by_id={a['account_id']:a for a in window['accounts']}
        self.assertEqual((by_id['acct_first']['failed'],by_id['acct_second']['success']),(1,1))
        self.assertEqual(by_id['acct_first']['account_label'],'手机尾号 8351')
        self.assertEqual(by_id['acct_second']['account_label'],'手机尾号 7132')
        self.assertEqual(by_id['acct_first']['last_at'],self.now)
        public=(self.root/'data/monitor-requests.json').read_text(encoding='utf-8')
        for private in ('13800138351','13900137132','SECRET_ACCOUNT'):self.assertNotIn(private,public)

    def test_api_failure_is_never_attributed_to_browser_phone(self):
        self.job('failure',[{'site_id':'yuanbao','ok':False,'account_id':'acct_browser','error_code':'platform_busy','api_error_code':'api_quota_exhausted','api_fallback_failed':True,'attempts':[
            {'channel':'web','account_id':'acct_browser','ok':False,'error_code':'platform_busy'},
            {'channel':'api','account_id':'acct_browser','ok':False,'error_code':'api_quota_exhausted'}]}],'failed')
        window=self.engine.tick(self.now,True)['windows']['1h']
        accounts={a['account_id']:a for a in window['accounts']}
        self.assertEqual(accounts['acct_browser']['last_error_code'],'platform_busy')
        self.assertEqual(accounts['api:yuanbao']['last_error_code'],'api_quota_exhausted')
        self.assertEqual(accounts['api:yuanbao']['account_label'],'元宝 API')

    def test_request_index_retains_more_than_recent_preview_and_sanitizes_details(self):
        for i in range(105):
            self.job('request'+str(i),[{'site_id':'yuanbao','ok':True,'source':'official_api','answer':'SECRET_ANSWER','diagnostic_id':'../../SECRET_DIAGNOSTIC','error':'SECRET_RAW_ERROR','attempts':[
                {'channel':'api','account_id':'SECRET_TOKEN','ok':True,'elapsed_ms':12,'error':'SECRET_ATTEMPT_ERROR','cookies':'SECRET_COOKIE','started_at':'SECRET_TIME'}]}])
        view=self.engine.tick(self.now,True)
        self.assertEqual(len(view['windows']['1h']['recent']),100)
        index=json.loads((self.root/'data/monitor-requests.json').read_text(encoding='utf-8'))
        self.assertEqual(len(index['rows']),105)
        self.assertEqual(index['updated_at'],self.now)
        public=json.dumps({'dashboard':view,'index':index})
        for secret in ('SECRET_PROMPT','SECRET_AK','SECRET_ANSWER','SECRET_RAW_ERROR','SECRET_DIAGNOSTIC','SECRET_TOKEN','SECRET_ATTEMPT_ERROR','SECRET_COOKIE','SECRET_TIME','private.example'):
            self.assertNotIn(secret,public)
        self.assertEqual((self.root/'data/monitor-requests.json').stat().st_mode&0o777,0o600)

    def test_calendar_windows_use_beijing_midnight_and_noon(self):
        now=m.stamp('2026-10-16T17:00:00+08:00')
        self.job('yesterday',[{'site_id':'yuanbao','ok':True,'source':'dom'}],at=m.stamp('2026-10-15T23:59:00+08:00'))
        self.job('morning',[{'site_id':'yuanbao','ok':True,'source':'dom'}],at=m.stamp('2026-10-16T11:59:59+08:00'))
        self.job('noon',[{'site_id':'yuanbao','ok':True,'source':'dom'}],at=m.stamp('2026-10-16T12:00:00+08:00'))
        view=self.engine.tick(now,True)
        self.assertEqual(view['windows']['24h']['total'],3)
        self.assertEqual(view['windows']['today']['total'],2)
        self.assertEqual(view['windows']['afternoon']['total'],1)

    def test_future_requests_are_excluded_from_windows_and_index(self):
        self.job('past',[{'site_id':'yuanbao','ok':True,'source':'dom'}],at=self.now-1)
        self.job('future',[{'site_id':'yuanbao','ok':True,'source':'dom'}],at=self.now+1)
        view=self.engine.tick(self.now,True)
        self.assertEqual(view['windows']['1h']['total'],1)
        index=json.loads((self.root/'data/monitor-requests.json').read_text(encoding='utf-8'))
        self.assertEqual([row['id'] for row in index['rows']],['past'])
        future=m.sanitize_job({'job_id':'future','created_at':m.dt.datetime.fromtimestamp(self.now+1,m.TZ).isoformat(),'results':[]})
        self.assertEqual(m.summarize([future],self.now,3600)['total'],0)

    def test_historical_successful_search_recovery_keeps_web_stage_success(self):
        self.job('recovered',[{'site_id':'doubao','ok':True,'source':'official_api','web_error_code':'search_not_confirmed','web_elapsed_ms':1200,'api_elapsed_ms':500,'search_recovery_status':'succeeded','account_id':'acct_browser'}])
        window=self.engine.tick(self.now,True)['windows']['1h']
        self.assertEqual((window['channels']['web']['success'],window['channels']['web']['failed']),(1,0))
        self.assertEqual((window['channels']['api']['success'],window['channels']['api']['failed']),(1,0))
        attempts=window['recent'][0]['results'][0]['attempts']
        self.assertEqual(attempts[0]['error_code'],'')

    def test_historical_failed_search_recovery_keeps_web_and_records_api_failure(self):
        self.job('recovery-failed',[{'site_id':'doubao','ok':True,'source':'dom','elapsed_ms':1200,'search_recovery_status':'failed','search_recovery_error_code':'api_quota_exhausted','search_recovery_elapsed_ms':500,'account_id':'acct_browser'}])
        window=self.engine.tick(self.now,True)['windows']['1h']
        self.assertEqual((window['channels']['web']['success'],window['channels']['web']['failed']),(1,0))
        self.assertEqual((window['channels']['api']['success'],window['channels']['api']['failed']),(0,1))
        accounts={a['account_id']:a for a in window['accounts']}
        self.assertEqual(accounts['acct_browser']['last_error_code'],'')
        self.assertEqual(accounts['api:doubao']['last_error_code'],'api_quota_exhausted')

    def test_historical_search_recovery_admission_failure_is_skipped(self):
        row=m.sanitize_job({'job_id':'recovery-admission','results':[{'site_id':'doubao','ok':True,'source':'dom','search_recovery_status':'failed','search_recovery_error_code':'api_admission_timeout','search_recovery_elapsed_ms':500}]})
        api=row['results'][0]['attempts'][1]
        self.assertTrue(api['skipped'])
        self.assertIsNone(api['ok'])
        self.assertEqual(api['elapsed_ms'],500)

    def test_historical_search_quality_failure_does_not_become_api_call_failure(self):
        row=m.sanitize_job({'job_id':'recovery-no-sources','results':[{'site_id':'doubao','ok':True,'source':'dom','search_recovery_status':'not_confirmed','search_recovery_error_code':'search_not_confirmed','search_recovery_elapsed_ms':500}]})
        self.assertTrue(all(a['ok'] for a in row['results'][0]['attempts']))
        self.assertTrue(all(a['error_code']=='' for a in row['results'][0]['attempts']))

    def test_watchdog_uses_optional_local_datagram_socket(self):
        self.assertFalse(m.notify_watchdog())
        address=str(self.root/'watchdog.sock')
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as receiver:
            receiver.bind(address);receiver.settimeout(1)
            with patch.dict(os.environ,{'NOTIFY_SOCKET':address}):self.assertTrue(m.notify_watchdog())
            self.assertEqual(receiver.recv(128),b'WATCHDOG=1')
        with patch.dict(os.environ,{'NOTIFY_SOCKET':str(self.root/'missing.sock')}):self.assertFalse(m.notify_watchdog())

    @unittest.skipUnless(m.os.name=='posix' and hasattr(socket,'AF_UNIX'),'requires Unix abstract sockets')
    def test_watchdog_supports_systemd_abstract_socket(self):
        address='geo-monitor-watchdog-'+str(os.getpid())+'-'+str(id(self))
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as receiver:
            try:receiver.bind('\0'+address)
            except OSError:self.skipTest('abstract sockets unavailable')
            receiver.settimeout(1)
            with patch.dict(os.environ,{'NOTIFY_SOCKET':'@'+address}):self.assertTrue(m.notify_watchdog())
            self.assertEqual(receiver.recv(128),b'WATCHDOG=1')

    def test_watchdog_is_sent_only_after_all_snapshot_files_are_published(self):
        original=m.atomic_json;published=[]
        def record(path,data):
            original(path,data);published.append(path.name)
        def heartbeat():
            self.assertEqual(published[-4:],['monitor-state.json','monitor-requests.json','monitor-daily.json','monitor-dashboard.json'])
            for name in ('monitor-requests.json','monitor-daily.json','monitor-dashboard.json'):
                self.assertEqual(json.loads((self.root/'data'/name).read_text(encoding='utf-8'))['updated_at'],self.now)
        with patch.object(m,'atomic_json',side_effect=record),patch.object(m,'notify_watchdog',side_effect=heartbeat) as notify:
            result=self.engine.tick(self.now,True)
        notify.assert_called_once()
        self.assertGreater(result['generated_at'],0)
        self.assertGreaterEqual(result['tick_ms'],0)
        for failing in ('monitor-state.json','monitor-requests.json','monitor-daily.json','monitor-dashboard.json'):
            with self.subTest(failing=failing):
                def fail_write(path,data):
                    if path.name==failing:raise OSError('simulated publication failure')
                    original(path,data)
                with patch.object(m,'atomic_json',side_effect=fail_write),patch.object(m,'notify_watchdog') as notify:
                    with self.assertRaises(OSError):self.engine.tick(self.now,True)
                notify.assert_not_called()

    def test_tick_delay_uses_start_to_start_monotonic_cadence(self):
        self.assertEqual(m.tick_delay(100,102),8)
        self.assertEqual(m.tick_delay(100,110),0)
        self.assertEqual(m.tick_delay(100,125),0)
        with patch.object(m.time,'monotonic',return_value=104.25),patch.object(m.time,'time',return_value=-999999):
            self.assertEqual(m.tick_delay(100),5.75)

    def test_daily_history_keeps_beijing_boundaries_and_old_requests_without_replaying_alerts(self):
        now=m.stamp('2026-10-16T17:00:00+08:00')
        self.engine.state['initialized']=True
        self.job('old-failure',[{'site_id':'yuanbao','ok':False,'error_code':'platform_quota'}],'failed',m.stamp('2026-10-14T15:59:59Z'))
        self.job('new-day',[{'site_id':'yuanbao','ok':True,'source':'dom'}],at=m.stamp('2026-10-14T16:00:00Z'))
        self.job('today',[{'site_id':'yuanbao','ok':True,'source':'dom'}],at=now)
        view=self.engine.tick(now,True)
        self.assertEqual(view['available_dates'],['2026-10-16','2026-10-15','2026-10-14'])
        daily=json.loads((self.root/'data/monitor-daily.json').read_text(encoding='utf-8'))
        self.assertEqual(daily['timezone'],'Asia/Shanghai')
        self.assertEqual(daily['days']['2026-10-14']['success_rate'],0)
        self.assertEqual(daily['days']['2026-10-15']['total'],1)
        self.assertEqual(view['windows']['24h']['total'],1)
        self.assertEqual(len(json.loads((self.root/'data/monitor-requests.json').read_text(encoding='utf-8'))['rows']),3)
        self.assertNotIn('old-failure/yuanbao',self.engine.state['seen'])
        self.assertEqual(self.sent,[])
        self.assertTrue(all('recent' not in day for day in daily['days'].values()))
        self.assertTrue(all(set(day)=={'date','total','success_rate','p50_ms','p95_ms','channels','delivery','recovery'} for day in view['daily']))
        public=json.dumps({'dashboard':view,'daily':daily})
        for secret in ('SECRET_PROMPT','SECRET_AK','SECRET_ANSWER','private.example'):self.assertNotIn(secret,public)

    def test_channel_daily_latency_excludes_skipped_unknown_and_missing_timings(self):
        self.job('timings',[{'site_id':'yuanbao','ok':True,'source':'official_api','attempts':[
            {'channel':'web','ok':True,'elapsed_ms':100},
            {'channel':'web','ok':False,'elapsed_ms':300},
            {'channel':'web','ok':True,'elapsed_ms':0},
            {'channel':'web','ok':False,'elapsed_ms':999999,'skipped':True},
            {'channel':'web','elapsed_ms':888888},
            {'channel':'api','ok':True,'elapsed_ms':50},
            {'channel':'api','ok':False,'elapsed_ms':150}]}])
        view=self.engine.tick(self.now,True)
        day=json.loads((self.root/'data/monitor-daily.json').read_text(encoding='utf-8'))['days'][view['available_dates'][0]]
        web=day['channels']['web'];api=day['channels']['api']
        self.assertEqual((web['total'],web['latency_samples'],web['p50_ms'],web['p95_ms'],web['avg_ms']),(3,2,100,300,200))
        self.assertEqual((api['latency_samples'],api['p50_ms'],api['p95_ms'],api['avg_ms']),(2,50,150,100))
        self.assertEqual((day['p50_ms'],day['p95_ms']),(4000,4000))

    def test_historical_cache_reads_only_changed_job_content(self):
        self.job('old',[{'site_id':'yuanbao','ok':True,'source':'dom'}],at=self.now-3*86400)
        self.job('today',[{'site_id':'yuanbao','ok':True,'source':'dom'}])
        original=Path.read_text;reads=[]
        def read(path,*args,**kwargs):
            if path.parent==self.root/'data/jobs':reads.append(path.name)
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',autospec=True,side_effect=read):
            self.assertEqual(len(self.engine.load_rows(self.now)),2)
            self.assertEqual(len(self.engine.load_rows(self.now)),2)
            changed=self.root/'data/jobs/old.json';mtime=changed.stat().st_mtime_ns
            os.utime(changed,ns=(mtime+1000000000,mtime+1000000000))
            self.engine.load_rows(self.now)
        self.assertEqual(reads.count('old.json'),2)
        self.assertEqual(reads.count('today.json'),1)

    def test_cursor_log_lifecycle_is_incremental_and_success_requires_structured_result(self):
        def line(offset,action,job,account):
            at=m.dt.datetime.fromtimestamp(self.now+offset,m.TZ).strftime('%Y/%m/%d %H:%M:%S')
            return at+' geo-server cursor captcha agent '+action+' job='+job+' account='+account+' SECRET_LOG_CONTENT\n'
        log=self.root/'geo-server.out.log'
        log.write_text(line(-120,'start','legacy','acct_first')+line(-110,'done','legacy','acct_first')+line(-90,'start','exit','acct_second')+line(-80,'exit','exit','acct_second')+line(-60,'start','timeout','acct_third')+line(-30,'timeout','timeout','acct_third')+line(-600,'start','lost','acct_fourth')+line(-5,'start','running','acct_fifth'),encoding='utf-8')
        calls=self.engine.load_cursor_calls(self.now);by_job={call['job_id']:call for call in calls}
        self.assertEqual(by_job['legacy']['status'],'unknown')
        self.assertEqual(by_job['exit']['error_code'],'cli_process_failed')
        self.assertEqual(by_job['timeout']['error_code'],'cli_timeout')
        self.assertEqual(by_job['lost']['status'],'unknown')
        self.assertEqual(by_job['running']['status'],'running')
        self.assertTrue(all(call['inferred'] for call in calls))
        offset=self.engine.cursor_log_offset
        self.assertEqual(len(self.engine.load_cursor_calls(self.now)),5)
        self.assertEqual(self.engine.cursor_log_offset,offset)
        with log.open('a',encoding='utf-8') as stream:stream.write(line(-3,'done','running','acct_fifth'))
        appended=self.engine.load_cursor_calls(self.now)
        self.assertEqual(len(appended),5)
        self.assertEqual(next(call for call in appended if call['job_id']=='running')['elapsed_ms'],2000)
        self.assertGreater(self.engine.cursor_log_offset,offset)
        self.cursor_call('structured',at=self.now-120+0.4,job_id='legacy',account_id='acct_first',status='success',ok=True,finished_at=self.now-110,elapsed_ms=9600,prompt='SECRET_PROMPT',phone='13800138351')
        calls=self.engine.load_cursor_calls(self.now)
        self.assertEqual(len(calls),5)
        winner=next(call for call in calls if call['job_id']=='legacy')
        self.assertEqual((winner['id'],winner['status'],winner['inferred']),('structured','success',False))
        public=json.dumps(calls)
        for private in ('SECRET_LOG_CONTENT','SECRET_PROMPT','13800138351'):self.assertNotIn(private,public)

    def test_cursor_stats_are_independent_and_grouped_by_call_start_date(self):
        now=m.stamp('2026-10-16T17:00:00+08:00');yesterday=m.stamp('2026-10-15T23:00:00+08:00')
        self.job('cursor-job',[{'site_id':'doubao','ok':True,'source':'dom','elapsed_ms':250}],at=yesterday)
        self.cursor_call('success',at=now-100,status='success',finished_at=now-90,elapsed_ms=1000)
        self.cursor_call('failed',at=now-80,status='failed',finished_at=now-70,elapsed_ms=3000,error_code='cli_timeout')
        self.cursor_call('unknown',at=now-60,status='unknown',finished_at=now-50,elapsed_ms=5000)
        self.cursor_call('running',at=now-10,status='running',elapsed_ms=9000)
        self.cursor_call('skip',at=now-5,status='skipped',elapsed_ms=8000,error_code='cli_unavailable')
        view=self.engine.tick(now,True);today=view['windows']['today'];cursor=today['channels']['cursor']
        self.assertEqual((today['total'],today['channels']['web']['total'],today['channels']['api']['total']),(0,0,0))
        self.assertEqual((cursor['total'],cursor['success'],cursor['failed'],cursor['unknown'],cursor['running'],cursor['skipped'],cursor['success_rate']),(4,1,1,1,1,1,50))
        self.assertEqual((cursor['latency_samples'],cursor['p50_ms'],cursor['p95_ms'],cursor['avg_ms']),(2,1000,3000,2000))
        self.assertIn('历史手工',cursor['coverage_note'])
        site=next(site for site in today['sites'] if site['site']=='doubao')
        self.assertEqual((site['total'],site['cursor_total'],site['cursor_p95_ms']),(0,4,3000))
        days=json.loads((self.root/'data/monitor-daily.json').read_text(encoding='utf-8'))['days']
        self.assertEqual(days['2026-10-16']['channels']['cursor']['total'],4)
        self.assertEqual(days['2026-10-15']['channels']['cursor']['total'],0)
        index=json.loads((self.root/'data/monitor-requests.json').read_text(encoding='utf-8'))
        self.assertEqual(len(index['cursor_calls']),5)
        self.assertNotIn('cursor_calls',index['rows'][0])
        self.assertEqual(len([a for a in index['rows'][0]['results'][0]['attempts'] if a['channel']=='cursor']),5)
        # A subsequent tick reuses cached rows with attached evidence. Counts
        # and detail attempts must remain independent rather than doubling.
        again=self.engine.tick(now,True)['windows']['24h']
        self.assertEqual(again['channels']['cursor']['total'],4)
        self.assertEqual(again['channels']['web']['total'],1)
        index=json.loads((self.root/'data/monitor-requests.json').read_text(encoding='utf-8'))
        self.assertEqual(len(index['rows'][0]['results'][0]['attempts']),6)

    def test_unmerged_cursor_evidence_does_not_invent_result_or_request(self):
        self.job('cursor-job',[],'running')
        self.cursor_call('background',status='running')
        self.cursor_call('orphan',job_id='missing-job',site='deepseek',status='unknown',elapsed_ms=1000)
        view=self.engine.tick(self.now,True)['windows']['1h']
        self.assertEqual(view['total'],1)
        self.assertTrue(all(site['total']==0 for site in view['sites']))
        self.assertEqual(view['channels']['cursor']['total'],2)
        index=json.loads((self.root/'data/monitor-requests.json').read_text(encoding='utf-8'))
        self.assertEqual(index['rows'][0]['results'],[])
        self.assertEqual(index['rows'][0]['cursor_calls'][0]['id'],'background')
        self.assertEqual(len(index['cursor_calls']),2)

    def test_distinct_structured_cursor_ids_in_one_second_are_preserved(self):
        self.cursor_call('first',at=self.now-10+0.1,status='success',finished_at=self.now-9,elapsed_ms=900)
        self.cursor_call('second',at=self.now-10+0.8,status='failed',finished_at=self.now-8,elapsed_ms=2200,error_code='cli_process_failed')
        calls=self.engine.load_cursor_calls(self.now)
        self.assertEqual({call['id'] for call in calls},{'first','second'})
        summary=m.summarize([],self.now,3600,cursor_calls=calls)
        self.assertEqual((summary['channels']['cursor']['total'],summary['channels']['cursor']['success_rate']),(2,50))

    def test_cursor_log_call_id_deduplicates_cross_second_start_delay(self):
        self.cursor_call('matching-id',at=self.now-100.8,status='success',finished_at=self.now-80,elapsed_ms=20800)
        def line(offset,action):
            at=m.dt.datetime.fromtimestamp(self.now+offset,m.TZ).strftime('%Y/%m/%d %H:%M:%S')
            return at+' geo-server cursor captcha agent '+action+' job=cursor-job account=acct_cursor call=matching-id\n'
        (self.root/'geo-server.out.log').write_text(line(-98,'start')+line(-80,'done'),encoding='utf-8')
        calls=self.engine.load_cursor_calls(self.now)
        self.assertEqual(len(calls),1)
        self.assertEqual((calls[0]['id'],calls[0]['status'],calls[0]['inferred']),('matching-id','success',False))

    def test_custom_data_directory_is_used_for_jobs_accounts_cursor_and_all_outputs(self):
        self.job('custom-job',[{'site_id':'doubao','ok':True,'source':'dom','account_id':'acct_cursor'}])
        self.cursor_call('custom-call',job_id='custom-job',status='success',finished_at=self.now+1,elapsed_ms=1000)
        (self.root/'data/accounts.json').write_text(json.dumps({'accounts':[{'id':'acct_cursor','phone':'13800138351'}]}),encoding='utf-8')
        custom=self.root/'custom-data';(self.root/'data').rename(custom)
        with patch.dict(os.environ,{'GEO_DATA_DIR':str(custom)}):
            engine=m.Monitor(self.root,{'enabled':False},sender=lambda *_:self.fail('unexpected notification'))
            view=engine.tick(self.now+1,True)
        self.assertEqual(engine.state_path,custom/'monitor-state.json')
        self.assertEqual(view['windows']['1h']['total'],1)
        self.assertEqual(view['windows']['1h']['channels']['cursor']['success'],1)
        for name in ('monitor-state.json','monitor-requests.json','monitor-daily.json','monitor-dashboard.json'):self.assertTrue((custom/name).is_file())
        index=json.loads((custom/'monitor-requests.json').read_text(encoding='utf-8'))
        self.assertEqual(index['cursor_calls'][0]['account_label'],'手机尾号 8351')
        self.assertFalse((self.root/'data').exists())

    def test_stage_timings_keep_only_finite_nonnegative_numeric_allowlist(self):
        raw={'pool_wait_ms':0,'site_gate_wait_ms':12.5,'web_execution_ms':3000,'api_wait_ms':5000,'api_execution_ms':0,
             'session_setup_ms':-1,'cloud_slot_wait_ms':'100','web_slot_wait_ms':True,'session_acquire_ms':float('inf'),
             'SECRET_TOKEN':'SECRET_VALUE','answer':'SECRET_ANSWER','nan':float('nan')}
        result=m.sanitize_job({'job_id':'timed','results':[{'site_id':'doubao','ok':False,'stage_timings':raw}]})['results'][0]
        self.assertEqual(result['stage_timings'],{'pool_wait_ms':0,'site_gate_wait_ms':12.5,'web_execution_ms':3000,'api_wait_ms':5000,'api_execution_ms':0})
        for invalid in (None,[],False,'bad',{'web_execution_ms':float('nan')},{'web_execution_ms':10**400}):
            self.assertEqual(m.sanitize_stage_timings(invalid),{})
        self.job('timed',[{'site_id':'doubao','ok':True,'source':'dom','stage_timings':raw}])
        self.engine.tick(self.now,True)
        index=json.loads((self.root/'data/monitor-requests.json').read_text(encoding='utf-8'))
        self.assertEqual(index['rows'][0]['results'][0]['stage_timings'],result['stage_timings'])
        self.assertNotIn('SECRET_',json.dumps(index))

    def test_local_skips_do_not_blame_accounts_or_platform_failure_rates(self):
        for i in range(5):
            self.job('local'+str(i),[{'site_id':'doubao','ok':False,'account_id':'acct_not_called',
                'error_code':'insufficient_web_budget','web_error_code':'insufficient_web_budget','web_skipped':True,
                'api_error_code':'api_admission_timeout','attempts':[
                    {'channel':'web','account_id':'acct_not_called','ok':False,'skipped':True,'error_code':'insufficient_web_budget','skip_reason':'insufficient_web_budget'},
                    {'channel':'api','account_id':'acct_not_called','ok':False,'skipped':True,'error_code':'api_admission_timeout','skip_reason':'api_admission'}]}],'failed')
        self.engine.state['initialized']=True
        view=self.engine.tick(self.now,True)['windows']['1h']
        for channel in ('web','api'):
            self.assertEqual((view['channels'][channel]['total'],view['channels'][channel]['failed'],view['channels'][channel]['skipped']),(0,0,5))
            self.assertIsNone(view['channels'][channel]['success_rate'])
        self.assertFalse(any(a['account_id']=='acct_not_called' for a in view['accounts']))
        for result in (row['results'][0] for row in view['recent']):
            self.assertEqual(result['account_id'],'')
            self.assertTrue(all(a['account_id']=='' and '本地' in a['account_label'] for a in result['attempts']))
        active=[i for i in self.engine.state['incidents'].values() if i['active']]
        self.assertTrue(active)
        self.assertTrue(all(i['channel']=='local' and i['severity']=='warning' for i in active))
        self.assertNotIn('doubao/rate/failure_rate',self.engine.state['incidents'])
        self.assertEqual(len(self.sent),1)

    def test_local_skip_does_not_rewrite_a_historical_real_failed_attempt(self):
        row=m.sanitize_job({'job_id':'real','results':[{'site_id':'kimi','ok':False,'account_id':'acct_called',
            'error_code':'session_setup_timeout','attempts':[{'channel':'web','account_id':'acct_called','ok':False,
            'skipped':False,'error_code':'session_setup_timeout','elapsed_ms':200}]}]})
        attempt=row['results'][0]['attempts'][0]
        self.assertEqual((attempt['status'],attempt['account_id']),('failed','acct_called'))
        self.assertFalse(m.local_skip_code(row['results'][0],'web'))
        row['created']=self.now;row['updated']=self.now
        self.assertEqual(m.summarize([row],self.now,300)['channels']['web']['failed'],1)

    def test_successful_api_fallback_does_not_recover_then_repush_same_local_skip(self):
        self.engine.state['initialized']=True
        result={'site_id':'doubao','ok':True,'source':'official_api','web_error_code':'browser_admission_timeout',
            'web_skipped':True,'attempts':[
                {'channel':'web','skipped':True,'error_code':'browser_admission_timeout','skip_reason':'browser_admission'},
                {'channel':'api','ok':True,'elapsed_ms':100}]}
        self.job('first-local', [result]);self.engine.tick(self.now,True)
        self.job('second-local',[result],at=self.now+10);self.engine.tick(self.now+10,True)
        self.assertEqual(len(self.sent),1)
        self.assertFalse(any(e['kind']=='recovery' and e['channel']=='local' for e in self.engine.state['outbox']))

    def test_api_local_skip_preserves_actual_web_failure_attribution(self):
        self.job('actual-web',[{'site_id':'yuanbao','ok':False,'account_id':'acct_called','error_code':'platform_busy',
            'web_error_code':'platform_busy','api_error_code':'insufficient_api_budget','attempts':[
                {'channel':'web','account_id':'acct_called','ok':False,'error_code':'platform_busy','elapsed_ms':500},
                {'channel':'api','account_id':'acct_called','skipped':True,'error_code':'insufficient_api_budget','skip_reason':'insufficient_api_budget'}]}],'failed')
        view=self.engine.tick(self.now,True)['windows']['1h']
        self.assertEqual((view['channels']['web']['failed'],view['channels']['api']['failed'],view['channels']['api']['skipped']),(1,0,1))
        self.assertEqual(next(a for a in view['accounts'] if a['account_id']=='acct_called')['last_error_code'],'platform_busy')
        active=[i for i in self.engine.state['incidents'].values() if i['active']]
        self.assertTrue(any(i['channel']=='web' and i['code']=='platform_busy' for i in active))
        self.assertFalse(any(i['channel']=='api' for i in active))

    def test_explicit_api_skip_metadata_is_honored_without_attempt_ledger(self):
        result=m.sanitize_job({'job_id':'api-skip','results':[{'site_id':'doubao','ok':False,
            'web_skipped':True,'error_code':'insufficient_web_budget','api_error_code':'insufficient_api_budget',
            'api_skipped':True,'api_skip_reason':'insufficient_api_budget','api_elapsed_ms':0}]})['results'][0]
        self.assertTrue(all(a['skipped'] and a['ok'] is None for a in result['attempts']))
        self.assertEqual(result['attempts'][-1]['skip_reason'],'insufficient_api_budget')
        for code in ('generation_timeout','answer_extract_timeout','share_timeout'):
            self.assertNotIn(code,m.LOCAL_SCHEDULING_CODES)
            self.assertNotEqual(m.error_label(code),'未知错误')

    def test_optional_stage_quality_keeps_answer_success_and_deduplicates_reason_changes(self):
        self.engine.state['initialized']=True
        base={'site_id':'doubao','ok':True,'source':'dom','answer':'完整回答已经保存','elapsed_ms':800,
              'share_status':'generation_failed','share_error_code':'share_generation_failed'}
        self.job('optional-first',[{**base,'optional_stage_error_code':'answer_extract_timeout'}])
        view=self.engine.tick(self.now,True)['windows']['1h']
        self.assertEqual((view['channels']['web']['success'],view['channels']['web']['failed']),(1,0))
        self.assertEqual(view['success_rate'],100)
        self.assertTrue(self.engine.state['incidents']['doubao/quality/answer_extract_timeout']['active'])
        self.assertTrue(all(i['channel']=='quality' and i['severity']=='warning' for i in self.engine.state['incidents'].values() if i['active']))
        self.job('optional-second',[{**base,'optional_stage_error_code':'share_timeout'}],at=self.now+10)
        view=self.engine.tick(self.now+10,True)['windows']['1h']
        self.assertEqual((view['channels']['web']['success'],view['channels']['web']['failed']),(2,0))
        self.assertEqual(len(self.sent),1)
        self.assertIn('正文提取超时',self.sent[0])
        self.assertTrue(self.engine.state['incidents']['doubao/quality/share_timeout']['active'])
        index=json.loads((self.root/'data/monitor-requests.json').read_text(encoding='utf-8'))
        result=index['rows'][0]['results'][0]
        self.assertEqual((result['optional_stage_error_code'],result['share_error_code']),('share_timeout','share_generation_failed'))
        unsafe=m.sanitize_job({'job_id':'unsafe-optional','results':[{'site_id':'doubao','optional_stage_error_code':'../../SECRET','share_error_code':'raw SECRET'}]})['results'][0]
        self.assertEqual((unsafe['optional_stage_error_code'],unsafe['share_error_code']),('unknown','unknown'))
        self.assertNotIn('SECRET',json.dumps(unsafe))

    def test_legacy_share_failure_still_uses_one_quality_incident(self):
        self.job('old-share',[{'site_id':'yuanbao','ok':True,'source':'dom','share_status':'generation_failed'}])
        self.engine.tick(self.now,True)
        active=[i for i in self.engine.state['incidents'].values() if i['active']]
        self.assertEqual([(i['channel'],i['code']) for i in active],[('quality','share_failed')])

    def test_web_first_estimates_never_trigger_hard_deadline_alerts(self):
        past=m.dt.datetime.fromtimestamp(self.now-120,m.TZ).isoformat()
        for status in ('queued','running'):
            doc={'job_id':'web-first-'+status,'status':status,'created_at':past,'updated_at':past,
                 'deadline_at':past,'deadline_policy':'web_first','sites':['doubao'],'results':[]}
            (self.root/'data/jobs'/(doc['job_id']+'.json')).write_text(json.dumps(doc),encoding='utf-8')
        self.engine.issue('service','request','job_timeout','web-first-running',self.now-100)
        view=self.engine.tick(self.now,True)
        self.assertFalse(any(i['active'] and i['code']=='job_timeout' for i in self.engine.state['incidents'].values()))
        self.assertEqual(self.engine.state['incidents']['service/request/job_timeout']['retired_reason'],'deadline_is_estimate')
        index=json.loads((self.root/'data/monitor-requests.json').read_text(encoding='utf-8'))
        self.assertTrue(all(r['deadline_policy']=='web_first' for r in index['rows']))
        self.assertEqual((view['windows']['1h']['counts']['queued'],view['windows']['1h']['counts']['running']),(1,1))
        legacy={'job_id':'legacy-overdue','status':'running','created_at':past,'updated_at':past,'deadline_at':past,'results':[]}
        (self.root/'data/jobs/legacy-overdue.json').write_text(json.dumps(legacy),encoding='utf-8')
        self.engine.tick(self.now+1,True)
        self.assertTrue(self.engine.state['incidents']['service/request/job_timeout']['active'])
        self.assertEqual(self.engine.state['incidents']['service/request/job_timeout']['job'],'legacy-overdue')

    def test_recovery_metadata_is_allowlisted_without_content_or_account_secrets(self):
        raw={'job_id':'recovered','prompt':'SECRET_PROMPT','recovery_states':{
            'doubao':{'phase':'verifying','account_id':'acct_live','cli_call_id':'call_live','error_code':'cli_result_unconfirmed',
                      'updated_at':m.dt.datetime.fromtimestamp(self.now,m.TZ).isoformat(),'phone':'13800138000','raw':'SECRET_LIVE'},
            'SECRET_SITE':{'phase':'verifying'},'kimi':{'phase':'SECRET_PHASE'}},
            'results':[{'site_id':'doubao','ok':True,'answer':'SECRET_ANSWER','web_recovery':{
                'status':'succeeded','attempts':1,'cli_call_id':'call_done','verified':True,'resumed':True,'cli_assisted':True,'elapsed_ms':2500,
                'prompt':'SECRET_RECOVERY','phone':'13800138000','raw_output':'SECRET_OUTPUT'}}]}
        row=m.sanitize_job(raw,{'acct_live':'手机尾号 8000'})
        self.assertEqual(row['recovery_states']['doubao']['account_label'],'手机尾号 8000')
        self.assertEqual(row['recovery_states']['doubao']['updated_at'],self.now)
        self.assertEqual(list(row['recovery_states']),['doubao'])
        evidence=row['results'][0]['web_recovery']
        self.assertEqual((evidence['verified'],evidence['resumed'],evidence['attempts']),(True,True,1))
        self.assertIs(evidence['cli_assisted'],True)
        self.assertNotIn('SECRET',json.dumps(row));self.assertNotIn('13800138000',json.dumps(row))
        invalid=m.sanitize_web_recovery({'status':'failed','attempts':True,'verified':'true','resumed':False,
            'cli_call_id':'../../SECRET','elapsed_ms':float('inf'),'error_code':'SECRET raw','cli_assisted':'true'})
        self.assertEqual((invalid['attempts'],invalid['elapsed_ms'],invalid['cli_call_id'],invalid['verified']),(0,0,'',None))
        self.assertEqual(invalid['error_code'],'unknown')
        self.assertIsNone(invalid['cli_assisted'])
        self.assertIsNone(m.sanitize_web_recovery({'status':'SECRET'}))

    def test_delivery_and_recovery_denominators_use_recorded_terminal_evidence(self):
        def record(identity,source='dom',ok=True,recovery=None,status='partial'):
            result={'site_id':'doubao','source':source,'ok':ok}
            if recovery is not None:result['web_recovery']=recovery
            self.job(identity,[result],status=status)
        common={'attempts':1,'verified':True,'resumed':True,'cli_assisted':True,'status':'succeeded'}
        record('web-recovered',recovery=common)
        record('api-after-failed-resume','official_api',recovery={**common,'status':'failed','resumed':False,'cli_assisted':False})
        record('legacy-web')
        record('failed-web',ok=False,recovery={**common,'status':'failed','verified':False,'resumed':False,'cli_assisted':False})
        record('probe-without-cli',recovery={**common,'attempts':0,'cli_assisted':False})
        record('canceled-web',ok=False,recovery={**common,'status':'cancelled','resumed':False,'cli_assisted':False})
        record('unconfirmed-web',ok=False,recovery={'attempts':1,'status':'failed'})
        self.job('live-recovery',[],status='running')
        path=self.root/'data/jobs/live-recovery.json';doc=json.loads(path.read_text(encoding='utf-8'))
        doc['recovery_states']={'doubao':{'phase':'resuming','account_id':'acct_live','cli_call_id':'call_live','updated_at':self.now}}
        path.write_text(json.dumps(doc),encoding='utf-8')
        view=self.engine.tick(self.now,True)['windows']['1h']
        self.assertEqual(view['delivery'],{'total':7,'pending':0,'web_success':3,'api_success':1,'web_share':42.9,'api_fallback_rate':14.3})
        self.assertEqual(view['recovery'],{'attempted':6,'completed':3,'known_completed':3,'pending':1,'cancelled':1,'skipped':0,'unknown':1,'web_success':1,'web_success_rate':33.3})
        doubao=next(s for s in view['sites'] if s['site']=='doubao')
        self.assertEqual(doubao['delivery'],view['delivery']);self.assertEqual(doubao['recovery'],view['recovery'])
        day=self.engine.snapshot['daily'][0]
        self.assertEqual(day['recovery']['web_success_rate'],33.3)
        empty=m.summarize([],self.now,3600)
        self.assertIsNone(empty['recovery']['web_success_rate']);self.assertIsNone(empty['delivery']['web_share'])

    def test_cli_recovery_success_requires_same_account_assistance_without_historical_inference(self):
        def record(identity,evidence):
            self.job(identity,[{'site_id':'doubao','ok':True,'source':'dom','web_recovery':evidence}])
        common={'status':'succeeded','attempts':1,'verified':True,'resumed':True}
        record('old-unknown',common)
        view=self.engine.tick(self.now,True)['windows']['1h']['recovery']
        self.assertEqual((view['completed'],view['known_completed'],view['unknown'],view['web_success_rate']),(1,0,1,None))
        record('a-cli-failed-b-direct-probe',{**common,'cli_assisted':False})
        record('same-account-cli-success',{**common,'cli_assisted':True})
        view=self.engine.tick(self.now,True)['windows']['1h']['recovery']
        self.assertEqual((view['completed'],view['known_completed'],view['unknown'],view['web_success'],view['web_success_rate']),(3,2,1,1,50))
        self.assertEqual(view['attempted'],3)

    def test_waiting_sms_recovery_progress_is_retained(self):
        row=m.sanitize_job({'job_id':'sms-wait','status':'running','recovery_states':{'doubao':{
            'phase':'waiting_sms','account_id':'acct_sms','cli_call_id':'call_sms','updated_at':self.now}}})
        self.assertEqual(row['recovery_states']['doubao']['phase'],'waiting_sms')

    def test_cursor_marker_is_not_page_verification_and_resume_failure_keeps_verified_success(self):
        started=m.dt.datetime.fromtimestamp(self.now,m.TZ).isoformat()
        base={'id':'call_verified','job_id':'job','site':'kimi','account_id':'acct_cli','started_at':started,'task_type':'web_recovery'}
        candidate=m.sanitize_cursor_call({**base,'status':'success','phase':'candidate','prompt':'SECRET'})
        self.assertEqual((candidate['status'],candidate['ok'],candidate['verified']),('unknown',None,None))
        success=m.sanitize_cursor_call({**base,'status':'success','phase':'completed','verified':True,'resume_ok':False,'error_code':'cli_resume_failed'})
        self.assertEqual((success['status'],success['ok'],success['verified'],success['resume_ok'],success['error_code']),('success',True,True,False,'cli_resume_failed'))
        self.assertEqual(success['task_type'],'web_recovery')
        self.assertNotIn('SECRET',json.dumps(candidate))
        for field in ('verified','resume_ok','phase'):
            self.assertIn(field,success)
        view=m.summarize([],self.now,3600,cursor_calls=[candidate,success])
        self.assertEqual((view['channels']['cursor']['success'],view['channels']['cursor']['unknown'],view['channels']['cursor']['success_rate']),(1,1,100))

    def test_invalid_historical_documents_do_not_block_publication(self):
        folder=self.root/'data/jobs'
        (folder/'malformed.json').write_text('{',encoding='utf-8')
        (folder/'list.json').write_text('[]',encoding='utf-8')
        (folder/'bad-date.json').write_text(json.dumps({'job_id':'bad-date','created_at':'invalid'}),encoding='utf-8')
        view=self.engine.tick(self.now,True)
        self.assertEqual(view['available_dates'],[])
        self.assertEqual(view['windows']['24h']['total'],0)
        self.assertEqual(json.loads((self.root/'data/monitor-requests.json').read_text(encoding='utf-8'))['rows'],[])

if __name__=='__main__':unittest.main()
