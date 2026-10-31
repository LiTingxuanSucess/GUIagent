"""从企业微信 PC 通讯录 →「外部联系人」列表滚动 OCR，导出全量（可见范围内）昵称与副标题。"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from src.ocr_runtime import init_ocr_runtime

init_ocr_runtime()

from src.add_customer import (
    AddCustomerOcrOptions,
    AddCustomerRatios,
    _click_ocr_or_ratio,
    _has_page_title,
    _is_already_in_contacts,
    _pick_boxes,
    _pick_contacts_nav,
    _refresh_main_hwnd,
    _snap_ocr,
    _wait_state,
    _wait_wework_foreground,
)
from src.actions import click_client_ratio, click_screen_point_foreground, scroll_client_ratio
from src.capture import (
    RegionRatios,
    capture_window_region_hash,
    capture_window_region_png,
    window_client_rect_screen,
)
from src.ocr_client import ocr_image_text_boxes, warmup_ocr_service
from src.window import activate_hwnd, ensure_wework_hwnd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


# 主内容区联系人列表（相对 WeWorkWindow 客户区）；可按分辨率微调参数
LIST_REGION = RegionRatios(left=0.26, top=0.095, width=0.704, height=0.805)

_SIDEBAR_REG = (0.05, 0.085, 0.46, 0.62)

# 侧栏「外部联系人」比例回退（与测试夹具 OCR 近似位置一致）
_EXTERNAL_RX = 0.22
_EXTERNAL_RY = 0.30

_NOISE_TITLE = frozenset(
    (
        "外部联系人",
        "组织架构",
        "我的客户",
        "新的客户",
        "其他联系人",
        "智能机器人",
        "添加",
        "管理",
        "搜索",
        "微信",
        "企业微信",
        "消息",
        "邮件",
        "文档",
        "日程",
        "待办",
        "工作台",
        "通讯录",
    )
)


def _norm_line(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip()


def _entry_key(title: str, subtitle: str) -> str:
    t = _norm_line(title)
    s = _norm_line(subtitle)
    key = _norm_line(f"{t} {s}")
    key = re.sub(r"@微信|外部|\.{3,}|…+", "", key)
    key = re.sub(r"[^\w\u4e00-\u9fff]+", "", key)
    return key[:96]


def _is_row_noise(title: str, subtitle: str) -> bool:
    t = _norm_line(title)
    if not t or len(t) <= 1:
        return True
    if t in _NOISE_TITLE:
        return True
    if re.fullmatch(r"[A-Za-z]", t):
        return True
    if re.fullmatch(r"\d{1,2}", t):
        return True
    if title.startswith("visible_row_"):
        return True
    return False


def _pick_external_contacts_sidebar(boxes: list, iw: int, ih: int):
    cand = _pick_boxes(
        boxes,
        iw,
        ih,
        ("外部联系人",),
        _SIDEBAR_REG,
        max_text_len=12,
    )
    if not cand:
        cand = _pick_boxes(
            boxes,
            iw,
            ih,
            ("外部联系人",),
            (0.03, 0.06, 0.52, 0.75),
            max_text_len=12,
        )
    if not cand:
        return None
    return sorted(cand, key=lambda b: (b.cy, b.cx))[0]


def _is_external_contacts_visible(boxes: list, iw: int, ih: int, o: AddCustomerOcrOptions) -> bool:
    if _has_page_title(boxes, iw, ih, "外部联系人", o):
        return True
    mid_sub = bool(
        _pick_boxes(boxes, iw, ih, ("外部联系人",), (0.10, 0.06, 0.48, 0.76), max_text_len=12)
    )
    return mid_sub or bool(
        _pick_boxes(boxes, iw, ih, ("外部联系人",), (0.20, 0.02, 0.82, 0.14), max_text_len=12)
    )


def _is_workbench_main(boxes: list, iw: int, ih: int) -> bool:
    """主界面「工作台」应用中心（易与通讯录混淆；比例点偏上时会误进）。"""
    if _pick_boxes(boxes, iw, ih, ("全部应用",), (0.22, 0.04, 0.88, 0.28), max_text_len=8):
        return True
    if _pick_boxes(boxes, iw, ih, ("效率工具", "选应用"), (0.22, 0.04, 0.90, 0.30), max_text_len=10):
        return True
    return False


def _contacts_middle_ready(boxes: list, iw: int, ih: int) -> bool:
    """通讯录第二栏出现典型入口（左侧导航点「通讯录」之后应可见）。"""
    reg = (0.05, 0.045, 0.52, 0.62)
    if _pick_boxes(boxes, iw, ih, ("新的客户", "新客户"), reg, max_text_len=14):
        return True
    if _pick_boxes(boxes, iw, ih, ("外部联系人",), reg, max_text_len=12):
        return True
    if _pick_boxes(boxes, iw, ih, ("组织架构",), reg, max_text_len=10):
        return True
    if _pick_boxes(boxes, iw, ih, ("我的客户",), reg, max_text_len=12):
        return True
    return False


def _entered_address_book(boxes: list, iw: int, ih: int) -> bool:
    return (
        _is_already_in_contacts(boxes, iw, ih)
        and _contacts_middle_ready(boxes, iw, ih)
        and not _is_workbench_main(boxes, iw, ih)
    )


def _box_to_screen(hwnd: int, bx: object, iw: int, ih: int) -> tuple[int, int]:
    left, top, right, bottom = window_client_rect_screen(hwnd)
    w, h = max(1, right - left), max(1, bottom - top)
    cx = float(getattr(bx, "cx", 0))
    cy = float(getattr(bx, "cy", 0))
    return (
        int(left + (cx / max(iw, 1)) * w),
        int(top + (cy / max(ih, 1)) * h),
    )


def _click_message_nav(hwnd: int, o: AddCustomerOcrOptions) -> None:
    boxes, iw, ih, *_ = _snap_ocr(hwnd)
    m = _pick_boxes(boxes, iw, ih, ("消息",), (0.0, 0.02, 0.16, 0.42), max_text_len=8)
    if m:
        b = min(m, key=lambda x: (x.cy, x.cx))
        sx, sy = _box_to_screen(hwnd, b, iw, ih)
        print(f">>> 点击导航「消息」（OCR «{b.text}»）→ screen=({sx},{sy})", flush=True)
        click_screen_point_foreground(hwnd, sx, sy, pause_after=0.55)
    else:
        print(">>> 「消息」OCR 未命中，按比例 (0.03, 0.10)", flush=True)
        click_client_ratio(hwnd, 0.03, 0.10, pause_after=0.55)


def _click_contacts_nav_smart(hwnd: int, o: AddCustomerOcrOptions, r: AddCustomerRatios) -> None:
    boxes, iw, ih, *_ = _snap_ocr(hwnd)
    direct = _pick_boxes(boxes, iw, ih, o.kw_contacts, o.region_contacts, max_text_len=12)
    if direct:
        b = min(direct, key=lambda x: (x.cy, x.cx))
        sx, sy = _box_to_screen(hwnd, b, iw, ih)
        print(f">>> 点击导航「通讯录」（OCR «{b.text}»）→ screen=({sx},{sy})", flush=True)
        click_screen_point_foreground(hwnd, sx, sy, pause_after=0.62)
        return
    inferred = _pick_contacts_nav(boxes, iw, ih, o)
    if inferred is None:
        print(f">>> 「通讯录」全失，按比例 ({r.contacts_rail_rx:.3f},{r.contacts_rail_ry:.3f})", flush=True)
        click_client_ratio(hwnd, r.contacts_rail_rx, r.contacts_rail_ry, pause_after=0.62)
        return
    sx, sy = _box_to_screen(hwnd, inferred, iw, ih)
    print(f">>> 点击导航「通讯录」（推断/inferred «{getattr(inferred,'text','')}»）→ screen=({sx},{sy})", flush=True)
    click_screen_point_foreground(hwnd, sx, sy, pause_after=0.62)


def _leave_workbench_to_messages(hwnd: int, o: AddCustomerOcrOptions) -> None:
    boxes, iw, ih, *_ = _snap_ocr(hwnd)
    if not _is_workbench_main(boxes, iw, ih):
        return
    print(">>> 当前在主界面工作台，先回「消息」", flush=True)
    _click_message_nav(hwnd, o)
    time.sleep(0.55)


def navigate_to_external_contacts(
    hwnd: int,
    o: AddCustomerOcrOptions,
    r: AddCustomerRatios,
    *,
    dry_run: bool,
    diag_dir: Path | None,
) -> None:
    hwnd = _refresh_main_hwnd()
    _wait_wework_foreground(hwnd, 120.0)
    if dry_run:
        print(">>> dry-run：跳过通讯录导航", flush=True)
        return

    _leave_workbench_to_messages(hwnd, o)

    boxes, iw, ih, cl, ct = _snap_ocr(hwnd)
    if _entered_address_book(boxes, iw, ih):
        print(">>> 已在通讯录页（含子菜单入口）", flush=True)
    else:
        _click_contacts_nav_smart(hwnd, o, r)
        try:
            _wait_state(
                hwnd,
                lambda b, bw, bh: _entered_address_book(b, bw, bh),
                name="进入通讯录页",
                timeout_sec=12.0,
                diag_dir=diag_dir,
            )
        except RuntimeError:
            print(">>> 通讯录未就绪，回「消息」后再次进入通讯录", flush=True)
            _click_message_nav(hwnd, o)
            time.sleep(0.5)
            _click_contacts_nav_smart(hwnd, o, r)
            _wait_state(
                hwnd,
                lambda b, bw, bh: _entered_address_book(b, bw, bh),
                name="进入通讯录页(第二轮)",
                timeout_sec=14.0,
                diag_dir=diag_dir,
            )

    time.sleep(0.35)
    hwnd = _refresh_main_hwnd()
    boxes, iw, ih, cl, ct = _snap_ocr(hwnd)
    if _is_workbench_main(boxes, iw, ih) or not _entered_address_book(boxes, iw, ih):
        print(">>> 外部联系人前先修正：离开工作台并进通讯录", flush=True)
        _leave_workbench_to_messages(hwnd, o)
        _click_contacts_nav_smart(hwnd, o, r)
        _wait_state(
            hwnd,
            lambda b, bw, bh: _entered_address_book(b, bw, bh),
            name="通讯录页复核",
            timeout_sec=14.0,
            diag_dir=diag_dir,
        )
        hwnd = _refresh_main_hwnd()
        boxes, iw, ih, cl, ct = _snap_ocr(hwnd)

    if _is_external_contacts_visible(boxes, iw, ih, o):
        print(">>> 已在「外部联系人」页", flush=True)
        return

    bx = _pick_external_contacts_sidebar(boxes, iw, ih)
    _click_ocr_or_ratio(
        hwnd,
        "外部联系人",
        bx,
        iw,
        ih,
        cl,
        ct,
        _EXTERNAL_RX,
        _EXTERNAL_RY,
        0.55,
        dry_run,
        o.use_ocr,
        True,
    )
    try:
        _wait_state(
            hwnd,
            lambda b, bw, bh: _is_external_contacts_visible(b, bw, bh, o),
            name="进入外部联系人列表",
            timeout_sec=10.0,
            diag_dir=diag_dir,
        )
    except RuntimeError:
        print(">>> 外部联系人 OCR 点击未确认，按比例重试", flush=True)
        last_ex: RuntimeError | None = None
        ok2 = False
        for rx, ry in ((_EXTERNAL_RX, _EXTERNAL_RY), (0.18, 0.28), (0.23, 0.34), (0.26, 0.40)):
            hwnd = _refresh_main_hwnd()
            click_client_ratio(hwnd, rx, ry, pause_after=0.65)
            time.sleep(0.55)
            try:
                _wait_state(
                    hwnd,
                    lambda b, bw, bh: _is_external_contacts_visible(b, bw, bh, o),
                    name=f"进入外部联系人列表({rx:.2f},{ry:.2f})",
                    timeout_sec=10.0,
                    diag_dir=diag_dir,
                )
                ok2 = True
                break
            except RuntimeError as exc:
                last_ex = exc
        if not ok2 and last_ex is not None:
            raise last_ex


def _list_center_ratios(reg: RegionRatios) -> tuple[float, float]:
    return reg.left + reg.width * 0.52, reg.top + reg.height * 0.48


def _scroll_list_to_top(
    hwnd: int,
    reg: RegionRatios,
    *,
    max_rounds: int,
    wheel_clicks_per_round: int,
) -> None:
    sx, sy = _list_center_ratios(reg)
    for i in range(max_rounds):
        h0 = capture_window_region_hash(hwnd, reg)
        scroll_client_ratio(hwnd, sx, sy, clicks=wheel_clicks_per_round, pause_after=0.28)
        time.sleep(0.12)
        h1 = capture_window_region_hash(hwnd, reg)
        if h0 == h1:
            print(f">>> 列表已到顶 ({i + 1} 轮滚动后 hash 不变)", flush=True)
            return
    print(">>> scroll-to-top：达到上限，可能未完全到顶", flush=True)


def _ocr_list_rows(hwnd: int, reg: RegionRatios, dbg_dir: Path) -> list[tuple[str, str, float]]:
    dbg_dir.mkdir(parents=True, exist_ok=True)
    shot = dbg_dir / "list.png"
    capture_window_region_png(hwnd, reg, shot)
    boxes = ocr_image_text_boxes(shot)

    left, top, right, bottom = window_client_rect_screen(hwnd)
    rh = max(1, int((bottom - top) * reg.height))
    rows: list[list[str]] = []
    centers: list[float] = []
    for box in sorted(boxes, key=lambda b: b.cy):
        text = _norm_line(box.text)
        if not text:
            continue
        if not rows or abs(box.cy - centers[-1]) > 36:
            rows.append([text])
            centers.append(float(box.cy))
        else:
            rows[-1].append(text)
            centers[-1] = (centers[-1] + float(box.cy)) / 2.0

    out: list[tuple[str, str, float]] = []
    for texts, cy in zip(rows, centers):
        y_ratio = reg.top + (cy / float(rh)) * reg.height
        if y_ratio < reg.top + 0.01 or y_ratio > reg.top + reg.height - 0.02:
            continue
        title = texts[0]
        subtitle = " ".join(texts[1:4]) if len(texts) > 1 else ""
        out.append((title, subtitle, y_ratio))
    return out


def scrape_external_contacts(
    hwnd: int,
    *,
    out_dir: Path,
    diag_dir: Path | None,
    max_pages: int,
    stable_limit: int,
    scroll_top: bool,
    top_rounds: int,
    down_clicks: int,
    dry_run: bool,
    ocr_opts: AddCustomerOcrOptions,
    ratios: AddCustomerRatios,
) -> dict[str, dict]:
    if dry_run:
        navigate_to_external_contacts(hwnd, ocr_opts, ratios, dry_run=True, diag_dir=diag_dir)
        return {}

    navigate_to_external_contacts(hwnd, ocr_opts, ratios, dry_run=False, diag_dir=diag_dir)

    reg = LIST_REGION
    sx, sy = _list_center_ratios(reg)

    out_dir.mkdir(parents=True, exist_ok=True)
    merged: dict[str, dict] = {}

    if scroll_top:
        _scroll_list_to_top(hwnd, reg, max_rounds=top_rounds, wheel_clicks_per_round=10)

    stable = 0
    for page_i in range(max_pages):
        page_dir = out_dir / "pages" / f"{page_i:04d}"
        rows = _ocr_list_rows(hwnd, reg, page_dir)

        fresh = 0
        for title, subtitle, _y in rows:
            if _is_row_noise(title, subtitle):
                continue
            k = _entry_key(title, subtitle)
            if not k:
                continue
            if k not in merged:
                fresh += 1
            prev = merged.get(k)
            if prev is None or len(subtitle) > len(prev.get("subtitle") or ""):
                merged[k] = {
                    "name": title,
                    "subtitle": subtitle,
                    "key": k,
                }

        dh = capture_window_region_hash(hwnd, reg)
        print(
            f">>> page={page_i + 1} rows~{len(rows)} new_keys={fresh} total={len(merged)} "
            f"hash={dh[:12]}…",
            flush=True,
        )

        scroll_client_ratio(hwnd, sx, sy, clicks=-abs(down_clicks), pause_after=0.35)
        time.sleep(0.08)
        nh = capture_window_region_hash(hwnd, reg)
        if nh == dh:
            stable += 1
            print(f">>> viewport stable {stable}/{stable_limit}", flush=True)
            if stable >= stable_limit:
                break
        else:
            stable = 0

    return merged


def _write_exports(out_dir: Path, merged: dict[str, dict]) -> None:
    exported_at = datetime.now().isoformat(timespec="seconds")
    contacts = sorted(merged.values(), key=lambda x: (x.get("name") or ""))
    payload = {"exported_at": exported_at, "source": "wework_external_contacts", "count": len(contacts), "contacts": contacts}

    js = json.dumps(payload, ensure_ascii=False, indent=2)
    (out_dir / "external_contacts.json").write_text(js, encoding="utf-8-sig")

    lines = [f"# 企业微信外部联系人\n\n导出时间：`{exported_at}` 共 **{len(contacts)}** 条\n"]
    for c in contacts:
        sub = (c.get("subtitle") or "").strip()
        if sub:
            lines.append(f"\n- **{c.get('name')}** — {sub}\n")
        else:
            lines.append(f"\n- **{c.get('name')}**\n")

    (out_dir / "external_contacts.md").write_text("".join(lines), encoding="utf-8-sig")
    print(f">>> wrote {out_dir / 'external_contacts.json'}", flush=True)
    print(f">>> wrote {out_dir / 'external_contacts.md'}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Export WeWork 「外部联系人」 list via OCR + scroll.")
    ap.add_argument("--out-dir", type=Path, default=Path("exports") / "wxwork_external_contacts")
    ap.add_argument("--diag-dir", type=Path, default=None, help="Save OCR misses / wait failures")
    ap.add_argument("--max-pages", type=int, default=400, help="Max scroll iterations (safety bound).")
    ap.add_argument("--stable", type=int, default=3, help="Stop after N successive unchanged viewports.")
    ap.add_argument("--skip-scroll-top", action="store_true", help="Do not rewind list to top first.")
    ap.add_argument("--top-rounds", type=int, default=50, help="Max wheel rounds when scrolling to top.")
    ap.add_argument("--down-clicks", type=int, default=6, help="Wheel down magnitude per page (negative internal).")
    ap.add_argument("--dry-run", action="store_true", help="Navigate only (no scraping).")
    ap.add_argument("--no-foreground", action="store_true", help="Do not call activate_hwnd before start.")
    args = ap.parse_args()

    warmup_ocr_service()

    hwnd = ensure_wework_hwnd()
    if not hwnd:
        print(">>> 未找到企业微信 WeWorkWindow", flush=True)
        return 2
    if not args.no_foreground:
        activate_hwnd(hwnd)
        time.sleep(0.55)

    o = AddCustomerOcrOptions()
    r = AddCustomerRatios()
    diag = args.diag_dir
    merged = scrape_external_contacts(
        hwnd,
        out_dir=args.out_dir.resolve(),
        diag_dir=diag,
        max_pages=args.max_pages,
        stable_limit=max(1, args.stable),
        scroll_top=not args.skip_scroll_top,
        top_rounds=max(5, args.top_rounds),
        down_clicks=max(2, args.down_clicks),
        dry_run=args.dry_run,
        ocr_opts=o,
        ratios=r,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if merged:
        _write_exports(args.out_dir.resolve(), merged)
    elif not args.dry_run:
        print(">>> 无数据写出（可能没有识别到联系人行或全部为噪声）", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
