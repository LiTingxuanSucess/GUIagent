import html
import json
import unittest
import time
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

from cn_ai.qianwen_completion import completed_answer
from cn_ai.protocol_completion import deepseek_turn
from cn_ai.search_policy import outbound_prompt
from cn_ai.web_recovery import display_prompt_matches
from cn_ai.share_links import _open_qianwen_share
from cn_ai.scraper import _completed_protocol_checkpoint, _looks_like_nav_junk


class QianwenCompletionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def fixture(self, *, question='原题？', complete=True, sources=True):
        cards = ''.join('<div id="deep-think-source-card-req-1-%s" data-c="refer_panel" data-click-extra="%s">来源</div>' % (
            index, html.escape(json.dumps({'req_id': 'req-1', 'display_scene': 'answer_rag',
                'url': 'https://sources.example/' + str(index), 'title': '原生来源'}, ensure_ascii=False), quote=True))
            for index in (1, 2))
        self.page.set_content('<div class="question-text">' + question + '</div><textarea></textarea>'
            '<div class="answer-common-card"><div class="' + ('qk-markdown-complete' if complete else 'qk-markdown-stream') + '">完整回答；https://answer-only.example/ 不算来源</div></div>'
            + ('<div class="search-content-native" onclick="document.querySelector(\'#panel\').hidden=false">2篇来源</div>'
               '<div id="panel" class="deep-think-source-native" hidden><div class="right-item-native"><div class="item-img-native" onclick="document.querySelector(\'#panel\').hidden=true">关闭</div></div>' + cards + '</div>' if sources else ''))

    def test_resume_collects_finished_native_sources_without_sending_or_reloading(self):
        self.fixture()
        result = completed_answer(self.page, '原题？')
        self.assertTrue(result['complete'])
        self.assertEqual(result['citations'], ['https://sources.example/1', 'https://sources.example/2'])
        self.assertFalse(self.page.locator('#panel').is_visible())
        self.assertEqual(self.page.locator('textarea').input_value(), '')

    def test_verified_list_answer_is_checkpointed_even_when_legacy_text_heuristic_rejects_it(self):
        self.fixture()
        text=''.join('第%d条核查说明，保留完整正文。\n' % i for i in range(40))
        self.page.locator('.qk-markdown-complete').evaluate('(e,text)=>e.innerText=text',text)
        result=completed_answer(self.page,'原题？')
        self.assertTrue(_looks_like_nav_junk(result['answer'],'原题？'))
        capture=SimpleNamespace(search_requests=[],submitted_prompt='原题？')
        checkpoint=_completed_protocol_checkpoint('qianwen','原题？','job_fixture',result,capture,time.perf_counter())
        self.assertTrue(checkpoint['web_answer_completed'])
        self.assertEqual(checkpoint['answer'],result['answer'])
        self.assertEqual(len(checkpoint['citations']),2)
        self.assertEqual(checkpoint['web_search_status'],'used')
        unverified=dict(result);unverified.pop('native_answer_verified')
        self.assertIsNone(_completed_protocol_checkpoint('qianwen','原题？','job_fixture',unverified,capture,time.perf_counter()))

    def test_changed_question_and_unfinished_answer_are_never_adopted(self):
        for kwargs in ({'question': '另一题？'}, {'complete': False}):
            self.fixture(**kwargs)
            self.assertIsNone(completed_answer(self.page, '原题？'))
            self.assertFalse(self.page.locator('#panel').is_visible())

    def test_answer_urls_never_become_search_sources(self):
        self.fixture(sources=False)
        self.assertEqual(completed_answer(self.page, '原题？')['citations'], [])

    def test_mixed_source_requests_are_rejected(self):
        self.fixture()
        self.page.locator('[data-click-extra]').last.evaluate('(e)=>{const d=JSON.parse(e.dataset.clickExtra);d.req_id="other";e.dataset.clickExtra=JSON.stringify(d)}')
        self.assertIsNone(completed_answer(self.page, '原题？'))

    def test_share_uses_current_conversation_and_waits_for_its_menu(self):
        self.page.set_content('''<button aria-label="更多" onclick="window.wrongMenu=true">无关菜单</button>
          <div style="height:1800px"></div><div data-session-active="true">
          <button data-session-switch-target="session-tools" onclick="setTimeout(()=>document.querySelector('#share-item').hidden=false,150)">更多</button></div>
          <button id="share-item" role="menuitem" hidden onclick="window.shareOpened=true">分享此对话</button>''')
        _open_qianwen_share(self.page)
        self.assertTrue(self.page.evaluate('!!window.shareOpened'))
        self.assertFalse(self.page.evaluate('!!window.wrongMenu'))


class WirePromptIdentityTest(unittest.TestCase):
    def test_known_suffix_matches_but_different_question_and_extra_turn_do_not(self):
        for site in ('deepseek', 'doubao', 'kimi'):
            wire = outbound_prompt(site, '原题？')
            self.assertTrue(display_prompt_matches(wire, '原题？', site))
            self.assertFalse(display_prompt_matches(wire + '再问一题', '原题？', site))
            messages = [{'role': 'USER', 'message_id': 1, 'fragments': [{'type': 'REQUEST', 'content': wire}]},
                        {'role': 'ASSISTANT', 'parent_id': 1, 'status': 'FINISHED', 'fragments': [{'type': 'RESPONSE', 'content': '正文'}]}]
            if site == 'deepseek':
                body = json.dumps({'data': {'chat_messages': messages}}, ensure_ascii=False)
                self.assertIsNotNone(deepseek_turn(body, '原题？'))
                self.assertIsNone(deepseek_turn(body, '另一题？'))
