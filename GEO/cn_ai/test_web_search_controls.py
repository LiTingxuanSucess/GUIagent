import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

from cn_ai.diagnostics import DiagnosticRun
from cn_ai.scraper import find_or_open_page
from cn_ai.search_policy import outbound_prompt
from cn_ai.site_flows import (
    PlatformFlowError,
    enable_web_search,
    run_deepseek_prep,
    run_kimi_prep,
    send_prompt,
)


class WebSearchControlsTest(unittest.TestCase):
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

    def tearDown(self):
        self.page.close()

    def test_six_platform_search_switches_enable_once_without_touching_thinking(self):
        for site in ('deepseek', 'doubao', 'kimi', 'yuanbao', 'yiyan', 'qianwen'):
            with self.subTest(site=site):
                self.page.set_content('''<button aria-pressed="false" onclick="window.searchClicks=(window.searchClicks||0)+1;this.setAttribute('aria-pressed','true')">联网搜索</button>
                  <button aria-pressed="false" onclick="window.wrongThinkingClick=true">深度思考</button>''')
                self.page.evaluate('window.searchClicks=0')
                self.assertEqual(enable_web_search(self.page, site), 'on')
                self.assertEqual(enable_web_search(self.page, site), 'on')
                self.assertEqual(self.page.evaluate('window.searchClicks'), 1)
                self.assertFalse(self.page.evaluate('!!window.wrongThinkingClick'))

    def test_actual_deepseek_div_switch_and_new_smart_search_label(self):
        self.page.set_content('''<div class="ds-toggle-button" aria-pressed="false"
            onclick="window.searchClicks=(window.searchClicks||0)+1;this.setAttribute('aria-pressed','true')"><span>智能搜索</span></div>
            <div class="ds-toggle-button" aria-pressed="true"><span>深度思考</span></div>''')
        self.assertEqual(run_deepseek_prep(self.page), 'search:on;thinking:on')
        self.assertEqual(run_deepseek_prep(self.page), 'search:on;thinking:on')
        self.assertEqual(self.page.evaluate('window.searchClicks'), 1)

    def test_explicit_search_off_cannot_silently_continue_when_enable_fails(self):
        self.page.set_content('<button aria-pressed="false">联网搜索</button>')
        with self.assertRaises(PlatformFlowError) as caught:
            enable_web_search(self.page, 'doubao')
        self.assertEqual(caught.exception.error_code, 'platform_setting_unavailable')
        self.assertFalse(caught.exception.retryable)

    def test_sidebar_search_answer_copy_and_model_mode_are_not_search_switches(self):
        self.page.set_content('''<aside><button data-testid="global-search-icon-entry" aria-label="搜索" onclick="window.wrongClick=true"></button></aside>
            <button aria-label="快速" aria-pressed="false" aria-haspopup="menu" onclick="window.wrongClick=true">快速</button>
            <div data-message-role="assistant"><button aria-pressed="false" onclick="window.wrongClick=true">联网搜索</button></div>
            <div style="display:none"><button aria-pressed="false" onclick="window.wrongClick=true">智能搜索</button></div>
            <main>全网搜索</main>''')
        with tempfile.TemporaryDirectory() as folder:
            with DiagnosticRun('doubao', 'fixture-search', 'fixture') as trace:
                trace.path = Path(folder)
                self.assertEqual(enable_web_search(self.page, 'doubao'), 'not_present')
                self.assertEqual(trace.meta['web_search_settings'], {'status': 'not_present', 'confirmed': False, 'control': ''})
        self.assertFalse(self.page.evaluate('!!window.wrongClick'))

    def test_unknown_search_switch_is_reported_without_flipping_it(self):
        self.page.set_content('<button onclick="window.wrongClick=true">联网搜索</button>')
        self.assertEqual(enable_web_search(self.page, 'yuanbao'), 'unconfirmed')
        self.assertFalse(self.page.evaluate('!!window.wrongClick'))

    def test_observed_auto_policy_is_not_reported_as_always_on(self):
        self.page.set_content('<button data-mode="auto">联网搜索</button>')
        self.assertEqual(enable_web_search(self.page, 'qianwen'), 'auto')

    def test_yiyan_current_chat_host_reuses_its_existing_tab(self):
        page = SimpleNamespace(url='https://chat.baidu.com/')
        browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
        with patch('cn_ai.scraper.page_answers', return_value=True):
            self.assertIs(find_or_open_page(browser, 'yiyan'), page)

    def kimi_modes(self, *, always=True, broken=False):
        always_row = '''<div class="connect-item" role="menuitemradio" aria-checked="false"
            onclick="document.querySelectorAll('.connect-item').forEach(e=>e.setAttribute('aria-checked','false'));this.setAttribute('aria-checked','true');window.chosenMode='on'">始终搜索</div>''' if always else ''
        onclick = "" if broken else "document.querySelectorAll('.connect-item').forEach(e=>e.setAttribute('aria-checked','false'));this.setAttribute('aria-checked','true');window.chosenMode='auto'"
        self.page.set_content('''<button class="toolkit-trigger-btn" aria-expanded="false"
            onclick="this.setAttribute('aria-expanded','true');document.querySelector('.toolkit-container').style.display='block'">工具</button>
            <div class="toolkit-container" style="display:none"><div class="toolkit-item" role="menuitem" aria-haspopup="menu"
            onmouseenter="document.querySelector('.connect-popover').style.display='block'">联网搜索</div></div>
            <div class="connect-popover" style="display:none">''' + always_row + '''
            <div class="connect-item" role="menuitemradio" aria-checked="false" onclick="''' + onclick + '''">自动</div>
            <div class="connect-item" role="menuitemradio" aria-checked="true">关闭</div></div>''')

    def test_kimi_prefers_verified_always_search_to_auto(self):
        self.kimi_modes()
        self.assertIn('search:on', run_kimi_prep(self.page))
        self.assertEqual(self.page.evaluate('window.chosenMode'), 'on')

    def test_kimi_current_radio_menu_reports_auto_without_claiming_forced_search(self):
        self.page.set_content('''<button class="toolkit-trigger-btn" aria-expanded="false"
          onclick="this.setAttribute('aria-expanded','true');document.querySelector('.toolkit-panel').hidden=false">工具</button>
          <div class="toolkit-panel" hidden><button class="toolkit-item"
          onclick="document.querySelector('#modes').hidden=false">联网搜索 查找实时新闻和信息</button>
          <div id="modes" hidden><button role="menuitemradio" aria-checked="false"
          onclick="this.setAttribute('aria-checked','true');window.searchClicks=(window.searchClicks||0)+1">
          <span>自动搜索</span><span>自动判断联网获取信息</span></button>
          <button role="menuitemradio" aria-checked="true">关闭搜索 不再联网获取信息</button></div></div>''')
        with tempfile.TemporaryDirectory() as folder:
            with DiagnosticRun('kimi', 'fixture-radio', 'fixture') as trace:
                trace.path = Path(folder)
                self.assertIn('search:auto', run_kimi_prep(self.page))
                self.assertEqual(trace.meta['web_search_settings']['status'], 'auto')
                self.assertTrue(trace.meta['web_search_settings']['confirmed'])
                self.assertIn('search:auto', run_kimi_prep(self.page))
        self.assertEqual(self.page.evaluate('window.searchClicks'), 1)

    def test_kimi_auto_only_menu_is_verified_and_reported_as_auto(self):
        self.kimi_modes(always=False)
        self.assertIn('search:auto', run_kimi_prep(self.page))
        self.assertEqual(self.page.evaluate('window.chosenMode'), 'auto')

    def test_kimi_known_disabled_search_must_not_claim_success_on_a_failed_click(self):
        self.kimi_modes(always=False, broken=True)
        with self.assertRaises(PlatformFlowError) as caught:
            run_kimi_prep(self.page)
        self.assertEqual(caught.exception.error_code, 'platform_setting_unavailable')

    def test_short_original_prompt_is_sent_once_without_instruction_or_duplication(self):
        self.page.set_content('''<textarea id="chat-input" onkeydown="if(event.key==='Enter'){event.preventDefault();window.sent=this.value}"></textarea>
            <button aria-pressed="false" onclick="this.setAttribute('aria-pressed','true')">联网搜索</button>
            <button data-testid="chat_input_send_button" onclick="window.sent=document.querySelector('textarea').value">发送</button>''')
        with patch.dict('cn_ai.site_flows.SITE_PREP', {'qianwen': lambda page: 'fixture'}):
            send_prompt(self.page, '你好', 'qianwen', new_chat=False)
        # The fixture observes the composer's real Enter handler, without
        # contacting an external page or making a model request.
        self.assertEqual(self.page.evaluate('window.sent'), '你好')
        self.assertEqual(self.page.locator('textarea').input_value(), '你好')
        self.assertEqual(self.page.get_by_role('button', name='联网搜索', exact=True).get_attribute('aria-pressed'), 'true')

    def test_kimi_multiline_prompt_does_not_submit_while_text_is_being_inserted(self):
        self.page.set_content('''<div class="chat-input-editor" contenteditable="true" role="textbox"
            onkeydown="if(event.key==='Enter')window.earlySubmit=true"></div>
            <div class="send-button-container" onclick="window.sent=document.querySelector('.chat-input-editor').innerText;this.classList.add('stop')">发送</div>''')
        with patch.dict('cn_ai.site_flows.SITE_PREP', {'kimi': lambda page: 'fixture'}):
            send_prompt(self.page, '第一行\n第二行', 'kimi', new_chat=False)
        self.assertFalse(self.page.evaluate('!!window.earlySubmit'))
        self.assertEqual(' '.join(self.page.evaluate('window.sent').split()), ' '.join(outbound_prompt('kimi', '第一行\n第二行').split()))

    def test_yuanbao_reselects_native_search_after_composer_click_clears_it(self):
        self.page.set_content('''<textarea onclick="document.querySelector('#chip').hidden=true"
          onkeydown="if(event.key==='Enter'){event.preventDefault();window.sent={text:this.value,search:!document.querySelector('#chip').hidden}}"></textarea>
          <button aria-label="添加" onclick="document.querySelector('#tools').hidden=false">+</button>
          <div id="tools" hidden><button onclick="document.querySelector('#chip').hidden=false;document.querySelector('#tools').hidden=true;document.querySelector('textarea').focus()">联网搜索</button></div>
          <button id="chip" aria-label="移除 联网搜索">联网搜索</button>''')
        with patch.dict('cn_ai.site_flows.SITE_PREP', {'yuanbao': lambda page: 'fixture'}):
            send_prompt(self.page, '元宝原题', 'yuanbao', new_chat=False)
        self.assertEqual(self.page.evaluate('window.sent'), {'text': '元宝原题', 'search': True})

    def test_kimi_modern_menu_prefers_always_and_never_flips_unknown_state(self):
        for state in ('false', None):
            attr = '' if state is None else 'aria-checked="false"'
            self.page.set_content('''<button class="toolkit-trigger-btn" aria-expanded="true">工具</button>
              <div class="toolkit-panel"><button class="toolkit-item" onclick="document.querySelector('#modes').hidden=false">联网搜索</button>
              <div id="modes" hidden><button role="menuitemradio" ''' + attr + ''' onclick="this.setAttribute('aria-checked','true');window.alwaysClicked=true">始终搜索</button>
              <button role="menuitemradio" aria-checked="true" onclick="window.autoClicked=true">自动搜索</button></div></div>''')
            self.page.evaluate('window.alwaysClicked=false;window.autoClicked=false')
            result = run_kimi_prep(self.page)
            self.assertIn('search:unconfirmed' if state is None else 'search:on', result)
            self.assertEqual(self.page.evaluate('!!window.alwaysClicked'), state is not None)
            self.assertFalse(self.page.evaluate('!!window.autoClicked'))


if __name__ == '__main__':
    unittest.main()
