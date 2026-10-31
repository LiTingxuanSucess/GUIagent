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
from urllib.parse import quote, urljoin, urlparse, parse_qs

from cloakbrowser import launch_persistent_context_async

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


@dataclass
class TmallCandidate:
    platform: str
    keyword: str
    rank_position: int
    item_id: str
    title: str
    product_url: str
    price_raw: str
    final_price: float | None
    sales_text: str
    location: str
    shop_name: str
    shop_type: str
    captured_at: str


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_url(url: str, base: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    return urljoin(base, url)


def item_id(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    return (qs.get("id") or [""])[0]


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
    m = re.search(r"\d+(?:\.\d+)?万?\+?人(?:付款|收货|购买|看过)|本月行业热销", text)
    return norm(m.group(0)) if m else ""


def parse_shop(text: str) -> str:
    patterns = [
        r"[\w\u4e00-\u9fff-]{2,60}(?:官方旗舰店|旗舰店|专营店|专卖店|天猫国际自营全球超级店|天猫超市)",
        r"(?:天猫国际自营全球超级店|天猫超市)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return norm(m.group(0))
    return ""


def parse_location(text: str) -> str:
    known = ["北京", "上海", "天津", "重庆", "广东", "广州", "深圳", "浙江", "杭州", "江苏", "南京", "河北", "安徽", "山东", "福建", "四川"]
    for name in known:
        if name in text:
            return name
    return ""


def shop_type(shop: str) -> str:
    if "自营" in shop or "天猫超市" in shop:
        return "self_operated"
    if "旗舰店" in shop:
        return "flagship"
    if shop:
        return "tmall"
    return ""


def parse_title(text: str, keyword: str) -> str:
    lines = [norm(x) for x in text.splitlines() if norm(x)]
    for line in lines[:12]:
        if len(line) < 8 or line.startswith("¥") or line in {"券后价", "正在秒杀"}:
            continue
        if keyword.split()[0] in line or "杜蕾斯" in line:
            return line
    return norm(text)[:160]


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Low-frequency Tmall list probe with CloakBrowser.")
    parser.add_argument("--profile-dir", type=Path, default=Path(__file__).resolve().parents[1] / ".cloak-profiles" / "ec-durex")
    parser.add_argument("--keyword", default="\u675c\u857e\u65af 001")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--out-dir", type=Path, default=Path("exports") / f"cloak_tmall_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
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
        search_url = f"https://list.tmall.com/search_product.htm?q={quote(args.keyword)}"
        print(f">>> open {search_url}", flush=True)
        await page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(15_000)
        await page.mouse.wheel(0, 700)
        await page.wait_for_timeout(3_500)
        await page.mouse.wheel(0, -500)
        await page.wait_for_timeout(2_500)

        screenshot = args.out_dir / "tmall_search.png"
        await page.screenshot(path=str(screenshot), full_page=True)
        body_text = await page.locator("body").inner_text(timeout=10_000)
        (args.out_dir / "tmall_search_text.txt").write_text(body_text, encoding="utf-8")

        data = await page.evaluate(
            """
            () => {
              const anchors = Array.from(document.querySelectorAll('a[href]'));
              const out = [];
              const seen = new Set();
              for (const a of anchors) {
                const href = new URL(a.getAttribute('href'), location.href).href;
                if (!/(detail\\.tmall\\.com|item\\.taobao\\.com|chaoshi\\.detail\\.tmall\\.com)/.test(href)) continue;
                const id = (href.match(/[?&]id=(\\d+)/) || [])[1] || href;
                if (!id || seen.has(id)) continue;
                seen.add(id);
                let card = a;
                let node = a;
                for (let i = 0; i < 9 && node; i++) {
                  const text = (node.innerText || '').trim();
                  const linkCount = node.querySelectorAll ? node.querySelectorAll('a[href]').length : 0;
                  if (text.length > 40 && text.length < 1600 && linkCount <= 45) { card = node; break; }
                  node = node.parentElement;
                }
                out.push({
                  href,
                  text: (card.innerText || a.innerText || '').trim(),
                  anchorText: (a.innerText || '').trim()
                });
              }
              return out;
            }
            """
        )
        rows: list[TmallCandidate] = []
        for item in data:
            href = clean_url(str(item.get("href") or ""), page.url)
            text = str(item.get("text") or "")
            title = norm(str(item.get("anchorText") or "")) or parse_title(text, args.keyword)
            if len(title) < 8:
                title = parse_title(text, args.keyword)
            if "杜蕾斯" not in title and "durex" not in title.lower():
                continue
            shop = parse_shop(text)
            rec = TmallCandidate(
                platform="tmall",
                keyword=args.keyword,
                rank_position=len(rows) + 1,
                item_id=item_id(href),
                title=title[:220],
                product_url=href,
                price_raw=norm(text),
                final_price=parse_price(text),
                sales_text=parse_sales(text),
                location=parse_location(text),
                shop_name=shop,
                shop_type=shop_type(shop),
                captured_at=datetime.now().isoformat(timespec="seconds"),
            )
            rows.append(rec)
            if len(rows) >= args.limit:
                break

        payload = {"keyword": args.keyword, "search_url": page.url, "screenshot": str(screenshot), "rows": [asdict(x) for x in rows]}
        (args.out_dir / "tmall_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with (args.out_dir / "tmall_results.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()) if rows else list(TmallCandidate.__dataclass_fields__.keys()))
            writer.writeheader()
            for row in rows:
                writer.writerow(asdict(row))
        print(f">>> cards={len(rows)}", flush=True)
        print(f">>> written {args.out_dir / 'tmall_results.json'}", flush=True)
        return 0
    finally:
        await context.close()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
