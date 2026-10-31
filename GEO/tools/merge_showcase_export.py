# -*- coding: utf-8 -*-
"""Merge best per-site solver_vendors exports into one showcase file; delete the rest."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

exp = Path(__file__).resolve().parents[1] / "exports"
picks = {
    "deepseek": "cn-ai-20260811-220026.json",
    "doubao": "cn-ai-20260811-220659.json",
    "yiyan": "cn-ai-20260811-221210.json",
    "yuanbao": "cn-ai-20260811-221654.json",
    "qianwen": "cn-ai-20260811-221909.json",
    "kimi": "cn-ai-20260811-230311.json",
}
order = ["deepseek", "doubao", "yiyan", "yuanbao", "qianwen", "kimi"]
results = []
for sid in order:
    src = exp / picks[sid]
    if not src.exists():
        raise SystemExit(f"missing source: {src}")
    d = json.loads(src.read_text(encoding="utf-8"))
    r = dict(d["results"][0])
    r["site_id"] = sid
    r["question_id"] = "solver_vendors"
    results.append(r)

batch_id = "cn-ai-showcase"
batch = {
    "batch_id": batch_id,
    "timestamp": datetime.now().isoformat(timespec="seconds"),
    "questions": ["solver_vendors"],
    "sites": order,
    "summary": {
        "total": len(results),
        "ok": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "with_citations": sum(1 for r in results if r.get("citations")),
    },
    "note": "六站 solver_vendors 验收合并件；已清理其余历史 export",
    "results": results,
}

for p in sorted(exp.glob("cn-ai-*.json")):
    p.unlink()
    print("deleted", p.name)

out = exp / f"{batch_id}.json"
out.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
print("wrote", out.name, "n=", len(results), batch["summary"])
