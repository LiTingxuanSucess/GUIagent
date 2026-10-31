#!/usr/bin/env python3
"""Signed GEO client: submit async ask job, then poll (and optional callback)."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import unquote, urlsplit
import uuid
from pathlib import Path

ROOT = Path("/root/workspace/GUIAgent/GEO/server")
DEFAULT_BASE = "https://127.0.0.1:8443"
# 探活用正常问句，不要「只回复一个英文单词」这类指令，容易被风控当成脚本。
DEFAULT_PROMPT = "帮我用一句话解释什么是哈希，别列提纲。"


def load_aksk(path: Path) -> tuple[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = data.get("keys") or []
    if not keys:
        raise SystemExit("没有可用 AK/SK，先启动 geo-server 生成 .aksk.json")
    k = next((x for x in keys if x.get("enabled", True)), keys[0])
    return k["ak"], k["sk"]


def sign(sk: str, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    # Match Go URL.Path: query parameters are sent, but not signed.
    canonical_path = unquote(urlsplit(path).path)
    canon = f"{method.upper()}\n{canonical_path}\n{ts}\n{nonce}\n{body_hash}"
    return hmac.new(sk.encode(), canon.encode(), hashlib.sha256).hexdigest()


def call(base: str, method: str, path: str, ak: str, sk: str, payload: dict | None = None, *, request_timeout: float = 30) -> dict:
    body = b""
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    sig = sign(sk, method, path, ts, nonce, body)
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=body if method != "GET" else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Geo-AK": ak,
            "X-Geo-Timestamp": ts,
            "X-Geo-Nonce": nonce,
            "X-Geo-Signature": sig,
        },
    )
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=request_timeout) as resp:
            return {"http_status": resp.status, **json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return {"http_status": e.code, **json.loads(raw)}
        except Exception:
            return {"http_status": e.code, "detail": raw}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--keys", default=str(ROOT / ".aksk.json"))
    ap.add_argument("--sites-only", action="store_true")
    ap.add_argument("--site", action="append", default=[])
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--callback-url", default="")
    ap.add_argument("--poll-sec", type=int, default=None, help="显式限制客户端等待秒数；默认持续轮询至任务终态")
    args = ap.parse_args()
    if args.poll_sec is not None and args.poll_sec <= 0:
        ap.error("--poll-sec 必须大于 0")
    ak, sk = load_aksk(Path(args.keys))
    snap = call(args.base, "GET", "/api/v1/sites", ak, sk)
    print(json.dumps({"sites": snap}, ensure_ascii=False, indent=2))
    if args.sites_only:
        return 0
    ready = [st["site_id"] for st in (snap.get("sites") or []) if st.get("status") == "logged_in"]
    targets = args.site or ready
    if not targets:
        print("没有 logged_in 站点可测")
        return 1
    payload = {"sites": targets, "prompt": args.prompt}
    if args.callback_url:
        payload["callback_url"] = args.callback_url
    submitted = call(args.base, "POST", "/api/v1/ask", ak, sk, payload)
    print("submitted", json.dumps(submitted, ensure_ascii=False, indent=2), flush=True)
    job_id = submitted.get("job_id")
    if not job_id:
        return 2
    return poll_job(args.base, ak, sk, job_id, args.poll_sec)


def classify_wait(status: str) -> str:
    """finished only for a terminal job. Queue and run waits stay unfinished."""
    if status in ("succeeded", "partial", "failed", "cancelled"):
        return "finished"
    if status == "queued":
        return "queue_timeout"
    return "run_timeout"


def wait_remaining(started: float, timeout_sec: float | None) -> float | None:
    """Only a caller's explicit limit stops waiting; server ETA may move."""
    return None if timeout_sec is None else max(0.0, started + timeout_sec - time.monotonic())


def job_read_unavailable(job: dict) -> bool:
    return job.get('http_status') in (401,403,404)


def poll_job(base: str, ak: str, sk: str, job_id: str, poll_sec: int | None = None) -> int:
    started = time.monotonic()
    last_report = started
    status = "unknown"
    job: dict = {}
    while (remaining := wait_remaining(started, poll_sec)) is None or remaining > 0:
        try:
            job = call(base, "GET", "/api/v1/jobs/" + job_id, ak, sk, request_timeout=30 if remaining is None else min(30,remaining))
        except OSError as error:
            print(f"poll retry job_id={job_id} network_error={type(error).__name__}; task continues",flush=True)
            remaining=wait_remaining(started,poll_sec)
            if remaining is None or remaining>0:time.sleep(5 if remaining is None else min(5,remaining))
            continue
        if job_read_unavailable(job):
            print(f"unreadable job_id={job_id} http_status={job['http_status']} task_status_unconfirmed; no resubmission",flush=True)
            return 5
        status = str(job.get("status") or "")
        now = time.monotonic()
        if now-last_report >= 30:
            print(
                f"progress job_id={job_id} status={status} waited_s={int(now - started)} unfinished",
                flush=True,
            )
            last_report = now
        if classify_wait(status) == "finished":
            print(json.dumps(job, ensure_ascii=False, indent=2))
            return 0 if status in ("succeeded", "partial") else 3
        remaining=wait_remaining(started,poll_sec)
        if remaining is None or remaining>0:time.sleep(5 if remaining is None else min(5,remaining))
    reason = classify_wait(status)
    print(
        f"unfinished job_id={job_id} status={status} waited_s={int(time.monotonic() - started)} reason={reason} client_wait_limit_only",
        flush=True,
    )
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
