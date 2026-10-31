"""Stable screenshot capture followed by a single OCR pass."""
from __future__ import annotations

import shutil
from pathlib import Path

from src.capture import RegionRatios, capture_window_region_hash, capture_window_region_temp_png
from src.ocr_client import ocr_image_file
from src.wait_util import sleep_until_hash_stable


def ocr_stable_region(
    hwnd: int,
    ratios: RegionRatios,
    *,
    stable_samples: int = 2,
    stable_sleep_sec: float = 0.30,
    timeout_sec: float = 8.0,
    keep_path: Path | None = None,
) -> tuple[str, str]:
    """
    Wait for the region pixels to settle, then OCR exactly one captured image.

    This avoids the expensive old pattern of running OCR repeatedly until the
    recognised text is byte-for-byte identical.
    """
    h = sleep_until_hash_stable(
        lambda: capture_window_region_hash(hwnd, ratios),
        samples=stable_samples,
        interval_sec=stable_sleep_sec,
        timeout_sec=timeout_sec,
    )
    path, h2 = capture_window_region_temp_png(hwnd, ratios)
    try:
        text = ocr_image_file(path)
        if keep_path is not None:
            keep_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, keep_path)
    finally:
        if keep_path is None:
            path.unlink(missing_ok=True)
    return text, h2 or h
