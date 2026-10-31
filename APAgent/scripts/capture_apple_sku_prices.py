from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "data" / "apagent.db"
APPLE_PRO_URL = "https://www.apple.com/shop/buy-iphone/iphone-17-pro"

MODELS = [
    {
        "model": "iPhone 17 Pro",
        "model_path": "6.3-inch-display",
        "display": "6.3-inch",
        "storages": ["256gb", "512gb", "1tb"],
    },
    {
        "model": "iPhone 17 Pro Max",
        "model_path": "6.9-inch-display",
        "display": "6.9-inch",
        "storages": ["256gb", "512gb", "1tb", "2tb"],
    },
]
COLORS = [
    ("Silver", "silver"),
    ("Cosmic Orange", "cosmic-orange"),
    ("Deep Blue", "deep-blue"),
]
CARRIERS = [
    ("Connect to any carrier later", "unlocked"),
    ("AT&T", "att"),
    ("Boost Mobile", "boost-mobile"),
    ("T-Mobile", "t-mobile"),
    ("Verizon", "verizon"),
]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_price_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sku_price_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          platform TEXT NOT NULL,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          total INTEGER NOT NULL DEFAULT 0,
          ok_count INTEGER NOT NULL DEFAULT 0,
          error_count INTEGER NOT NULL DEFAULT 0,
          note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sku_price_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id INTEGER NOT NULL,
          platform TEXT NOT NULL,
          product_family TEXT NOT NULL,
          model TEXT NOT NULL,
          model_path TEXT NOT NULL,
          display TEXT NOT NULL,
          storage TEXT NOT NULL,
          color TEXT NOT NULL,
          carrier TEXT NOT NULL,
          path TEXT NOT NULL,
          url TEXT NOT NULL,
          price_text TEXT NOT NULL,
          price_cents INTEGER,
          currency TEXT NOT NULL DEFAULT 'USD',
          ok INTEGER NOT NULL DEFAULT 1,
          error TEXT NOT NULL DEFAULT '',
          captured_at TEXT NOT NULL,
          raw_json TEXT NOT NULL DEFAULT '',
          FOREIGN KEY (run_id) REFERENCES sku_price_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sku_price_records_run ON sku_price_records(run_id);
        CREATE INDEX IF NOT EXISTS idx_sku_price_records_config ON sku_price_records(platform, model_path, storage, color, carrier);
        """
    )


def build_matrix() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for model in MODELS:
        for storage in model["storages"]:
            for color_label, color_value in COLORS:
                for carrier_label, carrier_value in CARRIERS:
                    path = f"{model['model_path']}-{storage}-{color_value}-{carrier_value}"
                    rows.append(
                        {
                            "platform": "apple",
                            "product_family": "iPhone 17 Pro",
                            "model": model["model"],
                            "model_path": model["model_path"],
                            "display": model["display"],
                            "storage": storage.upper().replace("GB", "GB"),
                            "storage_path": storage,
                            "color": color_label,
                            "color_path": color_value,
                            "carrier": carrier_label,
                            "carrier_path": carrier_value,
                            "path": path,
                            "url": f"{APPLE_PRO_URL}/{path}",
                        }
                    )
    return rows


def parse_price(text: str) -> tuple[str, int | None]:
    patterns = [
        r"Buy\s*\n?\s*(\$[\d,]+(?:\.\d{2})?)",
        r"One-time payment\s*\n?\s*(\$[\d,]+(?:\.\d{2})?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            price = match.group(1)
            return price, price_to_cents(price)
    money = re.findall(r"\$[\d,]+(?:\.\d{2})?", text)
    for price in money:
        cents = price_to_cents(price)
        if cents and cents >= 100_000:
            return price, cents
    return "", None


def build_ordering_metadata(item: dict[str, str], page_url: str, price_text: str) -> dict[str, Any]:
    return {
        "title_hint": item["model"],
        "url": page_url,
        "price_text": price_text,
        "selection_strategy": "url_path",
        "product_url_pattern": f"{APPLE_PRO_URL}/{{model_path}}-{{storage_path}}-{{color_path}}-{{carrier_path}}",
        "selection_path": [
            {
                "step": "model",
                "label": item["model"],
                "value": item["model_path"],
                "url_segment": item["model_path"],
                "page_section": "Model. Which is best for you?",
            },
            {
                "step": "finish",
                "label": item["color"],
                "value": item["color_path"],
                "url_segment": item["color_path"],
                "page_section": "Finish. Pick your favorite.",
            },
            {
                "step": "storage",
                "label": item["storage"],
                "value": item["storage_path"],
                "url_segment": item["storage_path"],
                "page_section": "Storage. How much space do you need?",
            },
            {
                "step": "payment_option",
                "label": "Buy",
                "value": "fullPrice",
                "page_section": "Payment options. Select the one that works for you.",
            },
            {
                "step": "carrier",
                "label": item["carrier"],
                "value": item["carrier_path"],
                "url_segment": item["carrier_path"],
                "page_section": "Connectivity. Choose a carrier.",
            },
            {
                "step": "applecare",
                "label": "No AppleCare coverage",
                "value": "none",
                "page_section": "AppleCare coverage. Peace of mind in every plan.",
            },
        ],
        "order_task_defaults": {
            "applecare": "none",
            "payment_option": "fullPrice",
            "quantity": 1,
            "stop_before_final_submit": True,
        },
    }


def price_to_cents(price: str) -> int | None:
    cleaned = re.sub(r"[^\d.]", "", price)
    if not cleaned:
        return None
    return int(round(float(cleaned) * 100))


async def scrape_one(context: Any, item: dict[str, str], sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        page = await context.new_page()
        try:
            await page.goto(item["url"], wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(4500)
            text = await page.locator("body").inner_text(timeout=30_000)
            price_text, price_cents = parse_price(text)
            if not price_text:
                raise RuntimeError("price not found")
            return {
                **item,
                "price_text": price_text,
                "price_cents": price_cents,
                "currency": "USD",
                "ok": 1,
                "error": "",
                "captured_at": now(),
                "raw_json": json.dumps(
                    {
                        "title": await page.title(),
                        **build_ordering_metadata(item, page.url, price_text),
                    },
                    ensure_ascii=False,
                ),
            }
        except Exception as exc:
            return {
                **item,
                "price_text": "",
                "price_cents": None,
                "currency": "USD",
                "ok": 0,
                "error": str(exc),
                "captured_at": now(),
                "raw_json": "{}",
            }
        finally:
            await page.close()


async def fill_missing_from_canonical(context: Any, records: list[dict[str, Any]]) -> None:
    missing_keys = sorted({(item["model"], item["model_path"], item["storage_path"]) for item in records if not item["ok"]})
    for model, model_path, storage_path in missing_keys:
        existing = next(
            (
                item
                for item in records
                if item["ok"] and item["model"] == model and item["storage_path"] == storage_path
            ),
            None,
        )
        if existing:
            apply_price(records, model, storage_path, existing["price_text"], existing["price_cents"], existing["raw_json"])
            continue

        canonical = {
            "url": f"{APPLE_PRO_URL}/{model_path}-{storage_path}-silver-unlocked",
        }
        page = await context.new_page()
        try:
            await page.goto(canonical["url"], wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(8000)
            text = await page.locator("body").inner_text(timeout=30_000)
            price_text, price_cents = parse_price(text)
            if not price_text:
                continue
            raw_json = json.dumps(
                {
                    "expanded_from_canonical": True,
                    "title": await page.title(),
                    "url": page.url,
                    "price_text": price_text,
                    "selection_strategy": "canonical_price_fill",
                    "canonical_url": canonical["url"],
                    "note": "Price was filled from the same model/storage canonical URL. Selection path should still use the original record URL.",
                },
                ensure_ascii=False,
            )
            apply_price(records, model, storage_path, price_text, price_cents, raw_json)
        finally:
            await page.close()


def apply_price(
    records: list[dict[str, Any]], model: str, storage_path: str, price_text: str, price_cents: int | None, raw_json: str
) -> None:
    for item in records:
        if item["model"] == model and item["storage_path"] == storage_path and not item["ok"]:
            item["price_text"] = price_text
            item["price_cents"] = price_cents
            item["ok"] = 1
            item["error"] = ""
            item["captured_at"] = now()
            item["raw_json"] = raw_json


def insert_records(run_id: int, records: list[dict[str, Any]]) -> None:
    with db() as conn:
        for record in records:
            conn.execute(
                """INSERT INTO sku_price_records(run_id, platform, product_family, model, model_path, display,
                   storage, color, carrier, path, url, price_text, price_cents, currency, ok, error,
                   captured_at, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    record["platform"],
                    record["product_family"],
                    record["model"],
                    record["model_path"],
                    record["display"],
                    record["storage"],
                    record["color"],
                    record["carrier"],
                    record["path"],
                    record["url"],
                    record["price_text"],
                    record["price_cents"],
                    record["currency"],
                    record["ok"],
                    record["error"],
                    record["captured_at"],
                    record["raw_json"],
                ),
            )
        ok_count = sum(1 for item in records if item["ok"])
        conn.execute(
            """UPDATE sku_price_runs SET status=?, finished_at=?, total=?, ok_count=?, error_count=? WHERE id=?""",
            ("done" if ok_count == len(records) else "partial", now(), len(records), ok_count, len(records) - ok_count, run_id),
        )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()

    matrix = build_matrix()
    with db() as conn:
        init_price_tables(conn)
        cur = conn.execute(
            "INSERT INTO sku_price_runs(platform, source, status, started_at, note) VALUES (?, ?, ?, ?, ?)",
            ("apple", APPLE_PRO_URL, "running", now(), "one-time buy price by SKU configuration"),
        )
        run_id = int(cur.lastrowid)

    sem = asyncio.Semaphore(max(1, args.concurrency))
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="en-US")
        records = await asyncio.gather(*(scrape_one(context, item, sem) for item in matrix))
        await fill_missing_from_canonical(context, records)
        await browser.close()

    insert_records(run_id, records)
    print(json.dumps({"run_id": run_id, "total": len(records), "ok": sum(1 for x in records if x["ok"])}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
