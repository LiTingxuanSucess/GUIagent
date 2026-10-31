import unittest
from playwright.sync_api import sync_playwright
from cn_ai.site_flows import PlatformFlowError, detect_platform_failure


class KimiQuotaSignalsTest(unittest.TestCase):
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
        self.page = self.browser.new_page()
        self.page.route('**/*', lambda route: route.fulfill(status=200, body=''))

    def tearDown(self):
        self.page.close()

    def test_history_answers_and_input_do_not_exhaust_quota(self):
        self.page.set_content('''<main>
          <div class="segment-user">请解释“免费额度已用完，将于 11-01 重置”是什么意思</div>
          <div class="segment-assistant"><div class="markdown-container">额度已用尽表示需要等待</div></div>
          <textarea>免费额度已用完</textarea>
          <aside>额度已用完</aside></main>''')
        detect_platform_failure(self.page, 'kimi')

    def test_hidden_alert_does_not_exhaust_quota(self):
        for style in ('display:none', 'visibility:hidden', 'opacity:0'):
            with self.subTest(style=style):
                self.page.set_content(f'<div style="{style}"><div role="alert">免费额度已用完</div></div>')
                detect_platform_failure(self.page, 'kimi')

    def test_current_quota_notice_keeps_stated_reset_date(self):
        self.page.set_content('''<div role="alert"><h3>免费额度已用完</h3>
          <p>将于 11-01 重置</p></div><main>正在生成回答</main>''')
        with self.assertRaises(PlatformFlowError) as caught:
            detect_platform_failure(self.page, 'kimi')
        self.assertEqual(caught.exception.error_code, 'platform_quota')
        self.assertIn('11-01 重置', str(caught.exception))
        self.assertFalse(caught.exception.retryable)

    def test_standalone_notice_is_still_detected(self):
        self.page.set_content('<main><div>额度已用尽</div></main>')
        with self.assertRaises(PlatformFlowError):
            detect_platform_failure(self.page, 'kimi')


if __name__ == '__main__':
    unittest.main()
