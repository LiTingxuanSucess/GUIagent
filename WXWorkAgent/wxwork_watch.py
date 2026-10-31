"""
轮询监听当前会话：截图 hash 变化 → 稳定采样 OCR → diff 出新内容 → Qwen 回复 → 发送。

用法:
  set DASHSCOPE_API_KEY=...
  cd WXWorkAgent
  py -3.11 wxwork_watch.py --contact "ltx"

Ctrl+C 退出。首次运行会先建立「基线」不自动回复，避免把历史记录当新消息。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from src.ocr_runtime import init_ocr_runtime

init_ocr_runtime()

import yaml

from src.actions import click_message_composer, paste_and_send, search_and_open_contact
from src.capture import (
    RegionRatios,
    capture_window_region_hash,
    mss_region_for_ratios,
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


def _wework_is_foreground(hwnd: int) -> bool:
    """
    Return True if the foreground window belongs to WeWork's process.
    Used to distinguish "new message arrived" (WeWork active) from
    "another app covered the capture region" (user switched away).
    Fails open (returns True) so we never suppress legitimate messages.
    """
    return is_same_process_foreground(hwnd)


def _load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing config: {path} (copy from config.example.yaml)")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _ocr_stable(
    capture_once: Callable[[], tuple[Path, str]],
    samples: int,
    sleep_sec: float,
    max_rounds: int = 40,
) -> tuple[str, str]:
    """Require `samples` consecutive identical OCR results; fresh capture between checks. Returns (text, sha256)."""
    prev_text: str | None = None
    streak = 0
    last_path: Path | None = None
    last_hash = ""
    rounds = 0
    try:
        while streak < samples and rounds < max_rounds:
            rounds += 1
            path, h = capture_once()
            if last_path is not None:
                last_path.unlink(missing_ok=True)
            last_path = path
            last_hash = h
            text = ocr_image_file(path)
            if text == prev_text:
                streak += 1
            else:
                streak = 1
                prev_text = text
            if streak < samples:
                time.sleep(sleep_sec)
        if streak < samples or prev_text is None:
            raise RuntimeError(f"OCR 在 {max_rounds} 次内未稳定（需连续 {samples} 次相同）")
        if last_path is not None:
            last_path.unlink(missing_ok=True)
            last_path = None
        return prev_text, last_hash
    except Exception:
        if last_path is not None:
            last_path.unlink(missing_ok=True)
        raise


def _sync_after_send(
    hwnd: int,
    ratios: RegionRatios,
    stable_samples: int,
    stable_sleep: float,
    pause_sec: float,
) -> tuple[str, str]:
    """Re-capture after our message lands; returns (ocr_text, sha256)."""
    time.sleep(pause_sec)
    activate_hwnd(hwnd)
    time.sleep(0.2)

    return ocr_stable_region(
        hwnd,
        ratios,
        stable_samples=stable_samples,
        stable_sleep_sec=stable_sleep,
        timeout_sec=max(8.0, stable_samples * stable_sleep * 8),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="WXWork poll: OCR diff + Qwen auto-reply")
    parser.add_argument("--contact", required=True, help="Search keyword to open the chat")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "config.yaml")
    parser.add_argument("--once", action="store_true", help="Run a single poll cycle then exit")
    parser.add_argument(
        "--debug-shot",
        type=Path,
        default=None,
        help="基线截取后把当前消息区 PNG 存到此路径（与监听用同一区域，便于肉眼看是否在框内）",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    cfg = _load_config(args.config)
    cap_cfg = cfg.get("capture") or {}
    inp = cfg.get("input") or {}
    timing = cfg.get("timing") or {}
    qwen_cfg = cfg.get("qwen") or {}
    reply_cfg = cfg.get("reply") or {}
    watch = cfg.get("watch") or {}

    ratios = RegionRatios(
        left=float(cap_cfg.get("left_ratio", 0.27)),
        top=float(cap_cfg.get("top_ratio", 0.12)),
        width=float(cap_cfg.get("width_ratio", 0.72)),
        height=float(cap_cfg.get("height_ratio", 0.55)),
    )
    from_bottom = float(inp.get("click_from_bottom_ratio", 0.10))
    after_search = float(timing.get("after_search_sec", 1.0))
    after_activate = float(timing.get("after_activate_sec", 0.35))

    interval = float(watch.get("interval_sec", 5.0))
    stable_samples = int(watch.get("stable_samples", 2))
    stable_sleep = float(watch.get("stable_sleep_sec", 0.30))
    min_new_chars = int(watch.get("min_new_chars", 2))
    rescan_every = int(watch.get("rescan_contact_every", 0))
    post_send_pause = float(watch.get("post_send_pause_sec", 1.2))

    system = reply_cfg.get("system") or "你是专业客服，用简短中文回复。"

    api_key = resolve_dashscope_api_key(qwen_cfg)
    if not api_key:
        log.error(
            "未配置百炼 API Key：WXWork config qwen.api_key、环境变量 DASHSCOPE_API_KEY，"
            "或 gui-agent/PRAgent/config.yaml 的 global.api_key。"
        )
        return 1

    client = QwenTextClient(
        api_key=api_key,
        base_url=str(qwen_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
        model=str(qwen_cfg.get("model", "qwen-turbo")),
        temperature=float(qwen_cfg.get("temperature", 0.3)),
        timeout=int(qwen_cfg.get("timeout", 60)),
    )

    _p = lambda s: (print(s, flush=True), sys.stdout.flush())  # noqa: E731

    _p(">>> OCR 预热中…")
    try:
        warmup_ocr_service()
    except Exception as e:
        print(f">>> OCR 预热失败: {e}", flush=True)
        log.exception("OCR 预热失败: %s", e)
        return 3

    hwnd = find_wework_hwnd()
    if not hwnd:
        print(">>> 未找到企业微信主窗口，请先打开并登录！", flush=True)
        return 2

    print(f">>> 激活窗口，搜索联系人: {args.contact}", flush=True)
    activate_hwnd(hwnd)
    time.sleep(after_activate)
    search_and_open_contact(args.contact, pause_after_enter=after_search, hwnd=hwnd)

    try:
        reg = mss_region_for_ratios(hwnd, ratios)
        print(
            f">>> 截取区域(屏幕物理像素): left={reg['left']} top={reg['top']} "
            f"width={reg['width']} height={reg['height']}",
            flush=True,
        )
    except Exception as e:
        print(f">>> 截取区域计算失败: {e}", flush=True)

    print(">>> 建立基线截图（本轮不回复）…", flush=True)
    debug_path = args.debug_shot.resolve() if args.debug_shot else None
    baseline_text, h0 = ocr_stable_region(
        hwnd,
        ratios,
        stable_samples=stable_samples,
        stable_sleep_sec=stable_sleep,
        timeout_sec=max(8.0, stable_samples * stable_sleep * 8),
        keep_path=debug_path,
    )
    if debug_path:
        print(f">>> 调试图已保存: {args.debug_shot}", flush=True)

    last_full_ocr = baseline_text
    last_hash = h0
    print(f">>> 基线 hash={h0[:16]}…  基线OCR({len(baseline_text)}字): {baseline_text[:60]!r}", flush=True)
    last_outgoing = ""
    loop_i = 0

    print(f">>> 监听中，每 {interval:.1f}s 检查一次（前台非 WeWork 时暂停 OCR），Ctrl+C 退出", flush=True)

    try:
        while True:
            time.sleep(interval)
            hwnd = find_wework_hwnd()
            if not hwnd:
                log.warning("主窗口丢失，%ds 后重试…", int(interval))
                continue

            # 仅解除最小化，不抢键盘焦点，用户可在其它窗口正常工作
            ensure_visible(hwnd)
            time.sleep(0.15)

            if rescan_every > 0 and loop_i > 0 and loop_i % rescan_every == 0:
                log.info("重新搜索联系人…")
                activate_hwnd(hwnd)
                time.sleep(after_activate)
                search_and_open_contact(args.contact, pause_after_enter=after_search, hwnd=hwnd)

            h_quick = capture_window_region_hash(hwnd, ratios)

            if h_quick == last_hash:
                print(
                    f"  [{loop_i+1}] hash 未变 {h_quick[:12]}…  (无新消息)",
                    flush=True,
                )
                loop_i += 1
                if args.once:
                    return 0
                continue

            # Hash changed — check if it's a real WeWork update or just
            # another app covering the capture region (user switched away).
            if not _wework_is_foreground(hwnd):
                print(
                    f"  [{loop_i+1}] hash 变化但 WeWork 非前台，"
                    "疑似切换到其它 App，暂停 OCR（等待切回）…",
                    flush=True,
                )
                # Do NOT update last_hash: when the user returns, the old
                # hash vs current hash will still differ and trigger OCR.
                loop_i += 1
                if args.once:
                    return 0
                continue

            print(f"  [{loop_i+1}] hash 变化 → {h_quick[:12]}…  开始 OCR", flush=True)
            try:
                text, h_stab = ocr_stable_region(
                    hwnd,
                    ratios,
                    stable_samples=stable_samples,
                    stable_sleep_sec=stable_sleep,
                    timeout_sec=max(8.0, stable_samples * stable_sleep * 8),
                )
            except Exception as e:
                log.exception("OCR 失败: %s", e)
                continue

            if text == last_full_ocr:
                last_hash = h_stab
                loop_i += 1
                if args.once:
                    return 0
                continue

            new_part = appended_content(last_full_ocr, text)
            print(f"  新增OCR: {new_part!r}", flush=True)

            if not new_part or len(new_part.strip()) < min_new_chars:
                log.debug("新增过短或 OCR 漂移，同步基线")
                last_full_ocr = text
                last_hash = h_stab
                loop_i += 1
                if args.once:
                    return 0
                continue

            if looks_like_echo(new_part, last_outgoing):
                log.info("跳过：疑似己方刚发送的内容进入 OCR")
                last_full_ocr = text
                last_hash = h_stab
                loop_i += 1
                if args.once:
                    return 0
                continue

            user_prompt = (
                "以下为会话区 OCR 全文（可能有错漏）：\n\n"
                f"{text}\n\n"
                "其中「相对上一轮快照新增」部分很可能是客户刚发的：\n"
                f"{new_part}\n\n"
                "请生成一条可直接发给客户的中文回复（只输出正文）。"
            )
            print("  调用 Qwen…", flush=True)
            try:
                reply = client.complete(system, user_prompt)
            except Exception as e:
                print(f"  Qwen 失败，跳过: {e}", flush=True)
                last_full_ocr = text
                last_hash = h_stab
                loop_i += 1
                continue
            print(f"  Qwen 回复: {reply!r}", flush=True)

            # 发送前才抢焦点（click 输入框 + 粘贴）
            activate_hwnd(hwnd)
            time.sleep(0.2)
            click_message_composer(hwnd, from_bottom_ratio=from_bottom)
            paste_and_send(reply, hwnd=hwnd, from_bottom_ratio=from_bottom)
            last_outgoing = reply

            last_full_ocr, last_hash = _sync_after_send(
                hwnd, ratios, stable_samples, stable_sleep, post_send_pause
            )
            print("  已发送，同步基线…", flush=True)

            loop_i += 1
            if args.once:
                return 0

    except KeyboardInterrupt:
        log.info("已停止。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
