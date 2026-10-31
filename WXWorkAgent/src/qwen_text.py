"""DashScope OpenAI-compatible chat (text-only) for reply generation."""
from __future__ import annotations

import os
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

    def complete(self, system: str, user: str, *, session_id: str | None = None) -> str:
        _ = session_id
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system.strip()},
                {"role": "user", "content": user.strip()},
            ],
            "temperature": self.temperature,
        }
        r = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        msg = data.get("choices", [{}])[0].get("message", {}).get("content")
        if not msg or not str(msg).strip():
            raise RuntimeError(f"Empty model output: {data}")
        return str(msg).strip()
