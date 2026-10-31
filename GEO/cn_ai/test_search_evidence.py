"""Offline checks for truthful, private webpage search observations."""
import hashlib
import json
from types import SimpleNamespace
import unittest

from cn_ai.network_parser import parse_deepseek_sse
from cn_ai.scraper import NetworkCapture
from cn_ai.search_evidence import request_search_evidence, web_search_evidence
from cn_ai.site_flows import outbound_prompt


PROMPT = "  行业 数据：如何找？  \n保留中文、空格\t及标点。 "
SUFFIX = "；请联网搜索后回答，并列出参考来源链接。"


class RequestSearchEvidenceTests(unittest.TestCase):
    def test_only_model_decided_sites_add_the_exact_wire_instruction(self):
        from cn_ai.search_policy import SEARCH_INSTRUCTIONS
        for site in ('qianwen', 'yiyan', 'yuanbao'):
            with self.subTest(site=site):
                self.assertEqual(outbound_prompt(site, PROMPT), PROMPT)
        for site in ('kimi', 'deepseek', 'doubao'):
            wire = outbound_prompt(site, PROMPT)
            self.assertEqual(wire, PROMPT + '\n\n' + SEARCH_INSTRUCTIONS[site])
            self.assertEqual(outbound_prompt(site, wire), wire)
            evidence = request_search_evidence(site, 'https://' + ('chat.deepseek.com' if site == 'deepseek' else 'www.'+site+'.com') + '/chat/completion', {'content': wire}, PROMPT)
            self.assertFalse(evidence['original_prompt_observed'])
            self.assertTrue(evidence['submitted_prompt_observed'])
            self.assertNotIn(PROMPT, json.dumps(evidence, ensure_ascii=False))

    def test_exact_original_prompt_preserves_chinese_and_whitespace(self):
        payload = {"messages": [{"role": "user", "content": PROMPT}], "enable_search": True}
        before = json.dumps(payload, ensure_ascii=False)
        evidence = request_search_evidence(
            "deepseek", "https://chat.deepseek.com/api/v0/chat/completion", payload, PROMPT
        )
        self.assertTrue(evidence["original_prompt_observed"])
        self.assertEqual(
            evidence["expected_prompt_sha256"], hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
        )
        self.assertEqual(json.dumps(payload, ensure_ascii=False), before)

    def test_suffix_or_whitespace_changes_do_not_prove_original_sent(self):
        for submitted in (PROMPT + SUFFIX, PROMPT.strip(), PROMPT.replace("  ", " ")):
            with self.subTest(submitted_length=len(submitted)):
                evidence = request_search_evidence(
                    "doubao", "https://www.doubao.com/chat/completion",
                    {"content": submitted, "search_enabled": True}, PROMPT,
                )
                self.assertFalse(evidence["original_prompt_observed"])

    def test_nested_serialized_content_keeps_only_safe_observations(self):
        payload = {
            "messages": [{
                "content": json.dumps({
                    "messages": [{"content": PROMPT}],
                    "options": {"enable_search": True, "search_mode": "auto"},
                    "authorization": "secret-nested-bearer",
                }, ensure_ascii=False)
            }]
        }
        evidence = request_search_evidence(
            "kimi", "https://www.kimi.com/apiv2/kimi.gateway.chat.v1.ChatService/Chat",
            payload, PROMPT,
        )
        self.assertTrue(evidence["original_prompt_observed"])
        self.assertEqual(evidence["search_flags"]["enable_search"], True)
        self.assertEqual(evidence["search_flags"]["search_mode"], "auto")
        self.assertNotIn("secret-nested-bearer", json.dumps(evidence))

    def test_metadata_never_retains_body_query_tokens_or_original_text(self):
        url = (
            "https://private-user:private-password@www.doubao.com/chat/completion"
            "?query=secret-url-query&token=secret-url-token#secret-fragment"
        )
        payload = {
            "messages": [{"content": PROMPT}],
            "search_enabled": True,
            "search_mode": "auto",
            "web_search": "secret-disguised-as-search-value",
            "query": "secret-other-query",
            "token": "secret-body-token",
            "authorization": "secret-body-bearer",
            "unrelated": {"body": "secret-raw-body"},
        }
        evidence = request_search_evidence("doubao", url, payload, PROMPT)
        self.assertEqual(evidence["endpoint"], "/chat/completion")
        self.assertEqual(set(evidence), {
            "endpoint", "search_flags", "original_prompt_observed", "expected_prompt_sha256",
            "submitted_prompt_observed", "expected_submitted_prompt_sha256"
        })
        serialized = json.dumps(evidence, ensure_ascii=False)
        for secret in (
            PROMPT, "private-user", "private-password", "secret-url-query",
            "secret-url-token", "secret-fragment", "secret-disguised-as-search-value",
            "secret-other-query", "secret-body-token", "secret-body-bearer", "secret-raw-body",
        ):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("web_search", evidence["search_flags"])

    def test_baidu_serialized_message_wrapper_observes_original_without_retaining_message(self):
        payload = {"message": "  " + json.dumps({"query": PROMPT, "web_search": True,
                                                 "token": "private-wrapper-token"}, ensure_ascii=False)}
        evidence = request_search_evidence(
            "yiyan", "https://chat.baidu.com/aichat/api/conversation", payload, PROMPT
        )
        self.assertTrue(evidence["original_prompt_observed"])
        self.assertEqual(evidence["search_flags"], {"web_search": True})
        self.assertNotIn("private-wrapper-token", json.dumps(evidence))
        self.assertNotIn(PROMPT, json.dumps(evidence, ensure_ascii=False))

    def test_each_provider_accepts_its_real_generation_endpoint(self):
        endpoints = {
            "deepseek": "https://chat.deepseek.com/api/v0/chat/completion",
            "doubao": "https://www.doubao.com/chat/completion",
            "kimi": "https://www.kimi.com/apiv2/kimi.gateway.chat.v1.ChatService/Chat",
            "qianwen": "https://www.qianwen.com/api/v2/chat",
            "yuanbao": "https://yuanbao.tencent.com/api/chat",
            "yiyan": "https://chat.baidu.com/aichat/api/conversation",
        }
        for site, url in endpoints.items():
            with self.subTest(site=site):
                evidence = request_search_evidence(site, url, {"query": PROMPT}, PROMPT)
                self.assertTrue(evidence["original_prompt_observed"])

    def test_unrelated_platform_host_and_path_cannot_supply_evidence(self):
        cases = [
            ("doubao", "https://www.kimi.com/chat/completion"),
            ("doubao", "https://doubao.com.attacker.example/chat/completion"),
            ("doubao", "https://otherdoubao.com/chat/completion"),
            ("yuanbao", "https://other.tencent.com/api/chat"),
            ("yiyan", "https://www.baidu.com/aichat/api/conversation"),
            ("unknown", "https://www.doubao.com/chat/completion"),
            ("deepseek", "https://chat.deepseek.com/api/v0/users/current"),
            ("deepseek", "https://chat.deepseek.com/assets/runtime.js"),
        ]
        for site, url in cases:
            with self.subTest(site=site, url=url):
                self.assertIsNone(request_search_evidence(
                    site, url, {"query": PROMPT, "enable_search": True}, PROMPT
                ))

    def test_unrelated_payload_has_no_evidence(self):
        self.assertIsNone(request_search_evidence(
            "deepseek", "https://chat.deepseek.com/api/v0/chat/completion",
            {"query": "A different question", "api_token": "private"}, PROMPT,
        ))


class WebSearchEvidenceTests(unittest.TestCase):
    def test_yuanbao_native_flags_never_replace_actual_sources(self):
        for flag in (0, 1):
            request = request_search_evidence('yuanbao', 'https://yuanbao.tencent.com/api/chat',
                {'query': PROMPT, 'internetFlag': flag, 'openInternetSearch': flag == 1}, PROMPT)
            self.assertEqual(request['search_flags'], {'internetflag': flag, 'openinternetsearch': flag == 1})
            evidence = web_search_evidence('yuanbao', None, [], [request])
            self.assertEqual(evidence['web_search_status'], 'not_confirmed')

    def test_ui_on_auto_and_request_flags_alone_do_not_prove_search_used(self):
        request = request_search_evidence(
            "deepseek", "https://chat.deepseek.com/api/v0/chat/completion",
            {"query": PROMPT, "enable_search": True}, PROMPT,
        )
        for setting, confirmed in (("on", True), ("auto", True), ("unconfirmed", False)):
            with self.subTest(setting=setting):
                diagnostic = SimpleNamespace(meta={
                    "web_search_settings": {
                        "status": setting, "confirmed": confirmed, "control": "智能搜索",
                    }
                })
                evidence = web_search_evidence("deepseek", diagnostic, [], [request])
                self.assertEqual(evidence["web_search_setting"], setting)
                self.assertEqual(evidence["web_search_setting_confirmed"], confirmed)
                self.assertEqual(evidence["web_search_status"], "not_confirmed")
                self.assertEqual(evidence["web_search_error_code"], "search_not_confirmed")
                self.assertEqual(evidence["search_result_count"], 0)
                self.assertEqual(evidence["web_search_request_evidence"], [request])

    def test_actual_protocol_sources_prove_search_without_enabled_ui(self):
        event = {"v": {"response": {"fragments": [
            {"type": "TOOL_SEARCH", "results": [
                {"url": "https://sources.example/reference/a"},
                {"url": "https://sources.example/reference/b"},
                {"url": "https://sources.example/reference/a"},
            ]},
            {"type": "RESPONSE", "content": "Completed answer"},
        ]}}}
        _, citations = parse_deepseek_sse("data: " + json.dumps(event) + "\n\ndata: [DONE]\n")
        self.assertEqual(set(citations), {
            "https://sources.example/reference/a", "https://sources.example/reference/b"
        })
        evidence = web_search_evidence("deepseek", None, citations, [])
        self.assertEqual(evidence["web_search_status"], "used")
        self.assertEqual(evidence["search_result_count"], 2)
        self.assertEqual(evidence["web_search_setting"], "not_observed")
        self.assertFalse(evidence["web_search_setting_confirmed"])
        self.assertNotIn("web_search_error_code", evidence)

    def test_urls_only_in_answer_text_are_not_search_protocol_sources(self):
        event = {"v": {"response": {"fragments": [
            {"type": "RESPONSE", "content": "Visit https://sources.example/manual-link"}
        ]}}}
        _, citations = parse_deepseek_sse("data: " + json.dumps(event) + "\n\n")
        self.assertEqual(citations, [])
        evidence = web_search_evidence("deepseek", None, citations, [])
        self.assertEqual(evidence["web_search_status"], "not_confirmed")

    def test_only_last_ten_safe_request_observations_are_exposed(self):
        requests = [{"endpoint": "/chat/completion", "search_flags": {}, "sequence": i}
                    for i in range(15)]
        diagnostic = SimpleNamespace(meta={
            "web_search_settings": {"status": "on", "confirmed": False},
            "private_token": "do-not-publish-this",
            "raw_body": "do-not-publish-body",
        })
        evidence = web_search_evidence("doubao", diagnostic, ["https://sources.example/a"], requests)
        self.assertEqual(evidence["web_search_request_evidence"], requests[-10:])
        self.assertFalse(evidence["web_search_setting_confirmed"])
        self.assertEqual(len(requests), 15)
        self.assertNotIn("do-not-publish", json.dumps(evidence))

    def test_legacy_deepseek_setting_remains_separate_from_search_use(self):
        diagnostic = SimpleNamespace(meta={"deepseek_settings": {"search": "on", "thinking": "off"}})
        evidence = web_search_evidence("deepseek", diagnostic, [], [])
        self.assertEqual(evidence["web_search_setting"], "on")
        self.assertTrue(evidence["web_search_setting_confirmed"])
        self.assertEqual(evidence["web_search_status"], "not_confirmed")


class FakePage:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler


class FakeRequest:
    def __init__(self, payload, method="POST", url="https://www.doubao.com/chat/completion"):
        self.method, self.url = method, url
        self.post_data_json = payload


class SearchRequestCaptureTests(unittest.TestCase):
    def test_request_callback_retains_private_bounded_metadata_without_response_reads(self):
        page = FakePage()
        capture = NetworkCapture("doubao", PROMPT)
        capture.attach(page)
        request = FakeRequest(
            {"query": PROMPT, "enable_search": True, "token": "secret-callback-token"},
            url="https://www.doubao.com/chat/completion?token=secret-query-token",
        )
        for _ in range(23):
            page.handlers["request"](request)
        self.assertEqual(len(capture.search_requests), 20)
        self.assertEqual(capture.items, [])
        serialized = json.dumps(capture.search_requests, ensure_ascii=False)
        for secret in (PROMPT, "secret-callback-token", "secret-query-token"):
            self.assertNotIn(secret, serialized)

    def test_request_callback_ignores_get_and_recovers_after_non_json_body(self):
        class NonJsonRequest:
            method = "POST"
            url = "https://www.doubao.com/chat/completion"

            @property
            def post_data_json(self):
                raise ValueError("not JSON")

        page = FakePage()
        capture = NetworkCapture("doubao", PROMPT)
        capture.attach(page)
        page.handlers["request"](FakeRequest({"query": PROMPT}, method="GET"))
        page.handlers["request"](NonJsonRequest())
        self.assertEqual(capture.search_requests, [])
        page.handlers["request"](FakeRequest({"query": PROMPT}))
        self.assertEqual(len(capture.search_requests), 1)
        self.assertTrue(capture.search_requests[0]["original_prompt_observed"])


if __name__ == "__main__":
    unittest.main()
