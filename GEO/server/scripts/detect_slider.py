#!/usr/bin/env python3
"""Detect Kimi-style slider captcha: puzzle piece on the left, gap to the right."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np


def find_puzzle_roi(bgr: np.ndarray) -> tuple[int, int, int, int]:
    """Find the landscape photo inside the captcha modal."""
    h, w = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    # white modal
    white = cv2.inRange(hsv, (0, 0, 210), (180, 40, 255))
    white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    cnts, _ = cv2.findContours(white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    modal = None
    for c in cnts:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < w * 0.25 or ch < h * 0.25:
            continue
        if modal is None or cw * ch > modal[2] * modal[3]:
            modal = (x, y, cw, ch)
    if modal is None:
        mx, my, mw, mh = int(w * 0.28), int(h * 0.12), int(w * 0.44), int(h * 0.55)
    else:
        mx, my, mw, mh = modal

    crop = bgr[my : my + mh, mx : mx + mw]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    # photo is more textured / colorful than the white chrome
    sat = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[:, :, 1]
    mask = ((sat > 18) | (cv2.Laplacian(gray, cv2.CV_16S).astype(np.int16).__abs__() > 12)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in cnts:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < mw * 0.55 or ch < mh * 0.35 or ch > mh * 0.85:
            continue
        if best is None or cw * ch > best[2] * best[3]:
            best = (x, y, cw, ch)
    if best is None:
        return mx + 8, my + 36, mw - 16, int(mh * 0.62)
    return mx + best[0], my + best[1], best[2], best[3]


def match_gap(gray: np.ndarray) -> tuple[int, int, int, int, int, int, float]:
    """Left piece via edge density; gap via edge template match on the right."""
    h, w = gray.shape
    left_w = max(56, w // 3)
    left = gray[:, :left_w]
    edges = cv2.Canny(cv2.GaussianBlur(left, (5, 5), 0), 50, 140)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    # piece sits over water: prefer lower-middle of the photo
    heat = cv2.blur(edges.astype(np.float32), (21, 21))
    _, _, _, max_loc = cv2.minMaxLoc(heat)
    cx, cy = int(max_loc[0]), int(max_loc[1])
    tw, th = 78, 78
    x0 = max(0, min(left_w - tw, cx - tw // 2))
    y0 = max(0, min(h - th, cy - th // 2))

    tmpl = gray[y0 : y0 + th, x0 : x0 + tw]
    t_edge = cv2.Canny(cv2.GaussianBlur(tmpl, (3, 3), 0), 40, 120)
    search_x = x0 + tw // 2
    search = gray[:, search_x:]
    s_edge = cv2.Canny(cv2.GaussianBlur(search, (3, 3), 0), 40, 120)
    res = cv2.matchTemplate(s_edge, t_edge, cv2.TM_CCOEFF_NORMED)
    # ignore the piece itself (leftmost peak)
    if res.size == 0:
        raise RuntimeError("模板匹配失败")
    res[:, : max(1, tw // 2)] = -1
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    gx = search_x + int(max_loc[0])
    gy = int(max_loc[1])
    return x0, y0, gx, gy, tw, th, float(max_val)


def detect(path: Path, out_dir: Path) -> dict:
    bgr = cv2.imread(str(path))
    if bgr is None:
        raise RuntimeError(f"读不了图: {path}")
    H, W = bgr.shape[:2]
    rx, ry, rw, rh = find_puzzle_roi(bgr)
    roi = bgr[ry : ry + rh, rx : rx + rw]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    x0, y0, gx, gy, tw, th, score = match_gap(gray)

    piece_cx = rx + x0 + tw / 2
    gap_cx = rx + gx + tw / 2
    piece_cy = ry + y0 + th / 2
    gap_cy = ry + gy + th / 2
    dx = gap_cx - piece_cx

    vis = bgr.copy()
    cv2.rectangle(vis, (rx, ry), (rx + rw, ry + rh), (80, 180, 255), 2)
    cv2.rectangle(vis, (rx + x0, ry + y0), (rx + x0 + tw, ry + y0 + th), (0, 220, 0), 2)
    cv2.rectangle(vis, (rx + gx, ry + gy), (rx + gx + tw, ry + gy + th), (0, 0, 255), 2)
    cv2.line(
        vis,
        (int(piece_cx), int(piece_cy)),
        (int(gap_cx), int(gap_cy)),
        (0, 255, 255),
        2,
    )
    cv2.putText(
        vis,
        f"dx={dx:.1f}px score={score:.3f}",
        (rx, max(24, ry - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 255),
        2,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    vis_path = out_dir / "slider_detect.png"
    roi_path = out_dir / "slider_roi.png"
    cv2.imwrite(str(vis_path), vis)
    cv2.imwrite(str(roi_path), roi)

    result = {
        "image": str(path),
        "viewport": {"w": W, "h": H},
        "roi": {"x": rx, "y": ry, "w": rw, "h": rh},
        "piece": {"x": rx + x0, "y": ry + y0, "w": tw, "h": th, "cx": piece_cx, "cy": piece_cy},
        "gap": {"x": rx + gx, "y": ry + gy, "w": tw, "h": th, "cx": gap_cx, "cy": gap_cy},
        "dx_px": round(dx, 2),
        "score": round(score, 4),
        "drag": {
            "from_nx": round(piece_cx / W, 4),
            "from_ny": round((ry + rh + 28) / H, 4),
            "to_nx": round((piece_cx + dx) / W, 4),
            "to_ny": round((ry + rh + 28) / H, 4),
            "note": "滑块按钮大约在拼图正下方，水平拖 dx 像素",
        },
        "vis": str(vis_path),
    }
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="检测滑块验证码缺口位移")
    ap.add_argument("image", nargs="?", default="/tmp/kimi-slider.png")
    ap.add_argument("-o", "--out", default="/tmp/slider-detect")
    args = ap.parse_args()
    info = detect(Path(args.image), Path(args.out))
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
