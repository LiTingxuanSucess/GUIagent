import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from cn_ai.manual_workflow import busy_rejection, new_manual_turn, refresh_busy, restore_original_turn, run
from cn_ai.site_flows import PlatformFlowError
from cn_ai.web_recovery import RecoveryJournal, display_prompt_matches, snapshot

PROMPT = "本次要提交的问题"


class ManualWorkflowTest(unittest.TestCase):
    def test_doubao_typographic_spaces_do_not_change_chinese_question_identity(self):
        self.assertTrue(display_prompt_matches('旧车卖给 4S 店有哪些费用？', '旧车卖给4S店有哪些费用？', 'doubao'))
        self.assertFalse(display_prompt_matches('对比 Model Y 和 4S 店', '对比 ModelY 和 4S 店', 'doubao'))
        self.assertFalse(display_prompt_matches('买 4S 车有哪些费用？', '买4S车有哪些风险？', 'doubao'))

    def test_automatic_reconcile_reads_matching_completed_turn_without_sending(self):
        self.add_user()
        with patch('cn_ai.scraper.scrape_one', return_value={'ok': True, 'answer': '原回答'}):
            run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-submitted')
        self.page.evaluate("history.pushState({},'', '/chat/verified-replacement')")
        self.page.locator('[data-message-role="user"]').evaluate("e=>e.dataset.messageId='verified-turn'")
        with patch('cn_ai.scraper.scrape_one', return_value={'ok': True, 'answer': '已生成回答'}) as collect:
            run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-reconcile')
        saved = RecoveryJournal.from_env('kimi', PROMPT, 'fixture-job').data
        self.assertEqual(saved['manual_turn_history'][-1]['reason'], 'automatic_verified_current_turn')
        self.assertEqual(collect.call_args.kwargs['recovery_action'], 'resume')
        self.assertEqual(snapshot(self.page, 'kimi')['user_count'], 1)
    def test_restore_is_supported_by_real_scraper_command(self):
        script = Path(__file__).resolve().parents[1] / 'server/scripts/run_scrape.py'
        result = subprocess.run([sys.executable, str(script), '--site', 'kimi', '--prompt', PROMPT,
                                 '--cdp-url', 'http://127.0.0.1:9', '--recovery-action', 'manual-restore'],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertNotIn('invalid choice', result.stderr)
        self.assertFalse(json.loads(result.stdout.strip().splitlines()[-1])['ok'])

    def test_busy_refresh_without_explicit_rejection_never_resends(self):
        self.journal.state('sending',self.page)
        with patch('cn_ai.manual_workflow.run') as collect, self.assertRaises(PlatformFlowError) as caught:
            refresh_busy(self.browser,self.journal,self.page,'kimi',PROMPT,'fixture-job')
        self.assertEqual(caught.exception.error_code,'manual_send_unconfirmed')
        collect.assert_not_called()

    def test_explicit_rejection_can_retry_with_durable_history(self):
        self.page.locator('body').evaluate("e=>{const n=document.createElement('div');n.textContent='和Kimi聊天的人太多了，订阅会员可进入优先队列';e.append(n)}")
        self.assertTrue(busy_rejection(self.page,'kimi'))
        self.journal.state('sending',self.page)
        with patch('cn_ai.manual_workflow.run',return_value={'ok':True}) as collect:
            refresh_busy(self.browser,self.journal,self.page,'kimi',PROMPT,'fixture-job')
        self.assertEqual(collect.call_args.args[-1],'manual-auto')
        saved=RecoveryJournal.from_env('kimi',PROMPT,'fixture-job')
        self.assertEqual(saved.data['send_state'],'before_send')
        self.assertEqual(saved.data['busy_retry_history'][0]['reason'],'explicit_platform_busy_rejection')

    def test_busy_refresh_never_resets_a_confirmed_send(self):
        self.page.locator('body').evaluate("e=>{const n=document.createElement('div');n.textContent='和Kimi聊天的人太多了';e.append(n)}")
        self.journal.state('sent',self.page)
        with patch('cn_ai.manual_workflow.run',return_value={'ok':True}) as collect:
            refresh_busy(self.browser,self.journal,self.page,'kimi',PROMPT,'fixture-job')
        self.assertEqual(collect.call_args.args[-1],'manual-continue')
        self.assertEqual(RecoveryJournal.from_env('kimi',PROMPT,'fixture-job').data['send_state'],'sent')

    def test_wenxin_share_picker_restores_editor_without_changing_question(self):
        from cn_ai.share_links import dismiss_share_ui
        self.add_user()
        self.page.evaluate("""() => {
          document.querySelector('textarea').style.display='none';
          const picker=document.createElement('div');picker.className='share-footer';
          picker.innerHTML='<button class="cos-icon-close">关闭分享</button>';
          picker.querySelector('button').onclick=()=>{picker.remove();document.querySelector('textarea').style.display=''};
          document.body.append(picker);
        }""")
        self.assertTrue(dismiss_share_ui(self.page, 'yiyan'))
        self.assertEqual(snapshot(self.page, 'kimi')['last_user'], PROMPT)
        self.assertEqual(snapshot(self.page, 'kimi')['user_count'], 1)
        self.assertTrue(snapshot(self.page, 'kimi')['editor_ready'])

    def test_next_question_closes_previous_share_picker_without_resending(self):
        self.page.evaluate("""() => {
          document.querySelector('textarea').style.display='none';
          const picker=document.createElement('div');
          picker.className='chat-action--share-mode';
          picker.innerHTML='<div class="share-bottom-container"><button class="cancel-button">取消</button></div>';
          picker.querySelector('button').onclick=()=>{picker.remove();document.querySelector('textarea').style.display=''};
          document.body.append(picker);
        }""")
        self.assertFalse(snapshot(self.page, 'kimi')['editor_ready'])
        with patch('cn_ai.scraper.scrape_one', return_value={'ok': True, 'answer': '下一题回答'}) as collect:
            result = run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-auto')
        self.assertEqual(result['answer'], '下一题回答')
        self.assertTrue(snapshot(self.page, 'kimi')['editor_ready'])
        self.assertEqual(snapshot(self.page, 'kimi')['user_count'], 0)
        collect.assert_called_once()

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "journal.json"
        self.env = patch.dict(os.environ, {"GEO_WEB_RECOVERY_FILE": str(self.path),
                                         "GEO_WEB_RECOVERY_ACCOUNT_ID": "fixture-account",
                                         "GEO_DIAGNOSTIC_DEADLINE_MS": "0"})
        self.env.start()
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html></html>"))
        self.page = self.context.new_page()
        self.page.goto("https://www.kimi.com/chat/current")
        self.page.set_content("<button class='user-info-container'>当前账号</button><textarea></textarea>")
        self.journal = RecoveryJournal.from_env("kimi", PROMPT, "fixture-job", create=True)
        self.journal.bind(self.page)
        run(self.browser, "kimi", PROMPT, "fixture-job", "manual-pause")

    def tearDown(self):
        self.context.close()
        self.env.stop()
        self.tmp.cleanup()

    def add_user(self, text=PROMPT, identity="new-turn"):
        self.page.evaluate("([text,id])=>{const n=document.createElement('div');n.dataset.messageRole='user';n.dataset.messageId=id;n.textContent=text;document.body.append(n)}", [text, identity])

    def test_new_tab_unsent_baseline_requires_an_empty_ready_page(self):
        self.journal.data.update(manual_new_tab=True, manual_baseline={
            'user_count': 2, 'user_turn_id': 'old-turn', 'url': 'https://www.kimi.com/chat/closed'})
        self.journal.save()
        with patch('cn_ai.scraper.scrape_one', return_value={'ok': True, 'answer': '下一题回答'}) as collect:
            result = run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-continue')
        self.assertEqual(result['answer'], '下一题回答')
        saved = RecoveryJournal.from_env('kimi', PROMPT, 'fixture-job')
        self.assertEqual(saved.data['send_state'], 'before_send')
        self.assertEqual(saved.data['manual_baseline']['user_count'], 0)
        self.assertEqual(saved.data['manual_baseline']['url'], self.page.url)
        collect.assert_called_once()

    def test_new_tab_keeps_sent_state_and_rejects_an_unrelated_conversation(self):
        self.journal.data.update(manual_new_tab=True)
        self.journal.state('sent', self.page)
        self.add_user('另一道题')
        with patch('cn_ai.scraper.scrape_one') as collect, self.assertRaises(PlatformFlowError):
            run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-continue')
        collect.assert_not_called()
        saved = RecoveryJournal.from_env('kimi', PROMPT, 'fixture-job')
        self.assertEqual(saved.data['send_state'], 'sent')
        self.assertTrue(saved.data['manual_new_tab'])

    def test_new_tab_unsent_does_not_rebase_over_an_existing_question(self):
        self.journal.data.update(manual_new_tab=True)
        self.journal.save()
        self.add_user('另一道题')
        with patch('cn_ai.scraper.scrape_one') as collect, self.assertRaises(PlatformFlowError):
            run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-continue')
        collect.assert_not_called()

    def test_new_tab_manual_submission_can_be_confirmed_without_an_old_baseline(self):
        self.journal.data.pop('manual_baseline', None)
        self.journal.data['manual_new_tab'] = True
        self.journal.save()
        self.add_user()
        with patch('cn_ai.scraper.scrape_one', return_value={'ok': True, 'answer': '人工提交后的回答'}) as collect:
            result = run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-submitted')
        self.assertEqual(result['answer'], '人工提交后的回答')
        self.assertEqual(result['submission_mode'], 'manual')
        saved = RecoveryJournal.from_env('kimi', PROMPT, 'fixture-job')
        self.assertEqual(saved.data['send_state'], 'sent')
        self.assertNotIn('manual_new_tab', saved.data)
        collect.assert_called_once()

    def test_continue_collects_the_exact_answer_already_present_in_a_new_tab(self):
        self.journal.data.pop('manual_baseline', None)
        self.journal.data['manual_new_tab'] = True
        self.journal.save()
        self.add_user()
        def collect(browser, site, prompt, **kwargs):
            saved = RecoveryJournal.from_env(site, prompt, 'fixture-job')
            self.assertEqual(saved.data['send_state'], 'sent')
            self.assertEqual(kwargs['recovery_action'], 'resume')
            return {'ok': True, 'answer': '本题已完成的网页回答'}
        with patch('cn_ai.scraper.scrape_one', side_effect=collect) as collect_mock, patch('cn_ai.site_flows.send_prompt') as send:
            result = run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-continue')
        self.assertTrue(result['ok'])
        collect_mock.assert_called_once()
        send.assert_not_called()

    def test_wenxin_continue_can_bind_latest_completed_pair_in_a_multi_turn_page(self):
        from cn_ai.test_yiyan_completion import qa
        page = self.context.new_page()
        page.goto('https://wenxin.baidu.com/search/current')
        page.set_content('<header><div class="user-avatar">当前账号</div></header><textarea></textarea>'
                         + qa(question=PROMPT, answer='上一轮回答，不能拿来覆盖当前本题的回答。')
                         + qa(question=PROMPT))
        self.path.unlink()
        journal = RecoveryJournal.from_env('yiyan', PROMPT, 'fixture-job', create=True)
        journal.bind(page)
        journal.data['manual_new_tab'] = True
        journal.save()
        with patch('cn_ai.scraper.scrape_one', return_value={'ok':True,'answer':'最新一轮网页正文'}) as collect, patch('cn_ai.site_flows.send_prompt') as send:
            result=run(self.browser,'yiyan',PROMPT,'fixture-job','manual-continue')
        self.assertTrue(result['ok'])
        collect.assert_called_once()
        send.assert_not_called()
        saved=RecoveryJournal.from_env('yiyan',PROMPT,'fixture-job')
        self.assertEqual(saved.data['manual_turn']['user_count'],2)

    def test_new_tab_manual_confirmation_rejects_other_or_repeated_questions(self):
        for questions in [('另一道题',), (PROMPT, PROMPT)]:
            with self.subTest(questions=questions):
                self.page.locator('[data-message-role="user"]').evaluate_all('nodes=>nodes.forEach(n=>n.remove())')
                self.journal.data['manual_new_tab'] = True
                self.journal.save()
                for question in questions:
                    self.add_user(question)
                with patch('cn_ai.scraper.scrape_one') as collect, self.assertRaises(PlatformFlowError):
                    run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-submitted')
                collect.assert_not_called()

    def test_manual_submission_resumes_without_resending(self):
        self.add_user()
        self.page.evaluate("()=>{const n=document.createElement('div');n.id='answer';n.textContent='人工提交后的真实回答';document.body.append(n)}")
        def collect(browser, site, prompt, *, question_id, recovery_action):
            self.assertEqual(recovery_action, "resume")
            saved = RecoveryJournal.from_env(site, prompt, question_id)
            self.assertEqual(saved.data["send_state"], "sent")
            self.assertEqual(saved.data["manual_turn"]["user_turn_id"], "new-turn")
            return {"ok": True, "answer": self.page.locator("#answer").inner_text()}
        with patch("cn_ai.scraper.scrape_one", side_effect=collect) as collect_mock:
            result = run(self.browser, "kimi", PROMPT, "fixture-job", "manual-submitted")
        self.assertEqual(result["answer"], "人工提交后的真实回答")
        self.assertEqual(result["submission_mode"], "manual")
        collect_mock.assert_called_once()
        self.assertEqual(snapshot(self.page, "kimi")["user_count"], 1)

    def test_wrong_or_old_identical_question_is_not_accepted(self):
        for text in ("其他问题", PROMPT):
            with self.subTest(text=text):
                self.page.locator('[data-message-role="user"]').evaluate_all("nodes=>nodes.forEach(n=>n.remove())")
                self.add_user(text, "old-turn")
                run(self.browser, "kimi", PROMPT, "fixture-job", "manual-pause")
                with patch("cn_ai.scraper.scrape_one") as collect, self.assertRaises(PlatformFlowError):
                    run(self.browser, "kimi", PROMPT, "fixture-job", "manual-submitted")
                collect.assert_not_called()

    def test_uncertain_send_never_automatically_resubmits(self):
        journal = RecoveryJournal.from_env("kimi", PROMPT, "fixture-job")
        journal.state("sending", self.page)
        with patch("cn_ai.scraper.scrape_one") as collect, self.assertRaises(PlatformFlowError) as error:
            run(self.browser, "kimi", PROMPT, "fixture-job", "manual-continue")
        self.assertEqual(error.exception.error_code, "manual_send_unconfirmed")
        collect.assert_not_called()

    def test_changed_conversation_rejected_after_confirmation(self):
        self.add_user()
        with patch("cn_ai.scraper.scrape_one", return_value={"ok": True, "answer": "answer"}):
            run(self.browser, "kimi", PROMPT, "fixture-job", "manual-submitted")
        self.page.evaluate("history.pushState({},'', '/chat/another')")
        with patch("cn_ai.scraper.scrape_one") as collect, self.assertRaises(PlatformFlowError):
            run(self.browser, "kimi", PROMPT, "fixture-job", "manual-continue")
        collect.assert_not_called()

    def test_explicit_current_turn_confirmation_preserves_old_identity(self):
        self.add_user()
        with patch('cn_ai.scraper.scrape_one', return_value={'ok': True, 'answer': '原回答'}):
            run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-submitted')
        original = self.page.url
        self.page.evaluate("history.pushState({},'', '/chat/operator-replacement')")
        self.page.locator('[data-message-role="user"]').evaluate("e=>e.dataset.messageId='operator-turn'")
        with patch('cn_ai.scraper.scrape_one', return_value={'ok': True, 'answer': '人工补采回答'}) as collect:
            result = run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-confirm-current')
        saved = RecoveryJournal.from_env('kimi', PROMPT, 'fixture-job').data
        self.assertEqual(saved['manual_turn_history'][0]['previous_turn']['url'], original)
        self.assertEqual(saved['manual_turn']['user_turn_id'], 'operator-turn')
        self.assertEqual(result['submission_mode'], 'manual')
        self.assertEqual(collect.call_args.kwargs['recovery_action'], 'resume')

    def test_current_turn_confirmation_rejects_mixed_or_wrong_question(self):
        self.journal.state('sent', self.page)
        for messages in ([PROMPT, PROMPT], ['其他问题']):
            self.page.locator('[data-message-role="user"]').evaluate_all('nodes=>nodes.forEach(n=>n.remove())')
            for i, text in enumerate(messages):
                self.add_user(text, str(i))
            with self.subTest(messages=messages), patch('cn_ai.scraper.scrape_one') as collect, self.assertRaises(PlatformFlowError):
                run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-confirm-current')
            collect.assert_not_called()
            self.assertNotIn('manual_turn_history', RecoveryJournal.from_env('kimi', PROMPT, 'fixture-job').data)

    def test_explicit_restore_reads_recorded_conversation_without_resending(self):
        self.add_user()
        with patch('cn_ai.scraper.scrape_one', return_value={'ok': True, 'answer': '原回答'}):
            run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-submitted')
        original = self.page.url
        self.context.route(original, lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=
                           '<button class="user-info-container">当前账号</button><textarea></textarea>'
                           '<div data-message-role="user" data-message-id="new-turn">'+PROMPT+'</div>'))
        self.page.goto('https://www.kimi.com/chat/other')
        with patch('cn_ai.scraper.scrape_one', return_value={'ok': True, 'answer': '原回答'}) as collect:
            run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-restore')
        self.assertEqual(self.page.url, original)
        self.assertEqual(collect.call_args.kwargs['recovery_action'], 'resume')

    def test_restore_rejects_external_or_nonpersistent_addresses(self):
        for url in ['https://example.org/chat/original', 'https://www.doubao.com/chat/local_1234',
                    'https://www.kimi.com/', 'https://user@www.kimi.com/chat/original']:
            data = {'send_state': 'sent', 'manual_turn': {'url': url}}
            with self.subTest(url=url), self.assertRaises(PlatformFlowError):
                restore_original_turn(self.page, 'kimi' if 'kimi' in url else 'doubao', data)

    def test_expired_login_is_not_reported_as_changed_question(self):
        self.journal.state('sent', self.page)
        self.page.goto('https://www.kimi.com/sign_in')
        with patch('cn_ai.scraper.scrape_one') as collect, self.assertRaises(PlatformFlowError) as caught:
            run(self.browser, 'kimi', PROMPT, 'fixture-job', 'manual-continue')
        self.assertEqual(caught.exception.error_code, 'platform_auth_unavailable')
        collect.assert_not_called()

    def test_missing_original_page_never_creates_replacement(self):
        self.page.close()
        with patch("cn_ai.scraper.scrape_one") as collect, self.assertRaises(PlatformFlowError):
            run(self.browser, "kimi", PROMPT, "fixture-job", "manual-submitted")
        collect.assert_not_called()
        self.assertEqual(len(self.context.pages), 0)

    def test_url_change_alone_is_not_new_submission_evidence(self):
        data={"prompt": PROMPT, "manual_baseline": {"user_count": 1, "user_turn_id": "old", "url": "old-url"}}
        self.assertFalse(new_manual_turn({"last_user": PROMPT, "user_count": 1, "user_turn_id": "old"}, data, "new-url"))


if __name__ == "__main__":
    unittest.main()
