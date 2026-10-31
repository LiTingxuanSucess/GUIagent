"""
多会话监控：监听企业微信左侧会话列表，有新消息就提醒；
对 config 指定的联系人自动用 Qwen 回复。

用法:
  set DASHSCOPE_API_KEY=...
  cd WXWorkAgent
  py -3.11 wxwork_multi_watch.py

特性
----
* alert_only=true  → 只打印提醒 + 系统提示音，不切窗口不发消息
* alert_only=false → 提醒 + 对 auto_reply_contacts 里的联系人自动回复
* auto_reply_contacts 为空列表 → 对所有会话自动回复
* 原 wxwork_watch.py（单联系人）不受影响，可同时使用

检测原理
--------
企业微信把最新消息的会话置顶。脚本每 interval_sec 秒截取左侧列表区，
pixel-hash 快速判断有无变化，变化后 OCR → 解析出 (名字, 最后预览文本)
→ 与上轮对比，找到预览改变的联系人 → 触发提醒/回复。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from src.ocr_runtime import init_ocr_runtime

init_ocr_runtime()

import win32api
import yaml

from src.actions import click_message_composer, paste_and_send, search_and_open_contact
from src.capture import RegionRatios, mss_region_for_ratios
from src.contact_list import (
    ContactEntry,
    ListRatios,
    capture_and_ocr_list,
    capture_list_hash,
    changed_contacts,
)
from src.dashscope_key import resolve_dashscope_api_key
from src.diff_util import appended_content, looks_like_echo
from src.ocr_stable import ocr_stable_region
from src.ocr_client import warmup_ocr_service
from src.qwen_text import QwenTextClient
from src.window import activate_hwnd, ensure_visible, find_wework_hwnd, is_same_process_foreground

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

log = logging.getLogger(__name__)
_p = lambda s: print(s, flush=True)   # noqa: E731  bypass PaddleOCR log hijack


# ── Helpers ────────────────────────────────────────────────────────────────

def _beep() -> None:
    """Play a system default alert sound (non-blocking)."""
    try:
        win32api.MessageBeep(0)      # MB_OK sound
    except Exception:
        pass


def _alert(entry: ContactEntry) -> None:
    """Print a clearly visible alert line + beep."""
    _beep()
    border = "─" * 60
    _p(f"\n{'═'*60}")
    _p(f"  🔔  新消息  →  {entry.name}")
    _p(f"  预览: {entry.preview[:80]}")
    _p(f"{'═'*60}\n")


def _should_reply(name: str, auto_reply_contacts: list[str]) -> bool:
    """
    True if this contact should get an auto-reply.
    Empty list = reply to everyone; otherwise name must match an entry
    (case-insensitive substring match).
    """
    if not auto_reply_contacts:
        return True
    nl = name.lower()
    return any(target.lower() in nl or nl in target.lower()
               for target in auto_reply_contacts)


def _ocr_stable_once(hwnd: int, ratios: RegionRatios, samples: int, sleep: float) -> tuple[str, str]:
    """Capture until `samples` consecutive OCR results agree. Returns (text, hash)."""
    return ocr_stable_region(
        hwnd,
        ratios,
        stable_samples=samples,
        stable_sleep_sec=sleep,
        timeout_sec=max(8.0, samples * sleep * 8),
    )


def _process_contact(
    name: str,
    hwnd: int,
    msg_ratios: RegionRatios,
    client: QwenTextClient,
    system_prompt: str,
    from_bottom: float,
    stable_samples: int,
    stable_sleep: float,
    post_send_pause: float,
    after_activate: float,
    after_search: float,
    last_texts: dict[str, str],
    last_outgoing: dict[str, str],
    min_new_chars: int,
) -> None:
    """
    Switch to *name*'s chat, OCR it, generate a Qwen reply, send it.
    Updates last_texts and last_outgoing in-place.
    """
    _p(f"  → 切换到会话: {name}")
    activate_hwnd(hwnd)
    time.sleep(after_activate)
    search_and_open_contact(name, pause_after_enter=after_search, hwnd=hwnd)

    text, h = _ocr_stable_once(hwnd, msg_ratios, stable_samples, stable_sleep)
    prev_text = last_texts.get(name, "")

    new_part = appended_content(prev_text, text)
    _p(f"  新增OCR: {new_part!r}")

    if not new_part or len(new_part.strip()) < min_new_chars:
        _p("  新增过短或 OCR 漂移，跳过回复")
        last_texts[name] = text
        return

    if looks_like_echo(new_part, last_outgoing.get(name, "")):
        _p("  疑似己方回声，跳过")
        last_texts[name] = text
        return

    user_prompt = (
        "以下为会话区 OCR 全文（可能有错漏）：\n\n"
        f"{text}\n\n"
        "其中「相对上一轮快照新增」部分很可能是客户刚发的：\n"
        f"{new_part}\n\n"
        "请生成一条可直接发给客户的中文回复（只输出正文）。"
    )
    _p("  调用 Qwen…")
    try:
        reply = client.complete(system_prompt, user_prompt)
    except Exception as e:
        _p(f"  Qwen 失败，跳过: {e}")
        last_texts[name] = text
        return

    _p(f"  Qwen 回复: {reply!r}")

    activate_hwnd(hwnd)
    time.sleep(0.2)
    click_message_composer(hwnd, from_bottom_ratio=from_bottom)
    paste_and_send(reply, hwnd=hwnd, from_bottom_ratio=from_bottom)
    last_outgoing[name] = reply

    time.sleep(post_send_pause)
    text2, _ = _ocr_stable_once(hwnd, msg_ratios, stable_samples, stable_sleep)
    last_texts[name] = text2
    _p(f"  已发送，{name} 基线已同步")


# ── Entry point ────────────────────────────────────────────────────────────

def _load_cfg(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing config: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="WXWork multi-contact monitor")
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).resolve().parent / "config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    cfg          = _load_cfg(args.config)
    cap_cfg      = cfg.get("capture")   or {}
    inp          = cfg.get("input")     or {}
    timing       = cfg.get("timing")    or {}
    qwen_cfg     = cfg.get("qwen")      or {}
    reply_cfg    = cfg.get("reply")     or {}
    watch        = cfg.get("watch")     or {}
    mw           = cfg.get("multi_watch") or {}

    # ── multi_watch config ──────────────────────────────────────────────
    interval          = float(mw.get("interval_sec", 2.0))
    alert_only        = bool(mw.get("alert_only", False))
    auto_reply_list   = [str(x) for x in (mw.get("auto_reply_contacts") or [])]

    list_ratios = ListRatios(
        left   = float(mw.get("list_left_ratio",   0.045)),
        top    = float(mw.get("list_top_ratio",    0.06)),
        width  = float(mw.get("list_width_ratio",  0.225)),
        height = float(mw.get("list_height_ratio", 0.88)),
    )

    # ── message-area OCR ratios (shared with wxwork_watch) ─────────────
    msg_ratios = RegionRatios(
        left   = float(cap_cfg.get("left_ratio",   0.27)),
        top    = float(cap_cfg.get("top_ratio",    0.12)),
        width  = float(cap_cfg.get("width_ratio",  0.72)),
        height = float(cap_cfg.get("height_ratio", 0.55)),
    )

    from_bottom     = float(inp.get("click_from_bottom_ratio", 0.10))
    after_search    = float(timing.get("after_search_sec",    1.0))
    after_activate  = float(timing.get("after_activate_sec",  0.35))

    stable_samples  = int(watch.get("stable_samples",    2))
    stable_sleep    = float(watch.get("stable_sleep_sec", 0.45))
    min_new_chars   = int(watch.get("min_new_chars",      2))
    post_send_pause = float(watch.get("post_send_pause_sec", 1.2))

    system_prompt   = reply_cfg.get("system") or "你是专业客服，用简短中文回复。"

    # ── Qwen client (not needed in alert_only mode) ─────────────────────
    client: QwenTextClient | None = None
    if not alert_only:
        api_key = resolve_dashscope_api_key(qwen_cfg)
        if not api_key:
            _p("未配置 DASHSCOPE_API_KEY，将以「仅提醒」模式运行")
            alert_only = True
        else:
            client = QwenTextClient(
                api_key    = api_key,
                base_url   = str(qwen_cfg.get("base_url",
                                 "https://dashscope.aliyuncs.com/compatible-mode/v1")),
                model      = str(qwen_cfg.get("model",       "qwen-turbo")),
                temperature= float(qwen_cfg.get("temperature", 0.3)),
                timeout    = int(qwen_cfg.get("timeout",       60)),
            )

    mode_label = "仅提醒" if alert_only else (
        f"提醒 + 对 {auto_reply_list or '所有联系人'} 自动回复"
    )

    # ── OCR warmup ───────────────────────────────────────────────────────
    _p(">>> OCR 预热中…")
    try:
        warmup_ocr_service()
    except Exception as e:
        _p(f">>> OCR 预热失败: {e}")
        return 3

    hwnd = find_wework_hwnd()
    if not hwnd:
        _p(">>> 未找到企业微信主窗口，请先打开并登录！")
        return 2

    # ── Build initial baseline of the contact list ───────────────────────
    _p(">>> 建立列表基线…")
    prev_entries, last_list_hash = capture_and_ocr_list(hwnd, list_ratios)
    _p(f">>> 基线：发现 {len(prev_entries)} 个会话条目")
    for e in prev_entries[:5]:
        _p(f"    {e.name!r}  →  {e.preview[:40]!r}")

    _p(f">>> 监控中（模式: {mode_label}），每 {interval:.1f}s 检查一次，Ctrl+C 退出")

    # Per-contact state for auto-reply
    last_texts:    dict[str, str] = {}
    last_outgoing: dict[str, str] = {}
    loop_i = 0

    try:
        while True:
            time.sleep(interval)

            hwnd = find_wework_hwnd()
            if not hwnd:
                _p(">>> 主窗口丢失，等待重试…")
                continue

            ensure_visible(hwnd)

            if not is_same_process_foreground(hwnd):
                if loop_i % 10 == 0:
                    _p(f"  [{loop_i+1}] WeWork not foreground; pause list OCR")
                loop_i += 1
                continue

            # ── Quick hash check ─────────────────────────────────────────
            h = capture_list_hash(hwnd, list_ratios)
            if h == last_list_hash:
                if loop_i % 30 == 0:     # periodic heartbeat every ~60s
                    _p(f"  [{loop_i+1}] 列表无变化，监控中…")
                loop_i += 1
                continue

            _p(f"  [{loop_i+1}] 列表 hash 变化 → OCR 解析…")

            # ── OCR + diff ───────────────────────────────────────────────
            curr_entries, h2 = capture_and_ocr_list(hwnd, list_ratios)
            changed = changed_contacts(prev_entries, curr_entries)
            last_list_hash = h2
            prev_entries   = curr_entries

            if not changed:
                _p("  列表 hash 变化但 OCR diff 为空（OCR 漂移），跳过")
                loop_i += 1
                continue

            _p(f"  发现 {len(changed)} 个有新消息的会话")

            for entry in changed:
                _alert(entry)

                if alert_only:
                    continue

                if not _should_reply(entry.name, auto_reply_list):
                    _p(f"  {entry.name} 不在自动回复列表，仅提醒")
                    continue

                if client is None:
                    continue

                try:
                    _process_contact(
                        name            = entry.name,
                        hwnd            = hwnd,
                        msg_ratios      = msg_ratios,
                        client          = client,
                        system_prompt   = system_prompt,
                        from_bottom     = from_bottom,
                        stable_samples  = stable_samples,
                        stable_sleep    = stable_sleep,
                        post_send_pause = post_send_pause,
                        after_activate  = after_activate,
                        after_search    = after_search,
                        last_texts      = last_texts,
                        last_outgoing   = last_outgoing,
                        min_new_chars   = min_new_chars,
                    )
                except Exception as exc:
                    _p(f"  处理 {entry.name} 时出错，跳过: {exc}")
                    log.exception("处理联系人出错")

            loop_i += 1

    except KeyboardInterrupt:
        _p("\n>>> 已停止。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
