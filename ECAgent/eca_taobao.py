from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from src.browser import attach_chrome, wait_for_cdp_or_start
from src.exporter import write_csv, write_json
from src.taobao import TAOBAO_HOME, enrich_detail, extract_search_candidates, search, sort_by_sales

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def first_taobao_target(cfg: dict[str, Any], keyword_arg: str | None) -> dict[str, str]:
    if keyword_arg:
        return {"platform": "taobao", "brand": "", "series": "", "keyword": keyword_arg}
    for target in cfg.get("targets") or []:
        if str(target.get("platform") or "").lower() == "taobao":
            brand = str(target.get("brand") or "")
            series = str(target.get("series") or "")
            keyword = str(target.get("keyword") or f"{brand} {series}").strip()
            return {"platform": "taobao", "brand": brand, "series": series, "keyword": keyword}
    return {"platform": "taobao", "brand": "杜蕾斯", "series": "001", "keyword": "杜蕾斯 001"}


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    browser_cfg = cfg.get("browser") or {}
    taobao_cfg = cfg.get("taobao") or {}
    target = first_taobao_target(cfg, args.keyword)

    cdp_url = str(args.cdp or browser_cfg.get("cdp_url") or "http://127.0.0.1:9222")
    await wait_for_cdp_or_start(cdp_url, start_if_missing=not args.no_start, url=TAOBAO_HOME)

    out_dir = args.out_dir or Path("exports") / f"taobao_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pw, browser, context = await attach_chrome(cdp_url)
    try:
        page = None
        for p in context.pages:
            if "taobao.com" in p.url or "tmall.com" in p.url:
                page = p
                break
        if page is None:
            page = await context.new_page()
            await page.goto(TAOBAO_HOME, wait_until="domcontentloaded", timeout=30_000)
        await page.bring_to_front()

        print(f">>> keyword: {target['keyword']}", flush=True)
        await search(
            page,
            target["keyword"],
            wait_after_search_sec=float(taobao_cfg.get("wait_after_search_sec", 4)),
        )
        print(f">>> search url: {page.url}", flush=True)

        if bool(taobao_cfg.get("sort_by_sales", True)):
            rank_source = await sort_by_sales(page, wait_after_sort_sec=float(taobao_cfg.get("wait_after_sort_sec", 3)))
            print(f">>> after sort url: {page.url}", flush=True)
        else:
            rank_source = "default_search_order"

        search_shot = out_dir / "search_results.png"
        await page.screenshot(path=str(search_shot), full_page=True)

        records = await extract_search_candidates(
            page,
            brand=target["brand"],
            series=target["series"],
            keyword=target["keyword"],
            max_candidates=int(args.max_candidates or taobao_cfg.get("max_candidates", 12)),
        )
        print(f">>> candidates: {len(records)}", flush=True)
        for rec in records:
            rec.rank_source = rank_source
        for rec in records:
            print(f"    #{rec.rank_position} price={rec.final_price} sales={rec.sales_text} shop={rec.shop_name} title={rec.title[:60]}", flush=True)

        detail_limit = int(args.detail_limit or taobao_cfg.get("detail_limit", 5))
        use_ocr = bool(args.ocr or taobao_cfg.get("use_ocr", False))
        for rec in records[:detail_limit]:
            print(f">>> detail #{rec.rank_position}: {rec.title[:80]}", flush=True)
            await enrich_detail(
                context,
                rec,
                out_dir,
                detail_wait_sec=float(taobao_cfg.get("detail_wait_sec", 4)),
                use_ocr=use_ocr,
            )

        rows = [x.to_dict() for x in records]
        write_json(out_dir / "taobao_results.json", {"target": target, "search_url": page.url, "rows": rows})
        write_csv(out_dir / "taobao_results.csv", rows)
        print(f">>> written: {out_dir / 'taobao_results.json'}", flush=True)
        print(f">>> written: {out_dir / 'taobao_results.csv'}", flush=True)
        print(f">>> screenshot: {search_shot}", flush=True)
        return 0
    finally:
        await pw.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Taobao price tracking flow")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "config.yaml")
    parser.add_argument("--cdp", default=None)
    parser.add_argument("--keyword", default=None)
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--detail-limit", type=int, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--no-start", action="store_true", help="Do not start Chrome when CDP is unavailable")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
