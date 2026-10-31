"""Exercise console pagination and recoverable failures in a real browser."""
import json
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


class ConsoleBrowserTests(unittest.TestCase):
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
        self.ctx = self.browser.new_context()
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.mode = 'ok'
        self.paths = []
        self.html = Path(__file__).resolve().parents[1].joinpath('web/console.html').read_text(encoding='utf-8')
        self.page.route('http://console.test/**', self.route)

    def tearDown(self):
        self.ctx.close()
        self.assertEqual(self.errors, [])

    def route(self, route):
        from urllib.parse import urlsplit, parse_qs
        parsed = urlsplit(route.request.url)
        path, query = parsed.path, parse_qs(parsed.query)
        self.paths.append((path, query))
        if path == '/':
            route.fulfill(content_type='text/html', body=self.html)
            return
        status, data = 200, {}
        if path == '/api/console/me':
            data = {'role': 'admin', 'name': 'admin'}
        elif path == '/api/console/usage':
            data = {'usage': [{'jobs': 123}]}
        elif path == '/api/admin/tenants':
            data = {'tenants': [{'ak': 'tenant-a', 'name': 'Tenant A'}]}
        elif path == '/api/console/jobs':
            if self.mode == 'offline':
                route.abort('failed')
                return
            if self.mode == 'expired':
                status, data = 401, {'detail': 'login required'}
            else:
                offset = int(query.get('offset', ['0'])[0])
                total = 2 if query.get('ak') else 123
                data = {'jobs': [{'job_id': str(i), 'prompt': '测试问题', 'status': 'succeeded', 'sites': ['doubao'],
                                   'results': [{'site_id': 'doubao', 'ok': True}]} for i in range(offset, min(offset+50, total))],
                        'total': total}
        elif path.startswith('/api/console/jobs/'):
            data = {'job_id': path.rsplit('/', 1)[1], 'status': 'succeeded', 'results': [{'site_id': 'doubao', 'ok': True, 'answer': '完整回答'}]}
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data, ensure_ascii=False))

    def open(self):
        self.page.goto('http://console.test/')

    def test_pages_filters_and_lazy_details(self):
        self.open()
        self.page.wait_for_function("document.querySelectorAll('#jobBody tr').length === 50 && !document.querySelector('#btnRefresh').disabled")
        self.assertIn('123', self.page.locator('#pageInfo').inner_text())
        self.page.locator('#btnNext').click()
        self.page.wait_for_function("document.querySelector('#jobBody tr').dataset.id === '50' && !document.querySelector('#btnRefresh').disabled")
        self.page.locator('#btnNext').click()
        self.page.wait_for_function("document.querySelectorAll('#jobBody tr').length === 23 && !document.querySelector('#btnRefresh').disabled")
        self.assertTrue(self.page.locator('#btnNext').is_disabled())
        self.page.locator('#tenantFilter').select_option('tenant-a')
        self.page.wait_for_function("document.querySelectorAll('#jobBody tr').length === 2")
        self.assertEqual(self.page.locator('#jobBody tr').first.get_attribute('data-id'), '0')
        self.assertFalse(any(p.startswith('/api/console/jobs/') for p, q in self.paths))
        self.page.locator('#jobBody tr').first.click()
        self.page.locator('#detail .ans').wait_for()
        self.assertEqual(self.page.locator('#detail .ans').inner_text(), '完整回答')

    def test_failed_load_keeps_console_and_refresh_recovers(self):
        self.mode = 'offline'
        self.open()
        self.page.locator('#dataError').wait_for()
        self.assertTrue(self.page.locator('#app').is_visible())
        self.assertIn('网络连接失败', self.page.locator('#dataError').inner_text())
        self.mode = 'ok'
        self.page.locator('#btnRefresh').click()
        self.page.wait_for_function("document.querySelectorAll('#jobBody tr').length === 50")
        self.assertTrue(self.page.locator('#dataError').is_hidden())
        self.page.wait_for_function("!document.querySelector('#btnRefresh').disabled")
        self.mode = 'offline'
        self.page.locator('#btnNext').click()
        self.page.locator('#dataError').wait_for()
        self.page.wait_for_function("!document.querySelector('#btnRefresh').disabled")
        self.assertIn('第 1–50', self.page.locator('#pageInfo').inner_text())
        self.assertEqual(self.page.locator('#jobBody tr').first.get_attribute('data-id'), '0')

    def test_expired_session_requests_login(self):
        self.mode = 'expired'
        self.open()
        self.page.wait_for_function("document.querySelector('#loginErr').textContent.includes('登录已过期')")
        self.assertTrue(self.page.locator('#gate').is_visible())
        self.assertTrue(self.page.locator('#app').is_hidden())


if __name__ == '__main__':
    unittest.main()
