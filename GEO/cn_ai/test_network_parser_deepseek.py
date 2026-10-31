import json
import unittest

from cn_ai.network_parser import parse_deepseek_history


def history(messages):
    return json.dumps({"code": 0, "data": {"chat_messages": messages}})


class DeepSeekHistoryTest(unittest.TestCase):
    def test_does_not_return_an_old_answer_for_a_new_prompt(self):
        body = history(
            [
                {"role": "USER", "fragments": [{"type": "REQUEST", "content": "旧问题"}]},
                {"role": "ASSISTANT", "fragments": [{"type": "RESPONSE", "content": "旧答案"}]},
            ]
        )
        self.assertEqual(parse_deepseek_history(body, prompt="新问题"), ("", []))

    def test_returns_the_answer_matching_the_requested_prompt(self):
        body = history(
            [
                {"role": "USER", "fragments": [{"type": "REQUEST", "content": "目标问题"}]},
                {"role": "ASSISTANT", "fragments": [{"type": "RESPONSE", "content": "目标答案"}]},
            ]
        )
        self.assertEqual(parse_deepseek_history(body, prompt="目标问题"), ("目标答案", []))


if __name__ == "__main__":
    unittest.main()
