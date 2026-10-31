"""Generate and optionally send an auto-reply for the currently selected Douyin CS chat.

This uses the verified DOM fallback from ``douyin_export_recent.py``. It is meant
for the currently open/selected browser conversation and defaults to dry-run.
Use ``--send`` only after the generated reply has been checked.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from playwright.async_api import Browser, Page, Playwright, async_playwright

sys.path.insert(0, str(Path(__file__).parent))

from douyin_export_recent import CDP_URL, DOUYIN_PAGE_MARKERS, EXTRACT_CURRENT_CHAT_JS, LIFE_SESSION_URL, SESSION_URL
from src.platform_reporter import PlatformReporter
from src.qwen_text import QwenTextClient
from src.revai_qa import RevaiQaClient

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

log = logging.getLogger("douyin_reply")

ReplyClient = QwenTextClient | RevaiQaClient
DEFAULT_PLATFORM_URL = "http://127.0.0.1:8000"

CLICK_FIRST_CONVERSATION_JS = """
() => {
  const rows = Array.from(document.querySelectorAll('div,span,a')).map((el) => {
    const r = el.getBoundingClientRect();
    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    return { el, text, x: r.left, y: r.top, w: r.width, h: r.height };
  }).filter((item) => {
    if (!item.text || item.x < 280 || item.x > 620 || item.y < 330 || item.y > window.innerHeight - 20) return false;
    if (item.w < 120 || item.h < 32 || item.h > 140) return false;
    if (/来源|全部状态|更多|暂无会话信息|当前会话|最近联系|群聊/.test(item.text)) return false;
    return true;
  });

  rows.sort((a, b) => {
    const ah = /\\d{1,2}:\\d{2}|刚刚|分钟前/.test(a.text) ? 0 : 1;
    const bh = /\\d{1,2}:\\d{2}|刚刚|分钟前/.test(b.text) ? 0 : 1;
    return ah - bh || a.y - b.y || b.w * b.h - a.w * a.h;
  });

  const row = rows[0];
  if (!row) return null;
  row.el.click();
  return {
    text: row.text,
    rect: [Math.round(row.x), Math.round(row.y), Math.round(row.w), Math.round(row.h)],
  };
}
"""

EXTRACT_VISIBLE_CONVERSATION_ROWS_JS = """
() => {
  const skip = /来源|全部状态|更多|单聊|群聊|历史会话|已拉取|暂无会话信息|当前会话/;
  const rows = Array.from(document.querySelectorAll('div')).map((el) => {
    const r = el.getBoundingClientRect();
    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    const cls = String(el.className || '');
    return { el, text, cls, x: r.left, y: r.top, w: r.width, h: r.height };
  }).filter((item) => {
    if (!item.text || skip.test(item.text)) return false;
    if (item.x < 280 || item.x > 610 || item.y < 330 || item.y > window.innerHeight - 40) return false;
    if (item.w < 240 || item.w > 340 || item.h < 44 || item.h > 90) return false;
    return true;
  });

  const seen = new Set();
  return rows.map((item) => {
    const lines = item.text.split(/\\s+/).filter(Boolean);
    const titleNode = item.el.querySelector('.JmY-pTMaySm_Q,[class*="pTMaySm"]');
    const title = (titleNode?.innerText || titleNode?.textContent || lines[0] || '').replace(/\\s+/g, ' ').trim();
    const key = `${Math.round(item.y)}|${title}|${item.text}`;
    if (seen.has(key)) return null;
    seen.add(key);
    return {
      title,
      text: item.text,
      selected: item.cls.includes('z27xAPvsg'),
      rect: [Math.round(item.x), Math.round(item.y), Math.round(item.w), Math.round(item.h)],
    };
  }).filter(Boolean);
}
"""

CLICK_CONVERSATION_ROW_JS = """
(target) => {
  const titleNeedle = String(target.title || '').trim();
  const textNeedle = String(target.text || '').trim();
  const rect = target.rect || [];
  const rows = Array.from(document.querySelectorAll('div')).map((el) => {
    const r = el.getBoundingClientRect();
    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    const titleNode = el.querySelector('.JmY-pTMaySm_Q,[class*="pTMaySm"]');
    const title = (titleNode?.innerText || titleNode?.textContent || '').replace(/\\s+/g, ' ').trim();
    return { el, text, title, x: r.left, y: r.top, w: r.width, h: r.height };
  }).filter((item) => {
    if (!item.text || item.x < 280 || item.x > 610 || item.y < 330) return false;
    if (item.w < 240 || item.w > 340 || item.h < 44 || item.h > 90) return false;
    if (titleNeedle && item.title && item.title !== titleNeedle) return false;
    if (textNeedle && item.text !== textNeedle && !item.text.includes(textNeedle) && !textNeedle.includes(item.text)) return false;
    return true;
  });
  rows.sort((a, b) => {
    const dyA = rect.length >= 2 ? Math.abs(a.y - rect[1]) : 0;
    const dyB = rect.length >= 2 ? Math.abs(b.y - rect[1]) : 0;
    return dyA - dyB;
  });
  const row = rows[0];
  if (!row) return null;
  row.el.click();
  return {
    title: row.title || titleNeedle,
    text: row.text,
    rect: [Math.round(row.x), Math.round(row.y), Math.round(row.w), Math.round(row.h)],
  };
}
"""


def _load_cfg(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing config: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def _append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")


def _round_ms(started_at: float, ended_at: float | None = None) -> float:
    return round(((ended_at or time.perf_counter()) - started_at) * 1000, 1)


def _row_preview(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "").strip()
    text = str(row.get("text") or "").strip()
    preview = text
    if title:
        preview = preview.replace(title, " ", 1)
    preview = re.sub(r"(?:\b\d\s+){8,}\d\b", " ", preview)
    preview = re.sub(r"<?\s*\d+\s*秒|<?\s*\d+\s*分钟(?:前)?|刚刚|昨天|前天", " ", preview)
    preview = re.sub(r"\b\d{1,2}:\d{2}\b|刚刚|\d+\s*分钟(?:前)?|昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天]", " ", preview)
    preview = re.sub(r"\s+", " ", preview).strip()
    return preview


def _row_signature(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "").strip()
    text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
    return f"{title}|{text}"


def _row_looks_recent(row: dict[str, Any], *, max_minutes: int = 15) -> bool:
    text = str(row.get("text") or "")
    if re.search(r"刚刚|<\s*30\s*秒|<?\s*\d+\s*秒", text):
        return True
    match = re.search(r"<?\s*(\d+)\s*分钟(?:前)?", text)
    return bool(match and int(match.group(1)) <= max_minutes)


def _visible_row_signatures(rows: list[dict[str, Any]]) -> dict[str, str]:
    signatures: dict[str, str] = {}
    for row in rows:
        title = str(row.get("title") or "").strip()
        if title:
            signatures[title] = _row_signature(row)
    return signatures


def _msg_key(chat: dict[str, Any], msg: dict[str, Any]) -> str:
    title = str(chat.get("title") or "selected_conversation")
    timestamp = str(msg.get("timestamp") or "")
    direction = str(msg.get("direction") or "")
    text = str(msg.get("text") or "")
    index = str(msg.get("_message_index") if msg.get("_message_index") is not None else "")
    ordinal = str(msg.get("_same_text_ordinal") if msg.get("_same_text_ordinal") is not None else "")
    return f"{title}|{timestamp}|{direction}|{index}|{text}|{ordinal}"


def _match_text(value: str) -> str:
    text = str(value or "").strip()
    text = text.replace("...", "").replace("…", "")
    return re.sub(r"\s+", "", text)


def _contact_id(chat: dict[str, Any]) -> str:
    return str(chat.get("conversation_key") or chat.get("title") or "selected_conversation")


def _contact_name(chat: dict[str, Any]) -> str:
    return str(chat.get("title") or _contact_id(chat))


def _build_prompt(chat: dict[str, Any], target: dict[str, Any], *, max_items: int = 12) -> str:
    rows: list[str] = []
    target_indices = set(target.get("_group_indices") or [])
    messages = chat.get("messages") or []
    start = max(0, len(messages) - max_items)
    for index, msg in enumerate(messages[start:], start=start):
        direction = msg.get("direction") or "unknown"
        sender = msg.get("sender_name") or direction
        timestamp = msg.get("timestamp") or ""
        text = msg.get("text") or ""
        marker = " <-- new" if msg is target or index in target_indices else ""
        rows.append(f"[{timestamp}] {sender} text: {text}{marker}")
    if target_indices:
        rows.append("本次需要合并回复的客户连续消息：")
        rows.extend(str(part).strip() for part in str(target.get("text") or "").splitlines() if str(part).strip())
    return (
        f"会话：{chat.get('title') or ''}\n\n"
        f"最近结构化消息：\n" + "\n".join(rows) + "\n\n"
        "请只回复标记为 new 的客户消息；如果有多条 new，合并理解后只发一条回复。"
        "如果信息不足，简短追问一个关键问题。"
        "不要编造价格、日期、订单号或承诺。"
        "只输出要发给客户的正文。"
    )


def _latest_incoming(chat: dict[str, Any], *, allow_stale: bool = False) -> dict[str, Any] | None:
    messages = chat.get("messages") or []
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if not str(msg.get("text") or "").strip():
            continue
        if msg.get("direction") != "incoming" and not allow_stale:
            return None
        if msg.get("direction") != "incoming":
            continue
        same_text_ordinal = 0
        for prior in messages[: index + 1]:
            if (
                prior.get("direction") == msg.get("direction")
                and str(prior.get("timestamp") or "") == str(msg.get("timestamp") or "")
                and str(prior.get("text") or "") == str(msg.get("text") or "")
            ):
                same_text_ordinal += 1
        msg["_message_index"] = index
        msg["_same_text_ordinal"] = same_text_ordinal
        return msg
    return None


def _latest_incoming_group(chat: dict[str, Any], *, allow_stale: bool = False, max_group_size: int = 3) -> dict[str, Any] | None:
    messages = chat.get("messages") or []
    last_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if not str(msg.get("text") or "").strip():
            continue
        if msg.get("direction") != "incoming" and not allow_stale:
            return None
        if msg.get("direction") != "incoming":
            continue
        last_index = index
        break
    if last_index is None:
        return None

    group_indices: list[int] = []
    for index in range(last_index, -1, -1):
        msg = messages[index]
        if msg.get("direction") != "incoming" or not str(msg.get("text") or "").strip():
            break
        group_indices.append(index)
        if len(group_indices) >= max_group_size:
            break
    group_indices.reverse()

    target = dict(messages[last_index])
    target["_message_index"] = last_index
    target["_same_text_ordinal"] = 1
    target["_group_indices"] = group_indices
    target["_group_size"] = len(group_indices)
    target["text"] = "\n".join(str(messages[index].get("text") or "").strip() for index in group_indices)
    return target


def _find_incoming_by_text(chat: dict[str, Any], target_text: str) -> dict[str, Any] | None:
    needle = target_text.strip()
    if not needle:
        return None
    compact_needle = _match_text(needle)
    messages = chat.get("messages") or []
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        text = str(msg.get("text") or "").strip()
        compact_text = _match_text(text)
        if msg.get("direction") != "incoming" or (
            needle not in text
            and compact_needle not in compact_text
            and compact_text not in compact_needle
        ):
            continue
        same_text_ordinal = 0
        for prior in messages[: index + 1]:
            if (
                prior.get("direction") == msg.get("direction")
                and str(prior.get("timestamp") or "") == str(msg.get("timestamp") or "")
                and str(prior.get("text") or "") == str(msg.get("text") or "")
            ):
                same_text_ordinal += 1
        msg["_message_index"] = index
        msg["_same_text_ordinal"] = same_text_ordinal
        return msg
    return None


def _make_client(cfg: dict[str, Any]) -> tuple[str, ReplyClient]:
    provider = str((cfg.get("reply_engine") or {}).get("provider") or "qwen").strip().lower()
    if provider in {"revai", "local_carzone"}:
        revai_cfg = (cfg.get("local_carzone") if provider == "local_carzone" else cfg.get("revai")) or {}
        return provider, RevaiQaClient(
            base_url=str(revai_cfg.get("base_url", "http://127.0.0.1:8787" if provider == "local_carzone" else "https://car.revai.cn")),
            endpoint=str(revai_cfg.get("endpoint", "/api/project/carzone/compat/chat/respond")),
            api_key=revai_cfg.get("api_key"),
            timeout=int(revai_cfg.get("timeout", 60)),
            session_prefix=str(revai_cfg.get("session_prefix", "douyin")),
            message_mode=str(revai_cfg.get("message_mode", "full_prompt" if provider == "local_carzone" else "latest_new")),
        )
    if provider == "qwen":
        qwen_cfg = cfg.get("qwen") or {}
        return provider, QwenTextClient(
            api_key=str(qwen_cfg.get("api_key", "")),
            base_url=str(qwen_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
            model=str(qwen_cfg.get("model", "qwen-turbo")),
            temperature=float(qwen_cfg.get("temperature", 0.3)),
            timeout=int(qwen_cfg.get("timeout", 60)),
        )
    raise ValueError(f"unsupported reply_engine.provider: {provider}")


def _normalize_reply_for_send(reply: str) -> str:
    """Keep Douyin's Enter-to-send textarea from mishandling line breaks."""
    text = str(reply or "")
    text = text.replace("\\r\\n", " ").replace("\\n", " ").replace("\\r", " ")
    text = re.sub(r"[\r\n\t\v\f\u0085\u2028\u2029]+", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


async def _send_reply(page: Page, reply: str) -> str:
    sent_text = _normalize_reply_for_send(reply)
    if not sent_text:
        raise ValueError("empty reply after normalization")
    if re.search(r"[\r\n\v\f\u0085\u2028\u2029]|\\r|\\n", sent_text):
        raise ValueError(f"reply still contains line break after normalization: {sent_text!r}")
    box = page.get_by_placeholder("回复内容，可按Enter键发送，按Alt+Enter换行")
    box = page.locator("textarea[placeholder*='Enter'][placeholder*='Alt+Enter']").first
    await box.click()
    await box.fill(sent_text)
    await box.press("Enter")
    return sent_text


async def _ensure_selected_conversation(page: Page) -> dict[str, Any] | None:
    chat = await page.evaluate(EXTRACT_CURRENT_CHAT_JS)
    if isinstance(chat, dict) and chat.get("messages"):
        return chat

    clicked = await page.evaluate(CLICK_FIRST_CONVERSATION_JS)
    if clicked:
        log.info("selected conversation row: %s", clicked.get("text"))
        await page.wait_for_timeout(1200)
        chat = await page.evaluate(EXTRACT_CURRENT_CHAT_JS)
        if isinstance(chat, dict) and chat.get("messages"):
            return chat
    return None


async def _visible_conversation_rows(page: Page) -> list[dict[str, Any]]:
    rows = await page.evaluate(EXTRACT_VISIBLE_CONVERSATION_ROWS_JS)
    return rows if isinstance(rows, list) else []


async def _select_conversation_row(page: Page, row: dict[str, Any]) -> dict[str, Any] | None:
    clicked = await page.evaluate(CLICK_CONVERSATION_ROW_JS, row)
    if not clicked:
        return None
    await page.wait_for_timeout(900)
    chat = await page.evaluate(EXTRACT_CURRENT_CHAT_JS)
    if not isinstance(chat, dict) or not chat.get("messages"):
        return None
    expected_title = str(row.get("title") or "").strip()
    actual_title = str(chat.get("title") or "").strip()
    if expected_title and actual_title and expected_title != actual_title:
        log.warning("conversation title mismatch after click: expected=%s actual=%s", expected_title, actual_title)
        return None
    return chat


async def _connect_page(cdp: str) -> tuple[Playwright, Browser, Page]:
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(cdp)
    for ctx in browser.contexts:
        for page in ctx.pages:
            if any(marker in page.url for marker in DOUYIN_PAGE_MARKERS):
                return pw, browser, page
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await ctx.new_page()
    await page.goto(LIFE_SESSION_URL or SESSION_URL, wait_until="domcontentloaded", timeout=30_000)
    return pw, browser, page


async def run_once(
    *,
    page: Page,
    cfg: dict[str, Any],
    state_path: Path,
    send: bool,
    force: bool,
    allow_stale_incoming: bool,
    target_text: str | None,
    reporter: PlatformReporter | None = None,
    profile_log_path: Path | None = None,
) -> bool:
    total_started = time.perf_counter()
    profile: dict[str, Any] = {
        "started_at": datetime.now().isoformat(timespec="milliseconds"),
        "send_enabled": bool(send),
        "force": bool(force),
    }
    stage_started = time.perf_counter()
    chat = await _ensure_selected_conversation(page)
    profile["select_current_chat_ms"] = _round_ms(stage_started)
    if not isinstance(chat, dict) or not chat.get("messages"):
        log.info("no active/current conversation with messages found")
        return False
    stage_started = time.perf_counter()
    target = _find_incoming_by_text(chat, target_text) if target_text else _latest_incoming_group(chat, allow_stale=allow_stale_incoming)
    profile["target_select_ms"] = _round_ms(stage_started)
    if not target:
        log.info("no incoming message found in selected chat")
        return False

    profile["conversation"] = chat.get("title") or ""
    profile["message_chars"] = len(str(target.get("text") or ""))
    profile["message_group_size"] = int(target.get("_group_size") or 1)
    key = _msg_key(chat, target)
    stage_started = time.perf_counter()
    state = _read_json(state_path)
    profile["state_read_ms"] = _round_ms(stage_started)
    replied = set(state.get("replied_keys") or [])
    sent = {str(item.get("message_key") or "") for item in (state.get("replies") or []) if item.get("status") == "sent"}
    reported_in = set(state.get("reported_in_keys") or [])
    reported_out = set(state.get("reported_out_keys") or [])

    if key in replied and not force and (not send or key in sent):
        log.debug("latest incoming already handled: %s", target.get("text"))
        return False

    if reporter and key not in reported_in:
        stage_started = time.perf_counter()
        reporter.report_conversation(
            platform="douyin",
            direction="in",
            content=str(target.get("text") or ""),
            contact_id=_contact_id(chat),
            contact_name=_contact_name(chat),
        )
        reported_in.add(key)
        state["reported_in_keys"] = sorted(reported_in)
        _write_json(state_path, state)
        profile["report_in_ms"] = _round_ms(stage_started)

    stage_started = time.perf_counter()
    provider, client = _make_client(cfg)
    system = str((cfg.get("reply") or {}).get("system") or "您是专业客服，用简短中文回复。")
    prompt = _build_prompt(chat, target)
    profile["prompt_build_ms"] = _round_ms(stage_started)
    profile["prompt_chars"] = len(prompt)
    stage_started = time.perf_counter()
    reply = client.complete(system, prompt, session_id=str(chat.get("title") or "selected"))
    profile["reply_complete_ms"] = _round_ms(stage_started)
    client_profile = getattr(client, "last_profile", None)
    if isinstance(client_profile, dict) and client_profile:
        profile["reply_client"] = client_profile
    print(f">>> conversation: {chat.get('title')}")
    print(f">>> customer: {target.get('text')}")
    print(f">>> {provider} reply: {reply}")

    stage_started = time.perf_counter()
    sent_text = _normalize_reply_for_send(reply)
    profile["normalize_ms"] = _round_ms(stage_started)
    profile["reply_chars"] = len(str(reply or ""))
    profile["sent_chars"] = len(str(sent_text or ""))
    if send:
        stage_started = time.perf_counter()
        confirm_chat = await _ensure_selected_conversation(page)
        if isinstance(confirm_chat, dict) and target_text:
            confirm_target = _find_incoming_by_text(confirm_chat, target_text)
        else:
            confirm_target = _latest_incoming_group(confirm_chat, allow_stale=False) if isinstance(confirm_chat, dict) else None
        profile["send_preflight_ms"] = _round_ms(stage_started)
        if not confirm_target or _msg_key(confirm_chat, confirm_target) != key:
            log.warning("selected conversation changed before send; skip this reply")
            return False
        stage_started = time.perf_counter()
        sent_text = await _send_reply(page, reply)
        profile["send_ms"] = _round_ms(stage_started)
        replied.add(key)
    else:
        replied.add(key)
    state.setdefault("replies", [])
    record = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "message_key": key,
        "conversation": chat.get("title") or "",
        "customer_text": target.get("text") or "",
        "reply_text": reply,
        "sent_text": sent_text,
        "provider": provider,
        "model": getattr(client, "model", provider),
        "status": "sent" if send else "dry_run",
        "profile": profile,
    }
    if sent_text != reply:
        record["normalized_for_send"] = True
    state["replies"].append(record)
    if reporter and send and key not in reported_out:
        stage_started = time.perf_counter()
        reporter.report_conversation(
            platform="douyin",
            direction="out",
            content=sent_text,
            contact_id=_contact_id(chat),
            contact_name=_contact_name(chat),
        )
        reported_out.add(key)
        profile["report_out_ms"] = _round_ms(stage_started)
    profile["total_ms"] = _round_ms(total_started)
    state["reported_in_keys"] = sorted(reported_in)
    state["reported_out_keys"] = sorted(reported_out)
    state["replied_keys"] = sorted(replied)
    stage_started = time.perf_counter()
    _write_json(state_path, state)
    profile["state_write_ms"] = _round_ms(stage_started)
    if profile_log_path:
        _append_jsonl(profile_log_path, record)
    log.info(
        "reply profile: conversation=%s total_ms=%s reply_ms=%s send_ms=%s",
        chat.get("title") or "",
        profile.get("total_ms"),
        profile.get("reply_complete_ms"),
        profile.get("send_ms", 0),
    )
    return True


async def seed_visible_conversations(
    *,
    page: Page,
    state_path: Path,
    row_cooldown_sec: float = 30.0,
) -> int:
    state = _read_json(state_path)
    rows = await _visible_conversation_rows(page)
    seed_rows = [row for row in rows if not _row_looks_recent(row)]
    signatures = _visible_row_signatures(seed_rows)
    state["visible_row_signatures"] = signatures
    processed = set(state.get("processed_row_signatures") or [])
    processed.update(signatures.values())
    state["processed_row_signatures"] = sorted(processed)
    now = time.time()
    cooldowns = dict(state.get("row_cooldowns") or {})
    for title in signatures:
        cooldowns[title] = now + row_cooldown_sec
    state["row_cooldowns"] = cooldowns
    _write_json(state_path, state)
    return len(signatures)


async def run_visible_conversations_once(
    *,
    page: Page,
    cfg: dict[str, Any],
    state_path: Path,
    send: bool,
    force: bool,
    reporter: PlatformReporter | None = None,
    profile_log_path: Path | None = None,
    row_cooldown_sec: float = 30.0,
) -> int:
    handled = 0
    rows = await _visible_conversation_rows(page)
    if not rows:
        log.info("no visible conversation rows found")
        return 0

    state = _read_json(state_path)
    known_signatures = dict(state.get("visible_row_signatures") or {})
    processed_signatures = set(state.get("processed_row_signatures") or [])
    row_cooldowns = dict(state.get("row_cooldowns") or {})
    now = time.time()
    current_signatures = _visible_row_signatures(rows)
    changed_titles = {
        title
        for title, signature in current_signatures.items()
        if known_signatures.get(title) != signature
        and signature not in processed_signatures
        and float(row_cooldowns.get(title) or 0) <= now
    }
    if not changed_titles:
        log.debug("no changed visible conversation rows")
        return 0

    for row in rows:
        title = str(row.get("title") or "").strip()
        if title not in changed_titles:
            continue
        row_signature = current_signatures.get(title) or _row_signature(row)
        preview = _row_preview(row)
        if not preview:
            log.debug("skip changed row with empty preview: %s", title)
            processed_signatures.add(row_signature)
            continue

        processed_signatures.add(row_signature)
        row_cooldowns[title] = time.time() + row_cooldown_sec
        state = _read_json(state_path)
        state["visible_row_signatures"] = current_signatures
        state["processed_row_signatures"] = sorted(processed_signatures)
        state["row_cooldowns"] = row_cooldowns
        _write_json(state_path, state)
        log.info("changed conversation row detected: title=%s preview=%s", title, preview)

        latest_rows = await _visible_conversation_rows(page)
        current = next(
            (
                candidate
                for candidate in latest_rows
                if str(candidate.get("title") or "") == str(row.get("title") or "")
                and str(candidate.get("text") or "") == str(row.get("text") or "")
            ),
            row,
        )
        chat = await _select_conversation_row(page, current)
        if not chat:
            log.warning("failed to select changed conversation row: %s", title)
            continue
        target = _find_incoming_by_text(chat, preview)
        if not target:
            log.warning("changed row preview not found in selected incoming messages; skip reply: title=%s preview=%s", title, preview)
            continue
        if await run_once(
            page=page,
            cfg=cfg,
            state_path=state_path,
            send=send,
            force=force,
            allow_stale_incoming=False,
            target_text=preview,
            reporter=reporter,
            profile_log_path=profile_log_path,
        ):
            handled += 1
            await page.wait_for_timeout(600)
        refreshed_rows = await _visible_conversation_rows(page)
        state = _read_json(state_path)
        state["visible_row_signatures"] = _visible_row_signatures(refreshed_rows)
        state["processed_row_signatures"] = sorted(processed_signatures)
        state["row_cooldowns"] = row_cooldowns
        _write_json(state_path, state)
        current_signatures = dict(state["visible_row_signatures"])
    if not handled:
        state = _read_json(state_path)
        state["visible_row_signatures"] = current_signatures
        state["processed_row_signatures"] = sorted(processed_signatures)
        state["row_cooldowns"] = row_cooldowns
        _write_json(state_path, state)
    return handled


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Douyin selected-chat auto reply")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "config.yaml")
    parser.add_argument("--cdp", default=CDP_URL)
    parser.add_argument("--state", type=Path, default=Path("exports") / "douyin_reply_state.json")
    parser.add_argument("--profile-log", type=Path, default=Path("exports") / "douyin_reply_profile.jsonl")
    parser.add_argument("--reply-provider", choices=("revai", "local_carzone", "qwen"), default=None, help="Temporarily override reply_engine.provider.")
    parser.add_argument("--revai-base-url", default=None, help="Temporarily override revai.base_url, useful for local E2E testing.")
    parser.add_argument("--revai-endpoint", default=None, help="Temporarily override revai.endpoint.")
    parser.add_argument("--revai-message-mode", choices=("latest_new", "full_prompt"), default=None, help="Temporarily override revai.message_mode.")
    parser.add_argument("--target-text", default=None, help="Dry-run against the last incoming message containing this text; ignored when --send is used.")
    parser.add_argument("--send", action="store_true", help="Actually send the generated reply.")
    parser.add_argument("--no-send", action="store_true", help="Explicit dry-run mode; useful for probes while the normal service uses --send.")
    parser.add_argument("--force", action="store_true", help="Reply even if the latest incoming message was handled before.")
    parser.add_argument("--allow-stale-incoming", action="store_true", help="Dry-run against the latest incoming message even if it has already been followed by an outgoing reply.")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--scan-visible-conversations", action=argparse.BooleanOptionalAction, default=None, help="In watch mode, scan visible left-side conversations instead of only the selected chat.")
    parser.add_argument("--reply-existing-on-start", action="store_true", help="In watch mode, reply to already-visible incoming messages at startup. Default is to seed them as handled.")
    parser.add_argument("--interval-sec", type=float, default=3.0)
    parser.add_argument("--row-cooldown-sec", type=float, default=30.0, help="Minimum seconds before probing the same visible conversation row again.")
    parser.add_argument("--duration-sec", type=float, default=None)
    parser.add_argument(
        "--platform-url",
        default=None,
        help="Agent Console backend URL for auto-writing Douyin messages, e.g. http://127.0.0.1:8000.",
    )
    parser.add_argument("--no-platform-report", action="store_true", help="Disable SQLite/Agent Console message reporting.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if args.no_send:
        args.send = False

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    cfg = _load_cfg(args.config)
    if args.reply_provider:
        cfg.setdefault("reply_engine", {})["provider"] = args.reply_provider
    if args.revai_base_url:
        cfg.setdefault("revai", {})["base_url"] = args.revai_base_url
    if args.revai_endpoint:
        cfg.setdefault("revai", {})["endpoint"] = args.revai_endpoint
    if args.revai_message_mode:
        cfg.setdefault("revai", {})["message_mode"] = args.revai_message_mode
    platform_url = args.platform_url or (cfg.get("platform") or {}).get("url") or DEFAULT_PLATFORM_URL
    watch_cfg = cfg.get("watch") or {}
    scan_visible = args.scan_visible_conversations
    if scan_visible is None:
        scan_visible = bool(watch_cfg.get("scan_visible_conversations", True))
    reporter = None if args.no_platform_report else PlatformReporter(str(platform_url))
    pw, browser, page = await _connect_page(args.cdp)
    try:
        log.info("using page: %s", page.url)
        if reporter:
            log.info("platform reporting enabled: %s", platform_url)
        if not args.watch:
            await run_once(
                page=page,
                cfg=cfg,
                state_path=args.state,
                send=args.send,
                force=args.force,
                allow_stale_incoming=args.allow_stale_incoming and not args.send,
                target_text=args.target_text if not args.send else None,
                reporter=reporter,
                profile_log_path=args.profile_log,
            )
            return 0
        log.info("watching Douyin conversations; mode=%s", "send" if args.send else "dry-run")
        if scan_visible and not args.reply_existing_on_start:
            seeded = await seed_visible_conversations(page=page, state_path=args.state, row_cooldown_sec=args.row_cooldown_sec)
            log.info("seeded existing visible incoming conversations: %d", seeded)
        started_at = asyncio.get_running_loop().time()
        while True:
            try:
                if args.duration_sec is not None and asyncio.get_running_loop().time() - started_at >= args.duration_sec:
                    return 0
                await run_once(
                    page=page,
                    cfg=cfg,
                    state_path=args.state,
                    send=args.send,
                    force=False,
                    allow_stale_incoming=False,
                    target_text=None,
                    reporter=reporter,
                    profile_log_path=args.profile_log,
                )
                if scan_visible:
                    await run_visible_conversations_once(
                        page=page,
                        cfg=cfg,
                        state_path=args.state,
                        send=args.send,
                        force=False,
                        reporter=reporter,
                        profile_log_path=args.profile_log,
                        row_cooldown_sec=args.row_cooldown_sec,
                    )
                else:
                    await run_once(
                        page=page,
                        cfg=cfg,
                        state_path=args.state,
                        send=args.send,
                        force=False,
                        allow_stale_incoming=False,
                        target_text=None,
                        reporter=reporter,
                        profile_log_path=args.profile_log,
                    )
                await asyncio.sleep(args.interval_sec)
            except KeyboardInterrupt:
                return 0
            except Exception:
                log.exception("watch loop error; continue")
                await asyncio.sleep(args.interval_sec)
    finally:
        await browser.close()
        await pw.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
