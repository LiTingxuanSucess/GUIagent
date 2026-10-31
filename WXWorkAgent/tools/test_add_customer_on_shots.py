"""
对 shots/add_customer/step*.png 跑 PaddleOCR，并打印 add_customer 各步会选中的框。

用法（在 WXWorkAgent 目录）:
  py -3.11 tools/test_add_customer_on_shots.py
  py -3.11 tools/test_add_customer_on_shots.py --mock   # 不跑 Paddle，用内置假框验证筛选逻辑

若本机 Paddle 报 RuntimeError，可试:
  set FLAGS_use_mkldnn=0
  set CUDA_VISIBLE_DEVICES=
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ocr_runtime import init_ocr_runtime  # noqa: E402

init_ocr_runtime()

from PIL import Image

from src.add_customer import (  # noqa: E402
    AddCustomerOcrOptions,
    _infer_contacts_from_anchors,
    _is_already_in_contacts,
    _pick_contacts_nav,
    _pick_invite_send,
    _pick_boxes,
    _pick_result_add,
    _pick_toolbar_add,
)
from src.ocr_run import OcrTextBox  # noqa: E402


def _mock_boxes_step01(iw: int, ih: int) -> list[OcrTextBox]:
    """与 step01 布局大致一致：左栏「通讯录」、中间「新的客户」，用于无 Paddle 时自测 pick。"""
    return [
        OcrTextBox("消息", iw * 0.03, ih * 0.08),
        OcrTextBox("通讯录", iw * 0.03, ih * 0.72),
        OcrTextBox("高级功能", iw * 0.03, ih * 0.90),
        OcrTextBox("新的客户", iw * 0.22, ih * 0.18),
        OcrTextBox("我的客户", iw * 0.22, ih * 0.24),
        OcrTextBox("灵耘致新", iw * 0.22, ih * 0.42),
    ]


def _mock_boxes_messages_page(iw: int, ih: int) -> list[OcrTextBox]:
    """模拟「消息」页面（通讯录未选中，文字灰色低对比度被 OCR 漏掉的情况）。
    仅保留 OCR 能识别的高对比度文字，通讯录文字故意不包含。
    """
    return [
        OcrTextBox("搜索", iw * 0.12, ih * 0.05),
        OcrTextBox("RevAI", iw * 0.03, ih * 0.06),
        OcrTextBox("消息", iw * 0.03, ih * 0.13),   # 已选中，高对比度
        OcrTextBox("邮件", iw * 0.03, ih * 0.20),
        OcrTextBox("文档", iw * 0.03, ih * 0.27),
        OcrTextBox("工作台", iw * 0.03, ih * 0.67),
        # 通讯录 (iw*0.03, ih*0.74) 故意缺失 — 模拟低对比度灰色文字未识别
        OcrTextBox("微盘", iw * 0.03, ih * 0.81),
        OcrTextBox("企业微信团队", iw * 0.18, ih * 0.15),
        OcrTextBox("登录操作通知", iw * 0.18, ih * 0.22),
    ]


def _mock_boxes_step02(iw: int, ih: int) -> list[OcrTextBox]:
    return [
        OcrTextBox("新的客户", iw * 0.22, ih * 0.12),
        OcrTextBox("+ 添加", iw * 0.82, ih * 0.11),
        OcrTextBox("添加客户", iw * 0.55, ih * 0.08),
        OcrTextBox("手机号/邮箱", iw * 0.48, ih * 0.28),
    ]


def _mock_boxes_step03(iw: int, ih: int) -> list[OcrTextBox]:
    return [
        OcrTextBox("13800138000", iw * 0.45, ih * 0.22),
        OcrTextBox("南岛 South Island", iw * 0.38, ih * 0.38),
        OcrTextBox("添加", iw * 0.78, ih * 0.38),
        OcrTextBox("+ 添加", iw * 0.85, ih * 0.10),
    ]


def _mock_boxes_step04(iw: int, ih: int) -> list[OcrTextBox]:
    return [
        OcrTextBox("发送添加邀请", iw * 0.52, ih * 0.12),
        OcrTextBox("发送添加邀请", iw * 0.73, ih * 0.40),
        OcrTextBox("我是灵耘致新的ltx", iw * 0.50, ih * 0.35),
        OcrTextBox("发送", iw * 0.52, ih * 0.62),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="使用内置假 OCR 框，不调用 Paddle")
    args = ap.parse_args()

    folder = ROOT / "shots" / "add_customer"
    paths = sorted(folder.glob("step*.png"))
    if not paths and not args.mock:
        print(f"未找到 {folder / 'step*.png'}，请把截图放进该目录。", flush=True)
        return 1

    o = AddCustomerOcrOptions()
    try:
        import yaml

        cfg_path = ROOT / "config.yaml"
        if cfg_path.exists():
            with cfg_path.open(encoding="utf-8") as f:
                ac = (yaml.safe_load(f) or {}).get("add_customer") or {}
            o = AddCustomerOcrOptions.from_dict(ac)
            print("已加载 config.yaml add_customer OCR 选项\n", flush=True)
    except Exception as e:
        print(f"(跳过 config: {e})\n", flush=True)

    if args.mock:
        mocks = {
            "step01.png": _mock_boxes_step01,
            "step02.png": _mock_boxes_step02,
            "step03.png": _mock_boxes_step03,
            "step04.png": _mock_boxes_step04,
        }
        print("=== --mock：内置框验证 pick 逻辑（非真实 OCR）===\n", flush=True)
        for name, fn in mocks.items():
            iw, ih = 1572, 1233
            if paths:
                for p in paths:
                    if p.name == name:
                        im = Image.open(p)
                        iw, ih = im.size
                        break
            print("=" * 72, flush=True)
            print(name, f"({iw}x{ih})", flush=True)
            boxes = fn(iw, ih)
            _print_picks(o, boxes, iw, ih)

        # 额外：验证通讯录推断路径（消息页，通讯录文字被 OCR 漏掉）
        print("=" * 72, flush=True)
        print("messages_page（通讯录未选中，文字低对比度被漏掉）", flush=True)
        iw, ih = 1572, 1233
        boxes_msg = _mock_boxes_messages_page(iw, ih)
        b_nav = _pick_contacts_nav(boxes_msg, iw, ih, o)
        already = _is_already_in_contacts(boxes_msg, iw, ih)
        inferred = _infer_contacts_from_anchors(boxes_msg, iw, ih, o.nav_rail_nx_max)
        print(f"  _is_already_in_contacts: {already}  (期望 False)", flush=True)
        print(
            f"  _infer_contacts_from_anchors: cy={inferred.cy:.0f} "
            f"(期望 ~{ih*0.74:.0f}, 误差<{ih*0.07:.0f}px)"
            if inferred else "  _infer_contacts_from_anchors: None  [FAIL]",
            flush=True,
        )
        print(f"  _pick_contacts_nav: {'命中 ' + b_nav.text if b_nav else 'None [FAIL]'}", flush=True)
        if inferred:
            expected_cy = ih * 0.74
            err = abs(inferred.cy - expected_cy)
            ok = err < ih * 0.07
            print(f"  推断误差: {err:.1f}px  {'[OK]' if ok else '[FAIL 误差过大]'}", flush=True)

        print("=" * 72, flush=True)
        return 0

    from src.ocr_run import ocr_image_text_boxes  # noqa: E402

    for path in paths:
        print("=" * 72, flush=True)
        print(path.name, flush=True)
        im = Image.open(path)
        iw, ih = im.size
        print(f"  image size: {iw}x{ih}", flush=True)

        try:
            boxes = ocr_image_text_boxes(path)
        except RuntimeError as e:
            print(f"  Paddle OCR 失败: {e}", flush=True)
            print("  请在本机终端重试，或使用: py -3.11 tools/test_add_customer_on_shots.py --mock", flush=True)
            continue
        print(f"  OCR boxes: {len(boxes)}", flush=True)
        for b in boxes[:40]:
            nx, ny = b.cx / iw, b.cy / ih
            print(f"    «{b.text}»  center=({b.cx:.0f},{b.cy:.0f})  norm=({nx:.3f},{ny:.3f})", flush=True)
        if len(boxes) > 40:
            print(f"    ... +{len(boxes) - 40} more", flush=True)

        _print_picks(o, boxes, iw, ih)

    print("=" * 72, flush=True)
    return 0


def _print_picks(o: AddCustomerOcrOptions, boxes: list[OcrTextBox], iw: int, ih: int) -> None:
    bc = _pick_contacts_nav(boxes, iw, ih, o)
    print(f"  pick 通讯录: {bc.text if bc else '—'}", flush=True)

    bn = _pick_boxes(boxes, iw, ih, o.kw_new_customers, o.region_new_customers)
    print(f"  pick 新的客户: {bn[0].text if bn else '—'}", flush=True)

    bt = _pick_toolbar_add(boxes, iw, ih, o)
    print(f"  pick +添加(工具栏): {bt.text if bt else '—'}", flush=True)

    bs = _pick_boxes(boxes, iw, ih, o.kw_dialog_search, o.region_dialog_search, max_text_len=24)
    print(f"  pick 搜索框(手机/邮箱): {bs[0].text if bs else '—'}", flush=True)

    br = _pick_result_add(boxes, iw, ih, o)
    print(f"  pick 结果行添加: {br.text if br else '—'}", flush=True)

    bf = _pick_invite_send(boxes, iw, ih, o)
    print(f"  pick 发送: {bf.text if bf else '—'}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
