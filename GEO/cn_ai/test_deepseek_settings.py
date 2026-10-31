import unittest

from playwright.sync_api import sync_playwright
from cn_ai.site_flows import run_deepseek_prep


class DeepSeekSettingsTests(unittest.TestCase):
    def test_independent_switches_are_idempotent(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('''<button aria-pressed="false" onclick="window.searchClicks=(window.searchClicks||0)+1;this.setAttribute('aria-pressed','true')">联网搜索</button>
                <button aria-pressed="false" onclick="window.thinkClicks=(window.thinkClicks||0)+1;this.setAttribute('aria-pressed','true')">深度思考</button>''')
            self.assertEqual(run_deepseek_prep(page), "search:on;thinking:on")
            self.assertEqual(run_deepseek_prep(page), "search:on;thinking:on")
            self.assertEqual(page.evaluate("[window.searchClicks,window.thinkClicks]"), [1, 1])
            browser.close()

    def test_unknown_state_does_not_flip_a_switch(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('''<button onclick="window.wrongClick=true">联网搜索</button>
                <button data-state="checked" onclick="window.wrongClick=true">深度思考</button>''')
            self.assertEqual(run_deepseek_prep(page), "search:unconfirmed;thinking:on")
            self.assertFalse(page.evaluate("!!window.wrongClick"))
            browser.close()


if __name__ == "__main__":
    unittest.main()
