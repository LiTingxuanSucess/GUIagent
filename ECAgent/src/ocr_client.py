from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_OCR_URL = "http://127.0.0.1:8765"


def ocr_service_url() -> str:
    return os.environ.get("WXWORK_OCR_SERVICE_URL", DEFAULT_OCR_URL).rstrip("/")


def ocr_image_file(image_path: Path, timeout: float = 30.0) -> str | None:
    payload = {"path": str(Path(image_path).resolve()), "mode": "text"}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        ocr_service_url() + "/ocr",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    if not result.get("ok"):
        return None
    return str(result.get("text") or "")
