# -*- coding: utf-8 -*-
"""Salvage DeepSeek Q2 from captures; re-scrape Yiyan Q3; update showcase."""
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
GEO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GEO))

from cn_ai.network_parser import merge_captures
from cn_ai.scraper import find_page, scrape_one
from chinese_ai_sites import CHINESE_AI_SITES
from playwright.sync_api import sync_playwright

EXPORT = GEO / "exports"
SHOWCASE = EXPORT / "cn-ai-showcase.json"


def domains(urls):
    return list({urlparse(u).netloc for u in urls if u})


def salvage_deepseek_decision() -> dict:
    rd = GEO / ".scrape_cache/cn_ai/deepseek/20260812_005909"
    items = json.loads((rd / "network_captures.json").read_text(encoding="utf-8"))
    prompt = "决策式AI厂商代表有哪些？"
    answer, cites, source = merge_captures(items, "deepseek", prompt=prompt)
    old = json.loads((rd / "result.json").read_text(encoding="utf-8"))
    old.update({
        "question_id": "decision_ai_vendors",
        "prompt": prompt,
        "answer": answer,
        "citations": cites,
        "citation_domains": domains(cites),
        "source": source,
        "ok": bool(answer) and "求解器" not in (answer[:40] if answer else ""),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    })
    (rd / "result.json").write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    return old


def rescrape_yiyan_ev() -> dict:
    cdp = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
    prompt = "全球纯电家用车推荐？"
    qid = "ev_cars_global"
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp)
        page = find_page(browser, "yiyan")
        if not page:
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.goto(CHINESE_AI_SITES["yiyan"].url, wait_until="domcontentloaded", timeout=120000)
        else:
            page.goto("https://wenxin.baidu.com/", wait_until="domcontentloaded", timeout=120000)
        time.sleep(2)
        print(f"yiyan url={page.url}", flush=True)
        r = scrape_one(browser, "yiyan", prompt, question_id=qid)
        print(
            f"yiyan ok={r.get('ok')} src={r.get('source')} "
            f"ans={len(r.get('answer') or '')} cites={len(r.get('citations') or [])}",
            flush=True,
        )
        print((r.get("answer") or r.get("error") or "")[:180].replace("\n", " "), flush=True)
        print("tail:", (r.get("answer") or "")[-120:].replace("\n", " "), flush=True)
        return r


def update_showcase(deepseek_r: dict, yiyan_r: dict) -> None:
    data = json.loads(SHOWCASE.read_text(encoding="utf-8"))
    out = []
    for r in data["results"]:
        key = (r.get("site_id"), r.get("question_id"))
        if key == ("deepseek", "decision_ai_vendors"):
            out.append(deepseek_r)
        elif key == ("yiyan", "ev_cars_global"):
            out.append(yiyan_r)
        else:
            out.append(r)
    data["results"] = out
    data["timestamp"] = datetime.now().isoformat(timespec="seconds")
    data["summary"] = {
        "total": len(out),
        "ok": sum(1 for r in out if r.get("ok")),
        "failed": sum(1 for r in out if not r.get("ok")),
        "with_citations": sum(1 for r in out if r.get("citations")),
    }
    data["note"] = "三题×六站；已修复 deepseek Q2 串题与 yiyan Q3 截断"
    SHOWCASE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("updated", SHOWCASE, data["summary"], flush=True)


def main() -> None:
    ds = salvage_deepseek_decision()
    print(
        f"deepseek salvage ok={ds.get('ok')} ans={len(ds.get('answer') or '')} "
        f"src={ds.get('source')} cites={len(ds.get('citations') or [])}",
        flush=True,
    )
    print((ds.get("answer") or "")[:120].replace("\n", " "), flush=True)

    yy = rescrape_yiyan_ev()
    if not yy.get("ok") or len(yy.get("answer") or "") < 600:
        raise SystemExit(f"yiyan rescrape insufficient: ok={yy.get('ok')} len={len(yy.get('answer') or '')}")

    update_showcase(ds, yy)

    # quick sanity
    data = json.loads(SHOWCASE.read_text(encoding="utf-8"))
    by = {(r["site_id"], r["question_id"]): r for r in data["results"]}
    a1 = by[("deepseek", "solver_vendors")]["answer"]
    a2 = by[("deepseek", "decision_ai_vendors")]["answer"]
    print("deepseek q1==q2?", a1 == a2, flush=True)
    print("yiyan q3 len", len(by[("yiyan", "ev_cars_global")]["answer"] or ""), flush=True)


if __name__ == "__main__":
    main()
