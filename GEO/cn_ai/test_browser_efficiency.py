import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch,MagicMock

from playwright.sync_api import sync_playwright
from cn_ai.diagnostics import DiagnosticRun
from cn_ai.share_links import create_public_share
from cn_ai.site_flows import wait_for_answer_growth,send_prompt,page_answers,dismiss_doubao_desktop_notice,start_new_chat,PlatformFlowError,find_send_button
from cn_ai import agentbay_doubao as cloud
from cn_ai.scraper import NetworkCapture,find_or_open_page
from cn_ai.network_parser import parse_yiyan_task_sources,merge_captures


class BrowserEfficiencyTest(unittest.TestCase):
    def test_yuanbao_current_output_marker_and_finished_toolbar_complete_the_reply(self):
        final='这是本题已经完整生成的网页回答，正文和来源都保留在当前对话中。'*4
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('<div data-conv-speaker="human">当前问题</div>'
                '<div data-conv-speaker="ai" data-conv-outputting="false">'+final+
                '<div class="agent-chat__toolbar__right"><div class="agent-chat__question-toolbar__copy-wrapper" style="width:20px;height:20px"></div></div></div>')
            completed=[]
            answer=wait_for_answer_growth(page,timeout_sec=8,site_id='yuanbao',on_completed=completed.append)
            self.assertEqual(answer,final)
            self.assertEqual(completed,['dom_state'])
            for output,toolbar in [('true',True),('false',False)]:
                page.locator('[data-conv-speaker="ai"]').evaluate('(e,v)=>e.setAttribute("data-conv-outputting",v)',output)
                page.locator('.agent-chat__toolbar__right').evaluate('(e,on)=>e.style.display=on?"block":"none"',toolbar)
                completed=[]
                with self.assertRaises(PlatformFlowError):
                    wait_for_answer_growth(page,timeout_sec=.1,site_id='yuanbao',on_completed=completed.append)
                self.assertEqual(completed,[])
            browser.close()
    def test_doubao_incomplete_answer_is_not_accepted_when_wait_expires(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('<div>用户abcd1234</div><div data-message-role="assistant"><div data-testid="message_text_content">尚未生成完整的正文片段</div></div>')
            with self.assertRaises(PlatformFlowError) as raised:
                wait_for_answer_growth(page,timeout_sec=.1,site_id='doubao')
            self.assertEqual(raised.exception.error_code,'platform_busy')
            browser.close()

    def test_doubao_new_composer_sends_once_without_clicking_last_button(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('''<div>用户abcd1234</div>
              <div class="tiptap ProseMirror" contenteditable="true" style="width:300px;height:50px"></div>
              <button id="flow-end-msg-send" data-testid="chat_input_send_button" onclick="window.sent=(window.sent||0)+1"></button>
              <button onclick="window.uploaded=true">上传附件</button>''')
            _,method=send_prompt(page,'平价连锁咖啡推荐，请给参考来源','doubao',new_chat=False)
            self.assertEqual(method,'button')
            self.assertEqual(page.evaluate('window.sent'),1)
            self.assertFalse(page.evaluate('!!window.uploaded'))
            browser.close()

    def test_doubao_missing_send_control_never_selects_attachment_button(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('<div contenteditable="true"></div><button onclick="window.uploaded=true">上传附件</button>')
            self.assertIsNone(find_send_button(page,allow_last_button=False))
            self.assertFalse(page.evaluate('!!window.uploaded'))
            browser.close()

    def test_kimi_new_chat_uses_client_navigation_without_full_reload(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page();home_calls=[]
            def route(r):
                if r.request.url=='https://www.kimi.com/':home_calls.append(1)
                r.fulfill(content_type='text/html; charset=utf-8',body='''<a aria-label="新建会话" href="/" onclick="event.preventDefault();history.pushState({},'', '/');document.querySelector('.segment-assistant').remove()">新建会话</a><div class="segment segment-assistant">旧回答</div>''')
            page.route('https://www.kimi.com/**',route);page.goto('https://www.kimi.com/chat/previous')
            page.evaluate('window.runtimePreserved=true')
            result=start_new_chat(page,'kimi')
            self.assertTrue(result.startswith('clicked:'),result)
            self.assertEqual(page.url,'https://www.kimi.com/')
            self.assertEqual(home_calls,[])
            self.assertTrue(page.evaluate('window.runtimePreserved'))
            self.assertEqual(page.locator('.segment-assistant').count(),0)
            browser.close()

    def test_kimi_ineffective_new_chat_does_not_reuse_old_answer(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page();home_calls=[]
            def route(r):
                if r.request.url=='https://www.kimi.com/':
                    home_calls.append(1);r.fulfill(content_type='text/html; charset=utf-8',body='<div class="chat-input-editor" contenteditable="true"></div>')
                else:r.fulfill(content_type='text/html; charset=utf-8',body='''<a aria-label="新建会话" href="/" onclick="event.preventDefault();history.pushState({},'', '/')">新建会话</a><div class="segment segment-assistant">旧回答</div>''')
            page.route('https://www.kimi.com/**',route);page.goto('https://www.kimi.com/chat/previous')
            start_new_chat(page,'kimi')
            self.assertEqual(home_calls,[1])
            self.assertEqual(page.locator('.segment-assistant').count(),0)
            browser.close()

    def test_wenxin_and_yuanbao_submit_once_without_navigation_click(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            for site in ('yiyan','yuanbao'):
                page.set_content('''<div contenteditable="true" onkeydown="if(event.key==='Enter'){event.preventDefault();window.sent=(window.sent||0)+1}"></div>
                  <img id="ci-submit-button-ai" class="ci-submit-button-ai-active" onclick="window.wrongSend=1">
                  <div id="yuanbao-send-btn" onclick="window.wrongSend=1">发送</div>''')
                page.evaluate('window.sent=0;window.wrongSend=0')
                prep,method=send_prompt(page,'简要说明文件校验并给参考来源',site,new_chat=False)
                self.assertEqual(method,'enter')
                self.assertEqual(page.evaluate('window.sent'),1)
                self.assertEqual(page.evaluate('window.wrongSend'),0)
            browser.close()

    def test_doubao_desktop_promotion_is_closed_before_typing(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('''<div>用户abcd1234</div><div contenteditable="true" style="width:300px;height:50px"></div>
              <button data-testid="send-button" onclick="window.sent=(window.sent||0)+1">发送</button>
              <div role="dialog" style="position:fixed;inset:0;background:white;z-index:100">
                <button data-testid="desktop_download_guide_popup_btn" onclick="window.download=1">下载电脑版</button>
                <button aria-label="关闭" onclick="this.parentNode.remove()">关闭</button></div>''')
            prep,method=send_prompt(page,'请说明文件校验并给参考来源','doubao',new_chat=False)
            self.assertEqual(page.evaluate('window.sent'),1)
            self.assertFalse(page.evaluate('!!window.download'))
            self.assertEqual(page.locator('[role=dialog]').count(),0)
            browser.close()

    def test_doubao_security_and_login_dialogs_are_not_dismissed(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            for text in ('请完成安全验证','手机验证码登录'):
                page.set_content('<div role="dialog">'+text+'<button aria-label="关闭" onclick="window.closed=1;this.parentNode.remove()">关闭</button></div>')
                self.assertFalse(dismiss_doubao_desktop_notice(page))
                self.assertEqual(page.locator('[role=dialog]').count(),1)
            browser.close()

    def test_wenxin_search_page_navigates_to_home_for_next_question(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.route('https://wenxin.baidu.com/**',lambda r:r.fulfill(content_type='text/html; charset=utf-8',body='<div>任务页面</div>'))
            page.goto('https://wenxin.baidu.com/search/old-task')
            self.assertEqual(start_new_chat(page,'yiyan'),'goto:wenxin')
            self.assertEqual(page.url,'https://wenxin.baidu.com/')
            self.assertEqual(start_new_chat(page,'yiyan'),'already:home')
            browser.close()

    def test_wenxin_failed_home_navigation_cannot_send_in_old_task(self):
        page=MagicMock()
        with patch('cn_ai.site_flows.start_new_chat',return_value='fail:TimeoutError'):
            with self.assertRaises(PlatformFlowError) as raised:
                send_prompt(page,'当前问题','yiyan')
        self.assertEqual(raised.exception.error_code,'platform_busy')
        page.keyboard.type.assert_not_called()

    def test_accepted_question_still_generating_is_not_retried(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.route('https://www.kimi.com/**',lambda r:r.fulfill(body='<div class="send-button-container stop">停止</div>'))
            page.goto('https://www.kimi.com/chat/accepted')
            with self.assertRaises(PlatformFlowError) as raised:
                wait_for_answer_growth(page,timeout_sec=.15,site_id='kimi')
            self.assertEqual(raised.exception.error_code,'generation_timeout')
            self.assertFalse(raised.exception.retryable)
            browser.close()

    def test_healthy_site_tab_is_reused_with_another_tab_open(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            context=browser.new_context()
            context.route('https://www.kimi.com/**',lambda route:route.fulfill(content_type='text/html; charset=utf-8',body='<div>已登录会话</div>'))
            page=context.new_page();page.goto('https://www.kimi.com/chat/saved')
            page.evaluate('window.existingConversation = "preserved"')
            other=context.new_page();other.set_content('<div>另一个页面</div>')
            with patch('cn_ai.scraper.close_site_targets') as close:
                self.assertIs(find_or_open_page(browser,'kimi'),page)
            close.assert_not_called()
            self.assertEqual(page.evaluate('window.existingConversation'),'preserved')
            self.assertEqual(len(context.pages),2)
            browser.close()

    def test_frozen_doubao_tab_is_awakened_without_reloading_login(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);context=browser.new_context()
            context.route('https://www.doubao.com/**',lambda route:route.fulfill(content_type='text/html; charset=utf-8',body='<div>用户abcd1234</div>'))
            page=context.new_page();page.goto('https://www.doubao.com/chat/saved')
            page.evaluate('window.existingConversation = "preserved"')
            session=context.new_cdp_session(page)
            session.send('Page.setWebLifecycleState',{'state':'frozen'});session.detach()
            with patch.dict(os.environ,{'GEO_BROWSER_AUTO_LOGIN':'0'}):
                self.assertIs(cloud.ensure_doubao_login(browser),page)
            self.assertEqual(page.url,'https://www.doubao.com/chat/saved')
            self.assertEqual(page.evaluate('window.existingConversation'),'preserved')
            browser.close()

    def test_page_health_timeout_releases_its_cdp_session(self):
        page=MagicMock();session=page.context.new_cdp_session.return_value
        page.wait_for_function.side_effect=TimeoutError('renderer unavailable')
        self.assertFalse(page_answers(page))
        page.wait_for_function.assert_called_once_with('() => true',timeout=3000,polling=100)
        session.detach.assert_called_once()

    def test_wenxin_nested_task_sources_are_bound_to_current_question(self):
        question='文件哈希如何校验？'
        payload={'data':{'query':[{'data':{'text':{'query':question}}}], 'chatResponse':[{'data':{'message':{'content':{'generator':[{'data':{'content':[{'content':[{'referenceList':[{'url':'https://example.org/hash'}]}]}]}}]}}}}], 'navigation':[{'url':'https://unrelated.example/navigation'}]}}
        body=json.dumps(payload,ensure_ascii=False)
        answer,citations,source=merge_captures([{'url':'https://chat.baidu.com/aichat/api/canvas/search','body':body,'mime':'application/json'}],'yiyan',question)
        self.assertEqual(answer,'')
        self.assertEqual(citations,['https://example.org/hash'])
        self.assertEqual(parse_yiyan_task_sources(body,'不同的问题'),('',[]))
        payload['data']['query'][0]['data']['text']['query']='文件 A B'
        self.assertEqual(parse_yiyan_task_sources(json.dumps(payload),'文件 AB'),('',[]))

    def test_wenxin_task_sse_requires_matching_question_before_sources(self):
        reference={'data':{'referenceList':[{'url':'https://example.org/hash'}]}}
        frame=lambda obj:'data:'+json.dumps(obj,ensure_ascii=False)+'\n'
        self.assertEqual(parse_yiyan_task_sources(frame(reference),'当前问题'),('',[]))
        stream=frame({'query':'当前问题'})+frame(reference)+frame({'query':'其他问题'})+frame({'data':{'referenceList':[{'url':'https://other.example/source'}]}})
        self.assertEqual(parse_yiyan_task_sources(stream,'当前问题'),('', ['https://example.org/hash']))
    def test_large_history_json_remains_parseable_with_its_sources(self):
        capture=NetworkCapture();response=MagicMock()
        response.url='https://chat.baidu.com/csaitab/history/list';response.status=200
        response.headers={'content-type':'application/json'}
        payload={'answer':'A'*600000,'sources':[{'url':'https://example.org/source'}]}
        response.body.return_value=json.dumps(payload).encode()
        capture._ingest(response)
        self.assertEqual(json.loads(capture.items[0]['body'])['sources'],payload['sources'])
    def test_wenxin_shares_final_answer_instead_of_task_acknowledgement(self):
        public='https://mr.baidu.com/r/abcdefgh123?f=ot&u=abcdefgh123'
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('''<div><button data-testid="menu-btn-share" onclick="window.selected='ack'">分享确认消息</button></div>
              <div class="cs-history-answer"><button data-testid="menu-btn-share" onclick="window.selected='final';document.querySelector('#copy').style.display='block'">分享最终回答</button></div>
              <div class="cs-history-answer" style="display:none"><button data-testid="menu-btn-share" onclick="window.selected='hidden'">隐藏分享</button></div>
              <button id="copy" style="display:none" onclick="document.querySelector('#link').textContent='''+json.dumps(public).replace('"','&quot;')+'''">复制链接</button><div id="link"></div>''')
            result=create_public_share(page,'yiyan')
            self.assertEqual(result['url'],public,result)
            self.assertEqual(page.evaluate('selected'),'final')
            browser.close()

    def test_wenxin_copy_requires_real_pointer_click_and_waits_for_result(self):
        public='https://mr.baidu.com/r/abcdefgh123?f=ot&u=abcdefgh123'
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('''<div class="cs-history-answer"><span data-testid="menu-btn-share" onclick="document.querySelector('#copy').style.display='block'">分享</span></div>
              <button id="copy" style="display:none" onclick="if(event.isTrusted){window.copied=true;setTimeout(()=>{window.clipboardValue='''+json.dumps(public).replace('"','&quot;')+'''},350)}">复制链接</button>''')
            with patch('cn_ai.share_links._read_clipboard',side_effect=lambda p:p.evaluate('window.clipboardValue || ""')):
                result=create_public_share(page,'yiyan')
            self.assertEqual(result['url'],public,result)
            self.assertTrue(page.evaluate('window.copied'))
            browser.close()
    def test_completed_response_body_is_read_once_outside_event_callback(self):
        capture=NetworkCapture();page=MagicMock();capture.attach(page)
        callbacks={call.args[0]:call.args[1] for call in page.on.call_args_list}
        response=MagicMock();response.url='https://www.kimi.com/api/chat'
        response.headers={'content-type':'application/json'};response.status=200
        response.body.return_value=b'{"answer":"finished answer"}'
        request=MagicMock();request.resource_type='fetch';request.url=response.url
        request.response.return_value=response
        callbacks['requestfinished'](request)
        response.body.assert_not_called()
        capture.flush();capture.flush()
        response.body.assert_called_once()
        self.assertIn('finished answer',capture.items[0]['body'])
    def test_kimi_send_is_acknowledged_before_first_answer_text(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.set_content('''<div class="chat-input-editor" contenteditable="true"></div><div class="send-button-container" onclick="this.classList.add('stop');window.sent=1">发送</div>''')
            started=time.monotonic()
            prep,method=send_prompt(page,'测试问题，请给参考来源','kimi',new_chat=False)
            self.assertEqual(page.evaluate('sent'),1)
            self.assertLess(time.monotonic()-started,5)
            self.assertEqual(method,'send-btn')
            browser.close()
    def test_blank_start_initializes_before_visiting_platform(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'AGENTBAY_API_KEY':'fixture','GEO_AGENTBAY_START_BLANK':'1','GEO_AGENTBAY_WARM_SEC':'0'}):
            lease=cloud.AgentBayDoubaoLease(state_path=Path(folder)/'state.json',home_url='https://www.doubao.com/chat/')
            client=MagicMock();session=MagicMock();session.session_id='test'
            session.keep_alive.return_value=SimpleNamespace(success=True)
            session.browser.get_endpoint_url.return_value='ws://test.invalid'
            client.context.get.return_value=SimpleNamespace(context=SimpleNamespace(id='saved-login'))
            client.create.return_value=SimpleNamespace(session=session)
            client.delete.return_value=SimpleNamespace(success=True)
            with patch('agentbay.AgentBay',return_value=client),patch.object(lease,'_acquire_slot'),patch.object(lease,'_acquire_startup_slot'),patch.object(lease,'_start_browser',return_value='') as init:
                with lease:self.assertEqual(lease.context_id,'saved-login')
            self.assertEqual(init.call_args.args[0].default_navigate_url,'about:blank')
    def test_kimi_waits_for_stop_control_to_clear_then_finishes_promptly(self):
        final='这是当前问题的最终回答，内容已经生成完毕，可以核对参考来源。'
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True);page=browser.new_page()
            page.route('https://www.kimi.com/**',lambda route:route.fulfill(content_type='text/html; charset=utf-8',body='''<div class="segment segment-assistant"><div class="markdown-container">这是当前问题尚未生成完毕的内容，即使文字暂停，也不应提前返回结果。</div><div class="segment-assistant-actions" style="display:none">分享</div></div><div class="send-button-container stop">停止</div><script>setTimeout(()=>{document.querySelector('.stop').remove();document.querySelector('.markdown-container').textContent='''+json.dumps(final,ensure_ascii=False)+''';document.querySelector('.segment-assistant-actions').style.display='block'},2000)</script>'''))
            page.goto('https://www.kimi.com/chat/fixture')
            started=time.monotonic()
            answer=wait_for_answer_growth(page,timeout_sec=8,site_id='kimi')
            elapsed=time.monotonic()-started
            self.assertEqual(answer,final)
            self.assertGreater(elapsed,2)
            self.assertLess(elapsed,7)
            browser.close()

    def test_equal_length_changes_do_not_count_as_stable_answer(self):
        clock=[0.0];reads=[];completed=[]
        class Page:
            def evaluate(self,*args):
                index=len(reads);text=('A' if index%2==0 else 'B')*60 if index<8 else 'C'*60
                reads.append(text)
                return {'text':text,'generating':False,'completed':True}
            def wait_for_timeout(self,ms):clock[0]+=ms/1000
        with patch('cn_ai.site_flows.time.time',side_effect=lambda:clock[0]),patch('cn_ai.site_flows.time.monotonic',side_effect=lambda:clock[0]),patch('cn_ai.site_flows.detect_logged_out'),patch('cn_ai.site_flows.detect_platform_failure'):
            answer=wait_for_answer_growth(Page(),timeout_sec=20,site_id='yuanbao',on_completed=completed.append)
        self.assertEqual(answer,'C'*60)
        self.assertGreaterEqual(len(reads),11)
        self.assertEqual(completed,['dom_state'])

    def test_timeout_text_does_not_claim_answer_completion(self):
        completed=[]
        class Page:
            def evaluate(self,*args):return 'unconfirmed page text'
        with self.assertRaises(PlatformFlowError) as raised:
            wait_for_answer_growth(Page(),timeout_sec=0,site_id='doubao',on_completed=completed.append)
        self.assertEqual(raised.exception.error_code,'platform_busy')
        self.assertEqual(completed,[])

    def test_share_failure_keeps_anonymous_page_separately_and_masks_input(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'GEO_DIAGNOSTIC_DIR':folder,'GEO_DIAGNOSTIC_SUCCESS_PERCENT':'100'}),sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            context=browser.new_context();page=context.new_page()
            page.set_content('<div>原始已登录回答页面</div>')
            with DiagnosticRun('kimi','anonymous-evidence','问题') as trace:
                trace.bind_page(page);trace.checkpoint(page,force=True)
                anonymous=browser.new_context();public=anonymous.new_page()
                public.set_content('<h1>分享尚未加载</h1><input value="private-value">')
                trace.public_share_checkpoint(public,False)
                manifest=json.loads((trace.path/'manifest.json').read_text())
                self.assertFalse(manifest['public_share_probe']['answer_matched'])
                html=(trace.path/'public-share.html').read_text()
                self.assertIn('分享尚未加载',html);self.assertNotIn('private-value',html)
                self.assertIn('原始已登录回答页面',(trace.path/'frame-0.html').read_text())
                self.assertEqual((trace.path/'public-share.html').stat().st_mode & 0o777,0o600)
                anonymous.close()
            browser.close()


if __name__=='__main__':unittest.main()
