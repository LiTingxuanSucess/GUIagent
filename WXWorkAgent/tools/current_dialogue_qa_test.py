"""Run a narrow current-chat WeChat -> WeWork -> RevAI QA test.

This tool intentionally does not search contacts, switch conversations, launch
apps, or activate WeWork. It assumes the user already opened the target personal
WeChat chat and the matching WeWork chat.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import win32api
import win32con
import win32gui

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.actions import click_message_composer, paste_and_send
from src.capture import RegionRatios, capture_window_region_png
from src.ocr_client import ocr_image_file, warmup_ocr_service
from src.ocr_runtime import init_ocr_runtime
from src.revai_qa import RevaiQaClient
from src.window import find_wework_hwnd


WEWORK_CHAT_REGION = RegionRatios(left=0.31, top=0.10, width=0.67, height=0.82)


def _area(hwnd: int) -> int:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return 0
    return max(0, right - left) * max(0, bottom - top)


def _find_weixin_main() -> int:
    hits: list[int] = []

    def cb(hwnd: int, _: object) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if title == "微信" and cls.startswith("Qt"):
                hits.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return max(hits, key=_area) if hits else 0


def _visible_children(hwnd: int) -> list[int]:
    out: list[int] = []

    def cb(child: int, _: object) -> bool:
        try:
            if win32gui.IsWindowVisible(child):
                out.append(child)
        except Exception:
            pass
        return True

    win32gui.EnumChildWindows(hwnd, cb, None)
    return out


def _send_wechat_current(text: str, *, fanout: bool = False) -> list[int]:
    hwnd = _find_weixin_main()
    if not hwnd:
        raise RuntimeError("personal WeChat main window not found")
    children = _visible_children(hwnd)
    targets = ([hwnd] + children) if fanout else (children[:1] or [hwnd])
    sent: list[int] = []
    for target in targets:
        if not win32gui.IsWindow(target):
            continue
        for ch in text:
            win32api.PostMessage(target, win32con.WM_CHAR, ord(ch), 0)
            time.sleep(0.01)
        time.sleep(0.05)
        win32api.PostMessage(target, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
        win32api.PostMessage(target, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
        sent.append(target)
        if not fanout:
            break
    return sent


def _capture_wework_ocr(hwnd: int, out_dir: Path, label: str) -> tuple[Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    shot = out_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{label}.png"
    capture_window_region_png(hwnd, WEWORK_CHAT_REGION, shot)
    return shot, ocr_image_file(shot)


def _wait_for_ocr_contains(
    hwnd: int,
    out_dir: Path,
    label: str,
    needle: str,
    *,
    timeout_sec: float,
    poll_sec: float,
) -> tuple[Path, str, bool]:
    deadline = time.monotonic() + timeout_sec
    last: tuple[Path, str] | None = None
    while True:
        shot, text = _capture_wework_ocr(hwnd, out_dir, label)
        last = (shot, text)
        if needle in text:
            return shot, text, True
        if time.monotonic() >= deadline:
            return shot, text, False
        time.sleep(poll_sec)


def _reply_wework_current(hwnd: int, reply: str, from_bottom_ratio: float) -> None:
    click_message_composer(hwnd, from_bottom_ratio=from_bottom_ratio)
    paste_and_send(reply, hwnd=hwnd, from_bottom_ratio=from_bottom_ratio)


def _send_wechat_and_wait(
    hwnd: int,
    out_dir: Path,
    text: str,
    label: str,
    *,
    wait_sec: float,
    poll_sec: float,
    fanout: bool,
) -> tuple[Path, str, bool]:
    targets = _send_wechat_current(text, fanout=fanout)
    print(f"WeChat sent {text!r} targets={targets} fanout={fanout}", flush=True)
    shot, ocr_text, ok = _wait_for_ocr_contains(
        hwnd,
        out_dir,
        label,
        text[:4],
        timeout_sec=wait_sec,
        poll_sec=poll_sec,
    )
    if ok or fanout:
        return shot, ocr_text, ok

    print("WeWork OCR did not confirm customer turn; retrying WeChat send with fanout", flush=True)
    targets = _send_wechat_current(text, fanout=True)
    print(f"WeChat resent {text!r} targets={targets} fanout=True", flush=True)
    return _wait_for_ocr_contains(
        hwnd,
        out_dir,
        f"{label}_retry",
        text[:4],
        timeout_sec=wait_sec,
        poll_sec=poll_sec,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", default=f"current-dialogue-{datetime.now():%Y%m%d-%H%M%S}")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "exports" / "current_dialogue_qa")
    parser.add_argument("--continue-after-one", action="store_true")
    parser.add_argument("--send-first-turn", action="store_true")
    parser.add_argument("--single-price-turn", action="store_true")
    parser.add_argument("--single-mileage-turn", action="store_true")
    parser.add_argument("--first-turn", default="325i")
    parser.add_argument("--confirm-turn", default="1")
    parser.add_argument("--price-turn", default="机油机滤多少钱")
    parser.add_argument("--wait-sec", type=float, default=18.0)
    parser.add_argument("--poll-sec", type=float, default=2.0)
    parser.add_argument("--from-bottom-ratio", type=float, default=0.10)
    parser.add_argument("--wechat-fanout", action="store_true")
    args = parser.parse_args()

    init_ocr_runtime()
    warmup_ocr_service()
    wxwork = find_wework_hwnd()
    if not wxwork:
        raise RuntimeError("WeWork window not found")

    client = RevaiQaClient(session_prefix="wxwork-current-dialogue")
    transcript: list[dict[str, str]] = []

    def revai_turn(customer_text: str) -> str:
        reply = client.complete("", customer_text, session_id=args.session_id)
        transcript.append({"customer": customer_text, "assistant": reply})
        print(f"RevAI <= {customer_text}", flush=True)
        print(f"RevAI => {reply}", flush=True)
        return reply

    if args.single_price_turn or args.single_mileage_turn:
        for seed in (args.first_turn, args.confirm_turn):
            _ = revai_turn(seed)
        customer_turn = args.price_turn if args.single_price_turn else "20000公里，想做小保养"
        shot, text, ok = _send_wechat_and_wait(
            wxwork,
            args.out_dir,
            customer_turn,
            "after_single_turn",
            wait_sec=args.wait_sec,
            poll_sec=args.poll_sec,
            fanout=True,
        )
        print(f"WeWork OCR single turn ok={ok} shot={shot}", flush=True)
        print(text, flush=True)
        reply = revai_turn(customer_turn)
        _reply_wework_current(wxwork, reply, args.from_bottom_ratio)
        shot, text = _capture_wework_ocr(wxwork, args.out_dir, "after_single_reply")
        print(f"WeWork OCR single reply shot={shot}", flush=True)
        print(text, flush=True)
        return 0 if ok else 4

    if args.continue_after_one:
        _ = revai_turn(args.first_turn)
        confirm_reply = revai_turn(args.confirm_turn)
        _reply_wework_current(wxwork, confirm_reply, args.from_bottom_ratio)
        shot, text, ok = _wait_for_ocr_contains(
            wxwork,
            args.out_dir,
            "after_confirm_reply",
            "确认",
            timeout_sec=args.wait_sec,
            poll_sec=args.poll_sec,
        )
        print(f"WeWork OCR after confirm ok={ok} shot={shot}", flush=True)
        print(text, flush=True)
    elif args.send_first_turn:
        shot, text, ok = _send_wechat_and_wait(
            wxwork,
            args.out_dir,
            args.first_turn,
            "after_first_turn",
            wait_sec=args.wait_sec,
            poll_sec=args.poll_sec,
            fanout=args.wechat_fanout,
        )
        print(f"WeWork OCR first turn ok={ok} shot={shot}", flush=True)
        print(text, flush=True)
        reply = revai_turn(args.first_turn)
        _reply_wework_current(wxwork, reply, args.from_bottom_ratio)

    shot, text, ok = _send_wechat_and_wait(
        wxwork,
        args.out_dir,
        args.price_turn,
        "after_price_turn",
        wait_sec=args.wait_sec,
        poll_sec=args.poll_sec,
        fanout=args.wechat_fanout,
    )
    print(f"WeWork OCR price turn ok={ok} shot={shot}", flush=True)
    print(text, flush=True)
    price_reply = revai_turn(args.price_turn)
    _reply_wework_current(wxwork, price_reply, args.from_bottom_ratio)
    shot, text, ok = _wait_for_ocr_contains(
        wxwork,
        args.out_dir,
        "after_price_reply",
        "338",
        timeout_sec=args.wait_sec,
        poll_sec=args.poll_sec,
    )
    print(f"WeWork OCR price reply ok={ok} shot={shot}", flush=True)
    print(text, flush=True)
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
