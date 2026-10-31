"""
test_on_fixtures.py — 基于录制的 fixture 文件离线跑回归测试
============================================================
不依赖 CDP / 浏览器 / Playwright，只读 fixtures/ 目录里的 JSON。

用法：
  先录制一次：  py -3.11 tools/capture_fixtures.py
  然后随时跑：  py -3.11 tools/test_on_fixtures.py
  指定目录：    py -3.11 tools/test_on_fixtures.py --fixtures tools/fixtures

测试内容：
  1. page_info          - URL 格式、input_exists 字段存在
  2. xhr_samples        - parse_messages() 对真实 XHR body 的解析
  3. js_extract         - EXTRACT_CHAT_JS 执行结果结构合法性
  4. dom_snapshot       - 关键 DOM 检查结果合法性
  5. parse_messages     - 边界情况（空 body、各种 v1/v2/flat 格式）
  6. phone regex        - _PHONE_RE 对 fixture 消息中的手机号检测
  7. _get_pending       - 用 fixture 消息注入 agent 状态，验证检测逻辑
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 轻量测试框架 ───────────────────────────────────────────────────────
_pass = _fail = 0

def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f"  ({detail})"
        print(msg)

def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ── 导入被测函数（无 Playwright 依赖） ────────────────────────────────
from douyin_watch import parse_messages, _PHONE_RE, EXTRACT_CHAT_JS  # noqa: E402


def _make_agent(msgs: list[dict], agent_uid: str = "") -> Any:
    """构造最小化 DouyinCSAgent 实例，注入消息列表，不需要 Playwright。"""
    import asyncio
    from unittest.mock import MagicMock

    # 动态 import，避免 Playwright 初始化
    from douyin_watch import DouyinCSAgent
    cfg = {"qwen": {"api_key": "fake"}}
    agent = DouyinCSAgent.__new__(DouyinCSAgent)
    # 手动初始化用到的属性
    agent.agent_uid = agent_uid
    agent._seen_msg_ids = set()
    agent._last_sent_text = ""
    agent._pending_reply_flag = False
    agent._latest_messages = msgs
    agent._messages_updated = asyncio.Event()
    agent.reporter = None
    agent._reported_phones = set()
    agent._contact_lead = {}
    return agent


# ── 测试逻辑 ──────────────────────────────────────────────────────────

def test_page_info(fix: dict) -> None:
    section("page_info.json")
    info = fix.get("page_info")
    if info is None:
        check("page_info 文件存在", False, "未找到 page_info.json，请先运行 capture_fixtures.py")
        return

    check("url 字段存在", "url" in info)
    check("url 包含 http", info.get("url", "").startswith("http"))
    check("input_placeholder 字段存在", "input_placeholder" in info)
    check("input_exists 字段存在", "input_exists" in info)
    check("captured_at 字段存在", "captured_at" in info)
    # input_exists 不强制为 True（取决于当时是否打开了会话页面）
    print(f"       input_exists={info.get('input_exists')}  url={info.get('url')[:60]!r}")


def test_xhr_samples(fix: dict) -> None:
    section("xhr_samples.json + parse_messages()")
    samples = fix.get("xhr_samples")
    if samples is None:
        check("xhr_samples 文件存在", False, "未找到 xhr_samples.json，请先运行 capture_fixtures.py")
        return

    check("xhr_samples 是列表", isinstance(samples, list))

    if not samples:
        print("       [INFO] 录制时未拦截到任何 IM API 响应（0 条），跳过 parse 断言")
        return

    print(f"       共 {len(samples)} 条 XHR 样本")
    for i, s in enumerate(samples):
        url_short = s.get("url", "")[:60]
        body = s.get("body", {})
        msgs = parse_messages(body)
        check(
            f"  样本#{i+1} parse_messages 返回列表",
            isinstance(msgs, list),
            url_short,
        )
        if msgs:
            m0 = msgs[0]
            check(
                f"  样本#{i+1} 首条消息含 msg_id/sender_uid/content_text",
                all(k in m0 for k in ("msg_id", "sender_uid", "content_text")),
            )
            check(
                f"  样本#{i+1} content_text 是字符串",
                isinstance(m0["content_text"], str),
            )
            print(f"       [{i+1}] {len(msgs)} msgs, sample: {m0['content_text'][:40]!r}")


def test_js_extract(fix: dict) -> None:
    section("js_extract.json (EXTRACT_CHAT_JS 结果结构)")
    js = fix.get("js_extract")
    if js is None:
        check("js_extract 文件存在", False, "未找到 js_extract.json")
        return

    check("结果是 dict", isinstance(js, dict))
    check("包含 ok 字段", "ok" in js)

    if js.get("ok"):
        msgs = js.get("msgs", [])
        check("ok=True 时 msgs 是列表", isinstance(msgs, list))
        if msgs:
            check("msgs[0] 含 text 和 x", all(k in msgs[0] for k in ("text", "x")))
            print(f"       msgs 数量={len(msgs)}, sample text={msgs[0].get('text','')[:40]!r}")
    else:
        reason = js.get("reason", "?")
        print(f"       [INFO] ok=False  reason={reason!r}  "
              "(可能当时没有打开会话页，属正常)")
        check("ok=False 时包含 reason", "reason" in js)


def test_dom_snapshot(fix: dict) -> None:
    section("dom_snapshot.json")
    snap = fix.get("dom_snapshot")
    if snap is None:
        check("dom_snapshot 文件存在", False, "未找到 dom_snapshot.json")
        return

    check("包含 page_title_text", "page_title_text" in snap)
    check("page_title_text 是字符串", isinstance(snap.get("page_title_text"), str))
    check("包含 input_count_by_placeholder", "input_count_by_placeholder" in snap)
    count = snap.get("input_count_by_placeholder", -1)
    # 合理值：0（未开会话）或 1（开着会话）
    check(
        "input_count 是整数",
        isinstance(count, int),
        f"actual={count}",
    )
    print(f"       title={snap.get('page_title_text','')!r}  input_count={count}")


def test_parse_messages_unit() -> None:
    section("parse_messages() 单元测试（内置边界用例，不依赖 fixture）")

    # 空 body
    check("空 body 返回空列表", parse_messages({}) == [])

    # v1 格式
    v1 = {"data": {"message_list": [
        {"msg_id": "1", "sender_uid": "u1", "content": json.dumps({"text": "hello"}), "create_time": 100},
    ]}}
    r = parse_messages(v1)
    check("v1 格式 - 解析出1条", len(r) == 1)
    check("v1 格式 - content_text 正确", r[0]["content_text"] == "hello")
    check("v1 格式 - msg_id 正确", r[0]["msg_id"] == "1")

    # v2 格式
    v2 = {"messages": {"message_list": [
        {"msg_id": "2", "sender_uid": "u2", "content": {"text": "world"}, "create_time": 200},
    ]}}
    r2 = parse_messages(v2)
    check("v2 格式 - 解析出1条", len(r2) == 1)
    check("v2 格式 - content_text 正确", r2[0]["content_text"] == "world")

    # flat 格式（message_list 在顶层）
    flat = {"message_list": [
        {"client_msg_id": "3", "from_uid": "u3", "msg_content": "rawtext", "send_time": 300},
    ]}
    r3 = parse_messages(flat)
    check("flat 格式 - 解析出1条", len(r3) == 1)
    check("flat 格式 - content_text 非空", bool(r3[0]["content_text"]))

    # content 是 JSON 字符串（包含 text）
    js_str = {"data": {"message_list": [
        {"msg_id": "4", "content": '{"text":"你好","extra":"x"}', "create_time": 400},
    ]}}
    r4 = parse_messages(js_str)
    check("JSON字符串 content - text 字段", r4[0]["content_text"] == "你好")

    # content 是非 JSON 纯文本
    raw_str = {"data": {"message_list": [
        {"msg_id": "5", "content": "纯文本消息", "create_time": 500},
    ]}}
    r5 = parse_messages(raw_str)
    check("纯文本 content - content_text 正确", r5[0]["content_text"] == "纯文本消息")


def test_phone_regex_on_fixture(fix: dict) -> None:
    section("_PHONE_RE 手机号检测（含 fixture 中真实消息）")

    # 内置用例
    check("标准手机号检测", bool(_PHONE_RE.search("我的号码是13800138000")))
    check("前缀 0 不匹配", not _PHONE_RE.search("013800138000"))
    check("11位非手机号不匹配", not _PHONE_RE.search("12345678901"))
    check("短号不匹配", not _PHONE_RE.search("1380013800"))

    # 对 fixture XHR body 中的真实消息文本跑一遍
    samples = fix.get("xhr_samples") or []
    texts: list[str] = []
    for s in samples:
        msgs = parse_messages(s.get("body", {}))
        texts.extend(m["content_text"] for m in msgs if m["content_text"])

    if not texts:
        print("       [INFO] fixture 中暂无消息文本，跳过真实消息检测")
        return

    phones_found = [t for t in texts if _PHONE_RE.search(t)]
    print(f"       共扫描 {len(texts)} 条消息，检测到含手机号: {len(phones_found)} 条")
    # 不强制要求必须找到手机号（会话内容不可控），仅确认不 crash
    check("对真实消息跑 _PHONE_RE 不 crash", True)


def test_get_pending_on_fixture(fix: dict) -> None:
    section("_get_pending_guest_message() 用 fixture 消息注入测试")

    samples = fix.get("xhr_samples") or []
    all_msgs: list[dict] = []
    for s in samples:
        all_msgs.extend(parse_messages(s.get("body", {})))

    if not all_msgs:
        print("       [INFO] fixture 中暂无消息，使用内置模拟数据")
        all_msgs = [
            {"msg_id": "m1", "sender_uid": "guest_1", "content_text": "你好", "create_time": 1000},
            {"msg_id": "m2", "sender_uid": "agent_0", "content_text": "您好，有什么可以帮您？", "create_time": 2000},
            {"msg_id": "m3", "sender_uid": "guest_1", "content_text": "想了解产品价格", "create_time": 3000},
        ]

    agent = _make_agent(all_msgs, agent_uid="agent_0")
    result = agent._get_pending_guest_message()
    check("_get_pending 返回 str 或 None", result is None or isinstance(result, str))
    print(f"       result={result!r}")

    # 注入已处理过的消息 ID，再次查询应返回 None
    sorted_msgs = sorted(all_msgs, key=lambda m: m["create_time"])
    for m in sorted_msgs:
        agent._seen_msg_ids.add(m["msg_id"])
    result2 = agent._get_pending_guest_message()
    check("所有消息已 seen 后返回 None", result2 is None)


def test_extract_js_structure() -> None:
    """验证 EXTRACT_CHAT_JS 是一段合法的 JS（检查字符串结构，不执行）"""
    section("EXTRACT_CHAT_JS 脚本结构静态检查")
    check("是字符串", isinstance(EXTRACT_CHAT_JS, str))
    check("包含 ok 返回值", "ok:" in EXTRACT_CHAT_JS or '"ok"' in EXTRACT_CHAT_JS)
    check("包含 msgs 返回值", "msgs:" in EXTRACT_CHAT_JS or '"msgs"' in EXTRACT_CHAT_JS)
    check("包含 input 查找逻辑", "querySelector" in EXTRACT_CHAT_JS)
    check("包含 ok: false 分支", "ok: false" in EXTRACT_CHAT_JS)


# ── 主入口 ────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Offline fixture-based tests for DouyinCSAgent")
    ap.add_argument(
        "--fixtures",
        type=Path,
        default=Path(__file__).parent / "fixtures",
        help="Fixtures directory (default: tools/fixtures/)",
    )
    args = ap.parse_args()

    fdir = args.fixtures
    print(f"Fixture 目录: {fdir.resolve()}")

    def load(name: str) -> Any:
        p = fdir / name
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [WARN] 读取 {name} 失败: {e}")
            return None

    fix = {
        "page_info": load("page_info.json"),
        "xhr_samples": load("xhr_samples.json"),
        "js_extract": load("js_extract.json"),
        "dom_snapshot": load("dom_snapshot.json"),
    }

    has_any = any(v is not None for v in fix.values())
    if not has_any:
        print("\n[WARN] fixtures/ 目录为空，请先运行：")
        print("       py -3.11 tools/capture_fixtures.py")
        print("\n继续跑纯内置单元测试（不依赖 fixture）...\n")

    test_page_info(fix)
    test_xhr_samples(fix)
    test_js_extract(fix)
    test_dom_snapshot(fix)
    test_parse_messages_unit()
    test_phone_regex_on_fixture(fix)
    test_get_pending_on_fixture(fix)
    test_extract_js_structure()

    total = _pass + _fail
    print(f"\n{'='*40}")
    print(f"结果: {_pass}/{total} 通过  {_fail} 失败")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
