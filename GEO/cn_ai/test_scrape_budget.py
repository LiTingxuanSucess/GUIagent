import io
import json
import os
import signal
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cn_ai import budget
from cn_ai.protocol_completion import deepseek_turn, yuanbao_turn, yiyan_turn
from cn_ai.scraper import (
    _scrape_one, _wait_deepseek_history, _wait_yiyan_history, _page_fetch_text,
    extraction_reserve, generation_reserve, protocol_reserve,
)
from cn_ai.site_flows import PlatformFlowError
from playwright.sync_api import sync_playwright


PROMPT = "当前问题"
ANSWER = "这是当前问题已经完整生成的正式答案，包含可核对的真实参考来源，提取和分享卡住时应当保留。"


def deepseek_body(status="FINISHED", prompt=PROMPT, parent=1, pending=False):
    return json.dumps({"code": 0, "data": {"chat_messages": [
        {"role": "USER", "message_id": 1, "fragments": [{"type": "REQUEST", "content": prompt}]},
        {"role": "ASSISTANT", "message_id": 2, "parent_id": parent, "status": status,
         "has_pending_fragment": pending, "fragments": [{"type": "THINK", "status": "FINISHED", "content": "private planning"},
                                                       {"type": "RESPONSE", "status": status, "content": ANSWER}]},
    ]}}, ensure_ascii=False)


class ProtocolCompletionTest(unittest.TestCase):
    def test_deepseek_requires_exact_latest_turn_parent_and_response_terminal(self):
        self.assertIsNotNone(deepseek_turn(deepseek_body(), PROMPT))
        for body in [deepseek_body(status="GENERATING"), deepseek_body(prompt=PROMPT+"额外内容"),
                     deepseek_body(parent=99), deepseek_body(pending=True)]:
            self.assertIsNone(deepseek_turn(body, PROMPT))
        obj = json.loads(deepseek_body())
        obj["data"]["chat_messages"].append({"role": "USER", "fragments": [{"type": "REQUEST", "content": "下一轮问题"}]})
        self.assertIsNone(deepseek_turn(json.dumps(obj), PROMPT))

    def test_yuanbao_binds_reverse_ordered_turns_to_conversation_and_prompt(self):
        obj = {"id": "current123", "convs": [
            {"speaker": "ai", "conversationId": "current123", "index": 2, "speechesV2": [{"content": [{"type": "text", "msg": ANSWER}]}]},
            {"speaker": "human", "index": 1, "speech": PROMPT},
        ]}
        body = json.dumps(obj)
        self.assertIsNotNone(yuanbao_turn(body, PROMPT, "https://yuanbao.tencent.com/chat/current123"))
        self.assertIsNone(yuanbao_turn(body, PROMPT, "https://yuanbao.tencent.com/chat/another123"))
        self.assertIsNone(yuanbao_turn(body, PROMPT+"额外内容", "https://yuanbao.tencent.com/chat/current123"))
        obj["convs"].append({"speaker": "human", "index": 3, "speech": PROMPT})
        self.assertIsNone(yuanbao_turn(json.dumps(obj), PROMPT, "https://yuanbao.tencent.com/chat/current123"))

    def test_yiyan_requires_exact_query_and_current_session(self):
        obj = {"data": {"historyData": [{"sessionId": "123", "msgList": [{"query": PROMPT, "chatResponse": [{"sessionId": "123"}]}]}]}}
        body = json.dumps(obj)
        self.assertIsNotNone(yiyan_turn(body, PROMPT, "https://wenxin.baidu.com/search/123"))
        self.assertIsNone(yiyan_turn(body, PROMPT, "https://wenxin.baidu.com/search/456"))
        self.assertIsNone(yiyan_turn(body, "当前", "https://wenxin.baidu.com/search/123"))
        self.assertIsNone(yiyan_turn(body, PROMPT, "https://wenxin.baidu.com/"))


class BudgetTest(unittest.TestCase):
    def test_wait_window_honors_long_attempt_and_finite_standalone_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(budget.wait_window(105, reserve=5), 105)
            self.assertEqual(budget.wait_window(180, reserve=5), 180)
            with patch.dict(os.environ, {"GEO_DIAGNOSTIC_DEADLINE_MS": "1300000"}), \
                 patch("cn_ai.budget.time.time", return_value=1000):
                self.assertEqual(budget.wait_window(105, reserve=5), 295)
                self.assertEqual(budget.wait_window(120, reserve=10), 290)
                with budget.stage(30):
                    self.assertEqual(budget.wait_window(180), 30)

    def test_protocol_reserve_is_independent_and_nested_deadlines_do_not_accumulate(self):
        clock = [1000.0]
        with patch.dict(os.environ, {"GEO_WEB_EXTRACTION_RESERVE_SEC": "10",
                                     "GEO_DIAGNOSTIC_DEADLINE_MS": "1060000"}, clear=True), \
             patch("cn_ai.budget.time.time", side_effect=lambda: clock[0]):
            self.assertEqual(protocol_reserve(), 5)
            self.assertEqual(extraction_reserve(), 10)
            for site in ("deepseek", "yiyan"):
                clock[0] = 1000.0
                with budget.stage(180, reserve=generation_reserve(site)):
                    self.assertEqual(budget.remaining(), 55)
                    clock[0] += 2
                    with budget.stage(105, reserve=protocol_reserve()):
                        self.assertEqual(budget.remaining(reserve=protocol_reserve()), 53)
            self.assertEqual(generation_reserve("yuanbao"), 10)
            for value, expected in (("invalid", 5), ("1", 2), ("99", 15), ("7", 7)):
                with patch.dict(os.environ, {"GEO_PROTOCOL_FINALIZE_RESERVE_SEC": value}):
                    self.assertEqual(protocol_reserve(), expected)
                    self.assertEqual(extraction_reserve(), 10)

    def test_pending_playwright_evaluate_cancels_without_hanging_cleanup(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            started = time.monotonic()
            with self.assertRaises(budget.StageDeadline):
                budget.call(lambda: page.evaluate("() => new Promise(() => {})"), .05)
            # Under simultaneous local browser suites the dispatcher can need
            # one 500ms turn to unwind; the test guards against stuck cleanup.
            self.assertLess(time.monotonic()-started, 1)
            browser.close()

    def test_stage_hard_deadline_is_not_swallowed_and_restores_handler(self):
        handler = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        def hangs():
            try:
                time.sleep(2)
            except Exception:
                self.fail("stage deadline was swallowed by recoverable browser exception handling")
        with self.assertRaises(budget.StageDeadline) as raised:
            budget.call(hangs, .03, code="answer_extract_timeout")
        self.assertEqual(raised.exception.error_code, "answer_extract_timeout")
        self.assertLess(time.monotonic()-started, .5)
        self.assertEqual(signal.getsignal(signal.SIGALRM), handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_fetch_and_sleep_leave_cleanup_time_on_shared_deadline(self):
        with patch.dict(os.environ, {"GEO_DIAGNOSTIC_DEADLINE_MS": str((time.time()+1.2)*1000)}):
            ms = budget.timeout_ms(8000, reserve=1)
            self.assertGreater(ms, 100)
            self.assertLessEqual(ms, 200)
            page = SimpleNamespace(evaluate=lambda _script, args: args["timeout"])
            self.assertLessEqual(_page_fetch_text(page, "https://example.org/source"), 200)
            started = time.monotonic()
            budget.sleep(2, reserve=1)
            self.assertLess(time.monotonic()-started, .4)

    def test_history_waits_do_not_consume_extraction_reserve_or_return_partial(self):
        page = SimpleNamespace()
        with patch.dict(os.environ, {"GEO_DIAGNOSTIC_DEADLINE_MS": str((time.time()+.1)*1000)}), \
             patch("cn_ai.scraper._fetch_deepseek_history") as deepseek, \
             patch("cn_ai.scraper._fetch_yiyan_history") as yiyan:
            started = time.monotonic()
            self.assertIsNone(_wait_deepseek_history(page, PROMPT, timeout_sec=105))
            self.assertIsNone(_wait_yiyan_history(page, PROMPT, timeout_sec=180))
            self.assertLess(time.monotonic()-started, .1)
            deepseek.assert_not_called()
            yiyan.assert_not_called()


class FakeProfiler:
    def save_summary(self):
        pass
    def __init__(self, *args, **kwargs): self.steps=[]
    def attach_page(self, page): pass
    def step(self, name, fn, **kwargs):
        started=time.monotonic()
        try:
            return fn()
        finally:
            self.steps.append(SimpleNamespace(name=name,elapsed_ms=(time.monotonic()-started)*1000,ok=True))


class CompletedCheckpointTest(unittest.TestCase):
    def harness(self, stack, site):
        page=SimpleNamespace(url={"deepseek":"https://chat.deepseek.com/a/chat/s/abc", "yuanbao":"https://yuanbao.tencent.com/chat/current123", "yiyan":"https://wenxin.baidu.com/search/123"}[site])
        capture=SimpleNamespace(items=[],search_requests=[],attach=lambda page:None,flush=lambda:None)
        extra={"answer":ANSWER,"citations":["https://example.org/source"],"source":"api:history_messages","complete":True,"prompt_matched":True}
        for name,value in [("find_or_open_page",page),("_page_logged_out",False),("send_prompt",None),
                           ("_wait_deepseek_history",extra),("_wait_yiyan_history",extra),("_fetch_yuanbao_detail",extra)]:
            stack.enter_context(patch("cn_ai.scraper."+name,return_value=value))
        stack.enter_context(patch("cn_ai.scraper.SiteProfiler",FakeProfiler))
        stack.enter_context(patch("cn_ai.scraper.NetworkCapture",return_value=capture))
        stack.enter_context(patch("cn_ai.scraper.budget.sleep"))
        def completed(*args,**kwargs): kwargs["on_completed"]("dom_state");return ANSWER
        stack.enter_context(patch("cn_ai.scraper.wait_for_answer_growth",side_effect=completed))
        return extra

    def simulated_protocol_wait(self, stack, site, *, completed, attempt_sec=60, complete_after=52):
        extra = self.harness(stack, site)
        clock = [1000.0]
        stack.enter_context(patch.dict(os.environ, {
            "GEO_WEB_EXTRACTION_RESERVE_SEC": "10",
            "GEO_DIAGNOSTIC_DEADLINE_MS": str((1000+attempt_sec)*1000),
        }, clear=True))
        stack.enter_context(patch("cn_ai.budget.time.time", side_effect=lambda: clock[0]))
        stack.enter_context(patch("cn_ai.scraper.current_diagnostics", return_value=None))
        stack.enter_context(patch("cn_ai.scraper.detect_platform_failure"))
        stack.enter_context(patch("cn_ai.scraper._wait_deepseek_history", side_effect=_wait_deepseek_history))
        stack.enter_context(patch("cn_ai.scraper._wait_yiyan_history", side_effect=_wait_yiyan_history))
        stack.enter_context(patch("cn_ai.scraper._current_prompt_matches", return_value=True))
        def fetch(*args, **kwargs):
            return extra if completed and clock[0] >= 1000+complete_after else None
        stack.enter_context(patch("cn_ai.scraper._fetch_deepseek_history", side_effect=fetch))
        stack.enter_context(patch("cn_ai.scraper._fetch_yiyan_history", side_effect=fetch))
        def advance(seconds, reserve=0):
            amount = budget.remaining(seconds, reserve)
            if completed and clock[0] < 1000+complete_after:
                amount = min(amount, 1000+complete_after-clock[0])
            clock[0] += amount
        stack.enter_context(patch("cn_ai.scraper.budget.sleep", side_effect=advance))
        card = SimpleNamespace(count=lambda: int(completed and clock[0] >= 1000+complete_after),
                               is_visible=lambda: True)
        card.filter = lambda **kwargs: card
        card.last = card
        page = SimpleNamespace(url="https://wenxin.baidu.com/search/123",
                               locator=lambda *args: card, get_by_text=lambda *args, **kwargs: card)
        if site == "yiyan":
            stack.enter_context(patch("cn_ai.scraper.find_or_open_page", return_value=page))
        return clock

    def test_long_protocol_attempt_completes_after_old_105_and_180_second_caps(self):
        for site in ("deepseek", "yiyan"):
            with self.subTest(site=site), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                clock = self.simulated_protocol_wait(stack, site, completed=True,
                                                     attempt_sec=300, complete_after=220)
                output = io.StringIO()
                def expired_dom(*args):
                    self.assertEqual(clock[0], 1220)
                    checkpoints = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
                    self.assertTrue(checkpoints[0]["web_answer_completed"])
                    clock[0] = 1298
                    raise budget.StageDeadline("answer_extract_timeout")
                stack.enter_context(patch("cn_ai.scraper.extract_answer_and_links", side_effect=expired_dom))
                with redirect_stdout(output):
                    result = _scrape_one(None, site, PROMPT, "job_current", Path(folder))
                self.assertTrue(result["ok"])
                self.assertEqual(result["answer"], ANSWER)
                self.assertEqual(result["optional_stage_error_code"], "answer_extract_timeout")

    def test_long_unfinished_protocol_still_expires_at_actual_attempt_deadline(self):
        for site in ("deepseek", "yiyan"):
            with self.subTest(site=site), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                clock = self.simulated_protocol_wait(stack, site, completed=False, attempt_sec=300)
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(PlatformFlowError) as raised:
                    _scrape_one(None, site, PROMPT, "job_current", Path(folder))
                self.assertEqual(clock[0], 1295)
                self.assertEqual(raised.exception.error_code, "generation_timeout")
                self.assertNotIn('"web_answer_completed"', output.getvalue())

    def test_generic_wait_receives_long_attempt_when_diagnostics_are_disabled(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            self.harness(stack, "deepseek")
            clock = [1000.0]
            stack.enter_context(patch.dict(os.environ, {"GEO_WEB_EXTRACTION_RESERVE_SEC": "10",
                                                       "GEO_DIAGNOSTIC_DEADLINE_MS": "1300000"}, clear=True))
            stack.enter_context(patch("cn_ai.budget.time.time", side_effect=lambda: clock[0]))
            stack.enter_context(patch("cn_ai.scraper.current_diagnostics", return_value=None))
            def finish(*args, **kwargs):
                self.assertEqual(kwargs["timeout_sec"], 290)
                clock[0] += 220
                kwargs["on_completed"]("dom_state")
                return ANSWER
            wait = stack.enter_context(patch("cn_ai.scraper.wait_for_answer_growth", side_effect=finish))
            stack.enter_context(patch("cn_ai.scraper.extract_answer_and_links",
                                     side_effect=budget.StageDeadline("answer_extract_timeout")))
            with redirect_stdout(io.StringIO()), self.assertRaises(PlatformFlowError) as raised:
                _scrape_one(None, "qianwen", PROMPT, "job_current", Path(folder))
            self.assertEqual(raised.exception.error_code, "answer_extract_timeout")
            self.assertEqual(clock[0], 1220)
            wait.assert_called_once()

    def test_protocol_answer_at_second_52_is_saved_before_optional_work_times_out(self):
        for site in ("deepseek", "yiyan"):
            with self.subTest(site=site), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                clock = self.simulated_protocol_wait(stack, site, completed=True)
                output = io.StringIO()
                def expired_dom(*args):
                    self.assertEqual(clock[0], 1052)
                    checkpoints = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
                    self.assertTrue(checkpoints[0]["web_answer_completed"])
                    self.assertEqual(checkpoints[0]["answer"], ANSWER)
                    clock[0] = 1058
                    raise budget.StageDeadline("answer_extract_timeout")
                stack.enter_context(patch("cn_ai.scraper.extract_answer_and_links", side_effect=expired_dom))
                with redirect_stdout(output):
                    result = _scrape_one(None, site, PROMPT, "job_current", Path(folder))
                self.assertTrue(result["ok"])
                self.assertEqual(result["answer"], ANSWER)
                self.assertEqual(result["optional_stage_error_code"], "answer_extract_timeout")

    def test_unfinished_protocol_at_second_55_emits_no_checkpoint(self):
        for site in ("deepseek", "yiyan"):
            with self.subTest(site=site), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                clock = self.simulated_protocol_wait(stack, site, completed=False)
                dom = stack.enter_context(patch("cn_ai.scraper.extract_answer_and_links"))
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(PlatformFlowError) as raised:
                    _scrape_one(None, site, PROMPT, "job_current", Path(folder))
                self.assertEqual(clock[0], 1055)
                self.assertEqual(raised.exception.error_code, "generation_timeout")
                self.assertNotIn('"web_answer_completed"', output.getvalue())
                dom.assert_not_called()

    def test_finished_protocol_answer_is_saved_before_hung_dom(self):
        for site in ["deepseek","yuanbao","yiyan"]:
            with self.subTest(site=site),tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
                self.harness(stack,site)
                stack.enter_context(patch.dict(os.environ,{"GEO_DOM_EXTRACT_BUDGET_SEC":"0.03"}))
                output=io.StringIO()
                def hung_dom(*args):
                    checkpoints=[json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
                    self.assertEqual(checkpoints[0]["answer"],ANSWER)
                    time.sleep(2)
                stack.enter_context(patch("cn_ai.scraper.extract_answer_and_links",side_effect=hung_dom))
                share=stack.enter_context(patch("cn_ai.scraper.create_public_share",return_value={"url":""}))
                started=time.monotonic()
                with redirect_stdout(output):result=_scrape_one(None,site,PROMPT,"job_current",Path(folder))
                self.assertLess(time.monotonic()-started,.5)
                self.assertTrue(result["ok"])
                self.assertEqual(result["answer"],ANSWER)
                self.assertEqual(result["optional_stage_error_code"],"answer_extract_timeout")
                self.assertEqual(result["citations"],["https://example.org/source"])
                share.assert_called_once()
                self.assertFalse(result.get("share_skipped", False))

    def test_share_creation_is_bounded_and_keeps_committed_answer(self):
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            self.harness(stack,"deepseek")
            stack.enter_context(patch.dict(os.environ,{"GEO_SHARE_CREATE_BUDGET_SEC":"0.03"}))
            stack.enter_context(patch("cn_ai.scraper.extract_answer_and_links",return_value={"text":"wrong optional DOM answer","links":[]}))
            stack.enter_context(patch("cn_ai.scraper.merge_captures",return_value=("unrelated captured answer",[],"unknown")))
            stack.enter_context(patch("cn_ai.scraper.create_public_share",side_effect=lambda *args:time.sleep(2)))
            output=io.StringIO()
            with redirect_stdout(output):result=_scrape_one(None,"deepseek",PROMPT,"job_current",Path(folder))
            self.assertTrue(result["ok"])
            self.assertEqual(result["answer"],ANSWER)
            self.assertEqual(result["optional_stage_error_code"],"share_timeout")
            self.assertEqual(result["url"],"")

    def test_unconfirmed_generation_does_not_emit_success_or_enter_dom(self):
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            self.harness(stack,"deepseek")
            stack.enter_context(patch("cn_ai.scraper._wait_deepseek_history",return_value=None))
            dom=stack.enter_context(patch("cn_ai.scraper.extract_answer_and_links"))
            output=io.StringIO()
            with redirect_stdout(output),self.assertRaises(PlatformFlowError) as raised:
                _scrape_one(None,"deepseek",PROMPT,"job_current",Path(folder))
            self.assertEqual(raised.exception.error_code,"generation_timeout")
            self.assertNotIn('"web_answer_completed"',output.getvalue())
            dom.assert_not_called()

    def test_partial_history_candidate_cannot_be_promoted_by_optional_dom(self):
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            extra=self.harness(stack,"deepseek")
            extra["complete"]=False
            dom=stack.enter_context(patch("cn_ai.scraper.extract_answer_and_links"))
            output=io.StringIO()
            with redirect_stdout(output),self.assertRaises(PlatformFlowError) as raised:
                _scrape_one(None,"deepseek",PROMPT,"job_current",Path(folder))
            self.assertEqual(raised.exception.error_code,"generation_timeout")
            self.assertNotIn('"web_answer_completed"',output.getvalue())
            dom.assert_not_called()


if __name__ == "__main__": unittest.main()
