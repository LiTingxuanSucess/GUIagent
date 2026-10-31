# -*- coding: utf-8 -*-
"""六站登录控制台：HTTPS 对外，截图点选 + 短信回填。"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from login_console.manager import DEFAULT_SITE_ORDER, manager

STATIC_DIR = Path(__file__).resolve().parent / "static"
TOKEN = os.environ.get("LOGIN_CONSOLE_TOKEN", "").strip()


def _check(token_q: str | None, token_h: str | None) -> None:
    if not TOKEN:
        return
    got = (token_h or token_q or "").strip()
    if got != TOKEN:
        raise HTTPException(status_code=401, detail="需要 token")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = threading.Event()
    boot = threading.Thread(target=manager.start, name="cn-ai-browser", daemon=True)
    boot.start()
    watch = threading.Thread(target=manager.watch, args=(stop,), name="cn-ai-watch", daemon=True)
    watch.start()
    yield
    stop.set()
    manager.stop()


app = FastAPI(title="CN AI Login Console", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class PhoneIn(BaseModel):
    phone: str = Field(min_length=11, max_length=20)


class CodeIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)


class ClickIn(BaseModel):
    nx: float
    ny: float


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/sessions")
def sessions(
    token: str | None = Query(default=None),
    x_token: str | None = Header(default=None, alias="X-Token"),
):
    _check(token, x_token)
    return manager.snapshot()


@app.get("/api/sessions/{site_id}/screenshot")
def screenshot(
    site_id: str,
    token: str | None = Query(default=None),
    x_token: str | None = Header(default=None, alias="X-Token"),
):
    _check(token, x_token)
    if site_id not in DEFAULT_SITE_ORDER:
        raise HTTPException(404, "unknown site")
    try:
        data = manager.screenshot(site_id)
    except Exception as exc:
        raise HTTPException(503, f"screenshot failed: {exc}") from exc
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/phone")
def set_phone(
    body: PhoneIn,
    token: str | None = Query(default=None),
    x_token: str | None = Header(default=None, alias="X-Token"),
):
    _check(token, x_token)
    try:
        manager.set_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "phone": manager.phone}


@app.post("/api/sessions/{site_id}/send-code")
def send_code(
    site_id: str,
    token: str | None = Query(default=None),
    x_token: str | None = Header(default=None, alias="X-Token"),
):
    _check(token, x_token)
    if site_id not in DEFAULT_SITE_ORDER:
        raise HTTPException(404, "unknown site")
    return manager.send_code(site_id, force=True)


@app.post("/api/sessions/{site_id}/submit-code")
def submit_code(
    site_id: str,
    body: CodeIn,
    token: str | None = Query(default=None),
    x_token: str | None = Header(default=None, alias="X-Token"),
):
    _check(token, x_token)
    if site_id not in DEFAULT_SITE_ORDER:
        raise HTTPException(404, "unknown site")
    try:
        return manager.submit_code(site_id, body.code)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/sessions/{site_id}/click")
def click(
    site_id: str,
    body: ClickIn,
    token: str | None = Query(default=None),
    x_token: str | None = Header(default=None, alias="X-Token"),
):
    _check(token, x_token)
    if site_id not in DEFAULT_SITE_ORDER:
        raise HTTPException(404, "unknown site")
    return manager.click_norm(site_id, body.nx, body.ny)


@app.post("/api/sessions/{site_id}/refresh")
def refresh(
    site_id: str,
    token: str | None = Query(default=None),
    x_token: str | None = Header(default=None, alias="X-Token"),
):
    _check(token, x_token)
    if site_id not in DEFAULT_SITE_ORDER:
        raise HTTPException(404, "unknown site")
    return manager.refresh(site_id).as_dict()

