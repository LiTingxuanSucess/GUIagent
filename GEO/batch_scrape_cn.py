# -*- coding: utf-8 -*-
"""
批量抓取：6 平台 × questions_cn.json 中的问题。

用法:
  set CDP_URL=http://127.0.0.1:9222
  py -3.11 batch_scrape_cn.py
  py -3.11 batch_scrape_cn.py --site deepseek
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

from chinese_ai_sites import DEFAULT_SITE_ORDER
from cn_ai.scraper import scrape_one

EXPORT_DIR = GEO_DIR / "exports"
QUESTIONS_PATH = GEO_DIR / "questions_cn.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-url", default=os.environ.get("CDP_URL", "http://127.0.0.1:9222"))
    parser.add_argument("--site", help="仅跑单站")
    parser.add_argument("--question-id", help="仅跑单题")
    args = parser.parse_args()

    qdata = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions = qdata["questions"]
    if args.question_id:
        questions = [q for q in questions if q["id"] == args.question_id]

    sites = [args.site] if args.site else list(DEFAULT_SITE_ORDER)
    batch_id = f"cn-ai-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp_url)
        print(f"CDP connected, batch={batch_id}")

        for site_id in sites:
            for q in questions:
                prompt = q["prompt"]
                qid = q["id"]
                print(f"\n>>> {site_id} | {qid} | {prompt[:40]}...")
                try:
                    r = scrape_one(browser, site_id, prompt, question_id=qid)
                    results.append(r)
                    print(f"    ok={r.get('ok')} source={r.get('source')} citations={len(r.get('citations', []))}")
                except Exception as e:
                    print(f"    FAIL: {e}")
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
        "timestamp": datetime.now().isoformat(),
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
    print(f"\nDone: {out}")
    print(f"Summary: {batch['summary']}")


if __name__ == "__main__":
    main()
