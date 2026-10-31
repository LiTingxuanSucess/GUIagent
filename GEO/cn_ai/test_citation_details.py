# -*- coding: utf-8 -*-
import json
import unittest

from cn_ai.citation_details import _MetadataParser, _decode_html, build_citation_details
from cn_ai.network_parser import (
    _maybe_fix_mojibake,
    clean_answer_citation_markers,
    extract_capture_citation_details,
    parse_deepseek_share_url,
    parse_yiyan_history,
)


class CitationDetailsTest(unittest.TestCase):
    def test_dom_hint_wins_and_shape_is_stable(self):
        fetched = []
        details = build_citation_details(
            ["https://www.shanshu.ai/solver?x=1"],
            dom_details=[
                {
                    "url": "https://www.shanshu.ai/solver?x=1",
                    "title": "杉数科技 COPT 介绍",
                    "icon": "https://www.shanshu.ai/icon.png",
                    "site_name": "杉数科技官网",
                }
            ],
            fetcher=lambda url: fetched.append(url) or {},
        )
        self.assertEqual(fetched, [])
        self.assertEqual(
            details,
            [
                {
                    "icon": "https://www.shanshu.ai/icon.png",
                    "index": 1,
                    "site_name": "杉数科技官网",
                    "title": "杉数科技 COPT 介绍",
                    "url": "https://www.shanshu.ai/solver?x=1",
                }
            ],
        )

    def test_fetched_metadata_and_empty_fallback(self):
        def fetcher(url):
            if "example.com" in url:
                return {"title": "示例标题", "site_name": "示例官网", "icon": "https://example.com/favicon.ico"}
            return {}

        details = build_citation_details(
            ["https://example.com/a", "https://www.no-meta.test/b"],
            fetcher=fetcher,
        )
        self.assertEqual(details[0]["title"], "示例标题")
        self.assertEqual(details[0]["site_name"], "示例官网")
        self.assertEqual(details[0]["icon"], "https://example.com/favicon.ico")
        self.assertEqual(details[1]["title"], "")
        self.assertEqual(details[1]["icon"], "")
        self.assertEqual(details[1]["site_name"], "no-meta.test")

    def test_reference_number_is_replaced_by_fetched_title(self):
        details = build_citation_details(
            ["https://example.com/a"],
            dom_details=[{"url": "https://example.com/a", "title": "- 1"}],
            fetcher=lambda _: {
                "title": "undefined-真实网页标题",
                "site_name": "示例官网",
                "icon": "https://example.com/favicon.ico",
            },
        )
        self.assertEqual(details[0]["title"], "真实网页标题")
        self.assertEqual(details[0]["icon"], "https://example.com/favicon.ico")

    def test_platform_original_metadata_has_priority_and_index_is_rebuilt(self):
        raw = (
            '{"results":[{"url":"https://example.com/a","title":"平台标题",'
            '"siteName":"平台站点","iconUrl":"https://cdn.example.com/icon.png","index":9}]}'
        )
        platform = extract_capture_citation_details(raw)
        details = build_citation_details(
            ["https://example.com/a"],
            dom_details=platform + [
                {
                    "url": "https://example.com/a",
                    "title": "- 1",
                    "site_name": "example.com",
                    "icon": "",
                }
            ],
            fetcher=lambda _: self.fail("complete platform metadata should avoid fetch"),
        )
        self.assertEqual(
            list(details[0]),
            ["icon", "index", "site_name", "title", "url"],
        )
        self.assertEqual(details[0]["index"], 1)
        self.assertEqual(details[0]["title"], "平台标题")
        self.assertEqual(details[0]["site_name"], "平台站点")
        self.assertEqual(details[0]["icon"], "https://cdn.example.com/icon.png")

    def test_repairs_latin1_mojibake_citation_title(self):
        expected = "北京英诺特生物技术股份有限公司|呼吸道|消化道|优生优育检测"
        mojibake = expected.encode("utf-8").decode("latin1")
        raw = json.dumps(
            {
                "docs": [
                    {
                        "url": "https://www.innovita.com.cn/",
                        "title": mojibake,
                        "sourceName": "北京英诺特生物技术股份有限公司",
                        "icon_url": "https://example.com/icon.png",
                    }
                ]
            },
            ensure_ascii=False,
        )
        platform = extract_capture_citation_details(raw)
        self.assertEqual(platform[0]["title"], expected)
        details = build_citation_details(
            ["https://www.innovita.com.cn/"],
            dom_details=platform,
            fetcher=lambda _: self.fail("repaired citation metadata should not be fetched"),
        )
        self.assertEqual(details[0]["title"], expected)
        self.assertEqual(details[0]["site_name"], "北京英诺特生物技术股份有限公司")

    def test_repairs_short_and_gbk_mojibake_titles(self):
        self.assertEqual(_maybe_fix_mojibake("首页".encode("utf-8").decode("latin1")), "首页")
        expected = "2026年年度报告"
        self.assertEqual(
            _maybe_fix_mojibake(expected.encode("gbk").decode("latin1")),
            expected,
        )
        for text in ("café au lait", "Ångström unit", "naïve résumé", "Größe der Straße", "Åäöéèü"):
            self.assertEqual(_maybe_fix_mojibake(text), text)

    def test_garbled_title_falls_back_to_fetched_page_title(self):
        details = build_citation_details(
            ["https://example.com/a"],
            dom_details=[
                {
                    "url": "https://example.com/a",
                    "title": "àààààààà",
                    "site_name": "示例官网",
                    "icon": "https://example.com/icon.png",
                }
            ],
            fetcher=lambda _: {"title": "真实网页标题", "site_name": "", "icon": ""},
        )
        self.assertEqual(details[0]["title"], "真实网页标题")
        self.assertEqual(details[0]["site_name"], "示例官网")

    def test_html_charset_ignores_latin1_header_and_reads_gbk(self):
        utf8_page = (
            "<html><head><meta charset=\"utf-8\"><title>示例标题</title>"
            "<meta property=\"og:site_name\" content=\"示例官网\"></head></html>"
        )
        text = _decode_html(utf8_page.encode("utf-8"), "iso-8859-1")
        parser = _MetadataParser()
        parser.feed(text)
        self.assertEqual(
            parser.metadata("https://example.com/a")["title"],
            "示例标题",
        )
        gbk_page = (
            "<html><head><meta charset=\"gbk\"><title>年度报告披露</title></head></html>"
        )
        text = _decode_html(gbk_page.encode("gbk"), None)
        parser = _MetadataParser()
        parser.feed(text)
        self.assertEqual(parser.metadata("https://example.com/b")["title"], "年度报告披露")

    def test_html_metadata_parser(self):
        parser = _MetadataParser()
        parser.feed(
            '<html><head><title>普通标题</title>'
            '<meta property="og:title" content="OG 标题">'
            '<meta property="og:site_name" content="站点名称">'
            '<link rel="icon" href="/favicon.png"></head></html>'
        )
        self.assertEqual(
            parser.metadata("https://example.com/page"),
            {
                "title": "OG 标题",
                "icon": "https://example.com/favicon.png",
                "site_name": "站点名称",
            },
        )

    def test_answer_markers_and_share_url(self):
        answer = "第一段[citation:7][citation:12]，第二段【reference:3】。"
        self.assertEqual(clean_answer_citation_markers(answer), "第一段，第二段。")
        self.assertEqual(
            parse_deepseek_share_url(
                '{"code":0,"data":{"biz_data":{"share_id":"abc123_xyz"}}}'
            ),
            "https://chat.deepseek.com/share/abc123_xyz",
        )

    def test_yiyan_progress_message_is_not_a_completed_answer(self):
        body = (
            '{"data":{"historyData":[{"msgList":[{"query":"测试问题",'
            '"chatResponse":[{"data":{"message":{"content":{"generator":[{"data":{'
            '"value":"好的，收到你的需求。我将联网搜索。大概需要3-5min，生成过程中你可以继续向我对话。"}}]}}}}]}]}]}}'
        )
        self.assertEqual(parse_yiyan_history(body, prompt="测试问题"), ("", []))


if __name__ == "__main__":
    unittest.main()
