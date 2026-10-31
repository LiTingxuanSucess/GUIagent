"""Probe whether WeWork message hover timestamps can be captured via OCR."""
from __future__ import annotations

import argparse
import ctypes
import json
import re
import sys
import time
from pathlib import Path

import mss
import mss.tools
import win32api
import win32gui

from src.ocr_runtime import init_ocr_runtime

init_ocr_runtime()

from src.actions import move_screen_point
from src.capture import RegionRatios, capture_window_region_png, window_client_rect_screen
from src.ocr_client import ocr_image_file, ocr_image_text_boxes, warmup_ocr_service
from src.window import activate_hwnd, ensure_wework_hwnd, is_same_process_foreground, window_process_id

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


CHAT_REGION = RegionRatios(left=0.31, top=0.10, width=0.67, height=0.64)
TIME_RE = re.compile(
    r"(\d{4}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?|"
    r"\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?|"
    r"(今天|昨天|星期[一二三四五六日天])\s*\d{1,2}:\d{2}(?::\d{2})?|"
    r"\d{1,2}:\d{2}(?::\d{2})?)"
)

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", INPUT_UNION)]


def _foreground_desc() -> str:
    try:
        fg = win32gui.GetForegroundWindow()
        return f"class={win32gui.GetClassName(fg)} title={win32gui.GetWindowText(fg)} pid={window_process_id(fg)}"
    except Exception as e:
        return f"unknown foreground: {e}"


def _wait_for_wework_foreground(hwnd: int, timeout_sec: float = 300.0) -> int:
    deadline = time.monotonic() + timeout_sec
    warned = False
    while time.monotonic() < deadline:
        if hwnd and win32gui.IsWindow(hwnd) and is_same_process_foreground(hwnd):
            return hwnd
        hwnd = ensure_wework_hwnd()
        if not warned:
            print(f">>> waiting for WeWork foreground. foreground={_foreground_desc()}", flush=True)
            warned = True
        time.sleep(1.0)
    raise RuntimeError("Timed out waiting for WeWork foreground")


def _screen_point_for_chat_box(hwnd: int, cx: float, cy: float) -> tuple[int, int]:
    left, top, right, bottom = window_client_rect_screen(hwnd)
    w = right - left
    h = bottom - top
    x = int(left + w * CHAT_REGION.left + cx)
    y = int(top + h * CHAT_REGION.top + cy)
    return x, y


def _interesting_boxes(hwnd: int, out_dir: Path, limit: int) -> list[tuple[str, int, int]]:
    chat = out_dir / "chat_before.png"
    capture_window_region_png(hwnd, CHAT_REGION, chat)
    boxes = ocr_image_text_boxes(chat)
    left, top, right, bottom = window_client_rect_screen(hwnd)
    region_w = max(1, int((right - left) * CHAT_REGION.width))
    region_h = max(1, int((bottom - top) * CHAT_REGION.height))
    picks: list[tuple[str, int, int]] = []
    for box in sorted(boxes, key=lambda b: b.cy):
        text = re.sub(r"\s+", " ", box.text).strip()
        if not text or TIME_RE.fullmatch(text) or len(text) <= 1:
            continue
        if box.cx > region_w * 0.72:
            continue
        if box.cy < region_h * 0.08:
            continue
        x, y = _screen_point_for_chat_box(hwnd, box.cx, box.cy)
        picks.append((text, x, y))
        if len(picks) >= limit:
            break
    return picks


def _capture_screen_png(out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with mss.mss() as sct:
        region = sct.monitors[0]
        shot = sct.grab(region)
        mss.tools.to_png(shot.rgb, shot.size, output=str(out_path))
    return out_path


def _capture_rect_png(region: dict[str, int], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with mss.mss() as sct:
        monitor = sct.monitors[0]
        left = max(monitor["left"], int(region["left"]))
        top = max(monitor["top"], int(region["top"]))
        right = min(monitor["left"] + monitor["width"], left + int(region["width"]))
        bottom = min(monitor["top"] + monitor["height"], top + int(region["height"]))
        clipped = {
            "left": left,
            "top": top,
            "width": max(1, right - left),
            "height": max(1, bottom - top),
        }
        shot = sct.grab(clipped)
        mss.tools.to_png(shot.rgb, shot.size, output=str(out_path))
    return out_path


def _capture_cursor_neighborhood(x: int, y: int, out_path: Path, *, width: int = 760, height: int = 360) -> Path:
    return _capture_rect_png(
        {
            "left": int(x - width // 2),
            "top": int(y - height // 2),
            "width": width,
            "height": height,
        },
        out_path,
    )


def _window_info(hwnd: int) -> dict[str, object]:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        left = top = right = bottom = 0
    return {
        "hwnd": int(hwnd),
        "class": win32gui.GetClassName(hwnd) if win32gui.IsWindow(hwnd) else "",
        "title": win32gui.GetWindowText(hwnd) if win32gui.IsWindow(hwnd) else "",
        "pid": window_process_id(hwnd),
        "visible": bool(win32gui.IsWindowVisible(hwnd)) if win32gui.IsWindow(hwnd) else False,
        "rect": [int(left), int(top), int(right), int(bottom)],
        "area": max(0, int(right - left)) * max(0, int(bottom - top)),
    }


def _same_process_top_windows(root_hwnd: int) -> dict[int, dict[str, object]]:
    root_pid = window_process_id(root_hwnd)
    found: dict[int, dict[str, object]] = {}

    def _cb(hwnd: int, _: object) -> bool:
        try:
            if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
                return True
            if window_process_id(hwnd) != root_pid:
                return True
            info = _window_info(hwnd)
            if int(info["area"]) > 0:
                found[int(hwnd)] = info
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return found


def _ocr_lines(path: Path) -> list[str]:
    return [s.strip() for s in ocr_image_file(path).splitlines() if s.strip()]


def _time_hits(lines: list[str]) -> list[str]:
    return [s for s in lines if TIME_RE.search(s)]


def _sendinput_relative_move(dx: int, dy: int) -> None:
    extra = ctypes.c_ulong(0)
    event = INPUT(
        type=INPUT_MOUSE,
        union=INPUT_UNION(
            mi=MOUSEINPUT(
                dx=int(dx),
                dy=int(dy),
                mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE,
                time=0,
                dwExtraInfo=ctypes.pointer(extra),
            )
        ),
    )
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))
    if sent != 1:
        raise ctypes.WinError(ctypes.get_last_error())


def _sendinput_absolute_move(x: int, y: int) -> None:
    vx = ctypes.windll.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = ctypes.windll.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = max(1, ctypes.windll.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    vh = max(1, ctypes.windll.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    ax = round((int(x) - vx) * 65535 / max(1, vw - 1))
    ay = round((int(y) - vy) * 65535 / max(1, vh - 1))
    extra = ctypes.c_ulong(0)
    event = INPUT(
        type=INPUT_MOUSE,
        union=INPUT_UNION(
            mi=MOUSEINPUT(
                dx=int(ax),
                dy=int(ay),
                mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                time=0,
                dwExtraInfo=ctypes.pointer(extra),
            )
        ),
    )
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))
    if sent != 1:
        raise ctypes.WinError(ctypes.get_last_error())


def _sendinput_hover_to(x: int, y: int, *, steps: int = 36, step_pause: float = 0.012, settle_sec: float = 1.6) -> tuple[int, int]:
    sx, sy = win32gui.GetCursorPos()
    for i in range(1, max(2, steps) + 1):
        nx = round(sx + (x - sx) * i / steps)
        ny = round(sy + (y - sy) * i / steps)
        _sendinput_absolute_move(nx, ny)
        time.sleep(step_pause)
    # Small real-input wiggle helps CEF refresh hover tracking without leaving the target.
    for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
        _sendinput_relative_move(dx, dy)
        time.sleep(0.03)
    time.sleep(settle_sec)
    return win32gui.GetCursorPos()


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe WeWork hover timestamp OCR")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--out-dir", type=Path, default=Path("exports") / "hover_time_probe")
    parser.add_argument("--hover-sec", type=float, default=1.2)
    parser.add_argument("--real-cursor", action="store_true", help="Use SetCursorPos for hover probing.")
    parser.add_argument("--sendinput-hover", action="store_true", help="Use SendInput absolute virtual-desktop mouse moves for hover probing.")
    parser.add_argument("--sendinput-steps", type=int, default=36)
    parser.add_argument("--local-ocr", action="store_true", help="OCR only the cursor neighborhood and popup windows.")
    parser.add_argument("--manual-hover", action="store_true", help="Wait while the user manually hovers, then capture OCR around the real cursor.")
    parser.add_argument("--manual-captures", type=int, default=1)
    parser.add_argument("--manual-delay", type=float, default=5.0)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(">>> OCR service warmup", flush=True)
    warmup_ocr_service()

    hwnd = ensure_wework_hwnd()
    if not hwnd:
        print(">>> WeWork window not found", flush=True)
        return 2
    activate_hwnd(hwnd)
    time.sleep(0.5)
    hwnd = _wait_for_wework_foreground(hwnd)

    picks = _interesting_boxes(hwnd, args.out_dir, args.limit)
    print(f">>> picked {len(picks)} text boxes", flush=True)
    base_full = args.out_dir / "screen_before.png"
    _capture_screen_png(base_full)
    base_text = set(_ocr_lines(base_full))
    results: list[dict[str, object]] = []

    if args.manual_hover:
        for i in range(1, args.manual_captures + 1):
            print(
                f">>> manual capture {i}/{args.manual_captures}: "
                f"hover the target message now; capturing in {args.manual_delay:.1f}s",
                flush=True,
            )
            time.sleep(args.manual_delay)
            hx, y = win32gui.GetCursorPos()
            print(f">>> manual hover point: ({hx},{y})", flush=True)
            local_shot = args.out_dir / f"manual_hover_{i:02d}_local.png"
            _capture_cursor_neighborhood(hx, y, local_shot)
            local_lines = _ocr_lines(local_shot)
            local_new_lines = [s for s in local_lines if s not in base_text]
            local_hits = _time_hits(local_lines)
            local_new_hits = _time_hits(local_new_lines)
            print(f"    local_new_lines={local_new_lines[:10]}", flush=True)
            print(f"    local_time_hits={local_hits[:10]}", flush=True)
            print(f"    local_new_time_hits={local_new_hits[:10]}", flush=True)
            results.append(
                {
                    "index": i,
                    "mode": "manual_hover",
                    "hover_point": [hx, y],
                    "local_shot": str(local_shot),
                    "local_lines": local_lines,
                    "local_new_lines": local_new_lines,
                    "local_time_hits": local_hits,
                    "local_new_time_hits": local_new_hits,
                }
            )
        summary = args.out_dir / "hover_time_results.json"
        summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f">>> shots written: {args.out_dir}", flush=True)
        print(f">>> result json: {summary}", flush=True)
        return 0

    for i, (text, x, y) in enumerate(picks, start=1):
        hwnd = _wait_for_wework_foreground(hwnd)
        offsets = [0, -80, 80, -160, 160] if args.real_cursor else [0]
        for j, dx in enumerate(offsets):
            hx = x + dx
            print(f">>> hover {i}.{j}: ({hx},{y}) {text[:40]}", flush=True)
            before_windows = _same_process_top_windows(hwnd)
            if args.sendinput_hover:
                actual_x, actual_y = _sendinput_hover_to(hx, y, steps=args.sendinput_steps, settle_sec=args.hover_sec)
            elif args.real_cursor:
                win32api.SetCursorPos((hx, y))
                time.sleep(args.hover_sec)
                actual_x, actual_y = win32gui.GetCursorPos()
            else:
                move_screen_point(hwnd, hx, y, pause_after=args.hover_sec)
                actual_x, actual_y = hx, y
            after_windows = _same_process_top_windows(hwnd)
            print(
                f"    actual_cursor=({actual_x},{actual_y}) delta=({actual_x - hx},{actual_y - y})",
                flush=True,
            )

            local_shot = args.out_dir / f"hover_{i:02d}_{j:02d}_local.png"
            _capture_cursor_neighborhood(actual_x, actual_y, local_shot)
            local_lines = _ocr_lines(local_shot)
            local_new_lines = [s for s in local_lines if s not in base_text]
            local_hits = _time_hits(local_lines)
            local_new_hits = _time_hits(local_new_lines)
            print(f"    local_new_lines={local_new_lines[:6]}", flush=True)
            print(f"    local_time_hits={local_hits[:8]}", flush=True)
            print(f"    local_new_time_hits={local_new_hits[:8]}", flush=True)

            popup_records: list[dict[str, object]] = []
            new_window_ids = [k for k in after_windows if k not in before_windows and k != hwnd]
            for k in new_window_ids:
                info = after_windows[k]
                left, top, right, bottom = [int(v) for v in info["rect"]]
                if right <= left or bottom <= top:
                    continue
                popup_shot = args.out_dir / f"hover_{i:02d}_{j:02d}_popup_{k}.png"
                _capture_rect_png(
                    {"left": left, "top": top, "width": right - left, "height": bottom - top},
                    popup_shot,
                )
                popup_lines = _ocr_lines(popup_shot)
                popup_record = {
                    **info,
                    "shot": str(popup_shot),
                    "lines": popup_lines,
                    "time_hits": _time_hits(popup_lines),
                }
                popup_records.append(popup_record)
                print(
                    f"    popup hwnd={k} class={info['class']} title={info['title']!r} "
                    f"time_hits={popup_record['time_hits'][:8]}",
                    flush=True,
                )

            shot = args.out_dir / f"hover_{i:02d}_{j:02d}.png"
            full_new_lines: list[str] = []
            full_time_hits: list[str] = []
            if not args.local_ocr:
                _capture_screen_png(shot)
                after_lines = _ocr_lines(shot)
                full_new_lines = [s for s in after_lines if s not in base_text]
                full_time_hits = _time_hits(after_lines)
                print(f"    full_new_lines={full_new_lines[:6]}", flush=True)
                print(f"    full_time_hits={full_time_hits[:8]}", flush=True)

            results.append(
                {
                    "index": i,
                    "offset_index": j,
                    "source_text": text,
                    "hover_point": [hx, y],
                    "actual_cursor": [actual_x, actual_y],
                    "cursor_delta": [actual_x - hx, actual_y - y],
                    "local_shot": str(local_shot),
                    "local_lines": local_lines,
                    "local_new_lines": local_new_lines,
                    "local_time_hits": local_hits,
                    "local_new_time_hits": local_new_hits,
                    "full_shot": str(shot) if not args.local_ocr else None,
                    "full_new_lines": full_new_lines,
                    "full_time_hits": full_time_hits,
                    "new_windows": popup_records,
                }
            )

    summary = args.out_dir / "hover_time_results.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f">>> shots written: {args.out_dir}", flush=True)
    print(f">>> result json: {summary}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
