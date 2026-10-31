from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import requests
from playwright.async_api import Browser, BrowserContext, Page, async_playwright


DEFAULT_CDP_URL = "http://127.0.0.1:9222"


def cdp_is_ready(cdp_url: str = DEFAULT_CDP_URL, timeout: float = 2.0) -> bool:
    try:
        resp = requests.get(f"{cdp_url.rstrip('/')}/json/version", timeout=timeout)
        return resp.ok and "webSocketDebuggerUrl" in resp.text
    except requests.RequestException:
        return False


def find_chrome_exe() -> Path:
    candidates = [
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("chrome.exe not found")


def start_chrome_with_cdp(
    cdp_port: int = 9222,
    url: str = "https://www.taobao.com",
    user_data_dir: str | None = None,
) -> None:
    chrome = find_chrome_exe()
    if user_data_dir is None:
        import os

        user_data_dir = str(Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data")
    args = [
        str(chrome),
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    subprocess.Popen(args)


async def attach_chrome(cdp_url: str = DEFAULT_CDP_URL) -> tuple[object, Browser, BrowserContext]:
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    return pw, browser, context


async def get_or_open_page(context: BrowserContext, url_keyword: str, fallback_url: str) -> Page:
    for page in context.pages:
        if url_keyword in page.url:
            await page.bring_to_front()
            return page
    page = await context.new_page()
    await page.goto(fallback_url, wait_until="domcontentloaded", timeout=30_000)
    return page


async def wait_for_cdp_or_start(
    cdp_url: str = DEFAULT_CDP_URL,
    start_if_missing: bool = True,
    url: str = "https://www.taobao.com",
    timeout_sec: float = 10.0,
) -> None:
    if cdp_is_ready(cdp_url):
        return
    if not start_if_missing:
        raise RuntimeError(f"Chrome CDP is not available at {cdp_url}")
    port = int(cdp_url.rstrip("/").rsplit(":", 1)[-1])
    start_chrome_with_cdp(cdp_port=port, url=url)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if cdp_is_ready(cdp_url):
            return
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Chrome CDP did not become ready at {cdp_url}")
