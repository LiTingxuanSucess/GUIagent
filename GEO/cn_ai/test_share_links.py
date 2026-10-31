# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
import json
import unittest
from unittest import mock

import cn_ai.share_links as share_links
from cn_ai.share_links import (
    _answer_snippets,
    _candidate_strings,
    _first_public_url,
    _is_login_wall,
    is_public_share_url,
    observed_public_share_url,
    share_answer_hint,
    verify_public_share,
)


class ShareLinkTests(unittest.TestCase):
    def test_accepts_observed_public_share_shapes(self):
        cases = {
            "deepseek": "https://chat.deepseek.com/share/abc123_xyz",
            "qianwen": "https://www.qianwen.com/share?shareId=abc123xyz",
            "yuanbao": "https://yuanbao.tencent.com/bot/app/share/chat/abc123xyz",
            "kimi": "https://www.kimi.com/share/1a0edc13-b1e2-8ec2-8000-0000b2b92e9e",
            "doubao": "https://www.doubao.com/s/abc123xyz",
            "yiyan": "https://chat.baidu.com/csaitab/history/share?share_id=abc123xyz&v=2",
        }
        for site_id, url in cases.items():
            self.assertTrue(is_public_share_url(site_id, url), site_id)
        self.assertTrue(
            is_public_share_url(
                "yiyan",
                "https://mr.baidu.com/r/23GC0PRap44?f=ot&u=5667a6dc1910455a",
            )
        )
        self.assertTrue(
            is_public_share_url(
                "qianwen",
                "https://qianwen.my.cn/share/chat/a7b435e82031412ba034aa42b78680d6?lang=zh-CN",
            )
        )

    def test_rejects_private_or_old_unusable_urls(self):
        cases = {
            "deepseek": "https://chat.deepseek.com/a/chat/s/private-id",
            "qianwen": "https://www.qianwen.com/chat/private-id",
            "yuanbao": "https://yuanbao.tencent.com/chat/naQivTmsDa/private",
            "kimi": "https://www.kimi.com/chat/1a0e81b1-b692-81cc-8000-095942a572b5",
            "doubao": "https://www.doubao.com/chat/private-id",
            "yiyan": "https://wenxin.baidu.com/search/123?enter_type=chat_url",
        }
        for site_id, url in cases.items():
            self.assertFalse(is_public_share_url(site_id, url), site_id)
        self.assertFalse(
            is_public_share_url(
                "yiyan",
                "https://chat.baidu.com/search?shareId=abc123xyz",
            )
        )
        self.assertFalse(
            is_public_share_url("yiyan", "https://yiyan.baidu.com/share?shareId=abc123xyz")
        )
        self.assertFalse(
            is_public_share_url(
                "yiyan",
                "https://mr.baidu.com/r/23GC0PRap44?f=other&u=5667a6dc1910455a",
            )
        )

    def test_does_not_invent_urls_from_bare_ids(self):
        self.assertEqual(_first_public_url("qianwen", ["abc123", "abc123xyz"]), "")
        self.assertEqual(observed_public_share_url("qianwen", ["abc123xyz"]), "")
        self.assertEqual(
            observed_public_share_url(
                "kimi",
                ["https://example.com/?shareId=1a0e81b1-b692-81cc-8000-095942a572b5"],
            ),
            "",
        )
        self.assertEqual(
            observed_public_share_url(
                "yiyan",
                ["f-5crNGRMsEWXMmcTip6BgJT-RlMjezrxEZ-9bu4MEYygg7kvn8@"],
            ),
            "",
        )
        self.assertEqual(
            observed_public_share_url(
                "kimi",
                ["/share/zh/1a0e81b1-b692-81cc-8000-095942a572b5"],
            ),
            "https://www.kimi.com/share/zh/1a0e81b1-b692-81cc-8000-095942a572b5",
        )
        self.assertEqual(
            observed_public_share_url(
                "yiyan",
                ["/csaitab/history/share?share_id=abc123xyz&v=2"],
            ),
            "https://chat.baidu.com/csaitab/history/share?share_id=abc123xyz&v=2",
        )

    def test_accepts_only_observed_platform_share_paths(self):
        cases = {
            "qianwen": (
                "/share?shareId=abc123xyz",
                "https://www.qianwen.com/share?shareId=abc123xyz",
            ),
            "yuanbao": (
                "/bot/app/share/chat/abc123xyz",
                "https://yuanbao.tencent.com/bot/app/share/chat/abc123xyz",
            ),
            "doubao": (
                "/s/abc123xyz",
                "https://www.doubao.com/s/abc123xyz",
            ),
        }
        for site_id, (path, expected) in cases.items():
            self.assertEqual(observed_public_share_url(site_id, [path]), expected)
        self.assertEqual(observed_public_share_url("qianwen", ["abc123xyz"]), "")

    def test_extracts_escaped_share_urls_without_using_bare_ids(self):
        raw = (
            r'{"url":"https:\/\/www.kimi.com\/share\/zh\/abc123xyz",'
            r'"wenxin":"\/csaitab\/history\/share?share_id=wenxin12xyz\\u0026v=2",'
            r'"share_id":"must_not_be_used"}'
        )
        candidates = _candidate_strings(raw)
        self.assertIn("https://www.kimi.com/share/zh/abc123xyz", candidates)
        self.assertIn(
            "/csaitab/history/share?share_id=wenxin12xyz&v=2",
            candidates,
        )
        self.assertNotIn("must_not_be_used", candidates)

    def test_extracts_current_kimi_share_url_from_create_response(self):
        url = "https://www.kimi.com/share/1a0edc13-b1e2-8ec2-8000-0000b2b92e9e"
        raw = '{"share":{"id":"unused-id","url":"' + url + '"}}'
        self.assertEqual(observed_public_share_url("kimi", _candidate_strings(raw)), url)
        self.assertEqual(
            observed_public_share_url(
                "kimi",
                _candidate_strings(
                    '{"url":"/share/1a0edc13-b1e2-8ec2-8000-0000b2b92e9e"}'
                ),
            ),
            url,
        )

    def test_yuanbao_share_api_uses_only_observed_target_url(self):
        page = mock.Mock()
        page.evaluate.return_value = (
            '{"status":200,"body":"{\\"shareId\\":\\"cSHgoWhKDRma\\",'
            '\\"targetUrl\\":\\"https://yb.tencent.com/s/cSHgoWhKDRma\\"}"}'
        )
        result = share_links._create_yuanbao(page)
        self.assertEqual(result["status"], 200)
        self.assertEqual(
            result["url"],
            "https://yb.tencent.com/s/cSHgoWhKDRma",
        )

    def test_shape_check_is_not_content_proof(self):
        url = "https://www.kimi.com/share/zh/1a0e81b1-b692-81cc-8000-095942a572b5"
        self.assertTrue(is_public_share_url("kimi", url))
        result = verify_public_share(None, "kimi", url, "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_answer_to_verify")

    @mock.patch("cn_ai.share_links.time.sleep")
    @mock.patch("cn_ai.share_links._http_get")
    @mock.patch("cn_ai.share_links._qianwen_public_answer", return_value="")
    def test_qianwen_retries_until_new_share_is_visible(self, public_answer, http_get, sleep):
        url = (
            "https://qianwen.my.cn/share/chat/"
            "a7b435e82031412ba034aa42b78680d6?lang=zh-CN"
        )
        http_get.side_effect = [
            ("页面正在生成", url, 200),
            ("仍在生成", url, 200),
            ("是的，火星是太阳系八大行星之一。", url, 200),
        ]
        result = verify_public_share(
            None,
            "qianwen",
            url,
            "是的，火星是太阳系八大行星之一。",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "http")
        self.assertEqual(http_get.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(3)])

    @mock.patch("cn_ai.share_links.time.sleep")
    @mock.patch("cn_ai.share_links._http_get")
    def test_yuanbao_retries_until_new_share_is_visible(self, http_get, sleep):
        url = "https://yb.tencent.com/s/cSHgoWhKDRma"
        http_get.side_effect = [
            ("页面正在生成", url, 200),
            ("是的，大象是哺乳动物。", url, 200),
        ]
        result = verify_public_share(
            None,
            "yuanbao",
            url,
            "是的，大象是哺乳动物。",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(http_get.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_tls_verification_stays_on(self):
        source = inspect.getsource(share_links)
        self.assertNotIn("_create_unverified_context", source)
        self.assertNotIn("ignore_https_errors", source)

    def test_login_wall_and_snippets(self):
        self.assertTrue(_is_login_wall("https://www.qianwen.com/login", "请先登录后查看"))
        self.assertFalse(_is_login_wall("https://chat.deepseek.com/share/abc123_xyz", "哈希就是指纹" * 20))
        snippets = _answer_snippets("哈希就是把任意长度的数据映射成固定长度指纹的算法。")
        self.assertTrue(any("哈希" in item or "指纹" in item for item in snippets))
        self.assertEqual(_answer_snippets("中国国庆节"), ["中国国庆节"])
        self.assertEqual(_answer_snippets("是"), [])

    def test_kimi_share_answer_hint_uses_shared_assistant_text(self):
        share = {
            "body": json.dumps(
                {
                    "share": {
                        "messages": [
                            {"role": "user", "blocks": []},
                            {
                                "role": "assistant",
                                "blocks": [
                                    {"think": {"content": "内部思考"}},
                                    {"text": {"content": "中华人民共和国国庆节为每年10月1日。"}},
                                ],
                            },
                        ]
                    }
                },
                ensure_ascii=False,
            )
        }
        self.assertEqual(
            share_answer_hint("kimi", share, "回退答案"),
            "中华人民共和国国庆节为每年10月1日。",
        )

    def test_yiyan_share_answer_hint_keeps_answer_from_current_task(self):
        share = {
            "candidates": [
                "对话历史\n问题\n搜索全网21篇资料\n21.\n最后一条资料\n\n"
                "太阳光穿过大气层时，蓝光更容易发生瑞利散射。\n"
                "人眼因此看到的天空通常呈蓝色。\n\n全选\n分享图片\n复制链接"
            ]
        }
        self.assertEqual(
            share_answer_hint("yiyan", share, "当前任务已完成的回答正文"),
            "当前任务已完成的回答正文",
        )


if __name__ == "__main__":
    unittest.main()
