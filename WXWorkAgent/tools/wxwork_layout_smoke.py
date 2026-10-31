"""Offline smoke tests for WeWork UI layout parsing."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.contact_list import parse_boxes_to_entries
from src.ocr_run import OcrTextBox
from wxwork_live_reply import _parse_messages


def _rapid_boxes(path: Path) -> list[OcrTextBox]:
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    result, _elapsed = engine(str(path))
    boxes: list[OcrTextBox] = []
    for row in result or []:
        if not row or len(row) < 2:
            continue
        poly = row[0]
        text = str(row[1]).strip()
        if not text:
            continue
        cx = sum(float(p[0]) for p in poly) / max(len(poly), 1)
        cy = sum(float(p[1]) for p in poly) / max(len(poly), 1)
        boxes.append(OcrTextBox(text=text, cx=cx, cy=cy))
    return boxes


def _image_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as img:
        return img.size


def _messages(path: Path):
    boxes = _rapid_boxes(path)
    w, h = _image_size(path)
    return _parse_messages(boxes, image_path=path, region_w=w, region_h=h, parse_cfg={})


def main() -> int:
    live_loop = ROOT / "exports" / "wxwork_live_reply_live" / "20260607_150751_811_刘廷曦_微信.png"
    juice = ROOT / "exports" / "wxwork_visible_list_probe3" / "20260607_152058_333_新能源车保养主.png"
    group = ROOT / "exports" / "wxwork_visible_list_probe" / "20260607_151716_235_陶薇薇-铭泰佳.png"
    list_shot = ROOT / "exports" / "wxwork_visible_list_debug" / "list.png"

    live_msgs = _messages(live_loop)
    assert live_msgs, "live-loop screenshot should produce messages"
    assert live_msgs[-1].direction == "outgoing", live_msgs[-1]
    assert all("收到～已为您标记" not in m.text or m.direction == "outgoing" for m in live_msgs), live_msgs
    print("live_loop last:", live_msgs[-1].direction, live_msgs[-1].text[:40])

    juice_msgs = _messages(juice)
    assert juice_msgs, "JuiceAl screenshot should produce messages"
    assert juice_msgs[-1].direction == "incoming", juice_msgs[-1]
    assert "新能源车保养主要检查哪些项目" in juice_msgs[-1].text, juice_msgs[-1]
    print("juice last:", juice_msgs[-1].direction, juice_msgs[-1].text)

    group_text = "\n".join(box.text for box in _rapid_boxes(group))
    assert "群成员" in group_text or "群公告" in group_text, group_text[:300]
    print("group guard text found")

    list_entries = parse_boxes_to_entries(list_shot, _rapid_boxes(list_shot))
    assert list_entries, "list screenshot should produce entries"
    assert any(e.name.startswith("JuiceAl") and "新能源车保养" in e.preview for e in list_entries), list_entries[:5]
    assert not any(e.name.startswith("新能源车保养") and "刘廷曦" in e.preview for e in list_entries), list_entries
    print("list entries:")
    for entry in list_entries[:8]:
        print(f"  {entry.name} :: {entry.preview}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
