from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse, parse_qs

from playwright.async_api import BrowserContext, Page

from src.browser import attach_chrome, wait_for_cdp_or_start
from src.exporter import write_csv, write_json

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


@dataclass
class ProductRecord:
    platform: str
    brand: str
    series: str
    keyword: str
    rank_position: int
    title: str
    product_url: str
    shop_name: str = ""
    shop_type: str = ""
    official_store_flag: bool = False
    price_raw: str = ""
    final_price: float | None = None
    price_source: str = ""
    ocr_price: float | None = None
    sales_text: str = ""
    location: str = ""
    main_image_url: str = ""
    spec_text: str = ""
    selected_sku: str = ""
    sku_options: list[str] = field(default_factory=list)
    piece_count: int | None = None
    unit_price: float | None = None
    rank_source: str = ""
    series_match_reason: str = ""
    detail_title: str = ""
    screenshot_path: str = ""
    confidence: float = 0.0
    notes: str = ""
    captured_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


def parse_price(text: str) -> float | None:
    text = norm(text)
    candidates: list[float] = []
    for m in re.finditer(r"[¥￥]\s*([1-9]\d{0,5})(?:\s*\.\s*(\d{1,2}))?", text):
        raw = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
        try:
            val = float(raw)
        except ValueError:
            continue
        if 1 <= val <= 100000:
            candidates.append(val)
    if not candidates:
        for m in re.finditer(r"(?:到手价|券后价|秒杀价|促销价|价格|PLUS价)\D{0,8}([1-9]\d{0,5}(?:\.\d{1,2})?)", text):
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            if 1 <= val <= 100000:
                candidates.append(val)
    return min(candidates) if candidates else None


def parse_piece_count(text: str) -> int | None:
    text = norm(text)
    text = re.sub(r"\b00[13]\b", "", text)
    totals: list[int] = []
    for a, b in re.findall(r"(\d{1,3})\s*[xX*×]\s*(\d{1,3})\s*(?:只|片|枚|个)", text):
        total = int(a) * int(b)
        if 1 <= total <= 2000:
            totals.append(total)
    for m in re.finditer(r"(?:共|合计)?\s*(\d{1,4})\s*(?:只|片|枚|个)", text):
        total = int(m.group(1))
        if 1 <= total <= 2000:
            totals.append(total)
    return max(totals) if totals else None


def unit_price(price: float | None, pieces: int | None) -> float | None:
    if price is None or not pieces:
        return None
    return round(price / pieces, 4)


def product_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if qs.get("id"):
        return qs["id"][0]
    m = re.search(r"/(\d+)\.html", parsed.path)
    if m:
        return m.group(1)
    return url


def shop_type(shop: str, title: str) -> str:
    hay = f"{shop} {title}"
    if any(x in hay for x in ["京东自营", "京东超市", "京东大药房自营", "天猫国际自营"]):
        return "self_operated"
    if any(x in hay for x in ["官方旗舰店", "旗舰店"]):
        return "flagship"
    if "天猫" in hay:
        return "tmall"
    return ""


def confidence(title: str, shop: str, brand: str, series: str) -> float:
    hay = f"{title} {shop}".lower()
    score = 0.0
    if brand and brand.lower() in hay:
        score += 0.35
    if series and series.lower() in hay:
        score += 0.3
    if any(x in hay for x in ["官方旗舰店", "旗舰店", "自营"]):
        score += 0.25
    if any(x in hay for x in ["避孕套", "安全套", "计生"]):
        score += 0.1
    return min(score, 1.0)


def match_reason(title: str, shop: str, brand: str, series: str) -> str:
    hay = f"{title} {shop}".lower()
    reasons: list[str] = []
    if brand and brand.lower() in hay:
        reasons.append("brand_in_text")
    if series and series.lower() in hay:
        reasons.append("series_in_text")
    if any(x in hay for x in ["官方旗舰店", "旗舰店", "自营"]):
        reasons.append("official_store_text")
    if any(x in hay for x in ["避孕套", "安全套", "计生"]):
        reasons.append("category_text")
    return ",".join(reasons)


async def find_or_open(context: BrowserContext, markers: list[str], url: str) -> Page:
    for page in context.pages:
        if any(marker in page.url for marker in markers):
            await page.bring_to_front()
            return page
    page = await context.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    return page


async def click_sales_sort(page: Page) -> str:
    before = page.url
    for label in ["销量", "销售", "成交"]:
        try:
            loc = page.get_by_text(label, exact=False).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=3000)
                await page.wait_for_timeout(2500)
                return "sales_sort_url_changed" if page.url != before else "sales_sort_clicked_unconfirmed"
        except Exception:
            continue
    return "sales_sort_not_found"


async def search_platform(page: Page, platform: str, keyword: str) -> None:
    encoded = quote(keyword)
    if platform == "jd":
        await page.goto(f"https://search.jd.com/Search?keyword={encoded}&enc=utf-8", wait_until="domcontentloaded", timeout=30_000)
    elif platform == "tmall":
        await page.goto(f"https://list.tmall.com/search_product.htm?q={encoded}", wait_until="domcontentloaded", timeout=30_000)
    else:
        if await has_product_links(page, "taobao") and "s.taobao.com/search" in page.url and "tab=mall" not in page.url:
            return
        await page.goto("https://www.taobao.com", wait_until="domcontentloaded", timeout=30_000)
        selectors = ['input[name="q"]', "#q", ".search-combobox-input", 'input[placeholder*="搜索"]']
        for selector in selectors:
            try:
                box = page.locator(selector).first
                await box.wait_for(state="visible", timeout=4000)
                await box.click()
                await box.evaluate(
                    """
                    (el, kw) => {
                      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                      if (setter) setter.call(el, kw); else el.value = kw;
                      el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: kw }));
                      el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    """,
                    keyword,
                )
                await page.keyboard.press("Enter")
                break
            except Exception:
                continue
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    await page.wait_for_timeout(4500)
    for _ in range(3):
        await page.mouse.wheel(0, 900)
        await page.wait_for_timeout(700)
    await page.mouse.wheel(0, -2400)
    await page.wait_for_timeout(1000)


async def has_product_links(page: Page, platform: str) -> bool:
    try:
        if platform == "jd":
            count = await page.evaluate("() => document.querySelectorAll('[data-sku]').length")
            body_len = await page.evaluate("() => (document.body.innerText || '').length")
            return int(count or 0) > 0 and int(body_len or 0) > 1000
        else:
            count = await page.evaluate(
                """() => document.querySelectorAll('a[href*="item.taobao.com"],a[href*="detail.tmall.com"]').length"""
            )
            body_len = await page.evaluate("() => (document.body.innerText || '').length")
            return int(count or 0) > 20 and int(body_len or 0) > 1000
    except Exception:
        return False


async def find_ready_search_page(context: BrowserContext, platform: str, keyword: str) -> Page | None:
    for page in context.pages:
        if platform == "taobao":
            if "s.taobao.com/search" not in page.url or "tab=mall" in page.url:
                continue
        elif platform == "tmall":
            if not (("list.tmall.com" in page.url) or ("s.taobao.com/search" in page.url and "tab=mall" in page.url)):
                continue
        elif platform == "jd":
            if "search.jd.com/Search" not in page.url:
                continue
        else:
            continue
        if keyword.split()[0] in (await page.title()) and await has_product_links(page, platform):
            await page.bring_to_front()
            return page
    return None


async def extract_candidates(page: Page, platform: str, brand: str, series: str, keyword: str, limit: int) -> list[ProductRecord]:
    if platform == "jd":
        js = """
        () => {
          const cards = Array.from(document.querySelectorAll('[data-sku], .gl-item, li[class*="gl-item"]'));
          const out = [];
          const seen = new Set();
          for (const card of cards) {
            const sku = card.getAttribute('data-sku') || '';
            const link = card.querySelector('a[href*="item.jd.com"], a[href*="//item.jd.com"], a[href*="/item/"]');
            let href = link ? new URL(link.getAttribute('href'), location.href).href : '';
            if (!href && sku) href = `https://item.jd.com/${sku}.html`;
            const key = sku || (href.match(/\\/(\\d+)\\.html/) || [])[1] || href;
            if (!href || !key || seen.has(key)) continue;
            seen.add(key);
            const titled = card.querySelector('[title]');
            const title = (titled?.getAttribute('title') || card.querySelector('.p-name em, .p-name a, [class*="p-name"], [class*="goods_title"]')?.innerText || link?.innerText || card.innerText || '').replace(/\\s+/g, ' ').trim();
            const price = (card.querySelector('.p-price, [class*="price"], [class*="Price"]')?.innerText || card.innerText || '').replace(/\\s+/g, ' ').trim();
            const text = (card.innerText || '').replace(/\\s+/g, ' ').trim();
            const shopMatch = text.match(/[\\w\\u4e00-\\u9fff-]{2,50}(?:京东自营旗舰店|官方旗舰店|旗舰店|专营店|专卖店|京东自营|京东超市)/);
            const shop = shopMatch ? shopMatch[0] : '';
            const commentsMatch = text.match(/已售\\s*\\d+(?:\\.\\d+)?万?\\+?|\\d+(?:\\.\\d+)?万?\\+?好评|\\d+(?:\\.\\d+)?万?\\+?评价/);
            const comments = commentsMatch ? commentsMatch[0] : '';
            const img = card.querySelector('img');
            const image = img ? (img.currentSrc || img.src || img.getAttribute('data-lazy-img') || '') : '';
            out.push({href, title, price, shop, comments, image, text});
          }
          return out;
        }
        """
    else:
        js = """
        () => {
          const anchors = Array.from(document.querySelectorAll('a[href]'));
          const out = [];
          const seen = new Set();
          for (const a of anchors) {
            const href = new URL(a.getAttribute('href'), location.href).href;
            if (!/(item\\.taobao\\.com|detail\\.tmall\\.com|world\\.taobao\\.com)/.test(href) || /login|shop/.test(href)) continue;
            const id = (href.match(/[?&]id=(\\d+)/) || [])[1] || href;
            if (seen.has(id)) continue;
            seen.add(id);
            let card = a;
            let node = a;
            for (let i = 0; i < 8 && node; i++) {
              const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
              const linkCount = node.querySelectorAll ? node.querySelectorAll('a[href]').length : 0;
              if (text.length > 30 && text.length < 1600 && linkCount <= 40) { card = node; break; }
              node = node.parentElement;
            }
            const text = (card.innerText || a.innerText || '').replace(/\\s+/g, ' ').trim();
            const img = card.querySelector ? card.querySelector('img') : null;
            const image = img ? (img.currentSrc || img.src || '') : '';
            out.push({href, title: (a.innerText || '').replace(/\\s+/g, ' ').trim(), price: '', shop: '', comments: '', image, text});
          }
          return out;
        }
        """
    raw_items = await page.evaluate(js)
    records: list[ProductRecord] = []
    for item in raw_items:
        text = norm(item.get("text"))
        title = norm(item.get("title"))
        if len(title) < 8:
            title = derive_title(text, brand, series)
        url = clean_url(str(item.get("href") or ""))
        if not title or not url:
            continue
        price_raw = norm(item.get("price")) or derive_price_raw(text)
        shop = norm(item.get("shop")) or derive_shop(text)
        sales = norm(item.get("comments")) or derive_sales(text)
        image = clean_url(str(item.get("image") or ""))
        price = parse_price(f"{price_raw} {text}")
        pieces = parse_piece_count(title)
        rec = ProductRecord(
            platform=platform,
            brand=brand,
            series=series,
            keyword=keyword,
            rank_position=len(records) + 1,
            title=title[:220],
            product_url=urljoin(page.url, url),
            shop_name=shop[:100],
            shop_type=shop_type(shop, title),
            official_store_flag=shop_type(shop, title) in {"self_operated", "flagship"},
            price_raw=price_raw[:120],
            final_price=price,
            price_source="search_dom" if price is not None else "",
            sales_text=sales[:80],
            main_image_url=image,
            piece_count=pieces,
            unit_price=unit_price(price, pieces),
            series_match_reason=match_reason(title, shop, brand, series),
            confidence=confidence(title, shop, brand, series),
        )
        records.append(rec)
        if len(records) >= limit:
            break
    return records


def derive_title(text: str, brand: str, series: str) -> str:
    candidates: list[str] = []
    for part in re.split(r"[\n|]", text):
        line = norm(part)
        if len(line) < 8:
            continue
        if brand and brand in line:
            candidates.append(line)
        elif series and series in line:
            candidates.append(line)
    if candidates:
        return max(candidates, key=len)
    return text[:180]


def derive_price_raw(text: str) -> str:
    m = re.search(r"[¥￥]\s*[1-9]\d{0,5}(?:\s*\.\s*\d{1,2})?", text)
    return norm(m.group(0)) if m else ""


def derive_sales(text: str) -> str:
    patterns = [
        r"\d+(?:\.\d+)?万?\+?人(?:付款|收货)",
        r"\d+(?:\.\d+)?万?\+?条?评价",
        r"\d+(?:\.\d+)?万?\+?评论",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(0)
    return ""


def derive_shop(text: str) -> str:
    for pattern in [
        r"[\w\u4e00-\u9fff-]{2,40}(?:官方旗舰店|旗舰店|专营店|专卖店|京东自营|京东超市)",
        r"(?:京东自营|天猫国际自营)",
    ]:
        m = re.search(pattern, text)
        if m:
            return m.group(0)
    return ""


async def enrich_detail(context: BrowserContext, record: ProductRecord, out_dir: Path, wait_sec: float = 4.0) -> ProductRecord:
    page = await context.new_page()
    try:
        await page.goto(record.product_url, wait_until="domcontentloaded", timeout=35_000)
        await page.wait_for_timeout(int(wait_sec * 1000))
        for _ in range(2):
            await page.mouse.wheel(0, 900)
            await page.wait_for_timeout(500)
        record.detail_title = norm(await page.title())
        shots_dir = out_dir / "screenshots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        shot = shots_dir / f"{record.platform}_{record.rank_position:02d}_{product_id_from_url(record.product_url)[:24]}.png"
        await page.screenshot(path=str(shot), full_page=True)
        record.screenshot_path = str(shot)
        detail = await page.evaluate(
            """
            () => {
              const lines = (document.body.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
              const images = Array.from(document.images).map(img => ({
                src: img.currentSrc || img.src || img.getAttribute('data-lazy-img') || '',
                w: img.naturalWidth || 0,
                h: img.naturalHeight || 0
              })).filter(x => x.src);
              return {lines, images};
            }
            """
        )
        lines = [norm(x) for x in detail.get("lines") or [] if norm(x)]
        body = "\n".join(lines[:500])
        if not record.shop_name:
            record.shop_name = derive_shop(body)
        if record.shop_name:
            record.shop_type = shop_type(record.shop_name, record.title)
            record.official_store_flag = record.shop_type in {"self_operated", "flagship"}
        detail_price = parse_price(body[:8000])
        if detail_price is not None:
            record.final_price = detail_price
            record.price_source = "detail_dom"
        if not record.sales_text:
            record.sales_text = derive_sales(body)
        if not record.piece_count:
            record.piece_count = parse_piece_count(f"{record.title} {body[:4000]}")
        record.unit_price = unit_price(record.final_price, record.piece_count)
        if not record.main_image_url:
            record.main_image_url = best_image(detail.get("images") or [])
        record.spec_text = extract_spec(lines)
        record.selected_sku = extract_selected_sku(lines, record.series)
        record.series_match_reason = match_reason(record.title, record.shop_name, record.brand, record.series)
        record.confidence = confidence(record.title, record.shop_name, record.brand, record.series)
        if record.final_price is None:
            record.notes = norm(f"{record.notes} missing_price")
        if record.piece_count is None:
            record.notes = norm(f"{record.notes} missing_piece_count")
        return record
    except Exception as exc:
        record.notes = norm(f"{record.notes} detail_error={type(exc).__name__}:{exc}")
        return record
    finally:
        await page.close()


def best_image(images: list[dict[str, Any]]) -> str:
    scored: list[tuple[int, str]] = []
    for image in images:
        src = clean_url(str(image.get("src") or ""))
        if not src:
            continue
        try:
            w = int(image.get("w") or 0)
            h = int(image.get("h") or 0)
        except Exception:
            w = h = 0
        if w >= 300 and h >= 300:
            scored.append((-(w * h), src))
    return min(scored)[1] if scored else ""


def extract_spec(lines: list[str]) -> str:
    keys = ["品牌", "商品名称", "产品名称", "型号", "材质", "规格", "颜色分类", "包装清单"]
    picked: list[str] = []
    for i, line in enumerate(lines[:240]):
        if any(key in line for key in keys) and len(line) <= 120:
            picked.append(line)
            if i + 1 < len(lines) and len(lines[i + 1]) <= 120:
                picked.append(lines[i + 1])
        if len(picked) >= 12:
            break
    return "；".join(dict.fromkeys(picked))[:1000]


def extract_selected_sku(lines: list[str], series: str) -> str:
    for line in lines[:260]:
        if len(line) <= 120 and (series and series in line) and re.search(r"\d+\s*(?:只|片|枚|个)", line):
            return line
    return ""


async def run_platform(
    context: BrowserContext,
    *,
    platform: str,
    keyword: str,
    brand: str,
    series: str,
    max_candidates: int,
    detail_limit: int,
    out_dir: Path,
) -> dict[str, Any]:
    start_urls = {
        "taobao": ("https://www.taobao.com", ["taobao.com", "tmall.com"]),
        "tmall": ("https://list.tmall.com/search_product.htm", ["tmall.com"]),
        "jd": ("https://www.jd.com", ["jd.com"]),
    }
    start_url, markers = start_urls[platform]
    page = await find_ready_search_page(context, platform, keyword)
    if page is None:
        page = await find_or_open(context, markers, start_url)
        await page.bring_to_front()
        await search_platform(page, platform, keyword)
    if platform == "taobao":
        rank_source = "default_search_order"
    else:
        rank_source = await click_sales_sort(page)
    search_shot = out_dir / f"{platform}_search_results.png"
    await page.screenshot(path=str(search_shot), full_page=True)
    records = await extract_candidates(page, platform, brand, series, keyword, max_candidates)
    for rec in records:
        rec.rank_source = rank_source
    for rec in records[:detail_limit]:
        print(f">>> {platform} detail #{rec.rank_position}: {rec.title[:80]}", flush=True)
        await enrich_detail(context, rec, out_dir)
    rows = [r.to_dict() for r in records]
    write_json(out_dir / f"{platform}_results.json", {"target": {"platform": platform, "brand": brand, "series": series, "keyword": keyword}, "search_url": page.url, "rows": rows})
    write_csv(out_dir / f"{platform}_results.csv", rows)
    print(f">>> {platform} candidates={len(records)} search_url={page.url}", flush=True)
    return {"platform": platform, "search_url": page.url, "rows": rows, "screenshot": str(search_shot)}


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Collect EC products from Taobao/Tmall/JD over Chrome CDP")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--platforms", default="taobao,tmall,jd")
    parser.add_argument("--keyword", default="\u675c\u857e\u65af 001")
    parser.add_argument("--brand", default="\u675c\u857e\u65af")
    parser.add_argument("--series", default="001")
    parser.add_argument("--max-candidates", type=int, default=12)
    parser.add_argument("--detail-limit", type=int, default=5)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--no-start", action="store_true")
    args = parser.parse_args()

    platforms = [x.strip().lower() for x in args.platforms.split(",") if x.strip()]
    unknown = [x for x in platforms if x not in {"taobao", "tmall", "jd"}]
    if unknown:
        raise ValueError(f"unsupported platforms: {unknown}")
    await wait_for_cdp_or_start(args.cdp, start_if_missing=not args.no_start, url="https://www.taobao.com")
    out_dir = args.out_dir or Path("exports") / f"ec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    pw, browser, context = await attach_chrome(args.cdp)
    try:
        results = []
        for platform in platforms:
            print(f">>> running {platform}: {args.keyword}", flush=True)
            platform_out = out_dir / platform
            platform_out.mkdir(parents=True, exist_ok=True)
            results.append(
                await run_platform(
                    context,
                    platform=platform,
                    keyword=args.keyword,
                    brand=args.brand,
                    series=args.series,
                    max_candidates=args.max_candidates,
                    detail_limit=args.detail_limit,
                    out_dir=platform_out,
                )
            )
        write_json(out_dir / "summary.json", {"keyword": args.keyword, "brand": args.brand, "series": args.series, "platforms": results})
        print(f">>> written summary: {out_dir / 'summary.json'}", flush=True)
        return 0
    finally:
        await pw.stop()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
