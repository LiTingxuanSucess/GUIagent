# -*- coding: utf-8 -*-
"""
对六个国内 AI 平台执行研究探测：profiling + DOM + 网络捕获。

用法:
  set CDP_URL=http://127.0.0.1:9222
  py -3.11 research_all_sites.py
  py -3.11 research_all_sites.py --site deepseek
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

os.environ.setdefault("NO_PROXY", "*")

GEO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(GEO_DIR))

from chinese_ai_sites import CHINESE_AI_SITES, DEFAULT_SITE_ORDER
from cn_ai.profiler import RESEARCH_ROOT, SiteProfiler
from cn_ai.site_flows import (
    SITE_URL_PATTERNS,
    TEST_PROMPT,
    extract_answer_and_links,
    probe_dom,
    send_prompt,
    wait_for_answer_growth,
)


def find_page_for_site(browser, site_id: str):
    patterns = SITE_URL_PATTERNS.get(site_id, [])
    for ctx in browser.contexts:
        for page in ctx.pages:
            url = page.url
            if any(p in url for p in patterns):
                return page
    return None


def research_site(browser, site_id: str, prompt: str) -> dict:
    run_dir = RESEARCH_ROOT / site_id / datetime.now().strftime("%Y%m%d_%H%M%S")
    profiler = SiteProfiler(site_id, run_dir)

    page = find_page_for_site(browser, site_id)
    if not page:
        return {"site_id": site_id, "error": "tab not found", "patterns": SITE_URL_PATTERNS.get(site_id)}

    profiler.attach_page(page)

    def step_attach():
        return page.url

    profiler.step("00_initial_url", step_attach)

    def step_probe():
        data = probe_dom(page)
        (run_dir / "dom_probe.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return f"inputs={len(data.get('inputs', []))} buttons={len(data.get('buttons', []))}"

    profiler.step("01_dom_probe", step_probe)

    def step_prep():
        from cn_ai.site_flows import SITE_PREP
        fn = SITE_PREP.get(site_id, lambda p: "skip")
        return fn(page)

    profiler.step("02_site_prep", step_prep)

    def step_send():
        return send_prompt(page, prompt, site_id)

    prep, method = profiler.step("03_send_prompt", step_send)
    (run_dir / "send_meta.json").write_text(
        json.dumps({"prep": prep, "method": method, "prompt": prompt}, ensure_ascii=False),
        encoding="utf-8",
    )

    def step_wait():
        text = wait_for_answer_growth(page, timeout_sec=75)
        return f"answer_chars={len(text)}"

    profiler.step("04_wait_answer", step_wait, save_dom=True)

    def step_extract():
        data = extract_answer_and_links(page, site_id)
        (run_dir / "extracted.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return f"links={len(data.get('links', []))}"

    profiler.step("05_extract", step_extract)

    summary_path = profiler.save_summary()
    return {
        "site_id": site_id,
        "url": page.url,
        "run_dir": str(run_dir.relative_to(GEO_DIR)),
        "summary": str(summary_path.relative_to(GEO_DIR)),
        "api_count": len(profiler._extract_api_endpoints()),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-url", default=os.environ.get("CDP_URL", "http://127.0.0.1:9222"))
    parser.add_argument("--site", help="单个站点")
    parser.add_argument("--prompt", default=TEST_PROMPT)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    sites = [args.site] if args.site else list(DEFAULT_SITE_ORDER)
    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp_url)
        print(f"CDP connected, contexts={len(browser.contexts)}")
        for sid in sites:
            print(f"\n=== Research: {sid} ({CHINESE_AI_SITES[sid].name}) ===")
            try:
                r = research_site(browser, sid, args.prompt)
                results.append(r)
                print(f"  -> {json.dumps(r, ensure_ascii=False)}")
            except Exception as e:
                print(f"  FAIL: {e}")
                results.append({"site_id": sid, "error": str(e)})
            time.sleep(2)

    out = RESEARCH_ROOT / "latest_run.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nAll done. {out}")


if __name__ == "__main__":
    main()
