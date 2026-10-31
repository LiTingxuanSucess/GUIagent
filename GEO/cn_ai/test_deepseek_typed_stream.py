import json
import unittest

from cn_ai.network_parser import parse_deepseek_sse


def stream(events):
    return '\n\n'.join('data: ' + json.dumps(event, ensure_ascii=False) for event in events) + '\n\ndata: [DONE]\n'


class DeepSeekTypedStreamTest(unittest.TestCase):
    def test_thinking_tool_and_response_deltas_share_paths_without_mixing_content(self):
        sources = [{'url': f'https://example.org/source/{i}'} for i in range(20)]
        events = [
            {'v': {'response': {'fragments': [{'id': 2, 'type': 'THINK', 'content': 'private'}]}}},
            {'p': 'response/fragments/-1/content', 'o': 'APPEND', 'v': '-planning'},
            {'v': '-delta'},
            {'p': 'response', 'o': 'BATCH', 'v': [
                {'p': 'fragments', 'o': 'APPEND', 'v': [{'id': 3, 'type': 'TOOL_SEARCH', 'results': []}]},
                {'p': 'has_pending_fragment', 'o': 'SET', 'v': False},
            ]},
            {'p': 'response/fragments/-1/results', 'o': 'SET', 'v': sources},
            {'p': 'response/fragments/-1', 'o': 'BATCH', 'v': [
                {'p': 'status', 'v': 'FINISHED'}, {'p': 'content', 'v': 'tool-progress'},
            ]},
            {'p': 'response', 'o': 'BATCH', 'v': [
                {'p': 'fragments', 'o': 'APPEND', 'v': [{'id': 4, 'type': 'THINK', 'content': 'private-second'}]},
            ]},
            {'p': 'response/fragments/-1/content', 'o': 'APPEND', 'v': '-delta'},
            {'p': 'response/fragments', 'o': 'APPEND', 'v': [{'id': 9, 'type': 'RESPONSE', 'content': '最终'}]},
            {'p': 'response/fragments/-1/content', 'v': '回答'},
            {'v': '包含'},
            {'p': 'response/fragments/-1', 'o': 'BATCH', 'v': [
                {'p': 'content', 'o': 'APPEND', 'v': '完整正文'},
                {'p': 'references', 'o': 'SET', 'v': [{'id': 1}]},
            ]},
            {'p': 'response/fragments/-1/content', 'o': 'APPEND', 'v': '。'},
            {'p': 'response/fragments/-2/content', 'o': 'APPEND', 'v': '-late-private-delta'},
        ]
        answer, citations = parse_deepseek_sse(stream(events))
        self.assertEqual(answer, '最终回答包含完整正文。')
        self.assertEqual(citations, [source['url'] for source in sources])

    def test_set_replaces_answer_and_response_prose_is_never_trimmed_by_keywords(self):
        final = '“我需要搜索”可以是正文讨论的原文，不应按措辞删除。'
        events = [
            {'v': {'response': {'fragments': [{'type': 'RESPONSE', 'content': '旧版本'}]}}},
            {'p': 'response/fragments/0/content', 'o': 'SET', 'v': final},
            {'p': 'response/fragments/0/content', 'o': 'APPEND', 'v': '完整保留。'},
        ]
        self.assertEqual(parse_deepseek_sse(stream(events)), (final + '完整保留。', []))

    def test_batch_operation_and_path_both_remain_sticky_when_omitted(self):
        events = [
            {'v': {'response': {'fragments': [
                {'type': 'THINK', 'content': 'private-planning'},
                {'type': 'TOOL_SEARCH', 'results': [{'url': 'https://example.org/source'}]},
                {'type': 'RESPONSE', 'content': '最终'},
            ]}}},
            {'p': 'response/fragments/-1', 'o': 'BATCH', 'v': [
                {'p': 'content', 'o': 'APPEND', 'v': '回答'},
                {'p': 'references', 'o': 'SET', 'v': [{'id': 1}]},
            ]},
            # The real stream omits both fields on consecutive batch events.
            # Treating this as APPEND would replace the typed fragment by a list.
            {'v': [
                {'p': 'content', 'o': 'APPEND', 'v': '完整'},
                {'p': 'references', 'o': 'SET', 'v': [{'id': 1}, {'id': 2}]},
            ]},
            {'p': 'response/fragments/-1/content', 'o': 'APPEND', 'v': '保留'},
            {'v': '。'},
        ]
        self.assertEqual(parse_deepseek_sse(stream(events)),
                         ('最终回答完整保留。', ['https://example.org/source']))

    def test_untyped_or_non_response_content_is_not_an_answer(self):
        for fragment in ({'content': 'unknown'}, {'type': 'THINK', 'content': 'private'},
                         {'type': 'TOOL_OPEN', 'content': 'tool-output'}):
            with self.subTest(type=fragment.get('type')):
                events = [{'v': {'response': {'fragments': [fragment]}}},
                          {'p': 'response/fragments/-1/content', 'o': 'APPEND', 'v': '-delta'}]
                self.assertEqual(parse_deepseek_sse(stream(events)), ('', []))


if __name__ == '__main__':
    unittest.main()
