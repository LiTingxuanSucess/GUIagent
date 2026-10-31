import unittest

from cn_ai.site_flows import classify_platform_failure


NOTICE = "此模型的本月用量已用完，次月1日零点刷新，可切换至专家模式，使用Hy3模型继续对话。"


class YuanbaoQuotaTest(unittest.TestCase):
    def test_entire_reply_is_quota_failure(self):
        for answer in (NOTICE, " \n" + NOTICE + "\n", NOTICE.replace("，", "，\n"), NOTICE.rstrip("。")):
            with self.subTest(answer=answer):
                code, _, retry = classify_platform_failure("yuanbao", answer)
                self.assertEqual(code, "platform_quota")
                self.assertFalse(retry)

    def test_mentions_and_other_platforms_stay_answers(self):
        for answer in ("这段提示的含义是：" + NOTICE, NOTICE + "你可以等待刷新或切换模型。", "普通回答", ""):
            self.assertIsNone(classify_platform_failure("yuanbao", answer))
        self.assertIsNone(classify_platform_failure("deepseek", NOTICE))
