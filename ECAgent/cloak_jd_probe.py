from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from cloakbrowser import launch_persistent_context_async

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


@dataclass
class JdCandidate:
    platform: str
    keyword: str
    rank_position: int
    sku_id: str
    title: str
    product_url: str
    price_raw: str
    final_price: float | None
    sales_text: str
    shop_name: str
    shop_type: str
    captured_at: str


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_price(text: str) -> float | None:
    candidates: list[float] = []
    for m in re.finditer(r"[¥￥]\s*([1-9]\d{0,5})(?:\s*\.\s*(\d{1,2}))?", text):
        raw = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
        try:
            val = float(raw)
        except ValueError:
            continue
        if 1 <= val <= 100000:
            candidates.append(val)
    return min(candidates) if candidates else None


def parse_sales(text: str) -> str:
    m = re.search(r"已售\s*\d+(?:\.\d+)?万?\+?|\d+(?:\.\d+)?万?\+?好评|\d+(?:\.\d+)?万?\+?评价", text)
    return norm(m.group(0)) if m else ""


def parse_shop(text: str) -> str:
    m = re.search(r"[\w\u4e00-\u9fff-]{2,60}(?:京东自营旗舰店|官方旗舰店|旗舰店|专营店|专卖店|京东自营|京东超市)", text)
    return norm(m.group(0)) if m else ""


def shop_type(shop: str) -> str:
    if any(x in shop for x in ["京东自营", "京东超市"]):
        return "self_operated"
    if "旗舰店" in shop:
        return "flagship"
    return ""


def parse_title(text: str) -> str:
    lines = [norm(x) for x in text.splitlines() if norm(x)]
    skip = {"标准", "到手价", "京东秒杀", "包邮"}
    for line in lines[:8]:
        if line in skip or line.startswith("|") or line.startswith("¥"):
            continue
        if len(line) >= 6:
            return line
    return norm(text)[:120]


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Low-frequency JD list probe with CloakBrowser.")
    parser.add_argument("--profile-dir", type=Path, default=Path(__file__).resolve().parents[1] / ".cloak-profiles" / "ec-durex")
    parser.add_argument("--keyword", default="\u675c\u857e\u65af 001")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--out-dir", type=Path, default=Path("exports") / f"cloak_jd_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    context = await launch_persistent_context_async(
        args.profile_dir,
        headless=False,
        humanize=True,
        human_preset="careful",
        viewport=None,
        locale="zh-CN",
        timezone="Asia/Shanghai",
        args=["--window-size=1440,1000", "--no-first-run", "--no-default-browser-check"],
    )
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        search_url = f"https://search.jd.com/Search?keyword={quote(args.keyword)}&enc=utf-8"
        print(f">>> open {search_url}", flush=True)
        await page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(12_000)
        await page.mouse.wheel(0, 700)
        await page.wait_for_timeout(3_000)
        await page.mouse.wheel(0, -500)
        await page.wait_for_timeout(2_000)

        screenshot = args.out_dir / "jd_search.png"
        await page.screenshot(path=str(screenshot), full_page=True)
        body_text = await page.locator("body").inner_text(timeout=10_000)
        (args.out_dir / "jd_search_text.txt").write_text(body_text, encoding="utf-8")

        cards = page.locator("[data-sku]")
        count = min(await cards.count(), args.limit)
        rows: list[JdCandidate] = []
        for index in range(count):
            card = cards.nth(index)
            sku = str(await card.get_attribute("data-sku") or "")
            text = await card.inner_text(timeout=5_000)
            title_attr = ""
            title_nodes = card.locator("[title]")
            if await title_nodes.count():
                title_attr = str(await title_nodes.first.get_attribute("title") or "")
            title = norm(title_attr) or parse_title(text)
            shop = parse_shop(text)
            rec = JdCandidate(
                platform="jd",
                keyword=args.keyword,
                rank_position=index + 1,
                sku_id=sku,
                title=title,
                product_url=f"https://item.jd.com/{sku}.html" if sku else "",
                price_raw=norm(text),
                final_price=parse_price(text),
                sales_text=parse_sales(text),
                shop_name=shop,
                shop_type=shop_type(shop),
                captured_at=datetime.now().isoformat(timespec="seconds"),
            )
            rows.append(rec)

        data = {"keyword": args.keyword, "search_url": page.url, "screenshot": str(screenshot), "rows": [asdict(x) for x in rows]}
        (args.out_dir / "jd_results.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with (args.out_dir / "jd_results.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()) if rows else list(JdCandidate.__dataclass_fields__.keys()))
            writer.writeheader()
            for row in rows:
                writer.writerow(asdict(row))
        print(f">>> cards={len(rows)}", flush=True)
        print(f">>> written {args.out_dir / 'jd_results.json'}", flush=True)
        return 0
    finally:
        await context.close()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
