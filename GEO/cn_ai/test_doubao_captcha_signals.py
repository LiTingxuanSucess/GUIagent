import unittest

from playwright.sync_api import sync_playwright

from cn_ai.site_flows import (
    PlatformFlowError,
    classify_platform_failure,
    detect_platform_failure,
)


class DoubaoCaptchaSignalsTest(unittest.TestCase):
    def test_capacity_refusal_is_busy_but_quoted_answer_is_not(self):
        refusal='算力紧张，办公能力得晚点才能用了。开通订阅，享高峰期优先使用。'
        self.page.set_content('<div data-message-role="assistant"><div>'+refusal+'</div></div>')
        with self.assertRaises(PlatformFlowError) as caught:
            detect_platform_failure(self.page,'doubao')
        self.assertEqual(caught.exception.error_code,'platform_busy')
        self.page.set_content('<div data-testid="message_text_content"><p>'+refusal+'</p></div>')
        detect_platform_failure(self.page,'doubao')

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        # Fixtures never contact Doubao or a verification provider.
        self.page.route('**/*', lambda route: route.fulfill(status=200, body=''))

    def tearDown(self):
        self.page.close()

    def assert_captcha(self):
        with self.assertRaises(PlatformFlowError) as caught:
            detect_platform_failure(self.page, 'doubao')
        self.assertEqual(caught.exception.error_code, 'platform_risk_control')
        self.assertFalse(caught.exception.retryable)

    def test_normal_answer_and_quoted_question_are_not_verification(self):
        self.assertIsNone(classify_platform_failure(
            'doubao', '支付产品通常通过短信验证码完成安全验证，避免账号被盗。', []
        ))
        self.page.set_content('''
          <div>用户Fixture1234</div>
          <div data-message-id="question"><div data-testid="message_text_content">
            <p>请解释提示语：</p><p>请完成安全验证</p>
          </div></div>
          <div data-message-id="answer"><div data-testid="message_text_content">
            <h2>安全验证</h2><p>支付产品通常通过短信验证码完成安全验证，避免账号被盗。</p>
          </div></div>
          <div contenteditable="true">请完成下方验证</div>
        ''')
        detect_platform_failure(self.page, 'doubao')

    def test_history_label_is_not_verification(self):
        self.page.set_content('<aside><a>安全验证</a></aside><main>正在生成回答</main>')
        detect_platform_failure(self.page, 'doubao')

    def test_ordinary_body_text_does_not_require_message_wrappers(self):
        self.page.set_content('<main>支付产品通常通过短信验证码完成安全验证，避免账号被盗。</main>')
        detect_platform_failure(self.page, 'doubao')

    def test_visible_semantic_iframe_still_blocks_a_logged_in_account(self):
        self.page.set_content('''<div>用户Fixture1234</div>
          <iframe style="width:380px;height:563px"
            src="https://rmc.bytedance.com/verifycenter/captcha/v2?verify_scene=doubao_message_web&amp;subtype=semantic_reasoning"></iframe>''')
        self.assert_captcha()

    def test_iframe_in_hidden_ancestor_is_not_verification(self):
        for style in ('display:none', 'visibility:hidden', 'opacity:0'):
            with self.subTest(style=style):
                self.page.set_content('''<div style="''' + style + '''">
                  <iframe style="width:380px;height:563px"
                    src="https://rmc.bytedance.com/verifycenter/captcha/v2"></iframe>
                </div><main>正在生成回答</main>''')
                detect_platform_failure(self.page, 'doubao')

    def test_iframe_with_explicit_visible_override_remains_a_real_challenge(self):
        self.page.set_content('''<div style="visibility:hidden">
          <iframe style="width:380px;height:563px;visibility:visible"
            src="https://rmc.bytedance.com/verifycenter/captcha/v2"></iframe>
        </div>''')
        self.assert_captcha()

    def test_visible_challenge_copy_without_iframe_is_retained(self):
        for html in (
            '<div role="dialog"><h2>安全验证</h2><button>继续</button></div>',
            '<div class="verify-modal"><h2>请完成安全验证</h2></div>',
            '<section><h2>请完成下方验证：</h2></section>',
        ):
            with self.subTest(html=html):
                self.page.set_content(html)
                self.assert_captcha()

    def test_hidden_challenge_copy_is_ignored(self):
        self.page.set_content('''<div style="opacity:0" role="dialog">
          <h2>安全验证</h2><p>请完成下方验证</p></div><main>正在生成回答</main>''')
        detect_platform_failure(self.page, 'doubao')

    def test_other_platform_rejections_keep_their_existing_detection(self):
        for site, text, code in (
            ('kimi', '和Kimi聊天的人太多了', 'platform_busy'),
            ('qianwen', '请完成安全验证', 'platform_risk_control'),
        ):
            with self.subTest(site=site):
                self.page.set_content('<main>' + text + '</main>')
                with self.assertRaises(PlatformFlowError) as caught:
                    detect_platform_failure(self.page, site)
                self.assertEqual(caught.exception.error_code, code)


if __name__ == '__main__':
    unittest.main()
