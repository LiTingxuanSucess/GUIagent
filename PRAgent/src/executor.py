"""
Action executor for Playwright browser automation.
"""
from __future__ import annotations

from typing import Any

from playwright.async_api import Page


class Executor:
    """Execute actions (click, type, scroll, next_page) on a Playwright page."""

    @staticmethod
    async def execute(action: dict[str, Any], page: Page) -> bool:
        """
        Execute action on page. Returns True on success, False on failure.
        """
        atype = (action.get("type") or "").strip().lower()
        if not atype:
            return False

        try:
            if atype == "click":
                return await Executor._do_click(action, page)
            if atype == "type":
                return await Executor._do_type(action, page)
            if atype == "scroll":
                return await Executor._do_scroll(action, page)
            if atype == "next_page":
                return await Executor._do_next_page(action, page)
            if atype == "done":
                return True
        except Exception:
            return False
        return False

    @staticmethod
    async def _do_click(action: dict[str, Any], page: Page) -> bool:
        selector = action.get("selector")
        coordinates = action.get("coordinates")
        target = action.get("target", "")

        if selector:
            try:
                await page.click(selector, timeout=5000)
                return True
            except Exception:
                pass

        if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
            x, y = int(coordinates[0]), int(coordinates[1])
            await page.mouse.click(x, y)
            return True

        if target:
            safe_target = target.replace('"', "'")[:80]
            for sel in [
                f'a:has-text("{safe_target}")',
                f'button:has-text("{safe_target}")',
                f'[aria-label*="{safe_target}"]',
            ]:
                try:
                    await page.click(sel, timeout=3000)
                    return True
                except Exception:
                    continue
        return False

    @staticmethod
    async def _do_type(action: dict[str, Any], page: Page) -> bool:
        text = action.get("text", "")
        selector = action.get("selector")
        target = action.get("target", "")

        if not text:
            return False

        if selector:
            try:
                await page.fill(selector, text)
                return True
            except Exception:
                pass

        for sel in [
            'input[type="search"]',
            'input[type="text"]',
            'input[placeholder*="Search"]',
            'input[name="q"]',
            '[role="searchbox"]',
            'textarea',
        ]:
            try:
                await page.fill(sel, text)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    async def _do_scroll(action: dict[str, Any], page: Page) -> bool:
        try:
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
            return True
        except Exception:
            return False

    @staticmethod
    async def _do_next_page(action: dict[str, Any], page: Page) -> bool:
        """Click next page / more results. Google News uses 'More stories' or similar."""
        for sel in [
            'button:has-text("More stories")',
            'a:has-text("More stories")',
            'button:has-text("Next")',
            'a:has-text("Next")',
            'button:has-text("下一页")',
            'a:has-text("下一页")',
            '[aria-label*="More"]',
            '[aria-label*="Next"]',
            '[aria-label*="next"]',
            'button[aria-label*="next"]',
            'a[aria-label*="next"]',
            'a[role="button"]:has-text("More")',
        ]:
            try:
                await page.click(sel, timeout=3000)
                return True
            except Exception:
                continue
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)
            for sel in ['button:has-text("More")', 'a:has-text("More")']:
                try:
                    await page.click(sel, timeout=2000)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False
