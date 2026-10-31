"""
Keyboard / mouse helpers for 企业微信 (logged-in desktop client).

All input goes via PostMessage / SendMessage to the WeWork HWND tree,
bypassing the global SendInput API and its LLMHF_INJECTED flag.

Key design decisions
--------------------
* WM_PASTE does NOT work with CEF (Chromium Embedded) input widgets.
* WM_CHAR (Unicode character message) DOES work and is the correct way
  to deliver text to a CEF renderer without involving keyboard state.
* For Enter / Ctrl+F we still use WM_KEYDOWN/WM_KEYUP, which CEF processes
  as physical key events (it handles these even via PostMessage).
* We locate the input-area CEF widget by checking which
  Chrome_RenderWidgetHostHWND child contains the target screen point,
  rather than blindly picking the last child.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import time

import pyperclip
import win32api
import win32con
import win32gui
import win32process

from src.capture import window_client_rect_screen

_GA_ROOT = getattr(win32gui, "GA_ROOT", 2)

# ── SendInput helpers (Unicode keyboard, goes to focused window) ──────────
_INPUT_KEYBOARD = 1
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_KEYUP   = 0x0002

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.wintypes.WORD),
        ("wScan",       ctypes.wintypes.WORD),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("_pad", ctypes.c_byte * 24)]

class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("_u", _INPUT_UNION)]


def _si_char(ch: str) -> list[_INPUT]:
    """Create SendInput DOWN+UP pair for a single Unicode character."""
    cp = ord(ch)
    inputs = []
    for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
        inp = _INPUT()
        inp.type = _INPUT_KEYBOARD
        inp._u.ki.wVk = 0
        inp._u.ki.wScan = cp & 0xFFFF
        inp._u.ki.dwFlags = flags
        inputs.append(inp)
    return inputs


def type_text_foreground(x_screen: int, y_screen: int, text: str, char_delay: float = 0.04) -> None:
    """Find the CEF widget in the foreground window at (x_screen, y_screen) and WM_CHAR *text*.

    Unlike type_text_at_screen, this searches the FOREGROUND window's child tree,
    so it works even when the target is a pop-up dialog separate from the main hwnd.
    """
    fg = win32gui.GetForegroundWindow()
    cef = _find_cef_hwnd_at(fg, x_screen, y_screen)
    win32api.SendMessage(cef, win32con.WM_SETFOCUS, 0, 0)
    time.sleep(0.05)
    _wm_type(cef, text, delay=char_delay)


def press_enter_foreground(x_screen: int, y_screen: int) -> None:
    """WM_KEYDOWN/UP VK_RETURN at the CEF widget in the foreground window."""
    fg = win32gui.GetForegroundWindow()
    cef = _find_cef_hwnd_at(fg, x_screen, y_screen)
    _wm_enter(cef)


def sendinput_text(text: str, char_delay: float = 0.04) -> None:
    """Kept for compatibility; delegates to type_text_foreground using last cursor pos."""
    x, y = win32api.GetCursorPos()
    type_text_foreground(x, y, text, char_delay=char_delay)


def sendinput_enter(delay: float = 0.05) -> None:
    """Kept for compatibility; delegates to press_enter_foreground using last cursor pos."""
    x, y = win32api.GetCursorPos()
    press_enter_foreground(x, y)


# ── VK codes & scan codes ─────────────────────────────────────────────────
_VK_CONTROL = 0x11
_VK_RETURN  = 0x0D
_VK_BACK    = 0x08
_VK_F       = ord('F')

_SC = {
    _VK_CONTROL: 0x1D,
    _VK_RETURN:  0x1C,
    _VK_BACK:    0x0E,
    _VK_F:       0x21,
}


def _lp_down(vk: int) -> int:
    return (_SC.get(vk, 0) << 16) | 1


def _lp_up(vk: int) -> int:
    return (1 << 31) | (1 << 30) | (_SC.get(vk, 0) << 16) | 1


# ── CEF child-window helpers ──────────────────────────────────────────────

def _find_cef_hwnd_at(parent: int, x_screen: int, y_screen: int) -> int:
    """
    Return the Chrome_RenderWidgetHostHWND child of *parent* whose window
    rect contains (x_screen, y_screen).  Falls back to the last found CEF
    child, then to *parent* itself.

    Using the screen point avoids picking the wrong CEF widget (e.g., the
    header panel instead of the input area) in apps with multiple renderers.
    """
    containing: list[int] = []
    others:     list[int] = []

    def _cb(hwnd: int, _: object) -> bool:
        cls = win32gui.GetClassName(hwnd)
        if cls in ("Chrome_RenderWidgetHostHWND", "CefBrowserWindow"):
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                if l <= x_screen <= r and t <= y_screen <= b:
                    containing.append(hwnd)
                else:
                    others.append(hwnd)
            except Exception:
                others.append(hwnd)
        return True

    try:
        win32gui.EnumChildWindows(parent, _cb, None)
    except Exception:
        pass

    if containing:
        return containing[-1]
    if others:
        return others[-1]
    return parent


def _find_cef_hwnd(parent: int) -> int:
    """Find any CEF child (fallback when no target coords are known)."""
    hits: list[int] = []

    def _cb(hwnd: int, _: object) -> bool:
        cls = win32gui.GetClassName(hwnd)
        if cls in ("Chrome_RenderWidgetHostHWND", "CefBrowserWindow"):
            hits.append(hwnd)
        return True

    try:
        win32gui.EnumChildWindows(parent, _cb, None)
    except Exception:
        pass
    return hits[-1] if hits else parent


# ── Low-level message helpers ─────────────────────────────────────────────

def _wm_type(hwnd: int, text: str, delay: float = 0.01) -> None:
    """
    Deliver each character via WM_CHAR.
    WM_CHAR passes the Unicode codepoint directly to the renderer —
    no keyboard state (GetKeyState) is consulted, so no Ctrl/Shift confusion.
    """
    for ch in text:
        win32api.PostMessage(hwnd, win32con.WM_CHAR, ord(ch), 0)
        if delay:
            time.sleep(delay)


def _wm_enter(hwnd: int) -> None:
    """Send the Enter key via WM_KEYDOWN + WM_KEYUP (triggers 'send', not newline)."""
    win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, _VK_RETURN, _lp_down(_VK_RETURN))
    win32api.PostMessage(hwnd, win32con.WM_KEYUP,   _VK_RETURN, _lp_up(_VK_RETURN))


def _key_down(vk: int) -> None:
    win32api.keybd_event(vk, 0, 0, 0)


def _key_up(vk: int) -> None:
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def _foreground_key(vk: int, delay: float = 0.04) -> None:
    _key_down(vk)
    time.sleep(delay)
    _key_up(vk)


def _foreground_hotkey(*vks: int, delay: float = 0.04) -> None:
    for vk in vks:
        _key_down(vk)
        time.sleep(delay)
    for vk in reversed(vks):
        _key_up(vk)
        time.sleep(delay)


def _ensure_foreground(hwnd: int) -> None:
    if not hwnd:
        return
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        elif not win32gui.IsWindowVisible(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.12)


def _foreground_click(hwnd: int, x_screen: int, y_screen: int, pause_after: float = 0.12) -> None:
    _ensure_foreground(hwnd)
    win32api.SetCursorPos((int(x_screen), int(y_screen)))
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(pause_after)


def _foreground_paste_text(text: str) -> None:
    try:
        previous = pyperclip.paste()
    except Exception:
        previous = None
    pyperclip.copy(text)
    time.sleep(0.06)
    _foreground_hotkey(win32con.VK_CONTROL, ord("V"))
    time.sleep(0.10)
    if previous is not None:
        try:
            pyperclip.copy(previous)
        except Exception:
            pass


def _mk_lparam(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def _unique_hwnds(hwnds: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for hwnd in hwnds:
        if hwnd and hwnd not in seen and win32gui.IsWindow(hwnd):
            seen.add(hwnd)
            out.append(hwnd)
    return out


def _window_process_id(hwnd: int) -> int:
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid)
    except Exception:
        return 0


def _same_process_window(a: int, b: int) -> bool:
    pa = _window_process_id(a)
    pb = _window_process_id(b)
    return bool(pa and pb and pa == pb)


def _post_click(hwnd: int, x_screen: int, y_screen: int) -> None:
    """Post WM_LBUTTONDOWN/UP in the client coordinates of *hwnd*."""
    sl, st = win32gui.ClientToScreen(hwnd, (0, 0))
    lp = _mk_lparam(x_screen - sl, y_screen - st)
    win32api.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.05)
    win32api.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)


def _post_mouse_move(hwnd: int, x_screen: int, y_screen: int) -> None:
    """Post WM_MOUSEMOVE in the client coordinates of *hwnd*."""
    sl, st = win32gui.ClientToScreen(hwnd, (0, 0))
    lp = _mk_lparam(x_screen - sl, y_screen - st)
    win32api.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp)


def _post_mouse_wheel(hwnd: int, x_screen: int, y_screen: int, delta: int) -> None:
    """Post WM_MOUSEWHEEL at a screen point inside *hwnd*."""
    # WM_MOUSEWHEEL is different from button messages: lParam is in screen
    # coordinates, not the recipient window's client coordinates.
    lp = _mk_lparam(x_screen, y_screen)
    wp = (delta & 0xFFFF) << 16
    win32api.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wp, lp)


def _send_mouse_wheel(hwnd: int, x_screen: int, y_screen: int, delta: int) -> None:
    """Synchronously send WM_MOUSEWHEEL at a screen point inside *hwnd*."""
    lp = _mk_lparam(x_screen, y_screen)
    wp = (delta & 0xFFFF) << 16
    try:
        win32gui.SendMessageTimeout(
            hwnd,
            win32con.WM_MOUSEWHEEL,
            wp,
            lp,
            win32con.SMTO_ABORTIFHUNG,
            200,
        )
    except Exception:
        try:
            win32api.SendMessage(hwnd, win32con.WM_MOUSEWHEEL, wp, lp)
        except Exception:
            pass


def _wheel_targets(parent: int, x_screen: int, y_screen: int) -> list[int]:
    """Return likely WeWork windows that can consume wheel messages."""
    cef = _find_cef_hwnd_at(parent, x_screen, y_screen)
    under_point = 0
    try:
        under_point = win32gui.WindowFromPoint((x_screen, y_screen))
    except Exception:
        pass

    parents: list[int] = []
    cur = cef
    for _ in range(4):
        try:
            cur = win32gui.GetParent(cur)
        except Exception:
            cur = 0
        if not cur:
            break
        parents.append(cur)
        if cur == parent:
            break
    return _unique_hwnds([under_point, cef, *parents, parent])


# ── Public API ─────────────────────────────────────────────────────────────

def search_and_open_contact(
    name: str,
    pause_after_enter: float = 1.0,
    hwnd: int | None = None,
    search_rx: float = 0.155,
    search_ry: float = 0.046,
) -> None:
    """Open 企业微信 search, type name, press Enter."""
    hwnd = hwnd or win32gui.GetForegroundWindow()
    left, top, right, bottom = window_client_rect_screen(hwnd)
    w = right - left
    h = bottom - top
    x = int(left + w * search_rx)
    y = int(top + h * search_ry)
    cef = _find_cef_hwnd_at(hwnd, x, y)
    if cef == hwnd:
        _ensure_foreground(hwnd)
        _foreground_hotkey(win32con.VK_CONTROL, _VK_F)
        time.sleep(0.20)
        _foreground_hotkey(win32con.VK_CONTROL, ord("A"))
        _foreground_paste_text(name)
        time.sleep(0.35)
        _foreground_key(_VK_RETURN)
        time.sleep(pause_after_enter)
        return

    _post_click(cef, x, y)
    time.sleep(0.15)
    try:
        win32api.SendMessage(cef, win32con.WM_SETFOCUS, 0, 0)
    except Exception:
        pass
    time.sleep(0.10)

    # Select existing search text if any. CEF accepts WM_CHAR control codes
    # more reliably than synthetic Ctrl+A key state via PostMessage.
    win32api.PostMessage(cef, win32con.WM_CHAR, 0x01, 0)
    time.sleep(0.05)

    _wm_type(cef, name, delay=0.03)
    time.sleep(0.3)
    _wm_enter(cef)
    time.sleep(pause_after_enter)


def click_screen_point(hwnd: int, x_screen: int, y_screen: int, pause_after: float = 0.12) -> None:
    """Left-click a screen pixel inside the WeWork CEF tree (no SendInput)."""
    cef = _find_cef_hwnd_at(hwnd, x_screen, y_screen)
    targets: list[int] = []
    try:
        targets.append(win32gui.WindowFromPoint((x_screen, y_screen)))
    except Exception:
        pass
    targets.extend([cef, hwnd])
    for target in _unique_hwnds(targets):
        _post_click(target, x_screen, y_screen)
        time.sleep(0.03)
    time.sleep(pause_after)
    try:
        win32api.SendMessage(cef, win32con.WM_SETFOCUS, 0, 0)
    except Exception:
        pass
    time.sleep(0.04)


def click_screen_point_foreground(hwnd: int, x_screen: int, y_screen: int, pause_after: float = 0.12) -> None:
    """
    Real foreground click at a screen point.

    This is for flows where WeWork only reacts to the actual cursor. It refuses
    to click unless the requested WeWork main window is already foreground.
    Mouse movement is animated in steps so the action is visually obvious.
    """
    fg = 0
    try:
        fg = win32gui.GetForegroundWindow()
        fg_root = win32gui.GetAncestor(fg, _GA_ROOT)
    except Exception:
        fg_root = 0
    if fg_root != hwnd and not _same_process_window(fg_root or fg, hwnd):
        try:
            cls = win32gui.GetClassName(fg_root or fg)
            title = win32gui.GetWindowText(fg_root or fg)
        except Exception:
            cls = title = "unknown"
        raise RuntimeError(
            "WeWork is not the foreground window; refusing real mouse click "
            f"at ({x_screen},{y_screen}). foreground class={cls!r} title={title!r}"
        )

    left, top, right, bottom = window_client_rect_screen(hwnd)
    if not (left <= x_screen <= right and top <= y_screen <= bottom):
        raise RuntimeError(
            f"Refusing click outside WeWork client area: point=({x_screen},{y_screen}) "
            f"client=({left},{top},{right},{bottom})"
        )

    # 分段移动鼠标，让动作肉眼可见
    try:
        cur_x, cur_y = win32api.GetCursorPos()
    except Exception:
        cur_x, cur_y = x_screen, y_screen
    steps = 8
    for i in range(1, steps + 1):
        ix = int(cur_x + (x_screen - cur_x) * i / steps)
        iy = int(cur_y + (y_screen - cur_y) * i / steps)
        win32api.SetCursorPos((ix, iy))
        time.sleep(0.018)

    win32api.SetCursorPos((int(x_screen), int(y_screen)))
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(pause_after)


def move_screen_point(hwnd: int, x_screen: int, y_screen: int, pause_after: float = 0.45) -> None:
    """Move the CEF mouse cursor via WM_MOUSEMOVE (no SendInput)."""
    cef = _find_cef_hwnd_at(hwnd, x_screen, y_screen)
    for target in _unique_hwnds([cef, hwnd]):
        _post_mouse_move(target, x_screen, y_screen)
    time.sleep(pause_after)


def click_client_ratio(hwnd: int, rx: float, ry: float, pause_after: float = 0.12) -> None:
    """
    Left-click a point given as fractions (0~1) of the *main* WeWorkWindow
    client area (same coordinate space as window_client_rect_screen).

    Resolves the CEF child under the point and posts WM_LBUTTONDOWN/UP there.
    """
    left, top, right, bottom = window_client_rect_screen(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        raise RuntimeError("Invalid window rect")
    x = int(left + w * rx)
    y = int(top + h * ry)
    cef = _find_cef_hwnd_at(hwnd, x, y)
    _post_click(cef, x, y)
    time.sleep(pause_after)
    try:
        win32api.SendMessage(cef, win32con.WM_SETFOCUS, 0, 0)
    except Exception:
        pass
    time.sleep(0.04)


def scroll_client_ratio(
    hwnd: int,
    rx: float,
    ry: float,
    clicks: int,
    pause_after: float = 0.20,
    wheel_delta: int = 120,
    fanout: bool = True,
) -> None:
    """
    Scroll at a point given as fractions of the main WeWorkWindow client area.

    Positive *clicks* scrolls up; negative scrolls down.
    """
    left, top, right, bottom = window_client_rect_screen(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        raise RuntimeError("Invalid window rect")
    x = int(left + w * rx)
    y = int(top + h * ry)
    cef = _find_cef_hwnd_at(hwnd, x, y)
    _post_click(cef, x, y)
    time.sleep(0.05)
    step = abs(int(wheel_delta)) if clicks > 0 else -abs(int(wheel_delta))
    targets = _wheel_targets(hwnd, x, y)
    if not fanout:
        targets = targets[:1]
    for _ in range(abs(int(clicks))):
        for target in targets:
            if fanout:
                _send_mouse_wheel(target, x, y, step)
            _post_mouse_wheel(target, x, y, step)
        time.sleep(0.03)
    time.sleep(pause_after)


def press_enter_at_screen(hwnd: int, x_screen: int, y_screen: int) -> None:
    """Press Enter at the CEF widget under (x_screen, y_screen)."""
    cef = _find_cef_hwnd_at(hwnd, x_screen, y_screen)
    _wm_enter(cef)


def type_text_at_screen(
    hwnd: int,
    x_screen: int,
    y_screen: int,
    text: str,
    char_delay: float = 0.02,
    click_first: bool = True,
) -> None:
    """Focus CEF under (x_screen,y_screen), optionally click, then WM_CHAR *text*."""
    cef = _find_cef_hwnd_at(hwnd, x_screen, y_screen)
    if click_first:
        _post_click(cef, x_screen, y_screen)
        time.sleep(0.1)
        win32api.SendMessage(cef, win32con.WM_SETFOCUS, 0, 0)
        time.sleep(0.05)
    _wm_type(cef, text, delay=char_delay)


def type_text_client_ratio(
    hwnd: int,
    rx: float,
    ry: float,
    text: str,
    char_delay: float = 0.02,
    click_first: bool = True,
) -> None:
    """
    Focus the CEF widget under (rx, ry), optionally click there first,
    then send *text* via WM_CHAR (no trailing Enter).
    """
    left, top, right, bottom = window_client_rect_screen(hwnd)
    w, h = right - left, bottom - top
    x = int(left + w * rx)
    y = int(top + h * ry)
    cef = _find_cef_hwnd_at(hwnd, x, y)
    if click_first:
        _post_click(cef, x, y)
        time.sleep(0.1)
        win32api.SendMessage(cef, win32con.WM_SETFOCUS, 0, 0)
        time.sleep(0.05)
    _wm_type(cef, text, delay=char_delay)


def click_message_composer(hwnd: int, from_bottom_ratio: float = 0.10) -> None:
    """Click the message input area via PostMessage."""
    left, top, right, bottom = window_client_rect_screen(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        raise RuntimeError("Invalid window rect")

    x = int(left + w * 0.52)
    y = int(bottom - max(24, h * from_bottom_ratio))

    cef = _find_cef_hwnd_at(hwnd, x, y)
    if cef == hwnd:
        _foreground_click(hwnd, x, y, pause_after=0.15)
        return
    _post_click(cef, x, y)
    # Give the renderer time to process focus change
    time.sleep(0.15)
    # Nudge CEF input focus explicitly
    win32api.SendMessage(cef, win32con.WM_SETFOCUS, 0, 0)
    time.sleep(0.05)


def paste_and_send(text: str, hwnd: int | None = None, from_bottom_ratio: float = 0.10) -> None:
    """
    Type *text* into the focused composer via WM_CHAR, then send with Enter.

    WM_PASTE does NOT work with CEF: the Chromium renderer ignores that
    clipboard message.  WM_CHAR delivers the Unicode codepoint directly to
    the active input handler without consulting GetKeyState(), so it works
    regardless of modifier state and without SendInput / LLMHF_INJECTED.
    """
    hwnd = hwnd or win32gui.GetForegroundWindow()

    # Recalculate the composer position so we target the right CEF child
    left = top = right = bottom = 0
    try:
        left, top, right, bottom = window_client_rect_screen(hwnd)
        w, h = right - left, bottom - top
        x = int(left + w * 0.52)
        y = int(bottom - max(24, h * from_bottom_ratio))
        cef = _find_cef_hwnd_at(hwnd, x, y)
    except Exception:
        cef = _find_cef_hwnd(hwnd)
    if cef == hwnd:
        if left and right and bottom:
            _foreground_click(hwnd, x, y, pause_after=0.15)
        else:
            _ensure_foreground(hwnd)
        _foreground_paste_text(text)
        time.sleep(0.12)
        _foreground_key(_VK_RETURN)
        return

    # Ensure input focus is on the composer widget
    win32api.SendMessage(cef, win32con.WM_SETFOCUS, 0, 0)
    time.sleep(0.05)

    # Type the reply text character by character
    _wm_type(cef, text, delay=0.01)
    time.sleep(0.08)

    # Enter key to send (WM_KEYDOWN, not WM_CHAR, so WeWork sees a key press)
    _wm_enter(cef)
