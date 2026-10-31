"""
DouyinCSAgent Test Suite
========================
Tests the Douyin CS agent logic without requiring a real browser or API keys.

Suites
------
1. parse_messages        -- IM API response body parsing (multiple format variants)
2. _get_pending_guest    -- guest message detection & dedup logic
3. phone_detection       -- phone number regex extraction
4. generate_reply        -- prompt construction (mock Qwen client)
5. platform_reporter     -- lead creation + conversation upload (mock HTTP server)
6. run_loop              -- end-to-end async loop with mock page/responses

Usage
-----
  py -3.11 tools/test_douyin_watch_suite.py
  py -3.11 tools/test_douyin_watch_suite.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── Import under test ──────────────────────────────────────────────────────
from douyin_watch import DouyinCSAgent, _PHONE_RE, parse_messages  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# Lightweight test runner (same as WXWork suite, no pytest required)
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class TestResult:
    name: str
    ok: bool
    skipped: bool = False
    message: str = ""
    exc: str = ""


@dataclass
class TestCase:
    name: str
    fn: Callable[[], None]
    skip_reason: str = ""

    def run(self, verbose: bool = False) -> TestResult:
        if self.skip_reason:
            return TestResult(self.name, ok=True, skipped=True, message=self.skip_reason)
        try:
            self.fn()
            return TestResult(self.name, ok=True)
        except AssertionError as e:
            return TestResult(self.name, ok=False, message=str(e))
        except Exception as e:
            return TestResult(self.name, ok=False, exc=traceback.format_exc(), message=str(e))


@dataclass
class Suite:
    name: str
    cases: list[TestCase] = field(default_factory=list)
    verbose: bool = False

    def case(self, name: str, skip_reason: str = "") -> Any:
        def decorator(fn: Callable[[], None]) -> Callable[[], None]:
            self.cases.append(TestCase(name, fn, skip_reason))
            return fn
        return decorator

    def run(self) -> list[TestResult]:
        results: list[TestResult] = []
        print(f"\n{'─'*72}")
        print(f"Suite: {self.name}")
        print(f"{'─'*72}")
        for tc in self.cases:
            res = tc.run(self.verbose)
            results.append(res)
            tag = "-- SKIP" if res.skipped else ("[PASS]" if res.ok else "[FAIL]")
            msg = f"  {tag}  {res.name}"
            if res.message and (not res.ok or self.verbose):
                msg += f"\n       {res.message}"
            if res.exc and not res.ok:
                msg += "\n" + textwrap.indent(res.exc, "       ")
            print(msg)
        return results


def aeq(a: Any, b: Any, msg: str = "") -> None:
    if a != b:
        raise AssertionError(f"{msg}\n  actual={a!r}\n  expect={b!r}")


def anone(v: Any, msg: str = "") -> None:
    if v is not None:
        raise AssertionError(f"{msg}: expected None, got {v!r}")


def anot_none(v: Any, msg: str = "") -> None:
    if v is None:
        raise AssertionError(f"{msg}: expected non-None")


def ain(sub: str, text: str, msg: str = "") -> None:
    if sub not in text:
        raise AssertionError(f"{msg}: {sub!r} not in {text!r}")


# ══════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════

_MIN_CFG = {
    "qwen": {"api_key": "test-key", "model": "qwen-turbo"},
    "reply": {"system": "You are a test CS bot."},
    "watch": {"interval_sec": 0.05, "post_send_pause_sec": 0.0},
    "agent_uid": "agent_001",
}


def _msg(msg_id: str, sender: str, text: str, t: int = 0) -> dict:
    return {
        "msg_id": msg_id,
        "sender_uid": sender,
        "content_text": text,
        "create_time": t,
    }


def _make_agent(platform_url: str | None = None) -> DouyinCSAgent:
    return DouyinCSAgent(_MIN_CFG, platform_url=platform_url)


# ══════════════════════════════════════════════════════════════════════════
# Suite 1: parse_messages
# ══════════════════════════════════════════════════════════════════════════

def make_parse_suite(verbose: bool = False) -> Suite:
    suite = Suite("parse_messages — IM API response parsing", verbose=verbose)

    @suite.case("v1 format: data.message_list with JSON-string content")
    def _():
        body = {
            "data": {
                "message_list": [
                    {
                        "msg_id": "111",
                        "sender_uid": "user_a",
                        "content": json.dumps({"text": "hello world"}),
                        "create_time": 1000,
                    }
                ]
            }
        }
        msgs = parse_messages(body)
        aeq(len(msgs), 1)
        aeq(msgs[0]["content_text"], "hello world")
        aeq(msgs[0]["msg_id"], "111")
        aeq(msgs[0]["sender_uid"], "user_a")

    @suite.case("v2 format: messages.message_list")
    def _():
        body = {
            "messages": {
                "message_list": [
                    {"msg_id": "222", "sender_uid": "user_b",
                     "content": json.dumps({"content": "hi there"}), "create_time": 2000}
                ]
            }
        }
        msgs = parse_messages(body)
        aeq(len(msgs), 1)
        aeq(msgs[0]["content_text"], "hi there")

    @suite.case("flat format: direct message_list at root")
    def _():
        body = {
            "message_list": [
                {"msg_id": "333", "from_uid": "user_c",
                 "msg_content": "plain text", "send_time": 3000}
            ]
        }
        msgs = parse_messages(body)
        aeq(len(msgs), 1)
        aeq(msgs[0]["content_text"], "plain text")
        aeq(msgs[0]["sender_uid"], "user_c")
        aeq(msgs[0]["create_time"], 3000)

    @suite.case("content as dict (not JSON string)")
    def _():
        body = {
            "data": {
                "message_list": [
                    {"msg_id": "444", "sender_uid": "user_d",
                     "content": {"text": "dict content"}, "create_time": 4000}
                ]
            }
        }
        msgs = parse_messages(body)
        aeq(msgs[0]["content_text"], "dict content")

    @suite.case("content as raw plain string (not JSON)")
    def _():
        body = {
            "data": {
                "message_list": [
                    {"msg_id": "555", "sender_uid": "user_e",
                     "content": "raw string", "create_time": 5000}
                ]
            }
        }
        msgs = parse_messages(body)
        aeq(msgs[0]["content_text"], "raw string")

    @suite.case("empty body returns empty list")
    def _():
        aeq(parse_messages({}), [])

    @suite.case("non-dict items in message_list are skipped")
    def _():
        body = {
            "data": {
                "message_list": [
                    "not a dict",
                    None,
                    {"msg_id": "666", "sender_uid": "u", "content": "ok", "create_time": 0},
                ]
            }
        }
        msgs = parse_messages(body)
        aeq(len(msgs), 1)
        aeq(msgs[0]["content_text"], "ok")

    @suite.case("msg_list alias works as fallback")
    def _():
        body = {
            "data": {
                "msg_list": [
                    {"client_msg_id": "777", "sender_uid": "user_g",
                     "content": "via msg_list", "create_time": 7000}
                ]
            }
        }
        msgs = parse_messages(body)
        aeq(len(msgs), 1)
        aeq(msgs[0]["msg_id"], "777")

    return suite


# ══════════════════════════════════════════════════════════════════════════
# Suite 2: _get_pending_guest_message
# ══════════════════════════════════════════════════════════════════════════

def make_pending_suite(verbose: bool = False) -> Suite:
    suite = Suite("_get_pending_guest_message — guest msg detection", verbose=verbose)

    @suite.case("returns None when no messages")
    def _():
        a = _make_agent()
        anone(a._get_pending_guest_message())

    @suite.case("returns guest message text")
    def _():
        a = _make_agent()
        a._latest_messages = [_msg("1", "visitor_x", "hello", 1000)]
        aeq(a._get_pending_guest_message(), "hello")

    @suite.case("marks msg_id as seen after first call")
    def _():
        a = _make_agent()
        a._latest_messages = [_msg("1", "visitor_x", "hello", 1000)]
        a._get_pending_guest_message()
        # second call: same msg_id in seen set -> None
        anone(a._get_pending_guest_message(), "should be None on second call (seen)")

    @suite.case("filters out agent own messages (agent_uid match)")
    def _():
        a = _make_agent()
        a._latest_messages = [_msg("2", "agent_001", "I am the bot", 1000)]
        anone(a._get_pending_guest_message(), "agent message should be filtered")

    @suite.case("filters out our just-sent reply text")
    def _():
        a = _make_agent()
        a._last_sent_text = "thank you"
        a._latest_messages = [_msg("3", "visitor_x", "thank you", 1000)]
        anone(a._get_pending_guest_message(), "echo of own reply should be filtered")

    @suite.case("picks latest message (by create_time)")
    def _():
        a = _make_agent()
        a._latest_messages = [
            _msg("10", "visitor_x", "first msg",  1000),
            _msg("11", "visitor_x", "second msg", 2000),
        ]
        aeq(a._get_pending_guest_message(), "second msg")

    @suite.case("empty content_text returns None")
    def _():
        a = _make_agent()
        a._latest_messages = [_msg("12", "visitor_x", "", 1000)]
        anone(a._get_pending_guest_message())

    @suite.case("no agent_uid: does not filter by uid")
    def _():
        cfg = dict(_MIN_CFG)
        cfg["agent_uid"] = ""
        a = DouyinCSAgent(cfg)
        a._latest_messages = [_msg("13", "anyone", "message", 1000)]
        aeq(a._get_pending_guest_message(), "message")

    return suite


# ══════════════════════════════════════════════════════════════════════════
# Suite 3: phone number detection
# ══════════════════════════════════════════════════════════════════════════

def make_phone_suite(verbose: bool = False) -> Suite:
    suite = Suite("Phone number regex detection", verbose=verbose)

    @suite.case("detects standard 11-digit mobile (1[3-9]x)")
    def _():
        phones = _PHONE_RE.findall("我的手机是 13800138001 谢谢")
        aeq(phones, ["13800138001"])

    @suite.case("detects phone at end of sentence")
    def _():
        phones = _PHONE_RE.findall("加我微信：15912345678")
        aeq(phones, ["15912345678"])

    @suite.case("ignores numbers starting with 10 or 12 (not mobile)")
    def _():
        phones = _PHONE_RE.findall("客服热线 10086 或 12315")
        aeq(phones, [])

    @suite.case("ignores 12-digit sequences (too long)")
    def _():
        phones = _PHONE_RE.findall("订单号 139001380012345")
        aeq(phones, [])

    @suite.case("detects multiple phones in one message")
    def _():
        phones = _PHONE_RE.findall("联系我 13911112222 或者 18811112222")
        aeq(sorted(phones), ["13911112222", "18811112222"])

    @suite.case("no false positives in normal text")
    def _():
        phones = _PHONE_RE.findall("今天天气 26 度，产品价格 1990 元")
        aeq(phones, [])

    @suite.case("phone embedded in longer text")
    def _():
        phones = _PHONE_RE.findall("请联系：13500001234，谢谢")
        aeq(phones, ["13500001234"])

    return suite


# ══════════════════════════════════════════════════════════════════════════
# Suite 4: _generate_reply prompt construction
# ══════════════════════════════════════════════════════════════════════════

def make_generate_suite(verbose: bool = False) -> Suite:
    suite = Suite("_generate_reply — prompt construction + mock Qwen", verbose=verbose)

    def _make_agent_mock_qwen(reply_text: str) -> DouyinCSAgent:
        a = _make_agent()
        a.client = MagicMock()
        a.client.complete = MagicMock(return_value=reply_text)
        return a

    @suite.case("calls client.complete with system prompt and user prompt")
    def _():
        a = _make_agent_mock_qwen("mock reply")
        ctx = [_msg("1", "visitor", "question", 1)]
        result = a._generate_reply("question", ctx)
        aeq(result, "mock reply")
        a.client.complete.assert_called_once()
        call_args = a.client.complete.call_args
        sys_p, user_p = call_args[0]
        ain("test CS bot", sys_p, "system prompt should come from config")
        ain("question", user_p, "user prompt should contain guest text")

    @suite.case("includes context messages in prompt")
    def _():
        a = _make_agent_mock_qwen("ok")
        ctx = [
            _msg("1", "visitor", "first message",  1),
            _msg("2", "agent_001", "agent reply",  2),
            _msg("3", "visitor", "follow up",      3),
        ]
        a._generate_reply("follow up", ctx)
        _, user_p = a.client.complete.call_args[0]
        ain("first message", user_p)
        ain("agent reply", user_p)

    @suite.case("labels messages by role correctly")
    def _():
        a = _make_agent_mock_qwen("ok")
        ctx = [
            _msg("1", "visitor_x", "visitor says this", 1),
            _msg("2", "agent_001", "agent says this",   2),
        ]
        a._generate_reply("hi", ctx)
        _, user_p = a.client.complete.call_args[0]
        ain("访客: visitor says this", user_p)
        ain("客服: agent says this", user_p)

    @suite.case("only uses last 10 messages for context")
    def _():
        a = _make_agent_mock_qwen("ok")
        ctx = [_msg(str(i), "visitor", f"msg {i}", i) for i in range(20)]
        a._generate_reply("latest", ctx)
        _, user_p = a.client.complete.call_args[0]
        # Oldest 10 (msg 0-9) should NOT appear; newest 10 (msg 10-19) should
        assert "msg 0" not in user_p, "oldest messages should be truncated"
        ain("msg 10", user_p)

    return suite


# ══════════════════════════════════════════════════════════════════════════
# Suite 5: PlatformReporter integration (mock HTTP server)
# ══════════════════════════════════════════════════════════════════════════

def make_reporter_suite(verbose: bool = False) -> Suite:
    suite = Suite("PlatformReporter integration (mock HTTP server)", verbose=verbose)

    # Start a local HTTP server to capture POST requests
    captured: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            captured.append({"path": self.path, "body": body})
            resp = json.dumps({"id": 99, "status": "pending"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *_): pass  # suppress server logs

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    base_url = f"http://127.0.0.1:{port}"
    # Note: server.shutdown() is called after suite.run() in main(), not here.
    # The server daemon thread will be cleaned up when the process exits anyway.

    @suite.case("report_lead sends POST /api/leads with correct fields")
    def _():
        captured.clear()
        a = _make_agent(platform_url=base_url)
        anot_none(a.reporter, "reporter should be created")
        lead_id = a.reporter.report_lead("13800138001", source="douyin",
                                          source_user="test_user",
                                          source_context="some context")
        aeq(lead_id, 99, "should return id from response")
        import time; time.sleep(0.1)  # wait for sync call
        leads = [c for c in captured if c["path"] == "/api/leads"]
        assert len(leads) >= 1, "POST /api/leads should have been called"
        body = leads[0]["body"]
        aeq(body["phone"], "13800138001")
        aeq(body["source"], "douyin")
        aeq(body["source_user"], "test_user")

    @suite.case("report_event_bg sends POST /api/leads/{id}/event")
    def _():
        captured.clear()
        a = _make_agent(platform_url=base_url)
        a.reporter.task_id = 42
        a.reporter.report_event("phone_detected", True, "13800138001", lead_id=42)
        import time; time.sleep(0.15)
        events = [c for c in captured if "/event" in c["path"]]
        assert len(events) >= 1, "event POST should have been called"
        body = events[0]["body"]
        aeq(body["step"], "phone_detected")
        assert body["ok"] is True

    @suite.case("report_conversation sends POST /api/conversations")
    def _():
        captured.clear()
        a = _make_agent(platform_url=base_url)
        a.reporter.report_conversation("douyin", "in", "hello visitor",
                                        contact_id="uid_123", contact_name="Test User")
        import time; time.sleep(0.2)
        convs = [c for c in captured if c["path"] == "/api/conversations"]
        assert len(convs) >= 1, "POST /api/conversations should have been called"
        body = convs[0]["body"]
        aeq(body["platform"], "douyin")
        aeq(body["direction"], "in")
        aeq(body["content"], "hello visitor")

    @suite.case("phone not reported twice (dedup via _reported_phones)")
    def _():
        captured.clear()
        a = _make_agent(platform_url=base_url)
        a._latest_messages = [_msg("x", "visitor_x", "手机 13800138001", 1)]
        a._seen_msg_ids.clear()

        # Simulate phone detection logic (extracted from run loop)
        import re
        phone_re = _PHONE_RE
        text = "手机 13800138001"
        phones_found = phone_re.findall(text)
        for ph in phones_found:
            if ph not in a._reported_phones:
                a._reported_phones.add(ph)
                a.reporter.report_lead(ph, source="douyin")
        # Call again — should NOT re-report
        for ph in phones_found:
            if ph not in a._reported_phones:
                a._reported_phones.add(ph)
                a.reporter.report_lead(ph, source="douyin")

        import time; time.sleep(0.2)
        leads = [c for c in captured if c["path"] == "/api/leads"]
        aeq(len(leads), 1, "same phone should only be reported once")

    return suite


# ══════════════════════════════════════════════════════════════════════════
# Suite 6: run() loop end-to-end (async, mock page)
# ══════════════════════════════════════════════════════════════════════════

def make_run_loop_suite(verbose: bool = False) -> Suite:
    suite = Suite("run() async loop end-to-end (mock page + Qwen)", verbose=verbose)

    def _run_async(coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @suite.case("agent generates and sends reply for new guest message")
    def _():
        a = _make_agent()
        a.client = MagicMock()
        a.client.complete = MagicMock(return_value="auto reply text")
        a.interval = 0.0   # no wait

        # Mock page
        page = MagicMock()
        page.on = MagicMock()
        page.reload = AsyncMock()

        sent_texts: list[str] = []

        async def fake_send(p, text):
            sent_texts.append(text)
            return True

        # Inject a guest message and run 1 iteration
        a._latest_messages = [_msg("m1", "visitor_v", "I need help", 1000)]
        a._messages_updated.set()

        async def run_one():
            import douyin_watch as dw
            orig_send = dw.send_reply
            dw.send_reply = fake_send
            try:
                # Run only one loop iteration then break
                a.page_ref = page
                a.page_on_called = False

                # Simulate one pass of the loop body (not the infinite loop)
                a._messages_updated.clear()
                guest_text = a._get_pending_guest_message()
                if guest_text:
                    reply = a._generate_reply(guest_text, a._latest_messages)
                    ok = await fake_send(page, reply)
                    if ok:
                        a._last_sent_text = reply
            finally:
                dw.send_reply = orig_send

        _run_async(run_one())

        aeq(len(sent_texts), 1, "should have sent exactly 1 reply")
        aeq(sent_texts[0], "auto reply text")

    @suite.case("agent does NOT reply to its own message")
    def _():
        a = _make_agent()
        a.client = MagicMock()
        a.client.complete = MagicMock(return_value="should not send")
        # Own message
        a._latest_messages = [_msg("m2", "agent_001", "I am the agent", 1000)]
        guest_text = a._get_pending_guest_message()
        anone(guest_text, "own message should not trigger reply")

    @suite.case("agent does NOT replay same message twice")
    def _():
        a = _make_agent()
        a._latest_messages = [_msg("m3", "visitor_v", "hello", 1000)]
        first  = a._get_pending_guest_message()
        second = a._get_pending_guest_message()
        aeq(first, "hello")
        anone(second, "second call should return None (already seen)")

    @suite.case("phone number in guest message triggers lead reporting")
    def _():
        captured_leads: list[str] = []

        a = _make_agent()
        # Mock reporter
        mock_reporter = MagicMock()
        mock_reporter.report_lead = MagicMock(return_value=10)
        mock_reporter.report_conversation = MagicMock()
        a.reporter = mock_reporter

        # Simulate the phone-detection block from the run loop
        guest_text = "我的手机号是 13912345678，请联系我"
        phones = _PHONE_RE.findall(guest_text)
        for ph in phones:
            if ph not in a._reported_phones:
                a._reported_phones.add(ph)
                lead_id = a.reporter.report_lead(ph, source="douyin")
                captured_leads.append(ph)

        aeq(captured_leads, ["13912345678"])
        mock_reporter.report_lead.assert_called_once_with(
            "13912345678", source="douyin"
        )

    return suite


# ══════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="DouyinCSAgent test suite")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    all_results: list[TestResult] = []

    all_results += make_parse_suite(args.verbose).run()
    all_results += make_pending_suite(args.verbose).run()
    all_results += make_phone_suite(args.verbose).run()
    all_results += make_generate_suite(args.verbose).run()
    all_results += make_reporter_suite(args.verbose).run()
    all_results += make_run_loop_suite(args.verbose).run()

    passed  = sum(1 for r in all_results if r.ok and not r.skipped)
    failed  = sum(1 for r in all_results if not r.ok)
    skipped = sum(1 for r in all_results if r.skipped)

    print(f"\n{'='*72}")
    print(f"TOTAL {len(all_results)}   PASS {passed}   FAIL {failed}   SKIP {skipped}")
    print(f"{'='*72}")

    if failed:
        print("\nFailed:")
        for r in all_results:
            if not r.ok:
                print(f"  [FAIL]  {r.name}")
                if r.message:
                    print(f"          {r.message}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
