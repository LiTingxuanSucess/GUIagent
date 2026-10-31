"""
通用 computer-use 框架。

核心概念
────────
UiStep          — 单个步骤的函数式描述（find + act + verify）
ElementSpec     — 声明式元素描述，用关键词 + 区域约束自动生成 find 函数
UiStateMachine  — 显式有限状态机，管理多步骤 GUI 流程
cef_type_text   — 统一 CEF 文字输入路由（始终从 GetForegroundWindow 开始）
save_diagnostics— 失败现场快照（截图 + OCR JSONL）

设计原则
────────
1. 零副作用：所有函数接收 hwnd / snap_fn，不持有全局状态。
2. 显式声明：用 ElementSpec 描述"长什么样"，框架负责"如何找到"。
3. 统一输入路由：所有 CEF 键盘事件经由 GetForegroundWindow() → 子树查找，
   避免跨窗口 hwnd 失效。
"""
from __future__ import annotations

import json
import shutil
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any

from src.ocr_run import OcrTextBox

# ── 懒加载 Win32（仅 Windows 可用）─────────────────────────────────────
try:
    import win32api
    import win32con
    import win32gui
    _WIN32 = True
except ImportError:
    _WIN32 = False


# ══════════════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════════════

def _norm(text: str) -> str:
    """去除空白和零宽字符，便于关键词匹配。"""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith("Z") and ch not in "\u200b\u200c\u200d\ufeff")


# ══════════════════════════════════════════════════════════════════════════
# ElementSpec — 声明式元素描述
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class ElementSpec:
    """声明式描述如何从 OCR 结果中定位一个 UI 元素。

    Examples::

        # 精确匹配"通讯录"，且在屏幕左侧 15% 内
        ElementSpec(keywords=["通讯录"], region=(0.0, 0.0, 0.15, 1.0), exact=True)

        # 包含"添加"且文本不超过 6 个字符（避免匹配"添加时间"等长文本）
        ElementSpec(keywords=["添加"], max_text_len=6)

        # 匹配任意以"添加"结尾的文本
        ElementSpec(keywords=["添加"], endswith=True)
    """

    keywords: list[str]
    """必须包含（或精确匹配/以此结尾）的候选关键词列表，任一匹配即可。"""

    region: tuple[float, float, float, float] | None = None
    """归一化坐标约束 (x0, y0, x1, y1)，cx/cy 超出范围则跳过。"""

    max_text_len: int | None = None
    """元素文本最大长度（过滤"添加时间"等长文本中的误识别）。"""

    exact: bool = False
    """True = 全文精确匹配（norm 后），False = 包含匹配。"""

    endswith: bool = False
    """True = 以关键词结尾匹配（优先级低于 exact）。"""

    prefer_first: bool = True
    """多个候选时返回第一个（按 OCR 顺序）；False = 返回最后一个。"""

    def matches(self, box: OcrTextBox, iw: int, ih: int) -> bool:
        """判断 box 是否满足本 spec 的所有约束。"""
        t = _norm(box.text)
        if not t:
            return False

        if self.max_text_len is not None and len(t) > self.max_text_len:
            return False

        if self.region is not None:
            x0, y0, x1, y1 = self.region
            nx = box.cx / max(iw, 1)
            ny = box.cy / max(ih, 1)
            if not (x0 <= nx <= x1 and y0 <= ny <= y1):
                return False

        for kw in self.keywords:
            kw_n = _norm(kw)
            if self.exact and t == kw_n:
                return True
            if self.endswith and t.endswith(kw_n):
                return True
            if not self.exact and not self.endswith and kw_n in t:
                return True

        return False

    def find(self, boxes: list[OcrTextBox], iw: int, ih: int) -> OcrTextBox | None:
        """从 OCR 框列表中找到第一个（或最后一个）匹配的框。"""
        candidates = [b for b in boxes if self.matches(b, iw, ih)]
        if not candidates:
            return None
        return candidates[0] if self.prefer_first else candidates[-1]

    def as_find_fn(self) -> Callable[[list[OcrTextBox], int, int], OcrTextBox | None]:
        """返回可直接传给 UiStep.find 的函数。"""
        return self.find


# ══════════════════════════════════════════════════════════════════════════
# UiStep — 函数式步骤描述（与原来保持兼容）
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class UiStep:
    """单个 UI 操作步骤的描述。

    可以直接传入 lambda，也可以用 ElementSpec 生成 find 函数::

        step = UiStep(
            name="通讯录",
            find=ElementSpec(["通讯录"], region=(0,0,.15,1), exact=True).as_find_fn(),
            act=lambda hwnd, box, iw, ih: click_box(hwnd, box, iw, ih),
        )
    """

    name: str
    find: Callable[[list[OcrTextBox], int, int], Any]
    act: Callable[[int, Any, int, int], None]
    verify: Callable[[list[OcrTextBox], int, int], bool] | None = None
    timeout: float = 3.0
    interval: float = 0.25


# ══════════════════════════════════════════════════════════════════════════
# UiStateMachine — 显式有限状态机
# ══════════════════════════════════════════════════════════════════════════

class StateStatus(Enum):
    PENDING   = auto()
    RUNNING   = auto()
    DONE      = auto()
    FAILED    = auto()
    SKIPPED   = auto()


@dataclass
class UiState:
    """状态机中一个命名状态（UI 流程步骤）。

    Attributes:
        name:       状态名称（唯一标识）
        step:       对应的 UiStep
        on_success: 成功后跳转的状态名（None = 流程结束）
        on_failure: 失败后跳转的状态名（None = 抛出异常）
        skip_if:    (boxes, iw, ih) -> bool — 返回 True 则跳过此状态
    """

    name: str
    step: UiStep
    on_success: str | None = None
    on_failure: str | None = None
    skip_if: Callable[[list[OcrTextBox], int, int], bool] | None = None


@dataclass
class RunResult:
    """状态机执行结果。"""

    ok: bool
    states_run: list[str] = field(default_factory=list)
    states_skipped: list[str] = field(default_factory=list)
    failed_state: str | None = None
    error: str | None = None


class UiStateMachine:
    """显式有限状态机，管理 GUI 自动化多步骤流程。

    Usage::

        fsm = UiStateMachine(
            states=[
                UiState("contacts",    step_contacts,    on_success="new_customer"),
                UiState("new_customer",step_new_customer,on_success="toolbar_add"),
                UiState("toolbar_add", step_toolbar_add, on_success=None),
            ],
            snap_fn=lambda: snap_ocr(hwnd),
            hwnd=hwnd,
        )
        result = fsm.run(initial="contacts")
        if not result.ok:
            raise RuntimeError(result.error)
    """

    def __init__(
        self,
        states: list[UiState],
        snap_fn: Callable[[], tuple[list[OcrTextBox], int, int, int, int]],
        hwnd: int,
        *,
        dry_run: bool = False,
        allow_fallback: bool = False,
        diag_dir: Path | None = None,
        on_step_done: Callable[[str, bool, str | None], None] | None = None,
    ) -> None:
        self._states: dict[str, UiState] = {s.name: s for s in states}
        self._snap_fn = snap_fn
        self._hwnd = hwnd
        self._dry_run = dry_run
        self._allow_fallback = allow_fallback
        self._diag_dir = diag_dir
        self._on_step_done = on_step_done  # callback(name, ok, detail)

    def run(self, initial: str) -> RunResult:
        """从 initial 状态开始执行，直到 on_success=None 或出错。"""
        result = RunResult(ok=False)
        current_name: str | None = initial

        while current_name is not None:
            if current_name not in self._states:
                result.error = f"未知状态: {current_name!r}"
                return result

            state = self._states[current_name]

            # ── skip_if 检查 ─────────────────────────────────────────────
            if state.skip_if is not None:
                boxes, iw, ih, _, _ = self._snap_fn()
                if state.skip_if(boxes, iw, ih):
                    result.states_skipped.append(current_name)
                    if self._on_step_done:
                        self._on_step_done(current_name, True, "skipped")
                    current_name = state.on_success
                    continue

            # ── 执行步骤 ──────────────────────────────────────────────────
            try:
                run_ocr_step(
                    self._hwnd,
                    state.step,
                    self._snap_fn,
                    dry_run=self._dry_run,
                    allow_fallback=self._allow_fallback,
                    diag_dir=self._diag_dir,
                )
                result.states_run.append(current_name)
                if self._on_step_done:
                    self._on_step_done(current_name, True, None)
                current_name = state.on_success

            except RuntimeError as exc:
                result.failed_state = current_name
                result.error = str(exc)
                if self._on_step_done:
                    self._on_step_done(current_name, False, str(exc))
                if state.on_failure is not None:
                    current_name = state.on_failure
                else:
                    return result

        result.ok = True
        return result


# ══════════════════════════════════════════════════════════════════════════
# CEF 统一输入路由
# ══════════════════════════════════════════════════════════════════════════

def _enum_child_hwnds(parent: int) -> list[int]:
    result: list[int] = []
    if not _WIN32:
        return result
    def cb(hwnd: int, _: Any) -> bool:
        result.append(hwnd)
        return True
    win32gui.EnumChildWindows(parent, cb, None)
    return result


def _find_cef_hwnd(root: int, sx: int, sy: int) -> int:
    """从 root 窗口的子树中查找 Chrome_RenderWidgetHostHWND，
    要求其客户区包含屏幕坐标 (sx, sy)。

    若找不到，返回 root 本身（兜底）。
    """
    if not _WIN32:
        return root
    CEF_CLASS = "Chrome_RenderWidgetHostHWND"
    best = root
    for child in _enum_child_hwnds(root):
        try:
            cls = win32gui.GetClassName(child)
        except Exception:
            continue
        if cls != CEF_CLASS:
            continue
        try:
            left, top, right, bottom = win32gui.GetWindowRect(child)
        except Exception:
            continue
        if left <= sx <= right and top <= sy <= bottom:
            best = child
            break
    return best


def cef_type_text(sx: int, sy: int, text: str, char_delay: float = 0.05) -> None:
    """统一 CEF 文字输入路由。

    始终从 GetForegroundWindow() 的子树中查找目标 CEF 句柄，
    然后逐字发送 WM_CHAR。

    Args:
        sx, sy:     屏幕坐标（用于定位 CEF 子窗口）
        text:       要输入的文本
        char_delay: 每个字符的间隔（秒）
    """
    if not _WIN32:
        return
    fg = win32gui.GetForegroundWindow()
    cef = _find_cef_hwnd(fg, sx, sy)
    win32api.SendMessage(cef, win32con.WM_SETFOCUS, 0, 0)
    time.sleep(0.05)
    for ch in text:
        win32api.PostMessage(cef, win32con.WM_CHAR, ord(ch), 0)
        time.sleep(char_delay)


def cef_press_enter(sx: int, sy: int) -> None:
    """统一 CEF 回车键路由。"""
    if not _WIN32:
        return
    fg = win32gui.GetForegroundWindow()
    cef = _find_cef_hwnd(fg, sx, sy)
    win32api.PostMessage(cef, win32con.WM_KEYDOWN, win32con.VK_RETURN, (0x1C << 16) | 1)
    time.sleep(0.05)
    win32api.PostMessage(cef, win32con.WM_KEYUP, win32con.VK_RETURN,
                         (1 << 31) | (1 << 30) | (0x1C << 16) | 1)


def cef_select_all(sx: int, sy: int) -> None:
    """统一 CEF Ctrl+A 路由（全选）。"""
    if not _WIN32:
        return
    fg = win32gui.GetForegroundWindow()
    cef = _find_cef_hwnd(fg, sx, sy)
    win32api.SendMessage(cef, win32con.WM_SETFOCUS, 0, 0)
    time.sleep(0.05)
    win32api.PostMessage(cef, win32con.WM_KEYDOWN, win32con.VK_CONTROL, (0x1D << 16) | 1)
    win32api.PostMessage(cef, win32con.WM_KEYDOWN, ord("A"), (0x1E << 16) | 1)
    win32api.PostMessage(cef, win32con.WM_KEYUP, ord("A"),
                         (1 << 31) | (1 << 30) | (0x1E << 16) | 1)
    win32api.PostMessage(cef, win32con.WM_KEYUP, win32con.VK_CONTROL,
                         (1 << 31) | (1 << 30) | (0x1D << 16) | 1)
    time.sleep(0.05)


# ══════════════════════════════════════════════════════════════════════════
# save_diagnostics — 失败现场快照
# ══════════════════════════════════════════════════════════════════════════

def save_diagnostics(
    name: str,
    boxes: list[OcrTextBox],
    iw: int,
    ih: int,
    png_src: Path | None,
    diag_dir: Path,
) -> None:
    """保存失败现场到 diag_dir。

    保存内容：
    - <name>_<ts>.png   — 完整截图（若提供）
    - <name>_<ts>.jsonl — 每行一个 OCR 框（text, cx, cy, nx, ny）
    """
    if not diag_dir:
        return
    diag_dir = Path(diag_dir)
    diag_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
    stem = f"{name}_{ts}".replace(" ", "_").replace("/", "-").replace("\\", "-")

    if png_src and Path(png_src).is_file():
        dst = diag_dir / f"{stem}.png"
        try:
            shutil.copy2(png_src, dst)
            print(f"  [diag] 截图 → {dst}", flush=True)
        except Exception as e:
            print(f"  [diag] 截图保存失败: {e}", flush=True)

    jsonl = diag_dir / f"{stem}.jsonl"
    try:
        lines = []
        for b in boxes:
            lines.append(json.dumps(
                {
                    "text": b.text,
                    "cx": round(b.cx, 1),
                    "cy": round(b.cy, 1),
                    "nx": round(b.cx / max(iw, 1), 4),
                    "ny": round(b.cy / max(ih, 1), 4),
                },
                ensure_ascii=False,
            ))
        jsonl.write_text("\n".join(lines), encoding="utf-8")
        print(f"  [diag] OCR框 ({len(boxes)}个) → {jsonl}", flush=True)
    except Exception as e:
        print(f"  [diag] JSONL保存失败: {e}", flush=True)


# ══════════════════════════════════════════════════════════════════════════
# run_ocr_step — 步骤执行器（与原版保持兼容）
# ══════════════════════════════════════════════════════════════════════════

def run_ocr_step(
    hwnd: int,
    step: UiStep,
    snap_fn: Callable[[], tuple[list[OcrTextBox], int, int, int, int]],
    *,
    dry_run: bool = False,
    allow_fallback: bool = False,
    diag_dir: Path | None = None,
) -> tuple[Any, int, int]:
    """执行一个 UI 步骤，带超时轮询和失败诊断。

    Returns:
        (element, iw, ih) — element 为 None 且 allow_fallback=True 时，
        调用方可选择按比例坐标回退。

    Raises:
        RuntimeError: 超时且 allow_fallback=False
    """
    deadline = time.monotonic() + max(0.0, step.timeout)
    last_boxes: list[OcrTextBox] = []
    last_iw = last_ih = 1
    last_cl = last_ct = 0
    element = None

    while True:
        boxes, iw, ih, cl, ct = snap_fn()
        last_boxes, last_iw, last_ih, last_cl, last_ct = boxes, iw, ih, cl, ct
        element = step.find(boxes, iw, ih)
        if element is not None or time.monotonic() >= deadline:
            break
        time.sleep(step.interval)

    if element is None:
        preview = ", ".join(b.text for b in last_boxes[:24])
        msg = (
            f"[{step.name}] OCR 未命中；snapshot={last_iw}x{last_ih} "
            f"client=({last_cl},{last_ct}) preview={preview}"
        )
        if diag_dir:
            save_diagnostics(f"failed_{step.name}", last_boxes, last_iw, last_ih, None, diag_dir)
        if not allow_fallback:
            raise RuntimeError(msg)
        print(f"  {msg}", flush=True)
        return None, last_iw, last_ih

    if not dry_run:
        step.act(hwnd, element, last_iw, last_ih)
    else:
        print(f"  [{step.name}] dry-run: found element text={getattr(element, 'text', element)!r}", flush=True)

    if step.verify is not None and not dry_run:
        verify_deadline = time.monotonic() + step.timeout
        while True:
            boxes, iw, ih, cl, ct = snap_fn()
            if step.verify(boxes, iw, ih):
                break
            if time.monotonic() >= verify_deadline:
                preview = ", ".join(b.text for b in boxes[:24])
                msg = f"[{step.name}] 状态验证失败；snapshot={iw}x{ih} preview={preview}"
                if diag_dir:
                    save_diagnostics(f"verify_failed_{step.name}", boxes, iw, ih, None, diag_dir)
                raise RuntimeError(msg)
            time.sleep(step.interval)

    return element, last_iw, last_ih
