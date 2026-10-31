"""Locate and activate 企业微信 main window (WeWorkWindow)."""
from __future__ import annotations

import os
import subprocess
import time
import win32api
import win32con
import win32gui
import win32process


def _rect_area(rect: tuple[int, int, int, int]) -> int:
    left, top, right, bottom = rect
    return max(0, right - left) * max(0, bottom - top)


def find_wework_hwnd() -> int:
    """
    Return HWND of the main WeWorkWindow.

    Includes windows that are **not** currently visible — e.g. main UI hidden
    while only a tray icon is shown (IsWindowVisible may be False).  We still
    prefer a visible, non-zero-area window when multiple handles exist.
    """
    found: list[int] = []

    def _cb(hwnd: int, _: object) -> bool:
        try:
            if win32gui.GetClassName(hwnd) != "WeWorkWindow":
                return True
            if not win32gui.IsWindow(hwnd):
                return True
            found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    if not found:
        return 0

    def score(h: int) -> tuple[int, int, int]:
        try:
            vis = 1 if win32gui.IsWindowVisible(h) else 0
            iconic = 1 if win32gui.IsIconic(h) else 0
            area = _rect_area(win32gui.GetWindowRect(h))
        except Exception:
            return (0, 0, 0)
        # Prefer visible, non-minimised, then largest area
        return (vis, -iconic, area)

    return max(found, key=score)


def _wework_exe_candidates() -> list[str]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    la = os.environ.get("LOCALAPPDATA", "")
    return [
        os.path.join(pf, "WXWork", "WXWork.exe"),
        os.path.join(pf86, "WXWork", "WXWork.exe"),
        os.path.join(pf, "Tencent", "WXWork", "WXWork.exe"),
        os.path.join(pf86, "Tencent", "WXWork", "WXWork.exe"),
        os.path.join(la, "WXWork", "WXWork.exe") if la else "",
    ]


def try_launch_wework() -> bool:
    """
    Start 企业微信 if an install path is found.

    A second launch usually **wakes the existing process** and shows the main
    window when the app was only in the tray (no HWND visible to automation).
    """
    for exe in _wework_exe_candidates():
        if not exe or not os.path.isfile(exe):
            continue
        try:
            subprocess.Popen([exe], cwd=os.path.dirname(exe) or None)
            return True
        except OSError:
            continue
    return False


def ensure_wework_hwnd(
    *,
    launch_if_missing: bool = True,
    wait_sec: float = 12.0,
    poll_interval: float = 0.45,
) -> int:
    """
    find_wework_hwnd + optional WXWork.exe launch + poll until a handle appears.

    Use this entry point when the user may have closed the window to tray only.
    """
    hwnd = find_wework_hwnd()
    if hwnd:
        return hwnd

    if not launch_if_missing or not try_launch_wework():
        return 0

    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        hwnd = find_wework_hwnd()
        if hwnd:
            return hwnd
    return 0


def activate_hwnd(hwnd: int) -> None:
    """Restore + bring to foreground (steals keyboard focus). Use only when about to interact."""
    if not hwnd:
        raise RuntimeError("Invalid HWND")
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    elif not win32gui.IsWindowVisible(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        try:
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOP,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )
            left, top, right, _bottom = win32gui.GetWindowRect(hwnd)
            win32api.SetCursorPos((left + min(120, max(20, right - left - 20)), top + 18))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.03)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        except Exception:
            pass


def ensure_visible(hwnd: int) -> None:
    """
    Ensure the window is not minimised so mss can capture it, but do NOT steal
    keyboard focus.  Use this in the poll loop instead of activate_hwnd so the
    user can keep typing in other apps while the watcher runs.
    """
    if not hwnd:
        return
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    elif not win32gui.IsWindowVisible(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)


def window_process_id(hwnd: int) -> int:
    """Return the owning process id for *hwnd*, or 0 on failure."""
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid)
    except Exception:
        return 0


def is_same_process_foreground(hwnd: int) -> bool:
    """
    True when the foreground window belongs to the same process as *hwnd*.

    The check fails open so capture code does not suppress legitimate events
    if Windows refuses the query.
    """
    try:
        fg = win32gui.GetForegroundWindow()
        if fg == hwnd:
            return True
        fg_pid = window_process_id(fg)
        wx_pid = window_process_id(hwnd)
        return bool(fg_pid and wx_pid and fg_pid == wx_pid)
    except Exception:
        return True
