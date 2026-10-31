import unittest
from unittest.mock import patch

from playwright.sync_api import TimeoutError as PwTimeout

from cn_ai.scraper import _looks_like_nav_junk, extract_answer_urls, find_or_open_page
from cn_ai.site_flows import (
    PlatformFlowError,
    _kimi_answer_in_progress,
    classify_platform_failure,
    dismiss_stale_kimi_busy_dialog,
    kimi_busy_refresh_limit,
    dismiss_qianwen_share_dialog,
    kimi_reply_advanced,
    outbound_prompt,
    send_prompt,
    start_new_chat,
    wait_doubao_auth,
    wait_for_answer_growth,
)


class DoubaoAnswerShapeTest(unittest.TestCase):
    def test_markdown_list_is_an_answer(self):
        lines = ["## 全球纯电家用车推荐"]
        for i in range(1, 20):
            lines.append(f"### {i}. 车型")
            lines.append(f"- 定位：家用纯电 {i}")
            lines.append("- 亮点：空间大，续航够用")
        text = "\n".join(lines)
        self.assertFalse(_looks_like_nav_junk(text, "全球纯电家用车推荐"))

    def test_sidebar_titles_stay_junk(self):
        text = "\n".join(f"历史问题{i}" for i in range(40))
        self.assertTrue(_looks_like_nav_junk(text))

    def test_answer_urls_keep_full_links_and_drop_jump_shells(self):
        answer = (
            "参考：https://www.ev\u2011sales.com/ 和 https://www.dongchedi.com/\n"
            "https://link.wtturl.cn/?target=https%3A%2F%2Fwww.ev"
        )
        self.assertEqual(
            extract_answer_urls(answer),
            ["https://www.ev-sales.com/", "https://www.dongchedi.com/"],
        )


class OutboundPromptTest(unittest.TestCase):
    def test_all_platforms_send_the_question_without_an_added_instruction(self):
        prompt = "特斯拉Model 3真实续航"
        for site in ("deepseek", "doubao", "kimi", "yuanbao", "yiyan", "qianwen"):
            self.assertEqual(outbound_prompt(site, prompt), prompt)

    def test_original_whitespace_and_user_written_search_instruction_are_preserved(self):
        prompt = "  第一行\n第二行；请联网搜索后回答，并列出参考来源链接。  "
        for site in ("deepseek", "doubao", "kimi", "yuanbao", "yiyan", "qianwen"):
            self.assertEqual(outbound_prompt(site, prompt), prompt)

    def test_existing_source_request_is_left_unchanged(self):
        prompt = "用三句话说明特斯拉 Model 3 的真实续航大概怎么看，并给出参考来源。"
        self.assertEqual(outbound_prompt("doubao", prompt), prompt)
        self.assertEqual(outbound_prompt("yuanbao", prompt), prompt)
        self.assertEqual(outbound_prompt("yiyan", prompt), prompt)


class FailurePage:
    url = "https://www.qianwen.com/"

    def evaluate(self, script, *args):
        return {
            "text": "",
            "urls": [
                "https://chat2.qianwen.com/api/v2/chat/_____tmd_____/punish?action=captcha"
            ],
        }


class MutedDeepSeekPage:
    url = "https://chat.deepseek.com/"

    def evaluate(self, script, *args):
        return {
            "deepseek_muted": "由于违反用户使用规范，你的账号已被禁言至 2026 年 10 月 19 日 03:03，如有疑问请 联系我们。",
            "text": "",
            "urls": [],
        }


class PlatformFailureTest(unittest.TestCase):
    def test_muted_deepseek_account_is_a_quota_failure_with_its_end_time(self):
        from cn_ai.site_flows import deepseek_mute_message, detect_platform_failure
        self.assertEqual(
            deepseek_mute_message("你的账号已被禁言至 2026 年 10 月 19 日 3:03"),
            "DeepSeek 账号已被禁言至 2026-10-19 03:03，暂停使用该账号",
        )
        self.assertEqual(deepseek_mute_message("聊聊禁言这个话题"), "")
        with self.assertRaises(PlatformFlowError) as raised:
            detect_platform_failure(MutedDeepSeekPage(), "deepseek")
        self.assertEqual(raised.exception.error_code, "platform_quota")
        self.assertFalse(raised.exception.retryable)
        self.assertIn("2026-10-19 03:03", str(raised.exception))

    def test_qianwen_share_is_closed_before_starting_next_chat(self):
        from unittest.mock import MagicMock
        page = MagicMock()
        mask = page.locator.return_value.first
        mask.count.return_value = 1
        mask.is_visible.return_value = True
        self.assertTrue(dismiss_qianwen_share_dialog(page))
        page.locator.assert_called_with("#share-modal-root [class*='mask-']")
        mask.wait_for.assert_called_once_with(state="hidden", timeout=1000)
        page.goto.assert_not_called()
        order = []
        with (
            patch("cn_ai.site_flows.dismiss_qianwen_share_dialog", side_effect=lambda p: order.append("close") or True),
            patch("cn_ai.site_flows.start_new_chat", side_effect=lambda p, s: order.append("new") or "new"),
            patch.dict("cn_ai.site_flows.SITE_PREP", {"qianwen": lambda p: "skip"}),
            patch("cn_ai.site_flows.detect_logged_out", side_effect=RuntimeError("stop after prep")),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after prep"):
                send_prompt(page, "第二个问题", "qianwen")
        self.assertEqual(order, ["close", "new"])

    def test_qianwen_unclosable_share_reloads_without_dropping_login_context(self):
        from unittest.mock import MagicMock
        page = MagicMock()
        mask = page.locator.return_value.first
        mask.count.return_value = 1
        mask.is_visible.return_value = True
        mask.wait_for.side_effect = PwTimeout("share still visible")
        self.assertTrue(dismiss_qianwen_share_dialog(page))
        page.goto.assert_called_once_with("https://www.qianwen.com/", wait_until="domcontentloaded", timeout=12000)

    def test_kimi_generation_status_is_not_a_completed_answer(self):
        self.assertTrue(_kimi_answer_in_progress("已搜索网页\\n正在思考中"))
        self.assertFalse(_kimi_answer_in_progress("思考已完成\\n天空呈蓝色。"))
        self.assertTrue(
            _kimi_answer_in_progress(
                "User asks in Chinese. Need to search. Use web_search and list links."
            )
        )
        self.assertFalse(
            _kimi_answer_in_progress("电动牙刷比手动牙刷更能减少牙菌斑，来源是 Cochrane 综述。")
        )

    def test_kimi_quota_is_not_retried(self):
        self.assertEqual(
            classify_platform_failure("kimi", "提示\n额度已用尽\n免费额度已用完，将于 11-01 重置"),
            ("platform_quota", "Kimi 免费额度已用完，平台提示 11-01 重置", False),
        )

    def test_kimi_quota_without_reset_stays_unknown(self):
        self.assertEqual(
            classify_platform_failure("kimi", "免费额度已用完"),
            ("platform_quota", "Kimi 免费额度已用完，请等待额度重置后再试", False),
        )

    def test_yiyan_submit_failure_has_a_reason(self):
        self.assertEqual(
            classify_platform_failure("yiyan", "提交失败: None"),
            ("platform_busy", "文心提交失败，请稍后重试", True),
        )
        self.assertIsNone(classify_platform_failure("doubao", "提交失败: None"))

    def test_kimi_capacity_message(self):
        self.assertEqual(
            classify_platform_failure(
                "kimi", "和Kimi聊天的人太多了，订阅会员可进入优先队列"
            ),
            ("platform_busy", "Kimi 当前请求过多，请稍后重试", True),
        )

    def test_kimi_busy_refresh_limit_defaults_to_ten(self):
        import os
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEO_KIMI_BUSY_REFRESH_LIMIT", None)
            self.assertEqual(kimi_busy_refresh_limit(), 10)
        with patch.dict(os.environ, {"GEO_KIMI_BUSY_REFRESH_LIMIT": "3"}):
            self.assertEqual(kimi_busy_refresh_limit(), 3)
        with patch.dict(os.environ, {"GEO_KIMI_BUSY_REFRESH_LIMIT": "99"}):
            self.assertEqual(kimi_busy_refresh_limit(), 10)

    def test_qianwen_punish_url(self):
        self.assertEqual(
            classify_platform_failure(
                "qianwen",
                "",
                ["https://chat2.qianwen.com/api/v2/chat/_____tmd_____/punish?action=captcha"],
            ),
            ("platform_risk_control", "千问触发安全验证，请完成验证后重试", False),
        )

    def test_doubao_semantic_reasoning_challenge(self):
        self.assertEqual(
            classify_platform_failure(
                "doubao",
                "",
                [
                    "https://rmc.bytedance.com/verifycenter/captcha/v2?"
                    "verify_scene=doubao_message_web&subtype=semantic_reasoning"
                ],
            ),
            (
                "platform_risk_control",
                "豆包触发语义安全验证，请人工完成验证后重试",
                False,
            ),
        )

    def test_unrelated_page_is_not_failure(self):
        self.assertIsNone(
            classify_platform_failure(
                "qianwen", "正在生成回答", ["https://chat2.qianwen.com/api/v2/chat"]
            )
        )

    def test_wait_propagates_platform_failure(self):
        with self.assertRaises(PlatformFlowError) as raised:
            wait_for_answer_growth(FailurePage(), timeout_sec=1, site_id="qianwen")
        self.assertEqual(raised.exception.error_code, "platform_risk_control")

    def test_no_kimi_dialog_is_not_dismissed(self):
        class EmptyLocator:
            def count(self):
                return 0

        class EmptyPage:
            def locator(self, _selector):
                return EmptyLocator()

        self.assertFalse(dismiss_stale_kimi_busy_dialog(EmptyPage()))

    def test_kimi_unusable_input_is_retryable_busy(self):
        class BlockedInput:
            def click(self, timeout=0):
                raise PwTimeout(f"blocked after {timeout}")

        class Page:
            pass

        with (
            patch("cn_ai.site_flows.find_input", return_value=BlockedInput()),
            patch("cn_ai.site_flows.detect_logged_out"),
            patch("cn_ai.site_flows._dismiss_overlays"),
            patch.dict("cn_ai.site_flows.SITE_PREP", {"kimi": lambda _page: "skip"}),
        ):
            with self.assertRaises(PlatformFlowError) as raised:
                send_prompt(Page(), "测试问题", "kimi", new_chat=False)
        self.assertEqual(raised.exception.error_code, "platform_busy")
        self.assertTrue(raised.exception.retryable)

    def test_doubao_new_chat_fails_fast_when_logged_out(self):
        class LoggedOutPage:
            url = "https://www.doubao.com/chat/"

            def evaluate(self, _script):
                return "logged_out"

        with patch("cn_ai.site_flows.DOUBAO_AUTH_SETTLE_SEC", 0):
            with self.assertRaises(PlatformFlowError) as raised:
                start_new_chat(LoggedOutPage(), "doubao")
        self.assertEqual(raised.exception.error_code, "platform_auth_unavailable")

    def test_doubao_login_shell_is_not_logout_once_the_account_chip_appears(self):
        class SettlingPage:
            def __init__(self):
                self.reads = 0

            def evaluate(self, _script):
                self.reads += 1
                return "logged_out" if self.reads == 1 else "logged_in"

            def wait_for_timeout(self, _ms):
                return None

        self.assertEqual(wait_doubao_auth(SettlingPage(), timeout=5), "logged_in")

    def test_wait_detects_midstream_yuanbao_logout(self):
        class LoggedOutPage:
            url = "https://yuanbao.tencent.com/chat/"

            def evaluate(self, script, *args, **kwargs):
                if "请登录后" in script:
                    return True
                return ""

        with self.assertRaises(PlatformFlowError) as raised:
            wait_for_answer_growth(
                LoggedOutPage(), timeout_sec=1, site_id="yuanbao"
            )
        self.assertEqual(raised.exception.error_code, "platform_auth_unavailable")


class AgentBayPageReuseTest(unittest.TestCase):
    def test_single_slow_tab_is_reused_instead_of_opening_another(self):
        class Page:
            url = "https://chat.deepseek.com/"

            def evaluate(self, *_args, **_kwargs):
                raise TimeoutError("renderer slow")

        class Context:
            def __init__(self):
                self.pages = [Page()]

            def new_page(self):
                raise AssertionError("cloud browser must not be asked for a second tab")

        class Browser:
            contexts = []

        ctx = Context()
        browser = Browser()
        browser.contexts = [ctx]
        with patch("cn_ai.scraper.page_answers", return_value=False):
            found = find_or_open_page(browser, "deepseek")
        self.assertIs(found, ctx.pages[0])

    def test_kimi_homepage_answer_counts_as_sent(self):
        class Page:
            def __init__(self):
                self.url = "https://www.kimi.com/"
                self.length = 0

            def evaluate(self, *_args, **_kwargs):
                return self.length

        page = Page()
        self.assertFalse(kimi_reply_advanced(page, "https://www.kimi.com/", 0))
        page.length = 80
        self.assertTrue(kimi_reply_advanced(page, "https://www.kimi.com/", 0))
        page.url = "https://www.kimi.com/chat/abc"
        self.assertTrue(kimi_reply_advanced(page, "https://www.kimi.com/", 80))


if __name__ == "__main__":
    unittest.main()
