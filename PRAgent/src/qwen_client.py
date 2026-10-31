"""
Qwen-VL API client for PRAgent decision making.
Uses DashScope OpenAI-compatible REST API (works without openai package).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import requests


class QwenVLClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        model: str = "qwen-vl-plus",
        temperature: float = 0.3,
        timeout: int = 30,
    ):
        key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        if not key:
            raise ValueError("Set DASHSCOPE_API_KEY env or pass api_key")
        self.api_key = key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self._prompt_template = self._load_prompt()

    def _load_prompt(self) -> str:
        prompt_path = Path(__file__).parent.parent / "prompts" / "decision.txt"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return (
            "Task: {goal}\n"
            "Recent news: {history}\n"
            "Elements: {elements}\n\n"
            'Return ONLY JSON: {"type": "click|type|scroll|next_page|done", "target": "...", "selector": "...", "text": "...", "reason": "..."}'
        )

    def _extract_json(self, text: str) -> dict[str, Any]:
        """Extract JSON from LLM response, handling markdown code blocks."""
        text = text.strip()
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if m:
            text = m.group(1).strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group(0))
        raise ValueError(f"No valid JSON found in: {text[:200]}")

    def _is_simple_action(self, elements: str | list, page_num: int, goal: str) -> bool:
        """Rule-based check for simple actions (e.g. next_page) to avoid API call."""
        if not elements:
            return False
        elem_str = elements if isinstance(elements, str) else json.dumps(elements)
        lower = elem_str.lower()
        # If we see "next" or "more" or pagination, could use rule
        if "next" in lower or "下一页" in lower or "more" in lower:
            return True
        return False

    def _rule_based_decision(
        self, elements: str | list, page_num: int, goal: str, total_pages: int
    ) -> dict[str, Any]:
        """Return next_page or done based on rules."""
        if page_num >= total_pages - 1:
            return {
                "type": "done",
                "target": "",
                "reason": "Reached last page",
            }
        return {
            "type": "next_page",
            "target": "next page / more results",
            "reason": "Rule: go to next page",
        }

    async def decide(
        self,
        elements: str | list,
        goal: str,
        history: list[dict[str, Any]] | None = None,
        page_num: int = 0,
        total_pages: int = 30,
        use_rule_first: bool = True,
    ) -> dict[str, Any]:
        """
        Decide next action. Prefer rule-based for simple cases to save API calls.
        """
        history = history or []
        history_str = json.dumps(history[-10:], ensure_ascii=False, indent=0)
        elem_str = elements if isinstance(elements, str) else json.dumps(elements, ensure_ascii=False)

        if use_rule_first and self._is_simple_action(elem_str, page_num, goal):
            return self._rule_based_decision(elem_str, page_num, goal, total_pages)

        prompt = self._prompt_template.format(
            goal=goal,
            history=history_str,
            elements=elem_str[:4000] if len(elem_str) > 4000 else elem_str,
        )

        def _sync_create():
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": self.temperature,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()

        try:
            result = await asyncio.to_thread(_sync_create)
        except AttributeError:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _sync_create)
        raw = result.get("choices", [{}])[0].get("message", {}).get("content") or "{}"
        return self._extract_json(raw)
