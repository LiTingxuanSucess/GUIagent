"""Smoke test: config/config/ocr_service.yaml + OCR health / warmup / OCR (base64).

Run from repo root WXWorkAgent/::

    python tools/ocr_cloud_smoke.py
"""
from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))

    from src.ocr_client import (  # noqa: E402
        health_ocr_service,
        ocr_image_file,
        service_url,
        warmup_ocr_service,
    )

    print(">>> service:", service_url(), flush=True)
    h = health_ocr_service()
    print(">>> health:", h, flush=True)
    w = warmup_ocr_service()
    print(f">>> warmup text sample: {(w[:80] + '…') if len(w) > 80 else w!r}", flush=True)

    one_px_png = base64.standard_b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(one_px_png)
        png_path = Path(f.name)

    try:
        text = ocr_image_file(png_path)
        print(">>> OCR(1×1 PNG):", repr(text), flush=True)
    finally:
        png_path.unlink(missing_ok=True)

    print(">>> OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
