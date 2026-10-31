"""DashScope OpenAI-compatible chat (text-only) for reply generation."""
from __future__ import annotations

import os
import time
from typing import Any

import requests


class QwenTextClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        model: str = "qwen-turbo",
        temperature: float = 0.3,
        timeout: int = 60,
    ) -> None:
        key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        if not key:
            raise ValueError("Set DASHSCOPE_API_KEY or pass api_key")
        self.api_key = key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.last_profile: dict[str, Any] = {}

    def complete(self, system: str, user: str, *, session_id: str | None = None) -> str:
        total_started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system.strip()},
                {"role": "user", "content": user.strip()},
            ],
            "temperature": self.temperature,
        }
        http_started = time.perf_counter()
        r = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        http_ms = round((time.perf_counter() - http_started) * 1000, 1)
        self.last_profile = {
            "provider": "qwen",
            "model": self.model,
            "http_ms": http_ms,
            "total_ms": round((time.perf_counter() - total_started) * 1000, 1),
            "status_code": r.status_code,
            "request_chars": len(system.strip()) + len(user.strip()),
        }
        r.raise_for_status()
        parse_started = time.perf_counter()
        data = r.json()
        self.last_profile["json_parse_ms"] = round((time.perf_counter() - parse_started) * 1000, 1)
        usage = data.get("usage") or {}
        if isinstance(usage, dict):
            self.last_profile["usage"] = {str(key): value for key, value in usage.items() if value is not None}
        msg = data.get("choices", [{}])[0].get("message", {}).get("content")
        if not msg or not str(msg).strip():
            raise RuntimeError(f"Empty model output: {data}")
        self.last_profile["reply_chars"] = len(str(msg))
        self.last_profile["total_ms"] = round((time.perf_counter() - total_started) * 1000, 1)
        return str(msg).strip()
