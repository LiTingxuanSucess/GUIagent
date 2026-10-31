"""Local Carzone compatibility endpoint backed by car-agent's pure LLM baseline.

The server intentionally does not contain business intent rules. It only:
1. Parses the structured Douyin prompt produced by ``douyin_current_chat_reply.py``.
2. Passes the full recent conversation and marked new messages to car-agent's
   pure LLM baseline.
3. Returns the model's structured JSON and final reply.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINT = "/api/project/carzone/compat/chat/respond"
PROMPT_LINE_RE = re.compile(r"^\[(?P<timestamp>[^\]]*)\]\s+(?P<speaker>.*?)\s+text:\s*(?P<text>.*?)(?P<marker>\s+<-- new)?$")


def _default_car_agent_root() -> Path:
    return Path(__file__).resolve().parents[2] / "car-agent"


def _prepare_llm_env() -> None:
    baseline_key_names = (
        "DOUYIN_BASELINE_LLM_API_KEY",
        "DOUYIN_REPLY_LLM_API_KEY",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ARK_API_KEY",
        "VOLCENGINE_API_KEY",
        "DOUBAO_API_KEY",
    )
    if any(os.getenv(name) for name in baseline_key_names):
        return
    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    if not dashscope_key:
        return
    os.environ["DOUYIN_BASELINE_LLM_API_KEY"] = dashscope_key
    os.environ.setdefault("DOUYIN_BASELINE_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    os.environ.setdefault("DOUYIN_BASELINE_LLM_MODEL", os.getenv("DASHSCOPE_MODEL") or os.getenv("QWEN_MODEL") or "qwen-plus")


def _load_baseline(car_agent_root: Path):
    _prepare_llm_env()
    if car_agent_root.is_dir():
        sys.path.insert(0, str(car_agent_root))
    try:
        from backend.douyin_llm_baseline import douyin_pure_llm_baseline

        return douyin_pure_llm_baseline
    except Exception as exc:
        return _UnavailableBaseline(exc)


class _UnavailableBaseline:
    def __init__(self, load_error: Exception) -> None:
        self.load_error = load_error
        self.available = False

    async def compose(self, conversation: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"car-agent baseline import failed: {self.load_error}")


def _infer_channel(session_id: str, prompt: str, explicit: str = "") -> str:
    raw = f"{explicit}\n{session_id}\n{prompt}".lower()
    if any(x in raw for x in ("wxwork", "wework", "企业微信", "企微")):
        return "wxwork"
    return "douyin"


def _parse_prompt(prompt: str, session_id: str, *, channel: str = "") -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for line in (prompt or "").splitlines():
        match = PROMPT_LINE_RE.match(line.strip())
        if not match:
            continue
        text = match.group("text").strip()
        if not text:
            continue
        messages.append(
            {
                "timestamp": match.group("timestamp").strip(),
                "speaker": match.group("speaker").strip(),
                "text": text,
                "is_new": bool(match.group("marker")),
            }
        )

    if not messages and prompt.strip():
        messages.append({"timestamp": "", "speaker": "customer", "text": prompt.strip(), "is_new": True})

    latest_new_messages = [item["text"] for item in messages if item.get("is_new")]
    if not latest_new_messages and messages:
        messages[-1]["is_new"] = True
        latest_new_messages = [messages[-1]["text"]]

    return {
        "session_id": session_id,
        "channel": _infer_channel(session_id, prompt, channel),
        "messages": messages[-12:],
        "latest_new_messages": latest_new_messages[-3:],
    }


def _json_response(handler: BaseHTTPRequestHandler, status: int, data: dict[str, Any]) -> None:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _round_ms(started_at: float, ended_at: float | None = None) -> float:
    return round(((ended_at or time.perf_counter()) - started_at) * 1000, 1)


def make_handler(car_agent_root: Path, endpoint: str):
    baseline = _load_baseline(car_agent_root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalCarzoneCompat/0.2"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

        def do_GET(self) -> None:
            if self.path.rstrip("/") in {"", "/health"}:
                _json_response(self, 200, {"ok": True, "endpoint": endpoint, "engine": "pure_llm_baseline", "available": baseline.available})
                return
            _json_response(self, 404, {"error": "not_found"})

        def do_POST(self) -> None:
            total_started = time.perf_counter()
            if self.path.split("?", 1)[0] != endpoint:
                _json_response(self, 404, {"error": "not_found", "endpoint": endpoint})
                return

            read_started = time.perf_counter()
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception as exc:
                _json_response(self, 400, {"error": f"invalid_json: {exc}"})
                return
            request_parse_ms = _round_ms(read_started)

            session_id = str(payload.get("session_id") or "")
            prompt_parse_started = time.perf_counter()
            conversation = _parse_prompt(
                str(payload.get("message") or ""),
                session_id,
                channel=str(payload.get("channel") or payload.get("platform") or ""),
            )
            prompt_parse_ms = _round_ms(prompt_parse_started)
            try:
                baseline_started = time.perf_counter()
                result = asyncio.run(baseline.compose(conversation))
            except Exception as exc:
                _json_response(self, 502, {"error": f"baseline_failed: {exc}", "engine": "pure_llm_baseline"})
                return
            baseline_ms = _round_ms(baseline_started)
            baseline_profile = result.get("_profile") if isinstance(result, dict) else {}
            profile = {
                "engine": "local_carzone_compat",
                "request_parse_ms": request_parse_ms,
                "prompt_parse_ms": prompt_parse_ms,
                "baseline_call_ms": baseline_ms,
                "total_ms": _round_ms(total_started),
                "request_bytes": len(body),
                "conversation_messages": len(conversation.get("messages") or []),
                "latest_new_count": len(conversation.get("latest_new_messages") or []),
                "baseline": baseline_profile or {},
            }
            sys.stderr.write(
                "profile session=%s total_ms=%s baseline_ms=%s llm_ms=%s\n"
                % (
                    session_id,
                    profile["total_ms"],
                    baseline_ms,
                    (baseline_profile or {}).get("llm_api_ms", ""),
                )
            )

            _json_response(
                self,
                200,
                {
                    "reply": result["reply"],
                    "baseline": result,
                    "profiling": profile,
                    "session_id": session_id,
                    "local_compat": True,
                    "engine": "pure_llm_baseline",
                },
            )

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local Carzone compatibility endpoint for Douyin E2E tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--car-agent-root", type=Path, default=_default_car_agent_root())
    args = parser.parse_args()

    handler = make_handler(args.car_agent_root.resolve(), args.endpoint)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"local Carzone pure LLM baseline listening on http://{args.host}:{args.port}{args.endpoint}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
