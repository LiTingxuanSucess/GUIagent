"""Keep Wenxin references bound to the question and response that supplied them."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from cn_ai.citation_details import build_citation_details
from cn_ai.network_parser import _is_noise_citation
from cn_ai.protocol_completion import normalized, yiyan_turn
from cn_ai.search_evidence import web_search_evidence


def _query(value):
    if isinstance(value, str):
        return normalized(value)
    if isinstance(value, list):
        parts = []
        for part in value:
            if not isinstance(part, dict):
                continue
            data = part.get("data")
            text = data.get("text") if isinstance(data, dict) else None
            if isinstance(text, dict) and isinstance(text.get("query"), str):
                parts.append(text["query"])
        return normalized("".join(parts))
    return ""


def _roots(body):
    try:
        value = json.loads(body)
        if isinstance(value, dict):
            yield value
        return
    except (ValueError, TypeError):
        pass
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            value = json.loads(line[5:].strip())
            if isinstance(value, dict):
                yield value
        except (ValueError, TypeError):
            continue  # A truncated final SSE event is not a source.


def _references(value, depth=0):
    if depth > 16:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "referenceList" and isinstance(child, list):
                yield from (ref for ref in child if isinstance(ref, dict))
            elif isinstance(child, (dict, list)):
                yield from _references(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child, depth + 1)


def current_yiyan_sources(captures, prompt, page_url):
    """No requests: inspect only current-query streams or exact session history."""
    expected = normalized(prompt)
    session = re.search(r"/search/([^/?#]+)", urlparse(page_url).path)
    expected_session = session.group(1) if session else ""
    references = []
    if not expected:
        return [], []
    for capture in captures:
        try:
            parsed = urlparse(capture.get("url") or "")
        except (ValueError, TypeError):
            continue
        if parsed.hostname not in {"chat.baidu.com", "wenxin.baidu.com", "yiyan.baidu.com"}:
            continue
        if capture.get("status", 200) != 200:
            continue
        body = capture.get("body") or ""
        if not isinstance(body, str):
            continue
        if parsed.path == "/csaitab/history/list":
            # A history list can include twenty unrelated conversations.
            bound = yiyan_turn(body, prompt, page_url)
            if bound:
                for root in _roots(bound):
                    references.extend(_references(root))
            continue
        if parsed.path not in {"/aichat/api/conversation", "/chat/api/agent/task_mode", "/aichat/api/canvas/search"}:
            continue
        if capture.get("request_prompt_matched") is False:
            continue
        matched = capture.get("request_prompt_matched") is True
        observed_session = expected_session
        for root in _roots(body):
            if root.get("status") not in (0, None):
                continue
            data = root.get("data") if isinstance(root.get("data"), dict) else {}
            if "query" in root or "query" in data:
                matched = _query(root.get("query", data.get("query"))) == expected
            root_session = root.get("sessionId") or data.get("sessionId")
            if root_session:
                if observed_session and str(root_session) != observed_session:
                    matched = False
                elif matched:
                    observed_session = str(root_session)
            if matched:
                references.extend(_references(root))
    urls, details = [], []
    for ref in references:
        url = ref.get("url") or ref.get("link")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        try:
            valid = bool(urlparse(url).hostname) and not _is_noise_citation(url, "yiyan")
        except ValueError:
            valid = False
        if not valid:
            continue
        if url not in urls:
            urls.append(url)
        detail = {"url": url, "title": ref.get("title") or ref.get("text") or "",
                  "site_name": ref.get("site_name") or ref.get("source") or "",
                  "icon": ref.get("icon") or ""}
        details.append({key: value if isinstance(value, str) else "" for key, value in detail.items()})
    return urls, build_citation_details(urls, dom_details=details, max_fetch=0)


def enrich_yiyan_checkpoint(checkpoint, captures, page_url, diagnostic=None, request_evidence=()):
    """Keep the completed answer; improve references and search evidence only."""
    sources, hints = current_yiyan_sources(captures, checkpoint.get("prompt", ""), page_url)
    if not str(checkpoint.get("source", "")).startswith("dom:"):
        sources = list(dict.fromkeys(sources + list(checkpoint.get("citations") or [])))
    urls = list(dict.fromkeys(list(checkpoint.get("citations") or []) + sources))
    details = build_citation_details(urls, dom_details=hints + list(checkpoint.get("citation_details") or []), max_fetch=0)
    enriched = dict(checkpoint)
    enriched.update(citations=urls, citation_details=details,
                    citation_domains=sorted({urlparse(url).hostname for url in urls if urlparse(url).hostname}),
                    citations_status="available" if details else "missing")
    enriched.pop("web_search_error_code", None)
    enriched.update(web_search_evidence("yiyan", diagnostic, sources,
                    request_evidence or checkpoint.get("web_search_request_evidence") or []))
    issues = [issue for issue in checkpoint.get("quality_issues", []) if issue != "citations_missing"]
    if not details:
        issues.append("citations_missing")
    enriched["quality_issues"] = issues
    return enriched, sources
