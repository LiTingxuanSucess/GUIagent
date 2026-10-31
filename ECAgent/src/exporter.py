from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


CSV_FIELDS = [
    "platform",
    "brand",
    "series",
    "keyword",
    "rank_position",
    "title",
    "product_url",
    "shop_name",
    "shop_type",
    "official_store_flag",
    "price_raw",
    "final_price",
    "price_source",
    "ocr_price",
    "sales_text",
    "location",
    "main_image_url",
    "spec_text",
    "selected_sku",
    "piece_count",
    "unit_price",
    "rank_source",
    "series_match_reason",
    "detail_title",
    "screenshot_path",
    "confidence",
    "notes",
    "captured_at",
]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
