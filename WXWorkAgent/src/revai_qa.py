"""RevAI Carzone respond client for text-only reply generation."""
from __future__ import annotations

import os
import re
from typing import Any

import requests


class RevaiQaClient:
    def __init__(
        self,
        base_url: str = "https://car.revai.cn",
        endpoint: str = "/api/project/carzone/compat/chat/respond",
        api_key: str | None = None,
        timeout: int = 60,
        session_prefix: str = "wxwork",
        message_mode: str = "latest_new",
        channel: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        self.api_key = api_key or os.getenv("REVAI_API_KEY", "")
        self.timeout = timeout
        self.session_prefix = session_prefix.strip() or "wxwork"
        self.message_mode = message_mode.strip().lower()
        self.channel = (channel or "").strip()
        self.model = "revai-carzone-respond"

    def complete(self, system: str, user: str, *, session_id: str | None = None) -> str:
        message = self._message_text(system, user)
        payload: dict[str, Any] = {
            "session_id": self._session_id(session_id),
            "message": message,
            "message_type": "text",
        }
        if self.channel:
            payload["channel"] = self.channel
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        r = requests.post(
            f"{self.base_url}{self.endpoint}",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        if not r.ok:
            body = r.text.strip()
            raise RuntimeError(f"RevAI HTTP {r.status_code}: {body[:1000]}")
        data = r.json()
        reply = data.get("reply")
        if not reply or not str(reply).strip():
            raise RuntimeError(f"Empty RevAI output: {data}")
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
    for line in reversed(prompt.splitlines()):
        if "<-- new" not in line:
            continue
        text = line.split("<-- new", 1)[0].strip()
        match = re.match(r"^\[[^\]]*\]\s+.*?\s+\w+:\s*(.*)$", text)
        if match:
            return match.group(1).strip()
        if ":" in text:
            return text.rsplit(":", 1)[-1].strip()
        return text
    return ""
