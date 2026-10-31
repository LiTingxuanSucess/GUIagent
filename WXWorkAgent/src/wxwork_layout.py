"""WeWork-specific UI layout parsing.

Paddle PPStructure is document-layout oriented and does not understand WeWork
chat bubbles or conversation rows. These helpers use stable UI geometry first,
then attach OCR text to detected UI blocks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image


@dataclass
class UiBlock:
    kind: str
    x0: float
    y0: float
    x1: float
    y1: float
    score: float = 0.0
    texts: list[Any] = field(default_factory=list)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def contains_point(self, x: float, y: float, *, pad_x: float = 8.0, pad_y: float = 8.0) -> bool:
        return self.x0 - pad_x <= x <= self.x1 + pad_x and self.y0 - pad_y <= y <= self.y1 + pad_y

    def vertical_overlap(self, y0: float, y1: float) -> float:
        return max(0.0, min(self.y1, y1) - max(self.y0, y0))


@dataclass
class MainLayout:
    list_left: float
    list_top: float
    list_width: float
    list_height: float
    chat_left: float
    chat_top: float
    chat_width: float
    chat_height: float
    confidence: float = 0.0
    source: str = "detected"


def _box_attr(box: Any, name: str, default: float = 0.0) -> float:
    try:
        return float(getattr(box, name))
    except Exception:
        return default


def _box_text(box: Any) -> str:
    return str(getattr(box, "text", "") or "").strip()


def _load_rgb_array(path: Path) -> np.ndarray:
    with Image.open(path) as img:
        return np.asarray(img.convert("RGB"))


def _ratio(value: float, total: int) -> float:
    return max(0.0, min(1.0, float(value) / max(1, total)))


def _clamp_region(left: float, top: float, width: float, height: float) -> tuple[float, float, float, float]:
    left = max(0.0, min(0.98, left))
    top = max(0.0, min(0.98, top))
    width = max(0.01, min(1.0 - left, width))
    height = max(0.01, min(1.0 - top, height))
    return left, top, width, height


def _stable_vertical_groups(arr: np.ndarray) -> list[tuple[int, int, float]]:
    """Find vertical chrome bands that span most of the current WeWork window."""
    h, w = arr.shape[:2]
    y0 = max(0, int(h * 0.14))
    y1 = min(h, int(h * 0.96))
    if y1 - y0 < 80 or w < 360:
        return []
    columns: list[tuple[int, float]] = []
    for x in range(max(8, int(w * 0.06)), min(w - 8, int(w * 0.94))):
        samples = arr[y0:y1:4, x, :].astype(np.int16)
        if samples.size == 0:
            continue
        mean_rgb = samples.mean(axis=0)
        brightness = float(mean_rgb.mean())
        channel_range = int((samples.max(axis=0) - samples.min(axis=0)).max())
        if channel_range > 48 or brightness < 185 or brightness > 252:
            continue
        left = arr[y0:y1:8, max(0, x - 8), :].astype(np.int16)
        right = arr[y0:y1:8, min(w - 1, x + 8), :].astype(np.int16)
        contrast = float(np.abs(left.mean(axis=0) - right.mean(axis=0)).mean()) if left.size and right.size else 0.0
        score = float(48 - channel_range) + contrast
        columns.append((x, score))

    groups: list[tuple[int, int, float]] = []
    current: list[tuple[int, float]] = []
    prev_x = -99
    for x, score in columns:
        if current and x - prev_x > 2:
            groups.append((current[0][0], current[-1][0], float(sum(s for _x, s in current) / len(current))))
            current = []
        current.append((x, score))
        prev_x = x
    if current:
        groups.append((current[0][0], current[-1][0], float(sum(s for _x, s in current) / len(current))))
    return [g for g in groups if g[1] - g[0] + 1 >= 3]


def _detect_header_bottom(arr: np.ndarray, *, chat_left_px: int) -> int | None:
    h, w = arr.shape[:2]
    x0 = max(0, min(w - 1, chat_left_px + 12))
    x1 = max(x0 + 1, min(w, w - 10))
    best: tuple[float, int] | None = None
    for y in range(max(8, int(h * 0.05)), min(h - 8, int(h * 0.24))):
        row = arr[y, x0:x1:4, :].astype(np.int16)
        if row.size == 0:
            continue
        spread = row.max(axis=1) - row.min(axis=1)
        brightness = row.mean(axis=1)
        line_ratio = float(((spread <= 12) & (brightness >= 210) & (brightness <= 252)).mean())
        if line_ratio < 0.86:
            continue
        above = arr[max(0, y - 2), x0:x1:4, :].astype(np.int16)
        below = arr[min(h - 1, y + 2), x0:x1:4, :].astype(np.int16)
        edge = float(np.abs(above - below).mean()) if above.size and below.size else 0.0
        score = edge + line_ratio * 8.0
        if best is None or score > best[0]:
            best = (score, y)
    if best is None or best[0] < 12.0:
        return None
    return best[1] + 2


def detect_main_layout_regions(image_path: Path, *, fallback: dict[str, float] | None = None) -> MainLayout:
    """Detect WeWork nav/list/chat boundaries from the current full-window image."""
    arr = _load_rgb_array(image_path)
    h, w = arr.shape[:2]
    fallback = fallback or {}

    def _fallback() -> MainLayout:
        list_left = float(fallback.get("list_left", 0.045))
        list_top = float(fallback.get("list_top", 0.06))
        list_width = float(fallback.get("list_width", 0.225))
        list_height = float(fallback.get("list_height", 0.88))
        chat_left = float(fallback.get("chat_left", 0.38))
        chat_top = float(fallback.get("chat_top", 0.10))
        chat_width = float(fallback.get("chat_width", 0.60))
        chat_height = float(fallback.get("chat_height", 0.76))
        list_left, list_top, list_width, list_height = _clamp_region(list_left, list_top, list_width, list_height)
        chat_left, chat_top, chat_width, chat_height = _clamp_region(chat_left, chat_top, chat_width, chat_height)
        return MainLayout(list_left, list_top, list_width, list_height, chat_left, chat_top, chat_width, chat_height, confidence=0.0, source="fallback")

    groups = _stable_vertical_groups(arr)
    if w < 360 or not groups:
        return _fallback()

    best_pair: tuple[float, tuple[int, int, float], tuple[int, int, float]] | None = None
    min_list_px = max(220, int(w * 0.12))
    max_list_px = min(760, int(w * 0.55))
    min_chat_px = max(260, int(w * 0.20))
    for left_group in groups:
        left_px = int(left_group[1]) + 1
        if left_px < w * 0.05 or left_px > w * 0.55:
            continue
        for right_group in groups:
            if right_group[0] <= left_group[1]:
                continue
            right_px = int(round((right_group[0] + right_group[1]) / 2.0))
            list_width_px = right_px - left_px
            chat_width_px = w - right_px
            if list_width_px < min_list_px or list_width_px > max_list_px or chat_width_px < min_chat_px:
                continue
            # Conversation-list width is mostly a fixed desktop UI width; prefer
            # the first pair that leaves a large chat pane, then higher chrome
            # confidence. This avoids tying the split to a window-width ratio.
            width_score = 1.0 - min(abs(list_width_px - 520.0) / 520.0, 1.0)
            chat_score = min(chat_width_px / max(1.0, w * 0.45), 1.0)
            group_score = min((left_group[2] + right_group[2]) / 90.0, 1.0)
            score = width_score * 0.45 + chat_score * 0.35 + group_score * 0.20
            if best_pair is None or score > best_pair[0]:
                best_pair = (score, left_group, right_group)

    if best_pair is None:
        return _fallback()

    _score, list_left_group, list_right_group = best_pair
    list_left_px = int(list_left_group[1]) + 1
    list_right_px = int(round((list_right_group[0] + list_right_group[1]) / 2.0))
    confidence = 0.86

    if list_right_px - list_left_px < max(160, int(w * 0.12)):
        return _fallback()
    chat_left_px = min(w - 2, max(list_right_px + 1, list_right_px))
    header_bottom = _detect_header_bottom(arr, chat_left_px=chat_left_px)
    chat_top_px = header_bottom if header_bottom is not None else int(round(h * float(fallback.get("chat_top", 0.10))))
    list_top_px = int(round(h * float(fallback.get("list_top", 0.06))))
    list_bottom_px = int(round(h * min(0.985, float(fallback.get("list_top", 0.06)) + float(fallback.get("list_height", 0.88)))))
    chat_bottom_px = int(round(h * min(0.985, float(fallback.get("chat_top", 0.10)) + float(fallback.get("chat_height", 0.76)))))
    chat_bottom_px = max(chat_top_px + 80, min(h, chat_bottom_px))

    list_left, list_top, list_width, list_height = _clamp_region(
        _ratio(list_left_px, w),
        _ratio(list_top_px, h),
        _ratio(list_right_px - list_left_px, w),
        _ratio(list_bottom_px - list_top_px, h),
    )
    chat_left, chat_top, chat_width, chat_height = _clamp_region(
        _ratio(chat_left_px, w),
        _ratio(chat_top_px, h),
        _ratio(w - chat_left_px, w),
        _ratio(chat_bottom_px - chat_top_px, h),
    )
    return MainLayout(
        list_left,
        list_top,
        list_width,
        list_height,
        chat_left,
        chat_top,
        chat_width,
        chat_height,
        confidence=confidence,
        source="detected",
    )


def _merge_rects(rects: list[tuple[int, int, int, int]], *, gap_x: int, gap_y: int) -> list[tuple[int, int, int, int]]:
    merged = list(rects)
    changed = True
    while changed:
        changed = False
        out: list[tuple[int, int, int, int]] = []
        used = [False] * len(merged)
        for i, a in enumerate(merged):
            if used[i]:
                continue
            ax0, ay0, ax1, ay1 = a
            used[i] = True
            again = True
            while again:
                again = False
                for j, b in enumerate(merged):
                    if used[j]:
                        continue
                    bx0, by0, bx1, by1 = b
                    separated = ax1 + gap_x < bx0 or bx1 + gap_x < ax0 or ay1 + gap_y < by0 or by1 + gap_y < ay0
                    if separated:
                        continue
                    ax0, ay0, ax1, ay1 = min(ax0, bx0), min(ay0, by0), max(ax1, bx1), max(ay1, by1)
                    used[j] = True
                    changed = True
                    again = True
            out.append((ax0, ay0, ax1, ay1))
        merged = out
    return merged


def _component_rects(mask: np.ndarray, *, min_area: int, min_w: int, min_h: int) -> list[tuple[int, int, int, int, int]]:
    n, _labels, stats, _centers = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    rects: list[tuple[int, int, int, int, int]] = []
    for idx in range(1, n):
        x, y, w, h, area = [int(v) for v in stats[idx]]
        if area < min_area or w < min_w or h < min_h:
            continue
        rects.append((x, y, x + w, y + h, area))
    return rects


def _contact_avatar_mask(arr: np.ndarray) -> np.ndarray:
    """Return pixels likely to belong to a contact avatar, not selected-row chrome."""
    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)
    maxc = np.maximum.reduce([r, g, b])
    minc = np.minimum.reduce([r, g, b])
    spread = maxc - minc
    selected_blue = (b >= 165) & (g >= 105) & (g <= 195) & (r <= 120) & ((b - r) >= 65)
    pale_neutral = (maxc >= 238) & (spread <= 18)
    avatar_like = ((spread >= 30) | (maxc <= 115)) & ~selected_blue & ~pale_neutral
    return avatar_like.astype(np.uint8)


def _dedupe_centers(centers: list[float], *, merge_gap: float = 42.0) -> list[float]:
    if not centers:
        return []
    groups: list[list[float]] = []
    for center in sorted(centers):
        if groups and center - groups[-1][-1] <= merge_gap:
            groups[-1].append(center)
        else:
            groups.append([center])
    return [float(sum(group) / len(group)) for group in groups]


def _avatar_centers_for_range(arr: np.ndarray, x0_ratio: float, x1_ratio: float) -> list[float]:
    h, w = arr.shape[:2]
    x0 = max(0, min(w - 1, int(w * x0_ratio)))
    x1 = max(x0 + 1, min(w, int(w * x1_ratio)))
    avatar = arr[:, x0:x1, :]
    mask = _contact_avatar_mask(avatar)
    mask[: int(h * 0.12), :] = 0
    mask[int(h * 0.985) :, :] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    range_w = max(1, x1 - x0)
    centers: list[float] = []
    for rx0, ry0, rx1, ry1, area in _component_rects(mask, min_area=260, min_w=12, min_h=18):
        rw = rx1 - rx0
        rh = ry1 - ry0
        cy = (ry0 + ry1) / 2.0
        if cy < h * 0.12 or cy > h * 0.985:
            continue
        if rh < 28 or rh > min(125, h * 0.24):
            continue
        if rw < 18:
            continue
        if rw >= range_w * 0.92 and rh > 70:
            continue
        aspect = rw / max(1, rh)
        if aspect < 0.35 or aspect > 2.4:
            continue
        fill = area / max(1, rw * rh)
        if fill < 0.08:
            continue
        centers.append(cy)
    return _dedupe_centers(centers)


def _score_avatar_centers(centers: list[float]) -> float:
    if not centers:
        return 0.0
    score = float(len(centers) * 100)
    if len(centers) >= 2:
        diffs = [b - a for a, b in zip(centers, centers[1:])]
        plausible = [d for d in diffs if 55 <= d <= 180]
        score += len(plausible) * 20
        if plausible:
            score -= float(np.std(plausible)) * 0.5
        score -= sum(35 for d in diffs if d < 48)
    return score


def _bands_from_centers(centers: list[float], *, image_width: int, image_height: int, text_start_x: float) -> list[UiBlock]:
    centers = _dedupe_centers([c for c in centers if image_height * 0.12 <= c <= image_height * 0.985])
    if not centers:
        return []
    diffs = [b - a for a, b in zip(centers, centers[1:]) if 48 <= b - a <= 190]
    spacing = float(np.median(diffs)) if diffs else 118.0
    half = max(44.0, min(78.0, spacing / 2.0))

    bands: list[UiBlock] = []
    for idx, center in enumerate(centers):
        prev_center = centers[idx - 1] if idx else None
        next_center = centers[idx + 1] if idx + 1 < len(centers) else None
        top = (prev_center + center) / 2.0 if prev_center is not None else center - half
        bottom = (center + next_center) / 2.0 if next_center is not None else center + half
        top = max(0.0, top)
        bottom = min(float(image_height), bottom)
        if bottom - top < 48 or bottom - top > 190:
            continue
        bands.append(UiBlock("row", float(text_start_x), top, float(image_width), bottom, score=float(bottom - top)))
    return bands


def _blue_bubble_mask(arr: np.ndarray) -> np.ndarray:
    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)
    return (b >= 175) & (g >= 140) & ((b - r) >= 28) & ((g - r) >= 8)


def _gray_bubble_mask(arr: np.ndarray) -> np.ndarray:
    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)
    spread = np.maximum.reduce([r, g, b]) - np.minimum.reduce([r, g, b])
    # Incoming bubbles are light neutral gray, distinct from the blue self bubbles
    # and from the almost-white chat background.
    return (r >= 205) & (r <= 238) & (g >= 205) & (g <= 238) & (b >= 205) & (b <= 238) & (spread <= 18)


def detect_chat_bubbles(image_path: Path) -> list[UiBlock]:
    """Detect visible WeWork chat bubbles by color and geometry."""
    arr = _load_rgb_array(image_path)
    h, w = arr.shape[:2]
    blocks: list[UiBlock] = []

    blue_mask = _blue_bubble_mask(arr)
    blue_mask = cv2.morphologyEx(blue_mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((11, 23), np.uint8))
    for x0, y0, x1, y1, area in _component_rects(blue_mask, min_area=900, min_w=60, min_h=24):
        if y0 < h * 0.02 or y1 > h * 0.965:
            continue
        if (x0 + x1) / 2.0 < w * 0.50 and x1 < w * 0.62:
            continue
        blocks.append(UiBlock("outgoing", x0, y0, x1, y1, score=float(area)))

    gray_mask = _gray_bubble_mask(arr)
    # Exclude UI chrome and huge pale panels; chat bubbles are compact rounded components.
    gray_mask[:, int(w * 0.55) :] = 0
    gray_mask[: int(h * 0.03), :] = 0
    gray_mask[int(h * 0.965) :, :] = 0
    gray_mask = cv2.morphologyEx(gray_mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((9, 17), np.uint8))
    gray_rects = [(x0, y0, x1, y1) for x0, y0, x1, y1, _area in _component_rects(gray_mask, min_area=500, min_w=45, min_h=22)]
    for x0, y0, x1, y1 in _merge_rects(gray_rects, gap_x=10, gap_y=6):
        if x0 > w * 0.48 or y0 < h * 0.02 or y1 > h * 0.965:
            continue
        if x1 - x0 > w * 0.55 or y1 - y0 > h * 0.22:
            continue
        blocks.append(UiBlock("incoming", x0, y0, x1, y1, score=float((x1 - x0) * (y1 - y0))))

    blocks.sort(key=lambda b: (b.y0, b.x0))
    return blocks


def attach_boxes_to_chat_bubbles(boxes: list[Any], bubbles: list[UiBlock], *, image_width: int, image_height: int) -> list[UiBlock]:
    """Attach OCR text boxes to the nearest detected chat bubble."""
    _ = image_width
    for bubble in bubbles:
        bubble.texts.clear()
    for box in sorted(boxes, key=lambda b: (_box_attr(b, "cy"), _box_attr(b, "cx"))):
        text = _box_text(box)
        if not text:
            continue
        cx = _box_attr(box, "cx")
        cy = _box_attr(box, "cy")
        best: tuple[float, UiBlock] | None = None
        for bubble in bubbles:
            if bubble.kind == "incoming":
                pad_x, pad_y = 28.0, 12.0
            else:
                pad_x, pad_y = 18.0, 12.0
            if not bubble.contains_point(cx, cy, pad_x=pad_x, pad_y=pad_y):
                continue
            dy = abs(cy - bubble.cy)
            dx = 0.0 if bubble.x0 <= cx <= bubble.x1 else min(abs(cx - bubble.x0), abs(cx - bubble.x1))
            score = dy + dx * 0.35
            if best is None or score < best[0]:
                best = (score, bubble)
        if best is not None:
            best[1].texts.append(box)
    return [b for b in bubbles if b.texts]


def detect_contact_row_bands(image_path: Path) -> list[UiBlock]:
    """Detect visible conversation-list rows from avatar/row separators."""
    arr = _load_rgb_array(image_path)
    h, w = arr.shape[:2]
    bands: list[UiBlock] = []

    # Prefer avatar clusters. RevUA can crop either the full left panel or just
    # the conversation list, so the avatar column is probed adaptively.
    avatar_ranges = (
        (0.04, 0.28),
        (0.08, 0.32),
        (0.18, 0.42),
        (0.36, 0.66),
    )
    best: tuple[float, list[float], float] | None = None
    for x0_ratio, x1_ratio in avatar_ranges:
        centers = _avatar_centers_for_range(arr, x0_ratio, x1_ratio)
        score = _score_avatar_centers(centers)
        text_start = min(float(w - 1), max(float(w) * 0.24, float(w) * (x1_ratio + 0.015)))
        if best is None or score > best[0]:
            best = (score, centers, text_start)
    if best is not None and best[1]:
        bands = _bands_from_centers(best[1], image_width=w, image_height=h, text_start_x=best[2])
        if len(bands) >= 2:
            return bands

    # In separator fallback mode, rows live to the right of the nav rail when
    # present. Use a permissive text start so pure-list crops still attach short
    # previews such as "在吗".
    x_start = int(w * 0.30)
    x_end = w
    crop = arr[:, x_start:x_end, :]

    r = crop[:, :, 0].astype(np.int16)
    g = crop[:, :, 1].astype(np.int16)
    b = crop[:, :, 2].astype(np.int16)
    spread = np.maximum.reduce([r, g, b]) - np.minimum.reduce([r, g, b])
    # Separator lines are pale gray and run horizontally through most of the row.
    sep = (r >= 218) & (g >= 218) & (b >= 218) & (spread <= 10)
    density = sep.mean(axis=1)
    sep_rows = [i for i, v in enumerate(density) if v > 0.58]

    anchors: list[int] = []
    if sep_rows:
        group: list[int] = []
        prev = -99
        for y in sep_rows:
            if y - prev > 3 and group:
                anchors.append(int(sum(group) / len(group)))
                group = []
            group.append(y)
            prev = y
        if group:
            anchors.append(int(sum(group) / len(group)))

    anchors = [y for y in anchors if h * 0.08 < y < h * 0.98]
    for top, bottom in zip(anchors, anchors[1:]):
        if bottom - top < 55 or bottom - top > 190:
            continue
        bands.append(UiBlock("row", float(x_start), float(top), float(x_end), float(bottom), score=float(bottom - top)))

    if len(bands) >= 3:
        return bands

    return bands


def attach_boxes_to_contact_rows(boxes: list[Any], rows: list[UiBlock], *, image_width: int, image_height: int) -> list[UiBlock]:
    """Attach OCR boxes to contact-list row bands."""
    _ = image_height
    for row in rows:
        row.texts.clear()
    for box in sorted(boxes, key=lambda b: (_box_attr(b, "cy"), _box_attr(b, "cx"))):
        text = _box_text(box)
        if not text:
            continue
        cx = _box_attr(box, "cx")
        cy = _box_attr(box, "cy")
        best: tuple[float, UiBlock] | None = None
        for row in rows:
            if cx < max(0.0, row.x0 - 12.0):
                continue
            if not (row.y0 - 5 <= cy <= row.y1 + 5):
                continue
            score = abs(cy - row.cy)
            if best is None or score < best[0]:
                best = (score, row)
        if best is not None:
            best[1].texts.append(box)
    return [row for row in rows if row.texts]
