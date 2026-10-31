"""Run PaddleOCR 3.x on an image file; extract plain text (sorted top-to-bottom)."""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

import filelock

from src.ocr_runtime import init_ocr_runtime, prepare_paddle_inference

_ocr_singleton: Any = None
_ocr_thread_lock = threading.Lock()

# Cross-process: only one process may run PaddleOCR() first-time init (model download / paddlex locks).
_init_lock_file = Path(os.environ.get("TEMP", os.environ.get("TMP", "."))) / "wxworkagent_paddleocr.init.lock"
_init_timeout_sec = int(os.environ.get("WXWORK_OCR_INIT_LOCK_TIMEOUT", "900"))

log = logging.getLogger(__name__)


def _paddleocr_major_version() -> int:
    try:
        import paddleocr

        raw = str(getattr(paddleocr, "__version__", "0") or "0")
        return int(raw.split(".", 1)[0])
    except Exception:
        return 0


def _build_paddle_ocr():
    from paddleocr import PaddleOCR

    major = _paddleocr_major_version()
    if major >= 3:
        det_model = os.environ.get("WXWORK_OCR_DET_MODEL", "PP-OCRv4_mobile_det")
        rec_model = os.environ.get("WXWORK_OCR_REC_MODEL", "PP-OCRv4_mobile_rec")

        log.info("加载 PaddleOCR 3.x (PP-OCRv4 mobile det+rec, 仅 2 模型)…")
        # 直接指定 mobile 模型（det 4MB + rec 10MB = 14MB），
        # 禁用 doc/textline 方向分类 + UVDoc（3 个额外模型），
        # 对聊天截图既不需要也无加速效果。
        return PaddleOCR(
            lang="ch",
            text_detection_model_name=det_model,
            text_recognition_model_name=rec_model,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )

    device = os.environ.get("WXWORK_OCR_DEVICE", "cpu").strip().lower()
    use_gpu = device.startswith("gpu")
    log.info("加载 PaddleOCR 2.x (PP-OCRv4, use_gpu=%s)…", use_gpu)
    return PaddleOCR(
        lang="ch",
        use_angle_cls=False,
        use_gpu=use_gpu,
        show_log=False,
    )


def _get_ocr():
    global _ocr_singleton
    init_ocr_runtime()

    if _ocr_singleton is not None:
        return _ocr_singleton

    with _ocr_thread_lock:
        if _ocr_singleton is not None:
            return _ocr_singleton

        _init_lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock = filelock.FileLock(str(_init_lock_file), timeout=_init_timeout_sec)
        log.info(
            "等待 PaddleOCR 初始化锁 %s（避免多进程争抢 paddlex 模型锁；仅首次较慢）",
            _init_lock_file,
        )
        try:
            with lock:
                prepare_paddle_inference()
                _ocr_singleton = _build_paddle_ocr()
        except filelock.Timeout as e:
            raise RuntimeError(
                "PaddleOCR 初始化锁等待超时。请：1) 关闭其他正在跑 wxwork_reply / jupyter 的 Python；"
                "2) 或结束卡死的 python 后删除锁文件: "
                f"{_init_lock_file}"
            ) from e

    return _ocr_singleton


def _y_center(poly: Any) -> float:
    if poly is None:
        return 0.0
    arr = np.asarray(poly)
    if arr.size == 0:
        return 0.0
    return float(arr[:, 1].mean())


def _poly_center(poly: Any) -> tuple[float, float]:
    if poly is None:
        return 0.0, 0.0
    arr = np.asarray(poly)
    if arr.size == 0:
        return 0.0, 0.0
    return float(arr[:, 0].mean()), float(arr[:, 1].mean())


def _collect_poly_text_pairs(results: Any) -> list[tuple[Any, str, float]]:
    """Return [(poly, text, y_center), ...] from one predict() output."""
    chunks: list[tuple[Any, str, float]] = []
    if not results:
        return chunks

    for res in results:
        texts: list[str] = []
        polys: list[Any] = []

        if hasattr(res, "__getitem__"):
            try:
                texts = list(res["rec_texts"] or [])
            except (KeyError, TypeError):
                texts = []
            try:
                polys = list(res.get("rec_polys", []) or res.get("dt_polys", []) or [])
            except Exception:
                polys = []

        if texts and polys and len(polys) == len(texts):
            for poly, txt in zip(polys, texts):
                t = (txt[0] if isinstance(txt, tuple) else str(txt)).strip()
                if t:
                    chunks.append((poly, t, _y_center(poly)))
        elif texts:
            for txt in texts:
                t = (txt[0] if isinstance(txt, tuple) else str(txt)).strip()
                if t:
                    chunks.append((None, t, 0.0))

    chunks.sort(key=lambda x: x[2])
    return chunks


def _collect_v2_poly_text_pairs(results: Any) -> list[tuple[Any, str, float]]:
    """Return [(poly, text, y_center), ...] from PaddleOCR 2.x ocr() output."""
    chunks: list[tuple[Any, str, float]] = []
    if not results:
        return chunks

    pages = results
    if (
        isinstance(results, list)
        and len(results) == 1
        and isinstance(results[0], list)
        and (not results[0] or (isinstance(results[0][0], list) and len(results[0][0]) >= 2))
    ):
        pages = results[0]

    for row in pages or []:
        if not row or len(row) < 2:
            continue
        poly = row[0]
        rec = row[1]
        txt = rec[0] if isinstance(rec, (tuple, list)) and rec else rec
        text = str(txt).strip()
        if text:
            chunks.append((poly, text, _y_center(poly)))

    chunks.sort(key=lambda x: x[2])
    return chunks


def _predict_pairs(ocr: Any, path: str) -> list[tuple[Any, str, float]]:
    if hasattr(ocr, "predict"):
        return _collect_poly_text_pairs(ocr.predict(path))
    return _collect_v2_poly_text_pairs(ocr.ocr(path, cls=False))


@dataclass
class OcrTextBox:
    """Single text line with center in image pixel coordinates (same as PNG)."""

    text: str
    cx: float
    cy: float


def ocr_image_text_boxes(image_path: Path) -> list[OcrTextBox]:
    """
    Run OCR and return each text line with the center of its quad polygon.

    Coordinates match the input image pixel space (for mapping to mss screenshots).
    """
    path = str(Path(image_path).resolve())
    ocr = _get_ocr()
    print(f"  [OCR] 推理中: {Path(path).name} …", flush=True)
    import time as _time

    _t = _time.perf_counter()
    pairs = _predict_pairs(ocr, path)
    print(f"  [OCR] 推理完成 {_time.perf_counter()-_t:.1f}s", flush=True)

    out: list[OcrTextBox] = []
    for poly, t, _yc in pairs:
        if poly is None:
            continue
        cx, cy = _poly_center(poly)
        out.append(OcrTextBox(text=t, cx=cx, cy=cy))
    return out


def ocr_image_file(image_path: Path) -> str:
    """Return newline-joined recognized lines, roughly reading order."""
    path = str(Path(image_path).resolve())
    ocr = _get_ocr()
    print(f"  [OCR] 推理中: {Path(path).name} …", flush=True)
    import time as _time

    _t = _time.perf_counter()
    pairs = _predict_pairs(ocr, path)
    print(f"  [OCR] 推理完成 {_time.perf_counter()-_t:.1f}s", flush=True)

    chunks: list[tuple[float, str]] = []
    for poly, t, yc in pairs:
        chunks.append((yc, t))

    lines = [c[1] for c in chunks]
    out: list[str] = []
    for line in lines:
        if not out or out[-1] != line:
            out.append(line)
    return "\n".join(out)
