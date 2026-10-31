"""Capture Douyin CS conversations, history, and incremental updates from Chrome CDP.

This collector follows the WXWork pattern:
- keep the raw network payloads
- normalize conversations/messages into a merged master
- preserve payload paths for later schema drift debugging

Primary mode:
- connect to an already opened Chrome with remote debugging enabled
- passively watch JSON/XHR/WebSocket traffic on the customer-service page
- optionally poke the page to trigger list/history loading

The script does not depend on a fixed API schema. It uses heuristics to extract
conversation and message records from the observed payloads and keeps the raw
responses alongside the normalized export.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml
from playwright.async_api import Browser, Page, Response, WebSocket, async_playwright

sys.path.insert(0, str(Path(__file__).parent))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


log = logging.getLogger("douyin_export")

CDP_URL = "http://localhost:9222"
SESSION_URL = "https://leads.cluerich.com/pc/cs/chat/session"
LIFE_SESSION_URL = (
    "https://life.douyin.com/cs/web/clue_private_message/chat/session"
    "?accountId=1867220805578056&conGroupId=13483733&groupId=1867220805578056"
    "&lifeAccountId=7648129816281057322"
)
DOUYIN_PAGE_MARKERS = (
    "leads.cluerich.com",
    "life.douyin.com/cs/web/clue_private_message",
)
TARGET_HOST_MARKERS = (
    "leads.cluerich.com",
    "life.douyin.com",
    "imapi3-normal.zijieapi.com",
    "imapi-oth.zijieapi.com",
)

EXTRACT_CURRENT_CHAT_JS = """
() => {
  const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();

  const legacyChat = document.querySelector('.leadsCsUI-MessageItem')?.closest('.flex.flex-col.flex-1.bg-1.overflow-hidden');
  if (legacyChat) {
    const title = norm(legacyChat.querySelector('.font-5.text-gray-5')?.innerText);
    const source = norm(legacyChat.querySelector('[class*="VhcPYysQM"]')?.innerText);
    const nodes = Array.from(legacyChat.querySelectorAll('.leadsCsUI-MsgTimeGap,.leadsCsUI-MessageItem'));
    let currentTime = '';
    const messages = [];

    for (const node of nodes) {
      const cls = String(node.className || '');
      if (cls.includes('leadsCsUI-MsgTimeGap')) {
        currentTime = norm(node.innerText || node.textContent);
        continue;
      }
      if (!cls.includes('leadsCsUI-MessageItem')) continue;
      const textEl = node.querySelector('.leadsCsUI-Text');
      const text = norm(textEl?.innerText || textEl?.textContent);
      if (!text) continue;
      const normal = node.querySelector('.leadsCsUI-NormalMessage');
      const normalCls = String(normal?.className || '');
      const direction = normalCls.includes('_right') ? 'outgoing' : 'incoming';
      const sender = norm(node.querySelector('.leadsCsUI-MessageNickname_name')?.innerText);
      const isRead = !!node.querySelector('.leadsCsUI-MessageAffix_isRead');
      const rect = node.getBoundingClientRect();
      messages.push({
        timestamp: currentTime,
        direction,
        sender_name: sender,
        text,
        is_read: isRead,
        screen_rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)],
      });
    }

    return {
      title,
      source,
      captured_at: new Date().toISOString(),
      messages,
    };
  }

  const lifeChat = document.querySelector('.wrap-rnVzum') || document.querySelector('.flex-1.flex.flex-col.page-card');
  if (!lifeChat) return null;

  const title = norm(
    document.querySelector('.msgTitle-yMthwV .text-base')?.innerText ||
    document.querySelector('.msgTitle-yMthwV')?.innerText?.split(/\\n/)[0] ||
    document.querySelector('.profile-fanrEb')?.innerText?.split(/\\n/)[0]
  );
  const source = norm(document.querySelector('.profile-fanrEb')?.innerText || '');
  const messageNodes = Array.from(lifeChat.querySelectorAll(
    '.px-4.flex.relative.w-fit.group.flex-row, .px-4.flex.relative.w-fit.group.flex-row-reverse.self-end, .px-4.text-xs.text-center.text-gray-2.my-4'
  ));
  let currentTime = '';
  const seen = new Set();
  const messages = [];

  for (const node of messageNodes) {
    const rect = node.getBoundingClientRect();
    const cls = String(node.className || '');
    const rawText = norm(node.innerText || node.textContent);
    if (!rawText) continue;
    if (cls.includes('text-center')) {
      currentTime = rawText;
      continue;
    }

    const leftBubble = node.querySelector('[class*="leftMsg"]');
    const rightBubble = node.querySelector('[class*="rightMsg"]');
    const bubble = leftBubble || rightBubble || node.querySelector('.whitespace-pre-wrap') || node;
    let text = norm(bubble?.innerText || bubble?.textContent);
    if (!text) continue;
    text = text.replace(/^灵耘致新科技-智能体开发\\s*/, '').replace(/^已读\\s*/, '').trim();
    if (!text || /^商家/.test(text)) continue;
    const direction = rightBubble || cls.includes('flex-row-reverse') ? 'outgoing' : 'incoming';
    const key = `${direction}|${currentTime}|${Math.round(rect.top)}|${text}`;
    if (seen.has(key)) continue;
    seen.add(key);
    messages.push({
      timestamp: currentTime,
      direction,
      sender_name: direction === 'outgoing' ? '灵耘致新科技-智能体开发' : title,
      text,
      is_read: direction === 'outgoing' && /已读/.test(rawText),
      screen_rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)],
    });
  }

  return {
    title,
    source,
    captured_at: new Date().toISOString(),
    messages,
  };
}
"""


@dataclass
class ExtractedConversation:
    conversation_key: str
    title: str = ""
    list_preview: str = ""
    conversation_id: str = ""
    session_id: str = ""
    unread_count: int | None = None
    last_message_text: str = ""
    last_message_time: str = ""
    participants: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)
    first_seen_at: str = ""
    last_seen_at: str = ""
    seen_count: int = 0
    structured_messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ExtractedMessage:
    message_key: str
    conversation_key: str
    message_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    direction: str = ""
    message_type: str = "text"
    text: str = ""
    timestamp: str = ""
    source_url: str = ""
    source_path: str = ""
    raw_path: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    seen_count: int = 0
    dedupe_confidence: str = "medium"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _slug(value: str, *, limit: int = 64) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._")
    return value[:limit] or "item"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_json_loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except Exception:
        return None


def _walk_json(value: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, sub in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            yield from _walk_json(sub, next_path)
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            next_path = f"{path}[{index}]"
            yield from _walk_json(sub, next_path)


def _find_first(obj: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in obj and obj[key] not in (None, "", [], {}):
            return obj[key]
    lower_map = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        v = lower_map.get(key.lower())
        if v not in (None, "", [], {}):
            return v
    return None


def _has_any_key(obj: dict[str, Any], keys: Iterable[str]) -> bool:
    lower_keys = {str(k).lower() for k in obj.keys()}
    return any(str(key).lower() in lower_keys for key in keys)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        parsed = _safe_json_loads(text)
        if isinstance(parsed, dict):
            for key in ("text", "content", "msg_content", "message", "body"):
                item = parsed.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
                if isinstance(item, dict):
                    nested = _content_text(item)
                    if nested:
                        return nested
        return text
    if isinstance(value, dict):
        for key in ("text", "content", "msg_content", "message", "body", "value"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict):
                nested = _content_text(item)
                if nested:
                    return nested
        return _norm_text(value)
    if isinstance(value, list):
        parts = [_content_text(item) for item in value]
        return "\n".join([p for p in parts if p])
    return _norm_text(value)


def _message_type_from_text(text: str, raw: dict[str, Any]) -> str:
    hint = _norm_text(_find_first(raw, ("msg_type", "message_type", "type", "content_type"))).lower()
    if hint:
        if "image" in hint or "picture" in hint:
            return "image"
        if "video" in hint:
            return "video"
        if "audio" in hint or "voice" in hint:
            return "voice"
        if "file" in hint:
            return "file"
        if "system" in hint:
            return "system"
    lowered = text.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "link"
    if any(token in text for token in ("图片", "图像")):
        return "image"
    if any(token in text for token in ("视频", "语音")):
        return "media"
    if any(token in text for token in ("文件", "附件")):
        return "file"
    return "text"


def _conversation_key(raw: dict[str, Any]) -> str | None:
    if not _looks_like_conversation_record(raw):
        return None
    conv_id = _find_first(
        raw,
        (
            "conversation_id",
            "conversationId",
            "conversationID",
            "session_id",
            "sessionId",
            "sessionID",
            "chat_session_id",
            "chatSessionId",
            "chatSessionID",
            "chat_id",
            "chatId",
            "chatID",
            "thread_id",
            "threadId",
        ),
    )
    title = _norm_text(_find_first(raw, ("title", "name", "conversation_name", "chat_name", "nick_name", "nickname")))
    if conv_id:
        return _slug(str(conv_id), limit=80)
    if title:
        return _slug(title, limit=80)
    return None


def _looks_like_conversation_record(raw: dict[str, Any]) -> bool:
    return _has_any_key(
        raw,
        (
            "conversation_id",
            "conversationId",
            "conversationID",
            "conversation_name",
            "session_id",
            "sessionId",
            "sessionID",
            "chat_session_id",
            "chatSessionId",
            "chatSessionID",
            "chat_id",
            "chatId",
            "chatID",
            "last_message",
            "lastMessage",
            "last_message_text",
            "latest_message",
            "unread_count",
            "unreadCount",
            "message_list",
            "msg_list",
        ),
    )


def _looks_like_message_record(raw: dict[str, Any]) -> bool:
    has_message_id = _has_any_key(raw, ("msg_id", "message_id", "msgId", "messageId", "client_msg_id", "clientMsgId"))
    has_content = _has_any_key(raw, ("content", "msg_content", "message", "body", "text"))
    has_sender_or_time = _has_any_key(
        raw,
        (
            "sender_id",
            "sender_uid",
            "from_uid",
            "from_id",
            "senderId",
            "fromId",
            "create_time",
            "send_time",
            "timestamp",
            "msg_time",
            "sendTime",
        ),
    )
    has_explicit_type = _has_any_key(raw, ("msg_type", "message_type", "content_type"))
    return bool(has_message_id or (has_content and has_sender_or_time) or (has_explicit_type and has_content))


def _conversation_title(raw: dict[str, Any]) -> str:
    return _norm_text(_find_first(raw, ("title", "name", "conversation_name", "chat_name", "nick_name", "nickname", "user_name")))


def _conversation_preview(raw: dict[str, Any]) -> str:
    return _norm_text(_find_first(raw, ("last_message_text", "last_message", "latest_message", "preview", "summary", "snippet")))


def _conversation_unread(raw: dict[str, Any]) -> int | None:
    unread = _find_first(raw, ("unread_count", "unread", "unread_num", "unreadNum"))
    try:
        if unread is None:
            return None
        return int(unread)
    except Exception:
        return None


def _message_key(conv_key: str, raw: dict[str, Any]) -> tuple[str, str]:
    msg_id = _norm_text(_find_first(raw, ("msg_id", "message_id", "msgId", "messageId", "client_msg_id", "clientMsgId")))
    ts = _norm_text(_find_first(raw, ("create_time", "send_time", "timestamp", "time", "msg_time", "sendTime")))
    sender = _norm_text(_find_first(raw, ("sender_id", "sender_uid", "from_uid", "from_id", "senderId", "fromId")))
    text = _content_text(_find_first(raw, ("content", "msg_content", "message", "body", "text", "data")))
    text_key = re.sub(r"\s+", " ", text)
    text_key = re.sub(r"[^\w\u4e00-\u9fff:/.-]+", "", text_key)[:180]
    if msg_id:
        return f"{conv_key}|{msg_id}", "high"
    if ts and sender and text_key:
        return f"{conv_key}|{ts}|{sender}|{text_key}", "medium"
    return f"{conv_key}|{sender}|{text_key}", "low"


def _coerce_conversation(raw: dict[str, Any], *, url: str, path: str, now: str) -> dict[str, Any] | None:
    key = _conversation_key(raw)
    if not key:
        return None
    title = _conversation_title(raw)
    preview = _conversation_preview(raw)
    conv_id = _norm_text(_find_first(raw, ("conversation_id", "conversationId", "conversationID"))) or ""
    session_id = _norm_text(_find_first(raw, ("session_id", "sessionId", "sessionID", "chat_session_id", "chatSessionId", "chatSessionID"))) or ""
    participants = []
    for field_name in ("participants", "members", "users", "chat_users"):
        value = raw.get(field_name)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    participants.append(item.strip())
                elif isinstance(item, dict):
                    name = _norm_text(_find_first(item, ("name", "nick_name", "nickname", "user_name", "display_name")))
                    if name:
                        participants.append(name)
    out = {
        "conversation_key": key,
        "title": title,
        "list_preview": preview,
        "conversation_id": conv_id,
        "session_id": session_id,
        "unread_count": _conversation_unread(raw),
        "last_message_text": preview,
        "last_message_time": _norm_text(_find_first(raw, ("last_message_time", "last_time", "update_time", "timestamp", "time"))) or "",
        "participants": sorted({p for p in participants if p}),
        "source_urls": [url] if url else [],
        "source_paths": [path] if path else [],
        "first_seen_at": now,
        "last_seen_at": now,
        "seen_count": 1,
        "structured_messages": [],
    }
    return out


def _coerce_message(raw: dict[str, Any], *, conv_key: str, url: str, path: str, now: str) -> dict[str, Any] | None:
    if not _looks_like_message_record(raw):
        return None
    text = _content_text(_find_first(raw, ("content", "msg_content", "message", "body", "text", "data")))
    msg_id = _norm_text(_find_first(raw, ("msg_id", "message_id", "msgId", "messageId", "client_msg_id", "clientMsgId")))
    ts = _norm_text(_find_first(raw, ("create_time", "send_time", "timestamp", "time", "msg_time", "sendTime")))
    sender_id = _norm_text(_find_first(raw, ("sender_id", "sender_uid", "from_uid", "from_id", "senderId", "fromId")))
    sender_name = _norm_text(_find_first(raw, ("sender_name", "from_name", "nick_name", "nickname", "user_name", "display_name")))
    direction = _norm_text(_find_first(raw, ("direction", "send_type", "is_self", "isMyself", "self", "from_self")))
    if isinstance(direction, str):
        direction = direction.lower()
    if not any([msg_id, ts, text, sender_id, sender_name]):
        return None
    key, confidence = _message_key(conv_key, raw)
    message_type = _message_type_from_text(text, raw)
    return {
        "message_key": key,
        "conversation_key": conv_key,
        "message_id": msg_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "direction": direction,
        "message_type": message_type,
        "text": text,
        "timestamp": ts,
        "source_url": url,
        "source_path": path,
        "raw_path": "",
        "first_seen_at": now,
        "last_seen_at": now,
        "seen_count": 1,
        "dedupe_confidence": confidence,
    }


def _infer_payload_conversation_key(payload: Any) -> str | None:
    """Find a likely conversation key on the enclosing response.

    Some history APIs keep ``conversation_id`` or ``session_id`` at the response
    root while individual message items only contain msg_id/sender/content.
    """
    if isinstance(payload, dict):
        direct = _conversation_key(payload)
        if direct:
            return direct
    for _path, obj in _walk_json(payload):
        if not isinstance(obj, dict):
            continue
        if any(
            key in obj
            for key in (
                "conversation_id",
                "conversationId",
                "conversationID",
                "session_id",
                "sessionId",
                "sessionID",
                "chat_session_id",
                "chatSessionId",
                "chatSessionID",
            )
        ):
            key = _conversation_key(obj)
            if key:
                return key
    return None


class DouyinCaptureCollector:
    def __init__(self, cfg: dict[str, Any], out_dir: Path, master_dir: Path) -> None:
        capture_cfg = cfg.get("capture") or {}

        self.out_dir = out_dir
        self.master_dir = master_dir
        self.raw_dir = out_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._now = _now()
        self._response_count = 0
        self._websocket_count = 0
        self._seen_payload_hashes: set[str] = set()
        self._conversations: dict[str, dict[str, Any]] = {}
        self._messages_by_key: dict[str, dict[str, Any]] = {}
        self._capture_history = bool(capture_cfg.get("capture_history", True))
        self._dom_fallback = bool(capture_cfg.get("dom_fallback", True))
        self._poke_history = bool(capture_cfg.get("poke_history", True))
        self._poke_interval_sec = float(capture_cfg.get("poke_interval_sec", 8.0))
        self._last_poke_at = 0.0
        self._source_runs: list[str] = []

    def _record_raw(self, kind: str, url: str, payload: Any, *, path: str) -> Path:
        payload_text = _json_dump(payload)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        digest = _sha256_text(payload_text)[:16]
        rel = _slug(kind, limit=24)
        filename = f"{stamp}_{rel}_{digest}.json"
        folder = self.raw_dir / rel
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / filename
        wrapped = {
            "captured_at": _now(),
            "kind": kind,
            "url": url,
            "path": path,
            "payload": payload,
        }
        out.write_text(json.dumps(wrapped, ensure_ascii=False, indent=2), encoding="utf-8-sig")
        return out

    def _ingest_payload(self, payload: Any, *, url: str, path: str, kind: str, raw_path: str) -> None:
        now = _now()
        payload_conv_key = _infer_payload_conversation_key(payload) or "unknown"
        for item_path, obj in _walk_json(payload):
            if not isinstance(obj, dict):
                continue
            conv = _coerce_conversation(obj, url=url, path=item_path or path, now=now)
            if conv:
                existing = self._conversations.get(conv["conversation_key"])
                if not existing:
                    conv["source_urls"] = [u for u in conv["source_urls"] if u]
                    conv["source_paths"] = [p for p in conv["source_paths"] if p]
                    conv["source_runs"] = [self.out_dir.name]
                    conv["first_seen_at"] = now
                    conv["last_seen_at"] = now
                    conv["seen_count"] = 1
                    self._conversations[conv["conversation_key"]] = conv
                else:
                    existing["title"] = conv["title"] or existing.get("title") or ""
                    existing["list_preview"] = conv["list_preview"] or existing.get("list_preview") or ""
                    existing["conversation_id"] = conv["conversation_id"] or existing.get("conversation_id") or ""
                    existing["session_id"] = conv["session_id"] or existing.get("session_id") or ""
                    existing["unread_count"] = conv["unread_count"] if conv["unread_count"] is not None else existing.get("unread_count")
                    existing["last_message_text"] = conv["last_message_text"] or existing.get("last_message_text") or ""
                    existing["last_message_time"] = conv["last_message_time"] or existing.get("last_message_time") or ""
                    existing.setdefault("participants", [])
                    existing["participants"] = sorted(set(existing["participants"]) | set(conv["participants"]))
                    existing.setdefault("source_urls", [])
                    if url and url not in existing["source_urls"]:
                        existing["source_urls"].append(url)
                    existing.setdefault("source_paths", [])
                    if item_path and item_path not in existing["source_paths"]:
                        existing["source_paths"].append(item_path)
                    existing["last_seen_at"] = now
                    existing["seen_count"] = int(existing.get("seen_count") or 0) + 1

            conv_key = conv["conversation_key"] if conv else ""
            msg = _coerce_message(obj, conv_key=conv_key or payload_conv_key, url=url, path=item_path or path, now=now)
            if not msg:
                continue
            if not conv_key:
                msg["conversation_key"] = payload_conv_key
            msg["raw_path"] = raw_path
            existing_msg = self._messages_by_key.get(msg["message_key"])
            if not existing_msg:
                self._messages_by_key[msg["message_key"]] = msg
                if msg["conversation_key"] != "unknown":
                    conv_store = self._conversations.setdefault(
                        msg["conversation_key"],
                        {
                            "conversation_key": msg["conversation_key"],
                            "title": "",
                            "list_preview": "",
                            "conversation_id": "",
                            "session_id": "",
                            "unread_count": None,
                            "last_message_text": "",
                            "last_message_time": "",
                            "participants": [],
                            "source_urls": [],
                            "source_paths": [],
                            "first_seen_at": now,
                            "last_seen_at": now,
                            "seen_count": 0,
                            "structured_messages": [],
                        },
                    )
                    conv_store.setdefault("structured_messages", [])
                    conv_store["structured_messages"].append(msg)
            else:
                existing_msg["last_seen_at"] = now
                existing_msg["seen_count"] = int(existing_msg.get("seen_count") or 0) + 1
                if not existing_msg.get("timestamp") and msg.get("timestamp"):
                    existing_msg["timestamp"] = msg["timestamp"]

    def _merge_dom_conversation(self, dom: dict[str, Any]) -> None:
        title = _norm_text(dom.get("title")) or "selected_conversation"
        conv_key = _slug(title, limit=80)
        now = _now()
        conv = self._conversations.setdefault(
            conv_key,
            {
                "conversation_key": conv_key,
                "title": title,
                "list_preview": "",
                "conversation_id": "",
                "session_id": "",
                "unread_count": None,
                "last_message_text": "",
                "last_message_time": "",
                "participants": [],
                "source_urls": [SESSION_URL],
                "source_paths": ["dom.selected_chat"],
                "first_seen_at": now,
                "last_seen_at": now,
                "seen_count": 0,
                "structured_messages": [],
            },
        )
        conv["title"] = title
        if dom.get("source"):
            conv.setdefault("participants", [])
            if dom["source"] not in conv["participants"]:
                conv["participants"].append(dom["source"])
        conv["last_seen_at"] = now
        conv["seen_count"] = int(conv.get("seen_count") or 0) + 1

        existing = {
            str(msg.get("message_key")): msg
            for msg in conv.setdefault("structured_messages", [])
            if isinstance(msg, dict) and msg.get("message_key")
        }
        for index, msg in enumerate(dom.get("messages") or []):
            text = _norm_text(msg.get("text"))
            if not text:
                continue
            timestamp = _norm_text(msg.get("timestamp"))
            direction = _norm_text(msg.get("direction")) or "unknown"
            sender = _norm_text(msg.get("sender_name"))
            key_source = f"{conv_key}|{timestamp}|{direction}|{sender}|{text}"
            msg_key = f"{conv_key}|dom|{_sha256_text(key_source)[:20]}"
            if msg_key in existing:
                existing[msg_key]["last_seen_at"] = now
                existing[msg_key]["seen_count"] = int(existing[msg_key].get("seen_count") or 0) + 1
                continue
            merged = {
                "message_key": msg_key,
                "conversation_key": conv_key,
                "message_id": "",
                "sender_id": "",
                "sender_name": sender,
                "direction": direction,
                "message_type": "text",
                "text": text,
                "timestamp": timestamp,
                "source_url": SESSION_URL,
                "source_path": "dom.selected_chat",
                "raw_path": "",
                "first_seen_at": now,
                "last_seen_at": now,
                "seen_count": 1,
                "dedupe_confidence": "medium",
                "is_read": bool(msg.get("is_read")),
                "screen_rect": msg.get("screen_rect") or [],
                "dom_index": index,
            }
            conv["structured_messages"].append(merged)
            self._messages_by_key[msg_key] = merged
            existing[msg_key] = merged
        if conv.get("structured_messages"):
            last = conv["structured_messages"][-1]
            conv["last_message_text"] = str(last.get("text") or "")
            conv["last_message_time"] = str(last.get("timestamp") or "")

    async def _capture_dom_current_conversation(self, page: Page) -> None:
        if not self._dom_fallback:
            return
        try:
            dom = await page.evaluate(EXTRACT_CURRENT_CHAT_JS)
        except Exception as e:
            log.debug("dom fallback skipped: %s", e)
            return
        if not isinstance(dom, dict) or not dom.get("messages"):
            return
        raw_path = self._record_raw("dom", page.url, dom, path="dom.selected_chat")
        self._merge_dom_conversation(dom)
        log.info(
            "dom fallback captured conversation=%s messages=%d raw=%s",
            dom.get("title") or "selected_conversation",
            len(dom.get("messages") or []),
            raw_path,
        )

    async def on_response(self, resp: Response) -> None:
        url = resp.url
        if not any(marker in url for marker in TARGET_HOST_MARKERS):
            return
        if resp.status >= 400:
            return
        try:
            body = await resp.text()
        except Exception:
            return
        text = body.strip()
        if not text:
            return
        payload = _safe_json_loads(text)
        if payload is None:
            return
        payload_hash = _sha256_text(_json_dump(payload))
        if payload_hash in self._seen_payload_hashes:
            return
        self._seen_payload_hashes.add(payload_hash)
        self._response_count += 1
        raw_path = self._record_raw("response", url, payload, path="")
        self._ingest_payload(payload, url=url, path="", kind="response", raw_path=str(raw_path))
        log.debug("response #%d captured from %s", self._response_count, url)

    async def on_websocket(self, ws: WebSocket) -> None:
        url = ws.url
        if not any(marker in url for marker in TARGET_HOST_MARKERS):
            return

        async def _framereceived(frame: Any) -> None:
            data = getattr(frame, "payload", None)
            if data is None:
                data = getattr(frame, "text", None)
            if data is None:
                return
            text = str(data).strip()
            if not text or len(text) < 2:
                return
            payload = _safe_json_loads(text)
            if payload is None:
                return
            payload_hash = _sha256_text(_json_dump(payload))
            if payload_hash in self._seen_payload_hashes:
                return
            self._seen_payload_hashes.add(payload_hash)
            self._websocket_count += 1
            raw_path = self._record_raw("websocket", url, payload, path="")
            self._ingest_payload(payload, url=url, path="", kind="websocket", raw_path=str(raw_path))
            log.debug("websocket #%d captured from %s", self._websocket_count, url)

        ws.on("framereceived", _framereceived)

    def _merge_master(self) -> None:
        self.master_dir.mkdir(parents=True, exist_ok=True)
        master_path = self.master_dir / "recent_chats.json"
        now = _now()
        if master_path.is_file():
            master = json.loads(master_path.read_text(encoding="utf-8-sig"))
        else:
            master = {
                "exported_at": now,
                "updated_at": now,
                "source_runs": [],
                "conversations": [],
            }

        run_name = self.out_dir.name
        master.setdefault("source_runs", [])
        if run_name not in master["source_runs"]:
            master["source_runs"].append(run_name)

        convs_by_key: dict[str, dict[str, Any]] = {}
        for conv in master.get("conversations", []):
            if isinstance(conv, dict) and conv.get("conversation_key"):
                convs_by_key[str(conv["conversation_key"])] = conv

        for conv_key, conv in self._conversations.items():
            existing = convs_by_key.get(conv_key)
            if not existing:
                existing = dict(conv)
                existing["index"] = len(convs_by_key) + 1
                existing["source_runs"] = [run_name]
                convs_by_key[conv_key] = existing
            else:
                existing["title"] = conv.get("title") or existing.get("title") or ""
                existing["list_preview"] = conv.get("list_preview") or existing.get("list_preview") or ""
                existing["conversation_id"] = conv.get("conversation_id") or existing.get("conversation_id") or ""
                existing["session_id"] = conv.get("session_id") or existing.get("session_id") or ""
                existing["unread_count"] = conv.get("unread_count") if conv.get("unread_count") is not None else existing.get("unread_count")
                existing["last_message_text"] = conv.get("last_message_text") or existing.get("last_message_text") or ""
                existing["last_message_time"] = conv.get("last_message_time") or existing.get("last_message_time") or ""
                existing.setdefault("participants", [])
                existing["participants"] = sorted(set(existing["participants"]) | set(conv.get("participants") or []))
                existing.setdefault("source_runs", [])
                if run_name not in existing["source_runs"]:
                    existing["source_runs"].append(run_name)
                existing.setdefault("source_urls", [])
                for url in conv.get("source_urls") or []:
                    if url not in existing["source_urls"]:
                        existing["source_urls"].append(url)
                existing.setdefault("source_paths", [])
                for p in conv.get("source_paths") or []:
                    if p not in existing["source_paths"]:
                        existing["source_paths"].append(p)
                existing["last_seen_at"] = now
                existing["seen_count"] = int(existing.get("seen_count") or 0) + int(conv.get("seen_count") or 1)

            existing_msgs = existing.setdefault("structured_messages", [])
            by_msg_key = {str(msg.get("message_key")): msg for msg in existing_msgs if isinstance(msg, dict) and msg.get("message_key")}
            for msg in conv.get("structured_messages") or []:
                if not isinstance(msg, dict):
                    continue
                key = str(msg.get("message_key") or "")
                if not key:
                    continue
                if key in by_msg_key:
                    target = by_msg_key[key]
                    target["last_seen_at"] = now
                    target["seen_count"] = int(target.get("seen_count") or 0) + 1
                    if not target.get("timestamp") and msg.get("timestamp"):
                        target["timestamp"] = msg["timestamp"]
                    continue
                merged = dict(msg)
                merged["first_seen_at"] = merged.get("first_seen_at") or now
                merged["last_seen_at"] = now
                merged["seen_count"] = int(merged.get("seen_count") or 1)
                existing_msgs.append(merged)
                by_msg_key[key] = merged
            existing["messages"] = [str(m.get("text") or "") for m in existing_msgs]

        master["updated_at"] = now
        master["exported_at"] = master.get("exported_at") or now
        master["conversations"] = sorted(convs_by_key.values(), key=lambda c: int(c.get("index") or 999999))
        master["summary"] = {
            "conversation_count": len(master["conversations"]),
            "message_count": sum(len(c.get("structured_messages") or []) for c in master["conversations"]),
        }
        master_path.write_text(json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8-sig")
        log.info("merged master -> %s", master_path)

    def _write_run_export(self) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        exported_at = _now()
        conversations = sorted(
            self._conversations.values(),
            key=lambda c: (int(c.get("index") or 999999), str(c.get("title") or c.get("conversation_key") or "")),
        )
        run_data = {
            "exported_at": exported_at,
            "page_url": SESSION_URL,
            "source_runs": [self.out_dir.name],
            "summary": {
                "conversation_count": len(conversations),
                "message_count": sum(len(c.get("structured_messages") or []) for c in conversations),
                "response_count": self._response_count,
                "websocket_count": self._websocket_count,
            },
            "conversations": conversations,
        }
        out_path = self.out_dir / "recent_chats.json"
        out_path.write_text(json.dumps(run_data, ensure_ascii=False, indent=2), encoding="utf-8-sig")
        return out_path

    def _write_markdown(self) -> Path:
        exported_at = _now()
        out_path = self.out_dir / "recent_chats.md"
        parts = [f"# Douyin Recent Chats\n\nExported at: {exported_at}\n"]
        for conv in sorted(self._conversations.values(), key=lambda c: str(c.get("title") or c.get("conversation_key") or "")):
            parts.append(f"\n## {conv.get('title') or conv.get('conversation_key')}\n")
            if conv.get("list_preview"):
                parts.append(f"\nList preview: {conv['list_preview']}\n")
            if conv.get("conversation_id") or conv.get("session_id"):
                parts.append(
                    f"\nConversation ID: {conv.get('conversation_id') or '-'}"
                    f"\nSession ID: {conv.get('session_id') or '-'}\n"
                )
            for msg in conv.get("structured_messages") or []:
                if not isinstance(msg, dict):
                    continue
                ts = msg.get("timestamp") or ""
                sender = msg.get("sender_name") or msg.get("sender_id") or msg.get("direction") or "unknown"
                text = msg.get("text") or ""
                prefix = f"[{ts}] " if ts else ""
                parts.append(f"- {prefix}{sender}: {text}\n")
        out_path.write_text("".join(parts), encoding="utf-8-sig")
        return out_path

    async def _poke_page(self, page: Page) -> None:
        if not self._capture_history:
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_poke_at < self._poke_interval_sec:
            return
        self._last_poke_at = now
        try:
            await page.mouse.wheel(0, -1200)
            await asyncio.sleep(0.8)
            await page.mouse.wheel(0, 1400)
            await asyncio.sleep(0.8)
        except Exception as e:
            log.debug("poke page skipped: %s", e)

    async def run(
        self,
        page: Page,
        *,
        duration_sec: float | None = None,
        reload_page: bool = True,
    ) -> None:
        log.info("Douyin capture started")
        page.on("response", self.on_response)
        page.on("websocket", lambda ws: asyncio.create_task(self.on_websocket(ws)))

        try:
            if not any(marker in page.url for marker in DOUYIN_PAGE_MARKERS):
                log.info("navigate to session page: %s", LIFE_SESSION_URL)
                await page.goto(LIFE_SESSION_URL, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(3)
            if reload_page:
                await page.reload(wait_until="domcontentloaded", timeout=20_000)
                await asyncio.sleep(2.5)
        except Exception as e:
            log.warning("page reload skipped: %s", e)

        loop_count = 0
        started_at = asyncio.get_running_loop().time()
        try:
            while True:
                if duration_sec is not None and asyncio.get_running_loop().time() - started_at >= duration_sec:
                    log.info("duration reached: %.1fs", duration_sec)
                    break
                loop_count += 1
                if self._capture_history:
                    await self._poke_page(page)
                await asyncio.sleep(3)
                await self._capture_dom_current_conversation(page)
                if loop_count % 4 == 0:
                    log.info(
                        "loop=%d conversations=%d messages=%d responses=%d ws=%d",
                        loop_count,
                        len(self._conversations),
                        len(self._messages_by_key),
                        self._response_count,
                        self._websocket_count,
                    )
        except KeyboardInterrupt:
            raise
        finally:
            await self._capture_dom_current_conversation(page)
            run_json = self._write_run_export()
            md_path = self._write_markdown()
            if not any(m.get("message_key") for c in self._conversations.values() for m in c.get("structured_messages", [])):
                log.warning("no structured messages captured; check payload schema or the active page")
            self._merge_master()
            log.info("written: %s", run_json)
            log.info("written: %s", md_path)


def _find_douyin_page(browser: Browser) -> Page | None:
    for ctx in browser.contexts:
        for page in ctx.pages:
            if any(marker in page.url for marker in DOUYIN_PAGE_MARKERS):
                return page
    return None


async def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Export Douyin CS conversations from an attached Chrome")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "config.yaml")
    parser.add_argument("--cdp", default=CDP_URL)
    parser.add_argument("--out-dir", type=Path, default=Path("exports") / "douyin_recent")
    parser.add_argument("--master-dir", type=Path, default=Path("exports") / "douyin_master")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-poke", action="store_true", help="Do not send wheel probes to trigger history loading.")
    parser.add_argument("--duration-sec", type=float, default=None, help="Stop automatically after this many seconds.")
    parser.add_argument("--no-reload", action="store_true", help="Keep the current browser state and do not reload the page.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    if args.no_poke:
        cfg.setdefault("capture", {})
        cfg["capture"]["poke_history"] = False

    collector = DouyinCaptureCollector(cfg, args.out_dir, args.master_dir)

    async with async_playwright() as pw:
        log.info("connect chrome cdp: %s", args.cdp)
        try:
            browser = await pw.chromium.connect_over_cdp(args.cdp)
        except Exception as e:
            log.error("unable to connect Chrome CDP: %s", e)
            return 1

        page = _find_douyin_page(browser)
        if not page:
            log.info("no open douyin page found, opening a new tab")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            await page.goto(LIFE_SESSION_URL, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(4)

        log.info("using page: %s", page.url)
        try:
            await collector.run(page, duration_sec=args.duration_sec, reload_page=not args.no_reload)
        except KeyboardInterrupt:
            log.info("stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
