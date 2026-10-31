"""
News extractor for Google News page content.
Uses DOM parsing - no API calls.
"""
from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Page


class Extractor:
    """Extract news entries from a Google News page."""

    @staticmethod
    async def extract(page: Page, keyword: str = "") -> list[dict[str, Any]]:
        """
        Extract news items from current page.
        Returns list of dicts: title, link, source, time, snippet.
        """
        items: list[dict[str, Any]] = []
        seen_links: set[str] = set()

        # Google News structures vary; try multiple selectors
        selectors = [
            "article",
            "[data-n-tid]",
            "a[href*='/articles/']",
            'div[role="article"]',
            ".SoaBEf",
            "c-wiz[data-n-tid]",
        ]

        for sel in selectors:
            try:
                elements = await page.locator(sel).all()
                for el in elements:
                    try:
                        item = await Extractor._parse_element(el, keyword)
                        if item and item.get("link") and item["link"] not in seen_links:
                            seen_links.add(item["link"])
                            items.append(item)
                    except Exception:
                        continue
                if items:
                    break
            except Exception:
                continue

        if not items:
            items = await Extractor._fallback_extract(page, seen_links, keyword)

        if not items:
            items = await Extractor._fallback_extract_any_news_links(page, seen_links)

        return items

    @staticmethod
    async def _parse_element(el: Any, keyword: str) -> dict[str, Any] | None:
        """Parse a single news element."""
        try:
            link_el = el.locator("a[href]").first
            href = await link_el.get_attribute("href")
            if not href or not (href.startswith("http") or href.startswith("/")):
                return None
            if "google.com/sorry" in href or "accounts.google" in href:
                return None
            if href.startswith("/"):
                href = "https://news.google.com" + href

            text = await el.inner_text()
            lines = [s.strip() for s in text.split("\n") if s.strip()]
            title = lines[0] if lines else ""
            if not title or len(title) < 3:
                return None

            if keyword and keyword.lower() not in title.lower():
                pass  # still include for context; filter optionally

            source = ""
            time_str = ""
            for line in lines[1:]:
                if re.match(r"\d+\s*(minute|hour|day|week)s?\s*ago", line, re.I):
                    time_str = line
                elif len(line) < 80 and not time_str:
                    source = line

            return {
                "title": title,
                "link": href,
                "source": source or "Unknown",
                "time": time_str or "",
                "snippet": lines[1] if len(lines) > 1 else "",
            }
        except Exception:
            return None

    @staticmethod
    async def _fallback_extract(
        page: Page, seen_links: set[str], keyword: str
    ) -> list[dict[str, Any]]:
        """Fallback: extract from all article links on page."""
        items: list[dict[str, Any]] = []
        links = await page.locator('a[href*="/articles/"], a[href*="news.google.com"]').all()
        for link_el in links:
            try:
                href = await link_el.get_attribute("href")
                if not href or href in seen_links:
                    continue
                if "google.com/sorry" in href or "accounts.google" in href:
                    continue
                title = (await link_el.inner_text()).strip()
                if len(title) < 5:
                    continue
                seen_links.add(href)
                items.append({
                    "title": title,
                    "link": href,
                    "source": "Unknown",
                    "time": "",
                    "snippet": "",
                })
            except Exception:
                continue
        return items

    @staticmethod
    async def _fallback_extract_any_news_links(
        page: Page, seen_links: set[str]
    ) -> list[dict[str, Any]]:
        """Broader fallback: any link that looks like a news article."""
        items: list[dict[str, Any]] = []
        for sel in ['a[href*="/articles/"]', 'a[href*="news.google.com"]', 'article a[href]']:
            try:
                links = await page.locator(sel).all()
                for link_el in links:
                    try:
                        href = await link_el.get_attribute("href")
                        if not href or href in seen_links:
                            continue
                        if "google.com/sorry" in href or "accounts.google" in href or "search" in href:
                            continue
                        title = (await link_el.inner_text()).strip()
                        if len(title) < 5 or len(title) > 300:
                            continue
                        seen_links.add(href)
                        items.append({
                            "title": title,
                            "link": href if href.startswith("http") else f"https://news.google.com{href}",
                            "source": "Unknown",
                            "time": "",
                            "snippet": "",
                        })
                    except Exception:
                        continue
                if items:
                    break
            except Exception:
                continue
        return items
