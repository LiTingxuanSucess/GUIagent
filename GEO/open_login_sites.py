# -*- coding: utf-8 -*-
"""
在浏览器中打开全部国内 AI 平台登录页，便于一次性登录。

用法:
  # 连接已有 CDP（Chrome 或 Cloak 已启动）
  set CDP_URL=http://127.0.0.1:9222
  py -3.11 open_login_sites.py

  # 指定站点子集
  py -3.11 open_login_sites.py --sites deepseek,doubao,kimi

  # 仅列出 URL
  py -3.11 open_login_sites.py --list
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from chinese_ai_sites import CHINESE_AI_SITES, DEFAULT_SITE_ORDER, ChineseAISite


def _parse_sites(raw: str | None) -> list[str]:
    if not raw:
        return list(DEFAULT_SITE_ORDER)
    ids = [s.strip().lower() for s in raw.split(",") if s.strip()]
    bad = [s for s in ids if s not in CHINESE_AI_SITES]
    if bad:
        raise SystemExit(f"未知站点: {bad}，可选: {list(CHINESE_AI_SITES)}")
    return ids


def list_sites(site_ids: list[str]) -> None:
    print("=" * 60)
    print("国内 AI 平台 — 登录 URL")
    print("=" * 60)
    for sid in site_ids:
        s = CHINESE_AI_SITES[sid]
        print(f"  [{sid}] {s.name}")
        print(f"       {s.url}")
        if s.notes:
            print(f"       ({s.notes})")
    print()


def open_tabs_via_playwright(cdp_url: str, site_ids: list[str], pause: float) -> None:
    from playwright.sync_api import sync_playwright

    sites = [CHINESE_AI_SITES[sid] for sid in site_ids]
    print(f"连接 CDP: {cdp_url}")
    print(f"将打开 {len(sites)} 个标签页…")

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = browser.new_context()

        # 第一个 URL：用已有 tab 或新建
        first = sites[0]
        if context.pages:
            page = context.pages[0]
            page.goto(first.url, wait_until="domcontentloaded", timeout=120000)
            print(f"  [1/{len(sites)}] {first.name}: {first.url}")
        else:
            page = context.new_page()
            page.goto(first.url, wait_until="domcontentloaded", timeout=120000)
            print(f"  [1/{len(sites)}] {first.name}: {first.url}")

        for i, site in enumerate(sites[1:], start=2):
            p = context.new_page()
            p.goto(site.url, wait_until="domcontentloaded", timeout=120000)
            print(f"  [{i}/{len(sites)}] {site.name}: {site.url}")
            if pause > 0:
                time.sleep(pause)

        print()
        print("全部标签页已打开。请在浏览器中完成各平台登录。")
        print("登录完成后关闭浏览器，或保持运行供 test.py 连接 CDP 抓取。")
        # 不 close browser — 保留会话供后续抓取


def main() -> None:
    parser = argparse.ArgumentParser(description="打开国内 AI 平台登录页")
    parser.add_argument(
        "--cdp-url",
        default=os.environ.get("CDP_URL", "http://127.0.0.1:9222"),
        help="CDP HTTP 地址（默认 CDP_URL 环境变量或 9222）",
    )
    parser.add_argument("--sites", help="逗号分隔站点 id，默认全部 6 个")
    parser.add_argument("--list", action="store_true", help="仅列出 URL")
    parser.add_argument(
        "--pause",
        type=float,
        default=0.5,
        help="每个新标签页间隔秒数",
    )
    args = parser.parse_args()

    site_ids = _parse_sites(args.sites)
    if args.list:
        list_sites(site_ids)
        return

    list_sites(site_ids)
    open_tabs_via_playwright(args.cdp_url.strip(), site_ids, args.pause)


if __name__ == "__main__":
    main()
