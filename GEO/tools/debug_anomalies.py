# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cn_ai.network_parser import parse_deepseek_sse, parse_yiyan_history

GEO = Path(__file__).resolve().parents[1]

rd = GEO / ".scrape_cache/cn_ai/deepseek/20260812_005909"
items = json.loads((rd / "network_captures.json").read_text(encoding="utf-8"))
for it in items:
    if "completion" in (it.get("url") or ""):
        a, c = parse_deepseek_sse(it["body"])
        print("completion", len(a), len(c), (a or "")[:160].replace("\n", " "))

hist = next(it["body"] for it in items if "history_messages" in (it.get("url") or ""))
data = json.loads(hist)
msgs = (
    (data.get("data") or {}).get("biz_data", {}).get("chat_messages")
    or (data.get("data") or {}).get("chat_messages")
    or []
)
for i, m in enumerate(msgs):
    role = m.get("role")
    fr = m.get("fragments") or []
    texts = []
    for f in fr:
        c = f.get("content")
        if isinstance(c, str):
            texts.append(c[:80])
        elif isinstance(c, dict):
            texts.append(str(c)[:80])
    print(i, role, "|".join(texts).replace("\n", " ")[:100])

rd = GEO / ".scrape_cache/cn_ai/yiyan/20260812_010257"
items = json.loads((rd / "network_captures.json").read_text(encoding="utf-8"))
for it in items:
    u = it.get("url") or ""
    b = it.get("body") or ""
    if "history/list" not in u:
        continue
    print("yiyan body len", len(b), "endswith", repr(b[-40:]))
    try:
        obj = json.loads(b)
        print("json ok")
    except Exception as e:
        print("json fail", e)
        # try salvage: find last complete history entry value lens via regex
    a, c = parse_yiyan_history(b, prompt="全球纯电家用车推荐？")
    print("parse_yiyan", len(a), len(c), (a[:100] + "..." + a[-60:]) if a else "")
