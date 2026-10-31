"""Structured WeWork monitor: detect new messages, merge master, and reply once."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
import re

from src.ocr_runtime import init_ocr_runtime

init_ocr_runtime()

import yaml

from src.actions import click_message_composer, paste_and_send, search_and_open_contact
from src.capture import RegionRatios
from src.contact_list import ListRatios, capture_and_ocr_list, capture_list_hash, changed_contacts
from src.dashscope_key import resolve_dashscope_api_key
from src.ocr_client import warmup_ocr_service
from src.qwen_text import QwenTextClient
from src.revai_qa import RevaiQaClient
from src.window import activate_hwnd, ensure_visible, find_wework_hwnd, is_same_process_foreground
import wxwork_export_recent as wx_recent
from wxwork_export_recent import (
    _capture_chat_text,
    _conversation_key,
    _hover_time_for_message,
    _merge_master,
    _message_key,
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

log = logging.getLogger(__name__)


ReplyClient = QwenTextClient | RevaiQaClient


def _load_cfg(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing config: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def _should_reply_contact(name: str, allow: list[str]) -> bool:
    if not allow:
        return True
    nl = name.lower()
    return any(x.lower() in nl or nl in x.lower() for x in allow)


def _canonical_contact_name(name: str, allow: list[str]) -> str:
    nl = name.lower()
    for item in allow:
        il = item.lower()
        if il and (il in nl or nl in il):
            return item
    return name


def _is_replyable_message(msg: dict[str, Any], allowed_types: set[str]) -> bool:
    if msg.get("kind") != "message":
        return False
    if msg.get("direction") != "incoming":
        return False
    if msg.get("reply_status") in {"sent", "skipped"}:
        return False
    message_type = str(msg.get("message_type") or "text")
    if message_type not in allowed_types:
        return False
    text = str(msg.get("text") or "").strip()
    if len(text) < 2:
        return False
    if re.fullmatch(r"(星期[一二三四五六日天]?|今天|昨天|\d{1,2}:\d{2}|[/\d.-]{3,})", text):
        return False
    return True


def _recent_context(messages: list[dict[str, Any]], target_key: str, *, max_items: int = 12) -> str:
    rows: list[str] = []
    for msg in messages[-max_items:]:
        if msg.get("kind") != "message":
            continue
        sender = msg.get("sender") or msg.get("direction") or "unknown"
        ts = msg.get("timestamp") or ""
        typ = msg.get("message_type") or "text"
        text = msg.get("text") or ""
        marker = " <-- new" if msg.get("message_key") == target_key else ""
        rows.append(f"[{ts}] {sender} {typ}: {text}{marker}")
    return "\n".join(rows)


def _build_reply_prompt(conversation: dict[str, Any], msg: dict[str, Any]) -> str:
    messages = conversation.get("structured_messages") or []
    context = _recent_context(messages, str(msg.get("message_key") or ""))
    return (
        f"会话：{conversation.get('title') or ''}\n\n"
        f"最近结构化消息：\n{context}\n\n"
        "请只回复最后标记为 new 的客户消息。"
        "如果信息不足，简短追问一个关键问题。"
        "不要编造价格、日期、订单号或承诺。"
        "只输出要发送给客户的正文。"
    )


def _mark_master_reply(
    master_path: Path,
    conv_key: str,
    msg_key: str,
    *,
    status: str,
    reply_text: str = "",
    reason: str = "",
    model: str = "",
) -> None:
    master = _read_json(master_path)
    now = datetime.now().isoformat(timespec="seconds")
    for conv in master.get("conversations", []):
        if conv.get("conversation_key") != conv_key:
            continue
        for msg in conv.get("structured_messages", []):
            if msg.get("message_key") != msg_key:
                continue
            msg["reply_status"] = status
            msg["reply_text"] = reply_text
            msg["reply_reason"] = reason
            msg["reply_model"] = model
            msg["reply_updated_at"] = now
            if status == "sent":
                msg["reply_sent_at"] = now
            _write_json(master_path, master)
            return


def _unreplied_run_messages(master_path: Path, run_data: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any], str, str]]:
    if master_path.is_file():
        master = _read_json(master_path)
    else:
        master = {"conversations": []}
    master_by_conv = {c.get("conversation_key"): c for c in master.get("conversations", [])}
    result: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
    for conv in run_data.get("conversations", []):
        conv_key = _conversation_key(conv)
        master_conv = master_by_conv.get(conv_key) or {}
        by_key = {
            m.get("message_key"): m
            for m in master_conv.get("structured_messages", [])
            if isinstance(m, dict)
        }
        for msg in conv.get("structured_messages") or []:
            key, _confidence = _message_key(conv_key, msg)
            master_msg = by_key.get(key)
            if master_msg and master_msg.get("reply_status") in {"sent", "skipped"}:
                continue
            msg = dict(msg)
            msg["message_key"] = key
            result.append((conv, msg, conv_key, key))
    return result


def _capture_changed_conversation(
    hwnd: int,
    name: str,
    *,
    run_dir: Path,
    conv_index: int,
    messages: int,
    max_scrolls: int,
    after_search: float,
    include_message_time: bool,
    allow_background_visible: bool,
) -> dict[str, Any]:
    activate_hwnd(hwnd)
    time.sleep(0.2)
    search_and_open_contact(name, pause_after_enter=after_search, hwnd=hwnd)
    msg_lines, shots, structured = _capture_chat_text(
        hwnd,
        conv_index,
        run_dir,
        target_lines=messages,
        max_scrolls=max_scrolls,
        include_message_time=include_message_time,
        allow_background_visible=allow_background_visible,
    )
    return {
        "index": conv_index,
        "title": name,
        "list_preview": "",
        "messages": msg_lines,
        "screenshots": shots,
        "structured_messages": structured,
    }


def _capture_conversation_ocr_only(
    hwnd: int,
    name: str,
    *,
    run_dir: Path,
    conv_index: int,
    messages: int,
    max_scrolls: int,
    after_search: float,
    allow_background_visible: bool,
) -> dict[str, Any]:
    return _capture_changed_conversation(
        hwnd,
        name,
        run_dir=run_dir,
        conv_index=conv_index,
        messages=messages,
        max_scrolls=max_scrolls,
        after_search=after_search,
        include_message_time=False,
        allow_background_visible=allow_background_visible,
    )


def _write_run(run_dir: Path, conversations: list[dict[str, Any]]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "recent_chats.json"
    data = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "conversations": conversations,
    }
    _write_json(path, data)
    return path


def _conversation_message_keys(conv: dict[str, Any]) -> set[str]:
    conv_key = _conversation_key(conv)
    keys: set[str] = set()
    for msg in conv.get("structured_messages") or []:
        key, _confidence = _message_key(conv_key, msg)
        keys.add(key)
    return keys


def _message_fingerprint(conv_key: str, msg: dict[str, Any]) -> str:
    text = " ".join(str(msg.get("text") or "").split())
    sender = str(msg.get("sender") or "unknown").strip()
    direction = str(msg.get("direction") or "").strip()
    message_type = str(msg.get("message_type") or "text").strip()
    return f"{conv_key}|{sender}|{direction}|{message_type}|{text}"


def _run_message_fingerprints(run_data: dict[str, Any]) -> dict[str, Counter[str]]:
    result: dict[str, Counter[str]] = {}
    for conv in run_data.get("conversations", []):
        conv_key = _conversation_key(conv)
        counter = result.setdefault(conv_key, Counter())
        for msg in conv.get("structured_messages") or []:
            if msg.get("kind") == "message":
                counter[_message_fingerprint(conv_key, msg)] += 1
    return result


def _load_seen_counts_from_master(master_path: Path) -> dict[str, Counter[str]]:
    if not master_path.is_file():
        return {}
    master = _read_json(master_path)
    result: dict[str, Counter[str]] = {}
    for conv in master.get("conversations", []):
        conv_key = str(conv.get("conversation_key") or _conversation_key(conv))
        counter = result.setdefault(conv_key, Counter())
        for msg in conv.get("structured_messages") or []:
            if isinstance(msg, dict) and msg.get("kind") == "message":
                counter[_message_fingerprint(conv_key, msg)] += 1
    return result


def _run_message_keys(run_data: dict[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for conv in run_data.get("conversations", []):
        conv_key = _conversation_key(conv)
        result.setdefault(conv_key, set()).update(_conversation_message_keys(conv))
    return result


def _filter_new_run_messages(
    run_data: dict[str, Any],
    seen_counts_by_conv: dict[str, Counter[str]],
) -> list[tuple[dict[str, Any], dict[str, Any], str, str]]:
    result: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
    for conv in run_data.get("conversations", []):
        conv_key = _conversation_key(conv)
        seen = seen_counts_by_conv.setdefault(conv_key, Counter())
        run_seen: Counter[str] = Counter()
        for msg in conv.get("structured_messages") or []:
            if msg.get("kind") != "message":
                continue
            fingerprint = _message_fingerprint(conv_key, msg)
            run_seen[fingerprint] += 1
            if run_seen[fingerprint] <= seen[fingerprint]:
                continue
            msg_key, _confidence = _message_key(conv_key, msg)
            msg = dict(msg)
            msg["message_key"] = msg_key
            result.append((conv, msg, conv_key, msg_key))
    return result


def _visible_reply_signature(conv_key: str, msg: dict[str, Any], index: int) -> str:
    text = " ".join(str(msg.get("text") or "").split())
    center = msg.get("ocr_center") or []
    try:
        y_bucket = int(float(center[1]) // 16)
    except Exception:
        y_bucket = -1
    return f"{conv_key}|{index}|{y_bucket}|{text}"


def _latest_visible_reply_signatures(run_data: dict[str, Any], allowed_types: set[str]) -> set[str]:
    signatures: set[str] = set()
    for conv in run_data.get("conversations", []):
        conv_key = _conversation_key(conv)
        messages = [m for m in conv.get("structured_messages") or [] if isinstance(m, dict) and m.get("kind") == "message"]
        for idx, msg in enumerate(messages):
            if _is_replyable_message(msg, allowed_types):
                signatures.add(_visible_reply_signature(conv_key, msg, idx))
    return signatures


def _latest_incoming_after_outgoing_candidates(
    run_data: dict[str, Any],
    allowed_types: set[str],
    seen_visible_signatures: set[str],
) -> list[tuple[dict[str, Any], dict[str, Any], str, str]]:
    result: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
    for conv in run_data.get("conversations", []):
        conv_key = _conversation_key(conv)
        messages = [m for m in conv.get("structured_messages") or [] if isinstance(m, dict) and m.get("kind") == "message"]
        last_outgoing = -1
        for idx, msg in enumerate(messages):
            if msg.get("direction") == "outgoing":
                last_outgoing = idx
        for idx in range(len(messages) - 1, last_outgoing, -1):
            msg = messages[idx]
            if not _is_replyable_message(msg, allowed_types):
                continue
            signature = _visible_reply_signature(conv_key, msg, idx)
            if signature in seen_visible_signatures:
                return result
            seen_visible_signatures.add(signature)
            msg_key, _confidence = _message_key(conv_key, msg)
            msg = dict(msg)
            msg["message_key"] = msg_key
            result.append((conv, msg, conv_key, msg_key))
            return result
    return result


def _mark_seen_from_run(run_data: dict[str, Any], seen_counts_by_conv: dict[str, Counter[str]]) -> None:
    for conv_key, counts in _run_message_fingerprints(run_data).items():
        seen = seen_counts_by_conv.setdefault(conv_key, Counter())
        for fingerprint, count in counts.items():
            seen[fingerprint] = max(seen[fingerprint], count)


def _incremental_hover_candidates(
    hwnd: int,
    run_data: dict[str, Any],
    candidates: list[tuple[dict[str, Any], dict[str, Any], str, str]],
    run_dir: Path,
) -> dict[str, Any]:
    if not candidates:
        return run_data
    hover_dir = run_dir / "incremental_hover"
    by_id: dict[tuple[str, str], dict[str, Any]] = {}
    for _conv, msg, conv_key, msg_key in candidates:
        label = f"{conv_key[:24]}_{msg_key[-24:]}".replace("\\", "_").replace("/", "_").replace(":", "_")
        by_id[(conv_key, str(msg.get("id") or ""))] = _hover_time_for_message(hwnd, msg, hover_dir, label)

    for conv in run_data.get("conversations", []):
        conv_key = _conversation_key(conv)
        for idx, msg in enumerate(conv.get("structured_messages") or []):
            replacement = by_id.get((conv_key, str(msg.get("id") or "")))
            if replacement:
                conv["structured_messages"][idx] = replacement
    return run_data


def _process_reply_candidates(
    *,
    candidates: list[tuple[dict[str, Any], dict[str, Any], str, str]],
    master_path: Path,
    allowed_types: set[str],
    client: ReplyClient | None,
    provider: str,
    reply_cfg: dict[str, Any],
    send_enabled: bool,
    hwnd: int,
    after_search: float,
    from_bottom: float,
) -> None:
    master = _read_json(master_path) if master_path.is_file() else {"conversations": []}
    master_by_key = {c.get("conversation_key"): c for c in master.get("conversations", [])}
    replyable: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
    for run_conv, msg, conv_key, msg_key in candidates:
        master_conv = master_by_key.get(conv_key)
        if not master_conv or not _is_replyable_message(msg, allowed_types):
            continue
        replyable.append((run_conv, msg, conv_key, msg_key))
    if len(replyable) > 1:
        for _run_conv, msg, conv_key, msg_key in replyable[:-1]:
            _mark_master_reply(master_path, conv_key, msg_key, status="skipped", reason="superseded_same_scan")
    for _run_conv, msg, conv_key, msg_key in replyable[-1:]:
        master_conv = master_by_key.get(conv_key)
        if not master_conv:
            continue
        if client is None:
            _mark_master_reply(master_path, conv_key, msg_key, status="skipped", reason=f"missing_{provider}_client")
            continue
        prompt = _build_reply_prompt(master_conv, msg)
        print(f">>> generate reply for {master_conv.get('title')} | {msg.get('text')}", flush=True)
        try:
            reply = client.complete(
                str(reply_cfg.get("system") or "你是专业客服，用简短中文回复。"),
                prompt,
                session_id=conv_key,
            )
        except Exception as e:
            _mark_master_reply(master_path, conv_key, msg_key, status="failed", reason=str(e))
            print(f">>> {provider} failed: {e}", flush=True)
            continue
        print(f">>> reply: {reply}", flush=True)
        if send_enabled:
            activate_hwnd(hwnd)
            time.sleep(0.2)
            search_and_open_contact(str(master_conv.get("title") or ""), pause_after_enter=after_search, hwnd=hwnd)
            click_message_composer(hwnd, from_bottom_ratio=from_bottom)
            paste_and_send(reply, hwnd=hwnd, from_bottom_ratio=from_bottom)
            _mark_master_reply(master_path, conv_key, msg_key, status="sent", reply_text=reply, model=client.model)
        else:
            _mark_master_reply(
                master_path,
                conv_key,
                msg_key,
                status="skipped",
                reply_text=reply,
                reason="dry_run",
                model=client.model,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="WXWork structured monitor + auto reply")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "config.yaml")
    parser.add_argument("--run-dir", type=Path, default=Path("exports") / "wxwork_agent_loop")
    parser.add_argument("--master-dir", type=Path, default=Path("exports") / "wxwork_master")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send", action="store_true", help="Actually send generated replies.")
    parser.add_argument("--active-scan", action="store_true", help="Bring WeWork to foreground before each scan.")
    parser.add_argument("--allow-background-visible", action="store_true", help="Capture visible, non-overlapped WeWork without foreground focus.")
    parser.add_argument("--force-ocr-each-scan", action="store_true", help="OCR the conversation list on every scan instead of trusting the pixel hash.")
    parser.add_argument("--contact", action="append", default=[], help="Only auto-reply to matching contacts. Repeatable.")
    parser.add_argument("--messages", type=int, default=12)
    parser.add_argument("--max-scrolls", type=int, default=2)
    parser.add_argument("--interval-sec", type=float, default=None)
    parser.add_argument("--baseline-scans", type=int, default=2, help="Direct-contact baseline capture passes before monitoring.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    cfg = _load_cfg(args.config)
    cap_cfg = cfg.get("capture") or {}
    inp = cfg.get("input") or {}
    timing = cfg.get("timing") or {}
    qwen_cfg = cfg.get("qwen") or {}
    revai_cfg = cfg.get("revai") or {}
    reply_engine_cfg = cfg.get("reply_engine") or {}
    reply_cfg = cfg.get("reply") or {}
    agent_cfg = cfg.get("agent_loop") or {}

    interval = float(args.interval_sec if args.interval_sec is not None else agent_cfg.get("interval_sec", 2.0))
    active_scan = bool(args.active_scan or agent_cfg.get("active_scan", False))
    allowed_contacts = args.contact or [str(x) for x in agent_cfg.get("auto_reply_contacts") or []]
    allowed_types = {str(x) for x in agent_cfg.get("reply_message_types") or ["text"]}
    include_message_time = bool(agent_cfg.get("include_message_time", True))
    from_bottom = float(inp.get("click_from_bottom_ratio", 0.10))
    after_search = float(timing.get("after_search_sec", 1.0))
    send_enabled = bool(args.send and not args.dry_run)

    msg_ratios = RegionRatios(
        left=float(cap_cfg.get("left_ratio", 0.27)),
        top=float(cap_cfg.get("top_ratio", 0.12)),
        width=float(cap_cfg.get("width_ratio", 0.72)),
        height=float(cap_cfg.get("height_ratio", 0.55)),
    )
    wx_recent.CHAT_REGION = msg_ratios
    list_ratios = ListRatios(
        left=float(agent_cfg.get("list_left_ratio", 0.045)),
        top=float(agent_cfg.get("list_top_ratio", 0.06)),
        width=float(agent_cfg.get("list_width_ratio", 0.225)),
        height=float(agent_cfg.get("list_height_ratio", 0.88)),
    )

    provider = str(reply_engine_cfg.get("provider") or "qwen").strip().lower()
    client: ReplyClient | None = None
    if provider == "qwen":
        api_key = resolve_dashscope_api_key(qwen_cfg)
        if not api_key:
            print(">>> missing DashScope API key; use --dry-run or configure qwen.api_key", flush=True)
            if send_enabled:
                return 1
        else:
            client = QwenTextClient(
                api_key=api_key,
                base_url=str(qwen_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
                model=str(qwen_cfg.get("model", "qwen-turbo")),
                temperature=float(qwen_cfg.get("temperature", 0.3)),
                timeout=int(qwen_cfg.get("timeout", 60)),
            )
    elif provider == "revai":
        client = RevaiQaClient(
            base_url=str(revai_cfg.get("base_url", "https://car.revai.cn")),
            endpoint=str(revai_cfg.get("endpoint", "/api/project/carzone/compat/chat/respond")),
            api_key=revai_cfg.get("api_key"),
            timeout=int(revai_cfg.get("timeout", 60)),
            session_prefix=str(revai_cfg.get("session_prefix", "wxwork")),
            message_mode=str(revai_cfg.get("message_mode", "latest_new")),
        )
    else:
        print(f">>> unsupported reply_engine.provider: {provider}", flush=True)
        return 1

    print(">>> OCR service warmup", flush=True)
    warmup_ocr_service()
    hwnd = find_wework_hwnd()
    if not hwnd:
        print(">>> WeWork window not found", flush=True)
        return 2

    activate_hwnd(hwnd)
    time.sleep(0.5)
    ensure_visible(hwnd)
    master_path = args.master_dir / "recent_chats.json"

    if allowed_contacts:
        seen_counts_by_conv = _load_seen_counts_from_master(master_path)
        seen_visible_signatures: set[str] = set()
        if seen_counts_by_conv:
            seen_total = sum(sum(counts.values()) for counts in seen_counts_by_conv.values())
            print(f">>> loaded master history messages={seen_total}", flush=True)
        baseline_passes = max(1, int(args.baseline_scans))
        for pass_i in range(baseline_passes):
            baseline_conversations: list[dict[str, Any]] = []
            for idx, contact in enumerate(allowed_contacts, start=1):
                print(f">>> direct baseline pass={pass_i + 1}/{baseline_passes} contact: {contact}", flush=True)
                baseline_conversations.append(
                    _capture_conversation_ocr_only(
                        hwnd,
                        contact,
                        run_dir=args.run_dir,
                        conv_index=idx,
                        messages=args.messages,
                        max_scrolls=args.max_scrolls,
                        after_search=after_search,
                        allow_background_visible=args.allow_background_visible,
                    )
                )
            baseline_path = _write_run(args.run_dir, baseline_conversations)
            baseline_data = _read_json(baseline_path)
            _merge_master(args.run_dir, args.master_dir)
            _mark_seen_from_run(baseline_data, seen_counts_by_conv)
            seen_visible_signatures.update(_latest_visible_reply_signatures(baseline_data, allowed_types))
            if pass_i + 1 < baseline_passes:
                time.sleep(0.5)
        seen_total = sum(sum(counts.values()) for counts in seen_counts_by_conv.values())
        print(
            f">>> direct baseline contacts={len(allowed_contacts)} messages={seen_total} "
            f"mode={'send' if send_enabled else 'dry-run'}",
            flush=True,
        )
        if args.once:
            return 0

        loop_i = 0
        while True:
            time.sleep(interval)
            hwnd = find_wework_hwnd()
            if not hwnd:
                print(">>> WeWork window lost", flush=True)
                if args.once:
                    return 2
                continue
            ensure_visible(hwnd)

            conversations: list[dict[str, Any]] = []
            for idx, contact in enumerate(allowed_contacts, start=1):
                print(f">>> direct scan contact: {contact}", flush=True)
                conversations.append(
                    _capture_conversation_ocr_only(
                        hwnd,
                        contact,
                        run_dir=args.run_dir,
                        conv_index=idx,
                        messages=args.messages,
                        max_scrolls=args.max_scrolls,
                        after_search=after_search,
                        allow_background_visible=args.allow_background_visible,
                    )
                )

            run_path = _write_run(args.run_dir, conversations)
            run_data = _read_json(run_path)
            candidates = _filter_new_run_messages(run_data, seen_counts_by_conv)
            if not candidates:
                candidates = _latest_incoming_after_outgoing_candidates(run_data, allowed_types, seen_visible_signatures)
                if candidates:
                    print(f">>> direct latest incoming after outgoing={len(candidates)}", flush=True)
            if include_message_time and candidates:
                run_data = _incremental_hover_candidates(hwnd, run_data, candidates, args.run_dir)
                _write_json(run_path, run_data)
                candidates = _filter_new_run_messages(run_data, seen_counts_by_conv)
            _merge_master(args.run_dir, args.master_dir)
            if candidates:
                print(f">>> direct new messages={len(candidates)}", flush=True)
            else:
                print(">>> direct no new messages", flush=True)
            _process_reply_candidates(
                candidates=candidates,
                master_path=master_path,
                allowed_types=allowed_types,
                client=client,
                provider=provider,
                reply_cfg=reply_cfg,
                send_enabled=send_enabled,
                hwnd=hwnd,
                after_search=after_search,
                from_bottom=from_bottom,
            )
            _mark_seen_from_run(run_data, seen_counts_by_conv)
            if args.once:
                return 0
            loop_i += 1

    prev_entries, last_hash = capture_and_ocr_list(hwnd, list_ratios)
    print(f">>> baseline conversations={len(prev_entries)} mode={'send' if send_enabled else 'dry-run'}", flush=True)
    loop_i = 0

    while True:
        time.sleep(interval if loop_i else 0.2)
        hwnd = find_wework_hwnd()
        if not hwnd:
            print(">>> WeWork window lost", flush=True)
            if args.once:
                return 2
            continue
        if active_scan:
            activate_hwnd(hwnd)
            time.sleep(0.35)
        ensure_visible(hwnd)
        if not active_scan and not args.allow_background_visible and not is_same_process_foreground(hwnd):
            print(">>> WeWork not foreground; waiting", flush=True)
            if args.once:
                return 0
            loop_i += 1
            continue

        h = capture_list_hash(hwnd, list_ratios)
        if h == last_hash and not args.once and not args.force_ocr_each_scan:
            loop_i += 1
            continue
        curr_entries, h2 = capture_and_ocr_list(hwnd, list_ratios)
        changed = changed_contacts(prev_entries, curr_entries)
        prev_entries = curr_entries
        last_hash = h2
        if not changed:
            print(">>> no changed contacts", flush=True)
            if args.once:
                return 0
            loop_i += 1
            continue

        conversations: list[dict[str, Any]] = []
        for idx, entry in enumerate(changed, start=1):
            contact_haystack = f"{entry.name} {entry.preview}"
            if not _should_reply_contact(contact_haystack, allowed_contacts):
                print(f">>> changed but not allowed: {entry.name}", flush=True)
                continue
            capture_name = _canonical_contact_name(contact_haystack, allowed_contacts)
            print(f">>> changed contact: {entry.name} preview={entry.preview[:40]}", flush=True)
            conv = _capture_changed_conversation(
                hwnd,
                capture_name,
                run_dir=args.run_dir,
                conv_index=idx,
                messages=args.messages,
                max_scrolls=args.max_scrolls,
                after_search=after_search,
                include_message_time=include_message_time,
                allow_background_visible=args.allow_background_visible,
            )
            conversations.append(conv)

        if conversations:
            run_path = _write_run(args.run_dir, conversations)
            run_data = _read_json(run_path)
            candidates = _unreplied_run_messages(master_path, run_data)
            _merge_master(args.run_dir, args.master_dir)
            _process_reply_candidates(
                candidates=candidates,
                master_path=master_path,
                allowed_types=allowed_types,
                client=client,
                provider=provider,
                reply_cfg=reply_cfg,
                send_enabled=send_enabled,
                hwnd=hwnd,
                after_search=after_search,
                from_bottom=from_bottom,
            )

        if args.once:
            return 0
        loop_i += 1


if __name__ == "__main__":
    raise SystemExit(main())
