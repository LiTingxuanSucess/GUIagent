"""Probe WeWork scroll behavior with screenshot-hash verification."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from src.ocr_runtime import init_ocr_runtime

init_ocr_runtime()

from src.actions import click_client_ratio, scroll_client_ratio
from src.capture import RegionRatios, capture_window_region_hash, capture_window_region_png
from src.window import activate_hwnd, ensure_wework_hwnd, is_same_process_foreground

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


REGIONS = {
    "list": RegionRatios(left=0.065, top=0.08, width=0.235, height=0.86),
    "chat": RegionRatios(left=0.31, top=0.10, width=0.67, height=0.64),
}

POINTS = {
    "list": (0.18, 0.50),
    "chat": (0.66, 0.40),
}


def _probe(hwnd: int, area: str, clicks: int, out_dir: Path, rounds: int) -> bool:
    ratios = REGIONS[area]
    rx, ry = POINTS[area]
    changed_any = False
    for i in range(rounds):
        before_png = out_dir / f"{area}_{i:02d}_before.png"
        after_png = out_dir / f"{area}_{i:02d}_after.png"
        before = capture_window_region_hash(hwnd, ratios)
        capture_window_region_png(hwnd, ratios, before_png)
        scroll_client_ratio(hwnd, rx, ry, clicks=clicks, pause_after=0.55)
        after = capture_window_region_hash(hwnd, ratios)
        capture_window_region_png(hwnd, ratios, after_png)
        changed = before != after
        changed_any = changed_any or changed
        print(
            f">>> {area} round {i + 1}/{rounds}: clicks={clicks}, changed={changed}",
            flush=True,
        )
        if not changed:
            time.sleep(0.25)
    return changed_any


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe WeWork list/chat scrolling")
    parser.add_argument("--area", choices=["list", "chat", "both"], default="both")
    parser.add_argument("--list-clicks", type=int, default=-6)
    parser.add_argument("--chat-clicks", type=int, default=8)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--out-dir", type=Path, default=Path("exports") / "scroll_probe")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    hwnd = ensure_wework_hwnd()
    if not hwnd:
        print(">>> WeWork window not found", flush=True)
        return 2

    activate_hwnd(hwnd)
    time.sleep(0.5)
    if not is_same_process_foreground(hwnd):
        print(">>> WeWork is not foreground; continuing with direct HWND messages", flush=True)

    ok = True
    if args.area in ("list", "both"):
        click_client_ratio(hwnd, 0.18, 0.50, pause_after=0.2)
        ok = _probe(hwnd, "list", args.list_clicks, args.out_dir, args.rounds) and ok
    if args.area in ("chat", "both"):
        click_client_ratio(hwnd, 0.16, 0.27, pause_after=0.8)
        click_client_ratio(hwnd, 0.66, 0.40, pause_after=0.2)
        ok = _probe(hwnd, "chat", args.chat_clicks, args.out_dir, args.rounds) and ok
    print(f">>> scroll probe changed_any={ok}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
