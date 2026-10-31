#!/usr/bin/env python3
"""Attach to the live Kimi tab and drag the NetEase Yidun slider.

Does not restart geo-server. Reads DevToolsActivePort from the Chrome profile.
"""

from __future__ import annotations

import base64
import json
import math
import random
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import websocket

ROOT = Path("/root/workspace/GUIAgent/GEO")
OUT = Path("/tmp/slider-detect")


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=20)
        self.n = 0

    def call(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result") or {}

    def eval(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"])
        return (r.get("result") or {}).get("value")

    def close(self):
        self.ws.close()


def find_kimi_ws(port: int) -> str:
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5))
    for t in tabs:
        if t.get("type") == "page" and "kimi.com" in (t.get("url") or ""):
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("找不到 Kimi 标签")


INFO_JS = r"""
(() => {
  const box = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height, src: el.src || '', left: el.style.left || ''};
  };
  const t = document.body.innerText || '';
  return {
    slider: box('.yidun_slider'),
    jigsaw: box('img.yidun_jigsaw'),
    bg: box('img.yidun_bg-img'),
    control: box('.yidun_control'),
    sms: /验证码已发送|重新发送|秒后/.test(t),
    has_yidun: !!document.querySelector('.yidun_slider'),
    tail: t.slice(-160)
  };
})()
"""

IMG_JS = r"""
(async () => {
  const toB64 = async (sel) => {
    const el = document.querySelector(sel);
    if (!el || !el.src) return null;
    const res = await fetch(el.src);
    const buf = await res.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  };
  return {bg: await toB64('img.yidun_bg-img'), piece: await toB64('img.yidun_jigsaw')};
})()
"""


def decode_img(b64: str) -> np.ndarray:
    raw = np.frombuffer(base64.b64decode(b64), np.uint8)
    img = cv2.imdecode(raw, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError("decode failed")
    return img


def detect_dx(bg: np.ndarray, piece: np.ndarray) -> dict:
    bg_bgr = bg[:, :, :3]
    if piece.ndim == 3 and piece.shape[2] == 4:
        alpha = piece[:, :, 3]
        pbgr = piece[:, :, :3]
    else:
        pbgr = piece[:, :, :3] if piece.ndim == 3 else cv2.cvtColor(piece, cv2.COLOR_GRAY2BGR)
        alpha = (cv2.cvtColor(pbgr, cv2.COLOR_BGR2GRAY) < 250).astype(np.uint8) * 255
    ys, xs = np.where(alpha > 40)
    px0, py0 = int(xs.min()), int(ys.min())
    px1, py1 = int(xs.max()) + 1, int(ys.max()) + 1
    tmpl = pbgr[py0:py1, px0:px1]
    tw, th = int(tmpl.shape[1]), int(tmpl.shape[0])
    gray = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY)
    bg_e = cv2.Canny(gray, 255, 255)
    tp_e = cv2.Canny(cv2.cvtColor(tmpl, cv2.COLOR_BGR2GRAY), 255, 255)
    res = cv2.matchTemplate(bg_e, tp_e, cv2.TM_CCOEFF_NORMED)
    res[:, : max(25, tw // 2)] = -1
    _, score, _, loc = cv2.minMaxLoc(res)
    gap_x = float(loc[0])
    vis = bg_bgr.copy()
    cv2.rectangle(vis, (int(loc[0]), int(loc[1])), (int(loc[0] + tw), int(loc[1] + th)), (255, 0, 0), 2)
    OUT.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(OUT / "yidun_match.png"), vis)
    return {"gap_x": gap_x, "px0": px0, "dx_img": gap_x - px0, "score": float(score)}


def mouse(cdp: CDP, typ: str, x: float, y: float, buttons: int = 0):
    p = {
        "type": typ,
        "x": round(x, 2),
        "y": round(y, 2),
        "button": "left",
        "buttons": buttons,
        "pointerType": "mouse",
    }
    if typ == "mousePressed":
        p["clickCount"] = 1
    cdp.call("Input.dispatchMouseEvent", **p)


def human_track(distance: float) -> list[float]:
    tracks = []
    current = 0.0
    mid = distance * 0.7
    t = 0.2
    v = 0.0
    while current < distance:
        a = random.uniform(1.6, 2.4) if current < mid else -random.uniform(2.2, 3.2)
        v0 = v
        v = v0 + a * t
        move = max(0.6, v0 * t + 0.5 * a * t * t)
        current += move
        tracks.append(min(current, distance))
    tracks.extend([distance + random.uniform(2, 5), distance + 1, distance])
    return tracks


def main() -> int:
    port = int((ROOT / ".chrome-cn-ai-profile" / "DevToolsActivePort").read_text().splitlines()[0])
    cdp = CDP(find_kimi_ws(port))
    try:
        cdp.call("Page.bringToFront")
        info = cdp.eval(INFO_JS)
        if not info or not info.get("has_yidun") or not info.get("slider"):
            print(json.dumps({"error": "当前没有易盾滑块", "info": info}, ensure_ascii=False, indent=2))
            return 2
        imgs = cdp.eval(IMG_JS)
        det = detect_dx(decode_img(imgs["bg"]), decode_img(imgs["piece"]))
        slider = info["slider"]
        dx_slider = det["dx_img"] * 1.117
        x0 = slider["x"] + slider["w"] / 2
        y0 = slider["y"] + slider["h"] / 2
        print(json.dumps({"detect": det, "dx_slider": dx_slider, "from": [x0, y0]}, ensure_ascii=False, indent=2))
        mouse(cdp, "mouseMoved", x0, y0, 0)
        time.sleep(0.12)
        mouse(cdp, "mousePressed", x0, y0, 1)
        time.sleep(0.1)
        for ox in human_track(dx_slider):
            mouse(cdp, "mouseMoved", x0 + ox, y0 + math.sin(ox / 18) * 1.2, 1)
            time.sleep(0.012 + random.uniform(0, 0.008))
        time.sleep(0.08)
        mouse(cdp, "mouseReleased", x0 + dx_slider, y0, 0)
        time.sleep(2.0)
        after = cdp.eval(INFO_JS)
        raw = cdp.call("Page.captureScreenshot", format="png")["data"]
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "after.png").write_bytes(base64.b64decode(raw))
        print(json.dumps({"after": after, "shot": str(OUT / "after.png")}, ensure_ascii=False, indent=2))
        return 0 if after.get("sms") or not after.get("has_yidun") else 3
    finally:
        cdp.close()


if __name__ == "__main__":
    raise SystemExit(main())
