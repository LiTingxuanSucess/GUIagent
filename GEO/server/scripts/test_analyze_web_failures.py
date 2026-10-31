import json
from pathlib import Path
import tempfile
import unittest

import analyze_web_failures as audit


class WebFailureAuditTests(unittest.TestCase):
    def test_counts_attempt_dates_keeps_retries_and_separates_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'data').mkdir()
            start=1792425600
            row={'id':'job_a','created':start-86400,'status':'succeeded','results':[
                {'site':'doubao','source':'api','ok':True,'attempts':[
                    {'channel':'web','status':'failed','error_code':'site_timeout','started_at':start+60,'elapsed_ms':1000},
                    {'channel':'web','status':'failed','error_code':'platform_risk_control','started_at':start+90,'elapsed_ms':1000},
                    {'channel':'web','status':'failed','skipped':True,'error_code':'browser_admission_timeout','started_at':start+100},
                    {'channel':'web','status':'unknown','started_at':start+110},
                    {'channel':'web','status':'success','started_at':start+120},
                    {'channel':'api','status':'failed','started_at':start+130},
                    {'channel':'web','status':'failed','error_code':'site_timeout','started_at':start+86400},
                ]}]}
            (root/'data/monitor-requests.json').write_text(json.dumps({'updated_at':start+90000,'rows':[row]}),encoding='utf-8')
            failures,skipped,summary=audit.analyze(root,'2026-10-20','2026-10-21')
            self.assertEqual(len(failures),2)
            self.assertEqual(len(skipped),1)
            self.assertEqual(summary['daily']['2026-10-20'],{'failed':2,'skipped':1,'unknown':1,'success':1})
            self.assertEqual(summary['unique_affected_jobs'],1)
            self.assertEqual(summary['final_sources_by_job_site'],{'api':1})

    def test_log_evidence_distinguishes_browser_crash_and_account_mute(self):
        self.assertIn('页面崩溃',audit.signal_label('Locator.count: Target crashed'))
        self.assertIn('禁言',audit.signal_label('DeepSeek 账号已被禁言至 2026-10-22'))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'server.log'
            path.write_text('2026/10/20 00:01:00 geo-server scrape diagnostic job=job_a site=yiyan diagnostic_id=job_a-yiyan-1792425600000000000 status=failed error_code=site_request_failed elapsed_ms=1000\n',encoding='utf-8')
            diagnostics,failures=audit.read_log_evidence(path)
            self.assertEqual(diagnostics[('job_a','wenxinyiyan')][0]['error_code'],'site_request_failed')
            self.assertFalse(failures)


if __name__=='__main__':unittest.main()
