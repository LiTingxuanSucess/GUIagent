import copy
import json
import unittest

from cn_ai.yiyan_citations import current_yiyan_sources, enrich_yiyan_checkpoint

PROMPT = "说明文件摘要校验的作用。"
URL = "https://example.org/checksum"
OTHER = "https://example.org/another-turn"


def stream(prompt=PROMPT, session="current", refs=None, matched=None):
    refs = refs if refs is not None else [{"url": URL, "text": "摘要校验资料", "source": "参考网站", "icon": "https://example.org/icon.png"}]
    events = [{"status": 0, "query": prompt}, {"status": 0, "sessionId": session,
              "data": {"message": {"content": {"generator": {"data": {"referenceList": refs}}}}}}]
    capture = {"url": "https://chat.baidu.com/aichat/api/conversation", "status": 200,
               "body": "\n".join("data: " + json.dumps(x, ensure_ascii=False) for x in events)}
    if matched is not None:
        capture["request_prompt_matched"] = matched
    return capture


class YiyanCitationTests(unittest.TestCase):
    def test_sources_preserve_platform_title_site_icon_and_deduplicate(self):
        capture = stream()
        refs, details = current_yiyan_sources([capture, capture], PROMPT, "https://wenxin.baidu.com/search/current")
        self.assertEqual(refs, [URL])
        self.assertEqual(details[0]["title"], "摘要校验资料")
        self.assertEqual(details[0]["site_name"], "参考网站")
        self.assertEqual(details[0]["icon"], "https://example.org/icon.png")

    def test_other_question_substring_session_request_and_endpoint_are_rejected(self):
        bad_endpoint = stream(); bad_endpoint["url"] = "https://chat.baidu.com/api/history/sidebar"
        bad_host = stream(); bad_host["url"] = "https://chat.baidu.com.evil.test/aichat/api/conversation"
        failed = stream(); failed["status"] = 403
        for capture in [stream(PROMPT+"还有其他要求"), stream("另一个问题"), stream(session="old"),
                        stream(matched=False), bad_endpoint, bad_host, failed]:
            with self.subTest(capture=capture):
                self.assertEqual(current_yiyan_sources([capture], PROMPT, "https://wenxin.baidu.com/search/current"), ([], []))

    def test_unbound_tail_requires_request_or_exact_query_and_ignores_truncated_event(self):
        capture = stream(matched=True)
        capture["body"] = capture["body"].splitlines()[1] + '\ndata: {"broken":'
        self.assertEqual(current_yiyan_sources([capture], PROMPT, "https://wenxin.baidu.com/")[0], [URL])
        capture.pop("request_prompt_matched")
        self.assertEqual(current_yiyan_sources([capture], PROMPT, "https://wenxin.baidu.com/")[0], [])

    def test_history_is_bound_to_exact_session_and_latest_question(self):
        def history(session, query, url):
            return {"sessionId": session, "msgList": [{"query": query, "chatResponse": [{"sessionId": session,
                    "data": {"message": {"content": {"generator": [{"data": {"referenceList": [{"url": url, "text": "来源"}]}}]}}}}]}]}
        body = {"data": {"historyData": [history("old", PROMPT, OTHER), history("current", PROMPT, URL)]}}
        capture = {"url": "https://chat.baidu.com/csaitab/history/list", "body": json.dumps(body)}
        self.assertEqual(current_yiyan_sources([capture], PROMPT, "https://wenxin.baidu.com/search/current")[0], [URL])
        self.assertEqual(current_yiyan_sources([capture], PROMPT, "https://wenxin.baidu.com/")[0], [])
        body["data"]["historyData"][1]["msgList"].append({"query": "最新的其他问题", "chatResponse": []})
        capture["body"] = json.dumps(body)
        self.assertEqual(current_yiyan_sources([capture], PROMPT, "https://wenxin.baidu.com/search/current")[0], [])

    def test_enrichment_keeps_answer_share_and_existing_citations_without_mutating_snapshot(self):
        checkpoint = {"site_id": "yiyan", "prompt": PROMPT, "answer": "已确认的最终回答", "answer_raw": "原始回答",
                      "source": "dom:yiyan-standard", "url": "verified-share", "share_status": "verified",
                      "citations": [OTHER], "citation_details": [], "web_search_error_code": "search_not_confirmed",
                      "quality_issues": ["citations_missing", "share_timeout"]}
        original = copy.deepcopy(checkpoint)
        result, sources = enrich_yiyan_checkpoint(checkpoint, [stream()], "https://wenxin.baidu.com/search/current")
        self.assertEqual(checkpoint, original)
        for field in ["answer", "answer_raw", "url", "share_status", "source"]:
            self.assertEqual(result[field], original[field])
        self.assertEqual(result["citations"], [OTHER, URL])
        self.assertEqual(sources, [URL])
        self.assertEqual(result["web_search_status"], "used")
        self.assertEqual(result["search_result_count"], 1)
        self.assertNotIn("web_search_error_code", result)
        self.assertEqual(result["quality_issues"], ["share_timeout"])

    def test_no_current_network_sources_does_not_invent_search_evidence(self):
        checkpoint = {"prompt": PROMPT, "answer": "已确认回答", "source": "dom:yiyan-standard",
                      "citations": [URL], "citation_details": [], "quality_issues": []}
        result, sources = enrich_yiyan_checkpoint(checkpoint, [stream("旧问题")], "https://wenxin.baidu.com/search/current")
        self.assertEqual(result["citations"], [URL])
        self.assertEqual(sources, [])
        self.assertEqual(result["web_search_status"], "not_confirmed")


if __name__ == "__main__":
    unittest.main()
