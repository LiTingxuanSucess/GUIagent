import unittest

from playwright.sync_api import sync_playwright

from cn_ai.agentbay_doubao import AgentBayDoubaoError, _doubao_phone_input


class DoubaoPhoneLoginTest(unittest.TestCase):
    def test_delayed_login_choice_opens_phone_input_without_sending_sms(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('''<div role="dialog"></div><script>
              setTimeout(() => {
                const button = document.createElement('button');
                button.textContent = '手机号登录';
                button.onclick = () => setTimeout(() => {
                  button.parentElement.innerHTML = '<input data-testid="login_phone_number_input">';
                }, 200);
                document.querySelector('[role=dialog]').append(button);
              }, 300);
            </script>''')
            field = _doubao_phone_input(page, timeout=3)
            self.assertTrue(field.is_visible())
            self.assertEqual(field.input_value(), '')
            browser.close()

    def test_existing_phone_form_does_not_require_another_choice(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('<div role="dialog"><input data-testid="login_phone_number_input"></div>')
            self.assertTrue(_doubao_phone_input(page, timeout=1).is_visible())
            browser.close()

    def test_missing_phone_form_is_an_auth_failure(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('<div role="dialog">使用豆包或飞书账号登录</div>')
            with self.assertRaises(AgentBayDoubaoError) as caught:
                _doubao_phone_input(page, timeout=.3)
            self.assertEqual(caught.exception.error_code, 'platform_auth_unavailable')
            browser.close()


if __name__ == '__main__':
    unittest.main()
