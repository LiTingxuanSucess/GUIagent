import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from cn_ai.site_flows import PlatformFlowError, detect_platform_failure
from cn_ai.scraper import _wait_deepseek_history


class DeepSeekUnavailableTest(unittest.TestCase):
    def test_real_error_is_detected_but_prompt_and_old_error_are_ignored(self):
        failed = '''<div data-virtual-list-item-key="-2">
          <div class="ds-message"><span>当前问题</span><button class="ds-button--warning">重试</button></div>
          <div><span>服务器暂时不可用</span></div>
        </div>'''
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(failed)
            with self.assertRaises(PlatformFlowError) as caught:
                detect_platform_failure(page, 'deepseek')
            self.assertEqual(caught.exception.error_code, 'platform_busy')
            self.assertTrue(caught.exception.retryable)
            with patch('cn_ai.scraper._fetch_deepseek_history') as fetch:
                with self.assertRaises(PlatformFlowError):
                    _wait_deepseek_history(page, '当前问题', timeout_sec=1)
                fetch.assert_not_called()

            page.set_content('''<div data-virtual-list-item-key="-2">
              <div class="ds-message"><span>服务器暂时不可用</span></div>
              <div>正在生成</div></div>''')
            detect_platform_failure(page, 'deepseek')

            page.set_content(failed + '''<div data-virtual-list-item-key="-4">
              <div class="ds-message"><span>新问题</span></div><div>正在生成</div></div>''')
            detect_platform_failure(page, 'deepseek')
            browser.close()


if __name__ == '__main__':
    unittest.main()
