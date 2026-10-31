import unittest

from cn_ai.public_share_recovery import parse_transcript
from cn_ai.site_flows import PlatformFlowError


class PublicShareRecoveryTest(unittest.TestCase):
    def payload(self, prompt='原问题'):
        return {'code': 0, 'data': {'session': {'record_list': [{
            'request_messages': [{'mime_type': 'text/plain', 'content': prompt}],
            'response_messages': [
                {'mime_type': 'multi_load/iframe', 'status': 'complete', 'content': '公开页中的完整回答'},
                {'mime_type': 'bar/workflow', 'meta_data': {'multi_load': [{
                    'type': 'bar_ref_source_inline', 'content': {'docs': [
                        {'url': 'https://example.org/source', 'title': '实际搜索来源'},
                        {'type': 'video', 'url': 'https://example.org/not-web-source'}]}}]}}]}]}}}

    def test_native_public_sources_without_type_are_preserved(self):
        answer, sources = parse_transcript(self.payload(), '原问题')
        self.assertEqual(answer, '公开页中的完整回答')
        self.assertEqual(sources, ['https://example.org/source'])

    def test_wrong_question_or_multiple_turns_are_rejected(self):
        for payload in [self.payload('其他问题'), self.payload()]:
            if payload['data']['session']['record_list'][0]['request_messages'][0]['content'] == '原问题':
                payload['data']['session']['record_list'] *= 2
            with self.assertRaises(PlatformFlowError) as caught:
                parse_transcript(payload, '原问题')
            self.assertEqual(caught.exception.error_code, 'manual_question_mismatch')

    def test_answer_links_do_not_substitute_for_actual_search_sources(self):
        payload = self.payload()
        messages = payload['data']['session']['record_list'][0]['response_messages']
        messages.pop()
        messages[0]['content'] = '普通正文链接 https://example.org/unproven'
        with self.assertRaises(PlatformFlowError):
            parse_transcript(payload, '原问题')
