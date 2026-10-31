"""Exercise channel/account drilldowns and recoverable errors in Chromium."""
import copy
import datetime as dt
import json
import os
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright


class MonitorBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True,
                                            args=['--no-sandbox', '--disable-dev-shm-usage'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.calls = []
        self.login_start_count = 0
        self.login_start_error = False
        self.login_task = {'status': 'idle', 'total': 0, 'completed': 0, 'results': []}
        self.actions = {'updated_at': time.time(), 'manual': 0, 'automatic': 0, 'rows': []}
        self.cursor_review = {'rows': [], 'total': 0, 'failure_reasons': {}}
        self.cursor_offline = False
        self.routing_policy = {'web_first': True, 'mode': 'web_first', 'revision': 0,
                               'sites': {site: {'web_first': True, 'mode': 'web_first'} for site in
                                         ['doubao', 'deepseek', 'kimi', 'qianwen', 'wenxinyiyan', 'yuanbao']}}
        self.routing_save_conflict = False
        self.routing_save_error = False
        self.routing_save_lost_response = False
        self.routing_read_error = False
        self.routing_writes = []
        self.login_counts = [{'site_id': site, 'name': name, 'logged_in': 0, 'enabled': 2}
                             for site, name in [('deepseek', 'DeepSeek'), ('doubao', '豆包'), ('kimi', 'Kimi'),
                                                ('qianwen', '千问'), ('yiyan', '文心一言'), ('yuanbao', '元宝')]]
        self.offline = False
        self.now = int(time.time())
        self.today = dt.datetime.fromtimestamp(self.now, dt.timezone(dt.timedelta(hours=8))).date()
        self.old_date = str(self.today - dt.timedelta(days=1))
        self.older_date = str(self.today - dt.timedelta(days=2))
        self.html = Path(os.environ.get('GEO_TEST_MONITOR_HTML', str(Path(__file__).resolve().parents[1] / 'web/monitor.html'))).read_text(encoding='utf-8')
        self.detail = {'id': 'job_00', 'request_id': 'external_request_01', 'prompt': '完整的测试问题\n第二行',
                       'content_available': True, 'status': 'succeeded', 'created': self.now-30, 'updated': self.now,
                       'finished': self.now, 'total_ms': 1200, 'sites': ['yuanbao'], 'results': [{
                           'site': 'yuanbao', 'ok': True, 'source': 'api', 'account_id': 'api:yuanbao',
                           'account_label': '元宝 API', 'citations': 3, 'web_ms': 500, 'api_ms': 700,
                           'answer': '完整的测试回答\n保留换行。', 'share_url': 'https://share.example.test/answer/123',
                           'citation_details': [{'url': 'https://source.example.test/report', 'title': '搜索来源报告', 'site_name': '测试来源'}],
                           'web_error_code': 'platform_quota', 'web_search_status': 'used', 'share_status': 'not_supported',
                           'attempts': [
                               {'channel': 'web', 'account_id': 'acct_first', 'account_label': '手机尾号 8351',
                                'ok': False, 'status': 'failed', 'error_code': 'platform_quota', 'started_at': self.now-20, 'elapsed_ms': 200},
                               {'channel': 'web', 'account_id': 'acct_second', 'account_label': '手机尾号 7132',
                                'ok': True, 'status': 'success', 'error_code': '', 'started_at': self.now-15, 'elapsed_ms': 300},
                               {'channel': 'api', 'account_id': 'api:yuanbao', 'account_label': '元宝 API',
                                'ok': True, 'status': 'success', 'error_code': '', 'started_at': self.now-10, 'elapsed_ms': 700},
                           ]}]}
        account = {'account_id': 'acct_first', 'account_label': '手机尾号 8351', 'site': 'yuanbao', 'channel': 'web',
                   'total': 1, 'success': 0, 'failed': 1, 'skipped': 0, 'unknown': 0, 'success_rate': 0,
                   'last_error_code': 'platform_quota', 'last_at': self.now}
        view = {'total': 61, 'counts': {'succeeded': 61, 'failed': 0, 'partial': 0, 'queued': 0, 'running': 0},
                'success_rate': 100, 'p95_ms': 1200, 'p50_ms': 1000, 'trend': [],
                'channels': {'web': {'total': 2, 'success': 1, 'failed': 1, 'skipped': 1, 'unknown': 1, 'success_rate': 50},
                             'api': {'total': 1, 'success': 1, 'failed': 0, 'skipped': 0, 'unknown': 0, 'success_rate': 100},
                             'cursor': {'total': 5, 'success': 2, 'failed': 1, 'skipped': 0, 'unknown': 1, 'running': 1,
                                        'success_rate': 66.7, 'p50_ms': 5000, 'p95_ms': 12000, 'avg_ms': 6500}},
                'sites': [{'site': 'yuanbao', 'name': '元宝', 'total': 61, 'success': 61, 'success_rate': 100,
                           'web_total': 2, 'web_success': 1, 'web_failed': 1, 'web_skipped': 1, 'web_unknown': 1, 'web_success_rate': 50,
                           'api_total': 1, 'api_success': 1, 'api_failed': 0, 'api_success_rate': 100,
                           'cursor_total': 5, 'cursor_success': 2, 'cursor_failed': 1, 'cursor_unknown': 1,
                           'cursor_running': 1, 'cursor_success_rate': 66.7, 'cursor_p50_ms': 5000, 'cursor_p95_ms': 12000}],
                'accounts': [account, {**account, 'account_id': '', 'account_label': '历史账号未知'}]}
        self.snapshot = {'updated_at': self.now, 'server_time': self.now+2, 'snapshot_age_seconds': 2, 'poll_interval_seconds': 10,
                         'service_healthy': True, 'alerts_enabled': False,
                         'dedup_seconds': 900, 'warning_dedup_seconds': 3600, 'delivery': {}, 'active_alerts': [],
                         'notifications': [], 'labels': {'platform_quota': '网页额度不足'},
                         'available_dates': [str(self.today), self.old_date, self.older_date],
                         'daily': [{'date': str(self.today), 'total': 61, 'success_rate': 100,
                                    'p50_ms': 1000, 'p95_ms': 1200, 'channels': copy.deepcopy(view['channels'])},
                                   {'date': self.old_date, 'total': 12, 'success_rate': 75,
                                    'p50_ms': 2000, 'p95_ms': 4000, 'channels': copy.deepcopy(view['channels'])}],
                         'windows': {key: copy.deepcopy(view) for key in ['today', 'afternoon', '15m', '1h', '6h', '24h']}}
        self.day_views = {self.old_date: {**copy.deepcopy(view), 'total': 12, 'success_rate': 75},
                          self.older_date: {**copy.deepcopy(view), 'total': 7, 'success_rate': 57.1}}
        self.page.route('http://monitor.test/**', self.route)

    def test_quota_panel_distinguishes_reset_recheck_and_verified_recovery(self):
        self.actions['account_limits'] = [
            {'account_id': 'acct_unknown', 'account_label': '手机尾号 7132', 'site': 'kimi',
             'kind': 'quota_exhausted', 'status': 'pending_recheck', 'source': 'history',
             'first_detected_at': '2026-10-20T08:10:00+08:00', 'last_detected_at': '2026-10-21T09:00:00+08:00',
             'reset_at': '0001-01-01T00:00:00Z', 'next_probe_at': '2026-10-21T09:30:00+08:00', 'detection_count': 34},
            {'account_id': 'acct_muted', 'account_label': '手机尾号 8351', 'site': 'deepseek',
             'kind': 'account_muted', 'status': 'blocked', 'source': 'history',
             'first_detected_at': '2026-10-20T00:00:00Z', 'last_detected_at': '2026-10-21T08:00:00+08:00',
             'reset_at': '2026-10-28T23:52:00+08:00', 'next_probe_at': '2026-10-28T23:52:00+08:00'},
            {'account_id': 'acct_recovered', 'account_label': '<script>bad()</script>', 'site': 'kimi',
             'kind': 'quota_exhausted', 'status': 'recovered', 'recovered_at': '2026-10-21T10:00:00+08:00'}]
        self.page.goto('http://monitor.test/admin/monitor')
        rows = self.page.locator('#limitRows')
        rows.wait_for()
        self.page.wait_for_function("document.querySelector('#limitSummary').textContent.includes('尚未确认恢复 2 项')")
        self.assertEqual(rows.locator('tr').count(), 2)
        text = rows.inner_text()
        self.assertIn('额度耗尽', text)
        self.assertIn('账号禁言', text)
        self.assertIn('平台未明确', text)
        self.assertIn('2026/10/20 08:10:00', text)
        self.assertIn('2026/10/28 23:52:00', text)
        self.assertNotIn('0001-', text)
        self.assertIn('到期待复查，尚未确认恢复', text)
        self.page.select_option('#limitFilter', 'recovered')
        self.assertEqual(rows.locator('tr').count(), 1)
        self.assertIn('网页调用成功，已确认恢复', rows.inner_text())
        self.assertEqual(rows.locator('script').count(), 0)
        self.assertIn('<script>bad()</script>', rows.inner_text())
        self.assertTrue(all(path in ('/admin/monitor', '/api/admin/monitor/actions', '/api/admin/monitor/cursor',
                                      '/api/admin/login-counts', '/api/admin/login-check', '/api/admin/monitor',
                                      '/api/admin/monitor/requests', '/api/admin/request-routing', '/api/v1/sites', '/favicon.ico') for path, _ in self.calls))

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def test_sms_submission_uncertainty_stays_in_manual_list(self):
        self.actions.update(manual=1, rows=[{
            'account_id':'acct_first','account_label':'手机尾号 8351','site':'kimi','kind':'manual',
            'reason':'短信自动接续未完成，需要检查原页面','next_action':'查看原页面',
            'sms_state':'received_unconfirmed','handoff_stage':'submission_unknown',
            'handoff_updated_at':'2026-10-21T10:00:00+08:00'}])
        self.page.goto('http://monitor.test/admin/monitor')
        self.page.wait_for_function("document.querySelector('#actionRows').textContent.includes('提交结果未知') || document.querySelector('#actionRows').textContent.includes('结果未知')")
        self.assertIn('提交期间服务中断，结果未知，请检查原页面', self.page.locator('#actionRows').inner_text())
        self.assertEqual(self.page.locator('#actionRows a').get_attribute('href'),
                         '/admin?account=acct_first&site=kimi&inspect=1')

    def route(self, route):
        u = urlsplit(route.request.url)
        query = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append((u.path, query))
        if u.path == '/admin/monitor':
            route.fulfill(content_type='text/html', body=self.html)
            return
        if self.offline and u.path == '/api/admin/monitor/requests':
            route.abort('failed')
            return
        if u.path == '/api/admin/request-routing':
            if route.request.method == 'PUT':
                body = route.request.post_data_json
                self.routing_writes.append(body)
                if self.routing_save_error:
                    route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': '保存失败'}))
                    return
                if self.routing_save_conflict:
                    self.routing_policy['sites']['kimi'] = {'web_first': False, 'mode': 'api_only'}
                    self.routing_policy['revision'] += 1
                    route.fulfill(status=409, content_type='application/json', body=json.dumps({'detail': '设置已被其他管理员修改'}))
                    return
                self.routing_policy['sites'][body['site_id']] = {'web_first': body['web_first'], 'mode': 'web_first' if body['web_first'] else 'api_only'}
                self.routing_policy['revision'] += 1
                modes = {s['mode'] for s in self.routing_policy['sites'].values()}
                self.routing_policy['mode'] = next(iter(modes)) if len(modes) == 1 else 'mixed'
                if self.routing_save_lost_response:
                    route.abort('failed')
                    return
            elif self.routing_read_error:
                route.abort('failed')
                return
            data = copy.deepcopy(self.routing_policy)
        elif u.path == '/api/admin/monitor/actions':
            data = copy.deepcopy(self.actions)
        elif u.path == '/api/admin/monitor/cursor':
            if self.cursor_offline:
                route.abort('failed')
                return
            data = copy.deepcopy(self.cursor_review)
            if query.get('outcome'):
                data['rows'] = [r for r in data['rows'] if r['status'] == query['outcome']]
                data['total'] = len(data['rows'])
            if query.get('error'):
                data['rows'] = [r for r in data['rows'] if r.get('error_code') == query['error']]
                data['total'] = len(data['rows'])
        elif u.path == '/api/admin/login-counts':
            data = {'sites': self.login_counts}
        elif u.path == '/api/admin/login-check':
            if route.request.method == 'POST':
                self.login_start_count += 1
                if self.login_start_error:
                    route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': '登录检测暂不可用'}))
                    return
                self.login_task = {'id': 'login-fixture', 'status': 'running', 'total': 3, 'completed': 0,
                                   'results': [{'account_id': 'acct_first', 'account_label': '手机尾号 8351',
                                                'site_id': site, 'status': 'checking' if i == 0 else 'pending'}
                                               for i, site in enumerate(['kimi', 'doubao', 'qianwen'])]}
            data = self.login_task
        elif u.path == '/api/admin/monitor':
            data = copy.deepcopy(self.snapshot)
            if query.get('window') == 'day':
                data['windows']['day'] = self.day_views.get(query.get('date'), copy.deepcopy(data['windows']['today']))
        elif u.path == '/api/admin/monitor/requests':
            page, size = int(query.get('page', 1)), int(query.get('page_size', 25))
            total = 2 if query.get('account') else 61
            rows = [{**self.detail, 'id': f'job_{i:02d}'} for i in range((page-1)*size, min(page*size, total))]
            data = {'updated_at': self.now, 'total': total, 'page': page, 'page_size': size, 'rows': rows}
        elif u.path.startswith('/api/admin/monitor/requests/'):
            data = self.detail
        else:
            route.fulfill(status=404, body='not found')
            return
        route.fulfill(content_type='application/json', body=json.dumps(data, ensure_ascii=False))

    def open(self):
        self.page.goto('http://monitor.test/admin/monitor')
        self.page.wait_for_function("document.querySelectorAll('#requests [data-request-detail]').length === 25 && !document.querySelector('#requests').hasAttribute('aria-busy')")

    def test_login_check_progress_duplicate_click_reload_and_sms_handoff(self):
        self.open()
        self.page.locator('#checkLogins').click()
        self.page.wait_for_function("document.querySelector('#loginCheckStatus').textContent.includes('检测进行中')")
        self.assertTrue(self.page.locator('#checkLogins').is_disabled())
        self.page.evaluate("document.querySelector('#checkLogins').click()")
        self.assertEqual(self.login_start_count, 1)
        self.page.reload()
        self.page.wait_for_function("document.querySelector('#checkLogins').disabled")
        self.assertEqual(self.login_start_count, 1)
        rows = copy.deepcopy(self.login_task['results'])
        rows[0].update(status='logged_in', detail='已通过登录检测')
        rows[1].update(status='waiting_sms', cli_used=True, detail='等待短信确认 <img src=x onerror=alert(1)>')
        rows[2].update(status='busy', detail='账号正在执行业务，本次跳过')
        self.login_task.update(status='completed', completed=3, results=rows)
        self.login_counts[2]['logged_in'] = 1
        self.page.evaluate('loadLoginCheck()')
        self.page.wait_for_function("document.querySelector('#loginCheckStatus').textContent.includes('本轮检测完成') && !document.querySelector('#checkLogins').disabled")
        status = self.page.locator('#loginCheckStatus').inner_text()
        for expected in ['3 / 3', '已登录 1', '待短信 1', '忙跳过 1']:
            self.assertIn(expected, status)
        self.page.wait_for_function("document.querySelectorAll('#loginAccountCards .number')[2].textContent==='1'")
        self.page.locator('#loginCheckResults summary').click()
        self.assertIn('等待短信', self.page.locator('#loginCheckRows').inner_text())
        self.assertIn('已执行 CLI 辅助', self.page.locator('#loginCheckRows').inner_text())
        self.assertEqual(self.page.locator('#loginCheckRows img').count(), 0)
        self.page.locator('#loginAccounts').screenshot(path='/tmp/geo-monitor-login-check.png')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(self.page.locator('#checkLogins').is_visible())
        self.assertLessEqual(self.page.locator('#checkLogins').bounding_box()['x'] + self.page.locator('#checkLogins').bounding_box()['width'], 390)

    def test_login_check_submission_failure_allows_retry(self):
        self.login_start_error = True
        self.open()
        self.page.locator('#checkLogins').click()
        self.page.wait_for_function("document.querySelector('#loginCheckStatus').textContent.includes('启动检测未成功')")
        self.assertFalse(self.page.locator('#checkLogins').is_disabled())
        self.assertIn('登录检测暂不可用', self.page.locator('#loginCheckStatus').inner_text())
        self.login_start_error = False
        self.page.locator('#checkLogins').click()
        self.page.wait_for_function("document.querySelector('#loginCheckStatus').textContent.includes('检测进行中')")
        self.assertEqual(self.login_start_count, 2)

    def test_separate_rates_failed_account_and_lazy_detail(self):
        self.open()
        self.page.screenshot(path='/tmp/geo-monitor-desktop-preview.png', full_page=True)
        self.assertIn('50%', self.page.locator('#webRate').inner_text())
        self.assertIn('100%', self.page.locator('#apiRate').inner_text())
        self.assertIn('跳过', self.page.locator('#webStats').inner_text())
        self.assertFalse(any('/requests/' in path for path, _ in self.calls))
        self.page.locator('[data-account-filter="acct_first"]').click()
        self.page.wait_for_function("document.querySelector('#pageInfo').textContent.includes('共 2 个')")
        query = [q for p, q in self.calls if p == '/api/admin/monitor/requests'][-1]
        self.assertEqual({k: query.get(k) for k in ['channel', 'account', 'site', 'outcome']},
                         {'channel': 'web', 'account': 'acct_first', 'site': 'yuanbao', 'outcome': 'failed'})
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_function("document.querySelectorAll('#detailBody .attempts tbody tr').length === 3")
        text = self.page.locator('#detailBody').inner_text()
        for expected in ['acct_first', 'acct_second', 'api:yuanbao', 'platform_quota']:
            self.assertIn(expected, text)
        self.page.locator('#closeDetail').click()
        self.assertFalse(self.page.locator('#requestDetail').is_visible())
        self.page.locator('[data-account-filter="unknown"]').click()
        self.page.wait_for_function("!document.querySelector('#requests').hasAttribute('aria-busy')")
        query = [q for p, q in self.calls if p == '/api/admin/monitor/requests'][-1]
        self.assertEqual(query.get('account'), 'unknown')

    def test_platform_web_rates_exclude_api_and_skips_and_filter_failures(self):
        view = self.snapshot['windows']['today']
        view['sites'].extend([
            {'site': 'doubao', 'web_success': 3, 'web_failed': 1, 'web_total': 500,
             'web_skipped': 400, 'web_unknown': 96, 'api_success': 99, 'success_rate': 100},
            {'site': 'deepseek', 'web_success': 0, 'web_failed': 2},
            {'site': 'kimi', 'web_skipped': 20, 'web_unknown': 4},
        ])
        self.open()
        cards = self.page.locator('#platformWebCards [data-web-platform]')
        self.assertEqual(cards.count(), 6)
        doubao = self.page.locator('[data-web-platform="doubao"]')
        self.assertEqual(doubao.locator('[data-web-rate]').inner_text(), '75%')
        self.assertIn('结果明确 4 次', doubao.inner_text())
        self.assertIn('跳过 400 · 历史未知 96', doubao.inner_text())
        self.assertEqual(self.page.locator('[data-web-platform="deepseek"] [data-web-rate]').inner_text(), '0%')
        self.assertEqual(self.page.locator('[data-web-platform="kimi"] [data-web-rate]').inner_text(), '—')
        self.assertTrue(self.page.locator('[data-platform-web-failed="kimi"]').is_disabled())
        self.assertEqual(self.page.locator('[data-web-platform="yuanbao"] [data-web-rate]').inner_text(), '50%')
        self.page.locator('[data-platform-web-failed="doubao"]').click()
        self.page.wait_for_function("!document.querySelector('#requests').hasAttribute('aria-busy')")
        query = [q for path, q in self.calls if path == '/api/admin/monitor/requests'][-1]
        self.assertEqual({k: query.get(k) for k in ['site', 'channel', 'outcome']},
                         {'site': 'doubao', 'channel': 'web', 'outcome': 'failed'})
        self.page.locator('#platformWebPanel').screenshot(path='/tmp/geo-platform-web-rates-desktop.png')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        for card in cards.all():
            box = card.bounding_box()
            self.assertGreaterEqual(box['x'], 0)
            self.assertLessEqual(box['x'] + box['width'], 390)

    def test_platform_web_rates_follow_date_and_empty_window(self):
        self.day_views[self.old_date]['sites'] = [{'site': 'doubao', 'web_success': 9, 'web_failed': 1}]
        self.day_views[self.older_date]['sites'] = []
        self.open()
        self.page.locator('#date').fill(self.old_date)
        self.page.locator('#date').dispatch_event('change')
        self.page.wait_for_function("document.querySelector('[data-web-platform=\"doubao\"] [data-web-rate]')?.textContent === '90%'")
        self.assertIn(self.old_date, self.page.locator('#platformWebRange').inner_text())
        self.page.locator('#date').fill(self.older_date)
        self.page.locator('#date').dispatch_event('change')
        self.page.wait_for_function("document.querySelector('#platformWebRange').textContent.includes('" + self.older_date + "')")
        self.assertEqual(self.page.locator('#platformWebCards [data-web-rate]').all_inner_texts(), ['—'] * 6)
        self.assertEqual(self.page.locator('#platformWebCards button:disabled').count(), 6)

    def test_routing_switch_is_independent_per_platform_and_survives_reload(self):
        self.open()
        toggle = self.page.locator('[data-site-routing="deepseek"]')
        self.page.wait_for_function("document.querySelectorAll('[data-site-routing]:not(:disabled)').length===6")
        self.assertTrue(toggle.is_checked())
        toggle.uncheck()
        self.page.wait_for_function("document.querySelector('[data-site-routing-mode=deepseek]').textContent==='直接调用 API'")
        self.assertEqual(self.routing_writes, [{'site_id': 'deepseek', 'web_first': False, 'revision': 0}])
        for site in ['doubao', 'kimi', 'qianwen', 'wenxinyiyan', 'yuanbao']:
            self.assertTrue(self.page.locator(f'[data-site-routing="{site}"]').is_checked())
        self.page.locator('[data-site-routing="wenxinyiyan"]').uncheck()
        self.page.wait_for_function("document.querySelectorAll('[data-site-routing]:not(:disabled)').length===6")
        self.assertEqual(self.routing_writes[-1], {'site_id': 'wenxinyiyan', 'web_first': False, 'revision': 1})
        self.page.reload()
        self.page.wait_for_function("document.querySelectorAll('[data-site-routing]:not(:disabled)').length===6")
        self.assertFalse(toggle.is_checked())
        self.assertFalse(self.page.locator('[data-site-routing="wenxinyiyan"]').is_checked())
        toggle.check()
        self.page.wait_for_function("document.querySelectorAll('[data-site-routing]:not(:disabled)').length===6")
        self.assertEqual(self.routing_writes[-1], {'site_id': 'deepseek', 'web_first': True, 'revision': 2})
        self.assertFalse(self.page.locator('[data-site-routing="wenxinyiyan"]').is_checked())
        self.page.locator('#platformWebPanel').screenshot(path='/tmp/geo-platform-routing-desktop.png')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.locator('#date').fill(self.old_date)
        self.page.locator('#date').dispatch_event('change')
        self.page.wait_for_function("document.querySelector('#platformWebRange').textContent.includes('" + self.old_date + "')")
        self.assertTrue(toggle.is_checked())
        self.assertFalse(self.page.locator('[data-site-routing="wenxinyiyan"]').is_checked())
        for control in self.page.locator('[data-site-routing]').all():
            control.scroll_into_view_if_needed()
            box = control.bounding_box()
            self.assertGreaterEqual(box['x'], 0)
            self.assertLessEqual(box['x'] + box['width'], 390)

    def test_routing_save_failure_or_lost_response_reconciles_without_repeating_write(self):
        self.open()
        toggle = self.page.locator('[data-site-routing="deepseek"]')
        self.page.wait_for_function("document.querySelectorAll('[data-site-routing]:not(:disabled)').length===6")
        self.routing_save_error = True
        toggle.click()
        self.page.wait_for_function("document.querySelectorAll('[data-site-routing]:not(:disabled)').length===6")
        self.assertTrue(toggle.is_checked())
        self.assertTrue(self.routing_policy['sites']['deepseek']['web_first'])
        self.routing_save_error = False
        self.routing_save_lost_response = True
        toggle.uncheck()
        self.page.wait_for_function("document.querySelectorAll('[data-site-routing]:not(:disabled)').length===6")
        self.assertFalse(toggle.is_checked())
        self.assertEqual(len(self.routing_writes), 2)
        self.assertTrue(self.page.locator('[data-site-routing="doubao"]').is_checked())

    def test_routing_conflict_reloads_other_platform_change_without_replaying(self):
        self.open()
        self.page.wait_for_function("document.querySelectorAll('[data-site-routing]:not(:disabled)').length===6")
        self.routing_save_conflict = True
        self.page.locator('[data-site-routing="deepseek"]').click()
        self.page.wait_for_function("document.querySelector('#routingStatus').textContent.includes('本次切换未生效')")
        self.assertEqual(len(self.routing_writes), 1)
        self.assertTrue(self.page.locator('[data-site-routing="deepseek"]').is_checked())
        self.assertFalse(self.page.locator('[data-site-routing="kimi"]').is_checked())

    def test_routing_unavailable_disables_mutation_without_hiding_metrics(self):
        self.routing_read_error = True
        self.open()
        self.page.wait_for_function("document.querySelector('#routingStatus').textContent.includes('当前模式未确认')")
        self.assertEqual(self.page.locator('[data-site-routing]:disabled').count(), 6)
        self.assertEqual(self.page.locator('#platformWebCards article').count(), 6)
        self.assertEqual(self.routing_writes, [])

    def test_pagination_failure_preserves_page_then_recovers(self):
        self.open()
        self.offline = True
        self.page.locator('#nextPage').click()
        self.page.wait_for_function("document.querySelector('#requestNotice').textContent.includes('读取失败')")
        self.assertIn('第 1 /', self.page.locator('#pageInfo').inner_text())
        self.offline = False
        self.page.locator('#nextPage').click()
        self.page.wait_for_function("document.querySelector('#pageInfo').textContent.includes('第 2 /')")
        self.assertEqual(self.page.locator('[data-request-detail]').first.get_attribute('data-request-detail'), 'job_25')
        self.page.locator('#window').select_option('afternoon')
        self.page.wait_for_function("document.querySelector('#pageInfo').textContent.includes('第 1 /')")
        self.assertEqual([q for p, q in self.calls if p == '/api/admin/monitor/requests'][-1]['window'], 'afternoon')

    def test_mobile_layout_and_untrusted_text(self):
        self.snapshot['windows']['today']['accounts'][0]['account_label'] = '<img src=x onerror="window.injected=1">'
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.open()
        self.assertIsNone(self.page.evaluate('window.injected'))
        self.assertEqual(self.page.locator('#accounts img').count(), 0)
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
        self.page.screenshot(path='/tmp/geo-monitor-mobile-preview.png', full_page=True)

    def test_client_clock_skew_does_not_report_stale_monitor(self):
        self.page.add_init_script("const realNow=Date.now.bind(Date); Date.now=()=>realNow()+300000;")
        self.open()
        self.page.wait_for_function("document.querySelector('#freshness').dataset.state==='fresh'")
        self.assertEqual(self.page.locator('#notice').inner_text(), '')
        self.assertEqual(self.page.locator('#freshness').get_attribute('data-age-source'), 'server')
        self.assertLess(float(self.page.locator('#freshness').get_attribute('data-age-seconds')), 10)
        summaries = [q for path, q in self.calls if path == '/api/admin/monitor']
        self.assertEqual(summaries[-1].get('window'), 'today')

    def test_actual_stale_snapshot_warns_and_refresh_recovers(self):
        self.snapshot['snapshot_age_seconds'] = 50
        self.open()
        self.page.wait_for_function("document.querySelector('#freshness').dataset.state==='stale'")
        self.assertIn('40 秒', self.page.locator('#notice').inner_text())
        self.snapshot['snapshot_age_seconds'] = 1
        self.page.locator('#refresh').click()
        self.page.wait_for_function("document.querySelector('#freshness').dataset.state==='fresh'")
        self.assertEqual(self.page.locator('#notice').inner_text(), '')

    def test_hung_summary_times_out_and_automatic_poll_recovers(self):
        self.page.clock.install()
        self.page.add_init_script("""(() => {
          const original=window.fetch.bind(window);let hang=true;
          window.fetch=(input,options={})=>{
            if(hang&&String(input).startsWith('/api/admin/monitor?')){
              hang=false;
              return new Promise((resolve,reject)=>{
                options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
              });
            }
            return original(input,options);
          };
        })();""")
        self.open()
        self.page.clock.fast_forward(16000)
        self.page.wait_for_function("document.querySelector('#notice').textContent.includes('读取超时')")
        self.assertFalse(self.page.locator('#refresh').is_disabled())
        self.page.clock.fast_forward(11000)
        self.page.wait_for_function("document.querySelector('#freshness').dataset.state==='fresh'")
        self.assertEqual(self.page.locator('#notice').inner_text(), '')

    def test_window_switch_cancels_hung_old_summary(self):
        self.page.add_init_script("""(() => {
          const original=window.fetch.bind(window);
          window.fetch=(input,options={})=>{
            if(String(input)==='/api/admin/monitor?window=today')return new Promise((resolve,reject)=>{
              options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
            });
            return original(input,options);
          };
        })();""")
        self.open()
        self.page.locator('#window').select_option('afternoon')
        self.page.wait_for_function("document.querySelector('#freshness').dataset.window==='afternoon' && document.querySelector('#freshness').dataset.state==='fresh'")
        self.assertIn('50%', self.page.locator('#webRate').inner_text())
        self.assertFalse(self.page.locator('#refresh').is_disabled())

    def test_daily_filter_metrics_and_cli_counts(self):
        self.open()
        self.assertIn('66.7%', self.page.locator('#cursorRate').inner_text())
        self.assertIn('已启动 5 次', self.page.locator('#cursorAttempts').inner_text())
        self.assertIn('历史未知', self.page.locator('#cursorStats').inner_text())
        self.assertIn('执行中', self.page.locator('#cursorStats').inner_text())
        self.assertIn('5.0s', self.page.locator('#cursorLatency').inner_text())
        self.assertIn('12.0s', self.page.locator('#cursorLatency').inner_text())
        self.assertIn('66.7%', self.page.locator('#sites').inner_text())
        self.assertEqual(self.page.locator('#daily tr').count(), 2)
        self.assertIn('2.0s / 4.0s', self.page.locator('#daily').inner_text())
        before_summary = len([1 for p, _ in self.calls if p == '/api/admin/monitor'])
        before_list = len([1 for p, _ in self.calls if p == '/api/admin/monitor/requests'])
        self.page.locator(f'[data-day="{self.old_date}"]').click()
        self.page.wait_for_function("document.querySelector('#freshness').dataset.state==='fresh' && document.querySelector('#window').value==='day' && document.querySelector('#total').textContent==='12'")
        self.page.wait_for_function("!document.querySelector('#requests').hasAttribute('aria-busy')")
        summaries = [q for p, q in self.calls if p == '/api/admin/monitor']
        lists = [q for p, q in self.calls if p == '/api/admin/monitor/requests']
        self.assertEqual(len(summaries), before_summary+1)
        self.assertEqual(len(lists), before_list+1)
        self.assertEqual(summaries[-1], {'window': 'day', 'date': self.old_date})
        self.assertEqual(lists[-1].get('date'), self.old_date)
        self.assertEqual(lists[-1].get('page'), '1')
        self.assertEqual(self.page.locator('#freshness').get_attribute('data-date'), self.old_date)
        self.page.locator('[data-channel-failed="cursor"]').click()
        self.page.wait_for_function("!document.querySelector('#requests').hasAttribute('aria-busy')")
        query = [q for p, q in self.calls if p == '/api/admin/monitor/requests'][-1]
        self.assertEqual(query.get('channel'), 'cursor')
        self.assertEqual(query.get('outcome'), 'failed')
        self.assertEqual(query.get('date'), self.old_date)

    def test_full_content_and_safe_external_links(self):
        self.detail['prompt'] = '<img src=x onerror="window.injected=1">\n完整问题'
        result = self.detail['results'][0]
        result['answer'] = '<script>window.injected=1</script>\n完整回答 & 保留所有文本'
        result['citation_details'].extend([
            {'url': 'javascript:window.injected=1', 'title': '<img src=x onerror="window.injected=1">'},
            {'url': 'https://user:password@unsafe.example.test/private', 'title': '不应打开的带凭据链接'},
            {'url': 'data:text/html,<script>window.injected=1</script>', 'title': '不可用来源'},
            {'url': 'http://source.example.test/another', 'title': '第二条可用来源'},
        ])
        self.open()
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_selector('#detailPrompt')
        self.assertEqual(self.page.locator('#detailPrompt').inner_text(), self.detail['prompt'])
        self.assertEqual(self.page.locator('[data-answer]').first.inner_text(), result['answer'])
        self.assertEqual(self.page.locator('#detailRequestID').inner_text(), 'external_request_01')
        self.assertEqual(self.page.locator('#detailBody img, #detailBody script').count(), 0)
        self.assertIsNone(self.page.evaluate('window.injected'))
        self.assertEqual(self.page.locator('.source-list a').count(), 2)
        self.assertEqual(self.page.locator('.share-link a').get_attribute('href'), result['share_url'])
        for link in self.page.locator('#detailBody a').all():
            self.assertIn(urlsplit(link.get_attribute('href')).scheme, ['http', 'https'])
            self.assertIsNone(urlsplit(link.get_attribute('href')).username)
            self.assertEqual(link.get_attribute('target'), '_blank')
            self.assertEqual(set(link.get_attribute('rel').split()), {'noopener', 'noreferrer'})
        self.page.locator('#closeDetail').click()
        result['share_url'] = 'javascript:window.injected=1'
        self.detail['content_available'] = False
        self.detail.pop('prompt')
        result.pop('answer')
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_selector('#detailPrompt')
        self.assertIn('完整内容未保留', self.page.locator('#detailBody').inner_text())
        self.assertIn('尚未记录回答正文', self.page.locator('[data-answer]').inner_text())
        self.assertEqual(self.page.locator('.share-link a').count(), 0)

    def test_cli_unknown_or_missing_is_not_zero_success_rate(self):
        for view in self.snapshot['windows'].values():
            view['channels'].pop('cursor')
            for key in list(view['sites'][0]):
                if key.startswith('cursor_'):
                    del view['sites'][0][key]
        self.open()
        self.assertIn('—', self.page.locator('#cursorRate').inner_text())
        self.assertIn('未记录', self.page.locator('#cursorStats').inner_text())
        self.assertIn('未记录', self.page.locator('#sites').inner_text())
        self.snapshot['windows']['today']['channels']['cursor'] = {
            'total': 3, 'success': 0, 'failed': 0, 'unknown': 3, 'running': 0, 'success_rate': None}
        self.page.locator('#refresh').click()
        self.page.wait_for_function("document.querySelector('#cursorAttempts').textContent.includes('已启动 3 次')")
        self.assertNotIn('0%', self.page.locator('#cursorRate').inner_text())
        self.assertIn('—', self.page.locator('#cursorRate').inner_text())

    def test_final_delivery_and_recovery_metrics_show_evidence_denominators(self):
        delivery={'total':10,'web_success':6,'api_success':3,'web_share':60,'api_fallback_rate':30}
        recovery={'attempted':5,'completed':3,'known_completed':3,'web_success':2,'web_success_rate':66.7,'pending':1,'cancelled':1,'skipped':0,'unknown':0}
        for view in self.snapshot['windows'].values():
            view['delivery']=copy.deepcopy(delivery);view['recovery']=copy.deepcopy(recovery)
            view['sites'][0]['delivery']=copy.deepcopy(delivery);view['sites'][0]['recovery']=copy.deepcopy(recovery)
        self.snapshot['daily'][0]['delivery']=copy.deepcopy(delivery)
        self.snapshot['daily'][0]['recovery']=copy.deepcopy(recovery)
        self.open()
        self.assertEqual(self.page.locator('#webDeliveryRate').inner_text(),'60%')
        self.assertEqual(self.page.locator('#apiFallbackRate').inner_text(),'30%')
        self.assertEqual(self.page.locator('#webRecoveryRate').inner_text(),'66.7%')
        self.assertIn('2 次 CLI 恢复后网页交付 / 3 个归因明确结果',self.page.locator('#webRecoveryEvidence').inner_text())
        self.assertIn('1 恢复中',self.page.locator('#webRecoveryEvidence').inner_text())
        self.assertIn('6 / 10 个平台结果',self.page.locator('#sites').inner_text())
        self.assertIn('2 / 3 个归因明确结果',self.page.locator('#daily').inner_text())
        self.snapshot['windows']['today'].pop('recovery')
        self.page.locator('#refresh').click()
        self.page.wait_for_function("document.querySelector('#webRecoveryEvidence').textContent.includes('历史未记录')")
        self.assertEqual(self.page.locator('#webRecoveryRate').inner_text(),'—')

    def test_live_recovery_progress_and_cli_verification_are_separate_from_resume(self):
        self.detail['recovery_states']={'yuanbao':{'phase':'resuming','account_id':'acct_first','account_label':'手机尾号 8351',
            'cli_call_id':'call_live','updated_at':self.now}}
        result=self.detail['results'][0]
        result['web_recovery']={'status':'failed','attempts':1,'cli_call_id':'call_live','verified':True,'resumed':False,'cli_assisted':False,
            'elapsed_ms':4000,'error_code':'cli_resume_failed'}
        result['attempts'].append({'channel':'cursor','task_type':'web_recovery','account_id':'acct_first','account_label':'手机尾号 8351',
            'status':'success','ok':True,'phase':'completed','verified':True,'resume_ok':False,'error_code':'cli_resume_failed'})
        self.open()
        self.assertIn('原网页续跑中',self.page.locator('#requests').inner_text())
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_selector('[data-web-recovery]')
        self.assertIn('原网页续跑中',self.page.locator('#recoveryProgress').inner_text())
        self.assertIn('页面复核：通过',self.page.locator('[data-recovery-verified]').inner_text())
        self.assertIn('未取得回答',self.page.locator('[data-recovery-resumed]').inner_text())
        self.assertIn('最终续跑由同账号 CLI 修复：否',self.page.locator('[data-recovery-assisted]').inner_text())
        self.assertIn('页面复核通过',self.page.locator('.attempts tbody tr').last.inner_text())
        self.assertIn('未取得回答',self.page.locator('[data-cli-resumed]').inner_text())
        self.assertIn('完整的测试回答',self.page.locator('[data-answer]').inner_text())

    def test_cli_candidate_and_untrusted_recovery_fields_are_safe(self):
        payload='<img src=x onerror="window.injected=1">'
        self.detail['results'][0]['web_recovery']={'status':payload,'attempts':0,'cli_call_id':payload,'error_code':payload}
        self.detail['recovery_states']={'doubao':{'phase':payload,'account_label':payload,'cli_call_id':payload}}
        self.detail['cursor_calls']=[{'site':'doubao','channel':'cursor','status':'unknown','ok':None,'task_type':'web_recovery',
            'phase':'candidate','verified':None,'resume_ok':None,'account_label':payload}]
        self.open();self.page.locator('[data-request-detail]').first.click();self.page.wait_for_selector('#cursorTasks')
        self.assertIn('CLI 已结束，待复核',self.page.locator('#cursorTasks').inner_text())
        self.assertIn('未确认 / 未执行',self.page.locator('[data-cli-verified]').inner_text())
        self.assertIn('未启动 CLI，不计入',self.page.locator('[data-web-recovery]').inner_text())
        self.assertEqual(self.page.locator('#detailBody img, #requests img').count(),0)
        self.assertIsNone(self.page.evaluate('window.injected'))

    def test_waiting_sms_and_missing_cli_attribution_remain_explicit(self):
        self.detail['recovery_states']={'doubao':{'phase':'waiting_sms','account_label':'手机尾号 8351','updated_at':self.now}}
        self.detail['results'][0]['web_recovery']={'status':'succeeded','attempts':1,'verified':True,'resumed':True}
        view=self.snapshot['windows']['today']
        view['recovery']={'attempted':1,'completed':1,'known_completed':0,'pending':0,'unknown':1,'web_success':0,'web_success_rate':None}
        self.open()
        self.assertEqual(self.page.locator('#webRecoveryRate').inner_text(),'—')
        self.assertIn('1 未知',self.page.locator('#webRecoveryEvidence').inner_text())
        self.assertIn('等待短信登录',self.page.locator('#requests').inner_text())
        self.page.locator('[data-request-detail]').first.click();self.page.wait_for_selector('[data-recovery-assisted]')
        self.assertIn('历史归因未知',self.page.locator('[data-recovery-assisted]').inner_text())
        self.assertIn('等待短信登录',self.page.locator('#recoveryProgress').inner_text())

    def test_cli_background_tasks_without_platform_result(self):
        self.detail['results'] = []
        base = {'id': 'cursor_01', 'site': 'doubao', 'channel': 'cursor', 'account_id': 'acct_first',
                'account_label': '手机尾号 8351', 'task_type': 'captcha_recovery', 'started_at': self.now-20,
                'elapsed_ms': 5000, 'ok': None, 'error_code': '', 'status': 'unknown'}
        self.detail['cursor_calls'] = [base, {**base, 'id': 'cursor_02', 'status': 'running', 'elapsed_ms': 0}]
        self.open()
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_selector('#cursorTasks')
        self.assertEqual(self.page.locator('#cursorTasks tbody tr').count(), 2)
        text = self.page.locator('#cursorTasks').inner_text()
        for expected in ['豆包', '验证恢复', '历史未知', '执行中', '手机尾号 8351']:
            self.assertIn(expected, text)

    def test_stage_timings_show_local_waits_separately_and_ignore_unknown_fields(self):
        self.detail['queued_ms']=1234
        result=self.detail['results'][0]
        result['stage_timings']={'pool_wait_ms':0,'site_gate_wait_ms':1250,'session_acquire_ms':20,
            'session_setup_ms':2000,'web_execution_ms':3000,'api_wait_ms':5000,'api_execution_ms':0,
            'cloud_slot_wait_ms':'SECRET_VALUE','web_slot_wait_ms':-1,'SECRET_TOKEN':'SECRET_VALUE'}
        result['web_error_code']='insufficient_web_budget'
        result['attempts']=[{'channel':'web','account_id':'','account_label':'本地调度 / 预算（未调用平台）',
            'ok':None,'status':'skipped','skipped':True,'error_code':'insufficient_web_budget','skip_reason':'insufficient_web_budget'}]
        self.open()
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_selector('.stage-timings')
        self.assertIn('1.23s',self.page.locator('#detailQueueWait').inner_text())
        self.assertEqual(self.page.locator('[data-stage]').count(),7)
        self.assertIn('0ms',self.page.locator('[data-stage="pool_wait_ms"]').inner_text())
        self.assertIn('5.00s',self.page.locator('[data-stage="api_wait_ms"]').inner_text())
        self.assertIn('0ms',self.page.locator('[data-stage="api_execution_ms"]').inner_text())
        text=self.page.locator('#detailBody').inner_text()
        self.assertIn('本地网页预算不足',text)
        self.assertIn('跳过',text)
        self.assertNotIn('SECRET_',text)
        self.assertEqual(self.page.locator('[data-stage="cloud_slot_wait_ms"]').count(),0)
        self.page.locator('#closeDetail').click()
        result.pop('stage_timings')
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_selector('#detailPrompt')
        self.assertIn('历史记录未保存阶段耗时',self.page.locator('#detailBody').inner_text())

    def test_optional_quality_errors_are_visible_without_hiding_saved_answer(self):
        result=self.detail['results'][0]
        result['optional_stage_error_code']='answer_extract_timeout'
        result['share_error_code']='share_generation_failed'
        self.open()
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_selector('[data-answer]')
        text=self.page.locator('#detailBody').inner_text()
        for reason in ['可选阶段（回答已保留）','正文提取超时','分享链接生成失败','answer_extract_timeout','share_generation_failed']:
            self.assertIn(reason,text)
        self.assertEqual(self.page.locator('[data-answer]').inner_text(),result['answer'])
        self.assertIn('最终交付成功',text)

    def test_web_first_deadline_is_labeled_as_extendable_estimate(self):
        self.detail['status']='running';self.detail['deadline_policy']='web_first';self.detail['deadline']=self.now-300
        self.open();self.page.locator('[data-request-detail]').first.click();self.page.wait_for_selector('#detailDeadline')
        self.assertIn('阶段预计时间',self.page.locator('#detailDeadline').inner_text())
        self.assertIn('可延长',self.page.locator('#detailDeadline').inner_text())
        self.assertNotIn('历史截止',self.page.locator('#detailDeadline').inner_text())
        self.page.locator('#closeDetail').click();self.detail['deadline']=0;self.detail['status']='queued'
        self.page.locator('[data-request-detail]').first.click();self.page.wait_for_selector('#detailDeadline')
        self.assertIn('等待开始执行',self.page.locator('#detailDeadline').inner_text())

    def test_eventual_delivery_waits_are_not_final_failures(self):
        self.detail.update(status='queued',delivery_policy='eventual',retry_at=dt.datetime.fromtimestamp(self.now+60,dt.timezone.utc).isoformat(),retry_count=3,wait_reason='recovery_state_missing')
        result=self.detail['results'][0]
        result.update(ok=False,delivery_pending=True,retry_state='awaiting_confirmation',error_code='recovery_state_missing',answer='')
        result['attempts']=[{'channel':'web','account_id':'acct_first','account_label':'手机尾号 8351','ok':False,'status':'failed','error_code':'generation_timeout','elapsed_ms':200}]
        for view in self.snapshot['windows'].values():
            view['delivery']={'total':1,'pending':2,'web_success':1,'api_success':0,'web_share':100,'api_fallback_rate':0}
        self.open()
        text=self.page.locator('#requests tr').first.inner_text()
        self.assertIn('发送结果待确认',text);self.assertIn('重试 3 次',text)
        self.assertNotIn('元宝 失败',text)
        self.assertIn('2 个等待交付',self.page.locator('#webDeliveryEvidence').inner_text())
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_selector('#persistentDelivery')
        detail=self.page.locator('#detailBody').inner_text()
        for expected in ('当前状态','累计重试 3 次','发送结果待确认','缺少原任务恢复记录','不会重新发送问题','排队等待不代表平台调用失败'):
            self.assertIn(expected,detail)
        self.assertNotIn('最终未取得回答',detail)
        self.assertIn('失败',self.page.locator('.attempts tbody tr').first.inner_text())
        self.assertEqual(self.page.locator('.detail-site header .pill').get_attribute('class'),'pill warn')

    def test_operator_wait_and_checkpoint_metadata_are_safe(self):
        self.detail.update(status='queued',delivery_policy='eventual',retry_count=1,wait_reason='<img src=x onerror="window.injected=1">')
        result=self.detail['results'][0]
        result.update(ok=False,delivery_pending=True,retry_state='operator_wait',attempts_truncated=True,attempts_dropped=4)
        self.snapshot['windows']['today']['attempts_dropped']=4
        self.open()
        self.assertIn('等待人工处理',self.page.locator('#requests tr').first.inner_text())
        self.assertIn('4 条较早尝试未保留',self.page.locator('#attemptsCoverage').inner_text())
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_selector('#persistentDelivery')
        self.assertIn('4 条较早记录未保留',self.page.locator('[data-attempts-truncated]').inner_text())
        self.assertEqual(self.page.locator('#persistentDelivery img').count(),0)
        self.assertIsNone(self.page.evaluate('window.injected'))
        self.page.locator('#closeDetail').click()
        self.detail.update(status='succeeded',retry_at='',wait_reason='')
        result.update(ok=True,delivery_pending=False,delivery_checkpoint_recovered=True)
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_selector('[data-checkpoint-recovered]')
        self.assertIn('已交付有效回答',self.page.locator('#persistentDelivery').inner_text())
        self.assertIn('未重新发送问题',self.page.locator('[data-checkpoint-recovered]').inner_text())
        self.assertEqual(self.page.locator('[data-delivery-pending]').count(),0)

    def test_provider_outage_and_sms_handoff_are_visible_without_false_success(self):
        self.snapshot['cursor_health']={'state':'blocked','error_code':'cli_billing_required','retry_at':'2026-10-20T03:00:00Z'}
        self.snapshot['windows']['today']['channels']['cursor']={'total':3,'success':0,'failed':1,'success_rate':0,'waiting_sms':1,'action_required':1,'provider_blocked':1,'verified_success':1,'resumed_success':1,'attributed_success':0}
        self.open()
        self.assertIn('账单未支付',self.page.locator('#cursorHealth').inner_text())
        self.assertIn('历史转交短信 / 人工',self.page.locator('#cursorStats').inner_text())
        self.detail['cursor_calls']=[{'id':'billing','site':'doubao','channel':'cursor','status':'failed','ok':False,'verified':True,'resume_ok':True,'process_status':'failed','process_error_code':'cli_billing_required','error_code':'cli_billing_required','cli_assisted':False,'build_id':'recovery-v2','stage_timings':{'cli_first_output_ms':1000}}]
        self.page.locator('[data-request-detail]').first.click()
        self.page.wait_for_selector('.cursor-evidence')
        self.assertIn('CLI 执行失败',self.page.locator('#detailBody').inner_text())
        self.assertIn('未归因于 CLI',self.page.locator('.cursor-evidence').inner_text())
        self.assertFalse(self.errors)

    def test_current_manual_queue_is_separate_from_historical_sms_and_automatic_wait(self):
        self.actions = {'updated_at': self.now, 'manual': 1, 'automatic': 1, 'rows': [
            {'account_id': 'acct_first', 'account_label': '手机尾号 8351', 'site': 'qianwen',
             'kind': 'manual', 'reason': '页面验证尚未通过', 'sms_state': 'not_sent',
             'next_action': '打开原页面处理验证码，再重新检测'},
            {'account_id': 'acct_second', 'account_label': '手机尾号 7132', 'site': 'deepseek',
             'kind': 'automatic', 'reason': '等待短信流程完成', 'sms_state': 'not_received',
             'sent_at': self.now-30, 'next_action': '等待自动回填；无需手工抄码或重复发码'}]}
        self.snapshot['windows']['today']['channels']['cursor']['waiting_sms'] = 39
        self.open()
        self.page.wait_for_function("document.querySelector('#actionSummary').textContent.includes('当前需人工 1 项')")
        self.assertEqual(self.page.locator('#actionRows tr').count(), 1)
        self.assertIn('8351', self.page.locator('#actionRows').inner_text())
        self.assertNotIn('7132', self.page.locator('#actionRows').inner_text())
        self.assertIn('inspect=1', self.page.locator('#actionRows a').get_attribute('href'))
        self.page.locator('#automaticActions').click()
        self.assertIn('7132', self.page.locator('#actionRows').inner_text())
        self.assertIn('尚未收到匹配回调', self.page.locator('#actionRows').inner_text())
        self.assertFalse(any(path.endswith('/send-code') for path, _ in self.calls))

    def test_cli_review_keeps_maintenance_records_filters_and_complete_export_link(self):
        self.cursor_review = {'total': 2, 'failure_reasons': {'cli_timeout': 1}, 'rows': [
            {'id': 'maintenance-call', 'job_id': 'login-check', 'parent_status': 'account_maintenance',
             'site': 'deepseek', 'account_label': '手机尾号 8351', 'task_type': 'login_check_assist',
             'status': 'waiting_sms', 'error_code': 'sms_required', 'assessment': '当次转交短信流程',
             'started_at': self.now-20, 'elapsed_ms': 5000},
            {'id': 'business-call', 'job_id': 'job_00', 'parent_status': 'succeeded',
             'site': 'doubao', 'account_label': '手机尾号 7132', 'task_type': 'web_recovery',
             'status': 'failed', 'error_code': 'cli_timeout', 'assessment': '执行预算用尽',
             'started_at': self.now-10, 'elapsed_ms': 10000}]}
        self.open()
        self.page.wait_for_function("document.querySelector('#cursorPageInfo').textContent.includes('共 2 次调用')")
        self.assertIn('账号维护，无业务请求', self.page.locator('#cursorReviewRows').inner_text())
        self.assertIn('当次转交短信', self.page.locator('#cursorReviewRows').inner_text())
        self.page.locator('#cursorOutcome').select_option('failed')
        self.page.wait_for_function("document.querySelector('#cursorPageInfo').textContent.includes('共 1 次调用')")
        self.assertNotIn('maintenance-call', self.page.locator('#cursorReviewRows').inner_text())
        export = parse_qs(urlsplit(self.page.locator('#cursorExport').get_attribute('href')).query)
        self.assertEqual(export['format'], ['csv'])
        self.assertEqual(export['outcome'], ['failed'])
        self.assertNotIn('page', export)
        self.cursor_offline = True
        self.page.locator('#refresh').click()
        self.page.wait_for_function("document.querySelector('#cursorReviewNotice').textContent.includes('读取失败')")
        self.assertIn('business-call', self.page.locator('#cursorReviewRows').inner_text())

    def test_date_switch_rejects_late_old_date_summary(self):
        old_payload = copy.deepcopy(self.snapshot)
        old_payload['windows'] = {'day': {**copy.deepcopy(self.snapshot['windows']['today']), 'total': 999}}
        self.page.add_init_script("""(() => {
          const original=window.fetch.bind(window);
          window.fetch=(input,options={})=>{
            if(String(input).includes('window=day&date=OLD_DATE')&&!String(input).includes('/requests')){
              return new Promise(resolve=>{window.releaseOldDay=()=>resolve(new Response(JSON.stringify(OLD_PAYLOAD),{headers:{'Content-Type':'application/json'}}));});
            }
            return original(input,options);
          };
        })();""".replace('OLD_DATE', self.old_date).replace('OLD_PAYLOAD', json.dumps(old_payload)))
        self.open()
        self.page.locator('#date').fill(self.old_date)
        self.page.locator('#date').dispatch_event('change')
        self.page.wait_for_function("typeof window.releaseOldDay==='function'")
        self.page.locator('#date').fill(self.older_date)
        self.page.locator('#date').dispatch_event('change')
        self.page.wait_for_function("document.querySelector('#total').textContent==='7' && document.querySelector('#freshness').dataset.state==='fresh'")
        self.page.evaluate('window.releaseOldDay()')
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.locator('#total').inner_text(), '7')
        self.assertEqual(self.page.locator('#freshness').get_attribute('data-date'), self.older_date)


if __name__ == '__main__':
    unittest.main()
