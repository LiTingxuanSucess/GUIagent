"""
Contact-list (left panel) capture, OCR and diff for 企业微信.

企业微信 layout (approximate window-width fractions):
  nav icons   0.00 – 0.05
  chat list   0.05 – 0.27
  chat area   0.27 – 1.00

Each visible list entry contains (top-to-bottom):
  • avatar  (left, ~square)
  • name    (right of avatar, first line)
  • preview (right of avatar, second line — may include trailing unread count)
  • time    (top-right corner, small)

OCR Strategy
------------
Rather than trying to detect red badge pixels (unreliable), we track the
*preview text* of each entry.  When a preview changes we know that contact
has a new message.  企业微信 always moves the freshest conversation to the
top of the list, so the first changed entry is always the most urgent.

Parsing heuristic
-----------------
Conversation rows are parsed by UI row bands first, then OCR text is attached
to each row.  This avoids pairing a name from one row with the preview from the
next row when OCR ordering jitters.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
import re
import struct
from difflib import SequenceMatcher
from typing import Any

from src.capture import RegionRatios, capture_window_region_hash, capture_window_region_temp_png
from src.ocr_client import ocr_image_file, ocr_image_text_boxes
from src.wxwork_layout import attach_boxes_to_contact_rows, detect_contact_row_bands


@dataclass
class ContactEntry:
    name: str
    preview: str  # last-message preview text seen in the list
    cy: float = 0.0  # row center in the captured list image


@dataclass
class ListRatios:
    """Capture ratios for the left contact-list panel."""
    left:   float = 0.045
    top:    float = 0.06
    width:  float = 0.225
    height: float = 0.88

    def as_region_ratios(self) -> RegionRatios:
        return RegionRatios(
            left=self.left,
            top=self.top,
            width=self.width,
            height=self.height,
        )


# ── Capture helpers ────────────────────────────────────────────────────────

def capture_list_hash(hwnd: int, ratios: ListRatios) -> str:
    """Quick pixel hash of the contact-list area (no OCR)."""
    return capture_window_region_hash(hwnd, ratios.as_region_ratios())


def capture_and_ocr_list(hwnd: int, ratios: ListRatios) -> tuple[list[ContactEntry], str]:
    """
    Screenshot + OCR the contact list.
    Returns (entries, pixel_hash).
    """
    path, h = capture_window_region_temp_png(hwnd, ratios.as_region_ratios())
    try:
        boxes = ocr_image_text_boxes(path)
        entries = parse_boxes_to_entries(path, boxes)
        if not entries:
            raw = ocr_image_file(path)
            entries = parse_ocr_to_entries(raw)
    finally:
        path.unlink(missing_ok=True)
    return entries, h


def _png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as f:
        header = f.read(24)
    if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", header[16:24])
    return 0, 0


def _is_noise_line(s: str) -> bool:
    s = s.strip()
    if not s:
        return True
    if s in {"@微信", "@ 微信", "微信"}:
        return True
    if s in {"搜索", "消息", "邮件", "文档", "日程", "待办", "通讯录"}:
        return True
    if any(token in s for token in ("认证将于", "认证后可保障", "将受限", "前往认证")):
        return True
    if s in {"GPT-5.5", "Medium", "RevAI", "R.", "Q", "V", "8", "+"}:
        return True
    if re.fullmatch(r"\d{1,3}\s*分钟前", s):
        return True
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        return True
    if re.fullmatch(r"\d{1,2}[:/]\d{2}", s):
        return True
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,3}", s):
        return True
    return False


def _is_time_token(s: str) -> bool:
    s = _clean_row_text(s)
    if s in {"今天", "昨天", "前天", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"}:
        return True
    if re.fullmatch(r"\d{1,3}\s*分钟前", s):
        return True
    if re.fullmatch(r"\d{1,2}[:：]\d{2}", s):
        return True
    if re.fullmatch(r"\d{1,2}[/.-]\d{1,2}", s):
        return True
    return False


def _clean_row_text(text: str) -> str:
    return " ".join(str(text or "").replace("\u3000", " ").split()).strip()


def _clean_preview_text(text: str) -> str:
    s = _clean_row_text(text)
    s = re.sub(r"\s*\d{1,3}\s*分钟前$", "", s)
    s = re.sub(r"[·.．。\s]*\d{1,2}[:：]\d{2}$", "", s)
    return s.strip(" ·.．。")


def _entry_name_key(name: str) -> str:
    s = _clean_row_text(name).lower()
    s = s.replace("@ 微信", "@微信")
    s = s.replace("@微信", "")
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)
    return s


def _names_match(a: str, b: str) -> bool:
    ak = _entry_name_key(a)
    bk = _entry_name_key(b)
    if not ak or not bk:
        return False
    if ak == bk or ak in bk or bk in ak:
        return True
    if len(ak) >= 5 and len(bk) >= 5:
        return SequenceMatcher(None, ak, bk).ratio() >= 0.86
    return False


def _preview_key(preview: str) -> str:
    s = _clean_preview_text(preview).lower()
    return re.sub(r"\s+", "", s)


def _is_preview_noise(s: str) -> bool:
    s = _clean_row_text(s)
    if not s:
        return True
    return _is_noise_line(s)


def _is_contact_name_part(text: str) -> bool:
    s = _clean_row_text(text)
    if not s:
        return False
    if s in {"@微信", "@ 微信", "微信"}:
        return True
    if re.fullmatch(r"\d{1,3}\s*分钟前", s) or re.fullmatch(r"\d{1,2}[:：]\d{2}", s):
        return False
    return not _is_noise_line(s)


def _merge_contact_name_parts(parts: list[str]) -> str:
    clean = [_clean_row_text(part) for part in parts if _clean_row_text(part)]
    if not clean:
        return ""
    main_parts = [part for part in clean if part not in {"@微信", "@ 微信", "微信"}]
    main = " ".join(main_parts).strip() if main_parts else clean[0]
    if any(part in {"@微信", "@ 微信", "微信"} for part in clean) and "@微信" not in main:
        return f"{main}@微信"
    return main


def _parse_external_contact_entries_from_boxes(boxes: list, *, image_width: int) -> list[ContactEntry]:
    """Parse rows anchored by WeWork external-contact name lines.

    RapidOCR often returns clean full name lines such as ``刘廷曦@微信``.  When
    it splits a title line, the parts still share a close y coordinate.  This
    parser uses those y-line anchors, then picks the nearest lower non-time
    text as the row preview.
    """
    lines: list[dict[str, Any]] = []
    for box in sorted(boxes, key=lambda b: (float(getattr(b, "cy", 0.0)), float(getattr(b, "cx", 0.0)))):
        text = _clean_row_text(getattr(box, "text", ""))
        if not text:
            continue
        cy = float(getattr(box, "cy", 0.0))
        cx = float(getattr(box, "cx", 0.0))
        matched = None
        for line in lines:
            if abs(float(line["cy"]) - cy) <= 14.0:
                matched = line
                break
        if matched is None:
            lines.append({"cy": cy, "parts": [(cx, text)]})
        else:
            matched["parts"].append((cx, text))
            matched["cy"] = (float(matched["cy"]) + cy) / 2.0

    merged: list[tuple[float, str]] = []
    for line in lines:
        parts = [
            text
            for cx, text in sorted(line["parts"], key=lambda item: item[0])
            if not (_is_time_token(text) and float(cx) > image_width * 0.72)
        ]
        text = _clean_row_text("".join(parts))
        if text:
            merged.append((float(line["cy"]), text))
    merged.sort(key=lambda item: item[0])

    entries: list[ContactEntry] = []
    for idx, (cy, text) in enumerate(merged):
        name = _clean_row_text(text).replace("@ 微信", "@微信")
        if "@微信" not in name:
            continue
        if _entry_name_key(name) in {"", "微信"}:
            continue
        preview = ""
        for next_cy, next_text in merged[idx + 1:]:
            if next_cy - cy > 70.0:
                break
            candidate = _clean_preview_text(next_text)
            if candidate and not _is_time_token(candidate) and not _is_noise_line(candidate):
                preview = candidate
                break
        entries.append(ContactEntry(name=name, preview=preview, cy=cy))
    return entries


def parse_boxes_to_entries(path: Path, boxes: list) -> list[ContactEntry]:
    """
    Parse the WeWork left conversation list using OCR box coordinates.

    The crop can include the nav rail and search box.  We keep only text from
    the right side of the crop, where conversation names/previews live, then
    pair adjacent visible lines.
    """
    width, height = _png_size(path)
    if width <= 0 or height <= 0:
        return []

    anchored = _parse_external_contact_entries_from_boxes(boxes, image_width=width)
    if anchored:
        return anchored

    rows = attach_boxes_to_contact_rows(
        boxes,
        detect_contact_row_bands(path),
        image_width=width,
        image_height=height,
    )
    entries: list[ContactEntry] = []
    for row in rows:
        sorted_boxes = sorted(row.texts, key=lambda x: (float(getattr(x, "cy", 0.0)), float(getattr(x, "cx", 0.0))))
        row_boxes = [b for b in sorted_boxes if not _is_noise_line(_clean_row_text(getattr(b, "text", "")))]
        if not row_boxes:
            continue
        title_y = min(float(getattr(b, "cy", 0.0)) for b in row_boxes)
        title_boxes = [
            b
            for b in sorted_boxes
            if abs(float(getattr(b, "cy", 0.0)) - title_y) <= 12.0
            and _is_contact_name_part(_clean_row_text(getattr(b, "text", "")))
        ]
        if not title_boxes:
            title_boxes = [min(row_boxes, key=lambda b: (float(getattr(b, "cy", 0.0)), float(getattr(b, "cx", 0.0))))]
        name = _merge_contact_name_parts([_clean_row_text(getattr(b, "text", "")) for b in title_boxes])
        title_box_ids = {id(b) for b in title_boxes}
        preview_parts = [
            _clean_preview_text(getattr(b, "text", ""))
            for b in row_boxes
            if id(b) not in title_box_ids and not _is_preview_noise(_clean_row_text(getattr(b, "text", "")))
        ]
        preview = " ".join(x for x in preview_parts if x)
        if name and not _is_noise_line(name):
            entries.append(ContactEntry(name=name, preview=preview, cy=float(row.cy)))
    if entries:
        return entries

    lines: list[tuple[float, str]] = []
    for box in sorted(boxes, key=lambda b: (b.cy, b.cx)):
        text = str(box.text).strip()
        if _is_noise_line(text):
            continue
        if box.cx < width * 0.42:
            continue
        # Search box/header text should not become a conversation row.
        if box.cy < height * 0.28:
            continue
        lines.append((float(box.cy), text))

    entries: list[ContactEntry] = []
    i = 0
    while i < len(lines):
        cy, name = lines[i]
        preview = lines[i + 1][1] if i + 1 < len(lines) else ""
        if name and not _is_noise_line(name):
            entries.append(ContactEntry(name=name, preview=preview, cy=cy))
        i += 2
    return entries


# ── OCR text parser ────────────────────────────────────────────────────────

def parse_ocr_to_entries(ocr_text: str) -> list[ContactEntry]:
    """
    Parse OCR output from the contact-list column into ContactEntry objects.

    Takes consecutive pairs of non-empty lines as (name, preview).
    Time strings ("今天", "昨天", weekday names, HH:MM) that appear as
    isolated short tokens are stripped so they don't become names.
    """
    _TIME_TOKENS = {"今天", "昨天", "星期一", "星期二", "星期三",
                    "星期四", "星期五", "星期六", "星期日"}

    def _is_time_token(s: str) -> bool:
        s = s.strip()
        if s in _TIME_TOKENS:
            return True
        # HH:MM  or  M/D
        return bool(re.fullmatch(r"\d{1,2}[:/]\d{2}", s))

    lines = [ln.strip() for ln in ocr_text.splitlines()
             if ln.strip() and not _is_time_token(ln.strip())]

    entries: list[ContactEntry] = []
    i = 0
    while i < len(lines):
        name    = lines[i]
        preview = lines[i + 1] if i + 1 < len(lines) else ""
        entries.append(ContactEntry(name=name, preview=preview))
        i += 2
    return entries


# ── Diff ───────────────────────────────────────────────────────────────────

def changed_contacts(
    prev: list[ContactEntry],
    curr: list[ContactEntry],
) -> list[ContactEntry]:
    """
    Return entries whose preview changed or that are entirely new.

    Matching is done by name (exact, case-insensitive, stripped).
    If OCR yields a slightly different name on two consecutive polls we may
    miss a match — acceptable given OCR noise is rare on names.
    """
    result: list[ContactEntry] = []
    for entry in curr:
        matched_prev = next((old for old in prev if _names_match(old.name, entry.name)), None)
        if matched_prev is None:
            result.append(entry)           # new contact appeared in list
        elif _preview_key(matched_prev.preview) != _preview_key(entry.preview):
            result.append(entry)           # preview text changed → new message
    return result
