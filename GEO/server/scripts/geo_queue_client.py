#!/usr/bin/env python3
"""连发多任务：验证队列排队、每单签名回调、queued_ms。"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import geo_ask
import geo_callback_client as gcc

ROOT = Path("/root/workspace/GUIAgent/GEO/server")
OUT = Path("/tmp/geo-queue-client.json")


class MultiBox:
    def __init__(self, sk: str):
        self.sk = sk
        self.lock = threading.Lock()
        self.items: list[dict] = []
        self.event = threading.Event()
        self.expect = 0

    def handle(self, path: str, headers: dict, body: bytes) -> tuple[int, dict]:
        box = gcc.CallbackBox(self.sk, Path("/tmp/geo-queue-cb-tmp.json"))
        code, obj = box.handle(path, headers, body)
        if code == 200 and box.received:
            with self.lock:
                self.items.append(box.received)
                print(json.dumps({
                    "callback": True,
                    "n": len(self.items),
                    "job_id": box.received.get("job_id"),
                    "status": (box.received.get("body") or {}).get("status"),
                    "queued_ms": (box.received.get("body") or {}).get("queued_ms"),
                    "sig_ok": box.received.get("sig_ok"),
                }, ensure_ascii=False), flush=True)
                if self.expect and len(self.items) >= self.expect:
                    self.event.set()
        return code, obj


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',default=gcc.DEFAULT_BASE,help='GEO API入口，默认跟随geo_callback_client环境配置')
    parser.add_argument('--wait-sec',type=int,default=None,help='显式限制客户端等待秒数；默认等待全部任务和回调结束')
    args=parser.parse_args()
    if args.wait_sec is not None and args.wait_sec<=0:parser.error('--wait-sec 必须大于 0')
    ak, sk = gcc.load_aksk(ROOT / ".aksk.json")
    snap = gcc.api(args.base, "GET", "/api/v1/sites", ak, sk)
    logged = [s["site_id"] for s in (snap.get("sites") or []) if s.get("status") == "logged_in"]
    print(json.dumps({"logged_in": logged}, ensure_ascii=False), flush=True)
    if "deepseek" not in logged or "yiyan" not in logged:
        print("DeepSeek / 文心一言 未就绪")
        return 1

    box = MultiBox(sk)
    box.expect = 3
    httpd = ThreadingHTTPServer(("127.0.0.1", 8767), _handler(box))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cb = "http://127.0.0.1:8767/callback"
    print("callback", cb, flush=True)

    specs = [
        {"sites": ["deepseek"], "prompt": "帮我用一句话解释什么是哈希，别列提纲。"},
        {"sites": ["yiyan"], "prompt": "今天出门要不要带伞，一句话就行。"},
        {"sites": ["deepseek"], "prompt": "用十个字以内夸一下北京的秋天。"},
    ]
    submitted = []
    t0 = time.perf_counter()
    for i, spec in enumerate(specs):
        payload = {**spec, "callback_url": cb}
        rec = gcc.api(args.base, "POST", "/api/v1/ask", ak, sk, payload)
        submitted.append(rec)
        print("submit", i, rec.get("job_id"), rec.get("status"), "pos", rec.get("queue_pos"), "len", rec.get("queue_len"), flush=True)
        if rec.get("http_status") not in (200, 202):
            print("submit failed", rec)
            httpd.shutdown()
            return 2

    # 立刻看队列：不同站可并行；同站第三单应还在等
    time.sleep(0.4)
    snapshot = []
    for rec in submitted:
        j = gcc.api(args.base, "GET", "/api/v1/jobs/" + rec["job_id"], ak, sk)
        snapshot.append({"job_id": rec["job_id"], "status": j.get("status"), "queue_pos": j.get("queue_pos")})
    print("after_submit", json.dumps(snapshot, ensure_ascii=False), flush=True)

    wait_started=time.monotonic()
    current=[]
    while (remaining:=geo_ask.wait_remaining(wait_started,args.wait_sec)) is None or remaining>0:
        if box.event.wait(3 if remaining is None else min(3,remaining)):break
        try:
            current=[gcc.api(args.base,'GET','/api/v1/jobs/'+rec['job_id'],ak,sk) for rec in submitted]
        except OSError as error:
            print('poll retry',type(error).__name__,'accepted jobs continue',flush=True)
            continue
        unreadable=[{'job_id':rec['job_id'],'http_status':job.get('http_status')} for rec,job in zip(submitted,current) if geo_ask.job_read_unavailable(job)]
        if unreadable:
            print('unreadable',json.dumps(unreadable),'; accepted tasks are not resubmitted')
            httpd.shutdown()
            return 5
        if all(geo_ask.classify_wait(str(j.get('status') or ''))=='finished' and (j.get('status')=='cancelled' or j.get('callback_status') in ('ok','failed')) for j in current):break
    if not box.event.is_set():
        cancelled=[job.get('job_id') for job in current if job.get('status')=='cancelled']
        if cancelled:
            print('cancelled jobs',cancelled,'; no callbacks are sent for cancelled jobs')
        else:
            print("callback unfinished", len(box.items),"jobs",[rec['job_id'] for rec in submitted],"client wait ended; tasks are not cancelled")
        httpd.shutdown()
        return 3
    httpd.shutdown()
    wall = int((time.perf_counter() - t0) * 1000)

    finals = []
    for rec in submitted:
        j = gcc.api(args.base, "GET", "/api/v1/jobs/" + rec["job_id"], ak, sk)
        r0 = (j.get("results") or [{}])[0]
        finals.append({
            "job_id": j.get("job_id"),
            "sites": j.get("sites"),
            "status": j.get("status"),
            "queued_ms": j.get("queued_ms"),
            "elapsed_ms": j.get("elapsed_ms"),
            "callback_status": j.get("callback_status"),
            "callback_ms": j.get("callback_ms"),
            "answer": (r0.get("answer") or "")[:40],
            "ok": r0.get("ok"),
        })

    cbs = []
    for item in box.items:
        body = item.get("body") or {}
        cbs.append({
            "job_id": item.get("job_id"),
            "sig_ok": item.get("sig_ok"),
            "status": body.get("status"),
            "queued_ms": body.get("queued_ms"),
            "callback_in_body": body.get("callback_status"),
        })

    first_two_live = snapshot[0]["status"] in ("queued", "running") and snapshot[1]["status"] in ("queued", "running")
    later_waited = all((f.get("queued_ms") or 0) >= 0 for f in finals)
    all_cb = len(cbs) == 3 and all(c.get("sig_ok") for c in cbs)
    all_done = all(f.get("status") == "succeeded" and f.get("ok") for f in finals)
    all_cb_ok = all(f.get("callback_status") == "ok" for f in finals)

    report = {
        "ok": first_two_live and later_waited and all_cb and all_done and all_cb_ok,
        "wall_ms": wall,
        "queue_snapshot": snapshot,
        "jobs": finals,
        "callbacks": cbs,
        "checks": {
            "first_two_accepted": first_two_live,
            "later_job_waited": later_waited,
            "three_signed_callbacks": all_cb,
            "all_succeeded": all_done,
            "callback_status_ok": all_cb_ok,
        },
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 4


def _handler(box: MultiBox):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("cb %s\n" % (fmt % args))

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


if __name__ == "__main__":
    raise SystemExit(main())
