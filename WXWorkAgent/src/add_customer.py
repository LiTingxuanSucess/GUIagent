"""
Automate 企业微信 PC「添加客户」流程（外部联系人 / 手机号搜索）。

定位方式（默认）：
  全窗口客户区截图 → PaddleOCR 文本框 → 按关键字 + 归一化区域过滤 →
  点击识别框中心（映射到屏幕坐标）。避免写死比例。

通讯录导航入口策略（多级）：
  1. 直接在左侧导航栏找到"通讯录"文字 — 选中态蓝色文字，最可靠。
  2. 从同列其它导航锚点（消息/工作台/邮件等）推算"通讯录"图标所在行 — 覆盖
     未选中时灰色低对比度文字 OCR 失败的情况。
  3. 预检查：若当前已在通讯录页（侧栏出现 新的客户/我的客户/组织架构），
     直接跳过导航步骤。

若 OCR 未命中（如无文字的纯图标），自动回退到 config 中的比例坐标。
"""
from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import win32api
import win32con
import win32gui

from src.actions import (
    _find_cef_hwnd_at,
    click_client_ratio,
    click_screen_point_foreground,
    press_enter_at_screen,
    press_enter_foreground,
    sendinput_enter,
    sendinput_text,
    type_text_at_screen,
    type_text_foreground,
    type_text_client_ratio,
)
from src.capture import RegionRatios, capture_window_region_temp_png_meta, window_client_rect_screen
from src.computer_use import save_diagnostics
from src.platform_reporter import PlatformReporter
from src.ocr_client import ocr_image_text_boxes
from src.ocr_run import OcrTextBox
from src.window import find_wework_hwnd, is_same_process_foreground

# 全客户区截图（与 OCR 坐标系一致）
_FULL = RegionRatios(left=0.0, top=0.0, width=1.0, height=1.0)
_GA_ROOT = getattr(win32gui, "GA_ROOT", 2)

# 企业微信左侧导航栏固定顺序（从上到下），用于锚点推断"通讯录"图标位置
_NAV_ORDER = ["消息", "邮件", "文档", "日程", "待办", "会议", "智能文档", "智能总结", "工作台", "通讯录", "微盘", "高级功能", "分组"]

# 通讯录页面特征词：出现这些词说明已在通讯录页，不必再点导航
_CONTACTS_PAGE_ANCHORS = frozenset(["新的客户", "我的客户", "其他联系人", "组织架构", "外部联系人", "智能机器人"])


def _refresh_main_hwnd() -> int:
    """每步重新解析主窗口，避免句柄失效或多实例导致坐标错乱。"""
    fg = _foreground_wework_hwnd()
    if fg:
        return fg
    h = find_wework_hwnd()
    if not h:
        raise RuntimeError("企业微信主窗口 (WeWorkWindow) 丢失，请保持客户端打开")
    return h


def _foreground_wework_hwnd() -> int:
    fg = win32gui.GetForegroundWindow()
    try:
        root = win32gui.GetAncestor(fg, _GA_ROOT)
    except Exception:
        root = fg
    try:
        if root and win32gui.GetClassName(root) == "WeWorkWindow":
            return root
    except Exception:
        pass
    hwnd = find_wework_hwnd()
    if hwnd and is_same_process_foreground(hwnd):
        return hwnd
    return 0


def _require_wework_foreground(hwnd: int, step: str) -> None:
    """Stop instead of bringing WeWork forward; avoids triggering security checks."""
    fg = win32gui.GetForegroundWindow()
    try:
        fg_root = win32gui.GetAncestor(fg, _GA_ROOT)
    except Exception:
        fg_root = fg
    if fg_root != hwnd and not is_same_process_foreground(hwnd):
        try:
            cls = win32gui.GetClassName(fg)
            title = win32gui.GetWindowText(fg)
        except Exception:
            cls = title = "unknown"
        raise RuntimeError(
            f"[{step}] 企业微信不是当前前台窗口，已停止。"
            f"请手动切到企业微信后重试；当前前台 hwnd={fg} class={cls!r} title={title!r}"
        )


def _wait_wework_foreground(hwnd: int, timeout_sec: float) -> None:
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        try:
            _require_wework_foreground(hwnd, "开始")
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def _client_rect_valid(hwnd: int) -> bool:
    """检查窗口 client rect 坐标是否合理（排除动画/DPI 偏移导致的负值）。"""
    try:
        left, top, right, bottom = window_client_rect_screen(hwnd)
        return left >= -10 and top >= -10 and right > left and bottom > top
    except Exception:
        return False


def _wait_client_rect_stable(hwnd: int, timeout_sec: float = 2.0) -> None:
    """等待窗口 client rect 稳定（排除动画帧/DPI 偏移）。"""
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while not _client_rect_valid(hwnd):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.15)


@dataclass
class AddCustomerOcrOptions:
    """OCR 定位：关键字 + 全窗归一化区域 (nx0, ny0, nx1, ny1) 缩小误匹配。"""

    use_ocr: bool = True

    region_contacts: tuple[float, float, float, float] = (0.0, 0.0, 0.16, 1.0)
    region_new_customers: tuple[float, float, float, float] = (0.05, 0.03, 0.45, 0.48)
    region_page_title: tuple[float, float, float, float] = (0.25, 0.02, 0.65, 0.12)
    region_toolbar_add: tuple[float, float, float, float] = (0.82, 0.02, 0.99, 0.12)
    region_dialog_search: tuple[float, float, float, float] = (0.14, 0.30, 0.88, 0.65)
    region_result_add: tuple[float, float, float, float] = (0.34, 0.30, 0.99, 0.72)
    region_send: tuple[float, float, float, float] = (0.15, 0.35, 0.90, 0.96)
    region_invite_clear: tuple[float, float, float, float] = (0.15, 0.35, 0.90, 0.96)
    region_invite_input: tuple[float, float, float, float] = (0.15, 0.35, 0.90, 0.80)

    kw_contacts: tuple[str, ...] = ("通讯录",)
    kw_new_customers: tuple[str, ...] = ("新的客户", "新客户")
    kw_toolbar_add: tuple[str, ...] = ("+ 添加", "+添加")
    kw_dialog_search: tuple[str, ...] = ("手机", "邮箱", "手机号")
    kw_result_add: tuple[str, ...] = ("添加",)
    kw_send: tuple[str, ...] = ("发送",)
    kw_invite_clear: tuple[str, ...] = ("清空",)

    toolbar_add_max_len: int = 8
    result_add_max_len: int = 6

    # 导航栏 X 轴范围（归一化），用于锚点推断
    nav_rail_nx_max: float = 0.07

    @classmethod
    def from_dict(cls, d: dict) -> "AddCustomerOcrOptions":
        o = cls()
        if not d:
            return o
        if "use_ocr" in d:
            o.use_ocr = bool(d["use_ocr"])

        def _reg(key: str, cur: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
            v = d.get(key)
            if isinstance(v, (list, tuple)) and len(v) == 4:
                return (float(v[0]), float(v[1]), float(v[2]), float(v[3]))
            return cur

        def _kws(key: str, cur: tuple[str, ...]) -> tuple[str, ...]:
            v = d.get(key)
            if isinstance(v, list) and v:
                return tuple(str(x) for x in v)
            return cur

        o.region_contacts = _reg("ocr_region_contacts", o.region_contacts)
        o.region_new_customers = _reg("ocr_region_new_customers", o.region_new_customers)
        o.region_page_title = _reg("ocr_region_page_title", o.region_page_title)
        o.region_toolbar_add = _reg("ocr_region_toolbar_add", o.region_toolbar_add)
        o.region_dialog_search = _reg("ocr_region_dialog_search", o.region_dialog_search)
        o.region_result_add = _reg("ocr_region_result_add", o.region_result_add)
        o.region_send = _reg("ocr_region_send", o.region_send)
        o.region_invite_clear = _reg("ocr_region_invite_clear", o.region_invite_clear)
        o.region_invite_input = _reg("ocr_region_invite_input", o.region_invite_input)

        o.kw_contacts = _kws("ocr_kw_contacts", o.kw_contacts)
        o.kw_new_customers = _kws("ocr_kw_new_customers", o.kw_new_customers)
        o.kw_toolbar_add = _kws("ocr_kw_toolbar_add", o.kw_toolbar_add)
        o.kw_dialog_search = _kws("ocr_kw_dialog_search", o.kw_dialog_search)
        o.kw_result_add = _kws("ocr_kw_result_add", o.kw_result_add)
        o.kw_send = _kws("ocr_kw_send", o.kw_send)
        o.kw_invite_clear = _kws("ocr_kw_invite_clear", o.kw_invite_clear)

        if "ocr_toolbar_add_max_len" in d:
            o.toolbar_add_max_len = int(d["ocr_toolbar_add_max_len"])
        if "ocr_result_add_max_len" in d:
            o.result_add_max_len = int(d["ocr_result_add_max_len"])
        if "ocr_nav_rail_nx_max" in d:
            o.nav_rail_nx_max = float(d["ocr_nav_rail_nx_max"])
        return o


@dataclass
class AddCustomerRatios:
    """比例回退（OCR 未命中时使用）。"""

    contacts_rail_rx: float = 0.028
    contacts_rail_ry: float = 0.775
    new_customers_rx: float = 0.16
    new_customers_ry: float = 0.21
    toolbar_add_rx: float = 0.88
    toolbar_add_ry: float = 0.13
    dialog_search_rx: float = 0.58
    dialog_search_ry: float = 0.36
    result_add_rx: float = 0.74
    result_add_ry: float = 0.44
    invite_send_rx: float = 0.50
    invite_send_ry: float = 0.58
    invite_clear_rx: float = 0.72
    invite_clear_ry: float = 0.54
    invite_input_rx: float = 0.50
    invite_input_ry: float = 0.49

    pause_after_contacts: float = 0.45
    pause_after_new_customers: float = 0.40
    pause_after_toolbar_add: float = 1.2   # 等弹窗动画完成
    pause_after_dialog: float = 0.5
    search_wait_sec: float = 2.0
    pause_after_result_add: float = 0.70
    pause_before_invite_send: float = 0.55
    wait_dialog_timeout_sec: float = 8.0   # 弹窗有时出现较慢
    wait_result_timeout_sec: float = 6.0
    wait_invite_timeout_sec: float = 5.0

    @classmethod
    def from_dict(cls, d: dict) -> "AddCustomerRatios":
        if not d:
            return cls()
        base = cls()
        for k, v in d.items():
            if hasattr(base, k):
                cur = getattr(base, k)
                try:
                    setattr(base, k, type(cur)(v))
                except (TypeError, ValueError):
                    setattr(base, k, v)
        return base


def _in_norm_region(nx: float, ny: float, reg: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = reg
    return x0 <= nx <= x1 and y0 <= ny <= y1


def _norm_text(s: str) -> str:
    return "".join(str(s).split())


def _contains_any(s: str, keywords: tuple[str, ...]) -> bool:
    compact = _norm_text(s)
    return any(_norm_text(k) in compact for k in keywords)


def _pick_boxes(
    boxes: list[OcrTextBox],
    iw: int,
    ih: int,
    keywords: tuple[str, ...],
    region: tuple[float, float, float, float],
    *,
    max_text_len: int = 99,
) -> list[OcrTextBox]:
    cand: list[OcrTextBox] = []
    for b in boxes:
        if len(b.text) > max_text_len:
            continue
        if not _contains_any(b.text, keywords):
            continue
        nx, ny = b.cx / max(iw, 1), b.cy / max(ih, 1)
        if not _in_norm_region(nx, ny, region):
            continue
        cand.append(b)
    return cand


# ── 通讯录导航定位（多级策略）─────────────────────────────────────────────

def _is_already_in_contacts(boxes: list[OcrTextBox], iw: int, ih: int) -> bool:
    """当前已在通讯录页（侧栏出现通讯录特征词），不需要再点导航。"""
    for b in boxes:
        nx = b.cx / max(iw, 1)
        if nx < 0.30 and _norm_text(b.text) in {_norm_text(a) for a in _CONTACTS_PAGE_ANCHORS}:
            return True
    return False


def _infer_contacts_from_anchors(
    boxes: list[OcrTextBox],
    iw: int,
    ih: int,
    nav_x_max: float = 0.07,
) -> OcrTextBox | None:
    """从导航栏其它可见标签推算「通讯录」图标的 Y 坐标。

    企业微信左侧导航栏的图标+文字按固定顺序垂直排列。即使「通讯录」本身
    是灰色低对比度文字导致 OCR 未识别，只要能识别出其它导航项，就可以
    通过等间距推断出「通讯录」应在的行。
    """
    contacts_idx = _NAV_ORDER.index("通讯录")

    # 收集导航栏区域（x < nav_x_max）内能识别到的导航项
    anchors: list[tuple[int, OcrTextBox]] = []
    for b in boxes:
        nx = b.cx / max(iw, 1)
        if nx > nav_x_max:
            continue
        t = _norm_text(b.text)
        for i, nav_label in enumerate(_NAV_ORDER):
            if t == _norm_text(nav_label) and i != contacts_idx:
                anchors.append((i, b))
                break

    if not anchors:
        return None

    # 用多个锚点估算图标间距（pitch）
    anchors.sort(key=lambda x: x[1].cy)
    if len(anchors) >= 2:
        pitches: list[float] = []
        for k in range(len(anchors) - 1):
            di = anchors[k + 1][0] - anchors[k][0]
            dy = anchors[k + 1][1].cy - anchors[k][1].cy
            if di > 0:
                pitches.append(dy / di)
        pitch = sum(pitches) / len(pitches) if pitches else ih * 0.07
    else:
        # 单锚点：用窗口高度的 7% 作为粗略间距
        pitch = ih * 0.07

    ref_idx, ref_box = anchors[0]
    inferred_cy = ref_box.cy + (contacts_idx - ref_idx) * pitch

    # 合理性校验：推断坐标应在窗口范围内
    if inferred_cy < 0 or inferred_cy > ih:
        return None

    anchor_names = [_NAV_ORDER[i] for i, _ in anchors]
    print(
        f"  [通讯录] 通过锚点 {anchor_names} 推断图标位置 "
        f"cy={inferred_cy:.0f} (pitch={pitch:.1f}px)",
        flush=True,
    )
    return OcrTextBox(text="通讯录[inferred]", cx=ref_box.cx, cy=inferred_cy)


def _pick_contacts_nav(
    boxes: list[OcrTextBox],
    iw: int,
    ih: int,
    o: AddCustomerOcrOptions,
) -> OcrTextBox | None:
    """多级策略定位「通讯录」导航入口。

    策略 1：直接 OCR 文字匹配（已选中蓝色文字，最可靠）
    策略 2：从同列其它导航锚点推断（未选中时灰色文字 OCR 失败的回退）
    """
    # 策略 1：直接匹配
    direct = _pick_boxes(boxes, iw, ih, o.kw_contacts, o.region_contacts, max_text_len=8)
    if direct:
        return min(direct, key=lambda b: (b.cy, b.cx))

    # 策略 2：锚点推断
    inferred = _infer_contacts_from_anchors(boxes, iw, ih, o.nav_rail_nx_max)
    return inferred


# ── 其它 picker ──────────────────────────────────────────────────────────

def _pick_toolbar_add(boxes: list[OcrTextBox], iw: int, ih: int, o: AddCustomerOcrOptions) -> OcrTextBox | None:
    """主内容区右上角「+ 添加」：排除左侧栏、弹窗标题、邀请标题等。

    OCR 经常把 ⊕ 图标读成 ④、@、⊙ 等，所以匹配策略是：
      优先：关键词完全匹配 (kw_toolbar_add)
      次之：文本结尾是「添加」且不在排除列表内 → 正确率高、漏报率低
    """
    reg = o.region_toolbar_add
    # 绝对不能命中的文本（弹窗标题、列表状态标签）
    _EXCLUDE = {"已添加", "添加客户", "发送添加邀请", "添加成员"}
    scored: list[tuple[int, float, float, OcrTextBox]] = []

    for b in boxes:
        raw = b.text.strip()
        if len(raw) > o.toolbar_add_max_len:
            continue
        if "邀请" in raw or "成员" in raw:
            continue
        compact = _norm_text(raw)
        if compact in _EXCLUDE or any(ex in compact for ex in _EXCLUDE):
            continue
        nx, ny = b.cx / max(iw, 1), b.cy / max(ih, 1)
        if not _in_norm_region(nx, ny, reg):
            continue

        pri = 9
        if _contains_any(compact, o.kw_toolbar_add):
            # 精确关键词命中（含 "+" / "+ "）
            pri = 0
        elif compact in {"添加", "＋添加", "+添加", "⊕添加"}:
            # 已知变体
            pri = 1
        elif compact.endswith("添加") and len(compact) <= 4:
            # OCR 乱读的前缀 + 添加，如 ④添加、@添加、⊙添加
            pri = 2
        else:
            continue
        scored.append((pri, b.cy, -b.cx, b))

    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    best = scored[0][3]
    print(f"  [+添加] 命中 OCR «{best.text}» pri={scored[0][0]}", flush=True)
    return best


def _pick_new_customer_nav(boxes: list[OcrTextBox], iw: int, ih: int, o: AddCustomerOcrOptions) -> OcrTextBox | None:
    cand = _pick_boxes(boxes, iw, ih, o.kw_new_customers, o.region_new_customers, max_text_len=12)
    if not cand:
        return None
    return min(cand, key=lambda b: (b.cy, b.cx))


def _has_page_title(boxes: list[OcrTextBox], iw: int, ih: int, text: str, o: AddCustomerOcrOptions) -> bool:
    return bool(_pick_boxes(boxes, iw, ih, (text,), o.region_page_title, max_text_len=16))


def _has_text(boxes: list[OcrTextBox], keywords: tuple[str, ...], *, max_text_len: int = 99) -> bool:
    return any(len(b.text) <= max_text_len and _contains_any(b.text, keywords) for b in boxes)


def _pick_result_add(boxes: list[OcrTextBox], iw: int, ih: int, o: AddCustomerOcrOptions) -> OcrTextBox | None:
    """Search-result row button in the add-customer dialog.

    The background customer list also contains many「已添加」status labels, so
    only accept short, exact add-button text and choose the rightmost candidate.
    """
    scored: list[tuple[int, float, OcrTextBox]] = []
    for b in boxes:
        raw = b.text.strip()
        compact = _norm_text(raw)
        if len(compact) > o.result_add_max_len:
            continue
        if compact in {"已添加", "+添加", "＋添加", "添加客户", "发送添加邀请"}:
            continue
        if compact != "添加" and not _contains_any(raw, o.kw_result_add):
            continue
        nx, ny = b.cx / max(iw, 1), b.cy / max(ih, 1)
        if not _in_norm_region(nx, ny, o.region_result_add):
            continue
        pri = 0 if compact == "添加" else 1
        scored.append((pri, -b.cx, b))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][2]


def _pick_dialog_search(boxes: list[OcrTextBox], iw: int, ih: int, o: AddCustomerOcrOptions) -> OcrTextBox | None:
    cand = _pick_boxes(boxes, iw, ih, o.kw_dialog_search, o.region_dialog_search, max_text_len=24)
    if not cand:
        return None
    return min(cand, key=lambda b: (b.cy, b.cx))


def _pick_invite_clear(boxes: list[OcrTextBox], iw: int, ih: int, o: AddCustomerOcrOptions) -> OcrTextBox | None:
    """「清空」button in the invite dialog."""
    cand = _pick_boxes(boxes, iw, ih, o.kw_invite_clear, o.region_invite_clear, max_text_len=6)
    return cand[0] if cand else None


def _pick_invite_input(boxes: list[OcrTextBox], iw: int, ih: int, o: AddCustomerOcrOptions) -> OcrTextBox | None:
    """Invite message text area — identified by the default placeholder / editable text."""
    # After clearing, the field is empty; find it by existing message content or common placeholder
    kw_input = ("你好", "Hi", "我是", "添加", "邀请")
    cand = _pick_boxes(boxes, iw, ih, kw_input, o.region_invite_input, max_text_len=80)
    return min(cand, key=lambda b: b.cy) if cand else None


def _pick_invite_send(boxes: list[OcrTextBox], iw: int, ih: int, o: AddCustomerOcrOptions) -> OcrTextBox | None:
    """Blue「发送」button in the final invite dialog, not the dialog title."""
    exact: list[OcrTextBox] = []
    fallback: list[OcrTextBox] = []
    for b in boxes:
        raw = b.text.strip()
        compact = _norm_text(raw)
        if len(compact) > 8:
            continue
        nx, ny = b.cx / max(iw, 1), b.cy / max(ih, 1)
        if not _in_norm_region(nx, ny, o.region_send):
            continue
        if compact == "发送":
            exact.append(b)
        elif "发送" in compact and "邀请" not in compact:
            fallback.append(b)
    pool = exact or fallback
    if not pool:
        return None
    return sorted(pool, key=lambda b: b.cy)[-1]


# ── OCR 截图工具 ──────────────────────────────────────────────────────────

def _snap_ocr(hwnd: int) -> tuple[list[OcrTextBox], int, int, int, int]:
    """截取全窗口并运行 OCR。先等待 client rect 稳定。"""
    _wait_wework_foreground(hwnd, 5.0)
    _wait_client_rect_stable(hwnd, timeout_sec=1.5)
    path, iw, ih, _digest = capture_window_region_temp_png_meta(hwnd, _FULL)
    try:
        boxes = ocr_image_text_boxes(path)
    finally:
        path.unlink(missing_ok=True)
    cl, ct, _cr, _cb = window_client_rect_screen(hwnd)
    return boxes, iw, ih, cl, ct


def _snap_ocr_with_png(hwnd: int) -> tuple[list[OcrTextBox], int, int, int, int, Path | None]:
    """截取全窗口并运行 OCR，保留 PNG 文件供诊断（调用方负责删除）。"""
    _wait_wework_foreground(hwnd, 5.0)
    _wait_client_rect_stable(hwnd, timeout_sec=1.5)
    path, iw, ih, _digest = capture_window_region_temp_png_meta(hwnd, _FULL)
    try:
        boxes = ocr_image_text_boxes(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    cl, ct, _cr, _cb = window_client_rect_screen(hwnd)
    return boxes, iw, ih, cl, ct, path


def _snap_pick(
    hwnd: int,
    picker: Callable[[list[OcrTextBox], int, int], OcrTextBox | None],
    *,
    timeout_sec: float,
    interval_sec: float = 0.25,
    diag_dir: Path | None = None,
    step_name: str = "unknown",
) -> tuple[OcrTextBox | None, int, int, int, int]:
    """轮询 OCR 直到 picker 命中或超时；超时时保存失败现场。"""
    deadline = time.monotonic() + max(0.0, timeout_sec)
    last: tuple[OcrTextBox | None, int, int, int, int] = (None, 1, 1, 0, 0)
    last_boxes: list[OcrTextBox] = []
    last_png: Path | None = None
    while True:
        hwnd = _refresh_main_hwnd()
        boxes, iw, ih, cl, ct, png = _snap_ocr_with_png(hwnd)
        if last_png:
            last_png.unlink(missing_ok=True)
        last_png = png
        last_boxes = boxes
        box = picker(boxes, iw, ih)
        last = (box, iw, ih, cl, ct)
        if box is not None or time.monotonic() >= deadline:
            if box is None and diag_dir:
                save_diagnostics(f"snap_pick_{step_name}", boxes, iw, ih, png, diag_dir)
                last_png = None  # 已被 save_diagnostics 复制，不再删
            break
        time.sleep(interval_sec)
    if last_png:
        last_png.unlink(missing_ok=True)
    return last


def _wait_state(
    hwnd: int,
    accept: Callable[[list[OcrTextBox], int, int], bool],
    *,
    name: str,
    timeout_sec: float = 3.0,
    interval_sec: float = 0.25,
    diag_dir: Path | None = None,
) -> tuple[list[OcrTextBox], int, int, int, int]:
    """轮询 OCR 直到 accept 返回 True 或超时；超时时保存失败现场。"""
    deadline = time.monotonic() + max(0.0, timeout_sec)
    last: tuple[list[OcrTextBox], int, int, int, int] = ([], 1, 1, 0, 0)
    while True:
        hwnd = _refresh_main_hwnd()
        boxes, iw, ih, cl, ct, png = _snap_ocr_with_png(hwnd)
        last = (boxes, iw, ih, cl, ct)
        if accept(boxes, iw, ih):
            png.unlink(missing_ok=True)
            return last
        if time.monotonic() >= deadline:
            preview = ", ".join(b.text for b in boxes[:24])
            if diag_dir:
                save_diagnostics(f"wait_state_{name}", boxes, iw, ih, png, diag_dir)
            else:
                png.unlink(missing_ok=True)
            raise RuntimeError(f"[{name}] 状态验证失败；snapshot={iw}x{ih}; preview={preview}")
        png.unlink(missing_ok=True)
        time.sleep(interval_sec)


def _click_ocr_or_ratio(
    hwnd: int,
    name: str,
    box: OcrTextBox | None,
    iw: int,
    ih: int,
    cl: int,
    ct: int,
    rx: float,
    ry: float,
    pause: float,
    dry_run: bool,
    prefer_ocr: bool,
    allow_ratio_fallback: bool,
) -> None:
    _wait_wework_foreground(hwnd, 5.0)
    if prefer_ocr and box is not None:
        left, top, right, bottom = window_client_rect_screen(hwnd)
        w, h = max(1, right - left), max(1, bottom - top)
        sx, sy = int(left + (box.cx / max(iw, 1)) * w), int(top + (box.cy / max(ih, 1)) * h)
        print(f"  [{name}] OCR «{box.text}» → screen=({sx},{sy})", flush=True)
        if dry_run:
            time.sleep(0.05)
            return
        click_screen_point_foreground(hwnd, sx, sy, pause_after=pause)
        return

    if prefer_ocr and not allow_ratio_fallback:
        raise RuntimeError(
            f"[{name}] OCR 未命中；安全模式已停止，避免比例回退误点；"
            f"snapshot={iw}x{ih} client=({cl},{ct})"
        )

    left, top, right, bottom = window_client_rect_screen(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        raise RuntimeError(
            f"[{name}] 客户区无效 left={left} top={top} right={right} bottom={bottom}，"
            "请确认企业微信未最小化到仅托盘且主窗口仍存在"
        )
    sx, sy = int(left + w * rx), int(top + h * ry)
    tag = "比例" if (not prefer_ocr or box is None) else "回退比例"
    print(f"  [{name}] {tag} ({rx:.3f},{ry:.3f}) → screen=({sx},{sy})", flush=True)
    if dry_run:
        time.sleep(0.05)
        return
    click_client_ratio(hwnd, rx, ry, pause_after=pause)


def add_customer_by_phone(
    hwnd: int,
    phone: str,
    ratios: AddCustomerRatios | None = None,
    ocr_opts: AddCustomerOcrOptions | None = None,
    *,
    skip_nav: bool = False,
    no_final_send: bool = False,
    dry_run: bool = False,
    allow_ratio_fallback: bool = False,
    require_foreground: bool = True,
    foreground_wait_sec: float = 12.0,
    diag_dir: Path | None = None,
    invite_message: str | None = None,
    task_id: int | None = None,
    platform_url: str | None = None,
) -> None:
    r = ratios or AddCustomerRatios()
    o = ocr_opts or AddCustomerOcrOptions()
    phone = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if not phone:
        raise ValueError("phone is empty after sanitise")

    rpt: PlatformReporter | None = (
        PlatformReporter(platform_url, task_id) if platform_url else None
    )

    prefer_ocr = o.use_ocr

    hwnd = _foreground_wework_hwnd() if require_foreground else _refresh_main_hwnd()
    if not hwnd:
        hwnd = _refresh_main_hwnd()
    if require_foreground:
        _wait_wework_foreground(hwnd, foreground_wait_sec)
    time.sleep(0.25)

    if not skip_nav:
        hwnd = _refresh_main_hwnd()

        # ── 预检查：是否已在通讯录页？────────────────────────────────────
        if o.use_ocr:
            pre_boxes, pre_iw, pre_ih, _, _ = _snap_ocr(hwnd)
            if _is_already_in_contacts(pre_boxes, pre_iw, pre_ih):
                print("  [通讯录] 已在通讯录页，跳过导航步骤", flush=True)
                skip_nav = True

    if not skip_nav:
        # ── Step 1：点击通讯录导航 ────────────────────────────────────────
        iw = ih = 1
        cl = ct = 0
        b0 = None
        if o.use_ocr:
            boxes, iw, ih, cl, ct, png = _snap_ocr_with_png(hwnd)
            b0 = _pick_contacts_nav(boxes, iw, ih, o)
            if b0 is None:
                preview = ", ".join(b.text for b in boxes[:20])
                print(f"  [通讯录] OCR未命中 boxes={len(boxes)} preview={preview}", flush=True)
                if diag_dir:
                    save_diagnostics("contacts_nav_miss", boxes, iw, ih, png, diag_dir)
                png.unlink(missing_ok=True)
            else:
                png.unlink(missing_ok=True)
        _click_ocr_or_ratio(
            hwnd, "通讯录", b0, iw, ih, cl, ct,
            r.contacts_rail_rx, r.contacts_rail_ry, r.pause_after_contacts, dry_run, prefer_ocr,
            allow_ratio_fallback,
        )

        # ── Step 1 验证：等待通讯录侧栏出现（轮询 5s）────────────────────
        if not dry_run and o.use_ocr:
            _wait_state(
                hwnd,
                lambda boxes, bw, bh: _is_already_in_contacts(boxes, bw, bh),
                name="进入通讯录页",
                timeout_sec=5.0,
                diag_dir=diag_dir,
            )
        if rpt:
            rpt.report_event_bg("通讯录", True)

    # ── Step 2：点击「新的客户」────────────────────────────────────────────
    time.sleep(0.25)
    hwnd = _refresh_main_hwnd()
    iw = ih = 1
    cl = ct = 0
    b1 = None
    if o.use_ocr:
        b1, iw, ih, cl, ct = _snap_pick(
            hwnd,
            lambda boxes, bw, bh: _pick_new_customer_nav(boxes, bw, bh, o),
            timeout_sec=5.0,
            diag_dir=diag_dir,
            step_name="新的客户",
        )
    _click_ocr_or_ratio(
        hwnd, "新的客户", b1, iw, ih, cl, ct,
        r.new_customers_rx, r.new_customers_ry, r.pause_after_new_customers, dry_run, prefer_ocr,
        allow_ratio_fallback,
    )
    if not dry_run and o.use_ocr:
        _wait_state(
            hwnd,
            lambda boxes, bw, bh: _has_page_title(boxes, bw, bh, "新的客户", o),
            name="进入新的客户页",
            timeout_sec=3.0,
            diag_dir=diag_dir,
        )
    if rpt:
        rpt.report_event_bg("新的客户", True)

    # ── Step 3：点击「+ 添加」────────────────────────────────────────────
    time.sleep(0.35)
    hwnd = _refresh_main_hwnd()
    iw = ih = 1
    cl = ct = 0
    bt = None
    if o.use_ocr:
        bt, iw, ih, cl, ct = _snap_pick(
            hwnd,
            lambda boxes, bw, bh: _pick_toolbar_add(boxes, bw, bh, o),
            timeout_sec=1.0,
            diag_dir=diag_dir,
            step_name="toolbar_add",
        )
    hwnd = _refresh_main_hwnd()
    _click_ocr_or_ratio(
        hwnd, "+ 添加", bt, iw, ih, cl, ct,
        r.toolbar_add_rx, r.toolbar_add_ry, r.pause_after_toolbar_add, dry_run, prefer_ocr,
        allow_ratio_fallback,
    )

    time.sleep(r.pause_after_dialog)
    if not dry_run and o.use_ocr:
        # 只验证弹窗标题出现即可，不强求搜索框同时被 OCR 识别
        _wait_state(
            hwnd,
            lambda boxes, bw, bh: _has_text(boxes, ("添加客户",), max_text_len=12),
            name="打开添加客户弹窗",
            timeout_sec=r.wait_dialog_timeout_sec,
            diag_dir=diag_dir,
        )
    if rpt:
        rpt.report_event_bg("+添加按钮", True)

    # ── Step 4：输入手机号 ────────────────────────────────────────────────
    if dry_run:
        print(f"  [输入手机号] {phone!r} (dry-run, skipped)", flush=True)
    elif o.use_ocr:
        bq, iw, ih, cl, ct = _snap_pick(
            hwnd,
            lambda boxes, bw, bh: _pick_dialog_search(boxes, bw, bh, o),
            timeout_sec=r.wait_dialog_timeout_sec,
            diag_dir=diag_dir,
            step_name="dialog_search",
        )
        if bq is not None and prefer_ocr:
            left, top, right, bottom = window_client_rect_screen(hwnd)
            w, h = max(1, right - left), max(1, bottom - top)
            sx = int(left + (bq.cx / max(iw, 1)) * w)
            sy = int(top + (bq.cy / max(ih, 1)) * h)
            print(f"  [输入手机号] OCR «{bq.text}» → screen=({sx},{sy})", flush=True)
            click_screen_point_foreground(hwnd, sx, sy, pause_after=0.25)
            type_text_foreground(sx, sy, phone, char_delay=0.06)
            time.sleep(0.15)
            press_enter_foreground(sx, sy)
            print(f"  [输入手机号] 已输入 {phone!r} 并按回车", flush=True)
            if rpt:
                rpt.report_event_bg("输入手机号", True, phone)
        else:
            if not allow_ratio_fallback:
                raise RuntimeError("[输入手机号] OCR 未找到搜索框；安全模式已停止，避免比例回退误点")
            print("  [输入手机号] OCR 未找到搜索框，回退比例", flush=True)
            left2, top2, right2, bottom2 = window_client_rect_screen(hwnd)
            w2, h2 = right2 - left2, bottom2 - top2
            sx2 = int(left2 + w2 * r.dialog_search_rx)
            sy2 = int(top2 + h2 * r.dialog_search_ry)
            click_screen_point_foreground(hwnd, sx2, sy2, pause_after=0.25)
            type_text_foreground(sx2, sy2, phone, char_delay=0.06)
            time.sleep(0.15)
            press_enter_foreground(sx2, sy2)
    else:
        print(f"  [输入手机号] {phone!r} (比例)", flush=True)
        left2, top2, right2, bottom2 = window_client_rect_screen(hwnd)
        w2, h2 = right2 - left2, bottom2 - top2
        sx2 = int(left2 + w2 * r.dialog_search_rx)
        sy2 = int(top2 + h2 * r.dialog_search_ry)
        click_screen_point_foreground(hwnd, sx2, sy2, pause_after=0.25)
        type_text_foreground(sx2, sy2, phone, char_delay=0.06)
        time.sleep(0.15)
        press_enter_foreground(sx2, sy2)

    # ── Step 5：等待搜索结果并点击「添加」────────────────────────────────
    print(f"  [等待搜索] {r.search_wait_sec:.1f}s …", flush=True)
    time.sleep(r.search_wait_sec)

    hwnd = _refresh_main_hwnd()
    iw = ih = 1
    cl = ct = 0
    br = None
    if o.use_ocr:
        br, iw, ih, cl, ct = _snap_pick(
            hwnd,
            lambda boxes, bw, bh: _pick_result_add(boxes, bw, bh, o),
            timeout_sec=r.wait_result_timeout_sec,
            diag_dir=diag_dir,
            step_name="result_add",
        )
    hwnd = _refresh_main_hwnd()
    _click_ocr_or_ratio(
        hwnd, "结果-添加", br, iw, ih, cl, ct,
        r.result_add_rx, r.result_add_ry, r.pause_after_result_add, dry_run, prefer_ocr,
        allow_ratio_fallback,
    )
    invite_boxes: list[OcrTextBox] = []
    invite_iw = invite_ih = 1
    invite_cl = invite_ct = 0
    if not dry_run and o.use_ocr:
        invite_boxes, invite_iw, invite_ih, invite_cl, invite_ct = _wait_state(
            hwnd,
            lambda boxes, bw, bh: _has_text(boxes, ("发送添加邀请",), max_text_len=16),
            name="打开发送添加邀请弹窗",
            timeout_sec=r.wait_invite_timeout_sec,
            diag_dir=diag_dir,
        )
    if rpt:
        rpt.report_event_bg("搜索结果-添加", True)

    if no_final_send:
        print("  [--no-final-send] 已停止，请在「发送添加邀请」里人工检查后点发送。", flush=True)
        return

    # ── Step 6：自定义邀请语 ──────────────────────────────────────────────
    # 从刚才 _wait_state 拿到的 OCR 框里找"发送添加邀请"标题，
    # 输入框就在标题正下方约 0.08 个窗口高度处。
    time.sleep(r.pause_before_invite_send)
    if invite_message and not dry_run:
        hwnd = _refresh_main_hwnd()
        left, top, right, bottom = window_client_rect_screen(hwnd)
        w, h = max(1, right - left), max(1, bottom - top)

        # 从已有 OCR 框定位"发送添加邀请"标题
        title_box = next(
            (b for b in invite_boxes if _norm_text(b.text) == "发送添加邀请"),
            None,
        )
        if title_box is not None:
            # 输入框：标题 X 位置偏右一点，Y 下方约 8% 窗口高
            title_nx = title_box.cx / max(invite_iw, 1)
            title_ny = title_box.cy / max(invite_ih, 1)
            six = int(left + (title_nx + 0.04) * w)
            siy = int(top  + (title_ny + 0.08) * h)
            print(f"  [邀请语] 从标题推算输入框 → screen=({six},{siy})", flush=True)
        else:
            six = int(left + w * r.invite_input_rx)
            siy = int(top  + h * r.invite_input_ry)
            print(f"  [邀请语] 比例回退 → screen=({six},{siy})", flush=True)

        # 点击输入框，用 foreground-window CEF 发 Ctrl+A + WM_CHAR
        click_screen_point_foreground(hwnd, six, siy, pause_after=0.20)
        fg = win32gui.GetForegroundWindow()
        cef = _find_cef_hwnd_at(fg, six, siy)
        win32api.SendMessage(cef, win32con.WM_SETFOCUS, 0, 0)
        time.sleep(0.05)
        # Ctrl+A 全选默认文案
        win32api.PostMessage(cef, win32con.WM_KEYDOWN, win32con.VK_CONTROL, (0x1D << 16) | 1)
        win32api.PostMessage(cef, win32con.WM_KEYDOWN, ord('A'), (0x1E << 16) | 1)
        win32api.PostMessage(cef, win32con.WM_KEYUP,   ord('A'), (1 << 31) | (1 << 30) | (0x1E << 16) | 1)
        win32api.PostMessage(cef, win32con.WM_KEYUP,   win32con.VK_CONTROL, (1 << 31) | (1 << 30) | (0x1D << 16) | 1)
        time.sleep(0.10)
        # 逐字 WM_CHAR 输入新邀请语
        for ch in invite_message:
            win32api.PostMessage(cef, win32con.WM_CHAR, ord(ch), 0)
            time.sleep(0.05)
        print(f"  [邀请语] 已输入: {invite_message!r}", flush=True)
        time.sleep(0.20)

    # ── Step 7：点击最终「发送」────────────────────────────────────────────
    hwnd = _refresh_main_hwnd()
    iw = ih = 1
    cl = ct = 0
    bf = None
    if o.use_ocr:
        bf, iw, ih, cl, ct = _snap_pick(
            hwnd,
            lambda boxes, bw, bh: _pick_invite_send(boxes, bw, bh, o),
            timeout_sec=r.wait_invite_timeout_sec,
            diag_dir=diag_dir,
            step_name="invite_send",
        )
    hwnd = _refresh_main_hwnd()
    _click_ocr_or_ratio(
        hwnd, "发送邀请", bf, iw, ih, cl, ct,
        r.invite_send_rx, r.invite_send_ry, 0.25, dry_run, prefer_ocr,
        allow_ratio_fallback,
    )
    print("  [完成] 已点击「发送」（若对方需验证，请等待对方通过）。", flush=True)
    if rpt:
        rpt.report_event("发送邀请", True)
