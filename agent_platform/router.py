from __future__ import annotations

import json
import subprocess  # noqa: S404 -- intentional
import sys
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from agent_platform.config import public_base_url, wxwork_cwd, wxwork_script_path
from agent_platform.db import append_lead_event, get_conn, init_db

router = APIRouter(tags=["platform"])


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class LeadCreate(BaseModel):
    phone: str = Field(..., description="手机号")
    source: str | None = "douyin"
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


class ConversationIn(BaseModel):
    platform: str = Field(..., pattern="^(douyin|wxwork)$")
    contact_id: str | None = None
    contact_name: str | None = None
    direction: str = Field(..., pattern="^(in|out)$")
    content: str
    msg_type: str = "text"


class ConversationOut(BaseModel):
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


def _peer_key(platform: str, contact_id: str | None, contact_name: str | None) -> str:
    cid = (contact_id or "").strip()
    cname = (contact_name or "").strip()
    if cid:
        return f"{platform}:{cid}"
    if cname:
        return f"{platform}:{cname}"
    return f"{platform}:unknown"


def _row_to_lead(row: Any) -> LeadOut:
    rd = row["result_detail"] or "[]"
    try:
        detail = json.loads(rd)
        if not isinstance(detail, list):
            detail = []
    except json.JSONDecodeError:
        detail = []
    return LeadOut(
        id=row["id"],
        phone=row["phone"],
        source=row["source"],
        source_user=row["source_user"],
        source_context=row["source_context"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        result_detail=detail,
    )


@router.post("/api/leads", response_model=LeadOut)
def create_lead(body: LeadCreate) -> LeadOut:
    phone = "".join(ch for ch in body.phone if ch.isdigit() or ch == "+")
    if len(phone) < 7:
        raise HTTPException(400, "invalid phone")

    today = datetime.now(timezone.utc).date().isoformat()
    init_db()

    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT * FROM leads
            WHERE phone = ? AND substr(created_at, 1, 10) = ?
            ORDER BY id DESC LIMIT 1
            """,
            (phone, today),
        ).fetchone()
        now = _utc_now_iso()
        if existing:
            return _row_to_lead(existing)

        cur = conn.execute(
            """
            INSERT INTO leads (phone, source, source_user, source_context, status, created_at, updated_at, result_detail)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, '[]')
            """,
            (phone, body.source, body.source_user, body.source_context, now, now),
        )
        conn.commit()
        lid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lid,)).fetchone()
    assert row is not None
    return _row_to_lead(row)


@router.get("/api/leads", response_model=list[LeadOut])
def list_leads(status: str | None = Query(None)) -> list[LeadOut]:
    init_db()
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM leads WHERE status = ? ORDER BY id DESC LIMIT 500",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 500").fetchall()
    return [_row_to_lead(r) for r in rows]


@router.get("/api/leads/{lead_id}", response_model=LeadOut)
def get_lead(lead_id: int) -> LeadOut:
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "lead not found")
    return _row_to_lead(row)


def _run_wxwork_add_customer(lead_id: int, phone: str) -> None:
    script = wxwork_script_path()
    cwd = wxwork_cwd()
    base = public_base_url()

    def _fail(msg: str) -> None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE leads SET status = 'failed', updated_at = ? WHERE id = ?",
                (_utc_now_iso(), lead_id),
            )
            conn.commit()
        append_lead_event(
            lead_id,
            {"step": "trigger", "ok": False, "detail": msg, "ts": _utc_now_iso()},
        )

    if not script.is_file():
        _fail(f"script not found: {script}")
        return

    cmd = [
        sys.executable,
        str(script),
        "--phone",
        phone,
        "--no-launch",
        "--foreground-wait-sec",
        "120",
        "--platform-url",
        base,
        "--task-id",
        str(lead_id),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            timeout=900,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        ok = proc.returncode == 0
        with get_conn() as conn:
            conn.execute(
                "UPDATE leads SET status = ?, updated_at = ? WHERE id = ?",
                ("done" if ok else "failed", _utc_now_iso(), lead_id),
            )
            conn.commit()
        tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
        append_lead_event(
            lead_id,
            {
                "step": "subprocess_exit",
                "ok": ok,
                "detail": tail or f"exit={proc.returncode}",
                "ts": _utc_now_iso(),
            },
        )
    except subprocess.TimeoutExpired:
        _fail("subprocess timeout")
    except OSError as e:
        _fail(str(e))


@router.post("/api/leads/{lead_id}/trigger", status_code=202)
def trigger_lead(lead_id: int) -> dict[str, str]:
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT id, phone, status FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "lead not found")
    if row["status"] in ("running", "done"):
        raise HTTPException(409, f"lead status is {row['status']}, cannot trigger")

    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET status = 'running', updated_at = ? WHERE id = ?",
            (_utc_now_iso(), lead_id),
        )
        conn.commit()
    append_lead_event(lead_id, {"step": "trigger_accepted", "ok": True, "ts": _utc_now_iso()})

    phone = row["phone"]
    threading.Thread(target=_run_wxwork_add_customer, args=(lead_id, phone), daemon=True).start()
    return {"status": "accepted", "lead_id": str(lead_id)}


@router.post("/api/leads/{lead_id}/event")
def post_lead_event(lead_id: int, body: LeadEvent) -> dict[str, str]:
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "lead not found")
    ev = body.model_dump()
    ev["ts"] = ev.get("ts") or _utc_now_iso()
    append_lead_event(lead_id, ev)
    return {"ok": "true"}


@router.post("/api/conversations", response_model=ConversationOut)
def post_conversation(body: ConversationIn) -> ConversationOut:
    init_db()
    peer_key = _peer_key(body.platform, body.contact_id, body.contact_name)
    now = _utc_now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO conversations (platform, contact_id, contact_name, direction, content, msg_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.platform,
                body.contact_id,
                body.contact_name,
                body.direction,
                body.content,
                body.msg_type,
                now,
            ),
        )
        conn.commit()
        mid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (mid,)).fetchone()
    assert row is not None
    return ConversationOut(
        id=row["id"],
        platform=row["platform"],
        contact_id=row["contact_id"],
        contact_name=row["contact_name"],
        peer_key=peer_key,
        direction=row["direction"],
        content=row["content"],
        msg_type=row["msg_type"],
        created_at=row["created_at"],
    )


@router.get("/api/conversations", response_model=list[ConversationOut])
def list_conversations(
    platform: str | None = Query(None),
    peer_key: str | None = Query(None, description="platform:id 或 platform:name"),
    limit: int = Query(500, ge=1, le=5000),
) -> list[ConversationOut]:
    init_db()
    with get_conn() as conn:
        if peer_key:
            plat, sep, rest = peer_key.partition(":")
            if not sep:
                raise HTTPException(400, "peer_key must be platform:identifier")
            p = plat.strip()
            ident = rest.strip()
            rows = conn.execute(
                """
                SELECT * FROM conversations
                WHERE platform = ? AND (
                  COALESCE(contact_id, '') = ? OR COALESCE(contact_name, '') = ?
                )
                ORDER BY id ASC
                LIMIT ?
                """,
                (p, ident, ident, limit),
            ).fetchall()
        elif platform:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE platform = ? ORDER BY id DESC LIMIT ?",
                (platform, limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            rows = list(reversed(rows))

    return [
        ConversationOut(
            id=r["id"],
            platform=r["platform"],
            contact_id=r["contact_id"],
            contact_name=r["contact_name"],
            peer_key=_peer_key(r["platform"], r["contact_id"], r["contact_name"]),
            direction=r["direction"],
            content=r["content"],
            msg_type=r["msg_type"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.get("/api/conversation_threads", response_model=list[ThreadOut])
def conversation_threads(limit_threads: int = Query(200, ge=1, le=1000)) -> list[ThreadOut]:
    init_db()
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM conversations ORDER BY id DESC LIMIT 10000").fetchall()

    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        pk = _peer_key(r["platform"], r["contact_id"], r["contact_name"])
        if pk not in groups:
            groups[pk] = {
                "platform": r["platform"],
                "peer_key": pk,
                "contact_name": r["contact_name"],
                "contact_id": r["contact_id"],
                "last_message": r["content"],
                "last_at": r["created_at"],
                "message_count": 0,
            }
        g = groups[pk]
        g["message_count"] += 1

    threads = sorted(groups.values(), key=lambda x: x["last_at"], reverse=True)[:limit_threads]
    return [ThreadOut(**t) for t in threads]
