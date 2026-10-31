"""RevAI Carzone respond client for text-only reply generation."""
from __future__ import annotations

import os
import re
import time
from typing import Any

import requests


class RevaiQaClient:
    def __init__(
        self,
        base_url: str = "https://car.revai.cn",
        endpoint: str = "/api/project/carzone/compat/chat/respond",
        api_key: str | None = None,
        timeout: int = 60,
        session_prefix: str = "douyin",
        message_mode: str = "latest_new",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        self.api_key = api_key or os.getenv("REVAI_API_KEY", "")
        self.timeout = timeout
        self.session_prefix = session_prefix.strip() or "douyin"
        self.message_mode = message_mode.strip().lower()
        self.model = "revai-carzone-respond"
        self.last_profile: dict[str, Any] = {}
        self.last_response: dict[str, Any] = {}

    def complete(self, system: str, user: str, *, session_id: str | None = None) -> str:
        total_started = time.perf_counter()
        build_started = time.perf_counter()
        message = self._message_text(system, user)
        payload: dict[str, Any] = {
            "session_id": self._session_id(session_id),
            "message": message,
            "message_type": "text",
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        build_ms = round((time.perf_counter() - build_started) * 1000, 1)
        http_started = time.perf_counter()
        resp = requests.post(
            f"{self.base_url}{self.endpoint}",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        http_ms = round((time.perf_counter() - http_started) * 1000, 1)
        if not resp.ok:
            body = resp.text.strip()
            self.last_profile = {
                "provider": "revai",
                "model": self.model,
                "message_build_ms": build_ms,
                "http_ms": http_ms,
                "total_ms": round((time.perf_counter() - total_started) * 1000, 1),
                "status_code": resp.status_code,
                "request_chars": len(message),
                "endpoint": self.endpoint,
            }
            raise RuntimeError(f"RevAI HTTP {resp.status_code}: {body[:1000]}")
        parse_started = time.perf_counter()
        data = resp.json()
        parse_ms = round((time.perf_counter() - parse_started) * 1000, 1)
        self.last_response = data
        reply = data.get("reply")
        if not reply or not str(reply).strip():
            raise RuntimeError(f"Empty RevAI output: {data}")
        remote_profile = data.get("profiling") or (data.get("baseline") or {}).get("_profile") or {}
        self.last_profile = {
            "provider": "revai",
            "model": self.model,
            "message_build_ms": build_ms,
            "http_ms": http_ms,
            "json_parse_ms": parse_ms,
            "total_ms": round((time.perf_counter() - total_started) * 1000, 1),
            "status_code": resp.status_code,
            "request_chars": len(message),
            "reply_chars": len(str(reply)),
            "endpoint": self.endpoint,
            "remote": remote_profile,
        }
        return str(reply).strip()

    def _session_id(self, session_id: str | None) -> str:
        if session_id and session_id.strip():
            safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in session_id.strip())
            return f"{self.session_prefix}-{safe}"[:128]
        return f"{self.session_prefix}-default"

    def _message_text(self, system: str, user: str) -> str:
        if self.message_mode == "full_prompt":
            return "\n\n".join(part.strip() for part in (system, user) if part and part.strip())
        message = _extract_latest_new_message(user)
        if message:
            return message
        return user.strip() or system.strip()


def _extract_latest_new_message(prompt: str) -> str:
    messages: list[str] = []
    for line in prompt.splitlines():
        if "<-- new" not in line:
            continue
        text = line.split("<-- new", 1)[0].strip()
        match = re.match(r"^\[[^\]]*\]\s+.*?\s+\w+:\s*(.*)$", text)
        if match:
            messages.append(match.group(1).strip())
        elif ":" in text:
            messages.append(text.rsplit(":", 1)[-1].strip())
        else:
            messages.append(text)
    return "\n".join(message for message in messages if message)


def _compose_reply(data: dict[str, Any], message: str) -> str:
    reply = str(data.get("reply") or "").strip()
    extras: list[str] = []

    group_buy = data.get("group_buy_context") or {}
    summary = str(group_buy.get("summary_text") or "").strip()
    asks_package = bool(re.search(r"套餐|价格|报价|多少钱|团购|保养|项目", message or ""))
    needs_structured_tail = reply.endswith(("：", ":")) or asks_package
    if summary and group_buy.get("available") and needs_structured_tail and summary not in reply:
        extras.append(summary)

    recommendations = data.get("recommendations") or []
    if recommendations and (reply.endswith(("：", ":")) or "建议" in reply):
        rendered = _render_recommendations(recommendations)
        if rendered and rendered not in reply:
            extras.append(rendered)

    parts = [part for part in [reply, *extras] if part]
    return "\n".join(parts)


def _render_recommendations(items: list[Any]) -> str:
    rows: list[str] = []
    for index, item in enumerate(items[:5], start=1):
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            name = item.get("name") or item.get("title") or item.get("item_name") or item.get("service_item")
            reason = item.get("reason") or item.get("desc") or item.get("description") or item.get("note")
            price = item.get("price") or item.get("group_buy_price") or item.get("market_price")
            pieces = [str(part).strip() for part in (name, reason, f"{price}元" if price else "") if part]
            text = "｜".join(pieces)
        else:
            text = str(item).strip()
        if text:
            rows.append(f"{index}. {text}")
    return "\n".join(rows)
