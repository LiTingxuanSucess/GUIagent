import json
import io
from contextlib import redirect_stdout
import os
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from playwright.sync_api import sync_playwright
from cn_ai.diagnostics import DiagnosticRun
from cn_ai.network_parser import parse_kimi_connect
from cn_ai.share_links import create_public_share, verify_public_share, _snippets_visible, _answer_snippets, observed_public_share_url, is_public_share_url, share_answer_hint
from cn_ai.scraper import _page_fetch_text, _wait_yiyan_history, _scrape_one
from cn_ai.site_flows import dismiss_kimi_notice, disable_kimi_knowledge_base, run_kimi_prep, PlatformFlowError
from cn_ai import agentbay_doubao as cloud


class WebQualityTest(unittest.TestCase):
    def test_completed_answer_is_committed_before_share_failure(self):
        answer='正文已经生成完成，包含真实来源，分享失败时也应保留完整回答和引用。'
        with tempfile.TemporaryDirectory() as folder,sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page();page.set_content('<main><div data-conv-speaker="human">当前问题</div>当前回答</main>')
            output=io.StringIO()
            def completed_wait(*args,**kwargs):
                kwargs['on_completed']('dom_state')
                return answer
            with patch('cn_ai.scraper.find_or_open_page',return_value=page),patch('cn_ai.scraper._page_logged_out',return_value=False),patch('cn_ai.scraper.send_prompt'),patch('cn_ai.scraper.wait_for_answer_growth',side_effect=completed_wait),patch('cn_ai.scraper._fetch_yuanbao_detail',return_value=None),patch('cn_ai.scraper.extract_answer_and_links',return_value={'text':answer,'links':['https://example.org/real'],'selector':'yuanbao-answer-finished'}),patch('cn_ai.scraper.merge_captures',return_value=(answer,['https://example.org/real'],'dom:answer')),patch('cn_ai.scraper.create_public_share',side_effect=RuntimeError('share timeout')),patch('cn_ai.scraper.time.sleep'),redirect_stdout(output):
                kept = _scrape_one(browser,'yuanbao','当前问题',question_id='job_current',out_dir=Path(folder))
                self.assertTrue(kept['ok'])
                self.assertEqual(kept['answer'],answer)
            checkpoints=[json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
            self.assertEqual(len(checkpoints),1)
            result=checkpoints[0]
            self.assertTrue(result['web_answer_completed']);self.assertTrue(result['ok'])
            self.assertEqual(result['question_id'],'job_current');self.assertEqual(result['prompt'],'当前问题')
            self.assertEqual(result['answer'],answer);self.assertEqual(result['citations'],['https://example.org/real'])
            self.assertEqual(result['url'],'');self.assertEqual(result['share_error_code'],'share_timeout')
            browser.close()

    def test_yiyan_share_proves_actual_answer_instead_of_followup_offer(self):
        answer='哈希值是文件的数字指纹，重新计算并比较摘要能够检查文件是否发生变化。'
        share={'candidates':[answer+'\n\n如果需要，我可以帮你制作一份操作手册。\n复制链接\n']}
        self.assertEqual(share_answer_hint('yiyan',share,answer),answer)
    def test_yiyan_opens_completed_task_card_and_reads_final_answer(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            answer='哈希值是文件的数字指纹，重新计算并比较摘要能够检查文件是否发生变化。'
            page.set_content('''<div class="conversation-flow-question-container"><div class="cs-question-pure-text">如何校验文件？</div></div><div data-show-ext='{"value":"task_card"}' onclick="document.querySelector('#answer').style.display='block'">任务已完成</div>
                <div data-show-ext='{"value":"task_finish_notice"}' onclick="window.wrongControl=1">任务已完成</div>
                <div class="ai-entry _task-detail-answer_fixture"><div id="answer" class="marklang" style="display:none">'''+answer+'<a href="https://example.org/hash">参考</a></div></div>')
            with patch('cn_ai.scraper._fetch_yiyan_history',return_value=None),patch('cn_ai.scraper.time.sleep'):
                result=_wait_yiyan_history(page,'如何校验文件？',timeout_sec=2)
            self.assertEqual(result['source'],'dom:yiyan-task')
            self.assertIn(answer,result['answer'])
            self.assertIn('https://example.org/hash',result['citations'])
            self.assertFalse(page.evaluate('!!window.wrongControl'))
            browser.close()
    def test_malformed_citation_does_not_abort_share_extraction(self):
        malformed='https://www.kepuchina.cn（对应材料：科普中国《网盘的秒传真的是传的快吗》）'
        public='https://qianwen.my.cn/share/chat/fixture-share-abcdefgh?lang=zh-CN'
        self.assertFalse(is_public_share_url('qianwen',malformed))
        self.assertEqual(observed_public_share_url('qianwen',[malformed,public]),public)

    def test_page_history_request_has_bounded_timeout(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path=='/pending':time.sleep(.4)
                self.send_response(200);self.end_headers()
                try:self.wfile.write(b'<html><body>fixture</body></html>')
                except (BrokenPipeError,ConnectionResetError):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        Thread(target=server.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True);page=browser.new_page()
                base='http://127.0.0.1:'+str(server.server_port)
                page.goto(base)
                started=time.monotonic()
                self.assertIsNone(_page_fetch_text(page,base+'/pending',timeout_ms=100))
                self.assertLess(time.monotonic()-started,2)
                browser.close()
        finally:
            server.shutdown();server.server_close()
    def test_start_chrome_failure_recreates_once_without_uploading_readonly_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            lease=cloud.AgentBayDoubaoLease(state_path=Path(folder)/'state.json',auto_upload=False)
            client=MagicMock()
            client.context.get.return_value=SimpleNamespace(context=SimpleNamespace(id='saved-login'))
            sessions=[MagicMock(),MagicMock()]
            for index,session in enumerate(sessions):
                session.session_id=str(index)
                session.keep_alive.return_value=SimpleNamespace(success=True)
                session.browser.get_endpoint_url.return_value='ws://test.invalid'
            client.create.side_effect=[SimpleNamespace(session=session) for session in sessions]
            client.delete.return_value=SimpleNamespace(success=True)
            with patch.dict(os.environ,{'AGENTBAY_API_KEY':'fixture','GEO_AGENTBAY_WARM_SEC':'0'}),patch('agentbay.AgentBay',return_value=client),patch.object(lease,'_acquire_slot'),patch.object(lease,'_acquire_startup_slot'),patch.object(lease,'_start_browser',side_effect=['startChromeByCdp DEFAULT_SYS_ERROR','']) as init:
                with lease:
                    self.assertEqual(lease.context_id,'saved-login')
                self.assertEqual(init.call_count,2)
            self.assertEqual(client.create.call_count,2)
            for call in client.create.call_args_list:
                self.assertFalse(call.args[0].browser_context.auto_upload)
                self.assertEqual(call.args[0].browser_context.context_id,'saved-login')
            for call in client.delete.call_args_list:
                self.assertFalse(call.kwargs['sync_context'])

    def test_test_mode_does_not_start_sms_login(self):
        page=MagicMock();page.url='https://www.doubao.com/chat/'
        browser=SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
        with patch.dict(os.environ,{'GEO_BROWSER_AUTO_LOGIN':'0'}),patch.object(cloud,'wait_doubao_auth',return_value='logged_out'),patch.object(cloud,'detect_platform_failure'),patch.object(cloud,'_SMSInboxClient') as inbox:
            with self.assertRaises(cloud.AgentBayDoubaoError) as failure:
                cloud.ensure_doubao_login(browser)
            self.assertEqual(failure.exception.error_code,'platform_auth_unavailable')
            inbox.assert_not_called()

    def test_kimi_connect_frames_respect_braces_inside_text_and_key_order(self):
        frames = [
            {"mask":"block.text.content", "op":"append", "block":{"text":{"content":"正文中有未闭合的 {，引号 \" 和转义字符。"}}},
            {"op":"set","mask":"block.tool.searchChunks","block":{"tool":{"searchChunks":[{"url":"https://example.org/source","title":"参考"}]}}},
        ]
        body=''.join(json.dumps(frame,ensure_ascii=False,indent=1) for frame in frames)
        answer,citations=parse_kimi_connect(body)
        self.assertEqual(answer,frames[0]['block']['text']['content'])
        self.assertIn('https://example.org/source',citations)

    def test_escaped_share_answer_is_recognized_without_accepting_unrelated_text(self):
        answer='哈希用于检查文件内容是否发生变化，是固定长度的数据指纹。'
        snippets=_answer_snippets(answer)
        self.assertTrue(_snippets_visible(snippets,json.dumps({'answer':answer},ensure_ascii=True)))
        self.assertFalse(_snippets_visible(snippets,'这是完全不同的问题和回答。'))

    def test_quality_failure_retains_evidence_even_when_answer_succeeded(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'GEO_DIAGNOSTIC_DIR':folder,'GEO_DIAGNOSTIC_SUCCESS_PERCENT':'0'}):
            with DiagnosticRun('qianwen','quality-test','问题') as trace:
                with patch.object(trace,'checkpoint'):
                    trace.retain_quality_issue(['share_generation_failed'],None)
                trace.meta['outcome']='succeeded'
            meta=json.loads((trace.path/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(meta['status'],'partial')
            self.assertEqual(meta['quality_issues'],['share_generation_failed'])

    def test_real_qianwen_share_requires_copy_action(self):
        public='https://qianwen.my.cn/share/chat/fixture-share-abcdefgh?lang=zh-CN'
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);context=browser.new_context();page=context.new_page()
            def route(request):
                if '/api/' in request.request.url:
                    request.fulfill(content_type='application/json',body=json.dumps({'url':public}))
                else:
                    request.fulfill(content_type='text/html; charset=utf-8',body='''<html><body>
                     <div data-session-active="true"><button data-session-switch-target="session-tools" onclick="document.querySelector('[role=menuitem]').style.display='block'">更多</button></div>
                     <button role="menuitem" style="display:none" onclick="document.querySelector('#share-modal-root').style.display='block'">分享此对话</button>
                     <div id="share-modal-root" style="display:none"><button id="copy" onclick="copyShare()">复制链接</button><div id="link"></div></div>
                     <script>window.copyCount=0;async function copyShare(){window.copyCount++;let r=await fetch('/api/v2/share/create',{method:'POST'});let v=await r.json();document.querySelector('#link').textContent=v.url;await navigator.clipboard.writeText(v.url);}</script>
                    </body></html>''')
            page.route('https://www.qianwen.com/**',route)
            page.goto('https://www.qianwen.com/')
            result=create_public_share(page,'qianwen')
            self.assertEqual(result['url'],public,result)
            self.assertIn('复制链接',result['clicked'])
            self.assertEqual(page.evaluate('copyCount'),1)
            browser.close()

    def test_public_spa_is_checked_in_anonymous_context_before_http_retries(self):
        public='https://www.kimi.com/share/fixture-share-abcdefgh'
        answer='哈希可以帮助校验文件内容是否发生变化，是一种数据指纹。'
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            logged=browser.new_context();logged.add_cookies([{'name':'auth','value':'private','domain':'www.kimi.com','path':'/'}])
            original=browser.new_context;seen=[]
            def fresh():
                context=original();seen.append(context.cookies())
                # Page routing takes precedence over the verifier's resource
                # filter on its fresh context; the fixture must stay offline.
                context.on('page',lambda page:page.route('https://www.kimi.com/**',lambda route:route.fulfill(content_type='text/html; charset=utf-8',body='<html><body><script>setTimeout(()=>document.body.textContent='+json.dumps(answer,ensure_ascii=False)+',100)</script></body></html>')))
                return context
            with patch.object(browser,'new_context',side_effect=fresh),patch('cn_ai.share_links._http_get',return_value=('app shell',public,200)) as http:
                result=verify_public_share(browser,'kimi',public,answer)
            self.assertTrue(result['ok'],result);self.assertEqual(result['method'],'browser')
            self.assertEqual(http.call_count,1);self.assertEqual(seen,[[]]);self.assertEqual(len(browser.contexts),1)
            browser.close()

    def test_kimi_promotion_is_closed_without_following_offer(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('<div class="common-notice-dialog"><button onclick="this.parentNode.remove();window.closedNotice=1">我知道了</button><button onclick="window.followedOffer=1">去体验</button></div>')
            self.assertTrue(dismiss_kimi_notice(page));self.assertFalse(dismiss_kimi_notice(page))
            self.assertEqual(page.evaluate('closedNotice'),1);self.assertFalse(page.evaluate('!!window.followedOffer'))
            browser.close()

    def test_kimi_knowledge_base_off_is_idempotent_and_search_stays_on(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('''<button class="toolkit-trigger-btn" aria-expanded="true">工具</button>
              <div class="toolkit-container">
              <button id="kb" role="menuitem" class="toolkit-item toolkit-item--selected" onclick="this.classList.toggle('toolkit-item--selected');window.kbClicks=(window.kbClicks||0)+1">知识库</button>
              <button id="web" role="menuitem" class="toolkit-item toolkit-item--selected" onclick="this.classList.toggle('toolkit-item--selected');window.webClicks=(window.webClicks||0)+1">联网搜索</button>
              </div>''')
            self.assertEqual(disable_kimi_knowledge_base(page),'off')
            self.assertEqual(disable_kimi_knowledge_base(page),'off')
            self.assertEqual(page.evaluate('kbClicks'),1)
            result=run_kimi_prep(page)
            self.assertIn('search:on',result)
            self.assertFalse(page.evaluate('!!window.webClicks'))
            self.assertNotIn('toolkit-item--selected',page.locator('#kb').get_attribute('class'))
            self.assertIn('toolkit-item--selected',page.locator('#web').get_attribute('class'))
            browser.close()

    def test_kimi_unknown_knowledge_toggle_stops_sending(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('<div class="toolkit-item">知识库</div>')
            with self.assertRaises(PlatformFlowError) as failure:
                disable_kimi_knowledge_base(page)
            self.assertEqual(failure.exception.error_code,'platform_setting_unavailable')
            browser.close()

    def test_kimi_switch_outside_toolkit_is_disabled(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('''<button role="switch" aria-checked="true" onclick="this.setAttribute('aria-checked','false')">知识库</button>''')
            run_kimi_prep(page)
            self.assertEqual(page.get_by_role('switch').get_attribute('aria-checked'),'false')
            browser.close()


if __name__=='__main__':unittest.main()
