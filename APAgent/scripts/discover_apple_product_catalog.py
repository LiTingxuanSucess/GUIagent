from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = BASE_DIR.parents[0]
DOCS_DIR = WORKSPACE_DIR / "docs"

SEED_URLS = [
    "https://www.apple.com/store",
    "https://www.apple.com/shop/buy-mac",
    "https://www.apple.com/shop/buy-iphone",
    "https://www.apple.com/shop/buy-ipad",
    "https://www.apple.com/shop/buy-watch",
    "https://www.apple.com/shop/buy-vision",
    "https://www.apple.com/shop/accessories/all",
    "https://www.apple.com/shop/accessories/all/made-by-apple",
    "https://www.apple.com/shop/accessories/all/cases-protection",
    "https://www.apple.com/shop/accessories/all/chargers-cables",
    "https://www.apple.com/shop/accessories/all/headphones-speakers",
    "https://www.apple.com/shop/accessories/all/mice-keyboards",
    "https://www.apple.com/shop/accessories/all/watch-bands",
]

CONFIGURABLE_PATTERNS = [
    r"^/shop/buy-mac(?:/[^/?]+(?:/[^/?]+)?)?$",
    r"^/shop/buy-iphone(?:/[^/?]+)?$",
    r"^/shop/buy-ipad(?:/[^/?]+)?$",
    r"^/shop/buy-watch(?:/[^/?]+(?:/[^/?]+)?)?$",
    r"^/shop/buy-airpods(?:/[^/?]+)?$",
    r"^/shop/buy-homepod(?:/[^/?]+)?$",
    r"^/shop/buy-vision(?:/[^/?]+)?$",
]

IGNORE_PATH_PATTERNS = [
    r"/carrier-offers$",
    r"/shop/buy-(mac|iphone|ipad|watch)$",
]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def part_number_from_path(path: str) -> str:
    match = re.search(r"/shop/product/([^/]+)/a", path, re.I)
    return match.group(1).upper() if match else ""


def classify(path: str) -> str:
    if path.startswith("/shop/product/"):
        return "fixed_product"
    if any(re.search(pattern, path, re.I) for pattern in CONFIGURABLE_PATTERNS):
        return "configurable"
    return "other"


def should_keep(path: str) -> bool:
    if path.startswith("/shop/accessories/all/"):
        return True
    if any(re.search(pattern, path, re.I) for pattern in IGNORE_PATH_PATTERNS):
        return False
    return path.startswith("/shop/product/") or path.startswith("/shop/buy-")


def infer_family(path: str, title: str) -> str:
    lowered = f"{path} {title}".lower()
    if "/buy-mac" in lowered or "mac" in lowered:
        return "Mac"
    if "/buy-iphone" in lowered or "iphone" in lowered:
        return "iPhone"
    if "/buy-ipad" in lowered or "ipad" in lowered:
        return "iPad"
    if "/buy-watch" in lowered or "watch" in lowered:
        return "Apple Watch"
    if "airpods" in lowered or "beats" in lowered or "earpods" in lowered:
        return "Audio"
    if "homepod" in lowered:
        return "HomePod"
    if "vision" in lowered:
        return "Vision"
    if "keyboard" in lowered or "mouse" in lowered or "trackpad" in lowered:
        return "Input"
    if "case" in lowered or "folio" in lowered or "band" in lowered or "strap" in lowered:
        return "Accessories"
    return "Other"


def infer_strategy(kind: str, path: str) -> str:
    if kind == "fixed_product":
        return "fixed_product_page"
    if "/buy-iphone/" in path:
        return "url_path_or_page_click"
    return "page_click"


async def collect_links(page: Any, url: str) -> list[dict[str, str]]:
    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(4_000)
    for _ in range(8):
        await page.mouse.wheel(0, 5000)
        await page.wait_for_timeout(600)
        await click_more_buttons(page)
    links = await page.locator("a[href]").evaluate_all(
        """els => els.map(a => ({
            text: (a.innerText || a.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim(),
            href: a.href
        }))"""
    )
    items: list[dict[str, str]] = []
    for link in links:
        href = normalize_url(str(link.get("href") or ""))
        if not href.startswith("https://www.apple.com"):
            continue
        path = urlparse(href).path
        if should_keep(path):
            items.append({"source": url, "name": str(link.get("text") or "").strip(), "url": href, "path": path})
    return items


async def click_more_buttons(page: Any) -> None:
    for pattern in (r"Show more", r"Load more", r"View more"):
        locs = page.get_by_role("button", name=re.compile(pattern, re.I))
        for idx in range(await locs.count()):
            loc = locs.nth(idx)
            try:
                if await loc.is_visible(timeout=250) and await loc.is_enabled(timeout=250):
                    await loc.click(timeout=2_000)
                    await page.wait_for_timeout(800)
            except Exception:
                continue


async def discover(seed_urls: list[str]) -> list[dict[str, Any]]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()
        discovered: dict[str, dict[str, Any]] = {}
        queue = list(dict.fromkeys(seed_urls))
        visited: set[str] = set()

        while queue:
            source = queue.pop(0)
            if source in visited:
                continue
            visited.add(source)
            try:
                links = await collect_links(page, source)
            except Exception as exc:
                print(json.dumps({"source": source, "error": str(exc)}, ensure_ascii=False))
                continue
            for link in links:
                path = link["path"]
                url = link["url"]
                if path.startswith("/shop/accessories/all") and url not in visited:
                    queue.append(url)
                    continue
                kind = classify(path)
                if kind == "other":
                    continue
                current = discovered.get(url)
                if not current or (not current.get("name") and link.get("name")):
                    discovered[url] = {
                        "name": link.get("name") or part_number_from_path(path) or path.rsplit("/", 1)[-1],
                        "url": url,
                        "path": path,
                        "kind": kind,
                        "family": infer_family(path, link.get("name") or ""),
                        "part_number": part_number_from_path(path),
                        "selection_strategy": infer_strategy(kind, path),
                        "source": source,
                        "discovered_at": now(),
                    }
        await browser.close()

    return sorted(discovered.values(), key=lambda item: (item["kind"], item["family"], item["name"], item["url"]))


def write_outputs(items: list[dict[str, Any]], prefix: str) -> dict[str, str]:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = DOCS_DIR / f"{prefix}.csv"
    json_path = DOCS_DIR / f"{prefix}.json"
    md_path = DOCS_DIR / f"{prefix}.md"

    fields = [
        "name",
        "family",
        "kind",
        "part_number",
        "selection_strategy",
        "url",
        "path",
        "source",
        "discovered_at",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in items:
            writer.writerow({field: item.get(field, "") for field in fields})

    json_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    configurable = [item for item in items if item["kind"] == "configurable"]
    fixed = [item for item in items if item["kind"] == "fixed_product"]
    lines = [
        "# Apple Product Catalog Discovery",
        "",
        f"Generated at: `{now()}`",
        "",
        f"- Configurable product entry points: `{len(configurable)}`",
        f"- Fixed product pages: `{len(fixed)}`",
        f"- Total rows: `{len(items)}`",
        "",
        "## Configurable Product Entry Points",
        "",
        "| Family | Name | Strategy | URL |",
        "|---|---|---|---|",
    ]
    for item in configurable:
        lines.append(f"| {item['family']} | {item['name']} | {item['selection_strategy']} | {item['url']} |")
    lines.extend(
        [
            "",
            "## Fixed Product Pages",
            "",
            "| Family | Name | Part Number | URL |",
            "|---|---|---|---|",
        ]
    )
    for item in fixed:
        lines.append(f"| {item['family']} | {item['name']} | {item['part_number']} | {item['url']} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {"csv": str(csv_path), "json": str(json_path), "md": str(md_path)}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default="APPLE_PRODUCT_CATALOG")
    parser.add_argument("--seed", action="append", default=[])
    args = parser.parse_args()
    seed_urls = SEED_URLS + args.seed
    items = await discover(seed_urls)
    outputs = write_outputs(items, args.prefix)
    print(json.dumps({"total": len(items), **outputs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
