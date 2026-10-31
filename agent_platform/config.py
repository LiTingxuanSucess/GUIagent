from __future__ import annotations

import os
from pathlib import Path

# gui-agent 根目录：…/gui-agent
GUI_AGENT_ROOT = Path(__file__).resolve().parent.parent


def db_path() -> Path:
    p = os.environ.get("AGENT_PLATFORM_DB")
    if p:
        return Path(p)
    (GUI_AGENT_ROOT / "data").mkdir(parents=True, exist_ok=True)
    return GUI_AGENT_ROOT / "data" / "agent_platform.db"


def wxwork_script_path() -> Path:
    p = os.environ.get("WXWORK_ADD_CUSTOMER_SCRIPT")
    if p:
        return Path(p)
    return GUI_AGENT_ROOT / "WXWorkAgent" / "wxwork_add_customer.py"


def wxwork_cwd() -> Path:
    p = os.environ.get("WXWORK_AGENT_CWD")
    if p:
        return Path(p)
    return GUI_AGENT_ROOT / "WXWorkAgent"


def public_base_url() -> str:
    """供子进程回调用的平台根 URL（无尾斜杠）。"""
    return os.environ.get("AGENT_PLATFORM_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
