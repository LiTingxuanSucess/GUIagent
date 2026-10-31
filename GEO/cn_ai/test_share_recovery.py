import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cn_ai.network_parser import parse_qianwen_chat_sse
from cn_ai import share_links


class ShareRecoveryTests(unittest.TestCase):
    def test_mac_clipboard_fallback_keeps_only_real_public_share_urls(self):
        page = Mock()
        page.evaluate.side_effect = RuntimeError('clipboard read denied')
        with patch.object(share_links, '_grant_clipboard_permissions'), \
             patch.object(share_links.sys, 'platform', 'darwin'), \
             patch.object(share_links.subprocess, 'run') as read:
            read.return_value.stdout = '已复制分享链接 https://www.kimi.com/share/12345678'
            self.assertEqual(share_links._read_clipboard(page), 'https://www.kimi.com/share/12345678')
            read.return_value.stdout = '私人剪贴板文本和 https://example.org/private'
            self.assertEqual(share_links._read_clipboard(page), '')

    def test_yiyan_verifies_the_published_final_card_in_a_multistep_answer(self):
        final = '优先比较品牌存续、售后覆盖和电池质保，再结合预算选择具体车型。' * 8
        fallback = '我先联网检索一下相关资料。\n\n' + final
        self.assertEqual(share_links.share_answer_hint('yiyan', {'answer_hint': final}, fallback), final)
        self.assertEqual(share_links.share_answer_hint('yiyan', {'answer_hint': '稍后我会继续为你整理。'}, fallback), fallback)
        self.assertEqual(share_links.share_answer_hint('yiyan', {'answer_hint': '这是另一条问题的回答。' * 20}, fallback), fallback)

    def test_qianwen_workflow_search_docs_are_real_sources_not_answer_urls(self):
        event = {'data': {'messages': [
            {'mime_type': 'bar/workflow', 'meta_data': {'multi_load': [
                {'type': 'bar_ref_source_inline', 'content': {'query_list': ['汽车品牌'], 'docs': [
                    {'type': 'web_source', 'raw_url': 'https://example.org/source', 'title': '搜索来源'},
                    {'type': 'web_source', 'url': 'https://example.org/source'},
                    {'type': 'video', 'url': 'https://example.org/unrelated-video'},
                ]}},
                {'type': 'unrelated_card', 'content': {'docs': [{'type': 'web_source', 'url': 'https://example.org/unrelated'}]}},
            ]}},
            {'mime_type': 'text/markdown', 'content': '完整回答，文字中的 https://example.org/manual-link 不是检索来源。'},
        ]}}
        answer, sources = parse_qianwen_chat_sse('data: ' + json.dumps(event, ensure_ascii=False))
        self.assertIn('完整回答', answer)
        self.assertEqual(sources, ['https://example.org/source'])

    def test_kimi_share_creation_cleans_picker_on_success_and_failure(self):
        page = Mock()
        url = 'https://www.kimi.com/share/12345678'
        with patch.object(share_links, '_observe_share_from_ui', return_value={'url': url}), \
             patch.object(share_links, 'dismiss_share_ui') as dismiss:
            self.assertEqual(share_links.create_public_share(page, 'kimi')['url'], url)
            dismiss.assert_called_once_with(page, 'kimi')
        with patch.object(share_links, '_observe_share_from_ui', side_effect=RuntimeError('failed')), \
             patch.object(share_links, 'dismiss_share_ui') as dismiss:
            self.assertEqual(share_links.create_public_share(page, 'kimi')['url'], '')
            dismiss.assert_called_once_with(page, 'kimi')

    def test_qianwen_mixed_snapshot_and_incremental_content(self):
        def frame(message):
            return 'data: '+json.dumps({'data': {'messages': [dict(mime_type='multi_load/iframe', **message)]}})
        body = '\n'.join([
            frame({'content': '坝', 'status': 'processing'}),
            frame({'patch': [{'op': 'add', 'path': '/content', 'value': '上草原'}]}),
            frame({'patch': [{'op': 'replace', 'path': '/content', 'value': '坝上草原地域辽阔'}]}),
            frame({'content': '坝上草原地域辽阔，适合自驾。'}),
            frame({'patch': [{'op': 'add', 'path': '/content', 'value': '注意保暖。'}]}),
            frame({'patch': [{'op': 'replace', 'path': '/status', 'value': 'complete'}]}),
        ])
        self.assertEqual(parse_qianwen_chat_sse(body)[0], '坝上草原地域辽阔，适合自驾。注意保暖。')

    def test_doubao_waits_for_create_response_after_clipboard(self):
        url = 'https://www.doubao.com/thread/xnYcpYEY8JFsA9L1q'
        listeners = {}
        request = SimpleNamespace(method='POST', url='https://www.doubao.com/im/message/share/save', post_data='{}')
        response = SimpleNamespace(request=request, url=request.url, text=lambda: json.dumps({'data': {'share_url': url}}))
        request.response = lambda: response
        page = Mock()
        page.context.pages = [page]
        page.on.side_effect = lambda event, callback: listeners.update({event: callback})
        def deliver(_):
            listeners['requestfinished'](request)
        page.wait_for_timeout.side_effect = deliver
        with patch.object(share_links, '_grant_clipboard_permissions'), \
             patch.object(share_links, '_read_clipboard', return_value=''), \
             patch.object(share_links, '_click_share_and_collect', return_value={'clicked': '复制链接'}):
            result = share_links._observe_share_from_ui(page, 'doubao')
        self.assertEqual(result['url'], url)
        page.remove_listener.assert_any_call('requestfinished', listeners['requestfinished'])

    def test_qianwen_content_replacement_can_shorten_the_answer(self):
        messages = [
            {'mime_type': 'multi_load/iframe', 'content': '这是一段稍后会被修正的较长回答。'},
            {'mime_type': 'multi_load/iframe', 'patch': [{'op': 'replace', 'path': '/content', 'value': '修正后的回答。'}]},
        ]
        body = '\n'.join('data: '+json.dumps({'data': {'messages': [m]}}) for m in messages)
        self.assertEqual(parse_qianwen_chat_sse(body)[0], '修正后的回答。')

    def test_observer_rejects_unchanged_clipboard_and_old_tab(self):
        stale = 'https://www.doubao.com/thread/oldshare123456789'
        page = Mock()
        page.context.pages = [page, Mock(url=stale)]
        with patch.object(share_links, '_grant_clipboard_permissions'), \
             patch.object(share_links, '_read_clipboard', return_value=stale), \
             patch.object(share_links.task_budget, 'remaining', return_value=0), \
             patch.object(share_links, '_click_share_and_collect', return_value={}):
            self.assertEqual(share_links._observe_share_from_ui(page, 'doubao')['url'], '')

    def test_qianwen_public_proof_reads_answer_only_without_credentials(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({'code': 0, 'data': {'session': {'record_list': [{
            'request_messages': [{'content': '不能用问题证明回答'}],
            'response_messages': [
                {'mime_type': 'bar/progress', 'content': '不能用思考过程'},
                {'mime_type': 'multi_load/iframe', 'content': '实际分享回答'},
            ]}]}}}).encode('utf-8')
        opener = Mock()
        opener.open.return_value = response
        with patch.object(share_links, 'build_opener', return_value=opener):
            actual = share_links._qianwen_public_answer('https://qianwen.my.cn/share/chat/abc123xyz?lang=zh-CN', 2)
        self.assertEqual(actual, '实际分享回答')
        req = opener.open.call_args.args[0]
        self.assertNotIn('Cookie', req.headers)
        self.assertNotIn('Authorization', req.headers)


if __name__ == '__main__':
    unittest.main()
