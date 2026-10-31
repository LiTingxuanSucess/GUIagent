"""
企业微信 PC：搜索联系人 → 截取消息区域 → 本地 PaddleOCR → 百炼 Qwen 生成回复 → 粘贴发送。

前置：已登录企业微信；设置环境变量 DASHSCOPE_API_KEY；首次 OCR 会下载模型。

用法:
  cd WXWorkAgent
  copy config.example.yaml config.yaml
  py -3.11 wxwork_reply.py --contact "客户昵称或备注"
  py -3.11 wxwork_reply.py --contact "张三" --dry-run
  py -3.11 wxwork_reply.py --contact "张三" --no-send
  py -3.11 wxwork_reply.py --contact "张三" --text "hello world"
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from pathlib import Path

import yaml

from src.ocr_runtime import init_ocr_runtime

init_ocr_runtime()

from src.actions import click_message_composer, paste_and_send, search_and_open_contact
from src.capture import RegionRatios, capture_window_region_png
from src.dashscope_key import resolve_dashscope_api_key
from src.ocr_stable import ocr_stable_region
from src.ocr_client import warmup_ocr_service
from src.qwen_text import QwenTextClient
from src.window import activate_hwnd, find_wework_hwnd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

log = logging.getLogger(__name__)


def _load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing config: {path} (copy from config.example.yaml)")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="WXWork OCR + Qwen reply")
    parser.add_argument("--contact", required=True, help="Search box keyword (name / remark)")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Only capture + OCR, print text")
    parser.add_argument(
        "--text",
        default=None,
        help="Send this message as-is (skip OCR + Qwen). Example: --text \"hello world\"",
    )
    parser.add_argument("--no-send", action="store_true", help="Generate reply but do not paste/send")
    parser.add_argument("--save-shot", type=Path, default=None, help="Keep screenshot PNG at this path")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    cfg = _load_config(args.config)
    cap = cfg.get("capture") or {}
    reply_cap = cfg.get("reply_capture") or {}
    inp = cfg.get("input") or {}
    timing = cfg.get("timing") or {}
    qwen_cfg = cfg.get("qwen") or {}
    reply_cfg = cfg.get("reply") or {}

    ratios = RegionRatios(
        left=float(reply_cap.get("left_ratio", cap.get("left_ratio", 0.27))),
        top=float(reply_cap.get("top_ratio", 0.34)),
        width=float(reply_cap.get("width_ratio", cap.get("width_ratio", 0.72))),
        height=float(reply_cap.get("height_ratio", 0.30)),
    )
    from_bottom = float(inp.get("click_from_bottom_ratio", 0.10))
    after_search = float(timing.get("after_search_sec", 1.0))
    after_activate = float(timing.get("after_activate_sec", 0.35))
    watch_cfg = cfg.get("watch") or {}
    stable_samples = int(watch_cfg.get("stable_samples", 2))
    stable_sleep = float(watch_cfg.get("stable_sleep_sec", 0.30))

    hwnd = find_wework_hwnd()
    if not hwnd:
        log.error("未找到企业微信主窗口 (WeWorkWindow)。请先打开并登录。")
        return 2

    log.info("激活窗口…")
    activate_hwnd(hwnd)
    time.sleep(after_activate)

    log.info("搜索并打开会话: %s", args.contact)
    search_and_open_contact(args.contact, pause_after_enter=after_search, hwnd=hwnd)

    if args.text is not None:
        body = args.text
        if args.dry_run:
            log.info("--dry-run：将发送（未实际发送）: %s", body)
            return 0
        if args.no_send:
            log.info("--no-send：不发送: %s", body)
            return 0
        log.info("发送固定文本…")
        click_message_composer(hwnd, from_bottom_ratio=from_bottom)
        paste_and_send(body, hwnd=hwnd, from_bottom_ratio=from_bottom)
        log.info("已发送: %s", body)
        return 0

    log.info("预加载 OCR（首次会下载模型，请耐心等待）…")
    warmup_ocr_service()

    if args.save_shot:
        shot_path = args.save_shot
    else:
        shot_path = Path(tempfile.gettempdir()) / "wxwork_msg_region.png"

    if args.save_shot:
        full_path = shot_path.with_name(f"{shot_path.stem}.full{shot_path.suffix}")
        log.info("保存全窗口定位截图 → %s", full_path)
        capture_window_region_png(hwnd, RegionRatios(0.0, 0.0, 1.0, 1.0), full_path)

    log.info("等待消息区域稳定并截图 → %s", shot_path)
    keep_path = shot_path if args.save_shot else None
    log.info("PaddleOCR 识别中（首次会较慢）…")
    ocr_text, _shot_hash = ocr_stable_region(
        hwnd,
        ratios,
        stable_samples=stable_samples,
        stable_sleep_sec=stable_sleep,
        timeout_sec=max(8.0, stable_samples * stable_sleep * 8),
        keep_path=keep_path,
    )
    log.info("--- OCR ---\n%s\n---", ocr_text or "(空)")

    if args.dry_run:
        return 0

    system = reply_cfg.get("system") or "你是专业客服，用简短中文回复。"
    user_prompt = (
        "以下为当前会话消息区域 OCR 提取的文字（可能有错漏、顺序不保证）：\n\n"
        f"{ocr_text or '(未能识别到文字)'}\n\n"
        "请生成一条可直接发给客户的中文回复。"
    )

    api_key = resolve_dashscope_api_key(qwen_cfg)
    if not api_key:
        log.error(
            "未配置百炼 API Key：qwen.api_key、DASHSCOPE_API_KEY 或 PRAgent/config.yaml global.api_key"
        )
        return 1
    client = QwenTextClient(
        api_key=api_key,
        base_url=str(qwen_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
        model=str(qwen_cfg.get("model", "qwen-turbo")),
        temperature=float(qwen_cfg.get("temperature", 0.3)),
        timeout=int(qwen_cfg.get("timeout", 60)),
    )
    log.info("调用 Qwen 生成回复…")
    reply = client.complete(system, user_prompt)
    log.info("--- 回复 ---\n%s\n---", reply)

    if args.no_send:
        log.info("--no-send：未发送")
        return 0

    log.info("聚焦输入框并发送…")
    click_message_composer(hwnd, from_bottom_ratio=from_bottom)
    paste_and_send(reply, hwnd=hwnd, from_bottom_ratio=from_bottom)
    log.info("已发送。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
