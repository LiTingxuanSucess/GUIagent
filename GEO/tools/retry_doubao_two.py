# -*- coding: utf-8 -*-
"""Retry doubao for decision_ai_vendors + salvage/re-scrape ev_cars_global."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

os.environ.setdefault("NO_PROXY", "*")
GEO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GEO_DIR))

from chinese_ai_sites import CHINESE_AI_SITES
from cn_ai.network_parser import parse_doubao_completion_sse
from cn_ai.scraper import find_page, scrape_one
from playwright.sync_api import sync_playwright

EXPORT_DIR = GEO_DIR / "exports"


def salvage_ev_cars() -> dict:
    run = GEO_DIR / ".scrape_cache" / "cn_ai" / "doubao" / "20260812_010133"
    items = json.loads((run / "network_captures.json").read_text(encoding="utf-8"))
    body = next(it["body"] for it in items if "/chat/completion" in (it.get("url") or ""))
    answer, cites = parse_doubao_completion_sse(body)
    old = json.loads((run / "result.json").read_text(encoding="utf-8"))
    old.update({
        "answer": answer,
        "citations": cites,
        "citation_domains": list({urlparse(u).netloc for u in cites if u}),
        "source": "network:/chat/completion+CHUNK_DELTA",
        "ok": bool(answer),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    })
    (run / "result.json").write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    return old


def main() -> None:
    salvaged = salvage_ev_cars()
    print(
        f"salvaged ev_cars ok={salvaged.get('ok')} ans={len(salvaged.get('answer') or '')} "
        f"cites={len(salvaged.get('citations') or [])}",
        flush=True,
    )

    cdp = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
    qid = "decision_ai_vendors"
    prompt = "决策式AI厂商代表有哪些？"
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp)
        page = find_page(browser, "doubao")
        if not page:
            ctx = browser.contexts[0]
            page = ctx.new_page()
        page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=120000)
        time.sleep(3)
        print(f"doubao url={page.url}", flush=True)
        r = scrape_one(browser, "doubao", prompt, question_id=qid)
        print(
            f"decision ok={r.get('ok')} src={r.get('source')} "
            f"ans={len(r.get('answer') or '')} cites={len(r.get('citations') or [])}",
            flush=True,
        )
        print((r.get("answer") or r.get("error") or "")[:220].replace("\n", " "), flush=True)

    batch = {
        "batch_id": f"cn-ai-doubao-retry-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "questions": ["decision_ai_vendors", "ev_cars_global"],
        "sites": ["doubao"],
        "results": [r, salvaged],
    }
    out = EXPORT_DIR / f"{batch['batch_id']}.json"
    out.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
