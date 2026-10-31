"""Offline PPStructure layout probe for WeWork screenshots.

This tool is intentionally read-only for WeWork. It runs PaddleOCR's
PPStructure on saved screenshots and writes normalized JSON so we can decide
whether document-layout detection is useful for chat UI parsing.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ocr_runtime import init_ocr_runtime, prepare_paddle_inference

init_ocr_runtime()


def _to_float_list(value: Any) -> list[float]:
    if value is None:
        return []
    arr = np.asarray(value, dtype=float).reshape(-1)
    return [round(float(x), 2) for x in arr.tolist()]


def _normalize_res(res: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(res or []):
        if not isinstance(item, dict):
            rows.append({"index": idx, "raw": repr(item)[:500]})
            continue
        row: dict[str, Any] = {
            "index": idx,
            "type": item.get("type"),
            "bbox": _to_float_list(item.get("bbox")),
        }
        text_rows: list[str] = []
        subres = item.get("res")
        if isinstance(subres, list):
            for part in subres:
                if isinstance(part, dict):
                    txt = part.get("text") or part.get("rec_text") or part.get("label")
                    if txt:
                        text_rows.append(str(txt))
                elif isinstance(part, (list, tuple)) and len(part) >= 2:
                    rec = part[1]
                    if isinstance(rec, (list, tuple)) and rec:
                        text_rows.append(str(rec[0]))
                    elif rec:
                        text_rows.append(str(rec))
        elif isinstance(subres, str):
            text_rows.append(subres)
        if item.get("text"):
            text_rows.append(str(item.get("text")))
        if text_rows:
            row["text"] = "\n".join(x.strip() for x in text_rows if x and x.strip())
        rows.append(row)
    rows.sort(key=lambda x: ((x.get("bbox") or [0, 0, 0, 0])[1], (x.get("bbox") or [0, 0, 0, 0])[0]))
    return rows


def _draw_blocks(image_path: Path, rows: list[dict[str, Any]], out_path: Path) -> None:
    img = _read_image(image_path)
    if img is None:
        return
    for row in rows:
        bbox = row.get("bbox") or []
        if len(bbox) < 4:
            continue
        x0, y0, x1, y1 = [int(round(float(x))) for x in bbox[:4]]
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 80, 255), 2)
        label = f"{row.get('index')}:{row.get('type')}"
        cv2.putText(img, label, (x0, max(18, y0 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 80, 255), 2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_image(out_path, img)


def _read_image(path: Path) -> np.ndarray | None:
    raw = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    return cv2.imdecode(raw, cv2.IMREAD_COLOR)


def _write_image(path: Path, img: np.ndarray) -> None:
    ok, encoded = cv2.imencode(path.suffix or ".png", img)
    if not ok:
        raise RuntimeError(f"failed to encode image: {path}")
    path.write_bytes(encoded.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PPStructure on saved WeWork screenshots.")
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "exports" / "paddle_layout_probe")
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--no-draw", action="store_true")
    args = parser.parse_args()

    import paddle
    from paddleocr import PPStructure

    paddle.set_device(args.device)
    prepare_paddle_inference()
    engine = PPStructure(
        lang="ch",
        use_gpu=args.device.startswith("gpu"),
        show_log=False,
        layout=True,
        table=False,
        ocr=True,
        use_angle_cls=False,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    for image_path in args.images:
        image_path = image_path.resolve()
        img = _read_image(image_path)
        if img is None:
            raise FileNotFoundError(f"cannot read image: {image_path}")
        t0 = time.perf_counter()
        res = engine(img)
        elapsed = time.perf_counter() - t0
        rows = _normalize_res(res)
        stem = image_path.stem
        payload = {
            "image": str(image_path),
            "elapsed_sec": round(elapsed, 3),
            "blocks": rows,
        }
        json_path = args.out_dir / f"{stem}.layout.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        draw_path = args.out_dir / f"{stem}.layout.png"
        if not args.no_draw:
            _draw_blocks(image_path, rows, draw_path)
        summary.append(
            {
                "image": str(image_path),
                "elapsed_sec": round(elapsed, 3),
                "block_count": len(rows),
                "types": sorted({str(row.get("type")) for row in rows}),
                "json": str(json_path),
                "draw": str(draw_path) if not args.no_draw else "",
            }
        )
        print(f"{image_path.name}: {elapsed:.2f}s blocks={len(rows)} types={summary[-1]['types']}", flush=True)

    (args.out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
