"""Narrow WeWork live auto-reply loop for fixed contacts.

This intentionally does not reuse wxwork_export_recent.py. Historical export
and live reply have different failure modes: live reply only needs the latest
visible incoming text after the latest visible outgoing message.
"""
from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from src.ocr_runtime import init_ocr_runtime

init_ocr_runtime()

import yaml
from PIL import Image

from src.actions import click_message_composer, click_screen_point, paste_and_send, scroll_client_ratio, search_and_open_contact
from src.capture import RegionRatios, capture_window_region_temp_png_meta, mss_region_for_ratios
from src.capture import mss_region_for_ratios as _mss_region_for_ratios
from src.contact_list import (
    ContactEntry,
    ListRatios,
    _entry_name_key,
    _names_match,
    _preview_key,
    capture_and_ocr_list,
    changed_contacts,
)
from src.dashscope_key import resolve_dashscope_api_key
from src.ocr_client import ocr_image_text_boxes, warmup_ocr_service
from src.qwen_text import QwenTextClient
from src.revai_qa import RevaiQaClient
from src.window import activate_hwnd, ensure_visible, ensure_wework_hwnd
from src.wxwork_layout import MainLayout, attach_boxes_to_chat_bubbles, detect_chat_bubbles, detect_main_layout_regions

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

log = logging.getLogger(__name__)


class ReplyClient(Protocol):
    model: str

    def complete(self, system: str, user: str, *, session_id: str | None = None) -> str:
        ...


@dataclass
class LiveMessage:
    direction: str
    text: str
    message_type: str
    cx: float
    cy: float
    y_bucket: int
    bg_blue_ratio: float


@dataclass
class LiveScan:
    contact: str
    screenshot: str
    screen_hash: str
    messages: list[LiveMessage]
    last_outgoing: LiveMessage | None
    latest_incoming: LiveMessage | None
    candidate_key: str
    should_reply: bool
    reason: str


@dataclass
class ReplyResult:
    reply: str = ""
    attempted_send: bool = False
    sent_confirmed: bool = False


TIME_MARKER_RE = re.compile(
    r"^(?:"
    r"今天|昨天|前天|"
    r"星期[一二三四五六日天]?|周[一二三四五六日天]|"
    r"\d{1,2}:\d{2}|"
    r"\d{1,2}\s*分钟前|"
    r"(?:\d{2,4}[-/年])?\d{1,2}[-/月]\d{1,2}(?:日)?|"
    r"[/\\]?\d{1,2}[/\\]\d{1,2}"
    r")$"
)
FILE_SIZE_RE = re.compile(r"^\d+(?:\.\d+)?\s*(?:[KMGT]B?|KB|MB|GB|TB)$", re.IGNORECASE)
UI_TEXT = {
    "归档",
    "置顶",
    "取消",
    "人员",
    "项目",
    "文档、微盘等",
    "文档、微盘",
    "微盘等",
}
SKIP_CONTACT_KEYWORDS = {
    "行业资讯",
    "人事助手",
    "企业微信团队",
    "企业邮箱",
    "邮件提醒",
    "服务通知",
    "管理企业",
}
BUSINESS_RELEVANCE_KEYWORDS = {
    "车",
    "汽车",
    "新能源",
    "保养",
    "维修",
    "修理",
    "检测",
    "检查",
    "故障",
    "异响",
    "抖动",
    "刹车",
    "制动",
    "轮胎",
    "补胎",
    "机油",
    "发动机",
    "变速箱",
    "电池",
    "三电",
    "续航",
    "油耗",
    "电耗",
    "费油",
    "耗油",
    "耗电",
    "公里",
    "空调",
    "冷却液",
    "底盘",
    "钣金",
    "喷漆",
    "洗车",
    "门店",
    "到店",
    "预约",
    "报价",
    "价格",
    "多少钱",
    "怎么收费",
}
GREETING_RE = re.compile(r"^(?:你?好|您好|在吗|在不在|有人吗|咨询一下|问一下|请问)[!！。,.，\s]*$")


def _load_cfg(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing config: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def _norm_text(text: str) -> str:
    return " ".join(str(text or "").replace("\u3000", " ").split()).strip()


def _strip_emoji(text: str) -> str:
    return re.sub(
        "["
        "\U0001f300-\U0001f5ff"
        "\U0001f600-\U0001f64f"
        "\U0001f680-\U0001f6ff"
        "\U0001f700-\U0001f77f"
        "\U0001f780-\U0001f7ff"
        "\U0001f800-\U0001f8ff"
        "\U0001f900-\U0001f9ff"
        "\U0001fa00-\U0001fa6f"
        "\U0001fa70-\U0001faff"
        "\u2600-\u27bf"
        "]+",
        "",
        text,
    )


def _sanitize_reply(text: str) -> str:
    return _norm_text(_strip_emoji(text))


def _match_key_text(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", _norm_text(text)).lower()


def _text_matches_reply(candidate: str, reply: str) -> bool:
    cand = _match_key_text(candidate)
    rep = _match_key_text(reply)
    if not cand or not rep:
        return False
    if rep[: min(18, len(rep))] and rep[: min(18, len(rep))] in cand:
        return True
    if cand[: min(18, len(cand))] and cand[: min(18, len(cand))] in rep:
        return True
    return SequenceMatcher(None, cand, rep).ratio() >= 0.62


def _hash_text(text: str, length: int = 16) -> str:
    return hashlib.sha1(_norm_text(text).encode("utf-8")).hexdigest()[:length]


def _slug(text: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text.strip())
    return safe[:40] or "contact"


def _message_type(text: str) -> str:
    s = _norm_text(text)
    lower = s.lower()
    if not s:
        return "empty"
    if lower.startswith(("http://", "https://")) or "链接" in s:
        return "link"
    if re.search(r"\.(zip|rar|7z|pdf|docx?|xlsx?|pptx?|jpg|jpeg|png|gif|mp4|mov)\b", lower):
        return "file"
    if "[图片]" in s or "图片" == s:
        return "image"
    if "[视频]" in s or "视频" == s:
        return "video"
    return "text"


def _looks_like_sender_label(text: str) -> bool:
    s = _norm_text(text)
    return bool(re.fullmatch(r"[\w\u4e00-\u9fff .·（）()_-]{1,24}\s*@\s*微信", s))


def _vehicle_terms(text: str) -> list[str]:
    patterns = [
        r"比亚迪\s*秦\s*PLUS(?:\s*DM-i|(?:\s*混动)?)?",
        r"秦\s*PLUS(?:\s*DM-i|(?:\s*混动)?)?",
        r"比亚迪\s*海豹",
        r"小鹏\s*P7",
        r"特斯拉",
        r"理想\s*L\s*系列",
        r"理想\s*L[789]",
        r"问界\s*M[579]",
        r"蔚来\s*(?:ES|ET|EC)\s*\d",
    ]
    terms: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text or "", flags=re.I):
            term = _norm_text(match.group(0))
            if term and term not in terms:
                terms.append(term)
    return terms


def _business_relevant(text: str) -> bool:
    s = _norm_text(text)
    if not s:
        return False
    if GREETING_RE.fullmatch(s):
        return True
    return any(keyword in s for keyword in BUSINESS_RELEVANCE_KEYWORDS)


def _scan_business_relevant(scan: LiveScan) -> bool:
    if scan.latest_incoming and _business_relevant(scan.latest_incoming.text):
        return True
    recent = "\n".join(msg.text for msg in scan.messages[-8:])
    return _business_relevant(recent)


def _reply_quality_block_reason(scan: LiveScan, reply: str) -> str:
    text = _norm_text(reply)
    if not text:
        return "empty_reply"
    max_chars = 140
    if len(text) > max_chars:
        return f"reply_too_long:{len(text)}>{max_chars}"
    forbidden = (
        "手机号",
        "手机号码",
        "电话",
        "联系方式",
        "微信号",
        "加微信",
        "留个电话",
        "留电话",
    )
    if any(word in text for word in forbidden):
        return "forbidden_contact_request"
    latest = scan.latest_incoming.text if scan.latest_incoming else ""
    latest_key = re.sub(r"\s+", "", latest).lower()
    for term in _vehicle_terms(text):
        term_key = re.sub(r"\s+", "", term).lower()
        if term_key and term_key not in latest_key:
            return f"unmentioned_vehicle:{term}"
    return ""


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


def _noise_reason(text: str) -> str:
    s = _norm_text(text)
    if not s:
        return "empty"
    if _looks_like_sender_label(s):
        return "sender_label"
    if len(s) < 2:
        return "too_short"
    if s in UI_TEXT:
        return "ui"
    if TIME_MARKER_RE.fullmatch(s):
        return "time_marker"
    if FILE_SIZE_RE.fullmatch(s):
        return "file_size"
    if re.fullmatch(r"[\\/]\d{1,2}[\\/]\d{1,2}", s):
        return "date_fragment"
    return ""


def _parse_region_ratios(cfg: dict[str, Any]) -> RegionRatios:
    live = cfg.get("live_reply") or {}
    cap = live.get("capture") or cfg.get("capture") or {}
    return RegionRatios(
        left=float(cap.get("left_ratio", 0.38)),
        top=float(cap.get("top_ratio", 0.12)),
        width=float(cap.get("width_ratio", 0.60)),
        height=float(cap.get("height_ratio", 0.56)),
    )


def _full_region() -> RegionRatios:
    return RegionRatios(left=0.0, top=0.0, width=1.0, height=1.0)


def _fallback_layout_cfg(cfg: dict[str, Any]) -> dict[str, float]:
    chat = _parse_region_ratios(cfg)
    listing = _list_ratios(cfg)
    return {
        "chat_left": chat.left,
        "chat_top": chat.top,
        "chat_width": chat.width,
        "chat_height": chat.height,
        "list_left": listing.left,
        "list_top": listing.top,
        "list_width": listing.width,
        "list_height": listing.height,
    }


def _layout_from_cfg(hwnd: int, cfg: dict[str, Any], *, run_dir: Path | None = None) -> MainLayout | None:
    live_cfg = _live_cfg(cfg)
    layout_cfg = live_cfg.get("layout") or {}
    if not bool(layout_cfg.get("auto_detect", True)):
        return None
    tmp, _w, _h, _digest = capture_window_region_temp_png_meta(hwnd, _full_region())
    try:
        layout = detect_main_layout_regions(tmp, fallback=_fallback_layout_cfg(cfg))
        if run_dir is not None and bool(layout_cfg.get("save_debug", False)):
            _copy_capture(tmp, run_dir, "layout_full_window")
        if bool(layout_cfg.get("log", False)):
            print(
                ">>> layout "
                f"source={layout.source} confidence={layout.confidence:.2f} "
                f"list=({layout.list_left:.3f},{layout.list_top:.3f},{layout.list_width:.3f},{layout.list_height:.3f}) "
                f"chat=({layout.chat_left:.3f},{layout.chat_top:.3f},{layout.chat_width:.3f},{layout.chat_height:.3f})",
                flush=True,
            )
        return layout
    finally:
        tmp.unlink(missing_ok=True)


def _chat_ratios(cfg: dict[str, Any], layout: MainLayout | None = None) -> RegionRatios:
    if layout is not None:
        return RegionRatios(left=layout.chat_left, top=layout.chat_top, width=layout.chat_width, height=layout.chat_height)
    return _parse_region_ratios(cfg)


def _list_ratios_from_layout(cfg: dict[str, Any], layout: MainLayout | None = None) -> ListRatios:
    if layout is not None:
        return ListRatios(left=layout.list_left, top=layout.list_top, width=layout.list_width, height=layout.list_height)
    return _list_ratios(cfg)


def _live_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("live_reply") or {}


def _list_ratios(cfg: dict[str, Any]) -> ListRatios:
    agent_cfg = cfg.get("agent_loop") or {}
    return ListRatios(
        left=float(agent_cfg.get("list_left_ratio", 0.045)),
        top=float(agent_cfg.get("list_top_ratio", 0.06)),
        width=float(agent_cfg.get("list_width_ratio", 0.225)),
        height=float(agent_cfg.get("list_height_ratio", 0.88)),
    )


def _entry_allowed(entry: ContactEntry, allowed_contacts: list[str], *, all_visible: bool) -> bool:
    hay = f"{entry.name} {entry.preview}".strip()
    if not hay:
        return False
    if any(x and x in hay for x in SKIP_CONTACT_KEYWORDS):
        return False
    if all_visible:
        return "@微信" in entry.name or "外部" in entry.name or _business_relevant(hay)
    if allowed_contacts and any(item.lower() in hay.lower() or hay.lower() in item.lower() for item in allowed_contacts if item.strip()):
        return True
    return False


def _visible_entries(
    hwnd: int,
    cfg: dict[str, Any],
    allowed_contacts: list[str],
    *,
    all_visible: bool,
    limit: int,
    state: dict[str, Any] | None = None,
    layout: MainLayout | None = None,
) -> list[ContactEntry]:
    entries, _digest = capture_and_ocr_list(hwnd, _list_ratios_from_layout(cfg, layout))
    followed = set()
    for name, row in (state or {}).get("contacts", {}).items():
        if isinstance(row, dict) and (row.get("sent_keys") or row.get("seen_keys") or row.get("sent_replies")):
            followed.add(str(name).lower())
    filtered = [
        entry
        for entry in entries
        if _entry_allowed(entry, allowed_contacts, all_visible=all_visible)
        or entry.name.lower() in followed
        or any(entry.name.lower() in name or name in entry.name.lower() for name in followed)
    ]
    return filtered[: max(1, limit)]


def _entry_matches(a: ContactEntry, b: ContactEntry) -> bool:
    if _names_match(a.name, b.name):
        return True
    hay = f"{b.name} {b.preview}".lower()
    an = _entry_name_key(a.name)
    return bool(an and an in _entry_name_key(hay))


def _find_visible_entry(entries: list[ContactEntry], target: ContactEntry) -> ContactEntry | None:
    for entry in entries:
        if _entry_matches(target, entry):
            return entry
    return None


def _entry_queue_key(entry: ContactEntry) -> str:
    name_key = _entry_name_key(entry.name) or entry.name.lower().strip()
    preview = _preview_key(entry.preview)
    return f"{name_key}|{_hash_text(preview, 10)}"


def _entry_contact_key(entry: ContactEntry) -> str:
    return _entry_name_key(entry.name) or entry.name.lower().strip()


def _enqueue_changed(
    queue: list[ContactEntry],
    entries: list[ContactEntry],
    *,
    queued_keys: set[str],
    ignore_keys: set[str] | None = None,
    state: dict[str, Any] | None = None,
    skip_recent_sent: bool = False,
) -> None:
    ignored = ignore_keys or set()
    for entry in entries:
        if skip_recent_sent and state is not None and _recently_sent(state, entry.name):
            continue
        key = _entry_queue_key(entry)
        if key in ignored:
            continue
        if key in queued_keys:
            continue
        queue.append(entry)
        queued_keys.add(key)


def _enqueue_periodic_visible(
    queue: list[ContactEntry],
    entries: list[ContactEntry],
    *,
    queued_keys: set[str],
    state: dict[str, Any],
    cooldowns: dict[str, float],
    interval_sec: float,
    skip_recent_sent: bool = True,
) -> int:
    now = time.time()
    added = 0
    for entry in entries:
        if skip_recent_sent and _recently_sent(state, entry.name):
            continue
        contact_key = _entry_contact_key(entry)
        if not contact_key:
            continue
        if float(cooldowns.get(contact_key) or 0.0) > now:
            continue
        queue_key = _entry_queue_key(entry)
        if queue_key in queued_keys:
            continue
        queue.append(entry)
        queued_keys.add(queue_key)
        cooldowns[contact_key] = now + interval_sec
        added += 1
    return added


def _open_visible_entry(hwnd: int, cfg: dict[str, Any], entry: ContactEntry, *, pause_after: float, layout: MainLayout | None = None) -> None:
    region = _mss_region_for_ratios(hwnd, _list_ratios_from_layout(cfg, layout).as_region_ratios())
    x = int(region["left"] + region["width"] * 0.55)
    y = int(region["top"] + max(1.0, float(entry.cy)))
    click_screen_point(hwnd, x, y, pause_after=pause_after)


def _parse_messages(
    boxes: list[Any],
    *,
    image_path: Path,
    region_w: int,
    region_h: int,
    parse_cfg: dict[str, Any],
) -> list[LiveMessage]:
    left_dead = float(parse_cfg.get("left_dead_zone_ratio", 0.005))
    right_dead = float(parse_cfg.get("right_dead_zone_ratio", 0.985))
    top_dead = float(parse_cfg.get("top_dead_zone_ratio", 0.035))
    bottom_dead = float(parse_cfg.get("bottom_dead_zone_ratio", 0.985))
    split = float(parse_cfg.get("direction_split_ratio", 0.58))
    line_merge_px = float(parse_cfg.get("line_merge_px", 42))
    y_bucket_px = max(8, int(parse_cfg.get("y_bucket_px", 24)))
    self_blue_ratio = float(parse_cfg.get("self_blue_ratio", 0.08))

    messages: list[LiveMessage] = []
    with Image.open(image_path) as raw_img:
        img = raw_img.convert("RGB")
        bubbles = attach_boxes_to_chat_bubbles(
            boxes,
            detect_chat_bubbles(image_path),
            image_width=region_w,
            image_height=region_h,
        )
        for bubble in sorted(bubbles, key=lambda b: (b.y0, b.x0)):
            parts: list[str] = []
            text_boxes = sorted(bubble.texts, key=lambda b: (float(getattr(b, "cy", 0.0)), float(getattr(b, "cx", 0.0))))
            for box in text_boxes:
                part = _norm_text(str(getattr(box, "text", "")))
                if _noise_reason(part):
                    continue
                parts.append(part)
            text = "\n".join(parts).strip()
            if not text:
                continue
            cx = float(bubble.cx)
            cy = float(bubble.cy)
            message_type = _message_type(text)
            messages.append(
                LiveMessage(
                    direction="outgoing" if bubble.kind == "outgoing" else "incoming",
                    text=text,
                    message_type=message_type,
                    cx=round(cx, 1),
                    cy=round(cy, 1),
                    y_bucket=int(cy // y_bucket_px),
                    bg_blue_ratio=1.0 if bubble.kind == "outgoing" else 0.0,
                )
            )
        if messages:
            return messages

        for box in sorted(boxes, key=lambda b: (float(b.cy), float(b.cx))):
            text = _norm_text(str(getattr(box, "text", "")))
            if _noise_reason(text):
                continue
            cx = float(getattr(box, "cx", 0.0))
            cy = float(getattr(box, "cy", 0.0))
            if cy < region_h * top_dead or cy > region_h * bottom_dead:
                continue
            if cx < region_w * left_dead or cx > region_w * right_dead:
                continue
            message_type = _message_type(text)
            blue_ratio = _self_bubble_ratio(img, cx, cy)
            direction = "outgoing" if blue_ratio >= self_blue_ratio else ("incoming" if cx < region_w * split else "outgoing")
            item = LiveMessage(
                direction=direction,
                text=text,
                message_type=message_type,
                cx=round(cx, 1),
                cy=round(cy, 1),
                y_bucket=int(cy // y_bucket_px),
                bg_blue_ratio=round(blue_ratio, 3),
            )
            if (
                messages
                and item.direction == messages[-1].direction
                and item.message_type == messages[-1].message_type == "text"
                and item.cy - messages[-1].cy <= line_merge_px
            ):
                prev = messages[-1]
                prev.text = f"{prev.text}\n{item.text}"
                prev.cx = round((prev.cx + item.cx) / 2, 1)
                prev.cy = item.cy
                prev.y_bucket = item.y_bucket
                prev.bg_blue_ratio = max(prev.bg_blue_ratio, item.bg_blue_ratio)
                continue
            messages.append(item)
    return messages


def _candidate_key(contact: str, msg: LiveMessage, last_outgoing: LiveMessage | None) -> str:
    contact_key = _entry_name_key(contact) or _slug(contact)
    out_text_hash = _hash_text(last_outgoing.text, 12) if last_outgoing else "none"
    out_bucket = last_outgoing.y_bucket if last_outgoing else -1
    return f"{contact_key}|{msg.direction}|{_hash_text(msg.text)}|y{msg.y_bucket}|after:{out_text_hash}:y{out_bucket}"


def _candidate_message_hash(key: str) -> str:
    parts = str(key or "").split("|")
    if len(parts) >= 3 and re.fullmatch(r"[0-9a-f]{8,40}", parts[2]):
        return parts[2]
    return ""


def _iso_age_seconds(value: str) -> float | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except Exception:
        return None
    return max(0.0, (datetime.now() - dt).total_seconds())


def _prefer_contact_state_name(existing: str, candidate: str) -> str:
    existing = _norm_text(existing)
    candidate = _norm_text(candidate)
    if not existing:
        return candidate
    if not candidate:
        return existing
    if "@微信" in candidate and "@微信" not in existing:
        return candidate
    if len(candidate) > len(existing) and _entry_name_key(existing) in _entry_name_key(candidate):
        return candidate
    return existing


def _state_contact(state: dict[str, Any], contact: str) -> dict[str, Any]:
    contacts = state.setdefault("contacts", {})
    for existing in list(contacts.keys()):
        if _names_match(str(existing), contact):
            preferred = _prefer_contact_state_name(str(existing), contact)
            if preferred != existing:
                contacts[preferred] = contacts.pop(existing)
                existing = preferred
            return contacts.setdefault(existing, {"seen_keys": [], "sent_keys": [], "attempted_keys": []})
    return contacts.setdefault(contact, {"seen_keys": [], "sent_keys": [], "attempted_keys": []})


def _remember_key(state: dict[str, Any], contact: str, key: str, bucket: str) -> None:
    if not key:
        return
    item = {
        "key": key,
        "message_hash": _candidate_message_hash(key),
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    cstate = _state_contact(state, contact)
    rows = [x for x in cstate.setdefault(bucket, []) if isinstance(x, dict) and x.get("key") != key]
    rows.append(item)
    cstate[bucket] = rows[-200:]


def _has_key(state: dict[str, Any], contact: str, key: str, *, include_seen: bool = True) -> bool:
    cstate = _state_contact(state, contact)
    buckets = ("seen_keys", "sent_keys", "attempted_keys") if include_seen else ("sent_keys", "attempted_keys")
    msg_hash = _candidate_message_hash(key)
    for bucket in buckets:
        for row in cstate.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            if row.get("key") == key:
                return True
            row_hash = str(row.get("message_hash") or _candidate_message_hash(str(row.get("key") or "")))
            if msg_hash and row_hash == msg_hash:
                age = _iso_age_seconds(str(row.get("at") or ""))
                if age is not None and age <= 900:
                    return True
    return False


def _remember_sent_reply(state: dict[str, Any], contact: str, reply: str) -> None:
    text = _norm_text(reply)
    if not text:
        return
    cstate = _state_contact(state, contact)
    rows = [x for x in cstate.setdefault("sent_replies", []) if isinstance(x, dict)]
    rows.append(
        {
            "hash": _hash_text(text),
            "text": text,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    cstate["sent_replies"] = rows[-20:]
    cstate["last_sent_at"] = time.time()


def _looks_like_recent_sent_reply(state: dict[str, Any], contact: str, text: str) -> bool:
    incoming = _norm_text(text)
    if len(incoming) < 4:
        return False
    cstate = _state_contact(state, contact)
    for row in reversed(cstate.get("sent_replies") or []):
        if not isinstance(row, dict):
            continue
        sent = _norm_text(str(row.get("text") or ""))
        if len(sent) < 4:
            continue
        if incoming == sent:
            return True
        if len(incoming) >= 6 and incoming in sent:
            return True
        if len(sent) >= 6 and sent in incoming:
            return True
    return False


def _recently_sent(state: dict[str, Any], contact: str, *, within_sec: float = 20.0) -> bool:
    cstate = _state_contact(state, contact)
    try:
        last = float(cstate.get("last_sent_at") or 0.0)
    except (TypeError, ValueError):
        return False
    return last > 0 and time.time() - last <= within_sec


def _build_prompt(contact: str, scan: LiveScan) -> str:
    rows: list[str] = []
    for msg in scan.messages[-10:]:
        who = "customer" if msg.direction == "incoming" else "self"
        marker = " <-- new" if scan.latest_incoming and msg is scan.latest_incoming else ""
        rows.append(f"[visible] {who} {msg.message_type}: {msg.text}{marker}")
    return (
        f"会话：{contact}\n\n"
        f"最近可见聊天记录：\n{chr(10).join(rows)}\n\n"
        "请只回复最后标记为 new 的客户消息。"
        "如果信息不足，简短追问一个关键问题。"
        "不要编造价格、日期、订单号或承诺。"
        "只输出要发送给客户的正文。"
    )


def _make_reply_client(cfg: dict[str, Any], *, send_enabled: bool) -> ReplyClient | None:
    provider = str((cfg.get("reply_engine") or {}).get("provider") or "qwen").strip().lower()
    if provider == "qwen":
        qwen_cfg = cfg.get("qwen") or {}
        api_key = resolve_dashscope_api_key(qwen_cfg)
        if not api_key:
            if send_enabled:
                raise RuntimeError("Missing DashScope API key")
            return None
        return QwenTextClient(
            api_key=api_key,
            base_url=str(qwen_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
            model=str(qwen_cfg.get("model", "qwen-turbo")),
            temperature=float(qwen_cfg.get("temperature", 0.3)),
            timeout=int(qwen_cfg.get("timeout", 60)),
        )
    if provider == "revai":
        revai_cfg = cfg.get("revai") or {}
        return RevaiQaClient(
            base_url=str(revai_cfg.get("base_url", "https://car.revai.cn")),
            endpoint=str(revai_cfg.get("endpoint", "/api/project/carzone/compat/chat/respond")),
            api_key=revai_cfg.get("api_key"),
            timeout=int(revai_cfg.get("timeout", 60)),
            session_prefix=str(revai_cfg.get("session_prefix", "wxwork-live")),
            message_mode=str(revai_cfg.get("message_mode", "latest_new")),
            channel=str(revai_cfg.get("channel", "wxwork")),
        )
    raise RuntimeError(f"Unsupported reply_engine.provider: {provider}")


def _copy_capture(tmp: Path, run_dir: Path, contact: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    out = run_dir / f"{stamp}_{_slug(contact)}.png"
    shutil.copyfile(tmp, out)
    return out


def _click_back_to_latest_if_present(
    hwnd: int,
    ratios: RegionRatios,
    boxes: list[Any],
    *,
    region_w: int,
    region_h: int,
) -> bool:
    _ = (region_w, region_h)
    region = mss_region_for_ratios(hwnd, ratios)
    for box in boxes:
        text = _norm_text(str(getattr(box, "text", "")))
        if "回到最新位置" not in text and "最新位置" not in text:
            continue
        x = int(region["left"] + float(getattr(box, "cx", 0.0)))
        y = int(region["top"] + float(getattr(box, "cy", 0.0)))
        click_screen_point(hwnd, x, y, pause_after=0.8)
        return True
    return False


def _scroll_chat_to_latest(hwnd: int, cfg: dict[str, Any], *, layout: MainLayout | None = None) -> None:
    live_cfg = _live_cfg(cfg)
    if not bool(live_cfg.get("scroll_to_latest_on_open", True)):
        return
    ratios = _chat_ratios(cfg, layout)
    clicks = int(live_cfg.get("scroll_to_latest_clicks", 8))
    if clicks <= 0:
        return
    rx = ratios.left + ratios.width * float(live_cfg.get("scroll_x_in_capture", 0.78))
    ry = ratios.top + ratios.height * float(live_cfg.get("scroll_y_in_capture", 0.78))
    scroll_client_ratio(hwnd, rx, ry, -abs(clicks), pause_after=float(live_cfg.get("scroll_to_latest_pause_sec", 0.45)))


def _click_message_composer(hwnd: int, cfg: dict[str, Any], *, from_bottom_ratio: float, layout: MainLayout | None = None) -> None:
    if layout is None:
        click_message_composer(hwnd, from_bottom_ratio=from_bottom_ratio)
        return
    full = mss_region_for_ratios(hwnd, _full_region())
    x_ratio = layout.chat_left + layout.chat_width * 0.50
    x = int(full["left"] + full["width"] * x_ratio)
    y = int(full["top"] + full["height"] * (1.0 - from_bottom_ratio))
    click_screen_point(hwnd, x, y, pause_after=0.18)


def scan_contact(
    hwnd: int,
    contact: str,
    *,
    cfg: dict[str, Any],
    run_dir: Path,
    state: dict[str, Any],
    open_contact: bool,
    after_search_sec: float,
    allow_click_latest: bool,
    layout: MainLayout | None = None,
) -> LiveScan:
    ratios = _chat_ratios(cfg, layout)
    live_cfg = _live_cfg(cfg)
    parse_cfg = dict(live_cfg.get("parse") or {})
    parse_cfg["_send_mode"] = bool((cfg.get("_runtime") or {}).get("send_enabled"))

    activate_hwnd(hwnd)
    time.sleep(0.15)
    if open_contact:
        search_and_open_contact(contact, pause_after_enter=after_search_sec, hwnd=hwnd)
    _scroll_chat_to_latest(hwnd, cfg, layout=layout)
    ensure_visible(hwnd)

    tmp, region_w, region_h, digest = capture_window_region_temp_png_meta(hwnd, ratios)
    try:
        shot = _copy_capture(tmp, run_dir, contact)
    finally:
        tmp.unlink(missing_ok=True)
    boxes = ocr_image_text_boxes(shot)

    if allow_click_latest and _click_back_to_latest_if_present(
        hwnd,
        ratios,
        boxes,
        region_w=region_w,
        region_h=region_h,
    ):
        tmp, region_w, region_h, digest = capture_window_region_temp_png_meta(hwnd, ratios)
        try:
            shot = _copy_capture(tmp, run_dir, contact)
        finally:
            tmp.unlink(missing_ok=True)
        boxes = ocr_image_text_boxes(shot)

    raw_texts = [_norm_text(str(getattr(box, "text", ""))) for box in boxes]
    if any("群成员" in text or "群公告" in text for text in raw_texts):
        return LiveScan(
            contact=contact,
            screenshot=str(shot),
            screen_hash=digest,
            messages=[],
            last_outgoing=None,
            latest_incoming=None,
            candidate_key="",
            should_reply=False,
            reason="group chat skipped",
        )

    messages = _parse_messages(boxes, image_path=shot, region_w=region_w, region_h=region_h, parse_cfg=parse_cfg)
    allowed_types = {str(x) for x in live_cfg.get("reply_message_types") or (cfg.get("agent_loop") or {}).get("reply_message_types") or ["text"]}

    last_outgoing: LiveMessage | None = None
    last_outgoing_idx = -1
    for idx, msg in enumerate(messages):
        if msg.direction == "outgoing":
            last_outgoing = msg
            last_outgoing_idx = idx

    latest: LiveMessage | None = None
    latest_idx = -1
    for idx in range(len(messages) - 1, last_outgoing_idx, -1):
        msg = messages[idx]
        if msg.direction == "incoming" and msg.message_type in allowed_types:
            latest = msg
            latest_idx = idx
            break

    if latest is None:
        key = ""
        reason = "no incoming text after latest outgoing"
        should_reply = False
    else:
        key = _candidate_key(contact, latest, last_outgoing)
        if latest_idx <= last_outgoing_idx:
            reason = "incoming is not after latest outgoing"
            should_reply = False
        elif _has_key(state, contact, key, include_seen=not bool(parse_cfg.get("_send_mode", False))):
            reason = "candidate already seen/sent"
            should_reply = False
        elif _looks_like_recent_sent_reply(state, contact, latest.text):
            reason = "candidate is self echo"
            should_reply = False
        elif bool(live_cfg.get("require_business_relevance", True)) and not _scan_business_relevant(
            LiveScan(
                contact=contact,
                screenshot=str(shot),
                screen_hash=digest,
                messages=messages,
                last_outgoing=last_outgoing,
                latest_incoming=latest,
                candidate_key=key,
                should_reply=True,
                reason="",
            )
        ):
            reason = "not business relevant"
            should_reply = False
        else:
            reason = "latest incoming after latest outgoing"
            should_reply = True

    return LiveScan(
        contact=contact,
        screenshot=str(shot),
        screen_hash=digest,
        messages=messages,
        last_outgoing=last_outgoing,
        latest_incoming=latest,
        candidate_key=key,
        should_reply=should_reply,
        reason=reason,
    )


def _print_scan(scan: LiveScan, *, prefix: str = "scan") -> None:
    latest = scan.latest_incoming.text if scan.latest_incoming else ""
    last_out = scan.last_outgoing.text if scan.last_outgoing else ""
    print(
        f">>> {prefix} contact={scan.contact} messages={len(scan.messages)} "
        f"should_reply={scan.should_reply} reason={scan.reason}",
        flush=True,
    )
    if latest:
        print(f">>> latest incoming: {latest}", flush=True)
    if last_out:
        print(f">>> latest outgoing: {last_out}", flush=True)
    if not scan.should_reply and scan.messages:
        for msg in scan.messages[-6:]:
            preview = msg.text.replace("\n", " / ")
            print(
                f">>> msg {msg.direction} {msg.message_type} "
                f"cy={msg.cy} blue={msg.bg_blue_ratio}: {preview[:160]}",
                flush=True,
            )
    print(f">>> screenshot: {scan.screenshot}", flush=True)


def _scan_to_jsonable(scan: LiveScan) -> dict[str, Any]:
    data = asdict(scan)
    data["messages"] = [asdict(x) for x in scan.messages]
    data["last_outgoing"] = asdict(scan.last_outgoing) if scan.last_outgoing else None
    data["latest_incoming"] = asdict(scan.latest_incoming) if scan.latest_incoming else None
    return data


def _mark_baseline(state: dict[str, Any], scans: list[LiveScan]) -> None:
    for scan in scans:
        if scan.candidate_key:
            _remember_key(state, scan.contact, scan.candidate_key, "seen_keys")


def _reply_once(
    hwnd: int,
    scan: LiveScan,
    *,
    cfg: dict[str, Any],
    client: ReplyClient | None,
    send_enabled: bool,
    from_bottom_ratio: float,
    after_search_sec: float,
    reopen_contact: bool = True,
    layout: MainLayout | None = None,
) -> ReplyResult:
    if not scan.should_reply:
        return ReplyResult()
    if client is None:
        print(">>> no reply client configured; dry-run only", flush=True)
        return ReplyResult()
    system = str((cfg.get("reply") or {}).get("system") or "你是专业客服，用简短中文回复。")
    prompt = _build_prompt(scan.contact, scan)
    print(f">>> generate reply for {scan.contact}: {scan.latest_incoming.text if scan.latest_incoming else ''}", flush=True)
    reply = _sanitize_reply(client.complete(system, prompt, session_id=_slug(scan.contact)))
    print(f">>> reply: {reply}", flush=True)
    block_reason = _reply_quality_block_reason(scan, reply)
    if block_reason:
        print(f">>> skip send: {block_reason}", flush=True)
        return ReplyResult(reply=reply)
    attempted_send = False
    sent_confirmed = False
    if send_enabled:
        activate_hwnd(hwnd)
        time.sleep(0.15)
        if reopen_contact:
            search_and_open_contact(scan.contact, pause_after_enter=after_search_sec, hwnd=hwnd)
        _click_message_composer(hwnd, cfg, from_bottom_ratio=from_bottom_ratio, layout=layout)
        paste_and_send(reply, hwnd=hwnd, from_bottom_ratio=from_bottom_ratio)
        attempted_send = True
        if not _confirm_reply_visible(hwnd, cfg=cfg, base_scan=scan, reply=reply, layout=layout):
            print(">>> send not confirmed; state will mark attempted only", flush=True)
            return ReplyResult(reply=reply, attempted_send=attempted_send, sent_confirmed=False)
        sent_confirmed = True
    return ReplyResult(reply=reply, attempted_send=attempted_send, sent_confirmed=sent_confirmed)


def _confirm_reply_visible(hwnd: int, *, cfg: dict[str, Any], base_scan: LiveScan, reply: str, layout: MainLayout | None = None) -> bool:
    """Confirm a sent reply appears as the latest outgoing bubble."""
    ratios = _chat_ratios(cfg, layout)
    run_dir = Path(base_scan.screenshot).parent if base_scan.screenshot else Path("exports") / "wxwork_live_reply_confirm"
    wait_sec = float((_live_cfg(cfg)).get("send_confirm_wait_sec", 1.0))
    attempts = int((_live_cfg(cfg)).get("send_confirm_attempts", 3))
    for attempt in range(max(1, attempts)):
        time.sleep(wait_sec if attempt == 0 else 0.7)
        tmp, region_w, region_h, digest = capture_window_region_temp_png_meta(hwnd, ratios)
        try:
            shot = _copy_capture(tmp, run_dir, f"{base_scan.contact}_confirm")
        finally:
            tmp.unlink(missing_ok=True)
        try:
            boxes = ocr_image_text_boxes(shot)
            messages = _parse_messages(
                boxes,
                image_path=shot,
                region_w=region_w,
                region_h=region_h,
                parse_cfg=(_live_cfg(cfg).get("parse") or {}),
            )
        except Exception as e:
            print(f">>> send confirm OCR failed: {e}", flush=True)
            continue
        outgoing = [msg for msg in messages if msg.direction == "outgoing"]
        latest = outgoing[-1] if outgoing else None
        if latest and _text_matches_reply(latest.text, reply):
            print(f">>> send confirmed: {shot}", flush=True)
            return True
        latest_text = latest.text[:80] if latest else ""
        print(
            f">>> send confirm miss attempt={attempt + 1}/{attempts} hash={digest} "
            f"latest_outgoing={latest_text}",
            flush=True,
        )
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="WXWork narrow live auto-reply loop")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "config.live.local.yaml")
    parser.add_argument("--run-dir", type=Path, default=Path("exports") / "wxwork_live_reply")
    parser.add_argument("--state", type=Path, default=Path("exports") / "wxwork_live_reply" / "state.json")
    parser.add_argument("--contact", action="append", default=[], help="Contact to monitor; repeatable.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-search", action="store_true", help="Scan the currently selected chat without opening contacts.")
    parser.add_argument("--watch-visible-list", action="store_true", help="Watch visible conversation-list entries instead of fixed contacts.")
    parser.add_argument("--all-visible-contacts", action="store_true", help="With --watch-visible-list, ignore configured contact allow-list.")
    parser.add_argument("--force-scan-visible", action="store_true", help="With --watch-visible-list, scan visible entries even if preview did not change.")
    parser.add_argument("--visible-limit", type=int, default=6)
    parser.add_argument(
        "--periodic-visible-scan-sec",
        type=float,
        default=None,
        help="With --watch-visible-list, message-scan allowed visible contacts even when the list preview is unchanged.",
    )
    parser.add_argument("--interval-sec", type=float, default=None)
    parser.add_argument("--baseline-scans", type=int, default=1)
    parser.add_argument("--skip-warmup", action="store_true")
    parser.add_argument("--no-click-latest", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    cfg = _load_cfg(args.config)
    live_cfg = _live_cfg(cfg)
    contacts = args.contact or [str(x) for x in live_cfg.get("contacts") or (cfg.get("agent_loop") or {}).get("auto_reply_contacts") or []]
    contacts = [x for x in contacts if x.strip()]
    if not contacts and not args.no_search and not args.watch_visible_list:
        print(">>> no contacts configured; pass --contact or set agent_loop.auto_reply_contacts", flush=True)
        return 2

    send_enabled = bool(args.send and not args.dry_run)
    cfg.setdefault("_runtime", {})["send_enabled"] = send_enabled
    interval = float(args.interval_sec if args.interval_sec is not None else live_cfg.get("interval_sec", (cfg.get("agent_loop") or {}).get("interval_sec", 2.0)))
    periodic_visible_scan_sec = float(
        args.periodic_visible_scan_sec
        if args.periodic_visible_scan_sec is not None
        else live_cfg.get("periodic_visible_scan_sec", (cfg.get("agent_loop") or {}).get("periodic_visible_scan_sec", 8.0))
    )
    timing = cfg.get("timing") or {}
    inp = cfg.get("input") or {}
    after_search_sec = float(timing.get("after_search_sec", 1.0))
    from_bottom_ratio = float(inp.get("click_from_bottom_ratio", 0.10))

    if not args.skip_warmup:
        print(">>> OCR service warmup", flush=True)
        warmup_ocr_service()
    client = _make_reply_client(cfg, send_enabled=send_enabled)

    hwnd = ensure_wework_hwnd()
    if not hwnd:
        print(">>> WeWork window not found", flush=True)
        return 2

    state = _read_json(args.state)
    args.run_dir.mkdir(parents=True, exist_ok=True)

    if args.watch_visible_list:
        if args.no_search:
            print(">>> --watch-visible-list requires opening contacts from the list/search; remove --no-search", flush=True)
            return 2

        layout = _layout_from_cfg(hwnd, cfg, run_dir=args.run_dir)
        prev_entries = _visible_entries(
            hwnd,
            cfg,
            contacts,
            all_visible=args.all_visible_contacts,
            limit=args.visible_limit,
            state=state,
            layout=layout,
        )
        print(
            ">>> visible baseline entries="
            + ", ".join(f"{entry.name}:{entry.preview[:16]}" for entry in prev_entries),
            flush=True,
        )

        baseline_scans = max(0, int(args.baseline_scans))
        if baseline_scans and not args.once:
            for pass_i in range(baseline_scans):
                layout = _layout_from_cfg(hwnd, cfg, run_dir=args.run_dir)
                prev_entries = _visible_entries(
                    hwnd,
                    cfg,
                    contacts,
                    all_visible=args.all_visible_contacts,
                    limit=args.visible_limit,
                    state=state,
                    layout=layout,
                )
                print(
                    f">>> visible list baseline {pass_i + 1}/{baseline_scans}: "
                    + ", ".join(f"{entry.name}:{entry.preview[:16]}" for entry in prev_entries),
                    flush=True,
                )
                time.sleep(0.4)
            _write_json(args.state, state)
            print(f">>> visible list baseline ready mode={'send' if send_enabled else 'dry-run'} interval={interval}", flush=True)

        pending_entries: list[ContactEntry] = []
        pending_keys: set[str] = set()
        periodic_cooldowns: dict[str, float] = {}
        while True:
            layout = _layout_from_cfg(hwnd, cfg, run_dir=args.run_dir)
            current_entries = _visible_entries(
                hwnd,
                cfg,
                contacts,
                all_visible=args.all_visible_contacts,
                limit=args.visible_limit,
                state=state,
                layout=layout,
            )
            if args.force_scan_visible or args.once or not prev_entries:
                _enqueue_changed(pending_entries, current_entries, queued_keys=pending_keys, state=state)
            else:
                changed = changed_contacts(prev_entries, current_entries)
                _enqueue_changed(pending_entries, changed, queued_keys=pending_keys, state=state, skip_recent_sent=True)
                if periodic_visible_scan_sec > 0:
                    added = _enqueue_periodic_visible(
                        pending_entries,
                        current_entries,
                        queued_keys=pending_keys,
                        state=state,
                        cooldowns=periodic_cooldowns,
                        interval_sec=periodic_visible_scan_sec,
                        skip_recent_sent=True,
                    )
                    if added:
                        print(f">>> visible list periodic scan queued={added}", flush=True)
            prev_entries = current_entries

            scans_out: list[dict[str, Any]] = []
            if not pending_entries:
                print(">>> visible list no changed contacts", flush=True)
            processed_entry_keys: set[str] = set()
            while pending_entries:
                queued_entry = pending_entries.pop(0)
                queued_key = _entry_queue_key(queued_entry)
                pending_keys.discard(queued_key)
                processed_entry_keys.add(queued_key)
                print(f">>> visible target queued: {queued_entry.name} preview={queued_entry.preview[:40]}", flush=True)
                latest_entries = _visible_entries(
                    hwnd,
                    cfg,
                    contacts,
                    all_visible=args.all_visible_contacts,
                    limit=args.visible_limit,
                    state=state,
                    layout=layout,
                )
                entry = _find_visible_entry(latest_entries, queued_entry)
                if entry is None:
                    print(f">>> visible target no longer visible: {queued_entry.name}", flush=True)
                    continue
                print(f">>> visible target: {entry.name} preview={entry.preview[:40]}", flush=True)
                _open_visible_entry(hwnd, cfg, entry, pause_after=after_search_sec, layout=layout)
                chat_layout = _layout_from_cfg(hwnd, cfg, run_dir=args.run_dir) or layout
                scan = scan_contact(
                    hwnd,
                    entry.name,
                    cfg=cfg,
                    run_dir=args.run_dir,
                    state=state,
                    open_contact=False,
                    after_search_sec=after_search_sec,
                    allow_click_latest=not args.no_click_latest,
                    layout=chat_layout,
                )
                _print_scan(scan)
                result = _reply_once(
                    hwnd,
                    scan,
                    cfg=cfg,
                    client=client,
                    send_enabled=send_enabled,
                    from_bottom_ratio=from_bottom_ratio,
                    after_search_sec=after_search_sec,
                    reopen_contact=False,
                    layout=chat_layout,
                )
                reply = result.reply
                if scan.candidate_key and (not send_enabled or reply):
                    if send_enabled and result.sent_confirmed:
                        bucket = "sent_keys"
                    elif send_enabled and result.attempted_send:
                        bucket = "attempted_keys"
                    else:
                        bucket = "seen_keys"
                    _remember_key(state, scan.contact, scan.candidate_key, bucket)
                    if send_enabled and result.attempted_send and reply:
                        _remember_sent_reply(state, scan.contact, reply)
                    _write_json(args.state, state)
                row = _scan_to_jsonable(scan)
                row["list_preview"] = entry.preview
                if reply:
                    row["reply"] = reply
                    row["attempted_send"] = result.attempted_send
                    row["sent_confirmed"] = result.sent_confirmed
                scans_out.append(row)
                if send_enabled and result.attempted_send:
                    time.sleep(float(live_cfg.get("post_send_pause_sec", (cfg.get("watch") or {}).get("post_send_pause_sec", 1.2))))
            latest_after = _visible_entries(
                hwnd,
                cfg,
                contacts,
                all_visible=args.all_visible_contacts,
                limit=args.visible_limit,
                state=state,
                layout=layout,
            )
            if not args.once:
                new_changes = changed_contacts(prev_entries, latest_after)
                _enqueue_changed(
                    pending_entries,
                    new_changes,
                    queued_keys=pending_keys,
                    ignore_keys=processed_entry_keys,
                    state=state,
                    skip_recent_sent=True,
                )
            prev_entries = latest_after
            _write_json(args.run_dir / "last_scan.json", {"scanned_at": datetime.now().isoformat(timespec="seconds"), "scans": scans_out})
            if args.once:
                return 0
            time.sleep(interval)

    baseline_scans = max(0, int(args.baseline_scans))
    if baseline_scans and not args.once:
        for pass_i in range(baseline_scans):
            scans: list[LiveScan] = []
            for contact in contacts or ["current"]:
                layout = _layout_from_cfg(hwnd, cfg, run_dir=args.run_dir)
                scan = scan_contact(
                    hwnd,
                    contact,
                    cfg=cfg,
                    run_dir=args.run_dir,
                    state=state,
                    open_contact=not args.no_search,
                    after_search_sec=after_search_sec,
                    allow_click_latest=not args.no_click_latest,
                    layout=layout,
                )
                scans.append(scan)
                _print_scan(scan, prefix=f"baseline {pass_i + 1}/{baseline_scans}")
            _mark_baseline(state, scans)
            _write_json(args.state, state)
            time.sleep(0.4)
        print(f">>> baseline ready mode={'send' if send_enabled else 'dry-run'} interval={interval}", flush=True)

    while True:
        scans_out: list[dict[str, Any]] = []
        for contact in contacts or ["current"]:
            hwnd = ensure_wework_hwnd()
            if not hwnd:
                print(">>> WeWork window lost", flush=True)
                if args.once:
                    return 2
                continue
            layout = _layout_from_cfg(hwnd, cfg, run_dir=args.run_dir)
            scan = scan_contact(
                hwnd,
                contact,
                cfg=cfg,
                run_dir=args.run_dir,
                state=state,
                open_contact=not args.no_search,
                after_search_sec=after_search_sec,
                allow_click_latest=not args.no_click_latest,
                layout=layout,
            )
            _print_scan(scan)
            result = _reply_once(
                hwnd,
                scan,
                cfg=cfg,
                client=client,
                send_enabled=send_enabled,
                from_bottom_ratio=from_bottom_ratio,
                after_search_sec=after_search_sec,
                layout=layout,
            )
            reply = result.reply
            if scan.candidate_key and (not send_enabled or reply):
                if send_enabled and result.sent_confirmed:
                    bucket = "sent_keys"
                elif send_enabled and result.attempted_send:
                    bucket = "attempted_keys"
                else:
                    bucket = "seen_keys"
                _remember_key(state, scan.contact, scan.candidate_key, bucket)
                if send_enabled and result.attempted_send and reply:
                    _remember_sent_reply(state, scan.contact, reply)
                _write_json(args.state, state)
            row = _scan_to_jsonable(scan)
            if reply:
                row["reply"] = reply
                row["attempted_send"] = result.attempted_send
                row["sent_confirmed"] = result.sent_confirmed
            scans_out.append(row)
            if send_enabled and result.attempted_send:
                time.sleep(float(live_cfg.get("post_send_pause_sec", (cfg.get("watch") or {}).get("post_send_pause_sec", 1.2))))
        _write_json(args.run_dir / "last_scan.json", {"scanned_at": datetime.now().isoformat(timespec="seconds"), "scans": scans_out})
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
