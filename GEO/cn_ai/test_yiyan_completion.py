"""Offline Wenxin DOM fixtures: no platform or history requests are made."""
import html
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from cn_ai import budget
from cn_ai.scraper import _poll_yiyan_history, _scrape_one, NetworkCapture
from cn_ai.test_yiyan_citations import stream
from cn_ai.site_flows import PlatformFlowError


PROMPT = "说明本地文件校验的作用，并给出参考来源。"
ANSWER = "文件校验通过比较文件内容的摘要，检查下载或复制后的数据是否发生变化。验证时应使用可信来源提供的摘要，并确认算法一致。"
FINAL = ANSWER + " 校验结果一致只能说明内容匹配，仍需核对文件来源和用途。"
SOURCE = "https://example.org/checksum"
NOTE = "还应记录摘要的来源和校验时间，便于后来复核同一文件。"


def qa(question=PROMPT, answer=ANSWER, *, menu="finished", generating="", card="", thinking=False):
    question_html = (
        '<div class="conversation-flow-question-container"><div class="cs-question-pure-text">'
        + html.escape(question) + '</div></div>'
    )
    if answer is None:
        return '<div class="chat-qa-container">' + question_html + '</div>'
    # Structure and state classes come from redacted production DOM. All text
    # and links below are synthetic, including the separate thinking fragment.
    thought = (
        '<div class="ai-thinking-steps _answer-finished_fixture">'
        '<div class="ai-entry-block ai-markdown"><div class="marklang">'
        '这只是已经结束的思考过程，不能把它当作用户需要的最终回答。'
        '</div></div></div>'
    ) if thinking else ''
    controls = ''
    if menu:
        state = '_generate-menu_fixture' if menu != 'generating' else '_answer-generating_fixture'
        hidden = ' style="display:none"' if menu == 'hidden' else ''
        disabled = ' aria-disabled="true"' if menu == 'disabled' else ''
        share = '' if menu == 'copy_only' else '<span><i class="cos-icon-share1"></i></span>'
        controls = ('<div class="answer-menu"><div class="cs-answer-hover-menu-container '
                    + state + '"' + hidden + '><span' + disabled
                    + '><i class="cos-icon-copy"></i></span>' + share + '</div></div>')
    return ('<div class="chat-qa-container">' + question_html
            + '<div class="conversation-flow-answer-container">'
            + '<div class="chat-search-answer-generate-item">' + thought
            + '<div class="answer-container"><div class="ai-entry-block ai-markdown">'
            + '<div class="cosd-markdown-content"><div class="marklang">'
            + html.escape(answer) + '<a href="' + SOURCE + '">参考资料</a>'
            + '</div></div></div></div>' + controls + generating + card
            + '</div></div></div>')


def split_qa(segments=(ANSWER, NOTE), *, nested=False, separate_entries=False,
             question=PROMPT, menu="finished", generating=""):
    """Synthetic text in the observed markdown/card/markdown reply structure."""
    original = qa(question=question, menu=menu, generating=generating)
    start = original.index('<div class="answer-container">')
    closing = '</div></div></div></div>'
    end = original.index(closing, start) + len(closing)
    blocks = []
    for text in segments:
        content = html.escape(text) + '<a href="' + SOURCE + '">参考资料</a>'
        if nested:
            content = '<div class="marklang">' + content + '</div>'
        block = ('<div class="ai-entry-block ai-markdown"><div class="cosd-markdown-content">'
                 '<div class="marklang">' + content + '</div></div></div>')
        blocks.append('<div class="ai-entry">' + block + '</div>' if separate_entries else block)
    thinking = ('<div class="ai-entry-block ai-thinking-steps _answer-finished_fixture">'
                '<div class="ai-entry-block ai-markdown"><div class="marklang">'
                '已经结束的思考内容，不属于正式回答。'
                '</div></div></div>')
    card = ('<div class="ai-entry-block ai-biz-block">'
            '<div class="ai-entry-block ai-markdown"><div class="marklang">'
            '资料卡正文，不属于正式回答。<a href="https://example.org/card">卡片来源</a>'
            '</div></div></div>')
    content = thinking + blocks[0] + card + ''.join(blocks[1:])
    if not separate_entries:
        content = '<div class="ai-entry"><div>' + content + '</div></div>'
    return original[:start] + '<div class="answer-container">' + content + '</div>' + original[end:]


class YiyanCompletionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {
            "GEO_DIAGNOSTIC_DEADLINE_MS": "0", "GEO_WEB_RECOVERY_FILE": "",
            "GEO_DIAGNOSTICS_ENABLED": "0",
        }))
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.abort())
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()
        self.stack.close()

    def poll(self, markup, *, prompt=PROMPT, history=None, on_sleep=None, timeout=9):
        self.page.set_content(markup)
        clock = [1000.0]
        sleeps = [0]

        def advance(seconds, reserve=0):
            clock[0] += seconds
            sleeps[0] += 1
            if on_sleep:
                on_sleep(sleeps[0], self.page)

        with patch("cn_ai.scraper.time.time", side_effect=lambda: clock[0]), \
             patch("cn_ai.scraper.budget.sleep", side_effect=advance), \
             patch("cn_ai.scraper.current_diagnostics", return_value=None), \
             patch("cn_ai.scraper.detect_platform_failure"), \
             patch("cn_ai.scraper._fetch_yiyan_history", return_value=history):
            return _poll_yiyan_history(self.page, prompt, timeout), sleeps[0]

    def test_ordinary_completed_reply_needs_no_task_card_and_keeps_sources(self):
        result, sleeps = self.poll(qa(thinking=True))
        self.assertTrue(result["complete"])
        self.assertTrue(result["prompt_matched"])
        self.assertEqual(result["source"], "dom:yiyan-standard")
        self.assertIn(ANSWER, result["answer"])
        self.assertNotIn("思考过程", result["answer"])
        self.assertEqual(result["citations"], [SOURCE])
        self.assertEqual(sleeps, 1)

    def test_stable_long_text_or_finished_thinking_alone_is_not_completion(self):
        for markup in (qa(answer=ANSWER * 20, menu=""), qa(menu="", thinking=True)):
            with self.subTest(markup_length=len(markup)):
                self.assertIsNone(self.poll(markup)[0])

    def test_same_reply_formal_segments_join_in_order_without_thinking_or_cards(self):
        result, sleeps = self.poll(split_qa())
        self.assertTrue(result["complete"])
        self.assertEqual(result["source"], "dom:yiyan-standard")
        self.assertLess(result["answer"].index(ANSWER), result["answer"].index(NOTE))
        self.assertNotIn("思考内容", result["answer"])
        self.assertNotIn("资料卡正文", result["answer"])
        self.assertEqual(result["citations"], [SOURCE])
        self.assertEqual(sleeps, 1)

    def test_split_reply_still_requires_finished_controls_and_no_generation(self):
        for markup in (split_qa(menu=""), split_qa(menu="generating"),
                       split_qa(generating='<button aria-label="停止生成">停止</button>')):
            with self.subTest(markup_length=len(markup)):
                self.assertIsNone(self.poll(markup)[0])

    def test_split_reply_cannot_join_different_answer_entries(self):
        self.assertIsNone(self.poll(split_qa(separate_entries=True))[0])

    def test_nested_and_duplicate_formal_markdown_is_not_repeated(self):
        result, _ = self.poll(split_qa(segments=(ANSWER, ANSWER, NOTE), nested=True))
        self.assertEqual(result["answer"].count(ANSWER), 1)
        self.assertEqual(result["answer"].count(NOTE), 1)
        self.assertEqual(result["citations"], [SOURCE])

    def test_old_split_reply_cannot_complete_a_new_unanswered_turn(self):
        self.assertIsNone(self.poll(split_qa(question="上一轮的另一个问题") + qa(answer=None))[0])
        self.assertIsNone(self.poll(split_qa(question=PROMPT + "另一个要求"))[0])

    def test_active_generation_overrides_finished_controls(self):
        for generating in (
            '<div class="_answer-generating_fixture"></div>',
            '<div class="_thinking-loading_fixture"></div>',
            '<div aria-busy="true"></div>',
            '<button aria-label="停止生成">停止</button>',
        ):
            with self.subTest(generating=generating):
                self.assertIsNone(self.poll(qa(generating=generating))[0])
        self.assertIsNone(self.poll(qa(menu="generating"))[0])

    def test_hidden_disabled_or_copy_only_controls_are_not_terminal(self):
        for menu in ("hidden", "disabled", "copy_only"):
            with self.subTest(menu=menu):
                self.assertIsNone(self.poll(qa(menu=menu))[0])

    def test_exact_current_question_is_required_with_whitespace_normalized(self):
        self.assertIsNone(self.poll(qa(question=PROMPT + "另一个要求"))[0])
        self.assertIsNone(self.poll(qa(), prompt="本地文件校验")[0])
        result, _ = self.poll(qa(question="  " + PROMPT + "\n"))
        self.assertTrue(result["complete"])

    def test_old_completed_reply_cannot_supply_new_unanswered_or_generating_turn(self):
        for current in (qa(answer=None), qa(menu="generating")):
            with self.subTest(current_has_answer="answer-container" in current):
                self.assertIsNone(self.poll(qa(answer="上一轮回答，具有完成菜单和来源，但不得用作新问题的结果。") + current)[0])

    def test_reply_must_follow_its_question_in_the_same_pair(self):
        markup = qa()
        start = markup.index('<div class="conversation-flow-question-container">')
        end = markup.index('<div class="conversation-flow-answer-container">')
        question = markup[start:end]
        markup = markup[:start] + markup[end:]
        markup = markup[:-6] + question + markup[-6:]
        self.assertIsNone(self.poll(markup)[0])

    def test_changed_or_resumed_generation_resets_stability(self):
        def change(count, page):
            if count == 1:
                page.set_content(qa(answer=FINAL, menu="generating"))
            elif count == 2:
                page.set_content(qa(answer=FINAL))
        result, sleeps = self.poll(qa(), on_sleep=change, timeout=15)
        self.assertIn(FINAL, result["answer"])
        self.assertEqual(sleeps, 3)

    def test_completed_task_card_history_path_is_preserved_and_question_bound(self):
        card = '<button data-show-ext="task_card">任务已完成</button>'
        history = {"answer":FINAL, "citations":[SOURCE], "source":"api:history/list", "prompt_matched":True}
        placeholder = "收到你的需求，将联网搜索后生成结果，大概需要3-5min。"
        result, sleeps = self.poll(qa(answer=placeholder, menu="", card=card), history=history)
        self.assertEqual(result["answer"], FINAL)
        self.assertEqual(result["source"], "api:history/list")
        self.assertTrue(result["complete"])
        self.assertEqual(sleeps, 0)
        self.assertIsNone(self.poll(qa(question="其他问题", answer=placeholder, menu="", card=card), history=history)[0])

    def test_task_card_can_still_open_its_final_dom(self):
        card = ('<button data-show-ext="task_card" onclick="'
                "document.querySelector('.marklang').textContent="
                + html.escape(json.dumps(FINAL, ensure_ascii=False), quote=True)
                + '">任务已完成</button>')
        result, _ = self.poll(qa(answer="收到你的需求，将联网搜索后生成结果。", menu="", card=card))
        self.assertTrue(result["complete"])
        self.assertEqual(result["source"], "dom:yiyan-task")
        self.assertEqual(result["answer"], FINAL)

    def test_ordinary_completion_checkpoints_before_optional_dom_timeout(self):
        self.page.set_content(qa())
        clock = [1000.0]
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, \
             patch("cn_ai.scraper.find_or_open_page", return_value=self.page), \
             patch("cn_ai.scraper._page_logged_out", return_value=False), \
             patch("cn_ai.scraper.send_prompt"), \
             patch("cn_ai.scraper._fetch_yiyan_history", return_value=None), \
             patch("cn_ai.scraper.current_diagnostics", return_value=None), \
             patch("cn_ai.scraper.detect_platform_failure"), \
             patch("cn_ai.scraper.time.time", side_effect=lambda: clock[0]), \
             patch("cn_ai.scraper.budget.sleep", side_effect=lambda seconds, reserve=0: clock.__setitem__(0, clock[0]+seconds)), \
             patch("cn_ai.scraper.extract_answer_and_links", side_effect=budget.StageDeadline("answer_extract_timeout")), \
             patch("cn_ai.scraper.create_public_share", return_value={"url": ""}), \
             redirect_stdout(output):
            result = _scrape_one(None, "yiyan", PROMPT, "fixture-current-question", Path(folder))
        checkpoints = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
        self.assertTrue(checkpoints[0]["web_answer_completed"])
        self.assertEqual(checkpoints[0]["question_id"], "fixture-current-question")
        self.assertTrue(result["ok"])
        self.assertEqual(result["answer"], checkpoints[0]["answer"])
        self.assertEqual(result["optional_stage_error_code"], "answer_extract_timeout")

    def test_current_network_sources_survive_dom_and_share_timeouts(self):
        for failure in ["none", "dom_timeout", "share_timeout"]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                # The completed DOM has no links; network sources arrive when
                # the finished response is drained during extraction.
                self.page.set_content(qa().replace('<a href="'+SOURCE+'">参考资料</a>', ''))
                capture = NetworkCapture("yiyan", PROMPT)
                response = stream(prompt=PROMPT, matched=True)
                clock = [1000.0]
                def flush():
                    if not capture.items:
                        capture.items.append(response)
                output = io.StringIO()
                public = "https://wenxin.baidu.com/csaitab/history/share?share_id=fixture-share-long&v=2"
                with ExitStack() as stack:
                    for target, kwargs in [
                        ("GEO_DIR", {"new": Path(folder)}),
                        ("find_or_open_page", {"return_value": self.page}),
                        ("NetworkCapture", {"return_value": capture}),
                        ("_page_logged_out", {"return_value": False}),
                        ("send_prompt", {}), ("_fetch_yiyan_history", {"return_value": None}),
                        ("current_diagnostics", {"return_value": None}), ("detect_platform_failure", {}),
                        ("time.time", {"side_effect": lambda: clock[0]}),
                        ("budget.sleep", {"side_effect": lambda seconds, reserve=0: clock.__setitem__(0, clock[0]+seconds)}),
                        ("extract_answer_and_links", {"side_effect": budget.StageDeadline("answer_extract_timeout")} if failure=="dom_timeout" else {"return_value": {"text": ANSWER, "links": [], "link_details": [], "selector": "yiyan-answer"}}),
                        ("create_public_share", {"side_effect": budget.StageDeadline("share_timeout")} if failure=="share_timeout" else {"return_value": {"url": public}}),
                        ("verify_public_share", {"return_value": {"ok": True, "final_url": public}}),
                    ]:
                        stack.enter_context(patch("cn_ai.scraper."+target, **kwargs))
                    stack.enter_context(patch.object(capture, "attach"))
                    stack.enter_context(patch.object(capture, "flush", side_effect=flush))
                    stack.enter_context(redirect_stdout(output))
                    result = _scrape_one(None, "yiyan", PROMPT, "fixture-current-question", Path(folder)/"run")
                checkpoints = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
                self.assertEqual(checkpoints[0]["citations"], [])
                self.assertEqual(len(checkpoints[-1]["citations"]), 1)
                self.assertEqual(result["answer"], ANSWER)
                self.assertEqual(result["citations"], checkpoints[-1]["citations"])
                self.assertEqual(result["citations_status"], "available")
                self.assertEqual(result["web_search_status"], "used")
                self.assertNotIn("web_search_error_code", result)
                self.assertNotIn("citations_missing", result.get("quality_issues", []))
                if failure!="share_timeout":
                    self.assertEqual(result["url"], public)
                    self.assertEqual(result["share_status"], "verified")
                    if failure=="dom_timeout":
                        self.assertEqual(result["optional_stage_error_code"], "answer_extract_timeout")
                else:
                    self.assertEqual(result["url"], "")
                    self.assertEqual(result["optional_stage_error_code"], "answer_extract_timeout" if failure=="dom_timeout" else "share_timeout")

    def test_same_old_question_cannot_checkpoint_without_a_new_send_confirmation(self):
        self.page.set_content(qa())
        clock = [1000.0]
        output = io.StringIO()

        def uncertain_send(page, prompt, site, **kwargs):
            kwargs["on_send_state"]("sending")

        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "recovery.json"
            with patch.dict(os.environ, {"GEO_WEB_RECOVERY_FILE":str(journal),
                                         "GEO_WEB_RECOVERY_ACCOUNT_ID":"fixture-account"}), \
                 patch("cn_ai.scraper.find_or_open_page", return_value=self.page), \
                 patch("cn_ai.scraper._page_logged_out", return_value=False), \
                 patch("cn_ai.scraper.send_prompt", side_effect=uncertain_send) as send, \
                 patch("cn_ai.scraper._fetch_yiyan_history", return_value=None), \
                 patch("cn_ai.scraper.current_diagnostics", return_value=None), \
                 patch("cn_ai.scraper.detect_platform_failure"), \
                 patch("cn_ai.scraper.time.time", side_effect=lambda: clock[0]), \
                 patch("cn_ai.scraper.budget.sleep", side_effect=lambda seconds, reserve=0: clock.__setitem__(0, clock[0]+seconds)), \
                 redirect_stdout(output), self.assertRaises(PlatformFlowError) as raised:
                _scrape_one(None, "yiyan", PROMPT, "fixture-new-question", Path(folder))
            self.assertEqual(raised.exception.error_code, "generation_timeout")
            send.assert_called_once()
            self.assertEqual(json.loads(journal.read_text())["send_state"], "sending")
        self.assertNotIn('"web_answer_completed"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
