#!/usr/bin/env python3
"""Local callback receiver: verify GEO AK/SK and dump the job JSON."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path("/root/workspace/GUIAgent/GEO/server")


def load_sk(path: Path, ak: str) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    for k in data.get("keys") or []:
        if k.get("ak") == ak and k.get("enabled", True):
            return k["sk"]
    raise SystemExit(f"AK 不在密钥文件里: {ak}")


class Handler(BaseHTTPRequestHandler):
    sink_path: Path
    keys_path: Path

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        ak = self.headers.get("X-Geo-AK", "")
        ts = self.headers.get("X-Geo-Timestamp", "")
        nonce = self.headers.get("X-Geo-Nonce", "")
        sig = (self.headers.get("X-Geo-Signature") or "").lower()
        job_id = self.headers.get("X-Geo-Job-Id", "")
        try:
            sk = load_sk(self.keys_path, ak)
        except SystemExit as e:
            self._reply(401, {"ok": False, "error": str(e)})
            return
        body_hash = hashlib.sha256(body).hexdigest()
        canon = f"POST\n{self.path}\n{ts}\n{nonce}\n{body_hash}"
        want = hmac.new(sk.encode(), canon.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            self._reply(401, {"ok": False, "error": "bad signature"})
            return
        rec = {
            "job_id": job_id,
            "path": self.path,
            "ak": ak,
            "body": json.loads(body.decode("utf-8")),
        }
        self.sink_path.parent.mkdir(parents=True, exist_ok=True)
        with self.sink_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(json.dumps({"received": True, "job_id": job_id, "status": rec["body"].get("status")}, ensure_ascii=False), flush=True)
        self._reply(200, {"ok": True, "job_id": job_id})

    def _reply(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out", default="/tmp/geo-callback.jsonl")
    ap.add_argument("--keys", default=str(ROOT / ".aksk.json"))
    args = ap.parse_args()
    Handler.sink_path = Path(args.out)
    Handler.keys_path = Path(args.keys)
    if Handler.sink_path.exists():
        Handler.sink_path.unlink()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"callback sink http://{args.host}:{args.port}/callback -> {Handler.sink_path}", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
