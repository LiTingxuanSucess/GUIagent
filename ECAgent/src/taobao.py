from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, Page

from .ocr_client import ocr_image_file
from .price import normalize_price, parse_piece_count, unit_price


TAOBAO_HOME = "https://www.taobao.com"


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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _shop_type(shop_name: str, title: str) -> str:
    hay = f"{shop_name} {title}"
    if "天猫国际自营" in hay or "自营" in hay:
        return "self_operated"
    if "官方旗舰店" in hay or "旗舰店" in hay:
        return "flagship"
    if "天猫" in hay:
        return "tmall"
    return ""


def _official_store_flag(shop_name: str, title: str, brand: str = "") -> bool:
    shop_type = _shop_type(shop_name, title)
    if shop_type == "self_operated":
        return True
    if shop_type != "flagship":
        return False
    if not brand:
        return "官方" in shop_name
    return brand.lower() in shop_name.lower() and ("官方" in shop_name or "旗舰店" in shop_name)


def _series_match_reason(title: str, brand: str, series: str) -> str:
    reasons: list[str] = []
    hay = title.lower()
    if brand and brand.lower() in hay:
        reasons.append("brand_in_title")
    if series and series.lower() in hay:
        reasons.append("series_in_title")
    if any(x in title for x in ["官方旗舰店", "旗舰店", "自营"]):
        reasons.append("official_store_text")
    if any(x in title for x in ["避孕套", "安全套", "计生用品"]):
        reasons.append("category_text")
    return ",".join(reasons)


def _score_record(title: str, shop_name: str, brand: str, series: str) -> float:
    hay = f"{title} {shop_name}".lower()
    score = 0.0
    if brand and brand.lower() in hay:
        score += 0.35
    if series and series.lower() in hay:
        score += 0.3
    if any(x in hay for x in ["官方旗舰店", "旗舰店", "自营", "天猫国际自营"]):
        score += 0.25
    if any(x in hay for x in ["避孕套", "安全套", "计生用品"]):
        score += 0.1
    return min(score, 1.0)


async def find_search_input(page: Page):
    selectors = [
        'input[name="q"]',
        "#q",
        'input[placeholder*="搜索"]',
        'input[aria-label*="搜索"]',
        ".search-combobox-input",
    ]
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            await loc.wait_for(state="visible", timeout=3_000)
            return loc
        except Exception:
            continue
    raise RuntimeError("Taobao search input not found")


async def fill_search_keyword(page: Page, keyword: str) -> None:
    input_box = await find_search_input(page)
    await input_box.click()
    await input_box.evaluate(
        """
        (el, kw) => {
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          setter.call(el, kw);
          el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: kw }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        }
        """,
        keyword,
    )


async def search(page: Page, keyword: str, wait_after_search_sec: float) -> None:
    if "taobao.com" not in page.url and "tmall.com" not in page.url:
        await page.goto(TAOBAO_HOME, wait_until="domcontentloaded", timeout=30_000)
    await fill_search_keyword(page, keyword)
    await page.keyboard.press("Enter")
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    await page.wait_for_timeout(int(wait_after_search_sec * 1000))


async def sort_by_sales(page: Page, wait_after_sort_sec: float) -> str:
    before = page.url
    candidates = [
        'a:has-text("销量")',
        'button:has-text("销量")',
        'text="销量"',
    ]
    for selector in candidates:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                await loc.click()
                await page.wait_for_timeout(int(wait_after_sort_sec * 1000))
                if page.url != before:
                    return "sales_sort_url_changed"
                return "sales_sort_clicked_unconfirmed"
        except Exception:
            continue
    return "sales_sort_not_found"


async def extract_search_candidates(
    page: Page,
    *,
    brand: str,
    series: str,
    keyword: str,
    max_candidates: int,
) -> list[ProductRecord]:
    data = await page.evaluate(
        """
        () => {
          const abs = (href) => {
            try { return new URL(href, location.href).href; } catch { return href || ""; }
          };
          const anchors = Array.from(document.querySelectorAll('a[href]'));
          const itemAnchors = anchors.filter(a => {
            const href = a.href || a.getAttribute('href') || '';
            return /item\\.taobao\\.com|detail\\.tmall\\.com|world\\.taobao\\.com/.test(href)
              && !/shop|login/.test(href);
          });
          const seen = new Set();
          const out = [];
          for (const a of itemAnchors) {
            const url = abs(a.href || a.getAttribute('href'));
            const m = url.match(/[?&]id=(\\d+)/);
            const key = m ? m[1] : url;
            if (!key || seen.has(key)) continue;
            seen.add(key);
            let node = a;
            let card = a;
            for (let i = 0; i < 8 && node; i++) {
              const txt = (node.innerText || '').trim();
              const linkCount = node.querySelectorAll ? node.querySelectorAll('a[href]').length : 0;
              if (txt.length > 40 && txt.length < 1200 && linkCount <= 30) {
                card = node;
                break;
              }
              node = node.parentElement;
            }
            const text = (card.innerText || a.innerText || '').trim();
            if (!text || text.length < 8) continue;
            const imgs = Array.from(card.querySelectorAll ? card.querySelectorAll('img') : []);
            const image = (imgs.map(img => img.currentSrc || img.src).find(src => /g-search|alicdn|imgextra/.test(src || '')) || '');
            out.push({ url, text, anchorText: (a.innerText || '').trim(), image });
            if (out.length >= 80) break;
          }
          return out;
        }
        """
    )
    records: list[ProductRecord] = []
    for item in data:
        raw_text = str(item.get("text") or "")
        text = _norm(raw_text)
        anchor_text = _norm(str(item.get("anchorText") or ""))
        url = str(item.get("url") or "")
        image = str(item.get("image") or "")
        title = _derive_title(raw_text, anchor_text, brand, series)
        if not title or len(title) < 6:
            continue
        price_raw = _derive_price_raw(raw_text)
        sales_text = _derive_sales(raw_text)
        location = _derive_location(raw_text)
        shop_name = _derive_shop(raw_text)
        price = normalize_price(price_raw or text)
        pieces = parse_piece_count(title)
        rec = ProductRecord(
            platform="taobao",
            brand=brand,
            series=series,
            keyword=keyword,
            rank_position=len(records) + 1,
            title=title,
            product_url=urljoin(page.url, url),
            shop_name=shop_name,
            shop_type=_shop_type(shop_name, title),
            official_store_flag=_official_store_flag(shop_name, title, brand),
            price_raw=price_raw,
            final_price=price,
            price_source="search_dom" if price is not None else "",
            sales_text=sales_text,
            location=location,
            main_image_url=image,
            piece_count=pieces,
            unit_price=unit_price(price, pieces),
            series_match_reason=_series_match_reason(title, brand, series),
            confidence=_score_record(title, shop_name, brand, series),
        )
        records.append(rec)
        if len(records) >= max_candidates:
            break
    return records


def _derive_title(text: str, anchor_text: str, brand: str, series: str) -> str:
    title_candidates = []
    for line in re.split(r"[\n|]", text):
        line = _norm(line)
        if len(line) < 6:
            continue
        if any(x in line for x in ["¥", "￥", "付款", "券后价", "发货地", "退货宝", "包邮"]):
            continue
        if brand and brand in line:
            title_candidates.append(line)
        elif series and series in line and any(x in line for x in ["避孕套", "安全套", "套", "计生"]):
            title_candidates.append(line)
    if anchor_text and len(anchor_text) >= 6:
        title_candidates.insert(0, anchor_text)
    if title_candidates:
        return max(title_candidates, key=len)[:180]
    parts = re.split(r"¥|￥|券后价|付款|人付款", text)
    return _norm(parts[0])[:180]


def _derive_price_raw(text: str) -> str:
    hits = re.findall(r"(?:券后价|到手价|秒杀价|¥|￥)\s*[0-9]+\s*(?:\.\s*[0-9]{1,2})?", text)
    if hits:
        return " ".join(dict.fromkeys(_norm(x) for x in hits))
    hits = re.findall(r"(?:^|[^\d])([1-9]\d{1,4}(?:\.\d{1,2})?)(?!\d)", text)
    return hits[0] if hits else ""


def _derive_sales(text: str) -> str:
    for pattern in [r"\d+(?:\.\d+)?万?\+?人付款", r"\d+(?:\.\d+)?万?\+?人收货", r"\d+(?:\.\d+)?万?\+?评价"]:
        m = re.search(pattern, text)
        if m:
            return m.group(0)
    return ""


def _derive_location(text: str) -> str:
    known = [
        "北京", "上海", "天津", "重庆", "广东", "广州", "深圳", "浙江", "杭州", "江苏", "南京",
        "山东", "福建", "安徽", "河南", "河北", "湖北", "湖南", "四川", "成都", "辽宁", "吉林",
    ]
    for item in known:
        if item in text:
            return item
    return ""


def _derive_shop(text: str) -> str:
    lines = [_norm(x) for x in re.split(r"[\n|]", text) if _norm(x)]
    candidates = [line for line in lines if any(x in line for x in ["旗舰店", "自营", "专营店", "专卖店", "天猫国际"])]
    if candidates:
        product_words = ["避孕套", "安全套", "超薄", "正品", "囤货", "家庭装", "券后价", "¥", "￥"]

        def score(line: str) -> tuple[int, int]:
            penalty = sum(1 for word in product_words if word in line)
            return (penalty, len(line))

        return min(candidates, key=score)[:80]
    return ""


def _clean_image_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    return url


def _best_main_image(images: list[dict[str, Any]]) -> str:
    candidates = []
    for item in images:
        src = _clean_image_url(str(item.get("src") or ""))
        if not src:
            continue
        try:
            w = int(item.get("w") or 0)
            h = int(item.get("h") or 0)
        except (TypeError, ValueError):
            w, h = 0, 0
        if w >= 400 and h >= 400 and ("alicdn.com" in src or "taobao.com" in src):
            aspect = w / max(h, 1)
            if not 0.55 <= aspect <= 1.6:
                continue
            penalty = 0
            if "-tps-" in src or "/tfs/" in src:
                penalty += 2_000_000
            if "uploaded" in src or "imgextra" in src:
                penalty -= 100_000
            candidates.append((penalty - (w * h), src))
    if not candidates:
        return ""
    return min(candidates, key=lambda x: x[0])[1]


def _line_after(lines: list[str], label: str) -> str:
    for i, line in enumerate(lines):
        if line == label and i + 1 < len(lines):
            return lines[i + 1]
    return ""


def _extract_sku_options(lines: list[str]) -> list[str]:
    options: list[str] = []
    start = -1
    for i, line in enumerate(lines):
        if line == "颜色分类":
            start = i + 1
    if start < 0:
        return options
    stop_words = {"数量", "加入购物车", "领券购买", "立即购买", "收藏"}
    for line in lines[start : start + 30]:
        if line in stop_words or line.startswith("有货"):
            break
        if len(line) >= 4 and (re.search(r"\d+\s*(只|片|枚|个)", line) or "001" in line or "黑金" in line):
            options.append(line)
    return options


def _selected_sku_from_options(options: list[str]) -> str:
    if not options:
        return ""
    # Tmall's visible text order usually places the selected/default SKU first.
    return options[0]


def _extract_detail_info(lines: list[str], images: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n".join(lines)
    shop_name = ""
    for i, line in enumerate(lines[:80]):
        if any(x in line for x in ["旗舰店", "自营", "专营店", "专卖店"]):
            if i + 1 < len(lines) and re.fullmatch(r"\d+(?:\.\d+)?", lines[i + 1]):
                shop_name = line
                break
            if not shop_name and len(line) <= 60:
                shop_name = line
    sku_options = _extract_sku_options(lines)
    selected_sku = _selected_sku_from_options(sku_options)
    spec_parts = []
    for label in ["产地", "安全套 厚薄", "品牌", "产品名称", "材质成分", "颜色分类"]:
        value = _line_after(lines, label)
        if value:
            spec_parts.append(f"{label}: {value}")
    sales_text = ""
    m = re.search(r"已售\s*([0-9.]+万?\+?)", text)
    if m:
        sales_text = f"已售 {m.group(1)}"
    return {
        "shop_name": shop_name,
        "main_image_url": _best_main_image(images),
        "sku_options": sku_options,
        "selected_sku": selected_sku,
        "spec_text": "；".join(spec_parts),
        "sales_text": sales_text,
    }


async def enrich_detail(
    context: BrowserContext,
    record: ProductRecord,
    out_dir: Path,
    *,
    detail_wait_sec: float,
    use_ocr: bool,
) -> ProductRecord:
    page = await context.new_page()
    try:
        await page.goto(record.product_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(int(detail_wait_sec * 1000))
        record.detail_title = _norm(await page.title())
        shots_dir = out_dir / "screenshots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        shot = shots_dir / f"taobao_{record.rank_position:02d}.png"
        await page.screenshot(path=str(shot), full_page=True)
        record.screenshot_path = str(shot)
        try:
            body = await page.locator("body").inner_text(timeout=8_000)
        except Exception:
            body = ""
        try:
            detail_dom = await page.evaluate(
                """
                () => {
                  const lines = (document.body.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                  const images = Array.from(document.images).map(img => ({
                    src: img.currentSrc || img.src,
                    alt: img.alt || '',
                    w: img.naturalWidth || 0,
                    h: img.naturalHeight || 0
                  })).filter(x => x.src);
                  return {lines, images};
                }
                """
            )
        except Exception:
            detail_dom = {"lines": body.splitlines(), "images": []}
        detail_info = _extract_detail_info(
            [str(x).strip() for x in detail_dom.get("lines") or [] if str(x).strip()],
            [x for x in detail_dom.get("images") or [] if isinstance(x, dict)],
        )
        if detail_info.get("shop_name"):
            record.shop_name = str(detail_info["shop_name"])
            record.shop_type = _shop_type(record.shop_name, record.title)
            record.official_store_flag = _official_store_flag(record.shop_name, record.title, record.brand)
        if detail_info.get("main_image_url"):
            record.main_image_url = str(detail_info["main_image_url"])
        if detail_info.get("sku_options"):
            record.sku_options = [str(x) for x in detail_info["sku_options"]]
        if detail_info.get("selected_sku"):
            record.selected_sku = str(detail_info["selected_sku"])
        if detail_info.get("spec_text"):
            record.spec_text = str(detail_info["spec_text"])
        if detail_info.get("sales_text"):
            record.sales_text = str(detail_info["sales_text"])
        combined = "\n".join([record.title, record.detail_title, body[:6000]])
        detail_price = normalize_price(combined)
        if detail_price is not None:
            record.final_price = detail_price
            record.price_source = "detail_dom"
        pieces = parse_piece_count(record.selected_sku) or parse_piece_count(record.spec_text) or parse_piece_count(combined)
        if pieces:
            record.piece_count = pieces
        record.unit_price = unit_price(record.final_price, record.piece_count)
        if use_ocr and record.screenshot_path:
            ocr_text = ocr_image_file(Path(record.screenshot_path))
            if ocr_text:
                ocr_price = normalize_price(ocr_text)
                if ocr_price is not None:
                    record.ocr_price = ocr_price
                    record.notes = _norm(f"{record.notes} ocr_price={ocr_price}")
        if not record.piece_count:
            record.notes = _norm(f"{record.notes} missing_piece_count")
        if record.final_price is None:
            record.notes = _norm(f"{record.notes} missing_price")
        return record
    except Exception as exc:
        record.notes = _norm(f"{record.notes} detail_error={type(exc).__name__}:{exc}")
        return record
    finally:
        await page.close()
