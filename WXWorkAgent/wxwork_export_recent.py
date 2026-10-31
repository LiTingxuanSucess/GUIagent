"""Export recent chat text from external WeWork conversations.

The script scans the left conversation list, exports conversations that look
like customer chats/groups, scrolls upward inside each chat to collect recent
messages, and writes both exported and skipped conversation records.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import win32gui
import win32process
import mss
import mss.tools
from PIL import Image

from src.ocr_runtime import init_ocr_runtime

init_ocr_runtime()

from src.actions import click_client_ratio, click_screen_point, scroll_client_ratio
from src.capture import (
    RegionRatios,
    capture_window_region_hash,
    capture_window_region_png,
    window_client_rect_screen,
)
from src.ocr_client import ocr_image_file, ocr_image_text_boxes, warmup_ocr_service
from src.window import activate_hwnd, ensure_wework_hwnd, is_same_process_foreground, window_process_id

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


LIST_REGION = RegionRatios(left=0.065, top=0.08, width=0.235, height=0.86)
CHAT_REGION = RegionRatios(left=0.31, top=0.10, width=0.67, height=0.64)
DEFAULT_SKIP_KEYWORDS = ["行业资讯", "企业微信团队", "邮件提醒", "人事助手", "企业邮箱", "服务通知"]
DEFAULT_REQUIRED_MARKERS = ["@微信", "外部"]


TIME_MARKER_RE = re.compile(
    r"(\d{4}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2}|"
    r"\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}|"
    r"(今天|昨天|星期[一二三四五六日天])\s*\d{1,2}:\d{2}|"
    r"\d{1,2}:\d{2})"
)
HOVER_TIME_RE = re.compile(
    r"(\d{4}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2}:\d{2}|"
    r"\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}:\d{2}|"
    r"\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2}:\d{2})"
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


@dataclass
class ConversationExport:
    index: int
    title: str
    list_preview: str
    messages: list[str]
    screenshots: list[str]
    structured_messages: list[dict] = field(default_factory=list)


@dataclass
class SkippedConversation:
    page_index: int
    title: str
    list_preview: str
    y_ratio: float
    reason: str


@dataclass
class ListCandidate:
    page_index: int
    row_index: int
    title: str
    list_preview: str
    y_ratio: float
    row_key: str


def _norm_line(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _filter_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    noise = {
        "普通话",
        "立即重启",
        "取消",
        "检测到你屏幕发生变化，可重启企业微信获得",
        "更佳的界面显示",
    }
    for raw in lines:
        s = _norm_line(raw)
        if not s:
            continue
        if s in noise:
            continue
        if re.fullmatch(r"(星期[一二三四五六日天]|昨天|今天|\d{1,2}:\d{2})", s):
            continue
        if out and out[-1] == s:
            continue
        out.append(s)
    return out


def _has_any(text: str, needles: list[str]) -> bool:
    return any(x and x in text for x in needles)


def _skip_reason(title: str, preview: str, skip_keywords: list[str], required_markers: list[str]) -> str | None:
    hay = f"{title} {preview}"
    if title.startswith("visible_row_"):
        return "unidentified"
    if title in {"外部", "@微信"}:
        return "marker_as_title"
    if len(_row_key(title, preview)) <= 1:
        return "suspicious_short_title"
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,2}", title):
        return "suspicious_short_title"
    if _has_any(hay, skip_keywords):
        return "skip_keyword"
    if required_markers and not _has_any(hay, required_markers):
        return "missing_required_marker"
    return None


def _merge_visible_snapshots(snapshots: list[list[str]], limit: int) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    # Newest viewport is captured first. Reverse snapshots to approximate
    # chronological order, then keep the newest tail.
    for lines in reversed(snapshots):
        for line in lines:
            key = _norm_line(line)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(line)
    return merged[-limit:]


def _conversation_rows(hwnd: int, count: int, out_dir: Path) -> list[tuple[str, str, float]]:
    """Return visible rows as (title, preview, y_ratio)."""
    shot = out_dir / "conversation_list.png"
    capture_window_region_png(hwnd, LIST_REGION, shot)
    boxes = ocr_image_text_boxes(shot)

    left, top, right, bottom = window_client_rect_screen(hwnd)
    region_h = max(1, int((bottom - top) * LIST_REGION.height))
    rows: list[list[str]] = []
    centers: list[float] = []
    for box in sorted(boxes, key=lambda b: b.cy):
        text = _norm_line(box.text)
        if not text:
            continue
        if not rows or abs(box.cy - centers[-1]) > 38:
            rows.append([text])
            centers.append(box.cy)
        else:
            rows[-1].append(text)
            centers[-1] = (centers[-1] + box.cy) / 2

    result: list[tuple[str, str, float]] = []
    for row, cy in zip(rows, centers):
        y_ratio = LIST_REGION.top + (cy / region_h) * LIST_REGION.height
        if y_ratio < 0.10 or y_ratio > 0.97:
            continue
        title = row[0]
        preview = " ".join(row[1:3]) if len(row) > 1 else ""
        result.append((title, preview, y_ratio))
        if len(result) >= count:
            break

    if not result:
        fixed_centers = [0.155 + i * 0.115 for i in range(min(count, 8))]
        result = [(f"visible_row_{i + 1}", "", y_ratio) for i, y_ratio in enumerate(fixed_centers)]
    return result


def _collect_visible_rows(hwnd: int, count: int, out_dir: Path, *, page_index: int) -> list[tuple[str, str, float]]:
    page_dir = out_dir / f"list_page_{page_index:02d}"
    page_dir.mkdir(parents=True, exist_ok=True)
    return _conversation_rows(hwnd, count, page_dir)


def _row_key(title: str, preview: str) -> str:
    key = _norm_line(title)
    if key.startswith("visible_row_"):
        key = _norm_line(f"{title} {preview}")
    key = re.sub(r"外部|@微信|\.{2,}|…+|\d{1,2}:\d{2}|星期[一二三四五六日天]|昨天|今天|\d{2}/\d{2}", "", key)
    key = re.sub(r"[^\w\u4e00-\u9fff]+", "", key)
    return key[:48]


def _preview_key(preview: str) -> str:
    key = _norm_line(preview)
    key = re.sub(r"\.{2,}|…+|\d{1,2}:\d{2}|星期[一二三四五六日天]|昨天|今天|\d{2}/\d{2}", "", key)
    key = re.sub(r"[^\w\u4e00-\u9fff]+", "", key)
    return key[:48]


def _overlap_keys(
    prev_rows: list[tuple[str, str, float]],
    next_rows: list[tuple[str, str, float]],
    *,
    edge_rows: int = 8,
) -> set[str]:
    prev = {_row_key(title, preview) for title, preview, _y in prev_rows[-edge_rows:]}
    nxt = {_row_key(title, preview) for title, preview, _y in next_rows[:edge_rows]}
    prev.discard("")
    nxt.discard("")
    return prev & nxt


def _ensure_live_hwnd(hwnd: int) -> int:
    if hwnd and win32gui.IsWindow(hwnd):
        try:
            window_client_rect_screen(hwnd)
            return hwnd
        except Exception:
            pass
    new_hwnd = ensure_wework_hwnd()
    if not new_hwnd:
        raise RuntimeError("WeWork window not found")
    activate_hwnd(new_hwnd)
    time.sleep(0.5)
    return new_hwnd


def _foreground_desc() -> str:
    try:
        fg = win32gui.GetForegroundWindow()
        return f"class={win32gui.GetClassName(fg)} title={win32gui.GetWindowText(fg)} pid={window_process_id(fg)}"
    except Exception as e:
        return f"unknown foreground: {e}"


def _wait_for_wework_foreground(
    hwnd: int,
    *,
    timeout_sec: float = 300.0,
    poll_sec: float = 1.0,
    allow_background_visible: bool = False,
) -> int:
    """Pause while another app is foreground unless background-visible capture is allowed."""
    deadline = time.monotonic() + timeout_sec
    warned = False
    while True:
        hwnd = _ensure_live_hwnd(hwnd)
        if allow_background_visible:
            return hwnd
        try:
            if win32gui.GetClassName(hwnd) == "WeWorkWindow" and is_same_process_foreground(hwnd):
                return hwnd
        except Exception:
            pass

        if not warned:
            print(f">>> WeWork is not foreground; waiting. foreground={_foreground_desc()}", flush=True)
            warned = True
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out waiting for WeWork to return to foreground")
        time.sleep(poll_sec)


def _rows_after_cursor(
    previous_rows: list[tuple[str, str, float]],
    current_rows: list[tuple[str, str, float]],
) -> tuple[list[tuple[str, str, float]], int]:
    """Return only rows after the last overlap with the previous page."""
    if not previous_rows:
        return current_rows, 0

    previous_keys = {_row_key(title, preview) for title, preview, _y in previous_rows}
    previous_keys.discard("")
    last_overlap_idx = -1
    for idx, (title, preview, _y) in enumerate(current_rows):
        key = _row_key(title, preview)
        if key and key in previous_keys:
            last_overlap_idx = idx

    if last_overlap_idx < 0:
        return [], 0
    return current_rows[last_overlap_idx + 1 :], last_overlap_idx + 1


def _advance_conversation_list(hwnd: int, *, clicks: int = -1, wheel_delta: int = 80) -> bool:
    """Scroll the conversation list down one small step and return whether the image changed."""
    before = capture_window_region_hash(hwnd, LIST_REGION)
    for _ in range(8):
        try:
            scroll_client_ratio(
                hwnd,
                0.18,
                0.50,
                clicks=clicks,
                pause_after=0.15,
                wheel_delta=wheel_delta,
                fanout=False,
            )
        except RuntimeError as e:
            print(f">>> list advance scroll skipped: {e}", flush=True)
            return False
        after = capture_window_region_hash(hwnd, LIST_REGION)
        if after != before:
            time.sleep(0.35)
            return True
    time.sleep(0.35)
    return False


def _reset_conversation_list_to_top(hwnd: int) -> None:
    """Scroll the left conversation list upward to the newest/top rows."""
    for _ in range(6):
        try:
            scroll_client_ratio(hwnd, 0.18, 0.45, clicks=8, pause_after=0.10)
        except Exception as e:
            print(f">>> list reset scroll skipped: {e}", flush=True)
            break
    time.sleep(0.3)


def _scan_conversation_list(
    hwnd: int,
    *,
    target_count: int,
    out_dir: Path,
    skip_keywords: list[str],
    required_markers: list[str],
    list_scroll_clicks: int,
    list_wheel_delta: int,
    max_pages: int,
    extra_pages_after_target: int,
) -> tuple[list[ListCandidate], list[SkippedConversation], int]:
    candidates: list[ListCandidate] = []
    candidates_by_key: dict[str, ListCandidate] = {}
    skipped: list[SkippedConversation] = []
    seen_keys: set[str] = set()
    previous_visible: list[tuple[str, str, float]] = []
    stale_pages = 0
    page_index = 0
    pages_after_target = 0

    while page_index < max_pages:
        hwnd = _ensure_live_hwnd(hwnd)
        visible = _collect_visible_rows(hwnd, 8, out_dir, page_index=page_index)
        if not visible:
            break

        if previous_visible:
            overlap = _overlap_keys(previous_visible, visible)
            if overlap:
                print(f">>> scan page {page_index}: overlap ok {len(overlap)} row(s)", flush=True)
            else:
                print(f">>> scan page {page_index}: warning no overlap", flush=True)
        else:
            print(">>> scan page 0", flush=True)

        for row_index, (title, preview, y_ratio) in enumerate(visible):
            row_key = _row_key(title, preview)
            if not row_key:
                continue
            if row_key in candidates_by_key:
                existing = candidates_by_key[row_key]
                if existing.y_ratio > 0.88 and y_ratio <= 0.88:
                    existing.page_index = page_index
                    existing.row_index = row_index
                    existing.title = title
                    existing.list_preview = preview
                    existing.y_ratio = y_ratio
                    print(
                        f">>> scan update safe row page={page_index} row={row_index + 1}: {title}",
                        flush=True,
                    )
                continue
            if row_key in seen_keys:
                continue
            seen_keys.add(row_key)

            reason = _skip_reason(title, preview, skip_keywords, required_markers)
            if reason:
                print(f">>> scan skip {reason}: {title}", flush=True)
                skipped.append(SkippedConversation(page_index, title, preview, y_ratio, reason))
                continue

            candidate = ListCandidate(page_index, row_index, title, preview, y_ratio, row_key)
            candidates.append(candidate)
            candidates_by_key[row_key] = candidate
            print(
                f">>> scan keep #{len(candidates)} page={page_index} row={row_index + 1}: {title}",
                flush=True,
            )

        if len(candidates) >= target_count:
            pages_after_target += 1
            if pages_after_target > extra_pages_after_target:
                candidates = candidates[:target_count]
                break

        if len(candidates) >= target_count and page_index + 1 >= max_pages:
            break

        previous_visible = visible
        try:
            hwnd = _ensure_live_hwnd(hwnd)
            moved = _advance_conversation_list(
                hwnd,
                clicks=list_scroll_clicks,
                wheel_delta=list_wheel_delta,
            )
        except Exception as e:
            print(f">>> list scan advance failed, stopping scan: {e}", flush=True)
            break
        if moved:
            stale_pages = 0
        else:
            stale_pages += 1
            print(f">>> scan list page did not move ({stale_pages}/2)", flush=True)
            if stale_pages >= 2:
                break
        page_index += 1

    return candidates, skipped, page_index


def _advance_to_page(
    hwnd: int,
    *,
    current_page: int,
    target_page: int,
    list_scroll_clicks: int,
    list_wheel_delta: int,
) -> int:
    while current_page < target_page:
        hwnd = _ensure_live_hwnd(hwnd)
        if not _advance_conversation_list(hwnd, clicks=list_scroll_clicks, wheel_delta=list_wheel_delta):
            raise RuntimeError(f"Cannot advance list to page {target_page}")
        current_page += 1
    return current_page


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


def _sendinput_hover_to(x: int, y: int, *, steps: int = 36, step_pause: float = 0.01, settle_sec: float = 1.0) -> tuple[int, int]:
    sx, sy = win32gui.GetCursorPos()
    for i in range(1, max(2, steps) + 1):
        nx = round(sx + (x - sx) * i / steps)
        ny = round(sy + (y - sy) * i / steps)
        _sendinput_absolute_move(nx, ny)
        time.sleep(step_pause)
    time.sleep(settle_sec)
    return win32gui.GetCursorPos()


def _capture_cursor_neighborhood(x: int, y: int, out_path: Path, *, width: int = 760, height: int = 360) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with mss.mss() as sct:
        monitor = sct.monitors[0]
        left = max(monitor["left"], int(x - width // 2))
        top = max(monitor["top"], int(y - height // 2))
        right = min(monitor["left"] + monitor["width"], left + width)
        bottom = min(monitor["top"] + monitor["height"], top + height)
        shot = sct.grab({"left": left, "top": top, "width": max(1, right - left), "height": max(1, bottom - top)})
        mss.tools.to_png(shot.rgb, shot.size, output=str(out_path))
    return out_path


def _ocr_lines(path: Path) -> list[str]:
    return [s.strip() for s in ocr_image_file(path).splitlines() if s.strip()]


def _screen_point_for_chat_box(hwnd: int, cx: float, cy: float) -> tuple[int, int]:
    left, top, right, bottom = window_client_rect_screen(hwnd)
    w = right - left
    h = bottom - top
    return int(left + w * CHAT_REGION.left + cx), int(top + h * CHAT_REGION.top + cy)


def _message_type(text: str) -> str:
    s = _norm_line(text)
    if not s:
        return "empty"
    if "撤回" in s or "鎾ゅ洖" in s:
        return "recalled"
    if "语音通话" in s or "通话时长" in s or "璇煶" in s:
        return "voice_call"
    if "视频通话" in s or "瑙嗛" in s:
        return "video_call"
    if "图片已过期" in s or "鍥剧墖宸茶繃鏈" in s:
        return "image_expired"
    if "图片" in s or "鍥剧墖" in s:
        return "image"
    if "文件" in s or "鏂囦欢" in s:
        return "file"
    if "链接" in s or "http" in s.lower():
        return "link"
    return "text"


def _is_self_bubble_pixel(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    return b >= 180 and g >= 145 and b - r >= 35 and g - r >= 10


def _self_bubble_ratio(img: Image.Image, cx: float, cy: float) -> float:
    w, h = img.size
    x0 = int(round(cx))
    y0 = int(round(cy))
    total = 0
    blue = 0
    for dy in (-28, -22, -16, -10, 10, 16, 22, 28):
        y = max(0, min(h - 1, y0 + dy))
        for dx in range(-140, 141, 20):
            x = max(0, min(w - 1, x0 + dx))
            total += 1
            if _is_self_bubble_pixel(img.getpixel((x, y))):
                blue += 1
    return blue / max(total, 1)


def _message_direction(img: Image.Image, region_w: int, cx: float, cy: float) -> str:
    blue_ratio = _self_bubble_ratio(img, cx, cy)
    if blue_ratio >= 0.08:
        return "outgoing"
    return "incoming" if cx < region_w * 0.58 else "outgoing"


def _is_time_marker(text: str) -> bool:
    return bool(TIME_MARKER_RE.fullmatch(_norm_line(text)))


def _extract_hover_time(lines: list[str]) -> str | None:
    for line in lines:
        m = HOVER_TIME_RE.search(_norm_line(line))
        if m:
            return m.group(1)
    return None


def _click_back_to_latest_if_present(hwnd: int, out_dir: Path, conv_index: int) -> bool:
    shot = out_dir / f"conv_{conv_index:02d}_back_to_latest_probe.png"
    capture_window_region_png(hwnd, CHAT_REGION, shot)
    boxes = ocr_image_text_boxes(shot)
    for box in boxes:
        text = _norm_line(box.text)
        if "回到最新位置" in text or "最新位置" in text:
            x, y = _screen_point_for_chat_box(hwnd, box.cx, box.cy)
            print(f"    click back-to-latest: {text}", flush=True)
            click_screen_point(hwnd, x, y, pause_after=0.8)
            return True
    return False


def _structured_from_boxes(
    hwnd: int,
    shot: Path,
    conv_index: int,
    page_index: int,
    *,
    include_message_time: bool,
    hover_dir: Path,
) -> list[dict]:
    boxes = ocr_image_text_boxes(shot)
    left, top, right, bottom = window_client_rect_screen(hwnd)
    region_w = max(1, int((right - left) * CHAT_REGION.width))
    region_h = max(1, int((bottom - top) * CHAT_REGION.height))
    out: list[dict] = []
    last_sender: str | None = None
    with Image.open(shot) as raw_img:
        img = raw_img.convert("RGB")
        for box_index, box in enumerate(sorted(boxes, key=lambda b: (b.cy, b.cx))):
            text = _norm_line(box.text)
            if not text:
                continue
            if box.cy < region_h * 0.03 or box.cy > region_h * 0.98:
                continue
            if box.cx > region_w * 0.94:
                continue
            x, y = _screen_point_for_chat_box(hwnd, box.cx, box.cy)
            item_id = f"c{conv_index:02d}p{page_index:02d}b{box_index:03d}"
            if _is_time_marker(text):
                out.append(
                    {
                        "id": item_id,
                        "kind": "time",
                        "message_type": "time_marker",
                        "timestamp_visible": text,
                        "timestamp": None,
                        "sender": None,
                        "direction": "system",
                        "text": text,
                        "screen_point": [x, y],
                        "ocr_center": [round(box.cx, 1), round(box.cy, 1)],
                        "source_screenshot": str(shot),
                    }
                )
                continue
            if "@微信" in text or "@寰俊" in text:
                last_sender = text
                continue

            direction = _message_direction(img, region_w, box.cx, box.cy)
            timestamp = None
            hover_shot = None
            cursor_delta = None
            if include_message_time and direction in {"incoming", "outgoing"} and _message_type(text) != "empty":
                try:
                    actual_x, actual_y = _sendinput_hover_to(x, y, settle_sec=1.15)
                    cursor_delta = [actual_x - x, actual_y - y]
                    hover_shot_path = hover_dir / f"{item_id}_hover.png"
                    _capture_cursor_neighborhood(actual_x, actual_y, hover_shot_path, width=760, height=360)
                    hover_shot = str(hover_shot_path)
                    timestamp = _extract_hover_time(_ocr_lines(hover_shot_path))
                    if timestamp:
                        print(f"      hover time hit: {timestamp} | {text[:24]}", flush=True)
                except Exception as e:
                    print(f"      hover time failed for {text[:24]}: {e}", flush=True)

            out.append(
                {
                    "id": item_id,
                    "kind": "message",
                    "message_type": _message_type(text),
                    "timestamp": timestamp,
                    "timestamp_visible": None,
                    "sender": "self" if direction == "outgoing" else (last_sender or "unknown"),
                    "direction": direction,
                    "text": text,
                    "screen_point": [x, y],
                    "ocr_center": [round(box.cx, 1), round(box.cy, 1)],
                    "source_screenshot": str(shot),
                    "hover_screenshot": hover_shot,
                    "cursor_delta": cursor_delta,
                }
            )
    return out


def _hover_time_for_message(hwnd: int, msg: dict, hover_dir: Path, label: str) -> dict:
    """Hover a single structured message and return a copy with timestamp fields filled."""
    updated = dict(msg)
    point = msg.get("screen_point") or []
    if len(point) != 2:
        return updated
    try:
        x, y = int(point[0]), int(point[1])
    except (TypeError, ValueError):
        return updated

    hover_dir.mkdir(parents=True, exist_ok=True)
    try:
        actual_x, actual_y = _sendinput_hover_to(x, y, settle_sec=1.15)
        hover_shot_path = hover_dir / f"{label}_hover.png"
        _capture_cursor_neighborhood(actual_x, actual_y, hover_shot_path, width=760, height=360)
        timestamp = _extract_hover_time(_ocr_lines(hover_shot_path))
        updated["hover_screenshot"] = str(hover_shot_path)
        updated["cursor_delta"] = [actual_x - x, actual_y - y]
        if timestamp:
            updated["timestamp"] = timestamp
            print(f"      incremental hover time hit: {timestamp} | {str(msg.get('text') or '')[:24]}", flush=True)
    except Exception as e:
        print(f"      incremental hover time failed for {str(msg.get('text') or '')[:24]}: {e}", flush=True)
    return updated


def _dedupe_structured_messages(items: list[dict], limit: int) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict] = []
    for item in items:
        key = (
            str(item.get("kind") or ""),
            str(item.get("message_type") or ""),
            _norm_line(str(item.get("text") or "")),
        )
        if not key[2] or key in seen:
            continue
        seen.add(key)
        result.append(item)
    messages_seen = 0
    tail: list[dict] = []
    for item in reversed(result):
        tail.append(item)
        if item.get("kind") == "message":
            messages_seen += 1
        if messages_seen >= limit:
            break
    return list(reversed(tail))


def _capture_chat_text(
    hwnd: int,
    conv_index: int,
    out_dir: Path,
    *,
    target_lines: int,
    max_scrolls: int,
    include_message_time: bool = False,
    allow_background_visible: bool = False,
) -> tuple[list[str], list[str], list[dict]]:
    snapshots: list[list[str]] = []
    structured_snapshots: list[list[dict]] = []
    shot_paths: list[str] = []
    hover_dir = out_dir / f"conv_{conv_index:02d}_hover"
    if include_message_time:
        hover_dir.mkdir(parents=True, exist_ok=True)
    _click_back_to_latest_if_present(hwnd, out_dir, conv_index)

    for i in range(max_scrolls + 1):
        hwnd = _wait_for_wework_foreground(hwnd, allow_background_visible=allow_background_visible)
        shot = out_dir / f"conv_{conv_index:02d}_page_{i:02d}.png"
        capture_window_region_png(hwnd, CHAT_REGION, shot)
        text = ocr_image_file(shot)
        lines = _filter_lines(text.splitlines())
        snapshots.append(lines)
        structured_snapshots.append(
            _structured_from_boxes(
                hwnd,
                shot,
                conv_index,
                i,
                include_message_time=include_message_time,
                hover_dir=hover_dir,
            )
        )
        shot_paths.append(str(shot))

        merged = _merge_visible_snapshots(snapshots, target_lines)
        print(f"    page {i + 1}: {len(lines)} lines, merged {len(merged)}/{target_lines}", flush=True)
        if len(merged) >= target_lines:
            break

        before = capture_window_region_hash(hwnd, CHAT_REGION)
        try:
            scroll_client_ratio(hwnd, 0.66, 0.40, clicks=8, pause_after=0.45)
        except RuntimeError as e:
            print(f"    scroll skipped: {e}", flush=True)
            break
        after = capture_window_region_hash(hwnd, CHAT_REGION)
        if after == before:
            print("    scroll produced no visual change", flush=True)
            break

    structured: list[dict] = []
    for page_items in reversed(structured_snapshots):
        structured.extend(page_items)
    structured = _dedupe_structured_messages(structured, target_lines)
    return _merge_visible_snapshots(snapshots, target_lines), shot_paths, structured


def _write_outputs(items: list[ConversationExport], skipped: list[SkippedConversation], out_dir: Path) -> None:
    exported_at = datetime.now().isoformat(timespec="seconds")
    data = {
        "exported_at": exported_at,
        "conversations": [asdict(x) for x in items],
    }
    (out_dir / "recent_chats.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )

    parts = [f"# WeWork Recent Chats\n\nExported at: {exported_at}\n"]
    for item in items:
        parts.append(f"\n## {item.index}. {item.title}\n")
        if item.list_preview:
            parts.append(f"\nList preview: {item.list_preview}\n")
        for msg in item.messages:
            parts.append(f"- {msg}\n")
    (out_dir / "recent_chats.md").write_text("".join(parts), encoding="utf-8-sig")

    skipped_data = {
        "exported_at": exported_at,
        "skipped": [asdict(x) for x in skipped],
    }
    (out_dir / "skipped_conversations.json").write_text(
        json.dumps(skipped_data, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )

    skipped_parts = [f"# Skipped WeWork Conversations\n\nExported at: {exported_at}\n"]
    for item in skipped:
        skipped_parts.append(
            f"\n- page={item.page_index} y={item.y_ratio:.3f} reason={item.reason} "
            f"title={item.title} preview={item.list_preview}\n"
        )
    (out_dir / "skipped_conversations.md").write_text("".join(skipped_parts), encoding="utf-8-sig")


def _conversation_key(item: dict) -> str:
    raw = str(item.get("title") or "").strip() or f"{item.get('title') or ''} {item.get('list_preview') or ''}"
    key = re.sub(r"@寰俊|澶栭儴|外部|@微信", "", raw)
    key = re.sub(r"[^\w\u4e00-\u9fff]+", "", key)
    return key[:80] or f"conversation_{item.get('index') or 'unknown'}"


def _message_key(conversation_key: str, msg: dict) -> tuple[str, str]:
    text_key = re.sub(r"\s+", " ", str(msg.get("text") or "")).strip()
    text_key = re.sub(r"[^\w\u4e00-\u9fff:/.-]+", "", text_key)[:160]
    timestamp = str(msg.get("timestamp") or "").strip()
    sender = str(msg.get("sender") or "unknown").strip()
    message_type = str(msg.get("message_type") or "text").strip()
    if timestamp:
        return f"{conversation_key}|{timestamp}|{sender}|{message_type}|{text_key}", "high"
    return f"{conversation_key}|{sender}|{message_type}|{text_key}", "medium"


def _merge_master(run_dir: Path, master_dir: Path) -> None:
    run_path = run_dir / "recent_chats.json"
    if not run_path.is_file():
        return
    run_data = json.loads(run_path.read_text(encoding="utf-8-sig"))
    master_dir.mkdir(parents=True, exist_ok=True)
    master_path = master_dir / "recent_chats.json"
    now = datetime.now().isoformat(timespec="seconds")
    if master_path.is_file():
        master = json.loads(master_path.read_text(encoding="utf-8-sig"))
    else:
        master = {
            "exported_at": now,
            "updated_at": now,
            "source_runs": [],
            "conversations": [],
        }

    master.setdefault("source_runs", [])
    run_name = run_dir.name
    if run_name not in master["source_runs"]:
        master["source_runs"].append(run_name)

    convs_by_key = {
        str(conv.get("conversation_key") or _conversation_key(conv)): conv
        for conv in master.get("conversations", [])
        if isinstance(conv, dict)
    }

    for conv in run_data.get("conversations", []):
        if not isinstance(conv, dict):
            continue
        conv_key = _conversation_key(conv)
        existing = convs_by_key.get(conv_key)
        if not existing:
            existing = {
                "index": len(convs_by_key) + 1,
                "conversation_key": conv_key,
                "title": conv.get("title") or "",
                "list_preview": conv.get("list_preview") or "",
                "messages": [],
                "screenshots": [],
                "structured_messages": [],
                "first_seen_at": now,
                "last_seen_at": now,
                "seen_count": 0,
                "source_runs": [],
            }
            convs_by_key[conv_key] = existing

        existing["title"] = conv.get("title") or existing.get("title") or ""
        existing["list_preview"] = conv.get("list_preview") or existing.get("list_preview") or ""
        existing["last_seen_at"] = now
        existing["seen_count"] = int(existing.get("seen_count") or 0) + 1
        existing.setdefault("source_runs", [])
        if run_name not in existing["source_runs"]:
            existing["source_runs"].append(run_name)
        existing.setdefault("screenshots", [])
        for shot in conv.get("screenshots") or []:
            if shot not in existing["screenshots"]:
                existing["screenshots"].append(shot)

        messages = existing.setdefault("structured_messages", [])
        by_msg_key = {
            str(msg.get("message_key")): msg
            for msg in messages
            if isinstance(msg, dict) and msg.get("message_key")
        }
        for msg in conv.get("structured_messages") or []:
            if not isinstance(msg, dict) or msg.get("kind") != "message":
                continue
            key, confidence = _message_key(conv_key, msg)
            if key in by_msg_key:
                target = by_msg_key[key]
                target["last_seen_at"] = now
                target["seen_count"] = int(target.get("seen_count") or 0) + 1
                if not target.get("timestamp") and msg.get("timestamp"):
                    target["timestamp"] = msg.get("timestamp")
                continue
            merged_msg = dict(msg)
            merged_msg["message_key"] = key
            merged_msg["dedupe_confidence"] = confidence
            merged_msg["first_seen_at"] = now
            merged_msg["last_seen_at"] = now
            merged_msg["seen_count"] = 1
            merged_msg["source_run"] = run_name
            messages.append(merged_msg)
            by_msg_key[key] = merged_msg
        existing["messages"] = [str(m.get("text") or "") for m in existing.get("structured_messages", [])]

    master["updated_at"] = now
    master["conversations"] = sorted(convs_by_key.values(), key=lambda c: int(c.get("index") or 999999))
    master_path.write_text(json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    print(f">>> merged master: {master_path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export recent external WeWork conversations")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--messages", type=int, default=20)
    parser.add_argument("--max-scrolls", type=int, default=5)
    parser.add_argument("--list-scroll-clicks", type=int, default=-1)
    parser.add_argument("--list-wheel-delta", type=int, default=80)
    parser.add_argument("--max-list-pages", type=int, default=80)
    parser.add_argument("--scan-extra-pages", type=int, default=4)
    parser.add_argument("--include-message-time", action="store_true", help="Hover each OCR message box and capture exact per-message timestamp when available.")
    parser.add_argument("--master-dir", type=Path, default=Path("exports") / "wxwork_master")
    parser.add_argument("--no-merge-master", action="store_true")
    parser.add_argument("--allow-background-visible", action="store_true", help="Allow captures when WeWork is visible but not foreground.")
    parser.add_argument("--out-dir", type=Path, default=Path("exports") / "wxwork_recent")
    parser.add_argument(
        "--required-marker",
        action="append",
        default=DEFAULT_REQUIRED_MARKERS,
        help="Only export conversations whose title/preview contains this marker. Can be repeated.",
    )
    parser.add_argument(
        "--skip-keyword",
        action="append",
        default=DEFAULT_SKIP_KEYWORDS,
        help="Skip conversations whose title/preview contains this keyword. Can be repeated.",
    )
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
    if not args.allow_background_visible and not is_same_process_foreground(hwnd):
        print(">>> WeWork is not foreground; continuing with direct HWND messages", flush=True)
    _reset_conversation_list_to_top(hwnd)

    exports: list[ConversationExport] = []
    skipped: list[SkippedConversation] = []
    seen_keys: set[str] = set()
    seen_preview_keys: set[str] = set()
    page_index = 0
    stale_pages = 0
    previous_visible: list[tuple[str, str, float]] = []

    while len(exports) < args.count and page_index < args.max_list_pages:
        try:
            hwnd = _ensure_live_hwnd(hwnd)
            hwnd = _wait_for_wework_foreground(hwnd, allow_background_visible=args.allow_background_visible)
        except Exception as e:
            print(f">>> WeWork unavailable, stopping cleanly: {e}", flush=True)
            break

        visible = _collect_visible_rows(hwnd, 8, args.out_dir, page_index=page_index)
        if previous_visible:
            overlap = _overlap_keys(previous_visible, visible)
            print(f">>> page {page_index}: overlap={len(overlap)}", flush=True)
        else:
            print(">>> page 0", flush=True)

        progressed = False
        for row_index, (title, preview, y_ratio) in enumerate(visible):
            row_key = _row_key(title, preview)
            if not row_key or row_key in seen_keys:
                continue
            preview_key = _preview_key(preview)
            if preview_key and len(preview_key) >= 8 and preview_key in seen_preview_keys:
                seen_keys.add(row_key)
                skipped.append(SkippedConversation(page_index, title, preview, y_ratio, "duplicate_preview"))
                print(f">>> skip duplicate_preview: {title}", flush=True)
                continue

            reason = _skip_reason(title, preview, args.skip_keyword, args.required_marker)
            if reason:
                seen_keys.add(row_key)
                if preview_key and len(preview_key) >= 8:
                    seen_preview_keys.add(preview_key)
                skipped.append(SkippedConversation(page_index, title, preview, y_ratio, reason))
                print(f">>> skip {reason}: {title}", flush=True)
                continue

            # Bottom-most partial rows are easy to misclick; leave them unseen
            # so the next small scroll can process them when they move upward.
            if y_ratio > 0.91:
                print(f">>> defer bottom row: {title}", flush=True)
                continue

            seen_keys.add(row_key)
            if preview_key and len(preview_key) >= 8:
                seen_preview_keys.add(preview_key)
            conv_index = len(exports) + 1
            print(
                f">>> [{conv_index}/{args.count}] open page={page_index} row={row_index + 1}: {title}",
                flush=True,
            )
            try:
                hwnd = _ensure_live_hwnd(hwnd)
                hwnd = _wait_for_wework_foreground(hwnd, allow_background_visible=args.allow_background_visible)
                click_client_ratio(hwnd, 0.16, y_ratio, pause_after=0.8)
                messages, shots, structured_messages = _capture_chat_text(
                    hwnd,
                    conv_index,
                    args.out_dir,
                    target_lines=args.messages,
                    max_scrolls=args.max_scrolls,
                    include_message_time=args.include_message_time,
                    allow_background_visible=args.allow_background_visible,
                )
            except Exception as e:
                print(f">>> conversation capture failed, stopping cleanly: {e}", flush=True)
                _write_outputs(exports, skipped, args.out_dir)
                return 1
            exports.append(
                ConversationExport(
                    index=conv_index,
                    title=title,
                    list_preview=preview,
                    messages=messages,
                    screenshots=shots,
                    structured_messages=structured_messages,
                )
            )
            progressed = True
            if len(exports) >= args.count:
                break

        if len(exports) >= args.count:
            break

        try:
            hwnd = _ensure_live_hwnd(hwnd)
            hwnd = _wait_for_wework_foreground(hwnd, allow_background_visible=args.allow_background_visible)
            moved = _advance_conversation_list(
                hwnd,
                clicks=args.list_scroll_clicks,
                wheel_delta=args.list_wheel_delta,
            )
        except Exception as e:
            print(f">>> list advance failed, stopping cleanly: {e}", flush=True)
            break
        if moved:
            stale_pages = 0
        else:
            stale_pages += 1
            print(f">>> list page did not move ({stale_pages}/2)", flush=True)
            if stale_pages >= 2:
                break
        previous_visible = visible
        page_index += 1
        if not progressed:
            time.sleep(0.1)

    print(f">>> exported {len(exports)} conversations", flush=True)
    print(f">>> skipped {len(skipped)} conversations", flush=True)
    _write_outputs(exports, skipped, args.out_dir)
    if not args.no_merge_master:
        _merge_master(args.out_dir, args.master_dir)
    print(f">>> written: {args.out_dir / 'recent_chats.json'}", flush=True)
    print(f">>> written: {args.out_dir / 'recent_chats.md'}", flush=True)
    print(f">>> written: {args.out_dir / 'skipped_conversations.json'}", flush=True)
    print(f">>> written: {args.out_dir / 'skipped_conversations.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
