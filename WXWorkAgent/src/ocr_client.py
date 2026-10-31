"""HTTP client for the resident WXWorkAgent OCR service (local or gateway URL)."""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from src.ocr_run import OcrTextBox
from src.text_encoding import repair_mojibake

_DEFAULT_URL = "http://127.0.0.1:8765"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OCR_CFG = _REPO_ROOT / "config" / "ocr_service.yaml"


@lru_cache(maxsize=1)
def _file_settings() -> dict:
    path = Path(os.environ.get("WXWORK_OCR_CONFIG_PATH", str(_DEFAULT_OCR_CFG)))
    if not path.is_file():
        return {}
    try:
        import yaml

        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def service_url() -> str:
    env = os.environ.get("WXWORK_OCR_SERVICE_URL")
    cfg = _file_settings()
    base = env if env is not None and str(env).strip() else cfg.get("base_url") or _DEFAULT_URL
    return str(base).rstrip("/")


def _is_local_service_url() -> bool:
    hostname = (urlparse(service_url()).hostname or "").lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def _urlopen(req: urllib.request.Request, timeout: float):
    if _is_local_service_url():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _timeout() -> float:
    env = os.environ.get("WXWORK_OCR_SERVICE_TIMEOUT")
    if env is not None and str(env).strip():
        return float(env)
    cfg = _file_settings()
    tv = cfg.get("timeout_sec")
    if tv is not None:
        try:
            return float(tv)
        except (TypeError, ValueError):
            pass
    return 120.0


def _use_image_base64() -> bool:
    """
    Remote OCR backends cannot open Windows screenshot paths — send PNG bytes in JSON instead.

    WXWORK_OCR_TRANSPORT: base64 | path | '' (auto).
    YAML key use_image_base64: optional explicit bool overrides auto if transport not set.
    """
    explicit = os.environ.get("WXWORK_OCR_TRANSPORT", "").strip().lower()
    if explicit == "base64":
        return True
    if explicit == "path":
        return False
    cfg = _file_settings()
    if "use_image_base64" in cfg and isinstance(cfg["use_image_base64"], bool):
        return cfg["use_image_base64"]
    hostname = (urlparse(service_url()).hostname or "").lower()
    return hostname not in {"", "localhost", "127.0.0.1"}


def _post_json(path: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        service_url() + path,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with _urlopen(req, timeout=_timeout()) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        snippet = ""
        try:
            err_obj = json.loads(raw or "{}")
            if err_obj.get("error"):
                snippet = f" ({err_obj.get('error')})"
        except Exception:
            snippet = raw[:280] + ("…" if len(raw) > 280 else "")
            if snippet:
                snippet = f" ({snippet})"
        raise RuntimeError(f"OCR POST {service_url()}{path} HTTP {e.code}{snippet}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"OCR service unreachable at {service_url()}: {e}. "
            r"Configure config/ocr_service.yaml base_url or WXWORK_OCR_SERVICE_URL."
        ) from e

    result = json.loads(body or "{}")
    if not result.get("ok"):
        raise RuntimeError(f"OCR service error: {result.get('error') or result}")
    return result


def health_ocr_service() -> dict:
    """GET /health on the OCR service (through gateway prefix)."""
    url = service_url() + "/health"
    req = urllib.request.Request(url, method="GET")
    try:
        with _urlopen(req, timeout=_timeout()) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        raise RuntimeError(f"OCR GET {url} HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OCR GET {url}: {e}") from e

    parsed = json.loads(body or "{}")
    if not parsed.get("ok"):
        raise RuntimeError(f"OCR health rejected: {parsed}")
    return parsed


def warmup_ocr_service() -> str:
    """Ask the OCR service to run a tiny warmup pass."""
    result = _post_json("/warmup", {})
    return str(result.get("text") or "")


def _ocr_payload_for_file(image_path: Path, mode: str) -> dict:
    p = Path(image_path).resolve()
    if _use_image_base64():
        return {
            "image_base64": base64.standard_b64encode(p.read_bytes()).decode("ascii"),
            "mode": mode,
        }
    return {"path": str(p), "mode": mode}


def ocr_image_file(image_path: Path) -> str:
    """Return newline-joined OCR text through the OCR service."""
    result = _post_json("/ocr", _ocr_payload_for_file(image_path, "text"))
    return repair_mojibake(str(result.get("text") or ""))


def ocr_image_text_boxes(image_path: Path) -> list[OcrTextBox]:
    """Return OCR text boxes through the OCR service."""
    result = _post_json("/ocr", _ocr_payload_for_file(image_path, "boxes"))
    boxes: list[OcrTextBox] = []
    for item in result.get("boxes") or []:
        try:
            boxes.append(
                OcrTextBox(
                    text=repair_mojibake(str(item["text"])),
                    cx=float(item["cx"]),
                    cy=float(item["cy"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return boxes
