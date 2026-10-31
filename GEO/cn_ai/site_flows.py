# -*- coding: utf-8 -*-
"""各站点 CDP 研究流程：发测试问题 + 等待回答。"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from playwright.sync_api import Browser, Page, TimeoutError as PwTimeout
from cn_ai import budget
from cn_ai.search_policy import outbound_prompt

TEST_PROMPT = "求解器厂商推荐？"

# URL 子串匹配（登录后可能跳转）
SITE_URL_PATTERNS: Dict[str, List[str]] = {
    "deepseek": ["chat.deepseek.com"],
    "doubao": ["doubao.com"],
    "yiyan": ["wenxin.baidu.com", "yiyan.baidu.com", "chat.baidu.com"],
    "yuanbao": ["yuanbao.tencent.com"],
    "qianwen": ["qianwen.com", "tongyi.aliyun.com"],
    "kimi": ["kimi.com", "kimi.moonshot.cn", "moonshot.cn"],
}


def page_answers(page: Page) -> bool:
    """Unfreeze a discarded tab and report whether its script context answers."""
    session = None
    try:
        session = page.context.new_cdp_session(page)
        session.send("Page.setWebLifecycleState", {"state": "active"})
    except Exception:
        pass
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
    try:
        # Page.evaluate does not accept timeout. It raised TypeError even for
        # healthy tabs, forcing Yuanbao reloads and closing reusable site tabs.
        handle = page.wait_for_function("() => true", timeout=3000, polling=100)
        handle.dispose()
        return True
    except Exception:
        return False


def close_site_targets(browser: Browser, site_id: str) -> None:
    """Close a site tab whose renderer no longer accepts commands."""
    patterns = SITE_URL_PATTERNS.get(site_id, [])
    try:
        session = browser.new_browser_cdp_session()
        info = session.send("Target.getTargets")
    except Exception:
        return
    for item in info.get("targetInfos") or []:
        if item.get("type") != "page":
            continue
        url = item.get("url") or ""
        if any(pattern in url for pattern in patterns):
            try:
                session.send("Target.closeTarget", {"targetId": item["targetId"]})
            except Exception:
                pass


class PlatformFlowError(RuntimeError):
    """平台明确拒绝本次请求，不应继续等待回答。"""

    def __init__(self, error_code: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


def deepseek_mute_message(text: str) -> str:
    """DeepSeek hides the composer while an account is muted."""
    found = re.search(
        r"禁言至\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2}):(\d{2})", text or ""
    )
    if not found:
        return ""
    year, month, day, hour, minute = found.groups()
    return f"DeepSeek 账号已被禁言至 {year}-{int(month):02d}-{int(day):02d} {int(hour):02d}:{minute}，暂停使用该账号"


def classify_platform_failure(
    site_id: str, visible_text: str, resource_urls: Optional[List[str]] = None
) -> Optional[Tuple[str, str, bool]]:
    """将页面或网络中的明确失败信号转换成稳定的业务错误。"""
    text = visible_text or ""
    urls = [str(url).lower() for url in (resource_urls or [])]
    if site_id == "yuanbao" and re.sub(r"\s+", "", text).rstrip("。.") == (
        "此模型的本月用量已用完，次月1日零点刷新，可切换至专家模式，使用Hy3模型继续对话"
    ):
        return "platform_quota", "元宝此模型的本月用量已用完，次月1日零点刷新", False
    if site_id == "deepseek" and text.strip() == "服务器暂时不可用":
        return "platform_busy", "DeepSeek 服务器暂时不可用，请稍后重试", True
    if site_id == "yiyan" and "提交失败" in re.sub(r"\s+", "", text):
        return "platform_busy", "文心提交失败，请稍后重试", True
    if site_id == "kimi" and (
        "额度已用尽" in text or "免费额度已用完" in text or "额度已用完" in text
    ):
        reset = re.search(r"(\d{1,2}-\d{1,2})\s*(?:重置|刷新|恢复)", text)
        hint = f"，平台提示 {reset.group(1)} 重置" if reset else "，请等待额度重置后再试"
        return "platform_quota", "Kimi 免费额度已用完" + hint, False
    if site_id == "kimi" and (
        "和Kimi聊天的人太多了" in text
        or "订阅会员可进入优先队列" in text
    ):
        return "platform_busy", "Kimi 当前请求过多，请稍后重试", True
    if site_id == "qianwen":
        punished = any(
            "_____tmd_____/punish" in url
            or ("/punish" in url and "action=captcha" in url)
            for url in urls
        )
        challenged = any(
            marker in text
            for marker in ("请完成安全验证", "请拖动滑块", "滑动滑块完成验证")
        )
        if punished or challenged:
            return "platform_risk_control", "千问触发安全验证，请完成验证后重试", False
    if site_id == "doubao":
        challenged = any(
            "verifycenter/captcha" in url
            or "verify_scene=doubao_message_web" in url
            or "subtype=semantic_reasoning" in url
            for url in urls
        ) or any(
            line.strip().rstrip("！!。:：") in (
                "请完成安全验证", "安全验证", "请完成下方验证"
            )
            for line in text.splitlines()
        )
        if challenged:
            return (
                "platform_risk_control",
                "豆包触发语义安全验证，请人工完成验证后重试",
                False,
            )
        if "算力紧张" in text and ("办公能力得晚点" in text or "高峰期优先使用" in text):
            return "platform_busy", "豆包算力紧张，请刷新后重试", True
    return None


def detect_platform_failure(
    page: Page,
    site_id: str,
    resource_urls: Optional[Callable[[], List[str]]] = None,
) -> None:
    """读取轻量页面快照；发现明确拒绝时直接中止等待。"""
    from cn_ai.web_recovery import snapshot as recovery_snapshot
    current = recovery_snapshot(page, site_id)
    if current.get("challenge_visible") is True:
        raise PlatformFlowError("platform_risk_control", "平台触发安全验证，保留当前页面等待恢复", False)
    if site_id not in ("kimi", "qianwen", "doubao", "deepseek"):
        return
    snapshot = page.evaluate(
        """(siteId) => {
          const body = document.body;
          const text = (body && (body.innerText || body.textContent)) || '';
          const visible = node => {
            const r = node.getBoundingClientRect();
            if (r.width <= 2 || r.height <= 2 ||
                ['hidden', 'collapse'].includes(getComputedStyle(node).visibility)) return false;
            for (let current = node; current; current = current.parentElement) {
              const s = getComputedStyle(current);
              if (s.display === 'none' || Number(s.opacity || 1) === 0) return false;
            }
            return true;
          };
          const doubaoCopy = [];
          const kimiCopy = [];
          if (siteId === 'kimi' && body) {
            const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
            for (let node = walker.nextNode(); node; node = walker.nextNode()) {
              if (!/额度已用尽|免费额度已用完|额度已用完|和Kimi聊天的人太多了|订阅会员可进入优先队列/.test(node.textContent || '')) continue;
              const parent = node.parentElement;
              if (!parent || !visible(parent) || parent.closest(
                '.segment-assistant, .segment-user, .markdown-container, .thinking-container, ' +
                '[data-message-role], [data-message-id], [data-role="user"], [data-role="assistant"], ' +
                'aside, nav, textarea, [contenteditable="true"], [role="textbox"]'
              )) continue;
              const alert = parent.closest('[role="alert"], [role="dialog"], [aria-modal="true"], ' +
                '[class*="toast"], [class*="notice"], [class*="quota"], [class*="banner"]');
              // A short standalone refusal is also used outside a modal. Never
              // search the entire conversation for a quota phrase.
              const container = alert || parent;
              const copy = (container.innerText || '').trim();
              if (copy.length <= 500) kimiCopy.push(copy);
            }
          }
          if (siteId === 'doubao' && body) {
            const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
            for (let node = walker.nextNode(); node; node = walker.nextNode()) {
              const copy = (node.textContent || '').trim().replace(/[！!。:：]+$/, '');
              const busyParent=node.parentElement;
              if(copy.length<180 && copy.includes('算力紧张') &&
                  (copy.includes('办公能力得晚点')||copy.includes('高峰期优先使用')) &&
                  busyParent&&visible(busyParent)&&!busyParent.closest(
                    '[data-testid="message_text_content"],[data-message-role="user"],textarea,[contenteditable="true"]')) {
                doubaoCopy.push(copy);
                continue;
              }
              if (!['请完成安全验证', '安全验证', '请完成下方验证'].includes(copy)) continue;
              const parent = node.parentElement;
              if (!parent || !visible(parent) || parent.closest(
                '[data-testid="message_text_content"], [data-message-id], ' +
                '[data-message-role], [data-role="user"], [data-role="assistant"], ' +
                'textarea, [contenteditable="true"], [role="textbox"]'
              )) continue;
              // A sidebar/history label can itself be called 安全验证. Only
              // a challenge container makes that generic heading a blocker.
              if (copy === '安全验证' && !parent.closest(
                '[role="dialog"], [aria-modal="true"], [class*="captcha"], ' +
                '[id*="captcha"], [class*="verify"], [id*="verify"]'
              )) continue;
              doubaoCopy.push(copy);
            }
          }
          const lastItem = [...document.querySelectorAll('[data-virtual-list-item-key]')].pop();
          const deepseekError = lastItem && lastItem.querySelector('.ds-button--warning') &&
            [...lastItem.querySelectorAll('span')].find(node => {
              const r = node.getBoundingClientRect(), s = getComputedStyle(node);
              return !node.closest('.ds-message') &&
                (node.innerText || '').trim() === '服务器暂时不可用' &&
                r.width > 2 && r.height > 2 && s.display !== 'none' && s.visibility !== 'hidden';
            });
          let deepseekMuted = '';
          if (siteId === 'deepseek' && body) {
            const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
            for (let node = walker.nextNode(); node && !deepseekMuted; node = walker.nextNode()) {
              const parent = node.parentElement;
              if (!(node.textContent || '').includes('禁言至') || !parent || !visible(parent) ||
                  parent.closest('.ds-message, .ds-markdown, textarea, [contenteditable="true"]')) continue;
              deepseekMuted = (parent.innerText || node.textContent || '').trim().slice(0, 200);
            }
          }
          return {
            deepseek_error: deepseekError ? '服务器暂时不可用' : '',
            deepseek_muted: deepseekMuted,
            doubao_challenge_text: [...new Set(doubaoCopy)].join(String.fromCharCode(10)),
            text: text.slice(0, 4000) + text.slice(-4000),
            kimi_notice_text: [...new Set(kimiCopy)].join(String.fromCharCode(10)),
            urls: (performance.getEntriesByType('resource') || [])
              .slice(-150).map(x => x.name || ''),
            challenge_urls: [...document.querySelectorAll('iframe')]
              .filter(visible)
              .map(frame => frame.src || '')
          };
        }""",
        site_id,
    )
    if site_id == "deepseek":
        muted = deepseek_mute_message(str((snapshot or {}).get("deepseek_muted") or ""))
        if muted:
            raise PlatformFlowError("platform_quota", muted, False)
    if site_id in ("doubao", "qianwen"):
        # Performance entries remain after a challenge is solved. Only the
        # currently visible verification frame represents an active blocker.
        urls = list((snapshot or {}).get("challenge_urls", (snapshot or {}).get("urls", [])) or [])
    else:
        urls = list((snapshot or {}).get("urls") or [])
    if resource_urls and site_id not in ("doubao", "qianwen"):
        urls.extend(resource_urls())
    failure = classify_platform_failure(
        site_id,
        str((snapshot or {}).get(
            "deepseek_error" if site_id == "deepseek" else
            "kimi_notice_text" if site_id == "kimi" else
            "doubao_challenge_text" if site_id == "doubao" else "text"
        ) or ""),
        urls,
    )
    if failure:
        raise PlatformFlowError(*failure)


# 豆包首屏会先画出「登录」，账号芯片晚一秒到数秒才出现。
# 这段时间内不能把页面判成未登录。
DOUBAO_AUTH_SETTLE_SEC = 3.0

_DOUBAO_AUTH_JS = """() => {
  const text = document.body ? (document.body.innerText || '') : '';
  if (/用户[A-Za-z0-9]{4,}/.test(text)) return 'logged_in';
  if (/未登录|请登录后|手机验证码登录/.test(text)) return 'logged_out';
  if ((location.href || '').indexOf('from_logout=1') >= 0) return 'logged_out';
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.display !== 'none' &&
      s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
  };
  const login = [...document.querySelectorAll('button,a,[role=button]')]
    .some(n => visible(n) && ['登录', '登录 / 注册'].includes((n.innerText || '').trim()));
  return login ? 'logged_out' : 'unknown';
}"""


def doubao_page_auth(page: Page) -> str:
    try:
        state = page.evaluate(_DOUBAO_AUTH_JS)
    except Exception:
        if "from_logout=1" in (getattr(page, "url", "") or ""):
            return "logged_out"
        return "unknown"
    if state in ("logged_in", "logged_out", "unknown"):
        return state
    return "unknown"


def wait_doubao_auth(page: Page, timeout: Optional[float] = None) -> str:
    """Return logged_in as soon as the account chip paints.

    A login button on the first paint is the logged-out shell only after the
    chip has had time to appear. Otherwise a live session is failed.
    """
    settle = DOUBAO_AUTH_SETTLE_SEC if timeout is None else max(0.0, timeout)
    deadline = time.monotonic() + settle
    last = "unknown"
    while True:
        last = doubao_page_auth(page)
        if last == "logged_in":
            return last
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return last if last == "logged_out" else "unknown"
        try:
            page.wait_for_timeout(max(1, int(min(0.4, remaining) * 1000)))
        except Exception:
            time.sleep(min(0.4, remaining))


def detect_logged_out(page: Page, site_id: str) -> None:
    """Catch auth loss that appears only after navigating to a fresh chat."""
    if site_id == "deepseek":
        if "/sign_in" in (page.url or ""):
            raise PlatformFlowError(
                "platform_auth_unavailable", "平台登录已失效，请重新登录", True
            )
        return
    if site_id == "doubao":
        if doubao_page_auth(page) != "logged_out":
            return
        if wait_doubao_auth(page, 2.0) == "logged_out":
            raise PlatformFlowError(
                "platform_auth_unavailable", "平台登录已失效，请重新登录", True
            )
        return
    if site_id != "yuanbao":
        return
    try:
        # A navigation in progress destroys the context. That is not a logout.
        logged_out = page.evaluate(
            """() => {
              const visible = el => {
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                return r.width > 2 && r.height > 2 && s.display !== 'none' &&
                  s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
              };
              const nodes = document.querySelectorAll('button,a,[role=button],textarea,[contenteditable=true]');
              const limit = Math.min(nodes.length, 40);
              for (let i = 0; i < limit; i++) {
                const n = nodes[i];
                if (!visible(n)) continue;
                const s = ((n.innerText || '') + '').replace(/\\s+/g, ' ').trim();
                if (s === '登录' || s === '登录 / 注册') return true;
                const placeholder = n.getAttribute('placeholder') || n.getAttribute('data-placeholder') || '';
                if (/请登录后/.test(placeholder)) return true;
              }
              return false;
            }""",
        )
    except Exception:
        return
    if logged_out:
        raise PlatformFlowError(
            "platform_auth_unavailable", "平台登录已失效，请重新登录", True
        )


def kimi_busy_refresh_limit() -> int:
    """一次 Kimi 提问遇到繁忙弹窗时，允许刷新当前页的次数。"""
    raw = os.environ.get("GEO_KIMI_BUSY_REFRESH_LIMIT", "10").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 10
    if value < 0:
        return 0
    if value > 10:
        return 10
    return value


def dismiss_stale_kimi_busy_dialog(page: Page) -> bool:
    """A capacity dialog from the previous job must not fail the next job."""
    try:
        dialog = page.locator("[data-testid='confirm-dialog']").first
        if dialog.count() == 0 or not dialog.is_visible():
            return False
        if "和Kimi聊天的人太多了" not in (dialog.inner_text() or ""):
            return False
        cancel = dialog.locator("[data-testid='confirm-dialog-cancel']").first
        if cancel.count() > 0 and cancel.is_visible():
            cancel.click(timeout=2000, force=True)
            time.sleep(0.5)
            return True
    except Exception:
        pass
    return False


def find_input(page: Page) -> Any:
    """查找可输入元素（textarea / contenteditable / input）。"""
    selectors = [
        ".chat-input-editor",
        "textarea",
        "div[contenteditable='true']",
        "[contenteditable='true']",
        "textarea[placeholder]",
        "div[role='textbox']",
        "#chat-input",
        ".chat-input textarea",
        "[data-testid='chat-input']",
        "[data-placeholder*='提问']",
    ]
    for sel in selectors:
        loc = page.locator(sel + ":visible").first
        if loc.count() > 0 and loc.is_visible():
            return loc
    return None


def find_send_button(page: Page, *, allow_last_button: bool = True) -> Any:
    """查找发送按钮。"""
    selectors = [
        "#flow-end-msg-send",
        "button[data-testid='chat_input_send_button']",
        "#ci-submit-button-ai.ci-submit-button-ai-active",
        ".ci-submit-button:has(.ci-submit-button-ai-active)",
        ".send-button-container:not(.disabled)",
        "#yuanbao-send-btn",
        "[data-new-input-control='send'] [aria-label='发送']",
        "button[data-testid='send-button']",
        "button[type='submit']",
        "[data-testid='send-button']",
        "button:has-text('发送')",
        "button:has-text('Send')",
        "div[role='button']:has-text('发送')",
        "button.send-btn",
        "button[class*='send']",
        ".send-button",
    ]
    for sel in selectors:
        loc = page.locator(sel + ":visible").first
        if loc.count() > 0 and loc.is_visible():
            return loc
    # 最后一个可见 button 靠近输入框（兜底）
    if not allow_last_button:
        return None
    buttons = page.locator("button:visible")
    if buttons.count() > 0:
        return buttons.last
    return None


def _dismiss_overlays(page: Page) -> None:
    for _ in range(3):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.15)


def probe_dom(page: Page) -> Dict[str, Any]:
    """探测页面关键 DOM 结构。"""
    return page.evaluate(
        """() => {
        const inputs = [...document.querySelectorAll('textarea, [contenteditable=true], [role=textbox]')]
          .filter(el => el.offsetParent !== null)
          .slice(0, 5)
          .map(el => ({
            tag: el.tagName,
            id: el.id,
            class: (el.className || '').toString().slice(0, 120),
            placeholder: el.getAttribute('placeholder') || '',
            role: el.getAttribute('role') || '',
          }));
        const buttons = [...document.querySelectorAll('button, [role=button]')]
          .filter(el => el.offsetParent !== null)
          .slice(0, 15)
          .map(el => ({
            tag: el.tagName,
            id: el.id,
            text: (el.innerText || '').trim().slice(0, 40),
            class: (el.className || '').toString().slice(0, 80),
          }));
        const links = [...document.querySelectorAll('a[href^=http]')]
          .slice(0, 30)
          .map(a => a.href);
        return { inputs, buttons, sample_links: links };
    }"""
    )


def kimi_assistant_length(page: Page) -> int:
    try:
        length = page.evaluate(
            """() => {
              const nodes = [...document.querySelectorAll('.segment.segment-assistant .markdown-container')];
              const last = nodes.filter(node => !node.closest('.thinking-container, .toolcall-container, .toolcall-flow')).pop();
              return last ? (last.innerText || '').trim().length : 0;
            }"""
        )
        return int(length or 0)
    except Exception:
        return 0


def kimi_reply_started(page: Page) -> bool:
    """A Kimi chat is visible by its URL or by assistant markdown."""
    url = ""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    return "/chat/" in url or "chat_id" in url or kimi_assistant_length(page) > 20


def kimi_reply_advanced(page: Page, url_before: str, text_before: int) -> bool:
    """The send moved Kimi off the page it was on, or grew the assistant text."""
    url = ""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if url and url != url_before and ("/chat/" in url or "chat_id" in url):
        return True
    return kimi_assistant_length(page) > text_before + 20


def kimi_generation_active(page: Page) -> bool:
    """Observe the real stop control without waiting for the first text."""
    try:
        return page.evaluate("""() => [...document.querySelectorAll('.send-button-container.stop')].some(e=>{
          const r=e.getBoundingClientRect(),s=getComputedStyle(e);
          return r.width>2 && r.height>2 && s.display!=='none' && s.visibility!=='hidden';
        })""") is True
    except Exception:
        return False


def _kimi_submission_state(page: Page) -> Dict[str, Any]:
    """Read acknowledgement evidence in one browser round trip."""
    return page.evaluate("""() => {
      const nodes = [...document.querySelectorAll('.segment.segment-assistant .markdown-container')];
      const last = nodes.filter(n => !n.closest('.thinking-container, .toolcall-container, .toolcall-flow')).pop();
      const generating = [...document.querySelectorAll('.send-button-container.stop')].some(e => {
        const r = e.getBoundingClientRect(), s = getComputedStyle(e);
        return r.width > 2 && r.height > 2 && s.display !== 'none' && s.visibility !== 'hidden';
      });
      return {url: location.href, length: last ? (last.innerText || '').trim().length : 0, generating};
    }""")


def wait_for_answer_growth(
    page: Page,
    timeout_sec: float = 90,
    site_id: str = "",
    resource_urls: Optional[Callable[[], List[str]]] = None,
    on_completed: Optional[Callable[[str], None]] = None,
    failure_check: Optional[Callable[[], None]] = None,
) -> str:
    """等待回答区域文本增长并稳定。"""
    last_text = ""
    stable = 0
    stable_since = 0.0
    generating = False
    deadline = time.time() + budget.remaining(timeout_sec)
    def pause(seconds):
        duration = min(seconds, max(0, deadline-time.time()), budget.remaining())
        if duration <= 0:
            return
        if hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(duration*1000)
        else:
            budget.sleep(duration)
    saw_chat_url = False
    pending_failure: Optional[PlatformFlowError] = None
    risk_first_seen: Optional[float] = None
    while time.time() < deadline:
        completed = False
        try:
            if failure_check:
                failure_check()
            from cn_ai.diagnostics import current
            diagnostic = current()
            if diagnostic:
                diagnostic.checkpoint(page)
            detect_logged_out(page, site_id)
            detect_platform_failure(page, site_id, resource_urls)
            pending_failure = None
            risk_first_seen = None
            if site_id == "kimi" and kimi_reply_started(page):
                saw_chat_url = True
            snapshot = page.evaluate(
                """(siteId) => {
                const readText = () => {
                if (siteId === 'kimi') {
                  const assistant = [...document.querySelectorAll('.segment.segment-assistant')].pop();
                  const scope = assistant || document.querySelector('.chat-content, main') || document.body;
                  const blocks = [...scope.querySelectorAll('.markdown-container')].filter(node => {
                    if (node.classList.contains('toolcall-content-text')) return false;
                    if (node.closest('.thinking-container, .toolcall-container, .toolcall-flow')) return false;
                    return (node.innerText || '').trim().length > 20;
                  });
                  const last = blocks.pop();
                  return last ? (last.innerText || '').trim() : '';
                }
                if (siteId === 'deepseek') {
                  const nodes = [...document.querySelectorAll('.ds-markdown.ds-assistant-message-main-content')];
                  const last = nodes[nodes.length - 1];
                  if (last) return (last.innerText || '').trim();
                }
                if (siteId === 'doubao') {
                  const nodes = [...document.querySelectorAll('[data-testid="message_text_content"]')];
                  const replies = nodes.filter(n =>
                    !n.querySelector('[datatype="query-source-paragraph-start"]')
                  );
                  const last = replies[replies.length - 1];
                  return last ? (last.innerText || '').trim() : '';
                }
                if (siteId === 'yuanbao') {
                  const last=[...document.querySelectorAll('[data-conv-speaker]')].pop();
                  return last && last.getAttribute('data-conv-speaker')==='ai' ? (last.innerText||'').trim() : '';
                }
                const main = document.querySelector('main') || document.body;
                return (main.innerText || '').slice(-3000);
                };
                const visible = el => {
                  if (!el) return false;
                  const r=el.getBoundingClientRect(),s=getComputedStyle(el);
                  return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none';
                };
                let generating=false,completed=false;
                if (siteId==='kimi') {
                  generating=[...document.querySelectorAll('.send-button-container.stop')].some(visible);
                  const reply=[...document.querySelectorAll('.segment.segment-user,.segment.segment-assistant')].pop();
                  completed=!!reply && reply.classList.contains('segment-assistant') && [...reply.querySelectorAll('.segment-assistant-actions')].some(visible);
                } else if (siteId==='doubao') {
                  const replies=[...document.querySelectorAll('[data-testid="message_text_content"]')]
                    .filter(n=>!n.querySelector('[datatype="query-source-paragraph-start"]'));
                  const reply=replies.pop();
                  const root=reply && (reply.closest('[data-message-id]') || reply);
                  const blocks=root ? [...root.querySelectorAll('[data-streaming]')] : [];
                  generating=blocks.some(n=>visible(n) && n.getAttribute('data-streaming')==='true');
                  // A completed block may still precede another tool/answer
                  // block. Retain text stability as the message-level check.
                } else if (siteId==='yuanbao') {
                  const last=[...document.querySelectorAll('[data-conv-speaker]')].pop();
                  const reply=last && last.getAttribute('data-conv-speaker')==='ai' ? last : null;
                  const status=reply && (reply.getAttribute('data-conv-status')||'').toLowerCase();
                  const outputting=reply && reply.getAttribute('data-conv-outputting');
                  const stop=[...document.querySelectorAll('[class*="stop"],[aria-label*="停止"]')].some(visible);
                  generating=(!!status && status!=='finished') || outputting==='true' || stop;
                  const copy=reply && [...reply.querySelectorAll(
                    '.agent-chat__toolbar__right .agent-chat__question-toolbar__copy-wrapper,'+
                    '.agent-chat__toolbar__right [class*="ToolbarCopy_copyIconWrap"]')].some(visible);
                  completed=!!reply && visible(reply) && !generating &&
                    (status==='finished' || (outputting==='false' && copy));
                }
                return {text:readText(),generating,completed};
            }""",
                site_id,
            )
            text = snapshot.get("text", "") if isinstance(snapshot, dict) else snapshot
            generating = isinstance(snapshot, dict) and snapshot.get("generating") is True
            completed = isinstance(snapshot, dict) and snapshot.get("completed") is True
            cur = len(text or "")
            # Kimi 必须先进入 /chat/，避免侧栏文本骗过稳定条件
            min_len = 4 if site_id == "doubao" else (20 if site_id == "kimi" else 50)
            if site_id == "kimi" and not saw_chat_url:
                stable = 0
                pause(2)
                continue
            # Kimi may pause for many seconds while a search/tool call is still
            # running. Text-length stability during that pause is not answer
            # completion and previously caused internal progress text to be
            # returned and shared with MESSAGE_STATUS_GENERATING.
            if generating or (site_id == "kimi" and _kimi_answer_in_progress(text or "")):
                stable = 0
                stable_since = 0.0
                last_text = text or ""
                pause(.5 if generating else 2)
                continue
            if cur > min_len and text == last_text:
                stable += 1
                if not stable_since:
                    stable_since = time.monotonic()
                need = 5 if site_id == "kimi" else 4
                if (completed and time.monotonic()-stable_since >= 1) or (site_id not in ("yuanbao", "kimi") and not completed and stable >= need):
                    completion = "dom_state" if completed else "text_stability"
                    from cn_ai.diagnostics import current
                    diagnostic = current()
                    if diagnostic:
                        diagnostic.meta["completion_detection"] = completion
                    if on_completed:
                        if on_completed(completion) is False:
                            stable = 0
                            pause(1)
                            continue
                    return text
            else:
                stable = 0
                stable_since = 0.0
            last_text = text or ""
        except PlatformFlowError as exc:
            if site_id == "doubao" and exc.error_code == "platform_risk_control":
                pending_failure = exc
                if risk_first_seen is None:
                    risk_first_seen = time.time()
                # Return while the verification frame is still likely visible,
                # so the admin screenshot can hand it to an operator.
                if time.time() - risk_first_seen >= 5:
                    raise pending_failure
                pause(2)
                continue
            raise
        except Exception:
            pass
        pause(.5 if completed else 2)
    if pending_failure:
        raise pending_failure
    if site_id in ("kimi", "doubao", "yuanbao") or generating:
        raise PlatformFlowError(
            "generation_timeout" if site_id in ("kimi", "yuanbao") else "platform_busy",
            "已发送问题，但在执行预算内尚未确认完整回答", False
        )
    return page.evaluate("() => (document.body.innerText || '').slice(-3000)")


def _kimi_reasoning_trace(text: str) -> bool:
    """Kimi's tool-call scratchpad, not the answer shown to the user."""
    sample = text or ""
    markers = (
        "User asks",
        "web_search",
        "用户要求",
        "需要联网",
        "Need to search",
        "需要搜索",
        "REF cite",
    )
    return sum(1 for marker in markers if marker in sample) >= 2


def _kimi_answer_in_progress(text: str) -> bool:
    if _kimi_reasoning_trace(text):
        return True
    compact = re.sub(r"\s+", "", text or "")
    return any(
        marker in compact
        for marker in (
            "正在思考中",
            "正在搜索中",
            "正在生成中",
        )
    )


def _record_web_search_setting(status: str, control: str = "") -> str:
    from cn_ai.diagnostics import current
    diagnostic = current()
    if diagnostic:
        diagnostic.meta["web_search_settings"] = {
            "status": status,
            "confirmed": status in ("on", "auto"),
            "control": control,
        }
        diagnostic.persist()
    return status


def _search_control_is_actionable(control) -> bool:
    """Search labels inside answers, histories and sidebar search are not settings."""
    return control.evaluate("""el => {
      if (el.closest('aside, nav, [data-testid="global-search-icon-entry"], ' +
          '[data-testid="aside-history-search-btn"], [data-message-id], [data-message-role], ' +
          '[data-testid="message_text_content"], .segment, .agent-chat__list__item, ' +
          '.answer-container, .ai-entry, .chat-content-item')) return false;
      const root=el.closest('button, [role=button], [role=switch], [role=checkbox], ' +
          '[role=menuitem], [role=menuitemcheckbox], [role=menuitemradio], ' +
          'input[type=checkbox], [aria-pressed], [aria-checked], .ds-toggle-button, .toolkit-item, .connect-item');
      if (!root || root.matches(':disabled, [aria-disabled="true"]')) return false;
      for (let node=root; node; node=node.parentElement) {
        const s=getComputedStyle(node);
        if (s.display==='none' || Number(s.opacity||1)===0) return false;
      }
      const r=root.getBoundingClientRect(), s=getComputedStyle(root);
      return r.width>2 && r.height>2 && s.visibility!=='hidden';
    }""", timeout=1000) is True


def enable_web_search(page: Page, site_id: str, *, activate: bool = True) -> str:
    """Enable an observed search switch; never substitute a prompt instruction.

    An absent independent switch is reported explicitly. Some current web UIs
    decide whether to search automatically; their model picker or history-search
    icon is not evidence that a request has used web search.
    """
    labels = ("联网搜索", "智能搜索", "网络搜索", "网页搜索", "全网搜索", "Web search", "Web Search")
    if site_id == "deepseek":
        labels += ("搜索", "Search")
    for label in labels:
        candidates = [page.get_by_role(role, name=label, exact=True)
                      for role in ("switch", "checkbox", "button")]
        candidates.append(page.get_by_text(label, exact=True))
        for group in candidates:
            for index in range(min(group.count(), 5)):
                control = group.nth(index)
                if not control.is_visible() or not _search_control_is_actionable(control):
                    continue
                state = _deepseek_toggle_state(control)
                if state == "off" and activate:
                    try:
                        control.click(timeout=2000)
                        deadline = time.monotonic() + 1.5
                        while time.monotonic() < deadline:
                            detect_platform_failure(page, site_id)
                            state = _deepseek_toggle_state(control)
                            if state in ("on", "auto"):
                                break
                            page.wait_for_timeout(100)
                    except PlatformFlowError:
                        raise
                    except Exception:
                        state = "unconfirmed"
                    if state not in ("on", "auto"):
                        _record_web_search_setting(state, label)
                        raise PlatformFlowError(
                            "platform_setting_unavailable", "联网搜索未能确认开启，已停止发送", False
                        )
                return _record_web_search_setting(state, label)
    return _record_web_search_setting("not_present")


def run_deepseek_prep(page: Page) -> str:
    """独立确认搜索与思考，不重复点击已开启或状态未知的开关。"""
    states = {"search": enable_web_search(page, "deepseek")}
    for setting, labels in (
        ("thinking", ("深度思考", "DeepThink", "DeepThink (R1)")),
    ):
        states[setting] = "not_present"
        for label in labels:
            try:
                control = page.get_by_text(label, exact=True).first
                if not control.count() or not control.is_visible():
                    continue
                state = _deepseek_toggle_state(control)
                if state == "off":
                    control.click(timeout=2000)
                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        state = _deepseek_toggle_state(control)
                        if state == "on":
                            break
                        page.wait_for_timeout(100)
                states[setting] = state
                break
            except Exception:
                states[setting] = "unconfirmed"
    from cn_ai.diagnostics import current
    diagnostic = current()
    if diagnostic:
        diagnostic.meta["deepseek_settings"] = states
    return ";".join(f"{key}:{state}" for key, state in states.items())


def _deepseek_toggle_state(control) -> str:
    return str(control.evaluate("""el => {
      const root=el.closest('[aria-pressed],[aria-checked],button,[role=button], ' +
        '[role=switch],[role=checkbox],[role=menuitemcheckbox],[role=menuitemradio], ' +
        '.ds-toggle-button,.toolkit-item,.connect-item') || el;
      if (root.matches('input[type=checkbox]')) return root.checked ? 'on' : 'off';
      for (const name of ['aria-pressed','aria-checked']) {
        const value=root.getAttribute(name);
        if (value==='true') return 'on';
        if (value==='false') return 'off';
      }
      const value=root.getAttribute('data-state');
      if (['on','checked','active','selected'].includes(value)) return 'on';
      if (['off','unchecked','inactive','unselected'].includes(value)) return 'off';
      for (const name of ['data-mode','data-value']) {
        const mode=root.getAttribute(name);
        if (['on','enabled','always','required'].includes(mode)) return 'on';
        if (['off','disabled','never'].includes(mode)) return 'off';
        if (mode==='auto') return 'auto';
      }
      const classes=typeof root.className==='string' ? root.className : '';
      if (/(?:^|[\\s_-])(active|selected|checked)(?:$|[\\s_-])/.test(classes)) return 'on';
      if (/(?:^|[\\s_-])(inactive|unselected|unchecked)(?:$|[\\s_-])/.test(classes)) return 'off';
      return 'unconfirmed';
    }""", timeout=1000))


def run_doubao_prep(page: Page) -> str:
    """Wait until the new Doubao shell finishes hydrating.

    The logged-in home can sit on skeleton chips and an empty composer for
    the whole scrape timeout. Select 对话, then reload once if no real
    editor appears.
    """
    dismiss_doubao_desktop_notice(page)
    try:
        tab = page.get_by_text("对话", exact=True)
        if tab.count() and tab.first.is_visible():
            tab.first.click(timeout=2000)
    except Exception:
        pass
    reloaded = False
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            ready = page.evaluate(
                """() => {
                  const visible = el => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 80 && r.height >= 20 && s.visibility !== 'hidden';
                  };
                  return [...document.querySelectorAll('textarea, [contenteditable="true"], div[role="textbox"]')].some(visible);
                }"""
            )
        except Exception:
            ready = False
        if ready:
            return "composer-ready" if not reloaded else "reloaded-composer-ready"
        if not reloaded and time.time() > deadline - 8:
            try:
                page.reload(wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            reloaded = True
        time.sleep(1)
    return "composer-not-ready"


def _on_site_home(page: Page, hosts: Tuple[str, ...], chat_markers: Tuple[str, ...]) -> bool:
    """True when the tab is already this site's home, not an old conversation."""
    url = page.url or ""
    if not any(host in url for host in hosts):
        return False
    return not any(marker in url for marker in chat_markers)


def start_new_chat(page: Page, site_id: str) -> str:
    """每题新开对话，避免同 tab 串题。"""
    try:
        if site_id == "doubao":
            if wait_doubao_auth(page) == "logged_out" or "from_logout=1" in (page.url or ""):
                raise PlatformFlowError(
                    "platform_auth_unavailable",
                    "豆包登录已失效，请重新登录",
                    True,
                )
            for sel in [
                "text=新对话",
                "[aria-label='新对话']",
                "a[href='/chat/']",
                "a[href='/chat']",
            ]:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=3000)
                    time.sleep(1.5)
                    return f"clicked:{sel}"
            page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=20000)
            time.sleep(1.5)
            return "goto:/chat/"
        if site_id == "deepseek":
            # 强制回首页再开新会话，避免停在旧 chat 导致 history 串题。
            # 已经在首页就不要再整页刷新。
            if not _on_site_home(page, ("chat.deepseek.com",), ("/a/chat",)):
                try:
                    page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=20000)
                    time.sleep(1.2)
                except Exception:
                    pass
            for sel in ["text=开启新对话", "text=新对话", "[data-testid='new-chat']"]:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=3000)
                    time.sleep(1)
                    return f"goto+clicked:{sel}"
            return "goto:home"
        if site_id == "kimi":
            # The observed logo/new-chat link performs client navigation.
            # Prefer it before a full load, but require the old answer to clear.
            control_failed = False
            for sel in ["[aria-label='新建会话']", "text=新建会话", "text=新对话"]:
                loc = page.locator(sel + ":visible").first
                try:
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=3000, no_wait_after=True)
                        page.wait_for_function(
                            """() => !location.pathname.startsWith('/chat/') &&
                            ![...document.querySelectorAll('.segment.segment-assistant')]
                              .some(el => el.offsetParent !== null)""",
                            timeout=3000,
                        )
                        return f"clicked:{sel}"
                except Exception:
                    control_failed = True
                    break
            # A missing or ineffective control falls back to the actual home.
            if (
                control_failed
                or not _on_site_home(page, ("kimi.com",), ("/chat/",))
                or page.locator(".segment.segment-assistant:visible").count()
            ):
                try:
                    page.goto("https://www.kimi.com/", wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(500)
                except Exception:
                    pass
            return "goto:home"
        if site_id == "yuanbao":
            new_chat = page.locator(
                "text=新对话, text=New Chat, [aria-label='新对话'], [aria-label='New Chat']"
            ).first
            try:
                new_chat.wait_for(state="visible", timeout=3000)
                new_chat.click(timeout=3000)
                time.sleep(0.6)
                return "clicked:new-chat"
            except Exception:
                pass
            page.goto("https://yuanbao.tencent.com/chat", wait_until="domcontentloaded", timeout=12000)
            time.sleep(1)
            return "goto:chat"
        if site_id == "yiyan":
            if (
                _on_site_home(page, ("wenxin.baidu.com", "yiyan.baidu.com"), ("/chat/",))
                and urlsplit(page.url or "").path in ("", "/")
            ):
                return "already:home"
            page.goto("https://wenxin.baidu.com/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1000)
            return "goto:wenxin"
        if site_id == "qianwen":
            for sel in ["text=新对话", "[aria-label='新对话']"]:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=3000)
                    time.sleep(1)
                    return f"clicked:{sel}"
            page.goto("https://www.qianwen.com/", wait_until="domcontentloaded", timeout=20000)
            time.sleep(1)
            run_qianwen_prep(page)
            return "goto:home"
    except PlatformFlowError:
        raise
    except Exception as e:
        return f"fail:{type(e).__name__}"
    return "skip"


def _dismiss_blocking_dialogs(page: Page) -> str:
    """Close a guide dialog that sits on top of the composer."""
    try:
        action = page.evaluate(
            """() => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 20 && r.height > 20 && s.display !== 'none' &&
                  s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
              };
              const dialogs = [...document.querySelectorAll(
                '.cos-dialog,[role="dialog"],[class*="guide-dialog"]'
              )].filter(visible);
              if (!dialogs.length) return '';
              const labels = ['我知道了','知道了','关闭','跳过','暂不开启','暂不','以后再说','开始体验','立即体验'];
              for (const dialog of dialogs) {
                const buttons = [...dialog.querySelectorAll('button,[role="button"],a')];
                const hit = buttons.find(n => labels.includes(
                  ((n.innerText || n.getAttribute('aria-label') || '') + '').replace(/\\s+/g, ' ').trim()
                ));
                const close = dialog.querySelector(
                  '[class*="close"],[aria-label="关闭"],[aria-label="Close"]'
                );
                const target = hit || close;
                if (target) {
                  target.click();
                  return 'clicked';
                }
              }
              for (const dialog of dialogs) {
                const root = dialog.closest('[class*="dialog"]') || dialog;
                root.remove();
              }
              document.querySelectorAll('[class*="dialog-mask"],[class*="modal-mask"]').forEach(el => el.remove());
              return 'removed';
            }"""
        )
        return str(action or "")
    except Exception:
        return ""


def run_yiyan_prep(page: Page) -> str:
    dismissed = _dismiss_blocking_dialogs(page)
    return f"dialog:{dismissed}" if dismissed else "skip"


def run_yuanbao_prep(page: Page) -> str:
    return "skip"


def ensure_yuanbao_web_search(page: Page) -> str:
    """Select the native tool after typing; focusing the editor can clear it."""
    selected = page.get_by_role('button', name=re.compile(r'^移除\s*联网搜索$'))
    def has_tool():
        return any(selected.nth(i).is_visible() for i in range(selected.count()))
    if has_tool():
        return _record_web_search_setting('on', '元宝 添加 → 联网搜索')
    add = page.get_by_role('button', name='添加', exact=True)
    if not add.count():
        add = page.get_by_label('添加', exact=True)
    if not add.count() or not add.first.is_visible():
        _record_web_search_setting('not_present', '元宝 添加 → 联网搜索')
        raise PlatformFlowError('platform_setting_unavailable', '未找到元宝添加联网搜索入口，未提交问题', False)
    add.first.click(timeout=2000)
    choice = page.get_by_text('联网搜索', exact=True).filter(visible=True)
    if choice.count() != 1:
        _record_web_search_setting('unconfirmed', '元宝 添加 → 联网搜索')
        raise PlatformFlowError('platform_setting_unavailable', '无法唯一确认元宝联网搜索选项，未提交问题', False)
    choice.click(timeout=2000)
    deadline = time.monotonic() + 2
    while not has_tool() and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    if not has_tool():
        _record_web_search_setting('unconfirmed', '元宝 添加 → 联网搜索')
        raise PlatformFlowError('platform_setting_unavailable', '未出现“移除 联网搜索”，未提交问题', False)
    return _record_web_search_setting('on', '元宝 添加 → 联网搜索')


def dismiss_qianwen_share_dialog(page: Page) -> bool:
    """Close the previous answer's share overlay before reusing its tab."""
    mask = page.locator("#share-modal-root [class*='mask-']").first
    try:
        if not mask.count() or not mask.is_visible():
            return False
        mask.click(position={"x": 4, "y": 4}, timeout=1500)
        mask.wait_for(state="hidden", timeout=1000)
        return True
    except Exception:
        # Some share dialogs ignore Escape/backdrop clicks. A normal page
        # navigation clears that UI while preserving the browser login.
        if mask.count() and mask.is_visible():
            page.goto("https://www.qianwen.com/", wait_until="domcontentloaded", timeout=12000)
            return True
        return False


def run_qianwen_prep(page: Page) -> str:
    """Dismiss only a visible guide; leave normal pages and unknown dialogs alone."""
    if dismiss_qianwen_share_dialog(page):
        return "dismissed:share"
    dialogs = page.locator('[role="dialog"]:visible').filter(has_text=re.compile(r'新功能|使用指南|欢迎使用|功能介绍'))
    for index in range(dialogs.count()):
        for label in ('我知道了', '跳过'):
            close = dialogs.nth(index).get_by_role('button', name=label, exact=True)
            if close.count() == 1 and close.is_visible():
                close.click(timeout=2000)
                return 'dismissed:guide'
    return 'skip'


def dismiss_kimi_notice(page: Page) -> bool:
    """Dismiss the observed promotion dialog using its own close action."""
    dismissed = False
    for _ in range(2):
        try:
            notice = page.locator(".common-notice-dialog").last
            if not notice.count() or not notice.is_visible():
                break
            close = notice.get_by_role("button", name="我知道了", exact=True)
            if not close.count() or not close.is_visible():
                break
            close.click(timeout=2000)
            dismissed = True
            page.wait_for_timeout(200)
        except Exception:
            break
    return dismissed


def dismiss_doubao_desktop_notice(page: Page) -> bool:
    """Use the observed desktop promotion's close button, leaving other dialogs."""
    try:
        dialogs = page.locator('[role="dialog"]:visible').filter(
            has=page.locator('[data-testid="desktop_download_guide_popup_btn"]')
        )
        legacy = page.locator('[role="dialog"]:visible').filter(
            has_text="下载豆包电脑版"
        )
        for candidates in (dialogs, legacy):
            for index in range(min(candidates.count(), 3)):
                notice = candidates.nth(index)
                close = notice.get_by_role("button", name="关闭", exact=True)
                if not close.count():
                    close = notice.get_by_role("button", name="下次提醒我", exact=True)
                if close.count() and close.first.is_visible():
                    close.first.click(timeout=2000)
                    notice.wait_for(state="hidden", timeout=2000)
                    return True
    except Exception:
        pass
    return False


def _kimi_toggle_state(control) -> Optional[bool]:
    return control.evaluate("""e => {
      for (const name of ['aria-checked', 'aria-pressed']) {
        const value = e.getAttribute(name);
        if (value === 'true' || value === 'false') return value === 'true';
      }
      if (e.tagName === 'INPUT' && e.type === 'checkbox') return e.checked;
      if (e.classList.contains('toolkit-item') && e.getAttribute('role') === 'menuitem')
        return e.classList.contains('toolkit-item--selected');
      return null;
    }""")


def disable_kimi_knowledge_base(page: Page) -> str:
    """Only click a knowledge-base toggle after observing that it is on."""
    name = re.compile(r"知识库|资料库|knowledge\s*base", re.I)
    controls = []
    for role in ("switch", "checkbox", "button"):
        group = page.get_by_role(role, name=name)
        controls.extend(group.nth(i) for i in range(group.count()) if group.nth(i).is_visible() and (role != "button" or _kimi_toggle_state(group.nth(i)) is not None))
    rows = page.locator(".toolkit-item,.connect-item").filter(has_text=name)
    for i in range(rows.count()):
        row = rows.nth(i)
        if not row.is_visible():
            continue
        nested = row.locator("[role=switch],input[type=checkbox],[aria-checked],[aria-pressed]").first
        controls.append(nested if nested.count() else row)
    state = "not_present"
    for control in controls:
        enabled = _kimi_toggle_state(control)
        if enabled is None:
            raise PlatformFlowError("platform_setting_unavailable", "Kimi 知识库开关状态无法确认，已停止发送", False)
        if enabled:
            control.click(timeout=2000)
            deadline = time.monotonic()+1
            while _kimi_toggle_state(control) is True and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            if _kimi_toggle_state(control) is not False:
                raise PlatformFlowError("platform_setting_unavailable", "Kimi 知识库未能确认关闭，已停止发送", False)
        state = "off"
    from cn_ai.diagnostics import current
    diagnostic = current()
    if diagnostic:
        diagnostic.meta["kimi_knowledge_base"] = state
        diagnostic.persist()
    return state


def run_kimi_prep(page: Page) -> str:
    """发送前关闭可见知识库开关，并按已观察状态开启联网搜索。"""
    dismissed_busy = dismiss_stale_kimi_busy_dialog(page)
    dismiss_kimi_notice(page)
    try:
        btn = page.locator(".toolkit-trigger-btn:visible").first
        if btn.count() == 0 or not btn.is_visible():
            disable_kimi_knowledge_base(page)
            search = enable_web_search(page, "kimi")
            return ("dismissed-busy;" if dismissed_busy else "") + "no-toolkit;search:" + search
        if btn.get_attribute("aria-expanded") != "true":
            btn.click(timeout=3000)
        time.sleep(0.5)
        knowledge = disable_kimi_knowledge_base(page)
        if btn.get_attribute("aria-expanded") == "false":
            btn.click(timeout=3000)
        # Current Kimi uses a radio submenu. The description is part of the
        # accessible name, and a highlighted row only means keyboard focus.
        modern = page.locator('.toolkit-panel .toolkit-item').filter(has_text='联网搜索').first
        if modern.count() and modern.is_visible():
            modern.click(timeout=2000)
            modes = page.get_by_role('menuitemradio')
            modes.first.wait_for(state='visible', timeout=2000)
            chosen, status = None, 'unconfirmed'
            for label, value in [('始终搜索', 'on'), ('总是搜索', 'on'), ('开启', 'on'), ('自动搜索', 'auto')]:
                option = page.get_by_role('menuitemradio', name=re.compile(r'^' + re.escape(label))).first
                if option.count() and option.is_visible():
                    chosen, status = option, value
                    break
            state = chosen.get_attribute('aria-checked') if chosen is not None else None
            if state == 'false':
                chosen.click(timeout=2000)
                if not chosen.is_visible():
                    if not modern.is_visible():
                        btn.click(timeout=2000)
                    modern.click(timeout=2000)
                chosen.wait_for(state='visible', timeout=2000)
            confirmed = chosen is not None and chosen.get_attribute('aria-checked') == 'true'
            page.keyboard.press('Escape')
            if not confirmed:
                _record_web_search_setting('unconfirmed', 'Kimi 联网搜索模式')
                if state in ('true', 'false'):
                    raise PlatformFlowError('platform_setting_unavailable', 'Kimi 搜索模式未能确认开启，未提交问题', False)
                return 'knowledge-base:' + knowledge + ';search:unconfirmed'
            _record_web_search_setting(status, 'Kimi 联网搜索模式')
            return 'knowledge-base:' + knowledge + ';search:' + status
        item = page.locator(".toolkit-container .toolkit-item").filter(has_text="联网搜索").first
        if item.count() == 0 or not item.is_visible():
            page.keyboard.press("Escape")
            _record_web_search_setting("not_present")
            return "no-search-item"
        # Some versions expose search modes in a submenu. Prefer an explicit
        # always/on option over auto, and verify the selected state afterward.
        try:
            item.hover(timeout=1000)
            page.wait_for_timeout(150)
        except Exception:
            pass
        modes = page.locator(".connect-popover .connect-item, .connect-container .connect-item")
        visible_modes = [modes.nth(i) for i in range(min(modes.count(), 12)) if modes.nth(i).is_visible()]
        if not visible_modes and item.get_attribute("aria-haspopup") in ("true", "menu", "listbox"):
            item.click(timeout=2000)
            page.wait_for_timeout(150)
            visible_modes = [modes.nth(i) for i in range(min(modes.count(), 12)) if modes.nth(i).is_visible()]
        if visible_modes:
            mode_labels = [(mode, mode.inner_text().strip()) for mode in visible_modes]
            enabled_names = ("始终搜索", "总是搜索", "每次搜索", "始终联网", "始终开启", "开启", "打开", "Always", "On")
            chosen = None
            status = "unconfirmed"
            for names, value in ((enabled_names, "on"), (("自动", "Auto"), "auto")):
                chosen = next((mode for name in names for mode, label in mode_labels if label == name), None)
                if chosen is not None:
                    status = value
                    break
            was_off = any(label in ("关闭", "Off") and _deepseek_toggle_state(mode) == "on"
                          for mode, label in mode_labels)
            confirmed = False
            if chosen is not None:
                try:
                    state = _deepseek_toggle_state(chosen)
                    if state == 'off':
                        chosen.click(timeout=2000)
                        page.wait_for_timeout(150)
                    confirmed = _deepseek_toggle_state(chosen) == "on"
                    if not confirmed and _kimi_toggle_state(item) is True:
                        # The row proves search is enabled, but not which
                        # submenu policy is selected after the popup closes.
                        confirmed, status = True, "on"
                except Exception:
                    confirmed = False
            _dismiss_overlays(page)
            if not confirmed:
                _record_web_search_setting("unconfirmed", "Kimi 联网搜索模式")
                if was_off:
                    raise PlatformFlowError("platform_setting_unavailable", "Kimi 联网搜索未能确认开启，已停止发送", False)
                return "knowledge-base:" + knowledge + ";search:unconfirmed"
            _record_web_search_setting(status, "Kimi 联网搜索模式")
            return "knowledge-base:" + knowledge + ";search:" + status
        current_state = _kimi_toggle_state(item)
        if current_state is not None:
            if not current_state:
                try:
                    item.click(timeout=2000)
                    deadline = time.monotonic() + 1.5
                    while _kimi_toggle_state(item) is not True and time.monotonic() < deadline:
                        page.wait_for_timeout(100)
                    current_state = _kimi_toggle_state(item)
                except Exception:
                    current_state = None
                if current_state is not True:
                    _record_web_search_setting("unconfirmed", "Kimi 联网搜索")
                    raise PlatformFlowError("platform_setting_unavailable", "Kimi 联网搜索未能确认开启", False)
            _dismiss_overlays(page)
            _record_web_search_setting("on", "Kimi 联网搜索")
            return f"knowledge-base:{knowledge};search:on"
        # 悬停展开 connect-popover（自动 / 关闭）
        try:
            item.hover(timeout=2000)
        except Exception:
            item.click(timeout=2000)
        time.sleep(0.6)
        auto = page.locator(".connect-popover .connect-item, .connect-container .connect-item").filter(
            has_text="自动"
        ).first
        if auto.count() > 0 and auto.is_visible() and _deepseek_toggle_state(auto) in ('on', 'off'):
            if _deepseek_toggle_state(auto) == 'off':
                auto.click(timeout=2000)
            time.sleep(0.4)
            confirmed = _deepseek_toggle_state(auto) == "on"
            _dismiss_overlays(page)
            _record_web_search_setting("auto" if confirmed else "unconfirmed", "Kimi 联网搜索模式")
            return "search:auto" if confirmed else "search:unconfirmed"
        # Without an observable state, clicking could turn an enabled switch
        # off. Leave it unchanged and report the limitation.
        _dismiss_overlays(page)
        _record_web_search_setting("unconfirmed", "Kimi 联网搜索")
        return "search:unconfirmed"
    except PlatformFlowError:
        raise
    except Exception as e:
        _dismiss_overlays(page)
        _record_web_search_setting("unconfirmed", "Kimi 联网搜索")
        return f"search-fail:{type(e).__name__}"


SITE_PREP = {
    "deepseek": run_deepseek_prep,
    "doubao": run_doubao_prep,
    "yiyan": run_yiyan_prep,
    "yuanbao": run_yuanbao_prep,
    "qianwen": run_qianwen_prep,
    "kimi": run_kimi_prep,
}


def _send_progress(step: str) -> None:
    from cn_ai.diagnostics import current
    diagnostic = current()
    if diagnostic:
        diagnostic.browser_progress(step)


def _wait_kimi_composer(page: Page):
    """Allow the authenticated shell to hydrate before using its controls."""
    from cn_ai.web_recovery import snapshot as recovery_snapshot
    deadline = time.monotonic() + budget.remaining(25, reserve=25)
    while True:
        input_el = find_input(page)
        if input_el is not None:
            _send_progress("composer_ready")
            return input_el
        # A shell with no editor is not an accepted question. Keep the
        # journal before_send so this case may safely use another account.
        observed = recovery_snapshot(page, "kimi")
        if observed.get("logged_out"):
            raise PlatformFlowError("platform_auth_unavailable", "Kimi 登录已失效，未提交问题", True)
        detect_platform_failure(page, "kimi")
        if time.monotonic() >= deadline:
            raise PlatformFlowError("platform_busy", "Kimi 页面输入框仍未加载，未提交问题", True)
        budget.sleep(.5)


def send_prompt(page: Page, prompt: str, site_id: str, new_chat: bool = True,
                on_send_state: Optional[Callable[[str], None]] = None) -> Tuple[str, str]:
    """输入并发送 prompt。"""
    if on_send_state:
        detect_platform_failure(page, site_id)
    prep_parts = []
    if site_id == "kimi" and dismiss_kimi_notice(page):
        prep_parts.append("dismissed:notice")
    if site_id == "doubao" and dismiss_doubao_desktop_notice(page):
        prep_parts.append("dismissed:desktop-notice")
    if site_id == "qianwen" and dismiss_qianwen_share_dialog(page):
        prep_parts.append("dismissed:share")
    if new_chat:
        new_chat_result = start_new_chat(page, site_id)
        prep_parts.append(new_chat_result)
        if site_id == "yiyan" and new_chat_result.startswith("fail:"):
            raise PlatformFlowError(
                "platform_busy", "文心新对话页面未就绪，请稍后重试", True
            )
    if site_id == "kimi":
        _wait_kimi_composer(page)
    prep_fn = SITE_PREP.get(site_id, lambda p: "skip")
    prep_parts.append(prep_fn(page))
    detect_logged_out(page, site_id)
    if site_id in ("doubao", "yiyan", "qianwen"):
        prep_parts.append("search:" + enable_web_search(page, site_id))
    prep = ";".join(prep_parts)
    if site_id == "doubao" and "composer-not-ready" in prep:
        raise PlatformFlowError(
            "platform_busy",
            "豆包页面还在加载，请稍后重试",
            True,
        )
    send_text = outbound_prompt(site_id, prompt)
    if site_id != 'qianwen':
        _dismiss_overlays(page)
    input_el = find_input(page)
    if not input_el and site_id == "deepseek":
        detect_platform_failure(page, site_id)
    if not input_el:
        ready = time.time() + 12
        while time.time() < ready and not input_el:
            time.sleep(0.5)
            input_el = find_input(page)
    if not input_el:
        href = ""
        try:
            href = (page.url or "").split("?")[0][:120]
        except Exception:
            href = ""
        raise RuntimeError(f"未找到输入框 url={href}")
    try:
        input_el.click(timeout=5000)
    except PwTimeout:
        if site_id == "yiyan":
            _dismiss_blocking_dialogs(page)
            input_el.click(timeout=5000, force=True)
        elif site_id == "kimi":
            if dismiss_kimi_notice(page):
                input_el.click(timeout=2000)
            else:
                raise PlatformFlowError(
                    "platform_busy", "Kimi 输入框暂不可用，按平台繁忙重试", True,
                )
        elif site_id == "doubao" and dismiss_doubao_desktop_notice(page):
            input_el.click(timeout=2000)
        else:
            raise
    _send_progress("composer_focused")
    time.sleep(0.3)
    # 清空并输入（Kimi contenteditable 上 fill 常失败，优先键盘）
    try:
        page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
        page.keyboard.press("Backspace")
    except Exception:
        pass

    typed_ok = False
    if site_id != "kimi":
        try:
            input_el.fill(send_text, timeout=5000)
            typed_ok = True
        except Exception:
            typed_ok = False
    if not typed_ok:
        # Pasting literal text does not turn a newline in the original prompt
        # into an Enter key that submits Kimi's composer prematurely.
        page.keyboard.insert_text(send_text)
    time.sleep(0.4)
    # 确认输入框有内容
    try:
        cur = input_el.evaluate("el => 'value' in el ? el.value : (el.innerText || el.textContent || '')")
        if not cur and send_text:
            input_el.click(timeout=3000)
            page.keyboard.insert_text(send_text)
            time.sleep(0.3)
            cur = input_el.evaluate("el => 'value' in el ? el.value : (el.innerText || el.textContent || '')")
        if site_id in ('kimi', 'qianwen') and ' '.join(str(cur or '').split()) != ' '.join(send_text.split()):
            raise PlatformFlowError("platform_busy", "输入内容未确认，未提交问题", True)
    except PlatformFlowError:
        raise
    except Exception:
        if site_id == "kimi":
            raise PlatformFlowError("browser_unresponsive", "Kimi 输入框未响应内容核实，未提交问题", True)

    _send_progress("prompt_entered")

    if site_id == 'yuanbao':
        prep += ';search:' + ensure_yuanbao_web_search(page)

    method = "enter"
    if on_send_state:
        on_send_state("sending")
    if site_id == "kimi":
        url_before = ""
        try:
            url_before = page.url or ""
        except Exception:
            url_before = ""
        before = _kimi_submission_state(page)
        text_before = before["length"]
        generating_before = before["generating"]
        refresh_limit = kimi_busy_refresh_limit()
        refreshes = 0
        unconfirmed_retry = False
        while True:
            detect_platform_failure(page, site_id)
            send = page.locator(".send-button-container:not(.disabled)").first
            attempt_suffix = "" if refreshes == 0 else f"-refresh-{refreshes}"
            if send.count() > 0 and send.is_visible():
                clicked = False
                try:
                    send.click(timeout=2500, no_wait_after=True)
                    clicked = True
                    _send_progress("send_clicked")
                except PwTimeout:
                    raise PlatformFlowError("recovery_unconfirmed", "发送点击结果未知，保留原页面核实，未再次回车", False)
                method = (
                    f"send-btn{attempt_suffix}"
                    if clicked
                    else f"enter-after-send-timeout{attempt_suffix}"
                )
            else:
                button = find_send_button(page)
                if button is not None:
                    try:
                        button.click(timeout=2500, no_wait_after=True)
                        _send_progress("send_clicked")
                        method = f"generic-send{attempt_suffix}"
                    except Exception:
                        raise PlatformFlowError("recovery_unconfirmed", "发送点击结果未知，未重复提交问题", False)
                else:
                    page.keyboard.press("Enter")
                    _send_progress("send_enter_pressed")
                    method = f"enter{attempt_suffix}"
            try:
                # 新版 Kimi 会把回答留在首页，地址不一定变成 /chat/。
                for _ in range(40):
                    observed = _kimi_submission_state(page)
                    acknowledged = not generating_before and observed["generating"]
                    navigated = observed["url"] != url_before and ("/chat/" in observed["url"] or "chat_id" in observed["url"])
                    if acknowledged or navigated or observed["length"] > text_before + 20:
                        from cn_ai.diagnostics import current
                        diagnostic = current()
                        if diagnostic:
                            diagnostic.meta["send_acknowledgement"] = "generation_started" if acknowledged else "answer_growth_or_navigation"
                            diagnostic.meta["kimi_busy_refreshes"] = refreshes
                        _send_progress("submission_confirmed")
                        if on_send_state:
                            on_send_state("sent")
                        return prep, method
                    detect_platform_failure(page, site_id)
                    time.sleep(0.5)
            except PlatformFlowError as exc:
                if exc.error_code != "platform_busy" or refreshes >= refresh_limit:
                    if exc.error_code == "platform_busy" and refreshes >= refresh_limit:
                        raise PlatformFlowError(
                            "platform_busy",
                            f"Kimi 繁忙已刷新 {refreshes} 次，达到本次提问上限",
                            True,
                        ) from exc
                    raise
                dismiss_stale_kimi_busy_dialog(page)
                try:
                    page.reload(wait_until="domcontentloaded", timeout=20000)
                    time.sleep(2)
                    input_el = find_input(page)
                    input_el.click(timeout=3000)
                    page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
                    page.keyboard.press("Backspace")
                    page.keyboard.insert_text(send_text)
                    time.sleep(0.4)
                    current_text = input_el.evaluate("el => 'value' in el ? el.value : (el.innerText || el.textContent || '')")
                    if ' '.join(str(current_text or '').split()) != ' '.join(send_text.split()):
                        raise PlatformFlowError("platform_busy", "Kimi 刷新后输入内容未确认，未再次提交", True)
                except PlatformFlowError:
                    raise
                except Exception as exc:
                    raise PlatformFlowError("platform_busy", "Kimi 刷新后未能重新输入问题", True) from exc
                refreshes += 1
                unconfirmed_retry = False
                url_before = ""
                try:
                    url_before = page.url or ""
                except Exception:
                    url_before = ""
                text_before = kimi_assistant_length(page)
                generating_before = kimi_generation_active(page)
                continue
            # 没有明确繁忙提示，但发送动作可能未生效，再尝试一次。
            if not unconfirmed_retry:
                if on_send_state:
                    raise PlatformFlowError("recovery_unconfirmed", "Kimi 发送状态尚未确认，未重复提交问题", False)
                unconfirmed_retry = True
                time.sleep(1)
                continue
            detect_platform_failure(page, site_id)
            href = ""
            try:
                href = page.url or ""
            except Exception:
                href = ""
            raise PlatformFlowError(
                "platform_busy",
                f"Kimi 发送后未进入会话，按平台繁忙重试 url={href.split('?')[0][:120]}",
                True,
            )
        raise PlatformFlowError(
            "platform_busy", "Kimi 当前请求过多，请稍后重试", True
        )
    if site_id in ("qianwen", "yiyan", "yuanbao"):
        # These web composers support Enter. Their pointer send controls can
        # navigate during the mouse action, leaving a submitted question with
        # a click timeout. Submit once through the focused editor instead.
        page.keyboard.press("Enter")
        return prep, "enter"
    send = find_send_button(page, allow_last_button=site_id != "doubao")
    if send:
        # Yuanbao's send control starts a navigation. Waiting for that
        # navigation turns a completed click into a 5s timeout.
        send.click(timeout=5000, no_wait_after=True)
        method = "button"
    else:
        page.keyboard.press("Enter")
        method = "enter"
    return prep, method


def extract_answer_and_links(page: Page, site_id: str = "") -> Dict[str, Any]:
    """从页面提取回答文本与链接（站点特化优先）。"""
    data = page.evaluate(
        """(siteId) => {
        const out = { text: '', links: [], link_details: [], selector: 'body' };
        const pickLinkDetails = (root) => [...root.querySelectorAll('a[href^=http]')]
          .map(a => {
            let siteName = '';
            try { siteName = new URL(a.href).hostname.replace(/^www\./, ''); } catch (_) {}
            const img = a.querySelector('img');
            const icon = img ? (img.currentSrc || img.src || '') : '';
            return {
              url: a.href,
              title: ((a.getAttribute('title') || a.getAttribute('aria-label') || a.innerText || '') + '').trim().slice(0, 300),
              icon: /^https?:/.test(icon) ? icon : '',
              site_name: siteName,
            };
          })
          .filter(x => x.url && !x.url.includes('javascript') && !x.url.includes('deepseek.com'));
        const setLinks = (details) => {
          const seen = new Set();
          out.link_details = details.filter(x => {
            if (!x.url || seen.has(x.url)) return false;
            seen.add(x.url);
            return true;
          }).slice(0, 100);
          out.links = out.link_details.map(x => x.url);
        };

        if (siteId === 'deepseek') {
          const nodes = [...document.querySelectorAll('.ds-markdown.ds-assistant-message-main-content')];
          if (nodes.length) {
            const last = nodes[nodes.length - 1];
            out.text = (last.innerText || '').trim();
            setLinks(pickLinkDetails(last));
            out.selector = '.ds-markdown.ds-assistant-message-main-content:last';
            return out;
          }
        }

        if (siteId === 'yiyan') {
          const nodes = [...document.querySelectorAll(
            '.answer-container .ai-entry-block.ai-markdown .marklang, .ai-entry[class*="_task-detail-answer_"] .marklang'
          )];
          const answer = nodes.filter(n => (n.innerText || '').trim().length > 20).pop();
          if (answer) {
            out.text = (answer.innerText || '').trim();
            setLinks(pickLinkDetails(answer));
            out.selector = 'yiyan-answer';
            return out;
          }
        }

        if (siteId === 'yuanbao') {
          const last=[...document.querySelectorAll('[data-conv-speaker]')].pop();
          const reply=last && last.getAttribute('data-conv-speaker')==='ai' ? last : null;
          if (reply && reply.getAttribute('data-conv-status')==='finished') {
            out.text=(reply.innerText || '').trim();
            setLinks(pickLinkDetails(reply));
            out.selector='yuanbao-answer-finished';
          }
          return out;
        }

        if (siteId === 'kimi') {
          const assistant = [...document.querySelectorAll('.segment.segment-assistant')].pop();
          const scope = assistant || document.querySelector('.chat-content, main') || document.body;
          const blocks = [...scope.querySelectorAll('.markdown-container')].filter(node => {
            if (node.classList.contains('toolcall-content-text')) return false;
            return !node.closest('.thinking-container, .toolcall-container, .toolcall-flow');
          });
          const last = blocks.filter(n => (n.innerText || '').trim().length > 20).pop();
          if (last) {
            out.text = (last.innerText || '').trim();
            setLinks(pickLinkDetails(last).filter(x =>
              !x.url.includes('kimi.com') && !x.url.includes('kimi.ai') &&
              !x.url.includes('kimi.link') && !x.url.includes('kimi.page') &&
              !x.url.includes('icon-cache')));
            out.selector = 'kimi-answer';
            return out;
          }
        }

        if (siteId === 'doubao') {
          const assistants = [...document.querySelectorAll(
            '[data-message-role="assistant"] [data-testid="message_text_content"]'
          )].filter(n => (n.innerText || '').trim());
          const assistant = assistants.pop();
          if (assistant) {
            out.text = (assistant.innerText || '').trim();
            setLinks(pickLinkDetails(assistant));
            out.selector = 'doubao-assistant:last';
            return out;
          }
          const extra = pickLinkDetails(document);
          document.querySelectorAll('[data-url^=http], [data-href^=http]').forEach(el => {
            const u = el.getAttribute('data-url') || el.getAttribute('data-href');
            if (u) extra.push({url:u, title:(el.innerText || '').trim().slice(0, 300), icon:'', site_name:''});
          });
          out.text = (document.body.innerText || '').slice(0, 15000);
          setLinks(extra);
          out.selector = 'body+data-url';
          return out;
        }

        out.text = (document.body.innerText || '').slice(0, 15000);
        setLinks(pickLinkDetails(document));
        return out;
    }""",
        site_id,
    )
    return data
