"""WXWork Agent Console — 统一后端。

保留原有 /api/wxwork/* 接口（企微桌面端同步的 recent_chats 归档），新增：
  /api/leads/*              — 客户线索（添加联系人任务）
  /api/conversations/*      — 双平台统一消息流（抖音/企微上报到 SQLite）
  /api/conversation_threads — 会话列表聚合
  /api/seed-demo               — 一键注入演示数据（方便无真实 agent 时测试）
  /api/import/douyin-recent-chats — 从 DouyinCSAgent/exports/*/recent_chats.json 写入真实抓取对话
  /api/moments/*               — 企业朋友圈（multipart：content + 可选图片上传）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
import sys
import threading
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlunparse
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ── 路径常量 ──────────────────────────────────────────────────────────────

BASE_DIR            = Path(__file__).resolve().parents[2]   # WXWorkAgent/
EXPORTS_DIR         = BASE_DIR / "exports"
MOMENT_UPLOADS_DIR = BASE_DIR / "data" / "moment_uploads"
DB_PATH             = BASE_DIR / "data" / "agent_platform.db"
LOGS_DIR            = BASE_DIR / "logs" / "add_customer"
DOUYIN_EXPORTS_ROOT = (BASE_DIR.parent / "DouyinCSAgent" / "exports").resolve()
REVAI_LOCAL_BASE_URL = os.getenv("REVAI_LOCAL_BASE_URL", "https://car.revai.cn").rstrip("/")

EXPORTS_DIR.mkdir(exist_ok=True)
MOMENT_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── FastAPI app ───────────────────────────────────────────────────────────

app = FastAPI(title="Agent Console", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/exports", StaticFiles(directory=EXPORTS_DIR), name="exports")


# ── 工具函数 ──────────────────────────────────────────────────────────────

def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS moments (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              content      TEXT NOT NULL,
              image_path   TEXT,
              cdp_url      TEXT NOT NULL DEFAULT 'http://127.0.0.1:9222',
              status       TEXT NOT NULL DEFAULT 'pending',
              created_at   TEXT NOT NULL,
              updated_at   TEXT NOT NULL,
              log_tail     TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_moments_created ON moments(created_at DESC);

            CREATE TABLE IF NOT EXISTS leads (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              phone         TEXT NOT NULL,
              source        TEXT,
              source_user   TEXT,
              source_context TEXT,
              status        TEXT NOT NULL DEFAULT 'pending',
              created_at    TEXT NOT NULL,
              updated_at    TEXT NOT NULL,
              result_detail TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_leads_phone    ON leads(phone);
            CREATE INDEX IF NOT EXISTS idx_leads_status   ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_leads_created  ON leads(created_at DESC);

            CREATE TABLE IF NOT EXISTS conversations (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              platform     TEXT NOT NULL,
              contact_id   TEXT,
              contact_name TEXT,
              direction    TEXT NOT NULL,
              content      TEXT NOT NULL,
              msg_type     TEXT NOT NULL DEFAULT 'text',
              created_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(platform, created_at DESC);
            """
        )
        conn.commit()


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    _init_db()
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def _peer_key(platform: str, cid: str | None, cname: str | None) -> str:
    if cid and cid.strip():
        return f"{platform}:{cid.strip()}"
    if cname and cname.strip():
        return f"{platform}:{cname.strip()}"
    return f"{platform}:unknown"


def _append_event(lead_id: int, ev: dict[str, Any]) -> None:
    with _conn() as c:
        row = c.execute("SELECT result_detail FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not row:
            return
        try:
            arr = json.loads(row["result_detail"] or "[]")
            if not isinstance(arr, list):
                arr = []
        except json.JSONDecodeError:
            arr = []
        arr.append(ev)
        c.execute(
            "UPDATE leads SET result_detail=?, updated_at=? WHERE id=?",
            (json.dumps(arr, ensure_ascii=False), _utc(), lead_id),
        )
        c.commit()


# ── Pydantic 模型 ─────────────────────────────────────────────────────────

class LeadCreate(BaseModel):
    phone: str
    source: str | None = "manual"
    source_user: str | None = None
    source_context: str | None = None


class LeadOut(BaseModel):
    id: int
    phone: str
    source: str | None
    source_user: str | None
    source_context: str | None
    status: str
    created_at: str
    updated_at: str
    result_detail: list[dict[str, Any]]


class LeadEvent(BaseModel):
    step: str
    ok: bool
    detail: str | None = None
    screenshot_path: str | None = None
    ts: str | None = None


class ConvIn(BaseModel):
    platform: str = Field(..., pattern="^(douyin|wxwork)$")
    contact_id: str | None = None
    contact_name: str | None = None
    direction: str = Field(..., pattern="^(in|out)$")
    content: str
    msg_type: str = "text"


class ConvOut(BaseModel):
    id: int
    platform: str
    contact_id: str | None
    contact_name: str | None
    peer_key: str
    direction: str
    content: str
    msg_type: str
    created_at: str


class ThreadOut(BaseModel):
    platform: str
    peer_key: str
    contact_name: str | None
    contact_id: str | None
    last_message: str
    last_at: str
    message_count: int


# ── 辅助：行转模型 ────────────────────────────────────────────────────────

def _lead(row: Any) -> LeadOut:
    try:
        detail = json.loads(row["result_detail"] or "[]")
        if not isinstance(detail, list):
            detail = []
    except json.JSONDecodeError:
        detail = []
    return LeadOut(
        id=row["id"], phone=row["phone"], source=row["source"],
        source_user=row["source_user"], source_context=row["source_context"],
        status=row["status"], created_at=row["created_at"],
        updated_at=row["updated_at"], result_detail=detail,
    )


def _conv(row: Any) -> ConvOut:
    return ConvOut(
        id=row["id"], platform=row["platform"],
        contact_id=row["contact_id"], contact_name=row["contact_name"],
        peer_key=_peer_key(row["platform"], row["contact_id"], row["contact_name"]),
        direction=row["direction"], content=row["content"],
        msg_type=row["msg_type"], created_at=row["created_at"],
    )


# ══════════════════════════════════════════════════════════════════════════
# 健康检查
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════════════════
# Leads API
# ══════════════════════════════════════════════════════════════════════════

@app.post("/api/leads", response_model=LeadOut)
def create_lead(body: LeadCreate) -> LeadOut:
    phone = "".join(ch for ch in body.phone if ch.isdigit() or ch == "+")
    if len(phone) < 7:
        raise HTTPException(400, "invalid phone")
    today = datetime.now(timezone.utc).date().isoformat()
    with _conn() as c:
        existing = c.execute(
            "SELECT * FROM leads WHERE phone=? AND substr(created_at,1,10)=? ORDER BY id DESC LIMIT 1",
            (phone, today),
        ).fetchone()
        if existing:
            return _lead(existing)
        now = _utc()
        cur = c.execute(
            """INSERT INTO leads (phone, source, source_user, source_context, status, created_at, updated_at, result_detail)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, '[]')""",
            (phone, body.source, body.source_user, body.source_context, now, now),
        )
        c.commit()
        row = c.execute("SELECT * FROM leads WHERE id=?", (cur.lastrowid,)).fetchone()
    assert row
    return _lead(row)


@app.get("/api/leads", response_model=list[LeadOut])
def list_leads(status: str | None = Query(None)) -> list[LeadOut]:
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM leads WHERE status=? ORDER BY id DESC LIMIT 500", (status,)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 500").fetchall()
    return [_lead(r) for r in rows]


@app.get("/api/leads/{lead_id}", response_model=LeadOut)
def get_lead(lead_id: int) -> LeadOut:
    with _conn() as c:
        row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    return _lead(row)


def _wxwork_subprocess(lead_id: int, phone: str) -> None:
    script = Path(__file__).resolve().parents[2] / "wxwork_add_customer.py"
    cwd    = Path(__file__).resolve().parents[2]
    base   = "http://127.0.0.1:8000"

    def _fail(msg: str) -> None:
        with _conn() as c:
            c.execute("UPDATE leads SET status='failed', updated_at=? WHERE id=?", (_utc(), lead_id))
            c.commit()
        _append_event(lead_id, {"step": "trigger", "ok": False, "detail": msg, "ts": _utc()})

    if not script.is_file():
        _fail(f"script not found: {script}")
        return

    cmd = [
        sys.executable, str(script),
        "--phone", phone,
        "--no-launch",
        "--foreground-wait-sec", "120",
        "--platform-url", base,
        "--task-id", str(lead_id),
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), timeout=900,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        ok = proc.returncode == 0
        with _conn() as c:
            c.execute(
                "UPDATE leads SET status=?, updated_at=? WHERE id=?",
                ("done" if ok else "failed", _utc(), lead_id),
            )
            c.commit()
        tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
        _append_event(lead_id, {
            "step": "subprocess_exit", "ok": ok,
            "detail": tail or f"exit={proc.returncode}", "ts": _utc(),
        })
    except subprocess.TimeoutExpired:
        _fail("timeout")
    except OSError as e:
        _fail(str(e))


@app.post("/api/leads/{lead_id}/trigger", status_code=202)
def trigger_lead(lead_id: int) -> dict[str, str]:
    with _conn() as c:
        row = c.execute("SELECT id, phone, status FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    if row["status"] in ("running", "done"):
        raise HTTPException(409, f"status is {row['status']}")
    with _conn() as c:
        c.execute("UPDATE leads SET status='running', updated_at=? WHERE id=?", (_utc(), lead_id))
        c.commit()
    _append_event(lead_id, {"step": "trigger_accepted", "ok": True, "ts": _utc()})
    threading.Thread(
        target=_wxwork_subprocess, args=(lead_id, row["phone"]), daemon=True
    ).start()
    return {"status": "accepted", "lead_id": str(lead_id)}


@app.post("/api/leads/{lead_id}/reset")
def reset_lead(lead_id: int) -> dict[str, str]:
    """将线索重置为 pending，允许重新触发（开发/测试用）。"""
    with _conn() as c:
        row = c.execute("SELECT id FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        c.execute(
            "UPDATE leads SET status='pending', result_detail='[]', updated_at=? WHERE id=?",
            (_utc(), lead_id),
        )
        c.commit()
    return {"status": "reset"}


@app.post("/api/leads/{lead_id}/event")
def post_lead_event(lead_id: int, body: LeadEvent) -> dict[str, str]:
    with _conn() as c:
        row = c.execute("SELECT id FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    ev = body.model_dump()
    ev["ts"] = ev.get("ts") or _utc()
    _append_event(lead_id, ev)
    return {"ok": "true"}


# ══════════════════════════════════════════════════════════════════════════
# Conversations API
# ══════════════════════════════════════════════════════════════════════════

@app.post("/api/conversations", response_model=ConvOut)
def post_conversation(body: ConvIn) -> ConvOut:
    now = _utc()
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO conversations (platform, contact_id, contact_name, direction, content, msg_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (body.platform, body.contact_id, body.contact_name,
             body.direction, body.content, body.msg_type, now),
        )
        c.commit()
        row = c.execute("SELECT * FROM conversations WHERE id=?", (cur.lastrowid,)).fetchone()
    assert row
    return _conv(row)


@app.get("/api/conversations", response_model=list[ConvOut])
def list_conversations(
    platform: str | None = Query(None),
    peer_key: str | None = Query(None),
    limit: int = Query(500, ge=1, le=5000),
) -> list[ConvOut]:
    with _conn() as c:
        if peer_key:
            plat, _, ident = peer_key.partition(":")
            rows = c.execute(
                """SELECT * FROM conversations
                   WHERE platform=? AND (COALESCE(contact_id,'')=? OR COALESCE(contact_name,'')=?)
                   ORDER BY id ASC LIMIT ?""",
                (plat.strip(), ident.strip(), ident.strip(), limit),
            ).fetchall()
        elif platform:
            rows = c.execute(
                "SELECT * FROM conversations WHERE platform=? ORDER BY id ASC LIMIT ?",
                (platform, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            rows = list(reversed(rows))
    return [_conv(r) for r in rows]


@app.get("/api/conversation_threads", response_model=list[ThreadOut])
def conversation_threads() -> list[ThreadOut]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM conversations ORDER BY id DESC LIMIT 20000").fetchall()

    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        pk = _peer_key(r["platform"], r["contact_id"], r["contact_name"])
        if pk not in groups:
            groups[pk] = {
                "platform": r["platform"], "peer_key": pk,
                "contact_name": r["contact_name"], "contact_id": r["contact_id"],
                "last_message": r["content"], "last_at": r["created_at"],
                "message_count": 0,
            }
        groups[pk]["message_count"] += 1

    return [
        ThreadOut(**g)
        for g in sorted(groups.values(), key=lambda x: x["last_at"], reverse=True)[:300]
    ]


# ══════════════════════════════════════════════════════════════════════════
# 抖音归档导入（DouyinCSAgent/exports — 真实 CDP/DOM 抓取）
# ══════════════════════════════════════════════════════════════════════════


def _douyin_export_json_candidates() -> list[Path]:
    if not DOUYIN_EXPORTS_ROOT.is_dir():
        return []
    return sorted(
        [p for p in DOUYIN_EXPORTS_ROOT.glob("*/recent_chats.json") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _parse_dom_export_ts(ts: Any) -> str:
    if ts is None:
        return _utc()
    s = str(ts).strip()
    if not s:
        return _utc()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            n = len(fmt)
            dt = datetime.strptime(s[:n], fmt)
            return datetime(
                dt.year, dt.month, dt.day,
                dt.hour, dt.minute, dt.second if "%S" in fmt else 0,
                tzinfo=timezone.utc,
            ).replace(microsecond=0).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(microsecond=0).isoformat()
    except ValueError:
        return _utc()


class DouyinArchiveImport(BaseModel):
    """folder 为 DouyinCSAgent/exports 下的一级子目录名；留空则取文件最新修改时间的归档。"""

    folder: str | None = None


class RevaiSessionClearIn(BaseModel):
    session_id: str = Field("douyin-ltx", min_length=1, max_length=128)
    base_url: str | None = None


@app.post("/api/douyin/revai/session/clear")
def clear_douyin_revai_session(
    body: RevaiSessionClearIn = Body(default_factory=RevaiSessionClearIn),
) -> dict[str, Any]:
    session_id = body.session_id.strip()
    if not re.fullmatch(r"[\w.-]{1,128}", session_id):
        raise HTTPException(400, "session_id only supports letters, numbers, underscore, dash and dot")

    base_url = (body.base_url or REVAI_LOCAL_BASE_URL).rstrip("/")
    url = f"{base_url}/api/project/carzone/compat/sessions/{session_id}/clear"
    req = urlrequest.Request(url, data=b"", method="POST")
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise HTTPException(e.code, detail or str(e)) from e
    except URLError as e:
        raise HTTPException(502, f"RevAI session clear request failed: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"raw": raw}
    if isinstance(data, dict):
        data.setdefault("session_id", session_id)
        data.setdefault("base_url", base_url)
        return data
    return {"success": True, "session_id": session_id, "base_url": base_url, "data": data}


@app.get("/api/import/douyin-archives")
def list_douyin_archives() -> list[dict[str, Any]]:
    """列出可导入的抖音 recent_chats 一级子目录。"""
    out: list[dict[str, Any]] = []
    for p in _douyin_export_json_candidates():
        out.append(
            {
                "folder": p.parent.name,
                "updated_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return out


@app.post("/api/import/douyin-recent-chats")
def import_douyin_recent_chats(
    body: DouyinArchiveImport = Body(default_factory=DouyinArchiveImport),
) -> dict[str, Any]:
    """将 Douyin Agent 抓取的 recent_chats.json 导入 conversations（platform=douyin）。"""
    candidates = _douyin_export_json_candidates()
    if not candidates:
        raise HTTPException(
            404,
            f"未发现抖音归档，请确认存在: {DOUYIN_EXPORTS_ROOT}{os.sep}<子目录>{os.sep}recent_chats.json",
        )

    root = DOUYIN_EXPORTS_ROOT.resolve()
    chosen: Path
    archive_name: str

    if body.folder and body.folder.strip():
        fname = body.folder.strip()
        if not re.fullmatch(r"[\w.-]+", fname):
            raise HTTPException(400, "invalid folder name")
        chosen_try = (root / fname / "recent_chats.json").resolve()
        try:
            chosen_try.relative_to(root)
        except ValueError as exc:
            raise HTTPException(400, "invalid export path") from exc
        if not chosen_try.is_file():
            raise HTTPException(404, f"未找到 {fname}/recent_chats.json")
        chosen = chosen_try
        archive_name = fname
    else:
        chosen = candidates[0]
        archive_name = chosen.parent.name

    data = _read_json(chosen)
    inserted = 0
    skipped = 0
    conv_count = len(data.get("conversations") or [])
    now = _utc()

    with _conn() as c:
        for conv in data.get("conversations") or []:
            if not isinstance(conv, dict):
                continue
            cid = (
                str(conv.get("conversation_key") or "").strip()
                or str(conv.get("conversation_id") or "").strip()
                or str(conv.get("session_id") or "").strip()
            )
            cname = str(conv.get("title") or "").strip() or cid or "unknown"
            if not cid:
                cid = cname[:128] or "unknown"
            for m in conv.get("structured_messages") or []:
                if not isinstance(m, dict):
                    skipped += 1
                    continue
                txt = str(m.get("text") or "").strip()
                if not txt:
                    skipped += 1
                    continue
                d_raw = str(m.get("direction") or "incoming").lower()
                direction = "out" if d_raw in ("outgoing", "out") else "in"
                mtype = str(m.get("message_type") or "text").strip() or "text"
                created_at = _parse_dom_export_ts(
                    m.get("timestamp") or m.get("first_seen_at") or m.get("last_seen_at"),
                )
                c.execute(
                    """INSERT INTO conversations (platform, contact_id, contact_name, direction, content, msg_type, created_at)
                       VALUES ('douyin', ?, ?, ?, ?, ?, ?)""",
                    (cid, cname, direction, txt, mtype[:32], created_at),
                )
                inserted += 1
        c.commit()

    return {
        "ok": True,
        "archive": archive_name,
        "source_file": str(chosen.relative_to(BASE_DIR.parent)),
        "conversations_seen": conv_count,
        "inserted_messages": inserted,
        "skipped_rows": skipped,
        "imported_at": now,
    }


# ══════════════════════════════════════════════════════════════════════════
# Demo 数据注入（一键填充演示数据，无真实 agent 时测试用）
# ══════════════════════════════════════════════════════════════════════════

@app.post("/api/seed-demo")
def seed_demo() -> dict[str, Any]:
    """一键注入演示数据：2 条线索 + 若干对话消息。"""
    now = _utc()

    # ── 线索 ──────────────────────────────────────────────────────────────
    leads_data = [
        {
            "phone": "13800138000",
            "source": "douyin",
            "source_user": "ltx",
            "source_context": "我想了解你们的产品，可以加一下吗",
        },
        {
            "phone": "13912345678",
            "source": "douyin",
            "source_user": "抖音用户_小红",
            "source_context": "请问有没有优惠活动？留个微信吧",
        },
    ]
    lead_ids: list[int] = []
    with _conn() as c:
        for ld in leads_data:
            today = datetime.now(timezone.utc).date().isoformat()
            existing = c.execute(
                "SELECT id FROM leads WHERE phone=? AND substr(created_at,1,10)=? LIMIT 1",
                (ld["phone"], today),
            ).fetchone()
            if existing:
                lead_ids.append(existing["id"])
                continue
            cur = c.execute(
                """INSERT INTO leads (phone, source, source_user, source_context, status, created_at, updated_at, result_detail)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, '[]')""",
                (ld["phone"], ld["source"], ld["source_user"], ld["source_context"], now, now),
            )
            lead_ids.append(int(cur.lastrowid))
        c.commit()

    # ── 给第一条线索加模拟步骤事件 ────────────────────────────────────────
    if lead_ids:
        steps = [
            ("通讯录", True, None),
            ("新的客户", True, None),
            ("+添加按钮", True, None),
            ("输入手机号", True, None),
            ("搜索结果-添加", True, None),
            ("发送邀请", True, None),
        ]
        for step, ok, detail in steps:
            _append_event(lead_ids[0], {"step": step, "ok": ok, "detail": detail, "ts": _utc()})
        with _conn() as c:
            c.execute(
                "UPDATE leads SET status='done', updated_at=? WHERE id=?",
                (_utc(), lead_ids[0]),
            )
            c.commit()

    # ── 对话消息 ──────────────────────────────────────────────────────────
    douyin_msgs = [
        ("in",  "ltx", "douyin_xiaoming_001", "你好，请问你们的产品多少钱？"),
        ("out", "ltx", "douyin_xiaoming_001", "您好！我们的产品有多个套餐，请问您主要想了解哪方面？"),
        ("in",  "ltx", "douyin_xiaoming_001", "我想了解一下基础版，另外我的手机号是 13800138000"),
        ("out", "ltx", "douyin_xiaoming_001", "好的，已为您记录手机号，稍后会有同事通过企业微信联系您。"),
        ("in",  "抖音用户_小红", "douyin_xiaohong_002", "有没有团购优惠？"),
        ("out", "抖音用户_小红", "douyin_xiaohong_002", "有的！10人以上可享8折，您方便留个联系方式吗？"),
        ("in",  "抖音用户_小红", "douyin_xiaohong_002", "好的，我手机 13912345678"),
        ("out", "抖音用户_小红", "douyin_xiaohong_002", "收到，已记录，会尽快联系您！"),
    ]
    wxwork_msgs = [
        ("in",  "ltx", "wxwork_xiaoming", "你好，我是抖音上联系过的ltx"),
        ("out", "ltx", "wxwork_xiaoming", "您好ltx！欢迎添加企业微信，很高兴认识您。"),
        ("in",  "ltx", "wxwork_xiaoming", "想了解一下你们基础版的具体功能"),
        ("out", "ltx", "wxwork_xiaoming", "基础版包含：AI 自动回复、多会话管理、数据看板，月费 199 元。"),
        ("in",  "ltx", "wxwork_xiaoming", "可以先试用吗？"),
        ("out", "ltx", "wxwork_xiaoming", "可以的！免费试用 7 天，我发给您试用链接。"),
    ]

    with _conn() as c:
        for direction, cname, cid, content in douyin_msgs:
            c.execute(
                """INSERT INTO conversations (platform, contact_id, contact_name, direction, content, msg_type, created_at)
                   VALUES ('douyin', ?, ?, ?, ?, 'text', ?)""",
                (cid, cname, direction, content, _utc()),
            )
        for direction, cname, cid, content in wxwork_msgs:
            c.execute(
                """INSERT INTO conversations (platform, contact_id, contact_name, direction, content, msg_type, created_at)
                   VALUES ('wxwork', ?, ?, ?, ?, 'text', ?)""",
                (cid, cname, direction, content, _utc()),
            )
        c.commit()

    return {"leads_created": len(lead_ids), "leads": lead_ids}


# ══════════════════════════════════════════════════════════════════════════
# Moments API  — 企业朋友圈群发
# ══════════════════════════════════════════════════════════════════════════

class MomentOut(BaseModel):
    id: int
    content: str
    image_path: str | None
    cdp_url: str
    status: str
    created_at: str
    updated_at: str
    log_tail: str


def _moment(row: Any) -> MomentOut:
    return MomentOut(
        id=row["id"], content=row["content"],
        image_path=row["image_path"], cdp_url=row["cdp_url"],
        status=row["status"], created_at=row["created_at"],
        updated_at=row["updated_at"], log_tail=row["log_tail"] or "",
    )


def _normalize_moment_cdp_url(url: str) -> str:
    """与 wxwork_pengyouquan._normalize_cdp_url 对齐：规避 localhost→::1 的连接失败。"""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return "http://127.0.0.1:9222"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().strip().strip("[]")
    if host not in ("localhost", "::1"):
        return raw.rstrip("/")
    hp = "127.0.0.1"
    if parsed.port is not None:
        hp += f":{parsed.port}"
    auth = ""
    if parsed.username is not None:
        u = quote(parsed.username, safe="")
        pw = ":" + quote(parsed.password, safe="") if parsed.password is not None else ""
        auth = u + pw + "@"
    new_netloc = auth + hp
    return urlunparse(
        (
            parsed.scheme,
            new_netloc,
            parsed.path or "",
            parsed.params or "",
            parsed.query or "",
            parsed.fragment or "",
        )
    ).rstrip("/")


def _run_moments_subprocess(moment_id: int, content: str, image_path: str | None, cdp_url: str) -> None:
    script = Path(__file__).resolve().parents[2] / "wxwork_pengyouquan.py"
    cwd    = Path(__file__).resolve().parents[2]

    def _set_status(status: str, log: str = "") -> None:
        with _conn() as c:
            c.execute(
                "UPDATE moments SET status=?, updated_at=?, log_tail=? WHERE id=?",
                (status, _utc(), log[-3000:] if log else "", moment_id),
            )
            c.commit()

    if not script.is_file():
        _set_status("failed", f"script not found: {script}")
        return

    cdp_url = _normalize_moment_cdp_url(cdp_url)
    cmd = [sys.executable, str(script), "--cdp", cdp_url, "--content", content]
    if image_path:
        cmd += ["--image", image_path]

    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), timeout=300,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        ok = proc.returncode == 0
        log = (proc.stdout or "") + (proc.stderr or "")
        _set_status("done" if ok else "failed", log)
    except subprocess.TimeoutExpired:
        _set_status("failed", "timeout after 300s")
    except OSError as e:
        _set_status("failed", str(e))


_IMG_EXT = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})


@app.post("/api/moments", response_model=MomentOut, status_code=202)
def create_moment(
    content: str = Form(...),
    cdp_url: str = Form("http://127.0.0.1:9222"),
    image: UploadFile | None = File(None),
) -> MomentOut:
    text = content.strip()
    if not text:
        raise HTTPException(400, "content is required")
    cdp = _normalize_moment_cdp_url(cdp_url.strip() or "http://127.0.0.1:9222")
    disk_path: str | None = None
    try:
        if image and image.filename and image.filename.strip():
            suf = Path(image.filename.strip()).suffix.lower()
            if not suf or suf not in _IMG_EXT:
                raise HTTPException(400, f"unsupported or missing image extension (allowed: {_IMG_EXT})")
            dest = MOMENT_UPLOADS_DIR / f"{uuid.uuid4().hex}{suf}"
            with dest.open("wb") as out:
                shutil.copyfileobj(image.file, out)
            disk_path = str(dest.resolve())
    finally:
        if image:
            image.file.close()
    now = _utc()
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO moments (content, image_path, cdp_url, status, created_at, updated_at, log_tail)
               VALUES (?, ?, ?, 'running', ?, ?, '')""",
            (text, disk_path, cdp, now, now),
        )
        c.commit()
        row = c.execute("SELECT * FROM moments WHERE id=?", (cur.lastrowid,)).fetchone()
    assert row
    moment = _moment(row)
    threading.Thread(
        target=_run_moments_subprocess,
        args=(moment.id, moment.content, moment.image_path, moment.cdp_url),
        daemon=True,
    ).start()
    return moment


@app.get("/api/moments", response_model=list[MomentOut])
def list_moments() -> list[MomentOut]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM moments ORDER BY id DESC LIMIT 100").fetchall()
    return [_moment(r) for r in rows]


@app.get("/api/moments/{moment_id}", response_model=MomentOut)
def get_moment(moment_id: int) -> MomentOut:
    with _conn() as c:
        row = c.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    return _moment(row)


# ══════════════════════════════════════════════════════════════════════════
# 原有 WXWork exports API（保留兼容）
# ══════════════════════════════════════════════════════════════════════════

def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise HTTPException(404, "export not found") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(500, f"invalid json: {path.name}") from exc


def _export_json_paths() -> list[Path]:
    """All recent_chats.json under exports/, any depth — newest file mtime first."""
    if not EXPORTS_DIR.exists():
        return []
    return sorted(
        [p for p in EXPORTS_DIR.rglob("recent_chats.json") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _resolve_recent_chats_json(export: str | None) -> Path:
    """Resolve to a concrete recent_chats.json path under EXPORTS_DIR (no path traversal)."""
    root = EXPORTS_DIR.resolve()
    if not export:
        paths = _export_json_paths()
        if not paths:
            raise HTTPException(404, "no exports found")
        return paths[0]
    if ".." in export or export.startswith(("/", "\\")) or "\x00" in export:
        raise HTTPException(400, "invalid export")
    cand = (EXPORTS_DIR / export.replace("/", os.sep) / "recent_chats.json").resolve()
    try:
        cand.relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, "invalid export path") from exc
    if not cand.is_file():
        raise HTTPException(404, "export not found")
    return cand


def _looks_like_time_marker(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(星期[一二三四五六日天]\d{1,2}:\d{2}|"
            r"\d{4}年\d{1,2}月\d{1,2}日\d{1,2}:\d{2}|"
            r"昨天\d{1,2}:\d{2}|今天\d{1,2}:\d{2}|\d{1,2}:\d{2})",
            text.strip(),
        )
    )


def _message_items(messages: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    bubble_index = 0
    for index, raw in enumerate(messages):
        text = str(raw).strip()
        if not text:
            continue
        if _looks_like_time_marker(text):
            result.append({"id": index, "kind": "time", "text": text})
            continue
        direction = "outgoing" if bubble_index % 5 in {2, 3} else "incoming"
        bubble_index += 1
        result.append({"id": index, "kind": "message", "direction": direction, "text": text})
    return result


def _screenshot_url(raw_path: str) -> str:
    normalized = str(raw_path).replace("\\", "/")
    if normalized.startswith("exports/"):
        normalized = normalized[len("exports/"):]
    return f"/exports/{normalized}"


def _normalize_message(raw: dict[str, Any], index: int) -> dict[str, Any]:
    kind = str(raw.get("kind") or "message")
    return {
        "id": raw.get("id") or index,
        "kind": kind,
        "message_type": raw.get("message_type") or ("time_marker" if kind == "time" else "text"),
        "direction": raw.get("direction") or ("system" if kind == "time" else "unknown"),
        "sender": raw.get("sender"),
        "timestamp": raw.get("timestamp") or raw.get("timestamp_visible"),
        "timestamp_visible": raw.get("timestamp_visible"),
        "text": str(raw.get("text") or "").strip(),
        "source_screenshot": raw.get("source_screenshot"),
        "hover_screenshot": raw.get("hover_screenshot"),
        "screen_point": raw.get("screen_point"),
        "ocr_center": raw.get("ocr_center"),
    }


def _normalize_export(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    conversations = data.get("conversations", [])
    normalized = []
    for item in conversations:
        structured = item.get("structured_messages") or []
        if structured:
            messages = [_normalize_message(x, i) for i, x in enumerate(structured) if isinstance(x, dict)]
        else:
            messages = _message_items([str(x) for x in item.get("messages", [])])
        screenshots = [str(x) for x in item.get("screenshots", [])]
        normalized.append(
            {
                "id": item.get("index"),
                "index": item.get("index"),
                "title": item.get("title") or "未命名会话",
                "list_preview": item.get("list_preview") or "",
                "message_count": len(messages),
                "messages": messages,
                "screenshots": screenshots,
                "screenshot_urls": [_screenshot_url(x) for x in screenshots],
            }
        )
    latest_mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    return {
        "platform": "wxwork",
        "export_name": path.parent.name,
        "exported_at": data.get("exported_at") or latest_mtime,
        "updated_at": latest_mtime,
        "conversation_count": len(normalized),
        "conversations": normalized,
    }


@app.get("/api/wxwork/exports")
def wxwork_exports() -> list[dict[str, Any]]:
    result = []
    for path in _export_json_paths():
        try:
            data = _read_json(path)
        except HTTPException:
            continue
        rel = path.parent.relative_to(EXPORTS_DIR.resolve()).as_posix()
        result.append(
            {
                "name": rel,
                "exported_at": data.get("exported_at"),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "conversation_count": len(data.get("conversations", [])),
            }
        )
    return result


@app.get("/api/wxwork/chats")
def wxwork_chats(export: str | None = Query(default=None)) -> dict[str, Any]:
    path = _resolve_recent_chats_json(export)
    return _normalize_export(path)
