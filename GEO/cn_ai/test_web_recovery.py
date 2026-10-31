import io
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from contextlib import ExitStack, nullcontext, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from cn_ai import budget
from cn_ai.scraper import NetworkCapture, _scrape_one, retry_after_seconds
from cn_ai.site_flows import PlatformFlowError, detect_platform_failure, extract_answer_and_links, send_prompt, wait_for_answer_growth
from cn_ai.web_recovery import RecoveryJournal, prepare, probe, snapshot, target_id


PROMPT = "当前任务的问题"
ANSWER = "这是当前任务已经完整生成的正式回答，原始页面恢复后继续读取，不得再发送相同的问题。"


class WebRecoveryTest(unittest.TestCase):
    def test_yiyan_fresh_auth_requires_current_server_login_flag(self):
        from cn_ai.web_recovery import fresh_protocol_auth
        page = self.context.new_page()
        page.goto('https://wenxin.baidu.com/')
        body = {'userInfo': {'name': '当前账号', 'isUserLogin': 0}}
        page.route('https://wenxin.baidu.com/', lambda route: route.fulfill(
            content_type='text/html', body='<script type="application/json">' + json.dumps(body) + '</script>'))
        self.assertFalse(fresh_protocol_auth(page, 'yiyan'))
        body['userInfo']['isUserLogin'] = 1
        self.assertTrue(fresh_protocol_auth(page, 'yiyan'))
        body['userInfo']['name'] = ''
        self.assertFalse(fresh_protocol_auth(page, 'yiyan'))

    def test_kimi_user_toolbar_is_not_part_of_the_question(self):
        page = self.page(account=True)
        page.locator('.segment-user').evaluate("""e => {
          e.innerHTML='<span class="user-content__text">当前任务的问题</span>'+
            '<div class="segment-user-actions">编辑\\n复制\\n分享</div>';
        }""")
        current = snapshot(page, 'kimi')
        self.assertEqual(current['last_user'], PROMPT)
        self.assertEqual(current['user_count'], 1)

    def test_kimi_account_control_matches_current_and_legacy_layouts(self):
        import re
        source = (Path(__file__).resolve().parents[1] / 'server/internal/sessions/manager.go').read_text(encoding='utf-8')
        go_probe = re.search(r'const kimiAccountControlJS = `([\s\S]*?)`', source).group(1)
        page = self.context.new_page()
        page.goto('https://www.kimi.com/')
        cases = [
            ('<div class="user-info-container user-area__main"><button class="user-profile-trigger"><span class="user-name">登月者4385</span></button><button>升级</button></div>', True),
            ('<button class="user-info-container">Alice</button>', True),
            ('<div class="user-area"><button class="user-profile-trigger">Alice</button></div>', True),
            ('<div class="user-info-container"><button class="user-profile-trigger">登录 / 注册</button></div>', False),
            ('<button class="user-info-container">我的 Kimi</button><textarea></textarea>', False),
            ('<div class="user-info-container"><img class="user-avatar"></div><textarea></textarea>', False),
            ('<div style="opacity:0"><button class="user-info-container">Alice</button></div>', False),
            ('<div data-role="assistant"><button class="user-info-container">Alice</button></div>', False),
            ('<div class="segment"><div class="user-info-container"><button class="user-profile-trigger">Alice</button></div></div>', False),
        ]
        for html, expected in cases:
            with self.subTest(html=html):
                page.set_content(html)
                self.assertEqual(page.evaluate(go_probe), expected)
                self.assertEqual(snapshot(page, 'kimi')['account_marker'], expected)

    def test_yuanbao_sidebar_identity_is_not_a_generic_avatar(self):
        page = self.context.new_page()
        page.goto('https://yuanbao.tencent.com/chat')
        footer = '<div class="yb-nav__user"><div class="nick-info-container">{}</div></div>'
        for identity in ('用户123456','138****1234'):
            page.set_content(footer.format(identity)+'<textarea></textarea>')
            self.assertTrue(snapshot(page,'yuanbao')['account_marker'])
        page.set_content(footer.format('登录')+'<div class="avatar"></div><textarea></textarea>')
        self.assertFalse(snapshot(page,'yuanbao')['account_marker'])
        page.set_content('<div data-conv-speaker="ai">'+footer.format('用户123456')+'</div><textarea></textarea>')
        self.assertFalse(snapshot(page,'yuanbao')['account_marker'])

    def test_doubao_sidebar_account_control_is_not_a_guest_or_message(self):
        page = self.context.new_page()
        page.goto('https://www.doubao.com/chat/')
        footer = '<div class="footer-[data-sidebar-footer-account-menu]"><button>{}</button></div>'
        page.set_content(footer.format('用户123456')+'<textarea></textarea>')
        current = snapshot(page, 'doubao')
        self.assertTrue(current['account_marker'])
        self.assertFalse(current['logged_out'])
        page.set_content(footer.format('登录')+'<textarea></textarea>')
        current = snapshot(page, 'doubao')
        self.assertFalse(current['account_marker'])
        self.assertTrue(current['logged_out'])
        page.set_content('<div data-role="assistant">'+footer.format('用户123456')+'</div><textarea></textarea>')
        self.assertFalse(snapshot(page,'doubao')['account_marker'])
        page.set_content('<div style="display:none">'+footer.format('用户123456')+'</div><textarea></textarea>')
        self.assertFalse(snapshot(page,'doubao')['account_marker'])

    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, args=[f"--remote-debugging-port={cls.port}"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.stack = ExitStack()
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.file = self.folder / "recovery.json"
        self.stack.enter_context(patch.dict(os.environ, {
            "GEO_WEB_RECOVERY_FILE": str(self.file), "GEO_WEB_RECOVERY_ACCOUNT_ID": "account-fixture",
            "GEO_WEB_RECOVERY_PRIOR_LOGGED_IN": "1", "GEO_DIAGNOSTIC_DEADLINE_MS": "0",
        }))
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html></html>"))

    def tearDown(self):
        self.context.close()
        self.stack.close()

    def page(self, site="kimi", *, prompt=PROMPT, account=True, editor_disabled=False):
        origins = {"kimi": "https://www.kimi.com/chat/current", "deepseek": "https://chat.deepseek.com/a/chat/s/current",
                   "yuanbao": "https://yuanbao.tencent.com/chat/current"}
        page = self.context.new_page()
        page.goto(origins[site])
        identity = ("<button class='user-info-container'>当前账号</button>" if site == 'kimi'
                    else "<header><div class='user-avatar'>当前账号</div></header>")
        page.set_content((identity if account else "")+
                         f"<div class='segment segment-user' data-message-role='user'>{prompt}</div>"+
                         f"<textarea {'disabled' if editor_disabled else ''}></textarea>")
        return page

    def journal(self, page, site="kimi"):
        journal = RecoveryJournal.from_env(site, PROMPT, "job-fixture", create=True)
        journal.bind(page)
        return journal

    def test_private_journal_rejects_account_question_and_target_rebinding(self):
        page = self.page()
        journal = self.journal(page)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        journal.state("sending", page)
        journal.state("sent", page)
        for site, prompt, question, account in (("kimi", PROMPT, "another-job", "account-fixture"),
                                                 ("kimi", "其他问题", "job-fixture", "account-fixture"),
                                                 ("kimi", PROMPT, "job-fixture", "another-account")):
            with self.assertRaises(PlatformFlowError):
                RecoveryJournal(self.file, site, prompt, question, account)
        with self.assertRaises(PlatformFlowError):
            journal.bind(self.page())
        self.assertEqual(journal.data["target_id"], target_id(page))

    def test_probe_checks_live_challenge_and_allows_disabled_editor_while_generating(self):
        page = self.page(editor_disabled=True)
        journal = self.journal(page)
        journal.state("sent", page)
        page.evaluate("() => {const f=document.createElement('iframe');f.src='https://verifycenter.example/captcha';document.body.append(f)}")
        page.wait_for_timeout(30)
        blocked = probe(self.browser, journal)
        self.assertTrue(blocked["context_valid"])
        self.assertFalse(blocked["ready"])
        self.assertTrue(blocked["challenge_visible"])
        self.assertEqual(blocked["target_id"], target_id(page))
        page.locator("iframe").evaluate("e => e.remove()")
        ready = probe(self.browser, journal)
        self.assertTrue(ready["ready"], ready)
        self.assertFalse(ready["editor_ready"])
        detect_platform_failure(page, "kimi")
        page.locator(".segment-user").evaluate("e => e.textContent='另一条问题'")
        self.assertFalse(probe(self.browser, journal)["ready"])

    def test_shape_captcha_on_login_page_is_a_challenge(self):
        page = self.page("deepseek", account=False)
        page.evaluate("() => document.body.insertAdjacentHTML('beforeend','<div>点击图中最小的蓝色长方体</div>')")
        shot = snapshot(page, "deepseek")
        self.assertTrue(shot["challenge_visible"], shot)

    def test_quoted_verification_in_answer_or_share_dialog_is_not_a_challenge(self):
        page = self.page()
        page.evaluate("() => document.body.insertAdjacentHTML('beforeend', '<div role=dialog><div data-message-role=assistant>请完成安全验证</div></div>')")
        self.assertFalse(snapshot(page, "kimi")["challenge_visible"])
        detect_platform_failure(page, "kimi")

    def test_qianwen_new_account_footer_is_distinct_from_guest_and_answer_text(self):
        page = self.page(account=False)
        page.set_content('<div class="bg-pc-sidebar text-primary px-6 pt-3"><button>Qwen8351</button></div><textarea></textarea>')
        self.assertTrue(snapshot(page, "qianwen")["account_marker"])
        page.locator('button').evaluate("e => e.textContent='登录 / 注册'")
        guest = snapshot(page, "qianwen")
        self.assertFalse(guest["account_marker"])
        self.assertTrue(guest["logged_out"])
        page.set_content('<div data-message-role="assistant">Qwen8351 退出登录</div><textarea></textarea>')
        self.assertFalse(snapshot(page, "qianwen")["account_marker"])

    def test_cursor_eval_cannot_forge_account_evidence(self):
        from cn_ai.recovery_cdp import Page
        page = self.page(account=False)
        tool = Page(f'http://127.0.0.1:{self.port}', target_id(page), 'kimi')
        try:
            self.assertTrue(tool.evaluate("document.querySelector('textarea').getBoundingClientRect().width > 0", read_only=True))
            with self.assertRaises(RuntimeError):
                tool.evaluate("document.querySelector('textarea').setAttribute('aria-label','账户')", read_only=True)
            self.assertIsNone(page.locator('textarea').get_attribute('aria-label'))
        finally:
            tool.ws.close()

    def test_uncertain_sending_rejects_identical_old_turn_until_new_turn_is_observed(self):
        page = self.page()
        journal = self.journal(page)
        journal.state("sending", page)
        rejected = probe(self.browser, journal)
        self.assertFalse(rejected["ready"])
        self.assertEqual(journal.data["send_state"], "sending")
        page.evaluate("text => {const n=document.createElement('div');n.className='segment segment-user';n.textContent=text;document.body.append(n)}", PROMPT)
        ready = probe(self.browser, journal)
        self.assertTrue(ready["ready"], ready)
        self.assertEqual(journal.data["send_state"], "sent")

    def test_url_change_alone_cannot_confirm_an_old_identical_turn(self):
        page = self.page()
        journal = self.journal(page)
        journal.state("sending", page)
        page.evaluate("() => history.pushState({},'', '/chat/another-conversation')")
        result = probe(self.browser, journal)
        self.assertFalse(result["ready"])
        self.assertEqual(journal.data["send_state"], "sending")

    def test_evaluate_ready_soft_auth_and_captcha_gates(self):
        from cn_ai.web_recovery import evaluate_ready
        journal = {"site_id": "qianwen", "prompt": "问题", "send_state": "before_send",
                   "prior_logged_in": True, "phase": "before_send"}
        clear = {"editor_present": True, "editor_ready": True, "logged_out": False,
                 "account_marker": False, "challenge_visible": False, "waiting_sms": False, "last_user": ""}
        self.assertTrue(evaluate_ready(clear, journal, require_fresh_auth=True)["ready"])
        blocked = dict(clear, challenge_visible=True)
        self.assertEqual(evaluate_ready(blocked, journal, require_fresh_auth=True)["reason"], "captcha_visible")
        guest = dict(clear)
        guest_journal = dict(journal, prior_logged_in=False)
        self.assertEqual(evaluate_ready(guest, guest_journal, require_fresh_auth=True)["reason"], "login_unconfirmed")
        deepseek = dict(journal, site_id="deepseek")
        self.assertEqual(evaluate_ready(clear, deepseek, require_fresh_auth=True)["reason"], "login_unconfirmed")
        sent = dict(journal, send_state="sent")
        matched = dict(clear, last_user="问题")
        self.assertTrue(evaluate_ready(matched, sent, require_fresh_auth=True)["ready"])
        self.assertEqual(evaluate_ready(clear, sent, require_fresh_auth=True)["reason"], "question_unconfirmed")

    def test_post_cli_probe_accepts_prior_login_shell_without_dom_marker(self):
        page = self.page(account=False)
        journal = self.journal(page)
        journal.state("sent", page)
        self.assertTrue(journal.data["prior_logged_in"])
        self.assertTrue(probe(self.browser, journal)["ready"])
        with patch.dict(os.environ, {"GEO_WEB_RECOVERY_REQUIRE_FRESH_AUTH": "1"}):
            # Virtualized account chrome after a captcha must not fail every
            # verified CLI candidate when the journal already saw a login.
            accepted = probe(self.browser, journal)
            self.assertTrue(accepted["ready"], accepted)
            self.assertEqual(accepted["reason"], "ready")
            journal.data["prior_logged_in"] = False
            journal.save()
            rejected = probe(self.browser, journal)
            self.assertFalse(rejected["ready"])
            self.assertEqual(rejected["reason"], "login_unconfirmed")
            page.evaluate("() => document.body.insertAdjacentHTML('beforeend','<button class=user-info-container>当前账号</button>')")
            self.assertTrue(probe(self.browser, journal)["ready"])

    def test_deepseek_fresh_auth_requires_business_success_not_http_200(self):
        page = self.page("deepseek", account=False)
        journal = self.journal(page, "deepseek")
        journal.state("sent", page)
        current = {"code": 40003, "data": {"biz_code": 0, "biz_data": {"id": "fixture-account"}}}
        page.route("**/api/v0/users/current", lambda route: route.fulfill(content_type="application/json", body=json.dumps(current)))
        with patch.dict(os.environ, {"GEO_WEB_RECOVERY_REQUIRE_FRESH_AUTH": "1"}):
            self.assertFalse(probe(self.browser, journal)["ready"])
            current["code"] = 0
            ready = probe(self.browser, journal)
            self.assertTrue(ready["ready"], ready)
            self.assertTrue(ready["authenticated"])
            self.assertNotIn("fixture-account", json.dumps(ready))

    def test_probe_never_substitutes_another_target_or_guest_shell(self):
        page = self.page(account=False)
        with patch.dict(os.environ, {"GEO_WEB_RECOVERY_PRIOR_LOGGED_IN": "0"}):
            journal = self.journal(page)
        guest = probe(self.browser, journal)
        self.assertFalse(guest["ready"])
        self.assertEqual(guest["reason"], "login_unconfirmed")
        page.close()
        self.page()
        missing = probe(self.browser, journal)
        self.assertFalse(missing["context_valid"])
        self.assertEqual(missing["error_code"], "recovery_page_missing")

    def test_probe_reports_sms_only_when_an_existing_sms_wait_is_visible(self):
        page = self.page(account=False)
        journal = self.journal(page)
        page.set_content('<input placeholder="请输入短信验证码"><span>59秒后重新发送</span>')
        waiting = probe(self.browser, journal)
        self.assertTrue(waiting["context_valid"])
        self.assertTrue(waiting["waiting_sms"])
        self.assertFalse(waiting["ready"])
        self.assertEqual(waiting["reason"], "sms_required")
        page.set_content('<input placeholder="请输入验证码"><button>获取短信验证码</button>')
        self.assertFalse(probe(self.browser, journal)["waiting_sms"])

    def test_real_http_429_is_distinct_from_unfinished_generation(self):
        for site, url in (("kimi", "https://www.kimi.com/apiv2/kimi.gateway.chat.v1.ChatService/Chat"),
                          ("yuanbao", "https://yuanbao.tencent.com/api/chat/current")):
            with self.subTest(site=site):
                handlers = {}
                capture = NetworkCapture(site, PROMPT)
                capture.attach(SimpleNamespace(on=lambda event, callback: handlers.update({event: callback})))
                response = SimpleNamespace(status=429, headers={"content-type": "application/json", "retry-after": "125"})
                request = SimpleNamespace(url=url, method="POST", resource_type="fetch", response=lambda: response)
                handlers["requestfinished"](request)
                with self.assertRaises(PlatformFlowError) as raised:
                    capture.check_rejection()
                self.assertEqual(raised.exception.error_code, "platform_rate_limited")
                self.assertEqual(raised.exception.retry_after_sec, 125)

    def test_retry_after_seconds_and_http_date_are_bounded_and_invalid_is_unknown(self):
        future = format_datetime(datetime.fromtimestamp(1090, timezone.utc), usegmt=True)
        past = format_datetime(datetime.fromtimestamp(990, timezone.utc), usegmt=True)
        for header, expected in (("125", 125), (future, 90), ("99999", 600), ("1", 1),
                                 ("", 0), ("0", 0), ("-1", 0), ("0.5", 0), ("invalid", 0), (past, 0)):
            with self.subTest(header=header):
                self.assertEqual(retry_after_seconds(header, now=1000), expected)

    def test_kimi_pause_without_completion_controls_is_not_a_finished_answer(self):
        clock = [0.0]
        class Page:
            def evaluate(self, *args):
                return {"text": ANSWER, "generating": False, "completed": False}
            def wait_for_timeout(self, ms):
                clock[0] += ms / 1000
        with patch("cn_ai.site_flows.time.time", side_effect=lambda: clock[0]), \
             patch("cn_ai.site_flows.time.monotonic", side_effect=lambda: clock[0]), \
             patch("cn_ai.site_flows.detect_logged_out"), patch("cn_ai.site_flows.detect_platform_failure"), \
             patch("cn_ai.site_flows.kimi_reply_started", return_value=True), \
             self.assertRaises(PlatformFlowError) as raised:
            wait_for_answer_growth(Page(), timeout_sec=20, site_id="kimi")
        self.assertEqual(raised.exception.error_code, "generation_timeout")

    def test_yuanbao_old_finished_answer_before_new_user_is_not_reused(self):
        page = self.page("yuanbao")
        page.set_content(f'<div data-conv-speaker="ai" data-conv-status="finished">{ANSWER}</div>'
                         f'<div data-conv-speaker="human">{PROMPT}</div><textarea></textarea>')
        self.assertEqual(extract_answer_and_links(page, "yuanbao")["text"], "")
        with self.assertRaises(PlatformFlowError) as raised:
            wait_for_answer_growth(page, timeout_sec=.1, site_id="yuanbao")
        self.assertEqual(raised.exception.error_code, "generation_timeout")

    def test_kimi_uncertain_submission_never_automatically_clicks_twice(self):
        page = self.page(prompt="上一条问题")
        page.evaluate("() => {document.querySelector('textarea').remove();document.body.insertAdjacentHTML('beforeend', `<div class='chat-input-editor' contenteditable='true'></div><div class='send-button-container' onclick='window.sends=(window.sends||0)+1'>发送</div>`)}")
        journal = self.journal(page)
        with patch.dict("cn_ai.site_flows.SITE_PREP", {"kimi": lambda page: "skip"}), \
             patch("cn_ai.site_flows.time.sleep"), self.assertRaises(PlatformFlowError) as raised:
            send_prompt(page, PROMPT, "kimi", new_chat=False,
                        on_send_state=lambda state: journal.state(state, page))
        self.assertEqual(raised.exception.error_code, "recovery_unconfirmed")
        self.assertEqual(page.evaluate("window.sends"), 1)
        self.assertEqual(journal.data["send_state"], "sending")

    def test_bootstrap_requires_unique_existing_page_without_sending(self):
        page = self.page()
        self.page()
        with self.assertRaises(PlatformFlowError):
            prepare(self.browser, "kimi", PROMPT, "job-fixture", bootstrap=True)
        self.context.pages[-1].close()
        journal = prepare(self.browser, "kimi", PROMPT, "job-fixture", bootstrap=True)
        self.assertEqual(journal.data["send_state"], "before_send")
        self.assertEqual(journal.data["target_id"], target_id(page))

    def test_prepare_keeps_original_target_when_other_platform_pages_exist(self):
        page = self.page()
        journal = self.journal(page)
        self.page()
        before = self.file.read_bytes()
        prepared = prepare(self.browser, "kimi", PROMPT, "job-fixture", bootstrap=True)
        self.assertEqual(prepared.data["target_id"], journal.data["target_id"])
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(len(self.context.pages), 2)

    def test_before_send_bootstrap_rebinds_existing_page_and_resume_sends_only_once(self):
        original = self.page("deepseek", prompt="上一条问题")
        self.journal(original, "deepseek")
        original.close()
        page = self.page("deepseek", prompt="上一条问题")
        navigations, new_pages = [], []
        page.on("framenavigated", lambda frame: navigations.append(frame))
        self.context.on("page", lambda opened: new_pages.append(opened))
        before = page.content()
        journal = prepare(self.browser, "deepseek", PROMPT, "job-fixture", bootstrap=True)
        self.assertEqual(journal.data["target_id"], target_id(page))
        self.assertEqual(journal.data["send_state"], "before_send")
        self.assertEqual(page.content(), before)
        self.assertEqual(navigations, [])
        self.assertEqual(new_pages, [])
        saved = RecoveryJournal.from_env("deepseek", PROMPT, "job-fixture")
        self.assertEqual(saved.data["target_id"], target_id(page))
        def send(_page, text, site, *, new_chat, on_send_state):
            self.assertIs(_page, page)
            self.assertFalse(new_chat)
            on_send_state("sending")
            page.evaluate("text => {const n=document.createElement('div');n.dataset.messageRole='user';n.textContent=text;document.body.append(n)}", text)
            on_send_state("sent")
        first, sender = self.scrape_fixture(page, saved, send=send)
        self.assertTrue(first["ok"])
        sender.assert_called_once()
        second, sender = self.scrape_fixture(page, saved)
        self.assertTrue(second["ok"])
        sender.assert_not_called()
        self.assertEqual(navigations, [])
        self.assertEqual(new_pages, [])

    def test_missing_sent_sending_or_unknown_target_never_rebinds(self):
        for state in ("sending", "sent", "unknown"):
            with self.subTest(state=state):
                for page in self.context.pages:
                    page.close()
                self.file.unlink(missing_ok=True)
                original = self.page()
                journal = self.journal(original)
                if state == "unknown":
                    journal.data["send_state"] = state
                    journal.save()
                else:
                    journal.state(state, original)
                original.close()
                replacement = self.page()
                before, content = self.file.read_bytes(), replacement.content()
                with self.assertRaises(PlatformFlowError) as raised:
                    prepare(self.browser, "kimi", PROMPT, "job-fixture", bootstrap=True)
                self.assertEqual(raised.exception.error_code,
                                 "recovery_unconfirmed" if state == "unknown" else "recovery_page_missing")
                self.assertEqual(self.file.read_bytes(), before)
                self.assertEqual(replacement.content(), content)
                self.assertEqual(len(self.context.pages), 1)

    def test_missing_before_send_target_requires_unique_existing_page(self):
        for bootstrap in (False, True):
            for count in (0, 2):
                with self.subTest(bootstrap=bootstrap, candidates=count):
                    for page in self.context.pages:
                        page.close()
                    self.file.unlink(missing_ok=True)
                    original = self.page()
                    self.journal(original)
                    original.close()
                    for _ in range(count):
                        self.page()
                    before = self.file.read_bytes()
                    with self.assertRaises(PlatformFlowError) as raised:
                        prepare(self.browser, "kimi", PROMPT, "job-fixture", bootstrap=bootstrap)
                    self.assertEqual(raised.exception.error_code, "recovery_unconfirmed")
                    self.assertEqual(self.file.read_bytes(), before)
                    self.assertEqual(len(self.context.pages), count)

    def test_missing_before_send_target_rejects_mismatched_identity(self):
        original = self.page()
        self.journal(original)
        original.close()
        self.page()
        before = self.file.read_bytes()
        for site, prompt, question, account in (("deepseek", PROMPT, "job-fixture", "account-fixture"),
                                               ("kimi", "其他问题", "job-fixture", "account-fixture"),
                                               ("kimi", PROMPT, "another-job", "account-fixture"),
                                               ("kimi", PROMPT, "job-fixture", "another-account")):
            with self.subTest(site=site, prompt=prompt, question=question, account=account), \
                 patch.dict(os.environ, {"GEO_WEB_RECOVERY_ACCOUNT_ID": account}), \
                 self.assertRaises(PlatformFlowError) as raised:
                prepare(self.browser, site, prompt, question, bootstrap=True)
            self.assertEqual(raised.exception.error_code, "recovery_unconfirmed")
            self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(len(self.context.pages), 1)

    def test_target_lookup_error_does_not_authorize_rebinding(self):
        original = self.page()
        self.journal(original)
        before = self.file.read_bytes()
        with patch.object(RecoveryJournal, "find", side_effect=RuntimeError("CDP lookup interrupted")), \
             self.assertRaises(RuntimeError):
            prepare(self.browser, "kimi", PROMPT, "job-fixture", bootstrap=True)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(len(self.context.pages), 1)

    def probe_script_fixture(self, *, prepare_enabled):
        script = Path(__file__).resolve().parents[1] / "server/scripts/run_scrape.py"
        spec = importlib.util.spec_from_file_location("web_recovery_probe_script_fixture", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = SimpleNamespace(site="kimi", prompt=PROMPT, question_id="job-fixture",
                               cdp_url="http://127.0.0.1:9", recovery_action="probe",
                               recovery_file=str(self.file))
        fake_pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda *args, **kwargs: self.browser))
        with patch.object(module, "sync_playwright", return_value=nullcontext(fake_pw)), \
             patch.dict(os.environ, {"GEO_WEB_RECOVERY_PREPARE": "1" if prepare_enabled else "0"}), \
             patch.object(module, "scrape_one") as scrape, \
             patch("cn_ai.scraper.send_prompt") as sender:
            try:
                return module._run(args)
            finally:
                scrape.assert_not_called()
                sender.assert_not_called()

    def test_probe_script_prepares_existing_before_send_missing_target_without_sending(self):
        original = self.page()
        self.journal(original)
        original.close()
        replacement = self.page()
        navigations, new_pages = [], []
        replacement.on("framenavigated", lambda frame: navigations.append(frame))
        self.context.on("page", lambda page: new_pages.append(page))
        content = replacement.content()
        result = self.probe_script_fixture(prepare_enabled=True)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["target_id"], target_id(replacement))
        self.assertEqual(result["send_state"], "before_send")
        saved = RecoveryJournal.from_env("kimi", PROMPT, "job-fixture")
        self.assertEqual(saved.data["target_id"], target_id(replacement))
        self.assertEqual(saved.data["send_state"], "before_send")
        self.assertEqual(replacement.content(), content)
        self.assertEqual(navigations, [])
        self.assertEqual(new_pages, [])

    def test_probe_script_prepare_never_rebinds_missing_sent_or_sending_target(self):
        for state in ("sending", "sent"):
            with self.subTest(state=state):
                for page in self.context.pages:
                    page.close()
                self.file.unlink(missing_ok=True)
                original = self.page()
                journal = self.journal(original)
                journal.state(state, original)
                original.close()
                replacement = self.page()
                before, content = self.file.read_bytes(), replacement.content()
                with self.assertRaises(PlatformFlowError) as raised:
                    self.probe_script_fixture(prepare_enabled=True)
                self.assertEqual(raised.exception.error_code, "recovery_page_missing")
                self.assertEqual(self.file.read_bytes(), before)
                self.assertEqual(replacement.content(), content)
                self.assertEqual(len(self.context.pages), 1)

    def test_probe_script_without_prepare_never_rebinds_missing_target(self):
        for state in ("before_send", "sending", "sent"):
            with self.subTest(state=state):
                for page in self.context.pages:
                    page.close()
                self.file.unlink(missing_ok=True)
                original = self.page()
                journal = self.journal(original)
                if state != "before_send":
                    journal.state(state, original)
                original.close()
                replacement = self.page()
                before, content = self.file.read_bytes(), replacement.content()
                result = self.probe_script_fixture(prepare_enabled=False)
                self.assertFalse(result["context_valid"], result)
                self.assertFalse(result["ready"])
                self.assertEqual(result["error_code"], "recovery_page_missing")
                self.assertEqual(self.file.read_bytes(), before)
                self.assertEqual(replacement.content(), content)
                self.assertEqual(len(self.context.pages), 1)

    def test_normal_script_binds_target_before_login_encounters_verification(self):
        page = self.page("yuanbao")
        script = Path(__file__).resolve().parents[1] / "server/scripts/run_scrape.py"
        spec = importlib.util.spec_from_file_location("web_recovery_script_fixture", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = SimpleNamespace(site="yuanbao", prompt=PROMPT, question_id="job-fixture", phone="fixture",
                               cdp_url="http://127.0.0.1:9", recovery_action="")
        def login(browser, phone):
            saved = json.loads(self.file.read_text(encoding="utf-8"))
            self.assertEqual(saved["target_id"], target_id(page))
            self.assertEqual(saved["send_state"], "before_send")
            raise PlatformFlowError("platform_risk_control", "平台触发安全验证", False)
        fake_pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda *args, **kwargs: self.browser))
        with patch.object(module, "sync_playwright", return_value=nullcontext(fake_pw)), \
             patch.object(module, "browser_route", return_value="local"), \
             patch.object(module, "scrape_one") as scrape, \
             patch("cn_ai.diagnostics.current", return_value=None), \
             patch("cn_ai.agentbay_doubao.ensure_yuanbao_login", side_effect=login), \
             self.assertRaises(PlatformFlowError):
            module._run(args)
        scrape.assert_not_called()

    def scrape_fixture(self, page, journal, *, resume=True, send=None):
        extra = {"answer": ANSWER, "citations": [], "complete": True, "prompt_matched": True,
                 "source": "api:history_messages"}
        with ExitStack() as stack:
            stack.enter_context(patch("cn_ai.scraper._wait_deepseek_history", return_value=extra))
            stack.enter_context(patch("cn_ai.scraper._page_logged_out", return_value=False))
            stack.enter_context(patch("cn_ai.scraper.current_diagnostics", return_value=None))
            find = stack.enter_context(patch("cn_ai.scraper.find_or_open_page", return_value=page))
            sender = stack.enter_context(patch("cn_ai.scraper.send_prompt", side_effect=send))
            stack.enter_context(patch("cn_ai.scraper.extract_answer_and_links",
                                     side_effect=budget.StageDeadline("answer_extract_timeout")))
            with redirect_stdout(io.StringIO()):
                result = _scrape_one(self.browser, "deepseek", PROMPT, "job-fixture", self.folder / "result",
                                     "resume" if resume else "")
            if resume:
                find.assert_not_called()
            return result, sender

    def test_sent_resume_and_saved_checkpoint_never_send_or_open_a_new_chat(self):
        page = self.page("deepseek")
        journal = self.journal(page, "deepseek")
        journal.state("sent", page)
        first, sender = self.scrape_fixture(page, journal)
        self.assertTrue(first["ok"])
        self.assertEqual(first["answer"], ANSWER)
        sender.assert_not_called()
        saved = RecoveryJournal.from_env("deepseek", PROMPT, "job-fixture")
        self.assertTrue(saved.data["checkpoint"]["web_answer_completed"])
        second, sender = self.scrape_fixture(page, saved)
        self.assertTrue(second["ok"])
        sender.assert_not_called()

    def test_before_send_resume_sends_once_without_new_chat_then_rejects_normal_replay(self):
        page = self.page("deepseek", prompt="上一条问题")
        journal = self.journal(page, "deepseek")
        def send(_page, text, site, *, new_chat, on_send_state):
            self.assertFalse(new_chat)
            on_send_state("sending")
            page.evaluate("text => {const n=document.createElement('div');n.dataset.messageRole='user';n.textContent=text;document.body.append(n)}", text)
            on_send_state("sent")
        result, sender = self.scrape_fixture(page, journal, send=send)
        self.assertTrue(result["ok"])
        sender.assert_called_once()
        with self.assertRaises(PlatformFlowError) as raised:
            self.scrape_fixture(page, journal, resume=False)
        self.assertEqual(raised.exception.error_code, "recovery_unconfirmed")

    def test_outgoing_request_and_old_same_prompt_protocol_do_not_prove_a_new_turn(self):
        page = self.page("deepseek")
        journal = self.journal(page, "deepseek")
        def uncertain_send(_page, text, site, *, new_chat, on_send_state):
            on_send_state("sending")
            page.evaluate("async text => {await fetch('/api/v0/chat/completion',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({prompt:text})})}", text)
        with self.assertRaises(PlatformFlowError) as raised:
            self.scrape_fixture(page, journal, resume=False, send=uncertain_send)
        self.assertEqual(raised.exception.error_code, "generation_timeout")
        saved = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(saved["send_state"], "sending")
        self.assertTrue(saved["submission_observed"])
        self.assertNotIn("checkpoint", saved)

    def test_probe_cli_reads_same_target_without_platform_network_or_sending(self):
        page = self.page()
        journal = self.journal(page)
        journal.state("sent", page)
        environment = dict(os.environ, GEO_DIAGNOSTICS_ENABLED="0", GEO_DIAGNOSTIC_DEADLINE_MS=str(int((time.time()+20)*1000)),
                           GEO_DIAGNOSTIC_RUN_DIR=str(self.folder / "diagnostics"), GEO_WEB_RECOVERY_PRIOR_LOGGED_IN="1")
        script = Path(__file__).resolve().parents[1] / "server/scripts/run_scrape.py"
        result = subprocess.run([sys.executable, str(script), "--cdp-url", f"http://127.0.0.1:{self.port}",
                                 "--site", "kimi", "--prompt", PROMPT, "--question-id", "job-fixture",
                                 "--recovery-action", "probe", "--recovery-file", str(self.file)],
                                env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
        payload = json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(payload["ready"], payload)
        self.assertEqual(payload["target_id"], target_id(page))
        self.assertEqual(len(self.context.pages), 1)


if __name__ == "__main__":
    unittest.main()
