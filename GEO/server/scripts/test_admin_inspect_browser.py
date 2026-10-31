"""A current-action deep link must not implicitly send SMS or start a CLI."""
import base64
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


class AdminInspectBrowserTests(unittest.TestCase):
    def test_inspect_opens_exact_account_site_without_mutating_requests(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True,
                                         args=['--no-sandbox', '--disable-dev-shm-usage'])
            page = browser.new_page()
            requests, errors = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            html = Path(__file__).resolve().parents[1].joinpath('web/admin.html').read_text(encoding='utf-8')

            def route(request_route):
                request = request_route.request
                path = urlsplit(request.url).path
                requests.append((request.method, path))
                if path == '/admin':
                    request_route.fulfill(content_type='text/html', body=html)
                elif path == '/api/admin/accounts':
                    request_route.fulfill(content_type='application/json', body=json.dumps({
                        'accounts': [{'id': 'acct_fixture', 'phone': '13900001234', 'enabled': True,
                                      'sites': {'qianwen': {'site_id': 'qianwen', 'name': '千问', 'status': 'need_captcha'}}}],
                        'sites': [{'id': 'qianwen', 'name': '千问'}]}))
                elif path == '/api/admin/sms':
                    request_route.fulfill(content_type='application/json', body='{"messages":[]}')
                elif path.endswith('/screenshot'):
                    request_route.fulfill(content_type='image/png', body=base64.b64decode(
                        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII='))
                else:
                    request_route.fulfill(status=404, content_type='application/json', body='{}')

            page.route('http://admin.test/**', route)
            page.goto('http://admin.test/admin?account=acct_fixture&site=qianwen&inspect=1')
            page.wait_for_function("!document.querySelector('#detail').hidden && document.querySelector('#detailShot').naturalWidth > 0")
            self.assertIn('千问', page.locator('#detailTitle').inner_text())
            self.assertIn(('GET', '/api/admin/accounts/acct_fixture/sites/qianwen/screenshot'), requests)
            self.assertFalse([request for request in requests if request[0] != 'GET'])
            self.assertFalse(errors)
            browser.close()


if __name__ == '__main__':
    unittest.main()
