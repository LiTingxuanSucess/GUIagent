# -*- coding: utf-8 -*-
"""Scrape decision_ai_vendors + ev_cars_global on all CN AI sites; one CDP session."""
from __future__ import annotations

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

GEO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GEO_DIR))

from chinese_ai_sites import CHINESE_AI_SITES, DEFAULT_SITE_ORDER
from cn_ai.scraper import find_page, scrape_one
from playwright.sync_api import sync_playwright

EXPORT_DIR = GEO_DIR / "exports"
QUESTIONS_PATH = GEO_DIR / "questions_cn.json"
TARGET_IDS = ["decision_ai_vendors", "ev_cars_global"]


def ensure_tabs(browser, site_ids: list[str]) -> None:
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    for sid in site_ids:
        if find_page(browser, sid):
            print(f"  tab ok: {sid}", flush=True)
            continue
        site = CHINESE_AI_SITES[sid]
        print(f"  open tab: {sid} -> {site.url}", flush=True)
        page = ctx.new_page()
        page.goto(site.url, wait_until="domcontentloaded", timeout=120000)
        time.sleep(1.5)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--sites", help="comma site ids; default all")
    args = parser.parse_args()

    cdp = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
    qdata = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions = [q for q in qdata["questions"] if q["id"] in TARGET_IDS]
    if len(questions) != 2:
        raise SystemExit(f"expected 2 questions, got {[q['id'] for q in questions]}")

    if args.sites:
        sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    else:
        sites = list(DEFAULT_SITE_ORDER)
    batch_id = f"cn-ai-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp)
        print(f"CDP connected, batch={batch_id}", flush=True)
        print("Ensuring tabs…", flush=True)
        ensure_tabs(browser, sites)
        for site_id in sites:
            for q in questions:
                prompt = q["prompt"]
                qid = q["id"]
                print(f"\n>>> {site_id} | {qid} | {prompt}", flush=True)
                try:
                    r = scrape_one(browser, site_id, prompt, question_id=qid)
                    results.append(r)
                    print(
                        f"    ok={r.get('ok')} source={r.get('source')} "
                        f"citations={len(r.get('citations') or [])}",
                        flush=True,
                    )
                except Exception as e:
                    print(f"    FAIL: {e}", flush=True)
                    results.append({
                        "site_id": site_id,
                        "question_id": qid,
                        "prompt": prompt,
                        "error": str(e),
                        "ok": False,
                    })
                time.sleep(3)

    ok = sum(1 for r in results if r.get("ok"))
    with_cites = sum(1 for r in results if r.get("citations"))
    batch = {
        "batch_id": batch_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "questions": [q["id"] for q in questions],
        "sites": sites,
        "summary": {
            "total": len(results),
            "ok": ok,
            "failed": len(results) - ok,
            "with_citations": with_cites,
        },
        "results": results,
    }
    EXPORT_DIR.mkdir(exist_ok=True)
    out = EXPORT_DIR / f"{batch_id}.json"
    out.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone: {out}", flush=True)
    print(f"Summary: {batch['summary']}", flush=True)


if __name__ == "__main__":
    main()
