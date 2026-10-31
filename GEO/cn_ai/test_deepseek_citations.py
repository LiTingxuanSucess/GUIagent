import json
import unittest

from cn_ai.network_parser import parse_deepseek_history, parse_deepseek_sse


class DeepSeekCitationTests(unittest.TestCase):
    def test_current_turn_keeps_all_tool_search_sources(self):
        question = "全球纯电家用车推荐"
        sources = [{"url": f"https://example.com/source/{i}"} for i in range(130)]
        messages = [
            {"role": "USER", "fragments": [{"type": "REQUEST", "content": "旧问题"}]},
            {"role": "ASSISTANT", "fragments": [
                {"type": "RESPONSE", "content": "旧回答"},
                {"type": "TOOL_SEARCH", "results": [{"url": "https://example.com/old"}]},
            ]},
            {"role": "USER", "fragments": [{"type": "REQUEST", "content": question}]},
            {"role": "ASSISTANT", "fragments": [
                {"type": "THINK", "content": "内部推理不属于最终回答"},
                {"type": "TOOL_SEARCH", "results": sources[:59]},
                {"type": "TOOL_SEARCH", "results": sources[59:] + sources[:1]},
                {"type": "RESPONSE", "content": "当前完整回答"},
            ]},
        ]
        body = json.dumps({"code": 0, "data": {"biz_data": {"chat_messages": messages}}})
        answer, citations = parse_deepseek_history(body, prompt=question)
        self.assertEqual(answer, "当前完整回答")
        self.assertEqual(citations, [source["url"] for source in sources])
        self.assertEqual(parse_deepseek_history(body, prompt="没有发问过的题目"), ("", []))

    def test_sse_supports_old_and_new_search_fragments(self):
        fragments = [
            {"type": "SEARCH", "results": [{"url": "https://example.com/old-format"}]},
            {"type": "TOOL_SEARCH", "results": [{"url": "https://example.com/new-format"}]},
            {"type": "THINK", "content": "内部推理"},
            {"type": "RESPONSE", "content": "最终回答"},
        ]
        events = [
            {"v": {"response": {"fragments": fragments}}},
            {"o": "BATCH", "v": [{"o": "APPEND", "p": "fragments", "v": fragments}]},
        ]
        for event in events:
            with self.subTest(event=event):
                body = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n"
                answer, citations = parse_deepseek_sse(body)
                self.assertEqual(answer, "最终回答")
                self.assertEqual(citations, ["https://example.com/old-format", "https://example.com/new-format"])


if __name__ == "__main__":
    unittest.main()
