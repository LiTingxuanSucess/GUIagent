"""Send one test message from personal WeChat to a searched contact."""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import mss
import mss.tools
import pyautogui
import win32clipboard
import win32con
import win32gui

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ocr_client import ocr_image_file


def _find_weixin_main() -> int:
    candidates: list[int] = []

    def _cb(hwnd: int, _: object) -> bool:
        try:
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            if cls == "Qt51514QWindowIcon" and title == "微信" and win32gui.IsWindowVisible(hwnd):
                candidates.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    if not candidates:
        return 0
    return max(candidates, key=lambda h: _area(win32gui.GetWindowRect(h)))


def _area(rect: tuple[int, int, int, int]) -> int:
    left, top, right, bottom = rect
    return max(0, right - left) * max(0, bottom - top)


def _activate(hwnd: int) -> None:
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    time.sleep(0.15)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        pyautogui.click((left + right) // 2, (top + bottom) // 2)
    time.sleep(0.25)


def _set_clipboard(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _paste(text: str) -> None:
    _set_clipboard(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)


def _click_ratio(rect: tuple[int, int, int, int], rx: float, ry: float) -> None:
    left, top, right, bottom = rect
    x = int(left + (right - left) * rx)
    y = int(top + (bottom - top) * ry)
    pyautogui.click(x, y)
    time.sleep(0.15)


def _capture_header(hwnd: int, out_dir: Path) -> tuple[Path, str]:
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = right - left
    height = bottom - top
    # Right chat header area; excludes the left conversation list.
    region = {
        "left": int(left + width * 0.34),
        "top": int(top + height * 0.02),
        "width": int(width * 0.62),
        "height": int(height * 0.14),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"wechat_header_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    with mss.mss() as sct:
        img = sct.grab(region)
        mss.tools.to_png(img.rgb, img.size, output=str(path))
    return path, ocr_image_file(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contact", required=True)
    parser.add_argument("--text", default="")
    parser.add_argument("--no-send", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "exports" / "wechat_sender_test")
    parser.add_argument("--search-rx", type=float, default=0.155)
    parser.add_argument("--search-ry", type=float, default=0.060)
    parser.add_argument("--composer-rx", type=float, default=0.665)
    parser.add_argument("--composer-ry", type=float, default=0.925)
    args = parser.parse_args()

    hwnd = _find_weixin_main()
    if not hwnd:
        raise RuntimeError("personal WeChat main window not found")

    text = args.text or f"企业微信自动化闭环测试 {datetime.now().strftime('%H:%M:%S')}"
    _activate(hwnd)
    rect = win32gui.GetWindowRect(hwnd)

    _click_ratio(rect, args.search_rx, args.search_ry)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.05)
    _paste(args.contact)
    time.sleep(0.8)
    pyautogui.press("enter")
    time.sleep(1.2)

    header_path, header_text = _capture_header(hwnd, args.out_dir)
    print(f"header_shot={header_path}")
    print(f"header_ocr={header_text!r}")
    if args.contact not in header_text:
        print(f"abort: header OCR does not contain contact {args.contact!r}", file=sys.stderr)
        return 3

    if args.no_send:
        print(f"no-send text={text}")
        return 0

    rect = win32gui.GetWindowRect(hwnd)
    _click_ratio(rect, args.composer_rx, args.composer_ry)
    _paste(text)
    pyautogui.press("enter")
    print(f"sent text={text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
