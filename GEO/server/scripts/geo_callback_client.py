#!/usr/bin/env python3
"""模拟业务客户端：签名调用 GEO API，本地接回调并验签。"""

from __future__ import annotations

import argparse
import hmac
import json
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import geo_ask

ROOT = Path("/root/workspace/GUIAgent/GEO/server")
DEFAULT_BASE = "https://127.0.0.1:8443"


def load_aksk(path: Path) -> tuple[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = data.get("keys") or []
    if not keys:
        raise SystemExit("没有可用 AK/SK")
    k = next((x for x in keys if x.get("enabled", True)), keys[0])
    return k["ak"], k["sk"]


def sign(sk: str, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    return geo_ask.sign(sk, method, path, ts, nonce, body)


def api(base: str, method: str, path: str, ak: str, sk: str, payload: dict | None = None) -> dict:
    body = b""
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=body if method != "GET" else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Geo-AK": ak,
            "X-Geo-Timestamp": ts,
            "X-Geo-Nonce": nonce,
            "X-Geo-Signature": sign(sk, method, path, ts, nonce, body),
        },
    )
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return {"http_status": resp.status, **json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return {"http_status": e.code, **json.loads(raw)}
        except Exception:
            return {"http_status": e.code, "detail": raw}


class CallbackBox:
    def __init__(self, sk: str, out: Path):
        self.sk = sk
        self.out = out
        self.event = threading.Event()
        self.received: dict | None = None
        self.error: str = ""

    def handle(self, path: str, headers: dict, body: bytes) -> tuple[int, dict]:
        ak = headers.get("X-Geo-AK", "")
        ts = headers.get("X-Geo-Timestamp", "")
        nonce = headers.get("X-Geo-Nonce", "")
        sig = (headers.get("X-Geo-Signature") or "").lower()
        job_id = headers.get("X-Geo-Job-Id", "")
        want = sign(self.sk, "POST", path, ts, nonce, body)
        if not hmac.compare_digest(sig, want):
            self.error = "bad signature"
            return 401, {"ok": False, "error": "bad signature"}
        payload = json.loads(body.decode("utf-8"))
        rec = {
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "job_id": job_id or payload.get("job_id"),
            "sig_ok": True,
            "ak": ak,
            "path": path,
            "body": payload,
        }
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        self.received = rec
        self.event.set()
        return 200, {"ok": True, "job_id": rec["job_id"]}


def make_handler(box: CallbackBox):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("callback %s\n" % (fmt % args))

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            headers = {k: self.headers.get(k, "") for k in (
                "X-Geo-AK", "X-Geo-Timestamp", "X-Geo-Nonce", "X-Geo-Signature", "X-Geo-Job-Id",
            )}
            code, obj = box.handle(self.path, headers, body)
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def wait_for_callback(box, poll, wait_sec=None, job_id=''):
    started=time.monotonic()
    last_poll=""
    while (remaining:=geo_ask.wait_remaining(started,wait_sec)) is None or remaining>0:
        if box.event.wait(3 if remaining is None else min(3,remaining)):
            return
        try:
            job=poll()
        except OSError as error:
            print('poll retry',type(error).__name__,'task continues',flush=True)
            continue
        if geo_ask.job_read_unavailable(job):
            print(f"unreadable job_id={job_id} http_status={job['http_status']} task_status_unconfirmed; no resubmission",flush=True)
            return job
        status=job.get('status')
        if status!=last_poll:
            print('poll',status,flush=True)
            last_poll=status
        if geo_ask.classify_wait(str(status or ''))=='finished' and (status=='cancelled' or job.get('callback_status') in ('ok','failed')):
            return


def main() -> int:
    ap = argparse.ArgumentParser(description="GEO 模拟客户端：提交异步任务并等待签名回调")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--keys", default=str(ROOT / ".aksk.json"))
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--site", action="append", default=[])
    ap.add_argument("--prompt", default=geo_ask.DEFAULT_PROMPT)
    ap.add_argument("--wait-sec", type=int, default=None, help="显式限制客户端等待秒数；默认等任务和回调结束")
    ap.add_argument("--out", default="/tmp/geo-callback-client.json")
    args = ap.parse_args()
    if args.wait_sec is not None and args.wait_sec <= 0:
        ap.error("--wait-sec 必须大于 0")

    ak, sk = load_aksk(Path(args.keys))
    snap = api(args.base, "GET", "/api/v1/sites", ak, sk)
    if snap.get("http_status") != 200:
        print(json.dumps({"error": "sites api failed", "resp": snap}, ensure_ascii=False, indent=2))
        return 1
    logged = [s["site_id"] for s in (snap.get("sites") or []) if s.get("status") == "logged_in"]
    targets = args.site or logged
    print(json.dumps({"sites_api": {"ready": snap.get("ready"), "logged_in": logged, "use": targets}}, ensure_ascii=False), flush=True)
    if not targets:
        print("没有可用 logged_in 站点")
        return 1

    box = CallbackBox(sk, Path(args.out))
    httpd = ThreadingHTTPServer((args.listen, args.port), make_handler(box))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    callback_url = f"http://{args.listen}:{args.port}/callback"
    print(f"callback listening {callback_url}", flush=True)

    t0 = time.perf_counter()
    submitted = api(args.base, "POST", "/api/v1/ask", ak, sk, {
        "sites": targets,
        "prompt": args.prompt,
        "callback_url": callback_url,
    })
    print("submitted", json.dumps({k: submitted.get(k) for k in (
        "http_status", "job_id", "status", "poll_url", "callback_url",
    )}, ensure_ascii=False), flush=True)
    job_id = submitted.get("job_id")
    if submitted.get("http_status") not in (200, 202) or not job_id:
        httpd.shutdown()
        return 2

    read_error=wait_for_callback(box,lambda:api(args.base,"GET",f"/api/v1/jobs/{job_id}",ak,sk),args.wait_sec,job_id)

    httpd.shutdown()
    wall = int((time.perf_counter() - t0) * 1000)
    polled = read_error if read_error else api(args.base, "GET", f"/api/v1/jobs/{job_id}", ak, sk)
    cb = box.received
    report = {
        "ok": bool(cb and cb.get("sig_ok") and polled.get("callback_status") == "ok"),
        "wall_ms": wall,
        "job_id": job_id,
        "read_http_status":polled.get('http_status'),
        "client_read_error":geo_ask.job_read_unavailable(polled),
        "poll_status": polled.get("status"),
        "callback_status": polled.get("callback_status"),
        "callback_ms": polled.get("callback_ms"),
        "callback_error": polled.get("callback_error") or box.error,
        "sig_ok": bool(cb and cb.get("sig_ok")),
        "elapsed_ms": polled.get("elapsed_ms"),
        "queued_ms": polled.get("queued_ms"),
        "results": [
            {
                "site_id": r.get("site_id"),
                "ok": r.get("ok"),
                "answer": (r.get("answer") or "")[:80],
                "error": r.get("error"),
                "elapsed_ms": r.get("elapsed_ms"),
                "steps": r.get("steps"),
            }
            for r in (polled.get("results") or [])
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    Path(args.out).with_suffix(".report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 5 if report['client_read_error'] else (0 if report["ok"] else 3)


if __name__ == "__main__":
    raise SystemExit(main())
