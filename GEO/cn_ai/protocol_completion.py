"""Strict current-turn binding for protocol answers used by early checkpoints."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse
from cn_ai.search_policy import prompt_variants


def normalized(text):
    return " ".join(str(text or "").split())


def _text(message):
    value = message.get("speech") or message.get("displayPrompt")
    if isinstance(value, str) and value:
        return value
    return "".join(str(f.get("content") or "") for f in message.get("fragments", [])
                   if f.get("type") in ("REQUEST", "TEXT") and isinstance(f.get("content"), str))


def deepseek_turn(body, prompt):
    """Return only the latest exact user turn and its finished direct child."""
    try:
        obj = json.loads(body)
        if obj.get("code") not in (0, None):
            return None
        data = obj.get("data") or {}
        messages = (data.get("biz_data") or {}).get("chat_messages") or data.get("chat_messages") or []
        users = [i for i, msg in enumerate(messages) if str(msg.get("role", "")).upper() == "USER"]
        if not users or not normalized(prompt):
            return None
        index = users[-1]
        user = messages[index]
        if normalized(_text(user)) not in [normalized(p) for p in prompt_variants('deepseek', prompt)]:
            return None
        replies = [msg for msg in messages[index+1:] if str(msg.get("role", "")).upper() == "ASSISTANT"]
        if not replies:
            return None
        reply = replies[-1]
        if user.get("message_id") is not None and reply.get("parent_id") != user["message_id"]:
            return None
        if str(reply.get("status", "")).upper() != "FINISHED" or reply.get("incomplete_message") or reply.get("has_pending_fragment"):
            return None
        fragments = [f for f in reply.get("fragments", []) if f.get("type") == "RESPONSE"]
        if not fragments or any(str(f.get("status", "FINISHED")).upper() != "FINISHED" for f in fragments):
            return None
        return json.dumps({"code": 0, "data": {"chat_messages": [user, reply]}}, ensure_ascii=False)
    except (ValueError, TypeError, AttributeError):
        return None


def yuanbao_turn(body, prompt, page_url):
    """Bind the latest human/AI pair to this page; DOM supplies completion."""
    try:
        obj = json.loads(body)
        data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
        convs = data.get("convs") or []
        match = re.search(r"/chat/(?:[A-Za-z0-9]+/)?([A-Za-z0-9]+)/?$", urlparse(page_url).path)
        if not match or str(data.get("id") or "") != match.group(1) or not normalized(prompt):
            return None
        ordered = sorted(convs, key=lambda c: int(c.get("index", 0)))
        users = [i for i, c in enumerate(ordered) if c.get("speaker") in ("human", "user")]
        if not users or normalized(_text(ordered[users[-1]])) != normalized(prompt):
            return None
        replies = [c for c in ordered[users[-1]+1:] if c.get("speaker") == "ai"]
        if not replies:
            return None
        reply = replies[-1]
        if reply.get("conversationId") and reply["conversationId"] != data["id"]:
            return None
        return json.dumps({"convs": [reply]}, ensure_ascii=False)
    except (ValueError, TypeError, AttributeError):
        return None


def yiyan_turn(body, prompt, page_url):
    """Select an exact query in the current page's session, never a substring."""
    try:
        obj = json.loads(body)
        if obj.get("status") not in (0, None):
            return None
        session = re.search(r"/search/([^/?#]+)", urlparse(page_url).path)
        if not session or not normalized(prompt):
            return None
        for item in (obj.get("data") or {}).get("historyData") or []:
            if str(item.get("sessionId", "")) != session.group(1):
                continue
            messages = item.get("msgList") or []
            if not messages:
                continue
            msg = messages[-1]
            query = msg.get("query")
            if isinstance(query, list):
                query = "".join((part.get("data") or {}).get("text", {}).get("query", "") for part in query)
            if normalized(query) != normalized(prompt):
                continue
            responses = msg.get("chatResponse") or []
            if any(str(r.get("sessionId", session.group(1))) != session.group(1) for r in responses):
                continue
            return json.dumps({"data": {"historyData": [{**item, "msgList": [msg]}]}}, ensure_ascii=False)
    except (ValueError, TypeError, AttributeError):
        return None
    return None
