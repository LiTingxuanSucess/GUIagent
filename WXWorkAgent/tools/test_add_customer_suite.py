"""
WXWork Add-Customer Automated Test Suite
=========================================
Design goals
------------
1. Mock mode (default): no Paddle / Win32 required; injects fake OCR boxes and
   verifies every _pick_* function, ElementSpec, and the state-machine helpers.
2. OCR mode (--ocr): runs real PaddleOCR on shots/add_customer/step*.png and
   verifies the correct box is selected each step.
3. Replay mode (--replay): monkey-patches click/type/enter actions and drives
   add_customer_by_phone end-to-end with a mock OCR sequence, then asserts that
   the right number of clicks happened and the phone number was typed.

Usage
-----
  py -3.11 tools/test_add_customer_suite.py
  py -3.11 tools/test_add_customer_suite.py --replay
  py -3.11 tools/test_add_customer_suite.py --ocr
  py -3.11 tools/test_add_customer_suite.py --ocr --replay -v
"""
from __future__ import annotations

import argparse
import sys
import textwrap
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_ocr_ready = False

def _ensure_ocr() -> None:
    global _ocr_ready
    if _ocr_ready:
        return
    from src.ocr_runtime import init_ocr_runtime
    init_ocr_runtime()
    _ocr_ready = True


from src.add_customer import (  # noqa: E402
    AddCustomerOcrOptions,
    _infer_contacts_from_anchors,
    _is_already_in_contacts,
    _pick_contacts_nav,
    _pick_dialog_search,
    _pick_invite_send,
    _pick_new_customer_nav,
    _pick_result_add,
    _pick_toolbar_add,
    _pick_boxes,
)
from src.computer_use import ElementSpec  # noqa: E402
from src.ocr_run import OcrTextBox  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# Lightweight test runner (no pytest dependency)
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class TestResult:
    name: str
    ok: bool
    skipped: bool = False
    message: str = ""
    exc: str = ""


@dataclass
class TestCase:
    name: str
    fn: Callable[[], None]
    skip_reason: str = ""

    def run(self, verbose: bool = False) -> TestResult:
        if self.skip_reason:
            return TestResult(self.name, ok=True, skipped=True, message=self.skip_reason)
        try:
            self.fn()
            return TestResult(self.name, ok=True)
        except AssertionError as e:
            return TestResult(self.name, ok=False, message=str(e))
        except Exception as e:
            return TestResult(self.name, ok=False, exc=traceback.format_exc(), message=str(e))


@dataclass
class Suite:
    name: str
    cases: list[TestCase] = field(default_factory=list)
    verbose: bool = False

    def case(self, name: str, skip_reason: str = "") -> Any:
        def decorator(fn: Callable[[], None]) -> Callable[[], None]:
            self.cases.append(TestCase(name, fn, skip_reason))
            return fn
        return decorator

    def run(self) -> list[TestResult]:
        results: list[TestResult] = []
        print(f"\n{'─'*72}")
        print(f"Suite: {self.name}")
        print(f"{'─'*72}")
        for tc in self.cases:
            res = tc.run(self.verbose)
            results.append(res)
            tag = "-- SKIP" if res.skipped else ("[PASS]" if res.ok else "[FAIL]")
            msg = f"  {tag}  {res.name}"
            if res.message and (not res.ok or self.verbose):
                msg += f"\n       {res.message}"
            if res.exc and not res.ok:
                msg += "\n" + textwrap.indent(res.exc, "       ")
            print(msg)
        return results


def assert_eq(actual: Any, expected: Any, msg: str = "") -> None:
    if actual != expected:
        raise AssertionError(f"{msg}\n  actual:   {actual!r}\n  expected: {expected!r}")


def assert_none(val: Any, msg: str = "") -> None:
    if val is not None:
        raise AssertionError(f"{msg}: expected None but got {val!r}")


def assert_not_none(val: Any, msg: str = "") -> None:
    if val is None:
        raise AssertionError(f"{msg}: expected non-None but got None")


def assert_in(sub: str, text: str, msg: str = "") -> None:
    if sub not in text:
        raise AssertionError(f"{msg}: {sub!r} not in {text!r}")


def assert_approx(actual: float, expected: float, tol: float, msg: str = "") -> None:
    if abs(actual - expected) > tol:
        raise AssertionError(
            f"{msg}: |{actual:.1f} - {expected:.1f}| = {abs(actual - expected):.1f} > {tol:.1f}"
        )


# ══════════════════════════════════════════════════════════════════════════
# Mock OCR scenes
# ══════════════════════════════════════════════════════════════════════════

IW, IH = 1572, 1233


def B(text: str, nx: float, ny: float) -> OcrTextBox:
    return OcrTextBox(text, IW * nx, IH * ny)


SCENE_CONTACTS_NORMAL = [
    B("消息",    0.03, 0.08),
    B("通讯录",  0.03, 0.72),
    B("高级功能",0.03, 0.90),
    B("新的客户",0.22, 0.18),
    B("我的客户",0.22, 0.24),
]

SCENE_CONTACTS_INFER = [
    # 通讯录文字 OCR 漏掉，只有其他锚点可用
    B("消息",    0.03, 0.08),
    B("邮件",    0.03, 0.15),
    B("文档",    0.03, 0.22),
    B("工作台",  0.03, 0.67),
    B("微盘",    0.03, 0.81),
]

SCENE_ALREADY_IN_CONTACTS = [
    B("新的客户",   0.22, 0.12),
    B("我的客户",   0.22, 0.18),
    B("组织架构",   0.22, 0.24),
    B("外部联系人", 0.22, 0.30),
]

SCENE_NEW_CUSTOMER_PAGE = [
    # "新的客户" title must be in region_page_title (0.25..0.65, 0.02..0.12)
    B("新的客户",  0.38, 0.06),
    B("+ 添加",   0.82, 0.11),
]

SCENE_TOOLBAR_ADD_NORMAL = [
    B("新的客户",  0.38, 0.06),
    B("+ 添加",   0.82, 0.11),
]

# Real-world OCR sometimes returns circled-digit variant instead of "+ 添加"
SCENE_TOOLBAR_ADD_VARIANT = [
    B("新的客户",    0.22, 0.12),
    B("\u2463\u6dfb\u52a0", 0.82, 0.11),   # U+2463 = circled 4
]

SCENE_TOOLBAR_ADD_NONE = [
    B("新的客户",  0.22, 0.12),
    B("刷新",      0.80, 0.11),
]

SCENE_DIALOG_SEARCH = [
    B("添加客户",         0.50, 0.08),
    B("手机号/邮箱",      0.48, 0.28),
    B("Q 手机号/邮箱",    0.48, 0.35),
]

SCENE_RESULT_ADD = [
    B("13800138000",       0.45, 0.22),
    B("南岛 South Island", 0.38, 0.38),
    B("添加",              0.78, 0.38),   # target: result-row add button
    B("+ 添加",            0.85, 0.10),  # toolbar add — should NOT be selected
]

SCENE_RESULT_ADD_NONE = [
    B("13800138000",   0.45, 0.22),
    B("该手机号不存在", 0.45, 0.35),
]

SCENE_INVITE_SEND = [
    B("发送添加邀请",           0.52, 0.12),
    B("我是灵耘致新的ltx",   0.50, 0.35),
    B("发送",                   0.52, 0.62),
]

SCENE_INVITE_SEND_NONE = [
    B("发送添加邀请",           0.52, 0.12),
    B("我是灵耘致新",           0.50, 0.35),
    # no send button yet
]

SCENE_MIXED = [
    B("消息",          0.03, 0.08),
    B("通讯录",        0.03, 0.72),
    B("新的客户",      0.22, 0.12),
    B("添加时间",      0.50, 0.50),
    B("+ 添加",        0.82, 0.11),
    B("添加",          0.78, 0.38),
    B("\u2463\u6dfb\u52a0", 0.82, 0.18),
    B("发送",          0.52, 0.62),
    B("发送添加邀请",  0.52, 0.12),
]


# ══════════════════════════════════════════════════════════════════════════
# Suite 1: _pick_* unit tests
# ══════════════════════════════════════════════════════════════════════════

def make_pick_suite(verbose: bool = False) -> Suite:
    suite = Suite("_pick_* function unit tests", verbose=verbose)
    o = AddCustomerOcrOptions()

    @suite.case("pick_contacts_nav normal scene finds tonxunlu")
    def _():
        b = _pick_contacts_nav(SCENE_CONTACTS_NORMAL, IW, IH, o)
        assert_not_none(b, "should hit tonxunlu")
        assert_in("通讯录", b.text)

    @suite.case("pick_contacts_nav infers from anchors when text missing")
    def _():
        b = _pick_contacts_nav(SCENE_CONTACTS_INFER, IW, IH, o)
        assert_not_none(b, "should infer from anchors")
        expected_ny = 0.74
        actual_ny = b.cy / IH
        assert_approx(actual_ny, expected_ny, 0.08, "inferred Y offset")

    @suite.case("is_already_in_contacts returns True on contacts page")
    def _():
        ok = _is_already_in_contacts(SCENE_ALREADY_IN_CONTACTS, IW, IH)
        assert ok, "should return True"

    @suite.case("is_already_in_contacts returns False on plain message page")
    def _():
        boxes = [B("消息", 0.03, 0.08), B("通讯录", 0.03, 0.72)]
        ok = _is_already_in_contacts(boxes, IW, IH)
        assert not ok, "no contacts-page anchor words, should return False"

    @suite.case("infer_contacts_from_anchors Y coordinate accuracy")
    def _():
        inferred = _infer_contacts_from_anchors(SCENE_CONTACTS_INFER, IW, IH, o.nav_rail_nx_max)
        assert_not_none(inferred, "should infer position")
        assert_approx(inferred.cy, IH * 0.74, IH * 0.08, "inferred cy")

    @suite.case("pick_toolbar_add normal plus-add button")
    def _():
        b = _pick_toolbar_add(SCENE_TOOLBAR_ADD_NORMAL, IW, IH, o)
        assert_not_none(b, "should find add button")
        assert_in("添加", b.text)

    @suite.case("pick_toolbar_add accepts circled-digit OCR variant")
    def _():
        b = _pick_toolbar_add(SCENE_TOOLBAR_ADD_VARIANT, IW, IH, o)
        assert_not_none(b, "circled-digit variant should match")
        assert_in("添加", b.text)

    @suite.case("pick_toolbar_add returns None when button absent")
    def _():
        b = _pick_toolbar_add(SCENE_TOOLBAR_ADD_NONE, IW, IH, o)
        assert_none(b, "no add button: should be None")

    @suite.case("pick_dialog_search finds phone/email placeholder")
    def _():
        b = _pick_dialog_search(SCENE_DIALOG_SEARCH, IW, IH, o)
        assert_not_none(b, "should find search box")
        assert_in("手机号", b.text)

    @suite.case("pick_result_add hits result-row add, not toolbar add")
    def _():
        b = _pick_result_add(SCENE_RESULT_ADD, IW, IH, o)
        assert_not_none(b, "should find result-row add")
        ny = b.cy / IH
        assert ny > 0.25, f"result row should be Y > 0.25, got {ny:.3f}"

    @suite.case("pick_result_add returns None when no search result")
    def _():
        b = _pick_result_add(SCENE_RESULT_ADD_NONE, IW, IH, o)
        assert_none(b, "no result row: should be None")

    @suite.case("pick_new_customer_nav finds sidebar entry")
    def _():
        b = _pick_new_customer_nav(SCENE_CONTACTS_NORMAL, IW, IH, o)
        assert_not_none(b, "should find new-customer nav")
        assert_in("新的客户", b.text)

    @suite.case("pick_invite_send finds send button")
    def _():
        b = _pick_invite_send(SCENE_INVITE_SEND, IW, IH, o)
        assert_not_none(b, "should find send button")
        assert_in("发送", b.text)

    @suite.case("pick_invite_send returns None when send button absent")
    def _():
        b = _pick_invite_send(SCENE_INVITE_SEND_NONE, IW, IH, o)
        assert_none(b, "no send button: should be None")

    return suite


# ══════════════════════════════════════════════════════════════════════════
# Suite 2: ElementSpec declarative matching
# ══════════════════════════════════════════════════════════════════════════

def make_elementspec_suite(verbose: bool = False) -> Suite:
    suite = Suite("ElementSpec declarative matching", verbose=verbose)

    @suite.case("exact match: tonxunlu in left rail only")
    def _():
        spec = ElementSpec(keywords=["通讯录"], exact=True, region=(0.0, 0.0, 0.15, 1.0))
        b = spec.find(SCENE_MIXED, IW, IH)
        assert_not_none(b)
        assert_eq(b.text, "通讯录")

    @suite.case("contains + max_text_len: hits faosong (4 chars) not fa-song-tian-jia-yao-qing (8 chars)")
    def _():
        spec = ElementSpec(keywords=["发送"], max_text_len=4)
        b = spec.find(SCENE_MIXED, IW, IH)
        assert_not_none(b)
        assert_eq(b.text, "发送")

    @suite.case("endswith: matches both + tianjia and variant circled tianjia")
    def _():
        spec = ElementSpec(keywords=["添加"], endswith=True, max_text_len=6,
                           region=(0.6, 0.0, 1.0, 0.25))
        b = spec.find(SCENE_MIXED, IW, IH)
        assert_not_none(b)
        assert b.text.endswith("添加"), f"expected endswith 添加, got {b.text!r}"

    @suite.case("max_text_len filters out tianjia-shijian (long text)")
    def _():
        spec = ElementSpec(keywords=["添加"], max_text_len=3)
        candidates = [b for b in SCENE_MIXED if spec.matches(b, IW, IH)]
        texts = [b.text for b in candidates]
        assert "添加时间" not in texts, f"tianjia-shijian should be filtered: {texts}"
        assert "添加" in texts, f"short tianjia should pass: {texts}"

    @suite.case("region constraint excludes xindekhach outside left rail")
    def _():
        spec = ElementSpec(keywords=["消息", "通讯录", "新的客户"],
                           region=(0.0, 0.0, 0.15, 1.0))
        candidates = [b for b in SCENE_MIXED if spec.matches(b, IW, IH)]
        texts = {b.text for b in candidates}
        assert "新的客户" not in texts, "xin-de-kehu at x=0.22 should be excluded by region"
        assert "通讯录" in texts, "tonxunlu at x=0.03 should pass"

    @suite.case("prefer_last returns box with higher Y than prefer_first")
    def _():
        spec_first = ElementSpec(keywords=["添加"], endswith=True, prefer_first=True)
        spec_last  = ElementSpec(keywords=["添加"], endswith=True, prefer_first=False)
        b_first = spec_first.find(SCENE_MIXED, IW, IH)
        b_last  = spec_last.find(SCENE_MIXED, IW, IH)
        assert_not_none(b_first)
        assert_not_none(b_last)
        assert b_last.cy >= b_first.cy, "prefer_last should return box with larger cy"

    @suite.case("as_find_fn returns callable equivalent to find()")
    def _():
        spec = ElementSpec(keywords=["发送"], max_text_len=4)
        fn = spec.as_find_fn()
        b1 = spec.find(SCENE_MIXED, IW, IH)
        b2 = fn(SCENE_MIXED, IW, IH)
        assert_eq(b1, b2, "as_find_fn should be equivalent")

    @suite.case("no match returns None")
    def _():
        spec = ElementSpec(keywords=["不存在的文字"], exact=True)
        b = spec.find(SCENE_MIXED, IW, IH)
        assert_none(b, "non-existent keyword should return None")

    return suite


# ══════════════════════════════════════════════════════════════════════════
# Suite 3: Full-pipeline monkeypatch replay
# ══════════════════════════════════════════════════════════════════════════

def make_replay_suite(verbose: bool = False) -> Suite:
    suite = Suite("Full pipeline monkeypatch replay", verbose=verbose)

    @suite.case("add_customer_by_phone: mock OCR -> correct clicks + phone input")
    def _():
        import src.add_customer as ac_mod
        import src.actions as act_mod
        import src.capture as cap_mod

        calls: list[dict] = []

        snap_sequence = [
            [B("消息", 0.03, 0.08), B("通讯录", 0.03, 0.72)],  # pre-check (not in contacts)
            SCENE_CONTACTS_NORMAL,       # step1 snap
            SCENE_ALREADY_IN_CONTACTS,   # step1 verify
            SCENE_NEW_CUSTOMER_PAGE,     # step2 snap
            SCENE_NEW_CUSTOMER_PAGE,     # step2 verify
            SCENE_TOOLBAR_ADD_NORMAL,    # step3 snap
            [B("添加客户", 0.50, 0.12)], # step3 verify (dialog appeared)
            SCENE_DIALOG_SEARCH,         # step4 snap
            SCENE_RESULT_ADD,            # step5 snap
            SCENE_INVITE_SEND,           # step5 verify (invite dialog)
            SCENE_INVITE_SEND,           # step7 snap (send button)
        ]
        snap_idx = [0]

        def fake_snap_ocr(hwnd):     # noqa: ARG001
            idx = min(snap_idx[0], len(snap_sequence) - 1)
            snap_idx[0] += 1
            return snap_sequence[idx], IW, IH, 0, 0

        def fake_snap_ocr_png(hwnd): # noqa: ARG001
            import tempfile
            png = Path(tempfile.mktemp(suffix=".png"))
            idx = min(snap_idx[0], len(snap_sequence) - 1)
            snap_idx[0] += 1
            return snap_sequence[idx], IW, IH, 0, 0, png

        def fake_click(hwnd, sx, sy, pause_after=0.0): # noqa: ARG001
            calls.append({"type": "click", "sx": sx, "sy": sy})

        def fake_type(sx, sy, text, char_delay=0.0): # noqa: ARG001
            calls.append({"type": "type", "text": text})

        def fake_enter(sx, sy): # noqa: ARG001
            calls.append({"type": "enter"})

        def fake_hwnd():
            return 1234

        def fake_rect(hwnd): # noqa: ARG001
            return (0, 0, IW, IH)

        orig = {}
        try:
            orig["snap"]     = ac_mod._snap_ocr
            orig["snap_png"] = ac_mod._snap_ocr_with_png
            orig["click"]    = act_mod.click_screen_point_foreground
            orig["type"]     = act_mod.type_text_foreground
            orig["enter"]    = act_mod.press_enter_foreground
            # add_customer.py uses its own imported references — must patch there too
            orig["click_ac"] = ac_mod.click_screen_point_foreground
            orig["type_ac"]  = ac_mod.type_text_foreground
            orig["enter_ac"] = ac_mod.press_enter_foreground
            orig["refresh"]  = ac_mod._refresh_main_hwnd
            orig["fg"]       = ac_mod._foreground_wework_hwnd
            orig["wait_fg"]  = ac_mod._wait_wework_foreground
            orig["rect_cap"] = cap_mod.window_client_rect_screen
            orig["rect_ac"]  = ac_mod.window_client_rect_screen

            ac_mod._snap_ocr                        = fake_snap_ocr
            ac_mod._snap_ocr_with_png               = fake_snap_ocr_png
            act_mod.click_screen_point_foreground   = fake_click
            act_mod.type_text_foreground            = fake_type
            act_mod.press_enter_foreground          = fake_enter
            ac_mod.click_screen_point_foreground    = fake_click
            ac_mod.type_text_foreground             = fake_type
            ac_mod.press_enter_foreground           = fake_enter
            ac_mod._refresh_main_hwnd               = fake_hwnd
            ac_mod._foreground_wework_hwnd          = fake_hwnd
            ac_mod._wait_wework_foreground          = lambda hwnd, sec: None
            cap_mod.window_client_rect_screen       = fake_rect
            ac_mod.window_client_rect_screen        = fake_rect  # patched at import site

            from src.add_customer import (
                AddCustomerOcrOptions, AddCustomerRatios, add_customer_by_phone,
            )
            add_customer_by_phone(
                hwnd=1234,
                phone="13800138000",
                ratios=AddCustomerRatios(),
                ocr_opts=AddCustomerOcrOptions(),
                skip_nav=False,
                no_final_send=False,
                dry_run=False,
                allow_ratio_fallback=False,
                require_foreground=True,
                foreground_wait_sec=0.0,
            )
        finally:
            ac_mod._snap_ocr                        = orig["snap"]
            ac_mod._snap_ocr_with_png               = orig["snap_png"]
            act_mod.click_screen_point_foreground   = orig["click"]
            act_mod.type_text_foreground            = orig["type"]
            act_mod.press_enter_foreground          = orig["enter"]
            ac_mod.click_screen_point_foreground    = orig["click_ac"]
            ac_mod.type_text_foreground             = orig["type_ac"]
            ac_mod.press_enter_foreground           = orig["enter_ac"]
            ac_mod._refresh_main_hwnd               = orig["refresh"]
            ac_mod._foreground_wework_hwnd          = orig["fg"]
            ac_mod._wait_wework_foreground          = orig["wait_fg"]
            cap_mod.window_client_rect_screen       = orig["rect_cap"]
            ac_mod.window_client_rect_screen        = orig["rect_ac"]

        click_calls = [c for c in calls if c["type"] == "click"]
        type_calls  = [c for c in calls if c["type"] == "type"]
        enter_calls = [c for c in calls if c["type"] == "enter"]

        # At least 4 clicks: tonxunlu / xin-de-kehu / toolbar-add / result-add / send
        assert len(click_calls) >= 4, f"too few clicks: {len(click_calls)}"

        # Phone number must have been typed
        typed_texts = [c["text"] for c in type_calls]
        assert "13800138000" in typed_texts, f"phone not typed; typed={typed_texts}"

        # Enter must have been pressed
        assert len(enter_calls) >= 1, "press_enter_foreground never called"

        # All click coords should be within window bounds (with 2x margin for safety)
        for c in click_calls:
            assert 0 <= c["sx"] <= IW * 2, f"click X={c['sx']} out of bounds"
            assert 0 <= c["sy"] <= IH * 2, f"click Y={c['sy']} out of bounds"

    return suite


# ══════════════════════════════════════════════════════════════════════════
# Suite 4: Real OCR (--ocr mode)
# ══════════════════════════════════════════════════════════════════════════

def make_ocr_suite(verbose: bool = False) -> Suite:
    suite = Suite("Real OCR on shots/add_customer/step*.png", verbose=verbose)
    folder = ROOT / "shots" / "add_customer"
    shots = sorted(folder.glob("step*.png")) if folder.exists() else []
    o = AddCustomerOcrOptions()

    def _shot(name: str) -> Path | None:
        return next((p for p in shots if name in p.name), None)

    step01 = _shot("step01")
    step02 = _shot("step02")
    step03 = _shot("step03")
    step04 = _shot("step04")

    @suite.case(
        "step01.png: pick_contacts_nav hits tonxunlu",
        skip_reason="" if step01 else "shots/add_customer/step01.png not found",
    )
    def _():
        _ensure_ocr()
        from src.ocr_run import ocr_image_text_boxes
        from PIL import Image
        im = Image.open(step01)
        iw, ih = im.size
        boxes = ocr_image_text_boxes(step01)
        b = _pick_contacts_nav(boxes, iw, ih, o)
        assert_not_none(b, f"OCR {len(boxes)} boxes: tonxunlu not found")
        assert_in("通讯录", b.text)

    @suite.case(
        "step02.png: pick_toolbar_add hits add button",
        skip_reason="" if step02 else "shots/add_customer/step02.png not found",
    )
    def _():
        _ensure_ocr()
        from src.ocr_run import ocr_image_text_boxes
        from PIL import Image
        im = Image.open(step02)
        iw, ih = im.size
        boxes = ocr_image_text_boxes(step02)
        b = _pick_toolbar_add(boxes, iw, ih, o)
        assert_not_none(b, f"OCR {len(boxes)} boxes: toolbar add not found")

    @suite.case(
        "step03.png: pick_result_add hits result-row add",
        skip_reason="" if step03 else "shots/add_customer/step03.png not found",
    )
    def _():
        _ensure_ocr()
        from src.ocr_run import ocr_image_text_boxes
        from PIL import Image
        im = Image.open(step03)
        iw, ih = im.size
        boxes = ocr_image_text_boxes(step03)
        b = _pick_result_add(boxes, iw, ih, o)
        assert_not_none(b, f"OCR {len(boxes)} boxes: result-row add not found")
        assert b.cy / ih > 0.2, f"Y={b.cy / ih:.3f} too high, likely toolbar match"

    @suite.case(
        "step04.png: pick_invite_send hits send button",
        skip_reason="" if step04 else "shots/add_customer/step04.png not found",
    )
    def _():
        _ensure_ocr()
        from src.ocr_run import ocr_image_text_boxes
        from PIL import Image
        im = Image.open(step04)
        iw, ih = im.size
        boxes = ocr_image_text_boxes(step04)
        b = _pick_invite_send(boxes, iw, ih, o)
        assert_not_none(b, f"OCR {len(boxes)} boxes: send button not found")

    return suite


# ══════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="WXWork Add-Customer test suite")
    ap.add_argument("--ocr",    action="store_true", help="run real Paddle OCR (needs screenshots)")
    ap.add_argument("--replay", action="store_true", help="run monkeypatch full-pipeline replay")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    all_results: list[TestResult] = []

    all_results += make_pick_suite(args.verbose).run()
    all_results += make_elementspec_suite(args.verbose).run()

    if args.replay:
        all_results += make_replay_suite(args.verbose).run()
    else:
        print("\n  [tip] skip full-pipeline replay; use --replay to enable")

    if args.ocr:
        all_results += make_ocr_suite(args.verbose).run()
    else:
        print("  [tip] skip real OCR; use --ocr to enable (put screenshots in shots/add_customer/)")

    passed  = sum(1 for r in all_results if r.ok and not r.skipped)
    failed  = sum(1 for r in all_results if not r.ok)
    skipped = sum(1 for r in all_results if r.skipped)

    print(f"\n{'='*72}")
    print(f"TOTAL {len(all_results)}   PASS {passed}   FAIL {failed}   SKIP {skipped}")
    print(f"{'='*72}")

    if failed:
        print("\nFailed:")
        for r in all_results:
            if not r.ok:
                print(f"  [FAIL]  {r.name}")
                if r.message:
                    print(f"         {r.message}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
