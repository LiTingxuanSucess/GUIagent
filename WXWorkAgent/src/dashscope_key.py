"""Resolve DashScope API key: WXWork qwen.api_key → env → PRAgent global.api_key."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def resolve_dashscope_api_key(qwen_cfg: dict[str, Any] | None) -> str:
    qwen_cfg = qwen_cfg or {}
    raw = qwen_cfg.get("api_key")
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    env = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if env:
        return env
    # 与 WXWorkAgent 同级的 PRAgent/config.yaml
    pragent_cfg = Path(__file__).resolve().parent.parent.parent / "PRAgent" / "config.yaml"
    if pragent_cfg.is_file():
        with pragent_cfg.open(encoding="utf-8") as f:
            pr = yaml.safe_load(f) or {}
        g = pr.get("global") or {}
        k = g.get("api_key")
        if k is not None and str(k).strip():
            return str(k).strip()
    return ""
