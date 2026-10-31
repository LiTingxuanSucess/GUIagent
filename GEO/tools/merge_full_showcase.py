# -*- coding: utf-8 -*-
"""Merge solver showcase + new two-question scrapes into one cn-ai-showcase.json."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

GEO = Path(__file__).resolve().parents[1]
EXP = GEO / "exports"

SITE_ORDER = ["deepseek", "doubao", "yiyan", "yuanbao", "qianwen", "kimi"]
Q_ORDER = ["solver_vendors", "decision_ai_vendors", "ev_cars_global"]


def load(name: str) -> dict:
    return json.loads((EXP / name).read_text(encoding="utf-8"))


def key(r: dict) -> tuple[str, str]:
    return (r.get("site_id") or "", r.get("question_id") or "")


def main() -> None:
    by: dict[tuple[str, str], dict] = {}

    # 1) existing solver_vendors six-site
    for r in load("cn-ai-showcase.json")["results"]:
        by[key(r)] = r

    # 2) deepseek two questions
    for r in load("cn-ai-20260812-005846.json")["results"]:
        if r.get("site_id") == "deepseek" and r.get("ok"):
            by[key(r)] = r

    # 3) five-site batch (skip failed doubao)
    for r in load("cn-ai-20260812-010125.json")["results"]:
        if r.get("site_id") == "doubao":
            continue
        if r.get("ok"):
            by[key(r)] = r

    # 4) doubao retry
    for r in load("cn-ai-doubao-retry-20260812-011134.json")["results"]:
        if r.get("ok"):
            by[key(r)] = r

    results = []
    missing = []
    for qid in Q_ORDER:
        for sid in SITE_ORDER:
            r = by.get((sid, qid))
            if not r:
                missing.append(f"{sid}/{qid}")
                continue
            results.append(r)

    batch = {
        "batch_id": "cn-ai-showcase",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "questions": Q_ORDER,
        "sites": SITE_ORDER,
        "summary": {
            "total": len(results),
            "ok": sum(1 for r in results if r.get("ok")),
            "failed": sum(1 for r in results if not r.get("ok")),
            "with_citations": sum(1 for r in results if r.get("citations")),
        },
        "note": "三题×六站完整合并件（solver + decision + ev）",
        "results": results,
    }

    # wipe other exports
    for p in sorted(EXP.glob("*.json")):
        if p.name != "cn-ai-showcase.json":
            p.unlink()
            print("deleted", p.name)

    out = EXP / "cn-ai-showcase.json"
    out.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out.name, batch["summary"])
    if missing:
        print("MISSING:", missing)
    else:
        print("complete: 3 questions × 6 sites")
    for qid in Q_ORDER:
        row = []
        for sid in SITE_ORDER:
            r = by.get((sid, qid))
            if not r:
                row.append(f"{sid}:MISS")
            else:
                row.append(f"{sid}:ok={r.get('ok')}/c={len(r.get('citations') or [])}")
        print(qid, " | ".join(row))


if __name__ == "__main__":
    main()
