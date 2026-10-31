# -*- coding: utf-8 -*-
"""单站抓取：CDP UI + 网络捕获 + profiling。"""
from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from playwright.sync_api import Browser, Page

from cn_ai.citation_details import build_citation_details
from cn_ai import budget
from cn_ai.protocol_completion import deepseek_turn, yuanbao_turn, yiyan_turn, normalized
from cn_ai.search_policy import outbound_prompt
from cn_ai.network_parser import (
    clean_answer_citation_markers,
    merge_capture_citation_details,
    render_qianwen_media,
    render_yuanbao_images,
    merge_captures,
    parse_deepseek_history,
    parse_yuanbao_detail,
)
from cn_ai.share_links import (
    create_public_share,
    is_public_share_url,
    share_answer_hint,
    verify_public_share,
)
from cn_ai.profiler import SiteProfiler
from cn_ai.diagnostics import current as current_diagnostics
from cn_ai.search_evidence import request_search_evidence, web_search_evidence
from cn_ai.yiyan_citations import enrich_yiyan_checkpoint
from cn_ai.site_flows import (
    PlatformFlowError,
    classify_platform_failure,
    close_site_targets,
    page_answers,
    SITE_URL_PATTERNS,
    _kimi_reasoning_trace,
    detect_platform_failure,
    extract_answer_and_links,
    send_prompt,
    wait_doubao_auth,
    wait_for_answer_growth,
)
from chinese_ai_sites import CHINESE_AI_SITES

GEO_DIR = Path(__file__).resolve().parent.parent
RESULT_ROOT = GEO_DIR / ".scrape_cache" / "cn_ai"


def retry_after_seconds(value, now=None):
    """Only retain a bounded delay, never the original response headers."""
    text = str(value or "").strip()
    if not text or len(text) > 128:
        return 0
    try:
        if re.fullmatch(r"[0-9]+", text):
            seconds = int(text)
        else:
            when = parsedate_to_datetime(text)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = when.timestamp() - (time.time() if now is None else now)
        return max(1, min(600, math.ceil(seconds))) if seconds > 0 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


class NetworkCapture:
    def __init__(self, site_id="", prompt=""):
        self.items: List[Dict[str, Any]] = []
        self._completed: List[Any] = []
        self.site_id, self.prompt = site_id, prompt
        self.submitted_prompt = prompt
        self.search_requests = []
        self.rate_limited = False
        self.retry_after_sec = 0
        self.on_prompt_sent = None
        self._yiyan_requests = {}

    def attach(self, page: Page) -> None:
        def on_request(request):
            if not self.site_id or request.method != 'POST':
                return
            try:
                evidence = request_search_evidence(self.site_id, request.url, request.post_data_json, self.prompt)
                if self.site_id == "yiyan" and urlparse(request.url).path == "/aichat/api/conversation":
                    self._yiyan_requests[getattr(request, "_impl_obj", request)] = bool(evidence and evidence.get("original_prompt_observed"))
                if evidence:
                    self.search_requests.append(evidence)
                    self.search_requests[:] = self.search_requests[-20:]
                    if (evidence.get("original_prompt_observed") is True or evidence.get("submitted_prompt_observed") is True) and self.on_prompt_sent:
                        self.on_prompt_sent()
            except Exception:
                pass
        page.on('request', on_request)
        def _interesting(url: str, mime: str) -> bool:
            if _is_noise_url(url):
                return False
            return any(
                k in url.lower()
                for k in [
                    "api", "chat", "completion", "message", "stream",
                    "conversation", "searchresult", "/im/", "apiv2",
                    "history_messages", "chain/single", "chain/recent",
                    "segment",
                ]
            ) or "json" in mime or "event-stream" in mime or "connect" in mime

        def on_request_finished(req):
            try:
                if req.resource_type in ("image", "font", "media"):
                    return
                res = req.response()
                if not res:
                    return
                path = urlparse(req.url).path.lower()
                host = (urlparse(req.url).hostname or "").lower()
                if (res.status == 429 and req.method == "POST" and (
                        self.site_id == "kimi" and (host == "kimi.com" or host.endswith(".kimi.com")) and "chatservice/chat" in path or
                        self.site_id == "yuanbao" and host == "yuanbao.tencent.com" and path.startswith("/api/chat/"))):
                    self.rate_limited = True
                    self.retry_after_sec = max(self.retry_after_sec, retry_after_seconds(res.headers.get("retry-after", "")))
                mime = res.headers.get("content-type", "")
                if not _interesting(req.url, mime):
                    return
                self._completed.append(res)
                # Sync Playwright callbacks can run while click/navigation
                # is still pending. Reading a body here nests another sync
                # operation in that action; collect it at the next flush.
            except BaseException:
                return

        def on_request_failed(req):
            try:
                if req.resource_type in ("image", "font", "media"):
                    return
                if not _interesting(req.url, ""):
                    return
                self.items.append({
                    "url": req.url,
                    "mime": "",
                    "status": 0,
                    "body": str(req.failure or "request_failed"),
                    "request_body": str(req.post_data or "")[:20000],
                    "ts": time.time(),
                })
            except BaseException:
                return

        page.on("requestfinished", on_request_finished)
        page.on("requestfailed", on_request_failed)

    def check_rejection(self):
        if self.rate_limited:
            error = PlatformFlowError("platform_rate_limited", "平台聊天接口明确返回HTTP 429，请稍后重试", False)
            error.retry_after_sec = self.retry_after_sec
            raise error

    def _ingest(self, res) -> None:
        url = res.url
        try:
            raw = res.body()
            body = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except BaseException:
            return
        diagnostic = current_diagnostics()
        if diagnostic:
            diagnostic.body(url, body, res.headers.get("content-type", ""), res.status)
        if len(body) < 10:
            return
        mime = res.headers.get("content-type", "")
        yiyan_stream = self.site_id == "yiyan" and urlparse(url).path == "/aichat/api/conversation"
        limit = 2000000 if any(key in url for key in (
            "history/list", "history_messages", "conversation/v1/detail", "chain/single", "ChatService/Chat", "/aichat/api/conversation"
        )) else 500000
        if yiyan_stream:
            request = res.request
            matched = self._yiyan_requests.get(getattr(request, "_impl_obj", request), False)
            # Different turns share this endpoint. Keep each finished response
            # with its own request binding, rather than the longest old stream.
            item = {"url": url, "mime": mime, "status": res.status,
                    "body": body[:limit], "ts": time.time()}
            item["request_prompt_matched"] = matched
            self.items.append(item)
            return
        # 同 URL 保留更长 body（流式结束后再读往往更完整）
        for i, it in enumerate(self.items):
            if it.get("url") == url:
                if len(body) > len(it.get("body") or ""):
                    self.items[i] = {
                        "url": url,
                        "mime": mime,
                        "status": res.status,
                        "body": body[:limit],
                        "ts": time.time(),
                    }
                return
        self.items.append({
            "url": url,
            "mime": mime,
            "status": res.status,
            "body": body[:limit],
            "ts": time.time(),
        })

    def flush(self) -> None:
        """Retry completed responses without waiting on long-lived streams."""
        completed = list(self._completed)
        self._completed.clear()
        for res in completed:
            if budget.remaining() <= 0:
                raise budget.StageDeadline("answer_extract_timeout")
            try:
                self._ingest(res)
            except BaseException:
                continue


def _is_noise_url(url: str) -> bool:
    u = url.lower()
    if any(u.endswith(ext) for ext in (".js", ".css", ".woff", ".woff2", ".png", ".jpg", ".svg", ".gif")):
        return True
    if "static/js" in u or "static/css" in u or "/obj/flow-doubao/" in u:
        return True
    return False


def find_page(browser: Browser, site_id: str) -> Optional[Page]:
    patterns = SITE_URL_PATTERNS.get(site_id, [])
    for ctx in browser.contexts:
        for page in ctx.pages:
            if any(p in page.url for p in patterns):
                return page
    return None


def _page_url(page: Page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def find_or_open_page(browser: Browser, site_id: str) -> Optional[Page]:
    home = CHINESE_AI_SITES[site_id].url
    patterns = SITE_URL_PATTERNS.get(site_id, [])
    ctx = browser.contexts[0] if browser.contexts else None
    if ctx is None:
        return None
    page = find_page(browser, site_id)
    if page is not None:
        url = _page_url(page)
        if url and "about:blank" not in url and not url.startswith("chrome://"):
            # AgentBay Chrome rejects a second tab with "Failed to open a
            # new tab". A slow renderer is still the page we should use.
            pages = list(getattr(ctx, "pages", []) or [])
            if page_answers(page) or len(pages) <= 1:
                return page
            close_site_targets(browser, site_id)
            page = None
    target = page
    if target is None:
        for item in ctx.pages:
            url = _page_url(item)
            if url in ("", "about:blank") or url.startswith("chrome://"):
                target = item
                break
    if target is None and ctx.pages:
        target = ctx.pages[0]
    if target is None:
        try:
            target = ctx.new_page()
        except Exception:
            if not ctx.pages:
                raise
            target = ctx.pages[0]
    current = _page_url(target)
    if not any(pattern in current for pattern in patterns):
        target.goto(home, wait_until="domcontentloaded", timeout=30000)
    return target


def _page_fetch_text(page: Page, url: str, timeout_ms: int = 8000) -> Optional[str]:
    timeout_ms = budget.timeout_ms(timeout_ms)
    if timeout_ms <= 0:
        return None
    try:
        return page.evaluate(
            """async ({url, timeout}) => {
            const r = await fetch(url, { credentials: 'include', signal: AbortSignal.timeout(timeout) });
            return await r.text();
        }""",
            {"url": url, "timeout": timeout_ms},
        )
    except Exception:
        return None


def _fetch_deepseek_history(page: Page, prompt: str = "") -> Optional[Dict[str, Any]]:
    m = re.search(r"/a/chat/s/([a-f0-9-]+)", page.url)
    if not m:
        return None
    sid = m.group(1)
    timeout_ms = budget.timeout_ms()
    if timeout_ms <= 0:
        return None
    try:
        body = page.evaluate(
            """async ({sid, timeout}) => {
            let tok = '';
            try {
              const raw = localStorage.getItem('userToken');
              if (raw) tok = JSON.parse(raw).value || '';
            } catch (e) {}
            const url = 'https://chat.deepseek.com/api/v0/chat/history_messages?chat_session_id=' + sid;
            const headers = {};
            if (tok) headers.Authorization = 'Bearer ' + tok;
            const r = await fetch(url, { credentials: 'include', headers, signal: AbortSignal.timeout(timeout) });
            return await r.text();
        }""",
            {"sid": sid, "timeout": timeout_ms},
        )
    except Exception:
        return None
    if not body:
        return None
    body = deepseek_turn(body, prompt)
    if not body:
        return None
    a, c = parse_deepseek_history(body, prompt=prompt)
    if a or c:
        return {
            "answer": a,
            "citations": c,
            "source": "api:history_messages",
            "raw_url": f"https://chat.deepseek.com/api/v0/chat/history_messages?chat_session_id={sid}",
            "body": body[:2000000],
            "complete": True,
            "prompt_matched": True,
        }
    return None


def _wait_deepseek_history(
    page: Page, prompt: str, timeout_sec: Optional[float] = None
) -> Optional[Dict[str, Any]]:
    if timeout_sec is None:
        timeout_sec = budget.wait_window(105, reserve=protocol_reserve())
    try:
        return budget.call(lambda: _poll_deepseek_history(page, prompt, timeout_sec), timeout_sec,
                           reserve=protocol_reserve(), code="generation_timeout")
    except budget.StageDeadline:
        return None


def _poll_deepseek_history(page, prompt, timeout_sec):
    deadline = time.time() + budget.remaining(timeout_sec, protocol_reserve())
    while time.time() < deadline:
        diagnostic = current_diagnostics()
        if diagnostic:
            diagnostic.checkpoint(page)
        detect_platform_failure(page, "deepseek")
        extra = _fetch_deepseek_history(page, prompt=prompt)
        if extra and extra.get("complete") and extra.get("prompt_matched") and extra.get("answer"):
            return extra
        budget.sleep(min(2, max(0, deadline - time.time())), protocol_reserve())
    return None


def _fetch_yiyan_history(page: Page, prompt: str = "") -> Optional[Dict[str, Any]]:
    """主动拉取文心 history/list（页面内 fetch，带 cookie）。"""
    from cn_ai.network_parser import parse_yiyan_history
    timeout_ms = budget.timeout_ms()
    if timeout_ms <= 0:
        return None

    try:
        body = page.evaluate(
            """async (timeout) => {
            // token 常见于当前页其它请求；退化用空 token 仍可能靠 cookie
            let token = '';
            try {
              const keys = Object.keys(localStorage);
              for (const k of keys) {
                const v = localStorage.getItem(k) || '';
                if (v.length > 20 && v.length < 200 && /^[A-Za-z0-9+/=|%]+$/.test(v)) {
                  // skip
                }
              }
              // 从 performance 资源里抠 token=
              const entries = performance.getEntriesByType('resource') || [];
              for (let i = entries.length - 1; i >= 0; i--) {
                const name = entries[i].name || '';
                const m = name.match(/[?&]token=([^&]+)/);
                if (m) { token = decodeURIComponent(m[1]); break; }
              }
            } catch (e) {}
            const url = 'https://chat.baidu.com/csaitab/history/list?rn=20&req_type=4&from=chat_search'
              + (token ? ('&token=' + encodeURIComponent(token)) : '');
            const r = await fetch(url, { credentials: 'include', signal: AbortSignal.timeout(timeout) });
            return await r.text();
        }""",
            timeout_ms,
        )
    except Exception:
        return None
    if not body or len(body) < 50:
        return None
    body = yiyan_turn(body, prompt, _page_url(page))
    if not body:
        return None
    a, c = parse_yiyan_history(body, prompt=prompt)
    if a or c:
        return {
            "answer": a,
            "citations": c,
            "source": "api:history/list",
            "raw_url": "https://chat.baidu.com/csaitab/history/list",
            "body": body[:2000000],
            "prompt_matched": True,
        }
    return None


def _yiyan_answer_ready(text: str, prompt: str = "") -> bool:
    """文心搜索会先吐改写/检索 JSON，那不算答完。"""
    t = (text or "").strip()
    if len(t) < 8:
        return False
    if t.startswith("需要重新规划检索") or '"rewrite_query"' in t[:400]:
        return False
    if (
        "大概需要3-5min" in t
        or "生成过程中你可以继续" in t
        or ("收到你的需求" in t and "联网搜索" in t)
    ):
        return False
    if t.startswith("{") and ("search" in t[:200] or "replan" in t[:200]):
        return False
    return not _looks_like_nav_junk(t, prompt)


def _current_prompt_matches(page, site_id, prompt):
    selectors = {"yiyan": ".conversation-flow-question-container .cs-question-pure-text", "yuanbao": '[data-conv-speaker="human"]'}
    selector = selectors.get(site_id)
    if not selector:
        return False
    try:
        text = page.locator(selector).last.inner_text(timeout=max(1, budget.timeout_ms(500)))
        return normalized(text) == normalized(prompt)
    except Exception:
        return False


def extraction_reserve():
    try:
        return max(5, min(25, int(os.getenv("GEO_WEB_EXTRACTION_RESERVE_SEC", "10"))))
    except ValueError:
        return 10


def protocol_reserve():
    # A completed protocol turn is checkpointed immediately; DOM/share are
    # optional, so only retain a short independent window for final cleanup.
    try:
        return max(2, min(15, int(os.getenv("GEO_PROTOCOL_FINALIZE_RESERVE_SEC", "5"))))
    except ValueError:
        return 5


def generation_reserve(site_id):
    return protocol_reserve() if site_id in ("deepseek", "yiyan") else extraction_reserve()


def _wait_yiyan_history(page: Page, prompt: str, timeout_sec: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if timeout_sec is None:
        timeout_sec = budget.wait_window(180, reserve=protocol_reserve())
    try:
        return budget.call(lambda: _poll_yiyan_history(page, prompt, timeout_sec), timeout_sec,
                           reserve=protocol_reserve(), code="generation_timeout")
    except budget.StageDeadline:
        return None


def _yiyan_standard_answer_candidate(page, prompt):
    """Read one current ordinary reply atomically; thinking alone is not terminal."""
    try:
        candidate = page.locator("html").evaluate(
            r"""(root, prompt) => {
              const norm = value => (value || '').replace(/\s+/g, ' ').trim();
              const rendered = el => {
                if (!el || !el.getClientRects().length) return false;
                for (let n = el; n; n = n.parentElement) {
                  const style = getComputedStyle(n);
                  if (style.display === 'none' || style.visibility === 'hidden') return false;
                }
                return true;
              };
              const questions = [...root.querySelectorAll(
                '.conversation-flow-question-container .cs-question-pure-text'
              )];
              const question = questions[questions.length - 1];
              if (!rendered(question) || !norm(prompt) || norm(question.innerText) !== norm(prompt)) return null;
              const qa = question.closest('.chat-qa-container');
              if (!qa || qa.querySelectorAll('.conversation-flow-question-container').length !== 1) return null;
              const replies = [...qa.querySelectorAll('.conversation-flow-answer-container')]
                .filter(el => el.closest('.chat-qa-container') === qa);
              if (replies.length !== 1 || !(question.compareDocumentPosition(replies[0]) & Node.DOCUMENT_POSITION_FOLLOWING)) return null;
              const items = [...replies[0].querySelectorAll('.chat-search-answer-generate-item')]
                .filter(rendered);
              if (items.length !== 1) return null;
              const item = items[0];
              const active = item.querySelectorAll(
                '[class*="_answer-generating_"],[class*="_thinking-loading_"],'
                + '[aria-busy="true"],[data-generating="true"],[data-status="generating"]'
              );
              if ([...active].some(rendered)) return null;
              const stopping = [...root.querySelectorAll('button,[role="button"]')].some(el =>
                rendered(el) && /^(停止生成|停止回答|停止输出|停止思考|Stop generating|Stop response)$/i
                  .test(norm(el.getAttribute('aria-label') || el.innerText)));
              if (stopping) return null;
              // This menu belongs to the finished reply, not its question or
              // thinking panel. Hover opacity does not change its final state.
              const menus = [...item.querySelectorAll(
                '.answer-menu .cs-answer-hover-menu-container[class*="_generate-menu_"]'
              )].filter(rendered);
              const enabled = icon => icon && !icon.closest('[disabled],[aria-disabled="true"]');
              if (!menus.some(menu => enabled(menu.querySelector('.cos-icon-copy'))
                  && enabled(menu.querySelector('.cos-icon-share1')))) return null;
              // A finished reply can have several formal markdown blocks
              // separated by business cards. Keep only this reply's text blocks.
              const matched = [...item.querySelectorAll(
                '.answer-container .ai-entry-block.ai-markdown .marklang, '
                + '.ai-entry[class*="_task-detail-answer_"] .marklang'
              )].filter(el => rendered(el) && !el.closest(
                '.ai-thinking-steps,[class*="_thinking-steps_"],.ai-biz-block'
              ));
              // Nested markdown wrappers must not duplicate the same text.
              const bodies = matched.filter(el => !matched.some(other => other !== el && other.contains(el)));
              if (!bodies.length) return null;
              if (bodies.length > 1) {
                const entry = bodies[0].closest('.ai-entry');
                const container = bodies[0].closest('.answer-container');
                if (!entry || !bodies.every(el => el.closest('.ai-entry') === entry
                    && el.closest('.answer-container') === container)) return null;
              }
              const seen = new Set(), parts = [];
              for (const body of bodies) {
                const text = (body.innerText || '').trim(), identity = norm(text);
                if (identity && !seen.has(identity)) { seen.add(identity); parts.push(text); }
              }
              const answer = parts.join('\n\n');
              if (answer.length <= 20) return null;
              const citations = [...new Set(bodies.flatMap(body => [...body.querySelectorAll('a[href]')])
                .map(a => a.href).filter(url => /^https?:\/\//i.test(url)))];
              return {answer, citations, turn: location.pathname + '|' + questions.length};
            }""",
            prompt,
            timeout=max(1, budget.timeout_ms(1500, reserve=protocol_reserve())),
        )
    except Exception:
        return None
    if not candidate or not _yiyan_answer_ready(candidate.get("answer") or "", prompt):
        return None
    return candidate


def _poll_yiyan_history(page, prompt, timeout_sec):
    """文心页面一直在刷搜索结果，DOM 稳定判定会空等到超时。改轮询 history。"""
    deadline = time.time() + budget.remaining(timeout_sec, protocol_reserve())
    last: Optional[Dict[str, Any]] = None
    opened_task = False
    standard_key = None
    standard_since = 0.0
    while time.time() < deadline:
        diagnostic = current_diagnostics()
        if diagnostic:
            diagnostic.checkpoint(page)
        detect_platform_failure(page, "yiyan")
        current_turn = _current_prompt_matches(page, "yiyan", prompt)
        standard = _yiyan_standard_answer_candidate(page, prompt) if current_turn else None
        if standard:
            key = (standard.get("turn"), standard["answer"])
            if key == standard_key and time.time() - standard_since >= 1:
                return {"answer":standard["answer"], "citations":standard.get("citations") or [],
                        "source":"dom:yiyan-standard", "complete":True, "prompt_matched":True}
            if key != standard_key:
                standard_key, standard_since = key, time.time()
        else:
            standard_key = None
        if opened_task and current_turn:
            dom = extract_answer_and_links(page, "yiyan")
            if dom.get("selector") == "yiyan-answer" and _yiyan_answer_ready(dom.get("text") or "", prompt):
                return {"answer":dom["text"], "citations":dom.get("links") or [], "source":"dom:yiyan-task", "complete": True, "prompt_matched": True}
        extra = _fetch_yiyan_history(page, prompt=prompt)
        if extra:
            last = extra
        if not opened_task:
            try:
                completed = page.locator('[data-show-ext*="task_card"]').filter(has_text="任务已完成").last
                if not completed.count():
                    completed = page.get_by_text("任务已完成", exact=True).last
                if completed.count() > 0 and completed.is_visible() and current_turn:
                    if extra and extra.get("prompt_matched") and _yiyan_answer_ready(extra.get("answer") or "", prompt):
                        extra["complete"] = True
                        return extra
                    completed.click(timeout=3000, force=True)
                    opened_task = True
                    budget.sleep(min(3, max(0, deadline-time.time())), protocol_reserve())
                    continue
            except Exception:
                pass
        budget.sleep(min(3, max(0, deadline-time.time())), protocol_reserve())
    return None


def _fetch_yuanbao_detail(page: Page, prompt: str = "") -> Optional[Dict[str, Any]]:
    # URL 形态：/chat/{agentId}/{conversationId} 或 /chat/{conversationId}
    m = re.search(r"/chat/(?:[A-Za-z0-9]+/)?([A-Za-z0-9]+)/?$", page.url)
    if not m:
        return None
    cid = m.group(1)
    timeout_ms = budget.timeout_ms()
    if timeout_ms <= 0:
        return None
    api = "https://yuanbao.tencent.com/api/user/agent/conversation/v1/detail"
    try:
        body = page.evaluate(
            """async ({ url, cid, timeout }) => {
            const r = await fetch(url, {
              method: 'POST',
              credentials: 'include',
              signal: AbortSignal.timeout(timeout),
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ conversationId: cid }),
            });
            return await r.text();
        }""",
            {"url": api, "cid": cid, "timeout": timeout_ms},
        )
    except Exception:
        return None
    if not body:
        return None
    body = yuanbao_turn(body, prompt, _page_url(page))
    if not body:
        return None
    a, c = parse_yuanbao_detail(body)
    if a or c:
        return {
            "answer": a,
            "citations": c,
            "source": "api:conversation_detail",
            "raw_url": api,
            "body": body[:2000000],
            "prompt_matched": True,
        }
    return None


def _fetch_doubao_from_page_state(page: Page) -> Optional[Dict[str, Any]]:
    """从页面内 IM 状态 / 消息列表 JSON 提取回答与引用。"""
    try:
        raw = page.evaluate(
            """() => {
            const out = { texts: [], urls: [] };
            const walk = (obj, depth) => {
              if (!obj || depth > 14) return;
              if (typeof obj === 'string') {
                if (obj.startsWith('http')) out.urls.push(obj);
                else if (obj.length > 80) out.texts.push(obj);
                return;
              }
              if (Array.isArray(obj)) {
                obj.forEach(x => walk(x, depth + 1));
                return;
              }
              if (typeof obj === 'object') {
                if (obj.search_references && Array.isArray(obj.search_references)) {
                  obj.search_references.forEach(r => {
                    if (r && r.url) out.urls.push(r.url);
                  });
                }
                if (obj.text_block && obj.text_block.text) out.texts.push(obj.text_block.text);
                for (const k of Object.keys(obj)) walk(obj[k], depth + 1);
              }
            };
            try {
              const rd = window._ROUTER_DATA;
              walk(rd, 0);
            } catch (e) {}
            document.querySelectorAll('[data-plugin-identifier*="search"]').forEach(el => {
              out.texts.push((el.innerText || '').slice(0, 5000));
            });
            return out;
        }"""
        )
    except Exception:
        return None
    if not raw:
        return None
    texts = raw.get("texts") or []
    urls = _uniq_urls(raw.get("urls") or [])
    answer = max(texts, key=len) if texts else ""
    if answer or urls:
        return {"answer": answer, "citations": urls, "source": "page:state"}
    return None


def _uniq_urls(urls: List[str]) -> List[str]:
    seen = set()
    out = []
    for u in urls:
        u = (u or "").strip()
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _completed_protocol_checkpoint(site_id, prompt, question_id, extra, capture, t0):
    if not extra or not extra.get("complete") or not extra.get("prompt_matched"):
        return None
    answer = extra.get("answer") or ""
    failure = classify_platform_failure(site_id, answer)
    if failure:
        raise PlatformFlowError(*failure)
    # A renderer's innerText drops list numbering and heading markup. The
    # generic short-line heuristic cannot overrule Qianwen's exact, completed
    # answer container after its question, sources and stability were verified.
    verified_native = (extra.get('native_answer_verified') is True
                       and str(extra.get('source', '')).startswith('dom:qianwen-completed'))
    if not answer or (_looks_like_nav_junk(answer, prompt) and not verified_native):
        return None
    images = []
    if site_id == "yuanbao":
        answer, images = render_yuanbao_images(answer, [extra.get("body") or ""])
    raw = answer
    answer = clean_answer_citation_markers(raw)
    citations = _uniq_urls(extra.get("citations") or [])
    hints = (extra.get('citation_details') or []) + merge_capture_citation_details([{"url": extra.get("raw_url", ""), "body": extra.get("body", "")}], site_id)
    details = build_citation_details(citations, dom_details=hints, max_fetch=0)
    checkpoint = {
        "web_answer_completed": True, "ok": True, "site_id": site_id,
        "site_name": CHINESE_AI_SITES[site_id].name, "question_id": question_id, "prompt": prompt,
        "answer": answer, "answer_raw": raw, "answer_origin": "web", "source": extra.get("source", ""),
        "citations": citations, "citation_details": details,
        "citation_domains": list({urlparse_domain(u) for u in citations}), "url": "",
        "share_status": "generation_failed", "share_error_code": "share_timeout",
        "citations_status": "available" if details else "missing", "quality_issues": ["share_generation_failed"],
        "elapsed_ms": int((time.perf_counter()-t0)*1000),
    }
    if images:
        checkpoint["images"] = images
    network_sources = [] if site_id == "yiyan" and str(extra.get("source", "")).startswith("dom:") else citations
    checkpoint.update(web_search_evidence(site_id, current_diagnostics(), network_sources, capture.search_requests))
    checkpoint['submitted_prompt'] = getattr(capture, 'submitted_prompt', prompt)
    return checkpoint


def _keep_completed_checkpoint(checkpoint, run_dir, profiler, code):
    if profiler.steps and code.endswith("timeout"):
        profiler.steps[-1].ok = False
    result = dict(checkpoint)
    result.pop("web_answer_completed", None)
    result["web_answer_recovered"] = True
    result["optional_stage_error_code"] = code
    if code.startswith("answer_extract"):
        result["share_skipped"] = True
        result["share_skip_reason"] = code
        result["share_error_code"] = "share_not_attempted"
    result["steps"] = _step_timings(profiler)
    result["timestamp"] = datetime.now().isoformat()
    result["run_dir"] = str(run_dir)
    diagnostic = current_diagnostics()
    if diagnostic:
        diagnostic.keep_evidence = True
        diagnostic.meta.update(quality_issues=[code], optional_stage_error_code=code)
        diagnostic.persist()
    (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def scrape_one(
    browser: Browser,
    site_id: str,
    prompt: str,
    question_id: str = "",
    out_dir: Optional[Path] = None,
    recovery_action: str = "",
) -> Dict[str, Any]:
    diagnostic = current_diagnostics()
    try:
        if diagnostic is None:
            return _scrape_one(browser, site_id, prompt, question_id, out_dir, recovery_action)
        diagnostic.bind_browser(browser)
        with diagnostic.guard():
            result = _scrape_one(browser, site_id, prompt, question_id, out_dir, recovery_action)
            if not result.get("ok"):
                diagnostic.meta["error_code"] = result.get("error_code", "")
                diagnostic.fail(result.get("error") or "No valid answer")
            elif diagnostic.should_sample():
                diagnostic.checkpoint(force=True, screenshot=True)
            if diagnostic.failed or diagnostic.keep_evidence or diagnostic.should_sample():
                result["diagnostic_id"] = diagnostic.path.name
            return result
    except BaseException as error:
        from cn_ai.web_recovery import RecoveryJournal
        journal = RecoveryJournal.from_env(site_id, prompt, question_id)
        if journal:
            journal.failure(error)
        raise


def _scrape_one(
    browser: Browser, site_id: str, prompt: str, question_id: str = "", out_dir: Optional[Path] = None,
    recovery_action: str = "",
) -> Dict[str, Any]:
    from cn_ai.web_recovery import RecoveryJournal, probe, snapshot
    journal = RecoveryJournal.from_env(site_id, prompt, question_id, create=not recovery_action)
    resume_sent = False
    if journal and recovery_action:
        checked = budget.call(lambda: probe(browser, journal), 25, code="recovery_unconfirmed")
        if not checked["ready"]:
            raise PlatformFlowError(checked.get("error_code", "recovery_unconfirmed"), "原问题页面尚未通过恢复复核："+checked["reason"], False)
        resume_sent = journal.data["send_state"] == "sent"
    elif journal and journal.data["send_state"] != "before_send":
        raise PlatformFlowError("recovery_unconfirmed", "问题可能已发送，必须通过恢复流程续读原页面", False)
    site = CHINESE_AI_SITES[site_id]
    run_dir = out_dir or (RESULT_ROOT / site_id / datetime.now().strftime("%Y%m%d_%H%M%S"))
    run_dir.mkdir(parents=True, exist_ok=True)
    # scrape 时禁用 profiler body，避免与 NetworkCapture 双重 res.body() 竞态
    profiler = SiteProfiler(site_id, run_dir, capture_bodies=False)
    capture = NetworkCapture(site_id, prompt)
    if journal and resume_sent:
        capture.submitted_prompt = journal.data.get('submitted_prompt', prompt)

    t0 = time.perf_counter()
    try:
        page = budget.call(lambda: journal.find(browser) if recovery_action and journal else find_or_open_page(browser, site_id), 30, reserve=generation_reserve(site_id), code="generation_timeout")
    except budget.StageDeadline:
        raise PlatformFlowError("generation_timeout", "浏览器初始化预算耗尽，未确认发送问题", False)
    if not page:
        return {
            "site_id": site_id,
            "question_id": question_id,
            "prompt": prompt,
            "error": "tab_not_found",
            "ok": False,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }

    if journal:
        journal.bind(page)
        def observed_submission():
            if journal.data["send_state"] == "sending":
                # An outgoing request alone does not prove a new accepted
                # conversation turn. Keep it uncertain until the UI agrees.
                journal.data["submission_observed"] = True
                journal.save()
        capture.on_prompt_sent = observed_submission
        if recovery_action and journal.data.get("checkpoint"):
            saved = journal.data["checkpoint"]
            if (saved.get("web_answer_completed") is True and saved.get("site_id") == site_id
                    and saved.get("question_id") == question_id and saved.get("prompt") == prompt
                    and not (site_id == 'qianwen' and saved.get('web_search_status') != 'used')):
                return _keep_completed_checkpoint(saved, run_dir, profiler, "share_not_attempted")

    profiler.attach_page(page)
    capture.attach(page)
    diagnostic = current_diagnostics()
    if diagnostic:
        diagnostic.bind_page(page)
        diagnostic.checkpoint(page, force=True)

    profiler.step("00_url", lambda: page.url, save_dom=False)
    if _page_logged_out(page, site_id):
        return {
            "site_id": site_id,
            "site_name": site.name,
            "question_id": question_id,
            "prompt": prompt,
            "error": "logged_out",
            "error_code": "platform_auth_unavailable",
            "retryable": True,
            "ok": False,
            "url": "",
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "steps": _step_timings(profiler),
            "timestamp": datetime.now().isoformat(),
        }

    if journal:
        observed = snapshot(page, site_id)
        if observed.get("account_marker") and not observed.get("logged_out"):
            journal.data["prior_logged_in"] = True
            journal.save()

    def do_send():
        if resume_sent:
            return "resume:sent"
        capture.submitted_prompt = outbound_prompt(site_id, prompt)
        kwargs = {"new_chat": not recovery_action}
        if journal:
            def on_state(state):
                if state == 'sending':
                    journal.data['submitted_prompt'] = capture.submitted_prompt
                journal.state(state, page)
            kwargs["on_send_state"] = on_state
        return send_prompt(page, prompt, site_id, **kwargs)

    try:
        profiler.step("01_send", lambda: budget.call(do_send, 60, reserve=generation_reserve(site_id), code="send_timeout"), save_dom=False)
        if journal and journal.data["send_state"] == "sending" and journal.matches_sent_turn(page, snapshot(page, site_id)):
            journal.state("sent", page)
    except budget.StageDeadline:
        if profiler.steps:
            profiler.steps[-1].ok = False
        raise PlatformFlowError("send_timeout", "发送阶段超时，保留原页面核实，未重复提交问题", False)
    deepseek_early: Dict[str, Any] = {}
    yiyan_early: Dict[str, Any] = {}
    yuanbao_early: Dict[str, Any] = {}
    qianwen_early: Dict[str, Any] = {}
    web_answer_completed = False
    early_checkpoint = None
    yiyan_network_citations = []

    def confirmed_completion(_kind):
        nonlocal web_answer_completed
        if journal and journal.data["send_state"] != "sent":
            if journal.matches_sent_turn(page, snapshot(page, site_id)):
                journal.state("sent", page)
            else:
                return False
        web_answer_completed = site_id not in ("deepseek", "yiyan", "yuanbao") or _kind in ("history", "dom_state")
        return web_answer_completed

    def bind_protocol_turn(extra):
        if not journal or journal.data["send_state"] == "sent":
            return True
        if journal.matches_sent_turn(page, snapshot(page, site_id)):
            journal.state("sent", page)
            return True
        return False

    def commit_protocol(extra):
        nonlocal early_checkpoint
        early_checkpoint = _completed_protocol_checkpoint(site_id, prompt, question_id, extra, capture, t0)
        if early_checkpoint:
            if journal:
                journal.checkpoint(early_checkpoint)
            print(json.dumps(early_checkpoint, ensure_ascii=False), flush=True)

    def enrich_checkpoint():
        nonlocal early_checkpoint, yiyan_network_citations
        if site_id != "yiyan" or not early_checkpoint:
            return
        enriched, yiyan_network_citations = enrich_yiyan_checkpoint(
            early_checkpoint, capture.items, _page_url(page), diagnostic, capture.search_requests)
        if enriched != early_checkpoint:
            early_checkpoint = enriched
            if journal:
                journal.checkpoint(early_checkpoint)
            print(json.dumps(early_checkpoint, ensure_ascii=False), flush=True)

    def do_wait():
        if site_id == 'qianwen':
            from cn_ai.qianwen_completion import wait_completed
            if resume_sent:
                from cn_ai.site_flows import enable_web_search
                enable_web_search(page, 'qianwen', activate=False)
            extra = wait_completed(page, prompt)
            if extra and bind_protocol_turn(extra):
                qianwen_early.update(extra)
                confirmed_completion('dom_state')
                commit_protocol(extra)
                return 'completed-native-answer:' + str(len(extra['answer']))
        if site_id == "deepseek":
            extra = _wait_deepseek_history(page, prompt)
            if extra and extra.get("complete") and extra.get("prompt_matched") and bind_protocol_turn(extra):
                deepseek_early.update(extra)
                confirmed_completion("history")
                commit_protocol(extra)
                return f"history:{len(extra.get('answer') or '')}"
            return "history:empty"
        if site_id == "yiyan":
            extra = _wait_yiyan_history(page, prompt)
            if extra and extra.get("complete") and extra.get("prompt_matched") and bind_protocol_turn(extra):
                yiyan_early.update(extra)
                confirmed_completion("history")
                commit_protocol(extra)
                ans = extra.get("answer") or ""
                return f"history:{len(ans)} ready={_yiyan_answer_ready(ans, prompt)}"
            return "history:empty"
        # The Go caller supplies a fresh deadline for each actual attempt.
        # Local 120/180s defaults must not shorten a configured longer attempt,
        # including when diagnostic capture is disabled.
        wait_budget = budget.wait_window(120, reserve=extraction_reserve())
        return wait_for_answer_growth(
            page,
            timeout_sec=wait_budget,
            site_id=site_id,
            resource_urls=lambda: [record.url for record in profiler.network[-150:]],
            on_completed=confirmed_completion,
            failure_check=getattr(capture, "check_rejection", None),
        )

    try:
        wait_budget = budget.wait_window(180, reserve=generation_reserve(site_id))
        profiler.step("02_wait", lambda: budget.call(do_wait, wait_budget, reserve=generation_reserve(site_id), code="generation_timeout"), save_dom=False)
    except budget.StageDeadline:
        if profiler.steps:
            profiler.steps[-1].ok = False
        raise PlatformFlowError("generation_timeout", "已发送问题，但在生成预算内未确认完整回答", False)
    if not web_answer_completed:
        raise PlatformFlowError("generation_timeout", "已发送问题，但尚未确认当前问题的完整回答", False)
    if site_id == "yuanbao":
        try:
            extra = budget.call(lambda: _fetch_yuanbao_detail(page, prompt=prompt), 8, reserve=2, code="answer_extract_timeout")
        except budget.StageDeadline:
            raise PlatformFlowError("answer_extract_timeout", "网页回答已结束，但正文提取超时", False)
        if extra and extra.get("prompt_matched"):
            yuanbao_early.update(extra)
            yuanbao_early["complete"] = web_answer_completed
        elif not _current_prompt_matches(page, "yuanbao", prompt):
            raise PlatformFlowError("generation_timeout", "未确认当前问题与已完成回答属于同一对话", False)
    if not early_checkpoint:
        commit_protocol(deepseek_early or yiyan_early or yuanbao_early)

    def collect_dom():
        capture.flush()
        enrich_checkpoint()
        if site_id == "doubao" and not _doubao_answer_landed(capture):
            budget.sleep(4)
            capture.flush()
        elif site_id != "doubao" and not early_checkpoint:
            budget.sleep(2)
            capture.flush()
        if site_id == "kimi":
            for sel in ["text=引用", "text=参考", "text=网页", "text=资料来源", "[class*='cite']"]:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(timeout=2000)
                        budget.sleep(1)
                        break
                except Exception:
                    pass
        data = extract_answer_and_links(page, site_id)
        if site_id == "yiyan":
            capture.flush()
            enrich_checkpoint()
        return data

    optional_extract_error = ""
    try:
        dom_data = profiler.step("03_dom_extract", lambda: budget.call(collect_dom, budget.seconds("GEO_DOM_EXTRACT_BUDGET_SEC", 10), reserve=2, code="answer_extract_timeout"), save_dom=False)
    except budget.StageDeadline:
        if early_checkpoint:
            optional_extract_error = "answer_extract_timeout"
        else:
            if profiler.steps:
                profiler.steps[-1].ok = False
            raise PlatformFlowError("answer_extract_timeout", "网页正文提取超时，未取得可验证的完整回答", False)
    except Exception:
        if early_checkpoint:
            optional_extract_error = "answer_extract_failed"
        else:
            raise
    if optional_extract_error:
        if budget.remaining(reserve=2) < 4:
            return _keep_completed_checkpoint(early_checkpoint, run_dir, profiler, optional_extract_error)
        dom_data = {"text": early_checkpoint["answer"], "links": early_checkpoint.get("citations") or [],
                    "link_details": early_checkpoint.get("citation_details") or [], "selector": "completed-checkpoint"}
    if site_id == "yuanbao" and not early_checkpoint and dom_data.get("selector") != "yuanbao-answer-finished":
        raise PlatformFlowError("answer_extract_timeout", "未取得当前元宝对话中已结束的完整回答", False)
    (run_dir / "dom_extracted.json").write_text(
        json.dumps(dom_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    api_extra = None
    if site_id == "deepseek":
        api_extra = deepseek_early or _fetch_deepseek_history(page, prompt=prompt)
    elif site_id == "yiyan":
        api_extra = yiyan_early or _fetch_yiyan_history(page, prompt=prompt)
    elif site_id == "yuanbao":
        api_extra = yuanbao_early or None
    elif site_id == 'qianwen':
        api_extra = qianwen_early or None
    # 豆包：只信 chain/single + completion

    if api_extra and api_extra.get("body"):
        capture.items.append({
            "url": api_extra.get("raw_url", "page:fetch"),
            "mime": "application/json",
            "status": 200,
            "body": api_extra["body"],
            "ts": time.time(),
        })

    # 写有效 JSON：关键接口放宽截断，避免文心 history 半截 JSON
    slim = []
    for item in capture.items:
        url = item.get("url") or ""
        limit = 200000
        if any(k in url for k in ("history/list", "history_messages", "chain/single", "/chat/completion", "conversation/v1/detail", "/api/chat/", "ChatService/Chat", "/aichat/api/conversation")):
            limit = 2000000
        slim.append({
            **item,
            "body": (item.get("body") or "")[:limit],
        })
    (run_dir / "network_captures.json").write_text(
        json.dumps(slim, ensure_ascii=False), encoding="utf-8"
    )

    answer, citations, source = merge_captures(capture.items, site_id, prompt=prompt)
    if site_id == "yuanbao" and not yuanbao_early:
        # An unmatched detail response may be from an older tab/request. Only
        # use the current finished assistant DOM in this fallback path.
        answer, citations, source = "", [], "dom:yuanbao-answer-finished"
    network_citations = list(citations)

    # 协议优先：元宝 detail / 文心 history；DeepSeek 仅在 history 按 prompt 命中且不更短时覆盖
    if early_checkpoint:
        answer, citations, source = early_checkpoint["answer"], early_checkpoint["citations"], early_checkpoint["source"]
        network_citations = list(yiyan_network_citations) if site_id == "yiyan" else list(citations)
    elif site_id in ("yiyan", "yuanbao") and api_extra and api_extra.get("answer"):
        answer = api_extra["answer"]
        if api_extra.get("citations"):
            citations = api_extra["citations"]
        source = api_extra.get("source", source)
    elif site_id == "deepseek" and api_extra and api_extra.get("answer"):
        ha = api_extra["answer"]
        hc = api_extra.get("citations") or []
        if len(ha) >= max(80, int(len(answer or "") * 0.85)):
            answer = ha
            source = api_extra.get("source", source)
            if hc:
                citations = hc
        elif len(hc) > len(citations):
            citations = hc
    elif api_extra:
        ha = api_extra.get("answer", "")
        hc = api_extra.get("citations") or []
        if len(ha) >= len(answer):
            answer = ha or answer
            source = api_extra.get("source", source)
        if len(hc) > len(citations):
            citations = hc
            if source.startswith("dom"):
                source = api_extra.get("source", source)

    if api_extra and not str(api_extra.get("source", "")).startswith("dom"):
        network_citations = list(dict.fromkeys(network_citations + (api_extra.get("citations") or [])))

    # A quota refusal can arrive as a normal conversation/detail answer.
    # Stop before DOM fallback or optional sharing can disguise it as success.
    if site_id == "yuanbao":
        failure = classify_platform_failure(site_id, answer or "")
        if failure:
            raise PlatformFlowError(*failure)

    # DOM 回退：必须像回答（排除侧栏历史标题堆）
    if not early_checkpoint and (not answer or len(answer) < 80 or _looks_like_nav_junk(answer, prompt)) and dom_data.get("text"):
        text = dom_data["text"]
        if site_id == "deepseek" and len(text) > 40 and not _looks_like_nav_junk(text, prompt):
            answer = text
            source = f"dom:{dom_data.get('selector', 'markdown')}"
        elif not _looks_like_nav_junk(text, prompt):
            idx = text.find(prompt)
            if idx >= 0:
                answer = text[idx + len(prompt):].strip()
            else:
                parts = text.split(prompt)
                answer = parts[-1].strip() if len(parts) > 1 else text[-8000:]
            source = "dom:text"

    images: List[Dict[str, Any]] = []
    if site_id == "yuanbao" and answer:
        bodies = [
            item.get("body") or ""
            for item in capture.items
            if any(
                key in (item.get("url") or "")
                for key in ("conversation/v1/detail", "/api/chat/")
            )
        ]
        answer, images = render_yuanbao_images(answer, bodies)

    if site_id == "qianwen" and answer:
        bodies = [
            item.get("body") or ""
            for item in capture.items
            if "/api/v2/chat" in (item.get("url") or "")
        ]
        answer, qianwen_images = render_qianwen_media(answer, bodies)
        if qianwen_images:
            images = qianwen_images

    # A stable Kimi tool-call scratchpad is not the answer and has no citations.
    if site_id == "kimi" and _kimi_reasoning_trace(answer or ""):
        answer = ""
        citations = []
        source = "kimi:reasoning"

    if not early_checkpoint and not citations and dom_data.get("links"):
        citations = [
            u for u in dom_data["links"]
            if not any(
                x in u
                for x in (
                    "deepseek.com",
                    "fengkongcloud",
                    "volces.com",
                    "kimi.com",
                    "kimi.ai",
                    "icon-cache",
                    "moonshot",
                    "aisearch.cdn.bcebos.com/homepage/privacy",
                )
            )
        ]
        if citations and source.startswith("dom"):
            source = "dom:links"

    if site_id == "doubao":
        inline_urls = extract_answer_urls(answer or "")
        if inline_urls and not any(not _weak_citation(u) for u in citations):
            citations = inline_urls
            if source.startswith("dom"):
                source = "answer:urls"

    platform_citation_details = (
        early_checkpoint.get("citation_details", []) if site_id == "yiyan" and early_checkpoint
        else merge_capture_citation_details(capture.items, site_id))
    citation_details = build_citation_details(
        citations,
        dom_details=platform_citation_details + (dom_data.get("link_details") or []),
        max_fetch=0,
    )
    answer_raw = answer
    answer = clean_answer_citation_markers(answer_raw)
    if site_id == "yuanbao":
        failure = classify_platform_failure(site_id, answer or "")
        if failure:
            raise PlatformFlowError(*failure)
    search_evidence = web_search_evidence(site_id, diagnostic, network_citations, capture.search_requests)
    search_evidence['submitted_prompt'] = capture.submitted_prompt
    # Commit the completed answer before optional share work. The Go runner
    # can retain it if a share gesture hangs or the browser budget expires.
    # This checkpoint is emitted only after completion detection/extraction,
    # and must match this exact site, question ID and prompt at the caller.
    completed_checkpoint = early_checkpoint
    if web_answer_completed and answer and not _looks_like_nav_junk(answer, prompt) and not early_checkpoint:
        checkpoint = {
            "web_answer_completed": True, "ok": True,
            "site_id": site_id, "site_name": site.name,
            "question_id": question_id, "prompt": prompt,
            "answer": answer, "answer_raw": answer_raw,
            "citations": citations, "citation_details": citation_details,
            "citation_domains": list({urlparse_domain(u) for u in citations if u}),
            "source": source, "answer_origin": "web", "url": "",
            "share_status": "generation_failed", "share_error_code": "share_timeout",
            "citations_status": "available" if citation_details else "missing",
            "quality_issues": ["share_generation_failed"] + ([] if citation_details else ["citations_missing"]),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }
        if images:
            checkpoint["images"] = images
        checkpoint.update(search_evidence)
        if journal:
            journal.checkpoint(checkpoint)
        print(json.dumps(checkpoint, ensure_ascii=False), flush=True)
        completed_checkpoint = checkpoint
    # Only expose a public share URL after an unauthenticated open check.
    # A normal chat URL requires the account session and must never be returned.
    result_url = ""
    try:
        share = profiler.step("04_share_create", lambda: budget.call(lambda: create_public_share(page, site_id), budget.seconds("GEO_SHARE_CREATE_BUDGET_SEC", 20), reserve=2, code="share_timeout"), save_dom=False)
    except budget.StageDeadline:
        if completed_checkpoint:
            return _keep_completed_checkpoint(completed_checkpoint, run_dir, profiler, "share_timeout")
        raise PlatformFlowError("answer_extract_timeout", "未取得可验证的完整回答", False)
    except Exception:
        if completed_checkpoint:
            return _keep_completed_checkpoint(completed_checkpoint, run_dir, profiler, "share_generation_failed")
        raise
    verify = {"ok": False}
    # Keep the URL observed from the real share gesture in the private run
    # evidence before an anonymous probe can time out. It is not a public
    # result until that probe verifies the corresponding answer.
    (run_dir / "share_response.json").write_text(
        json.dumps({"share": share, "verify": verify}, ensure_ascii=False, indent=2), encoding="utf-8")
    if share.get("url"):
        verify_answer = share_answer_hint(site_id, share, answer)
        try:
            verify = profiler.step("05_share_verify", lambda: budget.call(lambda: verify_public_share(
                browser, site_id, str(share["url"]), verify_answer,
            ), 25, reserve=2, code="share_timeout"), save_dom=False)
        except (budget.StageDeadline, Exception):
            if completed_checkpoint:
                return _keep_completed_checkpoint(completed_checkpoint, run_dir, profiler, "share_timeout")
            raise
        if verify.get("ok"):
            verified_final = str(verify.get("final_url") or "")
            result_url = (
                verified_final
                if is_public_share_url(site_id, verified_final)
                else str(share["url"])
            )
        else:
            share["rejected_url"] = share.get("url")
            share["url"] = ""
    (run_dir / "share_response.json").write_text(
        json.dumps({"share": share, "verify": verify}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result = {
        "site_id": site_id,
        "site_name": site.name,
        "question_id": question_id,
        "prompt": prompt,
        "answer": answer,
        "answer_raw": answer_raw,
        "citations": citations,
        "citation_domains": list({urlparse_domain(u) for u in citations if u}),
        "citation_details": citation_details,
        "source": source,
        "answer_origin": "web",
        "url": result_url,
        "share_status": "verified" if result_url else ("verification_failed" if share.get("rejected_url") else "generation_failed"),
        "citations_status": "available" if citation_details else "missing",
        "ok": bool(answer) and (bool(early_checkpoint) or not _looks_like_nav_junk(answer, prompt)),
        "run_dir": str(run_dir.relative_to(GEO_DIR)) if run_dir.is_relative_to(GEO_DIR) else str(run_dir),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "steps": _step_timings(profiler),
        "timestamp": datetime.now().isoformat(),
    }
    if images:
        result["images"] = images
    result.update(search_evidence)
    if optional_extract_error:
        result["optional_stage_error_code"] = optional_extract_error
        result["web_answer_recovered"] = True
    if site_id == "kimi" and diagnostic:
        result["kimi_knowledge_base"] = diagnostic.meta.get("kimi_knowledge_base", "not_observed")
    if site_id == "deepseek" and diagnostic:
        settings = diagnostic.meta.get("deepseek_settings") or {}
        result["web_thinking_setting"] = settings.get("thinking", "not_observed")
    if diagnostic and diagnostic.meta.get("completion_detection"):
        result["completion_detection"] = diagnostic.meta["completion_detection"]
    issues = []
    if not result_url:
        error = str(verify.get("error") or share.get("error") or "share_url_not_observed").split(":",1)[0]
        result["share_error_code"] = error if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error) else "share_"+result["share_status"]
        issues.append("share_"+result["share_status"])
    if not citation_details:
        issues.append("citations_missing")
    if result["ok"] and issues:
        result["quality_issues"] = issues
        if diagnostic:
            diagnostic.meta["share_status"] = result["share_status"]
            diagnostic.meta["share_error_code"] = result.get("share_error_code", "")
            diagnostic.retain_quality_issue(issues, page)
    if not result["ok"]:
        if not answer:
            result.update({
                "error": "平台未返回有效回答",
                "error_code": "empty_answer",
                "retryable": True,
            })
        else:
            result.update({
                "error": "平台返回内容无效",
                "error_code": "invalid_answer",
                "retryable": True,
            })
    profiler.save_summary()
    if journal and result.get("ok"):
        journal.checkpoint(dict(result, web_answer_completed=True))
    (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def urlparse_domain(url: str) -> str:
    try:
        return urlparse(url).netloc or ""
    except Exception:
        return ""


def _step_timings(profiler: SiteProfiler) -> list:
    return [
        {"name": s.name, "elapsed_ms": int(s.elapsed_ms), "ok": s.ok}
        for s in profiler.steps
    ]


def _doubao_answer_landed(capture: NetworkCapture) -> bool:
    for item in capture.items:
        url = str(item.get("url") or "")
        body = str(item.get("body") or "")
        if "doubao.com" not in url or len(body) < 200:
            continue
        if "/chat/completion" in url or "/im/chain/single" in url:
            return True
    return False


def _page_logged_out(page: Page, site_id: str = "") -> bool:
    if site_id == "doubao":
        return wait_doubao_auth(page) == "logged_out"
    try:
        # Role lookups build an accessibility tree and also wait out the 30s
        # navigation timeout. A short script over the first header controls
        # answers the same question without that stall.
        page.set_default_navigation_timeout(1500)
        return bool(page.locator("html").evaluate(
            """() => {
              const nodes = document.querySelectorAll('header button, header a, button, a');
              const limit = Math.min(nodes.length, 40);
              for (let i = 0; i < limit; i++) {
                const s = ((nodes[i].innerText || '') + '').replace(/\\s+/g, ' ').trim();
                if (s !== '登录' && s !== '登录 / 注册') continue;
                const r = nodes[i].getBoundingClientRect();
                if (r.width > 2 && r.height > 2) return true;
              }
              return false;
            }""",
            timeout=1500,
        ))
    except Exception:
        return "from_logout=1" in (page.url or "")
    finally:
        try:
            page.set_default_navigation_timeout(30000)
        except Exception:
            pass


_ANSWER_URL_RE = re.compile(r"https?://[^\s<>\]\)\"'，。；、]+", re.IGNORECASE)
_URL_DASHES = str.maketrans({
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
})


def extract_answer_urls(answer: str) -> List[str]:
    """回答正文里写明的链接。豆包常把网址拆进流式分片，拼完后这里才是完整地址。"""
    seen = set()
    out: List[str] = []
    for raw in _ANSWER_URL_RE.findall(answer or ""):
        url = raw.translate(_URL_DASHES).rstrip(".,;:)>】")
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        if _weak_citation(url):
            continue
        seen.add(url)
        out.append(url)
    return out


def _weak_citation(url: str) -> bool:
    """页面把长链接从中间截断，或只给出豆包跳转壳。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return True
    if not host or "." not in host:
        return True
    if host.endswith(("wtturl.cn", "doubao.com", "byteimg.com")):
        return True
    return False


def _structured_answer(text: str) -> bool:
    """标题或条目列表是回答，不是侧栏历史标题。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk < 40:
        return False
    headings = sum(1 for ln in lines if ln.startswith("#"))
    bullets = sum(1 for ln in lines if re.match(r"^(?:[-*•]|\d+[、.．])\s+\S", ln))
    return headings >= 1 or bullets >= 4


def _looks_like_nav_junk(text: str, prompt: str = "") -> bool:
    """侧栏/历史标题堆，不是正文。"""
    if not text:
        return True
    markers = ("收藏夹", "意见反馈", "百度首页", "历史对话", "新对话", "Ctrl\nK", "下载豆包电脑版", "有什么我能帮你的吗")
    hits = sum(1 for m in markers if m in text[:800])
    if hits >= 2:
        return True
    if _structured_answer(text):
        return False
    # 大量短行标题
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > 30 and sum(1 for ln in lines if len(ln) < 40) / len(lines) > 0.7:
        return True
    return False
