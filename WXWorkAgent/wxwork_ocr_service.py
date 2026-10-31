"""
Resident PaddleOCR service for WXWorkAgent.

Run once in a separate terminal:
  py -3.11 wxwork_ocr_service.py

Business scripts call this local HTTP service instead of importing PaddleOCR.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw

from src.ocr_runtime import init_ocr_runtime

init_ocr_runtime()

from src.ocr_run import OcrTextBox
from src.ocr_run import _get_ocr, ocr_image_file, ocr_image_text_boxes

log = logging.getLogger("wxwork_ocr_service")
_predict_lock = threading.Lock()
_rapid_singleton = None


def _backend() -> str:
    return os.environ.get("WXWORK_OCR_BACKEND", "rapidocr").strip().lower()


def _max_side() -> int:
    return max(0, int(os.environ.get("WXWORK_OCR_MAX_SIDE", "960")))


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _warmup_image() -> Path:
    path = Path(tempfile.gettempdir()) / "wxwork_ocr_service_warmup.png"
    img = Image.new("RGB", (360, 96), "white")
    draw = ImageDraw.Draw(img)
    draw.text((16, 30), "WXWork OCR ready", fill="black")
    img.save(path)
    return path


def _prepare_image_for_ocr(image_path: Path) -> tuple[Path, float, float, bool]:
    """
    Downscale large screenshots before OCR.

    PaddleOCR text detection is the expensive step on large desktop captures.
    Returning scale factors lets box coordinates be mapped back to the original
    screenshot coordinate space.
    """
    max_side = _max_side()
    if max_side <= 0:
        return image_path, 1.0, 1.0, False

    with Image.open(image_path) as img:
        w, h = img.size
        long_side = max(w, h)
        if long_side <= max_side:
            return image_path, 1.0, 1.0, False

        scale = max_side / float(long_side)
        nw = max(1, int(w * scale))
        nh = max(1, int(h * scale))
        resized = img.convert("RGB").resize((nw, nh), Image.Resampling.BILINEAR)
        digest = hashlib.sha1(str(image_path).encode("utf-8", errors="ignore")).hexdigest()[:10]
        out = Path(tempfile.gettempdir()) / f"wxwork_ocr_{digest}_{w}x{h}_to_{nw}x{nh}.png"
        resized.save(out)
    return out, w / float(nw), h / float(nh), True


def _get_rapid():
    global _rapid_singleton
    if _rapid_singleton is None:
        from rapidocr_onnxruntime import RapidOCR

        _rapid_singleton = RapidOCR()
    return _rapid_singleton


def _rapid_items(image_path: Path) -> list[tuple[object, str, float]]:
    engine = _get_rapid()
    result, _elapsed = engine(str(image_path))
    items: list[tuple[object, str, float]] = []
    for row in result or []:
        if not row or len(row) < 2:
            continue
        poly = row[0]
        text = str(row[1]).strip()
        if not text:
            continue
        try:
            y = sum(float(p[1]) for p in poly) / max(len(poly), 1)
        except Exception:
            y = 0.0
        items.append((poly, text, y))
    items.sort(key=lambda x: x[2])
    return items


def _ocr_text(image_path: Path) -> str:
    if _backend() == "paddle":
        return ocr_image_file(image_path)
    lines: list[str] = []
    for _poly, text, _y in _rapid_items(image_path):
        if not lines or lines[-1] != text:
            lines.append(text)
    return "\n".join(lines)


def _ocr_boxes(image_path: Path) -> list[OcrTextBox]:
    if _backend() == "paddle":
        return ocr_image_text_boxes(image_path)
    boxes: list[OcrTextBox] = []
    for poly, text, _y in _rapid_items(image_path):
        try:
            cx = sum(float(p[0]) for p in poly) / max(len(poly), 1)
            cy = sum(float(p[1]) for p in poly) / max(len(poly), 1)
        except Exception:
            continue
        boxes.append(OcrTextBox(text=text, cx=cx, cy=cy))
    return boxes


class OcrHandler(BaseHTTPRequestHandler):
    server_version = "WXWorkOCR/1.0"

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            _json_response(self, 200, {"ok": True, "service": "wxwork_ocr"})
            return
        _json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            n = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            payload = json.loads(raw or "{}")
            if self.path == "/warmup":
                self._handle_warmup()
            elif self.path == "/ocr":
                self._handle_ocr(payload)
            else:
                _json_response(self, 404, {"ok": False, "error": "not found"})
        except Exception as e:
            log.exception("request failed")
            _json_response(self, 500, {"ok": False, "error": str(e)})

    def _handle_warmup(self) -> None:
        t0 = time.perf_counter()
        with _predict_lock:
            if _backend() == "paddle":
                _get_ocr()
            text = _ocr_text(_warmup_image())
        _json_response(self, 200, {"ok": True, "text": text, "elapsed_sec": time.perf_counter() - t0})

    def _handle_ocr(self, payload: dict) -> None:
        mode = str(payload.get("mode") or "text")
        uploaded_tmp: Path | None = None
        try:
            b64_raw = payload.get("image_base64")
            path_raw = payload.get("path")

            if b64_raw is not None and str(b64_raw).strip() != "":
                try:
                    raw = base64.b64decode(str(b64_raw), validate=False)
                except Exception as e:
                    _json_response(self, 400, {"ok": False, "error": f"bad image_base64: {e}"})
                    return
                fd, tmp_path = tempfile.mkstemp(prefix="wxwork_ocr_up_", suffix=".png")
                os.close(fd)
                uploaded_tmp = Path(tmp_path)
                uploaded_tmp.write_bytes(raw)
                image_path = uploaded_tmp.resolve()
            elif path_raw is not None and str(path_raw).strip() != "":
                image_path = Path(str(path_raw)).resolve()
            else:
                _json_response(self, 400, {"ok": False, "error": "provide path or image_base64"})
                return

            if not image_path.is_file():
                _json_response(self, 400, {"ok": False, "error": f"image not found: {image_path}"})
                return

            t0 = time.perf_counter()
            with _predict_lock:
                run_path, sx, sy, temporary = _prepare_image_for_ocr(image_path)
                if mode == "boxes":
                    boxes = _ocr_boxes(run_path)
                    for b in boxes:
                        b.cx *= sx
                        b.cy *= sy
                    _json_response(
                        self,
                        200,
                        {
                            "ok": True,
                            "boxes": [b.__dict__ for b in boxes],
                            "elapsed_sec": time.perf_counter() - t0,
                        },
                    )
                else:
                    text = _ocr_text(run_path)
                    _json_response(
                        self,
                        200,
                        {"ok": True, "text": text, "elapsed_sec": time.perf_counter() - t0},
                    )
                if temporary:
                    run_path.unlink(missing_ok=True)
        finally:
            if uploaded_tmp is not None:
                uploaded_tmp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="WXWorkAgent resident PaddleOCR service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--max-side",
        type=int,
        default=None,
        help="Resize OCR input so its longest side is at most this many pixels (default: env WXWORK_OCR_MAX_SIDE or 960)",
    )
    parser.add_argument("--no-warmup", action="store_true", help="Do not load OCR before serving")
    parser.add_argument("--backend", choices=["rapidocr", "paddle"], default=None)
    parser.add_argument("--device", default=None, help="Paddle device, e.g. cpu or gpu:0")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    if args.backend is not None:
        os.environ["WXWORK_OCR_BACKEND"] = args.backend
    if args.device is not None:
        os.environ["WXWORK_OCR_DEVICE"] = args.device
    if args.max_side is not None:
        os.environ["WXWORK_OCR_MAX_SIDE"] = str(args.max_side)
    server = ThreadingHTTPServer((args.host, args.port), OcrHandler)
    log.info("WXWork OCR service listening on http://%s:%d", args.host, args.port)
    log.info("OCR backend: %s", _backend())
    log.info("OCR device: %s", os.environ.get("WXWORK_OCR_DEVICE", "cpu"))
    log.info("OCR max side: %s", _max_side() or "disabled")

    if not args.no_warmup:
        log.info("Warming up PaddleOCR...")
        t0 = time.perf_counter()
        with _predict_lock:
            if _backend() == "paddle":
                _get_ocr()
            _ocr_text(_warmup_image())
        log.info("OCR ready in %.1fs", time.perf_counter() - t0)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopping OCR service")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
