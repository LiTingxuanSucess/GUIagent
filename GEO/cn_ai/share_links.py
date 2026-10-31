# -*- coding: utf-8 -*-
"""Collect observed public share URLs. Shape checks are not proof of visibility."""
from __future__ import annotations

import json
try:
    import fcntl
except ImportError:  # Windows deployments do not enable the macOS UI lock.
    fcntl = None
import html as html_parser
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from playwright.sync_api import Browser, Page

from cn_ai.network_parser import parse_deepseek_share_url
from cn_ai import budget as task_budget

_ID = r"[A-Za-z0-9_-]{8,}"
_YIYAN_ID = r"[A-Za-z0-9_@.-]{8,}"
_KIMI_SHARE = re.compile(
    rf"^https://(www\.)?kimi\.(com|moonshot\.cn)/share/(?:[A-Za-z]{{2,8}}/)?{_ID}$"
)
_YIYAN_SHARE = re.compile(
    rf"^https://(chat|wenxin)\.baidu\.com/csaitab/history/share\?share_id={_YIYAN_ID}(&v=2)?$"
)
_YIYAN_SHORT_SHARE = re.compile(
    rf"^https://mr\.baidu\.com/r/{_ID}\?f=ot&u={_ID}$"
)
PUBLIC_SHARE_PATTERNS: Dict[str, List[re.Pattern[str]]] = {
    "deepseek": [re.compile(rf"^https://chat\.deepseek\.com/share/{_ID}$")],
    "qianwen": [
        re.compile(rf"^https://www\.qianwen\.com/share\?shareId={_ID}$"),
        re.compile(rf"^https://www\.qianwen\.com/share/chat/{_ID}$"),
        re.compile(rf"^https://qianwen\.my\.cn/share/chat/{_ID}\?lang=zh-CN$"),
    ],
    "yuanbao": [
        re.compile(rf"^https://yuanbao\.tencent\.com/bot/app/share/chat/{_ID}$"),
        re.compile(rf"^https://yb\.tencent\.com/s/{_ID}$"),
    ],
    "kimi": [_KIMI_SHARE],
    "doubao": [
        re.compile(rf"^https://(www\.)?doubao\.com/s/{_ID}$"),
        re.compile(rf"^https://(www\.)?doubao\.com/thread/{_ID}$"),
    ],
    "yiyan": [_YIYAN_SHARE, _YIYAN_SHORT_SHARE],
}

_TRUSTED_SHARE_HOSTS: Dict[str, set[str]] = {
    "deepseek": {"chat.deepseek.com"},
    "qianwen": {"www.qianwen.com", "qianwen.my.cn"},
    "yuanbao": {"yuanbao.tencent.com", "yb.tencent.com"},
    "kimi": {"www.kimi.com", "kimi.com", "kimi.moonshot.cn", "www.kimi.moonshot.cn"},
    "doubao": {"www.doubao.com", "doubao.com"},
    "yiyan": {"chat.baidu.com", "wenxin.baidu.com", "mr.baidu.com"},
}

_LOGIN_MARKERS = (
    "请先登录",
    "请登录",
    "立即登录",
    "扫码登录",
    "sign in",
    "该分享不存在",
    "分享已过期",
    "分享已失效",
    "该会话暂时无法查看",
    "对话不存在",
    "内容不存在",
    "页面不存在",
)


def is_public_share_url(site_id: str, raw: str) -> bool:
    """Shape check only. Does not prove the Q&A is visible while logged out."""
    url = str(raw or "").strip()
    if not url.startswith("https://"):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.username or parsed.fragment:
        return False
    if site_id == "yiyan":
        return _yiyan_share_shape(parsed)
    if site_id == "kimi":
        return bool(_KIMI_SHARE.match(url))
    for pattern in PUBLIC_SHARE_PATTERNS.get(site_id, []):
        if pattern.match(url):
            return True
    return False


def _yiyan_share_shape(parsed) -> bool:
    host = (parsed.hostname or "").lower()
    if host == "mr.baidu.com":
        if not re.fullmatch(r"/r/[A-Za-z0-9_-]{8,}", parsed.path or ""):
            return False
        qs = parse_qs(parsed.query)
        return (
            set(qs) == {"f", "u"}
            and (qs.get("f") or [""])[0] == "ot"
            and bool(re.fullmatch(_ID, (qs.get("u") or [""])[0]))
        )
    if host not in {"chat.baidu.com", "wenxin.baidu.com"}:
        return False
    if parsed.path != "/csaitab/history/share":
        return False
    qs = parse_qs(parsed.query)
    share_id = (qs.get("share_id") or [""])[0]
    if not re.fullmatch(_YIYAN_ID, share_id):
        return False
    version = (qs.get("v") or ["2"])[0]
    return version == "2"


def dismiss_share_ui(page: Page, site_id: str) -> bool:
    """Leave the observed share picker without changing the conversation."""
    selectors = {
        "kimi": ".chat-action--share-mode .share-bottom-container .cancel-button",
        "yiyan": ".share-footer .cos-icon-close",
    }
    if site_id not in selectors:
        return False
    cancel = page.locator(selectors[site_id]).last
    try:
        if cancel.count() and cancel.is_visible():
            cancel.click(timeout=2000, no_wait_after=True)
            cancel.wait_for(state="hidden", timeout=2000)
            page.locator('textarea:visible,[contenteditable=true]:visible,[role=textbox]:visible').first.wait_for(
                state="visible", timeout=2000)
            return True
    except Exception:
        pass
    return False


def create_public_share(page: Page, site_id: str) -> Dict[str, Any]:
    """Return a URL observed from the real share flow. Not yet content-verified."""
    lock_path = os.getenv('GEO_SHARE_UI_LOCK_FILE', '')
    if not lock_path:
        return _create_public_share_locked(page, site_id)
    if fcntl is None:
        return {'url': '', 'error': 'share_ui_lock_unavailable'}
    # Clipboard and OS foreground activation are shared across Chrome tabs.
    # Hold this only during share creation; generation/verification stay parallel.
    with open(lock_path, 'a', encoding='utf-8') as lock:
        deadline = time.monotonic() + task_budget.remaining(25)
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return {'url': '', 'error': 'share_ui_busy'}
                time.sleep(.1)
        try:
            return _create_public_share_locked(page, site_id)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _create_public_share_locked(page: Page, site_id: str) -> Dict[str, Any]:
    if site_id == "deepseek":
        try:
            result = _create_deepseek(page)
        except Exception as exc:
            result = {"url": "", "status": 0, "body": "", "error": str(exc)}
    elif site_id == "yuanbao":
        try:
            result = _create_yuanbao(page)
            if not result.get("url"):
                result = _observe_share_from_ui(page, site_id)
        except Exception as exc:
            result = {"url": "", "status": 0, "body": "", "error": str(exc)}
    elif site_id in PUBLIC_SHARE_PATTERNS:
        try:
            result = _observe_share_from_ui(page, site_id)
        except Exception as exc:
            result = {"url": "", "status": 0, "body": "", "error": str(exc)}
        finally:
            dismiss_share_ui(page, site_id)
    else:
        return {"url": "", "status": 0, "body": "", "error": "unsupported_site"}
    url = str(result.get("url") or "")
    if not is_public_share_url(site_id, url):
        result["url"] = ""
        if url:
            result.setdefault("rejected_url", url)
    return result


def share_answer_hint(site_id: str, share: Dict[str, Any], fallback: str) -> str:
    """Prefer the answer payload used by the platform's own share object."""
    if site_id == "yiyan":
        hint = str(share.get("answer_hint") or "").strip()
        compact = lambda value: re.sub(r'[^\w\u4e00-\u9fff]+', '', value)
        # Bind this to the exact answer card whose share control was clicked.
        # Multi-step answers can start with acknowledgements absent from the
        # final card that the platform actually publishes.
        if len(compact(hint)) >= 80 and compact(hint)[:100] in compact(fallback):
            return hint
        # The final answer is now read directly from its task DOM. A share
        # dialog's last paragraph can be a generic follow-up offer, not the
        # answer whose public visibility must be proven.
        return fallback
    bodies = [str(share.get("body") or "")]
    if site_id == "kimi":
        for hit in share.get("network_hits") or []:
            if not isinstance(hit, dict) or hit.get("kind") != "response":
                continue
            if "CreateChatShare" in str(hit.get("url") or ""):
                bodies.append(str(hit.get("body") or ""))
    for body in bodies:
        if not body:
            continue
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            continue
        if site_id == "yuanbao":
            description = str(payload.get("description") or "").strip()
            if description:
                return description
            continue
        if site_id != "kimi":
            continue
        texts: List[str] = []
        messages = ((payload.get("share") or {}).get("messages") or [])
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for block in message.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, dict):
                    content = str(text.get("content") or "").strip()
                    content = re.sub(
                        r"<REF>.*?</REF>",
                        "",
                        content,
                        flags=re.IGNORECASE | re.DOTALL,
                    ).strip()
                    if content:
                        texts.append(content)
        if texts:
            return texts[-1]
    return fallback


def verify_public_share(
    browser: Optional[Browser],
    site_id: str,
    raw: str,
    answer: str = "",
) -> Dict[str, Any]:
    """Open the URL without cookies and require this answer text to be visible."""
    url = str(raw or "").strip()
    out: Dict[str, Any] = {"ok": False, "url": url, "final_url": "", "method": ""}
    if not is_public_share_url(site_id, url):
        out["error"] = "not_public_pattern"
        return out
    snippets = _answer_snippets(answer)
    if not snippets:
        out["error"] = "no_answer_to_verify"
        return out
    try:
        budget = max(1, min(45, float(os.getenv("GEO_SHARE_VERIFY_BUDGET_SEC", "25"))))
    except ValueError:
        budget = 25
    from cn_ai.diagnostics import current
    diagnostic = current()
    if diagnostic and diagnostic.deadline_ms:
        budget = min(budget, diagnostic.deadline_ms/1000-time.time()-7)
    deadline = time.monotonic()+max(0, budget)
    def fetch():
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            return "", url, 0
        return _http_get(url, timeout=min(5, remaining))
    if budget <= 0:
        out["error"] = "share_verify_timeout"
        return out
    html, final_url, status = fetch()
    out["http_status"] = status
    out["final_url"] = final_url
    if _is_login_wall(final_url, html):
        out["error"] = "login_wall_http"
        return out
    if 200 <= status < 300 and _snippets_visible(snippets, html):
        out["ok"] = True
        out["method"] = "http"
        return out
    # The public Qianwen SPA loads its shared transcript from this anonymous
    # endpoint (tongyi-app-share getShareInfo). No account cookies or tokens.
    if site_id == "qianwen" and deadline > time.monotonic():
        shared = _qianwen_public_answer(url, min(5, deadline-time.monotonic()))
        if _snippets_visible(snippets, shared):
            out.update(ok=True, method="public_transcript")
            return out
    # Public SPAs may never put their answer into the initial HTML. Probe
    # an anonymous browser before spending the whole budget on HTTP retries.
    if browser is not None:
        try:
            text, page_url = _open_fresh_context(browser, url, snippets, timeout_sec=min(15, max(.1, deadline-time.monotonic())))
            out.update(final_url=page_url, method="browser")
            if _is_login_wall(page_url, text):
                out["error"] = "login_wall_browser"
                return out
            if _snippets_visible(snippets, text):
                out["ok"] = True
                return out
        except Exception as exc:
            out["browser_error"] = type(exc).__name__
    # Qianwen and Yuanbao return the public URL before the share page is
    # populated. Keep checking the logged-out endpoint so a valid new share is
    # not discarded merely because the CDN/API is briefly eventually
    # consistent.
    if site_id in ("qianwen", "yuanbao", "kimi", "yiyan"):
        delays = (2, 3, 5, 8)
        for delay in delays:
            if deadline-time.monotonic() <= delay:
                break
            time.sleep(delay)
            html, final_url, status = fetch()
            out["http_status"] = status
            out["final_url"] = final_url
            if _is_login_wall(final_url, html):
                out["error"] = "login_wall_http"
                return out
            if 200 <= status < 300 and _snippets_visible(snippets, html):
                out["ok"] = True
                out["method"] = "http"
                return out
    if browser is None:
        out["error"] = "http_missing_answer"
        return out
    out["error"] = "share_verify_timeout" if time.monotonic() >= deadline else "answer_not_visible"
    return out


def observed_public_share_url(site_id: str, candidates: List[str]) -> str:
    return _first_public_url(site_id, candidates)


def _first_public_url(site_id: str, candidates: List[str]) -> str:
    """Accept only complete share URLs on trusted hosts, or trusted share paths.

    Bare tokens and IDs from unrelated responses are ignored. Visibility of
    this answer is checked later by verify_public_share.
    """
    for raw in candidates:
        candidate = _trusted_share_candidate(site_id, raw)
        if candidate:
            return candidate
    return ""


def _trusted_share_candidate(site_id: str, raw: str) -> str:
    value = _clean_candidate(raw)
    if not value:
        return ""
    if value.startswith("/"):
        if site_id == "kimi" and value.startswith("/share/"):
            value = "https://www.kimi.com" + value
        elif site_id == "yiyan" and value.startswith("/csaitab/history/share"):
            value = "https://chat.baidu.com" + value
        elif site_id == "qianwen" and (
            value.startswith("/share?") or value.startswith("/share/chat/")
        ):
            value = "https://www.qianwen.com" + value
        elif site_id == "yuanbao" and value.startswith("/bot/app/share/chat/"):
            value = "https://yuanbao.tencent.com" + value
        elif site_id == "doubao" and (
            value.startswith("/s/") or value.startswith("/thread/")
        ):
            value = "https://www.doubao.com" + value
        else:
            return ""
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in _TRUSTED_SHARE_HOSTS.get(site_id, set()):
        return ""
    if is_public_share_url(site_id, value):
        return value
    return ""


def _create_deepseek(page: Page) -> Dict[str, Any]:
    m = re.search(r"/a/chat/s/([a-f0-9-]+)", page.url or "")
    if not m:
        return {"url": "", "status": 0, "body": ""}
    sid = m.group(1)
    raw = page.evaluate(
        """async (sid) => {
            const tok = _dsToken();
            const headers = {'Content-Type': 'application/json'};
            if (tok) headers.Authorization = 'Bearer ' + tok;
            const historyURL = 'https://chat.deepseek.com/api/v0/chat/history_messages?chat_session_id=' + sid;
            const historyResponse = await fetch(historyURL, {credentials: 'include', headers});
            let history = {};
            try { history = await historyResponse.json(); } catch (e) {}
            const data = history.data || {};
            const biz = data.biz_data || data;
            const messages = biz.chat_messages || data.chat_messages || [];
            const messageIds = messages
              .map(m => m.message_id || m.id)
              .filter(x => (typeof x === 'string' || typeof x === 'number') && x);
            const r = await fetch('https://chat.deepseek.com/api/v0/share/create', {
              method: 'POST',
              credentials: 'include',
              headers,
              body: JSON.stringify({chat_session_id: sid, message_ids: messageIds}),
            });
            return JSON.stringify({status: r.status, body: await r.text()});
            function _dsToken() {
              try {
                const saved = localStorage.getItem('userToken');
                if (saved) return JSON.parse(saved).value || '';
              } catch (e) {}
              return '';
            }
        }""",
        sid,
    )
    response = json.loads(raw)
    body = str(response.get("body") or "")
    return {"url": parse_deepseek_share_url(body), "status": int(response.get("status") or 0), "body": body[:20000]}


def _create_yuanbao(page: Page) -> Dict[str, Any]:
    """Call the same general_share endpoint observed from Yuanbao's ShareBar."""
    raw = page.evaluate(
        """async () => {
          const parts = location.pathname.split('/').filter(Boolean);
          const chatAt = parts.indexOf('chat');
          const agentId = chatAt >= 0 ? (parts[chatAt + 1] || '') : '';
          const conversationId = chatAt >= 0 ? (parts[chatAt + 2] || '') : '';
          if (!agentId || !conversationId) {
            return JSON.stringify({status: 0, body: '', error: 'missing_chat_ids'});
          }
          const detailResponse = await fetch(
            '/api/user/agent/conversation/v1/detail',
            {
              method: 'POST',
              credentials: 'include',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({conversationId}),
            }
          );
          const detailText = await detailResponse.text();
          let detail = {};
          try { detail = JSON.parse(detailText); } catch (e) {}
          const data = detail.data || detail;
          const conversations = Array.isArray(data.convs) ? data.convs : [];
          const indexes = [...new Set(conversations
            .map(item => Number(item && item.index))
            .filter(index => Number.isFinite(index) && index > 0))]
            .sort((a, b) => a - b);
          const userId = String(
            data.userId ||
            (conversations.find(item => item && item.userId) || {}).userId ||
            ''
          );
          if (!userId || !indexes.length) {
            return JSON.stringify({
              status: detailResponse.status,
              body: detailText,
              error: 'missing_share_metadata',
            });
          }
          const accountMatch = ((document.body && document.body.innerText) || '')
            .match(/用户[A-Za-z0-9]{4,}/);
          const payload = {
            agentId,
            userId,
            userName: accountMatch ? accountMatch[0] : userId,
            shareContentType: 0,
            shareType: 'conversations',
            shareChannel: 'copy_link',
            targetUrlPrefix: 'https://yuanbao.tencent.com/',
            extra: JSON.stringify({
              conversationId,
              agentId,
              conversations: indexes.map(index => ({index})),
              selectAll: true,
              excludeIndexes: [],
              nickname: userId,
              platform: 'WEB',
            }),
          };
          const response = await fetch('/api/share/general_share', {
            method: 'POST',
            credentials: 'include',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
          });
          return JSON.stringify({
            status: response.status,
            body: await response.text(),
          });
        }"""
    )
    response = json.loads(str(raw or "{}"))
    body = str(response.get("body") or "")
    return {
        "url": observed_public_share_url(
            "yuanbao",
            _candidate_strings(body),
        ),
        "status": int(response.get("status") or 0),
        "body": body[:20000],
        "source": "api" if body else "",
        "error": str(response.get("error") or ""),
    }


def _open_qianwen_share(page: Page) -> None:
    more = page.locator("[data-session-active='true'] button[data-session-switch-target='session-tools']").first
    more.scroll_into_view_if_needed(timeout=2000)
    more.hover(timeout=2000)
    more.click(timeout=3000, no_wait_after=True)
    share = page.get_by_role('menuitem', name='分享此对话', exact=True).last
    share.wait_for(state='visible', timeout=3000)
    share.click(timeout=3000, no_wait_after=True)


def _observe_share_from_ui(page: Page, site_id: str) -> Dict[str, Any]:
    try:
        page.bring_to_front()
    except Exception:
        pass
    _grant_clipboard_permissions(page)
    previous_clipboard = _read_clipboard(page)
    original_pages = set(page.context.pages)
    # Observe the real UI gesture without replacing fetch/XHR/clipboard.
    # Replacing fetch also delays a later streaming answer in a reused tab.
    # Page scripts may cache fetch/XHR/clipboard methods before our in-page
    # hooks are installed.  Playwright's protocol-level events still observe
    # those requests, so keep both collectors around the actual UI gesture.
    network_hits: List[Dict[str, str]] = []

    def on_request(request: Any) -> None:
        try:
            method = str(request.method or "").upper()
            url = str(request.url or "")
            if "share" in urlparse(url).path.lower():
                network_hits.append(
                    {
                        "kind": "request",
                        "url": url,
                        "body": str(request.post_data or "")[:20000],
                    }
                )
        except Exception:
            pass

    def on_finished(request: Any) -> None:
        try:
            response = request.response()
            if response is None:
                return
            method = str(response.request.method or "").upper()
            url = str(response.url or "")
            if "share" in urlparse(url).path.lower():
                limit = 200000 if "CreateChatShare" in url else 20000
                network_hits.append(
                    {
                        "kind": "response",
                        "url": url,
                        "body": str(response.text() or "")[:limit],
                    }
                )
        except Exception:
            pass

    page.on("request", on_request)
    page.on("requestfinished", on_finished)
    def observed_candidates() -> List[str]:
        values: List[str] = []
        for hit in network_hits:
            values.append(hit.get("url", ""))
            values.extend(_candidate_strings(hit.get("body", "")))
        return values

    try:
        ui = _click_share_and_collect(page, site_id, observed_candidates)
        # Clipboard reads and waits pump Playwright's event dispatcher. Keep
        # the observer installed until the asynchronous create response lands.
        clipboard = _read_clipboard(page)
        deadline = time.monotonic() + task_budget.remaining(2, reserve=.2)
        while not observed_public_share_url(site_id, observed_candidates()) and time.monotonic() < deadline:
            if clipboard and clipboard != previous_clipboard and observed_public_share_url(site_id, _candidate_strings(clipboard)):
                break
            page.wait_for_timeout(100)
            clipboard = _read_clipboard(page)
        network_candidates = observed_candidates()
        candidates = network_candidates + list(ui.get("candidates") or [])
        if clipboard and (clipboard != previous_clipboard or observed_public_share_url(site_id, network_candidates)):
            candidates.extend(_candidate_strings(clipboard))
        candidates.extend(str(p.url or "") for p in page.context.pages if p not in original_pages)
        url = observed_public_share_url(site_id, candidates)
    finally:
        try:
            page.remove_listener("request", on_request)
            page.remove_listener("requestfinished", on_finished)
        except Exception:
            pass
    return {
        "url": url,
        "status": 0,
        "body": "",
        "clicked": ui.get("clicked") or "",
        "candidates": candidates[:40],
        "network_hits": network_hits[:40],
        "answer_hint": ui.get("answer_hint") or "",
        "source": "ui" if url else "",
    }


def _clean_candidate(raw: str) -> str:
    value = str(raw or "").strip().strip("<>\"',;")
    if value.startswith("//"):
        value = "https:" + value
    return value.split("#")[0].rstrip(").,，。")


def _install_share_hooks(page: Page) -> None:
    try:
        page.evaluate(
            """() => {
              if (window.__geoShareHooked) {
                window.__geoShareHits = [];
                return;
              }
              window.__geoShareHooked = true;
              window.__geoShareHits = [];
              const push = (url, body) => {
                window.__geoShareHits.push({
                  url: String(url || ''),
                  body: String(body || '').slice(0, 20000),
                });
              };
              const origFetch = window.fetch;
              window.fetch = async function() {
                const r = await origFetch.apply(this, arguments);
                try {
                  const req = arguments[0];
                  const init = arguments[1] || {};
                  const url = (typeof req === 'string') ? req : (req && req.url) || '';
                  const method = String(init.method || (req && req.method) || 'GET').toUpperCase();
                  if (/share/i.test(url) || method === 'POST') {
                    push(url, await r.clone().text());
                  }
                } catch (e) {}
                return r;
              };
              const origOpen = XMLHttpRequest.prototype.open;
              const origSend = XMLHttpRequest.prototype.send;
              XMLHttpRequest.prototype.open = function(method, url) {
                this.__geoMethod = method;
                this.__geoUrl = url;
                return origOpen.apply(this, arguments);
              };
              XMLHttpRequest.prototype.send = function() {
                this.addEventListener('load', function() {
                  if (/share/i.test(String(this.__geoUrl || '')) ||
                      String(this.__geoMethod || '').toUpperCase() === 'POST') {
                    push(this.__geoUrl, this.responseText);
                  }
                });
                return origSend.apply(this, arguments);
              };
              const origWindowOpen = window.open;
              window.open = function(url) {
                push(url, '');
                return origWindowOpen.apply(this, arguments);
              };
              if (navigator.clipboard && navigator.clipboard.writeText) {
                const origWrite = navigator.clipboard.writeText.bind(navigator.clipboard);
                navigator.clipboard.writeText = async (text) => {
                  push('clipboard', text);
                  return origWrite(text);
                };
              }
            }"""
        )
    except Exception:
        pass


def _hook_candidates(page: Page) -> List[str]:
    try:
        hits = page.evaluate("() => window.__geoShareHits || []") or []
    except Exception:
        return []
    out: List[str] = []
    for item in hits:
        out.append(str((item or {}).get("url") or ""))
        out.extend(_candidate_strings(str((item or {}).get("body") or "")))
    return [x for x in out if x]


def _click_share_and_collect(page: Page, site_id: str = "",
                             observed_candidates: Optional[Callable[[], List[str]]] = None) -> Dict[str, Any]:
    labels = [
        "分享对话",
        "分享会话",
        "分享当前对话",
        "分享给朋友",
        "复制分享链接",
        "复制公开链接",
        "生成分享链接",
        "生成永久链接",
        "公开分享",
        "创建分享",
        "复制链接",
        "分享",
    ]
    clicked: List[str] = []
    candidates: List[str] = []
    answer_hint = ""
    if site_id == "yuanbao":
        try:
            share_buttons = page.locator("[aria-label='分享'], [title='分享']")
            visible_shares = [
                share_buttons.nth(i)
                for i in range(share_buttons.count())
                if share_buttons.nth(i).is_visible()
            ]
            if visible_shares:
                visible_shares[-1].click(timeout=3000, force=True, no_wait_after=True)
                clicked.append("分享")
                page.wait_for_timeout(1000)
            select_all = page.locator(
                ".agent-chat__share-bar__content__left"
            ).last
            select_all.wait_for(state="visible", timeout=5000)
            select_all.click(timeout=3000, force=True, no_wait_after=True)
            clicked.append("全选")
            page.wait_for_function(
                """() => {
                  const items = [...document.querySelectorAll('.agent-chat__list__item')];
                  return items.length > 0 &&
                    items.every(el => el.classList.contains('agent-chat__list__item--selected'));
                }""",
                timeout=5000,
            )
            # The selected CSS state appears before ShareBar receives the
            # updated disabled=false prop; wait for that React render too.
            page.wait_for_timeout(2000)
            # The visible "复制链接" text is a sibling of the actual click
            # target in Yuanbao's ShareBar; clicking the text does not bubble
            # into the icon's onClick handler.
            copy_link = page.locator(
                "[data-share-type='link'][aria-label='复制链接']"
            ).last
            copy_link.wait_for(state="visible", timeout=5000)
            copy_link.click(timeout=3000, force=True, no_wait_after=True)
            clicked.append("复制链接")
            # Yuanbao performs an anti-abuse challenge before calling
            # /api/share/general_share.  The request can start several seconds
            # after the click, so keep the protocol observer attached.
            page.wait_for_timeout(5000)
        except Exception:
            pass
    if site_id == "doubao":
        try:
            copy_buttons = page.locator(
                "[data-testid='thread_share_copy_btn']"
            )
            visible_copies = [
                copy_buttons.nth(i)
                for i in range(copy_buttons.count())
                if copy_buttons.nth(i).is_visible()
            ]
            if not visible_copies:
                share_buttons = page.locator(
                    "[data-testid='message_action_share']"
                )
                visible_shares = [
                    share_buttons.nth(i)
                    for i in range(share_buttons.count())
                    if share_buttons.nth(i).is_visible()
                ]
                if visible_shares:
                    visible_shares[-1].click(
                        timeout=3000,
                        no_wait_after=True,
                    )
                    clicked.append("分享")
                    page.wait_for_timeout(800)
                copy_buttons = page.locator(
                    "[data-testid='thread_share_copy_btn']"
                )
                visible_copies = [
                    copy_buttons.nth(i)
                    for i in range(copy_buttons.count())
                    if copy_buttons.nth(i).is_visible()
                ]
            if visible_copies:
                # Doubao only creates /thread/... after a trusted pointer
                # click. HTMLElement.click() does not trigger this flow.
                visible_copies[-1].click(
                    timeout=3000,
                    no_wait_after=True,
                )
                clicked.append("复制链接")
                deadline = time.monotonic() + task_budget.remaining(4, reserve=.2)
                while time.monotonic() < deadline:
                    if observed_candidates and observed_public_share_url(site_id, observed_candidates()):
                        break
                    page.wait_for_timeout(100)
        except Exception:
            pass
    if site_id == "kimi":
        try:
            # The answer toolbar is now icon-only. The share control is the
            # button whose svg name is Share_a, not a .simple-button labeled 分享.
            share_buttons = page.locator(
                ".segment-assistant-actions .icon-button:has(svg[name='Share_a'])"
            )
            if share_buttons.count() == 0:
                share_buttons = page.locator(".simple-button").filter(
                    has_text=re.compile(r"^分享$")
                )
            visible_shares = [
                share_buttons.nth(i)
                for i in range(share_buttons.count())
                if share_buttons.nth(i).is_visible()
            ]
            if visible_shares:
                # Kimi ignores HTMLElement.click() for this control. A real
                # Playwright pointer click is also required for clipboard
                # activation after the share dialog opens.
                visible_shares[-1].click(timeout=3000, no_wait_after=True)
                clicked.append("分享")
                # Wait for React's copy action instead of a fixed delay.
                page.wait_for_function("""() => [...document.querySelectorAll('button,[role=button],div,span')]
                  .some(e => /^(复制链接|Copy link)$/i.test((e.innerText || '').trim()) &&
                    e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0 &&
                    getComputedStyle(e).visibility !== 'hidden')""", timeout=3000)
                copy_buttons = page.get_by_text(
                    re.compile(r"复制链接|Copy link", re.I)
                )
                visible_copies = [
                    copy_buttons.nth(i)
                    for i in range(copy_buttons.count())
                    if copy_buttons.nth(i).is_visible()
                ]
                if visible_copies:
                    visible_copies[-1].click(timeout=3000, no_wait_after=True)
                    clicked.append("复制链接")
                    # CreateChatShare is asynchronous. Leave as soon as the
                    # public URL shows up instead of always waiting 6 seconds.
                    deadline = time.monotonic() + 6
                    while time.monotonic() < deadline:
                        observed = _hook_candidates(page)
                        if observed_candidates is not None:
                            observed.extend(observed_candidates())
                        if observed_public_share_url(site_id, observed):
                            break
                        page.wait_for_timeout(100)
                else:
                    labels = page.evaluate(
                        """() => [...document.querySelectorAll('button,[role=button],div,span')]
                          .filter(n => {
                            const r = n.getBoundingClientRect();
                            return r.width > 8 && r.height > 8 && r.top >= 0 && r.top < innerHeight;
                          })
                          .map(n => (n.innerText || n.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim())
                          .filter(t => t && t.length <= 24)
                          .slice(0, 12)"""
                    ) or []
                    clicked.append("no-copy:" + "|".join(str(x) for x in labels)[:180])
        except Exception:
            pass
    if site_id == "qianwen":
        try:
            _open_qianwen_share(page)
            clicked.extend(('session-more', '分享此对话'))
            modal = page.locator("#share-modal-root").last
            copies = modal.get_by_text("复制链接", exact=True)
            copies.last.wait_for(state="visible", timeout=3000)
            copies.last.click(timeout=2500, no_wait_after=True)
            clicked.append("复制链接")
            deadline = time.monotonic()+8
            while time.monotonic() < deadline:
                candidates = _hook_candidates(page)
                candidates.extend(_candidate_strings(modal.inner_text(timeout=1000)))
                clipboard = _read_clipboard(page)
                if clipboard:
                    candidates.extend(_candidate_strings(clipboard))
                if observed_public_share_url(site_id, candidates):
                    break
                page.wait_for_timeout(250)
        except Exception:
            pass
    if site_id == "yiyan":
        try:
            opened = page.evaluate(
                """() => {
                  const shares = [...document.querySelectorAll(
                    '[data-testid="menu-btn-share"], .cos-icon-share1'
                  )].filter(el=>{
                    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
                    return r.width>2 && r.height>2 && s.display!=='none' && s.visibility!=='hidden';
                  });
                  // Work-mode Wenxin renders an acknowledgement and a final
                  // history answer with separate share controls. The first
                  // control publishes the acknowledgement, not the result.
                  const share = shares.filter(el=>el.closest('.cs-history-answer')).pop() || shares.pop();
                  if (!share) return '';
                  const card=share.closest('.cs-history-answer');
                  const answer=card&&([...card.querySelectorAll('.marklang,.ai-markdown')]
                    .filter(n=>(n.innerText||'').trim().length>=80).pop());
                  const hint=answer ? (answer.innerText||'').trim() : '';
                  (share.closest('button,[role="button"],[data-testid]') || share).click();
                  return {action:'answer-share',answer_hint:hint};
                }""",
            )
            if not opened and page.locator('.share-footer:visible').count():
                opened = 'existing-answer-share'
            if opened:
                if isinstance(opened, dict):
                    answer_hint = str(opened.get('answer_hint') or '')
                    clicked.append(str(opened.get('action') or 'answer-share'))
                else:
                    clicked.append(str(opened))
                page.wait_for_timeout(800)
                copy = page.get_by_role("button", name="复制链接", exact=True).last
                copy.wait_for(state="visible", timeout=3000)
                # The real share UI creates a token then writes the URL to
                # the clipboard. A JS HTMLElement.click lacks user activation.
                copy.click(timeout=3000, no_wait_after=True)
                clicked.append("复制链接")
                for _ in range(16):
                    page.wait_for_timeout(250)
                    clipboard = _read_clipboard(page)
                    if clipboard:
                        candidates.append(clipboard)
                        if observed_public_share_url(site_id, candidates):
                            break
        except Exception:
            pass
    handled = (
        site_id == "qianwen"
    ) or (
        site_id == "kimi" and "分享" in clicked
    ) or (
        site_id == "yuanbao" and "复制链接" in clicked
    ) or (
        site_id == "doubao" and "复制链接" in clicked
    ) or (
        site_id == "yiyan" and "复制链接" in clicked
    )
    rounds = 0 if handled else 8
    for _round in range(rounds):
        label = ""
        try:
            label = page.evaluate(
                """(labels) => {
                  const exact = new Set(labels);
                  const nodes = [...document.querySelectorAll('button,[role="button"],span,div,[aria-label],[title]')];
                  const hit = nodes.find(n => {
                    if (!n.offsetParent) return false;
                    if (n.tagName === 'A' && /^https?:/.test(n.getAttribute('href') || '')) return false;
                    const text = (n.innerText || '').replace(/\\s+/g, ' ').trim();
                    const aria = (n.getAttribute('aria-label') || '').trim();
                    const title = (n.getAttribute('title') || '').trim();
                    return exact.has(text) || exact.has(aria) || exact.has(title);
                  });
                  if (!hit) return '';
                  hit.click();
                  return (hit.innerText || hit.getAttribute('aria-label') || 'share').replace(/\\s+/g, ' ').trim().slice(0, 40);
                }""",
                labels,
            ) or ""
        except Exception:
            label = ""
        if label:
            clicked.append(label)
            page.wait_for_timeout(1400)
            if "复制" in label:
                break
        else:
            if site_id == "qianwen":
                try:
                    opened = page.evaluate(
                        """() => {
                          const active = document.querySelector('[data-session-active="true"]');
                          const nodes = [...document.querySelectorAll(
                            'button[aria-label="更多"],[role="button"][aria-label="更多"]'
                          )].filter(n => n.offsetParent && !n.dataset.geoShareTried);
                          const hit = nodes.find(n => !active || !active.contains(n));
                          if (!hit) return '';
                          hit.dataset.geoShareTried = '1';
                          hit.click();
                          return 'more:' + nodes.indexOf(hit);
                        }"""
                    ) or ""
                except Exception:
                    opened = ""
                if opened:
                    clicked.append(str(opened))
                    page.wait_for_timeout(800)
                    continue
            break
    try:
        candidates.extend(page.evaluate(
            """() => {
              const out = [];
              const visit = root => {
                root.querySelectorAll(
                  'input,textarea,a[href],[data-share],[data-url],[data-href]'
                ).forEach(el => {
                  for (const v of [
                    el.value, el.href, el.getAttribute('data-share'),
                    el.getAttribute('data-url'), el.getAttribute('data-href')
                  ]) {
                    if (v) out.push(String(v).trim());
                  }
                  if (el.shadowRoot) visit(el.shadowRoot);
                });
                root.querySelectorAll('*').forEach(el => {
                  if (el.shadowRoot) visit(el.shadowRoot);
                });
              };
              visit(document);
              out.push((document.body && document.body.innerText) || '');
              return out;
            }"""
        ) or [])
    except Exception:
        pass
    expanded: List[str] = []
    for candidate in candidates:
        expanded.append(str(candidate))
        expanded.extend(_candidate_strings(str(candidate)))
    return {"url": "", "clicked": ",".join(clicked), "candidates": expanded, "answer_hint": answer_hint}


def _candidate_strings(raw: str) -> List[str]:
    """Extract complete observed URLs/paths; never construct one from a bare ID."""
    text = str(raw or "").replace("\\/", "/")
    text = re.sub(r"\\+u0026", "&", text, flags=re.IGNORECASE)
    patterns = (
        r"https?://[^\"'\s<>\\]+",
        r"/share/(?:[A-Za-z]{2,8}/)?[A-Za-z0-9_-]{8,}",
        r"/share\?shareId=[A-Za-z0-9_-]{8,}",
        r"/share/chat/[A-Za-z0-9_-]{8,}",
        r"/csaitab/history/share\?[^\"'\s<>\\]+",
        r"/bot/app/share/chat/[A-Za-z0-9_-]{8,}",
        r"/(?:s|thread)/[A-Za-z0-9_-]{8,}",
    )
    out: List[str] = []
    for pattern in patterns:
        out.extend(re.findall(pattern, text))
    return out


def _context_page_candidates(page: Page) -> List[str]:
    try:
        return [str(candidate.url or "") for candidate in page.context.pages]
    except Exception:
        return []


def _read_clipboard(page: Page) -> str:
    try:
        _grant_clipboard_permissions(page)
        value = str(
            page.evaluate(
                "() => navigator.clipboard ? navigator.clipboard.readText() : ''"
            )
            or ""
        ).strip()
        if value:
            return value
    except Exception:
        pass
    if sys.platform == 'darwin':
        try:
            # Chrome can deny clipboard.readText in a background CDP tab even
            # after the native copy gesture succeeded. Keep only recognized
            # public share URLs from the Mac clipboard, never unrelated text.
            value = subprocess.run(['/usr/bin/pbpaste'], capture_output=True, text=True,
                                   encoding='utf-8', timeout=1, check=True).stdout
            return ' '.join(url for url in _candidate_strings(value) if any(
                is_public_share_url(site, url) for site in PUBLIC_SHARE_PATTERNS))
        except (OSError, subprocess.SubprocessError):
            pass
    return ''


def _grant_clipboard_permissions(page: Page) -> None:
    try:
        parsed = urlparse(page.url or "")
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None
        page.context.grant_permissions(
            ["clipboard-read", "clipboard-write"],
            origin=origin,
        )
    except Exception:
        pass


def _answer_snippets(answer: str) -> List[str]:
    compact = re.sub(r"\s+", "", answer or "")
    compact = re.sub(r"[\[【][^\]】]{0,48}[\]】]", "", compact)
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", compact)
    out: List[str] = []
    if len(compact) >= 16:
        out.append(compact[8:24])
        out.append(compact[:16])
        if len(compact) > 40:
            out.append(compact[20:36])
    elif compact:
        out.append(compact)
    # Short-answer prompts are common in production (dates, names, yes/no).
    # The URL shape and logged-out/login-wall checks still run before this
    # content proof, so four distinctive characters are sufficient here.
    return [x for x in out if len(x) >= 4]


def _snippets_visible(snippets: List[str], text: str) -> bool:
    decoded = html_parser.unescape(text or "")
    decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match:chr(int(match[1],16)), decoded)
    compact = re.sub(r"\s+", "", decoded)
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", compact)
    return any(snippet in compact for snippet in snippets)


def _is_login_wall(url: str, text: str) -> bool:
    lowered = (url or "").lower()
    if any(token in lowered for token in ("/login", "sign_in", "passport", "from_logout=1")):
        return True
    blob = (text or "").lower()
    if any(marker.lower() in blob for marker in _LOGIN_MARKERS) and len(re.sub(r"\s+", "", text or "")) < 800:
        return True
    return False


def _qianwen_public_answer(url: str, timeout: float) -> str:
    parsed = urlparse(url)
    share_id = (parse_qs(parsed.query).get("shareId") or [""])[0]
    if not share_id and parsed.path.startswith("/share/chat/"):
        share_id = parsed.path.rsplit("/", 1)[-1]
    if not re.fullmatch(_ID, share_id):
        return ""
    request = Request(
        "https://chat2-api.qianwen.com/api/v1/share/info?pr=qwen&fr=mac",
        data=json.dumps({"share_id": share_id, "biz_id": "ai_qwen"}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with build_opener().open(request, timeout=max(.1, timeout)) as response:
            data = json.loads(response.read(2_000_000).decode("utf-8"))
        if data.get("code", 0) != 0 or data.get("success") is False:
            return ""
        shared = data.get("data") or {}
        if shared.get("expired"):
            return ""
        answers = []
        for record in (shared.get("session") or {}).get("record_list") or []:
            for message in record.get("response_messages") or []:
                if message.get("mime_type") in ("text/plain", "text/markdown", "multi_load/iframe"):
                    content = message.get("content")
                    if isinstance(content, str):
                        answers.append(content)
        return "\n".join(answers)
    except Exception:
        return ""


def _http_get(url: str, timeout: float = 5) -> Tuple[str, str, int]:
    try:
        opener = build_opener()
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 GEO-share-verify",
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            },
        )
        with opener.open(req, timeout=timeout) as resp:
            final_url = str(resp.geturl() or url)
            body = resp.read(400000).decode("utf-8", errors="replace")
            return body, final_url, int(getattr(resp, "status", 200) or 200)
    except Exception as exc:
        return str(exc), url, 0


def _open_fresh_context(
    browser: Browser,
    url: str,
    snippets: Optional[List[str]] = None,
    timeout_sec: float = 15,
) -> Tuple[str, str]:
    context = browser.new_context()
    page = None
    text = ""
    from cn_ai.diagnostics import current
    diagnostic = current()
    try:
        deadline = time.monotonic()+timeout_sec
        # Anonymous verification needs text and scripts, not image/media/font
        # downloads competing with the authenticated conversation's browser.
        context.route("**/*", lambda route: route.abort() if route.request.resource_type in
                      ("image", "media", "font") else route.continue_())
        page = context.new_page()
        if diagnostic:
            diagnostic.bind_page(page)
        response = page.goto(url, wait_until="commit", timeout=max(100,min(10000, int((deadline-time.monotonic())*1000))))
        if response is not None and response.status >= 400:
            raise RuntimeError("public_share_http_failed")
        # Public SPAs paint shared messages asynchronously. Poll for this
        # answer instead of accepting the initial app shell.
        slow_public_spa = any(
            host in url
            for host in (
                "kimi.",
                "doubao.com",
                "qianwen.my.cn",
                "wenxin.baidu.com",
                "chat.baidu.com",
                "mr.baidu.com",
            )
        )
        if not slow_public_spa:
            deadline = min(deadline, time.monotonic()+2.5)
        text = ""
        while True:
            try:
                text = page.evaluate(
                    """() => (document.body && document.body.innerText) || ''"""
                ) or ""
            except Exception:
                if time.monotonic() >= deadline:
                    break
                page.wait_for_timeout(100)
                continue
            if _is_login_wall(page.url or url, text):
                break
            if snippets and _snippets_visible(snippets, text):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            page.wait_for_timeout(min(500, remaining*1000))
        return text, page.url or url
    finally:
        if diagnostic and page is not None:
            try:
                diagnostic.public_share_checkpoint(page, bool(snippets and _snippets_visible(snippets,text)))
            except Exception:
                pass
        context.close()
