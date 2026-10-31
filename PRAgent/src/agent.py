"""
GoogleNewsAgent - main agent loop for PRAgent.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
from pathlib import Path
from typing import Any

import yaml
from playwright.async_api import async_playwright

from .executor import Executor
from .extractor import Extractor
from .omniparser_client import OmniParserClient
from .qwen_client import QwenVLClient


def _load_config() -> dict[str, Any]:
    config_path = Path(__file__).parent.parent / "config.yaml"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


MAX_RETRIES = 3


class GoogleNewsAgent:
    def __init__(
        self,
        keyword: str = "XREAL",
        pages: int = 30,
        output_path: str = "news_output.csv",
    ):
        self.keyword = keyword
        self.pages = pages
        self.output_path = output_path
        self.news_data: list[dict[str, Any]] = []
        self._last_saved_count = 0
        self._selector_cache: dict[str, str] = {}  # cache for next_page etc.

        cfg = _load_config()
        global_cfg = cfg.get("global") or {}
        omni_cfg = cfg.get("omniparser") or {}
        api_key = global_cfg.get("api_key") or os.getenv("DASHSCOPE_API_KEY")
        self.qwen = QwenVLClient(
            api_key=api_key,
            base_url=global_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            model=global_cfg.get("model", "qwen-vl-plus"),
            temperature=global_cfg.get("temperature", 0.3),
            timeout=global_cfg.get("timeout", 30),
        )
        self.omniparser: OmniParserClient | None = None
        if omni_cfg.get("enabled"):
            self.omniparser = OmniParserClient(url=omni_cfg.get("url", "http://localhost:8000"))
            if not self.omniparser.probe():
                self.omniparser = None

    def _get_dom_summary(self, html: str) -> str:
        """Extract a brief summary of interactive elements from HTML."""
        import re
        # Simple heuristic: links and buttons
        links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{3,80})</a>', html)
        buttons = re.findall(r'<button[^>]*>([^<]{0,50})</button>', html)
        parts = []
        for href, text in links[:20]:
            text = text.strip()[:60]
            if text and "Next" not in text and "More" not in text:
                parts.append(f"link: {text}")
        for b in buttons[:5]:
            if b.strip():
                parts.append(f"button: {b.strip()[:40]}")
        parts.append("link/button: Next, More stories")
        return "\n".join(parts[:30]) if parts else "Interactive elements: links, buttons (Next, More stories)"

    async def run(self, headless: bool = False):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_default_timeout(30000)
            try:
                # Navigate directly to search results
                search_url = f"https://news.google.com/search?q={self.keyword.replace(' ', '+')}"
                await page.goto(search_url, wait_until="load", timeout=15000)
                await page.wait_for_timeout(3000)

                # Dismiss cookie/consent if present
                for btn in ['button:has-text("Accept")', 'button:has-text("I agree")', '[aria-label="Accept"]']:
                    try:
                        await page.click(btn, timeout=2000)
                        await page.wait_for_timeout(500)
                        break
                    except Exception:
                        pass

                for page_num in range(self.pages):
                    print(f"  Page {page_num + 1}/{self.pages}...", flush=True)
                    screenshot = await page.screenshot()
                    if self.omniparser:
                        parsed = self.omniparser.parse(screenshot)
                        elements = json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, list) else (parsed or "")
                        if not elements:
                            elements = self._get_dom_summary(await page.content())
                    else:
                        html = await page.content()
                        elements = self._get_dom_summary(html)
                    goal = f"Search {self.keyword} news, page {page_num + 1}/{self.pages}. Extract news, then go to next page or done."
                    history = [{"title": n.get("title", "")} for n in self.news_data[-10:]]

                    action = await self.qwen.decide(
                        elements=elements,
                        goal=goal,
                        history=history,
                        page_num=page_num,
                        total_pages=self.pages,
                        use_rule_first=True,
                    )

                    if action.get("type") == "done":
                        break

                    ok = False
                    for attempt in range(MAX_RETRIES):
                        ok = await Executor.execute(action, page)
                        if ok:
                            break
                        if attempt < MAX_RETRIES - 1:
                            await page.wait_for_timeout(1000)
                            screenshot = await page.screenshot()
                            html = await page.content()
                            if self.omniparser:
                                parsed = self.omniparser.parse(screenshot)
                                elements = json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, list) and parsed else self._get_dom_summary(html)
                            else:
                                elements = self._get_dom_summary(html)
                            action = await self.qwen.decide(
                                elements=elements,
                                goal=f"Previous action failed. Retry. Goal: {goal}",
                                history=history,
                                page_num=page_num,
                                total_pages=self.pages,
                                use_rule_first=False,
                            )
                            if action.get("type") == "done":
                                break

                    if not ok and action.get("type") == "next_page":
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(1500)

                    news = await Extractor.extract(page, self.keyword)
                    self.news_data.extend(news)
                    print(f"    Extracted {len(news)} items (total: {len(self.news_data)})", flush=True)
                    self._save_incremental()

                    if action.get("type") != "done":
                        await page.wait_for_timeout(1500)
            finally:
                await browser.close()

        self._save_final()

    def _save_incremental(self):
        """Append only new rows since last save."""
        new_count = len(self.news_data) - self._last_saved_count
        if new_count <= 0:
            return
        path = Path(self.output_path)
        new_rows = self.news_data[self._last_saved_count :]
        if path.suffix.lower() == ".csv":
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["title", "link", "source", "time", "snippet"])
                if not path.exists() or path.stat().st_size == 0:
                    w.writeheader()
                for row in new_rows:
                    w.writerow(row)
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.news_data, f, ensure_ascii=False, indent=2)
        self._last_saved_count = len(self.news_data)

    def _save_final(self):
        """Write final output, deduplicated."""
        path = Path(self.output_path)
        seen = set()
        unique = []
        for n in self.news_data:
            k = n.get("link") or n.get("title", "")
            if k not in seen:
                seen.add(k)
                unique.append(n)
        self.news_data = unique

        if path.suffix.lower() == ".csv":
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["title", "link", "source", "time", "snippet"])
                w.writeheader()
                w.writerows(self.news_data)
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.news_data, f, ensure_ascii=False, indent=2)
