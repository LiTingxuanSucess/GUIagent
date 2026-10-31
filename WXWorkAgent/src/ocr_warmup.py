"""One-shot PaddleOCR load + model fetch (run before GUI automation)."""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from PIL import Image


def _p(msg: str) -> None:
    """Print directly to stdout with flush, bypassing any logging config paddleocr may have altered."""
    print(msg, flush=True)
    sys.stdout.flush()


def warmup_ocr_pipeline() -> str:
    """Load PaddleOCR singleton and run one inference pass. Returns recognised text (empty is fine)."""
    from src.ocr_run import _get_ocr, ocr_image_file

    _p("[OCR] 初始化模型（约 10-20s）…")
    t0 = time.perf_counter()
    _get_ocr()
    _p(f"[OCR] 模型加载完成（{time.perf_counter()-t0:.1f}s）。开始推理热身（约 5-10s）…")

    p = Path(tempfile.gettempdir()) / "wxwork_ocr_warmup.png"
    Image.new("RGB", (320, 120), (248, 248, 248)).save(p, "PNG")
    t1 = time.perf_counter()
    try:
        result = ocr_image_file(p)
    finally:
        p.unlink(missing_ok=True)

    _p(f"[OCR] 推理热身完成（{time.perf_counter()-t1:.1f}s）。OCR 就绪。")
    return result
