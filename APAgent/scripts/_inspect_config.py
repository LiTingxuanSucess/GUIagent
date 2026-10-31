import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_apple_catalog_sku_prices import discover_dimensions

URLS = [
    "https://www.apple.com/shop/buy-mac/macbook-pro",
    "https://www.apple.com/shop/buy-watch/apple-watch",
    "https://www.apple.com/shop/buy-ipad/ipad-air",
    "https://www.apple.com/shop/buy-iphone/iphone-17-pro",
]


async def inspect(url: str) -> None:
    print("\n===", url, flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(locale="en-US")
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(5000)
        dims = await discover_dimensions(page)
        print("dimension groups", {k: len(v) for k, v in dims.items()}, flush=True)
        other = await page.evaluate(
            """
            () => {
              const names = new Set();
              for (const input of document.querySelectorAll('input[type="radio"]')) {
                if (input.name && !input.disabled) names.add(input.name);
              }
              return [...names].sort();
            }
            """
        )
        print("all radio names", other, flush=True)
        await browser.close()


async def main() -> None:
    for url in URLS:
        await inspect(url)


if __name__ == "__main__":
    asyncio.run(main())
