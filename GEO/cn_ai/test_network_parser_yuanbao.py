# -*- coding: utf-8 -*-
import json
import unittest

from cn_ai.network_parser import (
    clean_answer_citation_markers,
    parse_doubao_chain,
    parse_yuanbao_detail,
    render_qianwen_media,
    render_yuanbao_images,
)


class YuanbaoParserTests(unittest.TestCase):
    def test_repairs_utf8_answer_misdecoded_as_latin1(self):
        expected = "是的，天王星是太阳系八大行星之一。"
        mojibake = expected.encode("utf-8").decode("latin1")
        body = json.dumps(
            {
                "convs": [
                    {
                        "speaker": "ai",
                        "speechesV2": [
                            {
                                "content": [
                                    {"type": "text", "msg": mojibake},
                                ]
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )
        answer, citations = parse_yuanbao_detail(body)
        self.assertEqual(answer, expected)
        self.assertEqual(citations, [])
        self.assertEqual(clean_answer_citation_markers(mojibake), expected)

    def test_cleans_yuanbao_inline_markers_after_mojibake_repair(self):
        expected = "10月1日"
        raw = (expected + "[](@mark_underline=1)").encode("utf-8").decode("latin1")
        self.assertEqual(clean_answer_citation_markers(raw), expected)

    def test_replaces_media_placeholders_with_markdown_images(self):
        image = "https://cdn.example.com/model-y.png"
        article = "https://news.example.com/model-y"
        body = json.dumps(
            {
                "convs": [
                    {
                        "speaker": "ai",
                        "speechesV2": [
                            {
                                "content": [
                                    {
                                        "type": "text",
                                        "msg": "1. Model Y\n[](@replace=media_abc)\n[](@replace=missing)",
                                    }
                                ],
                                "extra": {
                                    "replaces": [
                                        {
                                            "id": "media_abc",
                                            "display": "image",
                                            "multimedias": [
                                                {
                                                    "type": "image",
                                                    "url": image,
                                                    "description": "特斯拉 Model Y",
                                                    "jumpUrl": article,
                                                }
                                            ],
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )
        answer, _ = parse_yuanbao_detail(body)
        rendered, images = render_yuanbao_images(answer, [body])
        self.assertEqual(
            rendered,
            f"1. Model Y\n[![特斯拉 Model Y]({image})]({article})",
        )
        self.assertEqual(
            images,
            [{"description": "特斯拉 Model Y", "url": image, "jump_url": article}],
        )
        self.assertNotIn("@replace", rendered)

    def test_doubao_keeps_citations_from_an_earlier_assistant_message(self):
        body = json.dumps(
            {
                "downlink_body": {
                    "pull_singe_chain_downlink_body": {
                        "messages": [
                            {
                                "user_type": 2,
                                "content_block": [
                                    {
                                        "content": {
                                            "search_query_result_block": {
                                                "results": [
                                                    {"text_card": {"url": "https://www.tesla.cn/model3", "title": "Model 3"}}
                                                ]
                                            }
                                        }
                                    }
                                ],
                            },
                            {
                                "user_type": 2,
                                "content_block": [
                                    {"content": {"text_block": {"text": "这是一段足够长的回答正文，用来作为最终答案。"}}},
                                ],
                            },
                        ]
                    }
                }
            },
            ensure_ascii=False,
        )
        answer, citations = parse_doubao_chain(body)
        self.assertIn("最终答案", answer)
        self.assertEqual(citations, ["https://www.tesla.cn/model3"])

    def test_replaces_qianwen_video_cards_and_drops_followup_buttons(self):
        answer = "续航大概按七折估算。\n[[inline_action_reply_1]][(video_note_list_1)]"
        payload = {
            "data": {
                "messages": [
                    {
                        "content": answer,
                        "meta_data": {
                            "multi_load": [
                                {
                                    "type": "inline_action_reply",
                                    "source_seq": "inline_action_reply_1",
                                    "content": {"text": "交给工作助理"},
                                },
                                {
                                    "type": "video_note_list",
                                    "source_seq": "video_note_list_1",
                                    "content": {
                                        "list": [
                                            {
                                                "title": "特斯拉续航全解析",
                                                "duration": "05:45",
                                                "cover": "https://img.example/cover.jpg",
                                                "norm_url": "https://www.bilibili.com/video/BV1",
                                                "url": "https://page.sm.cn/wrapper",
                                            }
                                        ]
                                    },
                                },
                            ]
                        },
                    }
                ]
            }
        }
        body = "data: " + json.dumps(payload, ensure_ascii=False)
        rendered, images = render_qianwen_media(answer, [body])
        self.assertNotIn("inline_action_reply", rendered)
        self.assertNotIn("video_note_list", rendered)
        self.assertIn("https://img.example/cover.jpg", rendered)
        self.assertIn("https://www.bilibili.com/video/BV1", rendered)
        self.assertEqual(images[0]["url"], "https://img.example/cover.jpg")
        self.assertEqual(images[0]["jump_url"], "https://www.bilibili.com/video/BV1")

    def test_cleans_kimi_internal_ref_markers(self):
        raw = (
            "国庆节是每年的10月1日。"
            "<REF>cite✦tools://web_search:1#0:~:text=中华人民共和国国庆节</REF>"
        )
        self.assertEqual(
            clean_answer_citation_markers(raw),
            "国庆节是每年的10月1日。",
        )


if __name__ == "__main__":
    unittest.main()
