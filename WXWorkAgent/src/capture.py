"""Capture a sub-rectangle of a window.

Default capture uses Win32 PrintWindow so WeWork can be OCR'ed while another
window, such as the Douyin Cloak browser, is in front.  mss remains available
as a screen-capture fallback and for screen-coordinate calculations.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import ctypes
from dataclasses import dataclass
from pathlib import Path

import mss
import mss.tools
from PIL import Image
import win32ui
import win32gui


@dataclass
class RegionRatios:
    left: float = 0.33
    top: float = 0.12
    width: float = 0.62
    height: float = 0.55

    def __post_init__(self) -> None:
        for name, v in [
            ("left", self.left),
            ("top", self.top),
            ("width", self.width),
            ("height", self.height),
        ]:
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"{name} must be in [0,1], got {v}")
        if self.left + self.width > 1.001:
            raise ValueError(f"left({self.left}) + width({self.width}) > 1.0, would crop outside window")
        if self.top + self.height > 1.001:
            raise ValueError(f"top({self.top}) + height({self.height}) > 1.0, would crop outside window")


def window_client_rect_screen(hwnd: int) -> tuple[int, int, int, int]:
    """
    主窗口客户区在屏幕上的物理像素范围 (left, top, right, bottom)。

    依赖进程已通过 ocr_runtime.init_ocr_runtime() / bootstrap_env.apply() 声明 Per-Monitor DPI Aware。
    在 DPI-aware 进程中 GetClientRect / ClientToScreen 返回物理像素，
    与 mss 截图坐标系一致；若进程未声明 DPI-aware，高缩放比下会偏移。
    """
    l, t, r, b = win32gui.GetClientRect(hwnd)
    sl, st = win32gui.ClientToScreen(hwnd, (l, t))
    sr, sb = win32gui.ClientToScreen(hwnd, (r, b))
    return sl, st, sr, sb


def _window_frame_rect_screen(hwnd: int) -> tuple[int, int, int, int]:
    return win32gui.GetWindowRect(hwnd)


def _effective_capture_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Return a window rectangle in the coordinate space used by mss.

    On some Windows remote/display setups, declaring DPI awareness makes Win32
    return physical coordinates while mss still captures the scaled virtual
    desktop.  mss will happily grab any requested width/height, so comparing the
    returned screenshot size to the requested region is not a valid test.  Clamp
    oversized Win32 rectangles back into the mss monitor coordinate space.
    """
    client = window_client_rect_screen(hwnd)
    frame = _window_frame_rect_screen(hwnd)
    with mss.mss() as sct:
        monitor = sct.monitors[0]
    mx = int(monitor.get("left", 0))
    my = int(monitor.get("top", 0))
    mw = int(monitor.get("width", 0))
    mh = int(monitor.get("height", 0))
    mon_right = mx + max(1, mw)
    mon_bottom = my + max(1, mh)

    def _fits(rect: tuple[int, int, int, int]) -> bool:
        left, top, right, bottom = rect
        return right > left and bottom > top and left >= mx - 8 and top >= my - 8 and right <= mon_right + 8 and bottom <= mon_bottom + 8

    if _fits(client):
        return client
    if _fits(frame):
        return frame

    left, top, right, bottom = client
    width = max(1, right - left)
    height = max(1, bottom - top)
    scale = max(width / max(1, mw), height / max(1, mh), 1.0)
    scaled = (
        int(round(left / scale)),
        int(round(top / scale)),
        int(round(right / scale)),
        int(round(bottom / scale)),
    )
    if _fits(scaled):
        return scaled

    # Last-resort clamp keeps captures inside the visible virtual desktop
    # instead of silently sampling unrelated off-screen/desktop pixels.
    return (
        max(mx, min(mon_right - 1, scaled[0])),
        max(my, min(mon_bottom - 1, scaled[1])),
        max(mx + 1, min(mon_right, scaled[2])),
        max(my + 1, min(mon_bottom, scaled[3])),
    )


def mss_region_for_ratios(hwnd: int, ratios: RegionRatios) -> dict[str, int]:
    """返回传给 mss.grab 的 region 字典（屏幕物理坐标），便于打日志核对。"""
    left, top, right, bottom = _effective_capture_rect(hwnd)
    w = right - left
    h = bottom - top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"Client rect invalid: w={w} h={h}")
    return {
        "left":   int(left + w * ratios.left),
        "top":    int(top  + h * ratios.top),
        "width":  max(1, int(w * ratios.width)),
        "height": max(1, int(h * ratios.height)),
    }


def pixel_hash(shot) -> str:
    """
    SHA-256 of raw RGB bytes from an mss screenshot.

    比对 PNG 文件字节不可靠：zlib 压缩每次结果不同，导致
    画面完全没变时 hash 也不一致。对原始像素 hash 则确定性稳定。
    """
    return hashlib.sha256(bytes(shot.rgb)).hexdigest()


def _image_pixel_hash(img: Image.Image) -> str:
    return hashlib.sha256(img.convert("RGB").tobytes()).hexdigest()


def _ratio_crop_box(width: int, height: int, ratios: RegionRatios) -> tuple[int, int, int, int]:
    left = int(width * ratios.left)
    top = int(height * ratios.top)
    right = left + max(1, int(width * ratios.width))
    bottom = top + max(1, int(height * ratios.height))
    return (
        max(0, min(width - 1, left)),
        max(0, min(height - 1, top)),
        max(1, min(width, right)),
        max(1, min(height, bottom)),
    )


def _capture_window_frame_printwindow(hwnd: int) -> tuple[Image.Image, tuple[int, int, int, int]]:
    frame = _window_frame_rect_screen(hwnd)
    left, top, right, bottom = frame
    width = max(1, int(right - left))
    height = max(1, int(bottom - top))

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    if not hwnd_dc:
        raise RuntimeError("GetWindowDC failed")
    src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    mem_dc = src_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(src_dc, width, height)
    old_obj = mem_dc.SelectObject(bitmap)
    try:
        ok = ctypes.windll.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), 2)
        if not ok:
            ok = ctypes.windll.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), 0)
        if not ok:
            raise RuntimeError("PrintWindow failed")
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB",
            (int(info["bmWidth"]), int(info["bmHeight"])),
            bits,
            "raw",
            "BGRX",
            0,
            1,
        ).copy()
        return img, frame
    finally:
        try:
            mem_dc.SelectObject(old_obj)
        except Exception:
            pass
        win32gui.DeleteObject(bitmap.GetHandle())
        mem_dc.DeleteDC()
        src_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def _capture_window_effective_printwindow(hwnd: int) -> Image.Image:
    img, _frame = _capture_window_frame_printwindow(hwnd)
    return img


def _capture_region_printwindow(hwnd: int, ratios: RegionRatios) -> Image.Image:
    img = _capture_window_effective_printwindow(hwnd)
    return img.crop(_ratio_crop_box(img.width, img.height, ratios))


def _capture_region_mss(hwnd: int, ratios: RegionRatios):
    region = mss_region_for_ratios(hwnd, ratios)
    with mss.mss() as sct:
        return sct.grab(region)


def _capture_region_image(hwnd: int, ratios: RegionRatios) -> Image.Image:
    backend = os.environ.get("WXWORK_CAPTURE_BACKEND", "printwindow").strip().lower()
    if backend not in {"mss", "screen"}:
        try:
            return _capture_region_printwindow(hwnd, ratios)
        except Exception:
            if backend in {"printwindow", "win32"}:
                raise
    shot = _capture_region_mss(hwnd, ratios)
    return Image.frombytes("RGB", shot.size, shot.rgb)


def capture_window_region_png(hwnd: int, ratios: RegionRatios, out_path: Path) -> Path:
    """Grab window region and save as PNG; returns path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = _capture_region_image(hwnd, ratios)
    img.save(out_path)
    return out_path


def capture_window_region_hash(hwnd: int, ratios: RegionRatios) -> str:
    """
    Grab window region and return a raw-pixel hash without writing a PNG.

    Use this for polling/change detection. PNG encoding and temp-file churn are
    only needed when the caller is about to run OCR or keep a debug image.
    """
    img = _capture_region_image(hwnd, ratios)
    return _image_pixel_hash(img)


def capture_window_region_temp_png_meta(
    hwnd: int, ratios: RegionRatios
) -> tuple[Path, int, int, str]:
    """
    Same as capture_window_region_temp_png but also returns PNG width/height
    (matches mss grab size, for OCR box → screen mapping).
    """
    fd, name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    path = Path(name)
    try:
        img = _capture_region_image(hwnd, ratios)
        digest = _image_pixel_hash(img)
        img.save(path)
        return path, img.width, img.height, digest
    except Exception:
        path.unlink(missing_ok=True)
        raise


def capture_window_region_temp_png(hwnd: int, ratios: RegionRatios) -> tuple[Path, str]:
    """
    Grab window region, write a temp PNG, and return (path, pixel_hash).

    - Caller must unlink the path when done.
    - Hash is over raw RGB pixels (not PNG file bytes), so identical frames
      always produce the same hash regardless of compression entropy.
    """
    path, _w, _h, digest = capture_window_region_temp_png_meta(hwnd, ratios)
    return path, digest
