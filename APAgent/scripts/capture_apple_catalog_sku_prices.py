from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = BASE_DIR.parents[0]
DOCS_DIR = WORKSPACE_DIR / "docs"
DB_PATH = BASE_DIR / "data" / "apagent.db"
CATALOG_JSON = DOCS_DIR / "APPLE_PRODUCT_CATALOG.json"

EXCLUDED_RADIO_NAMES = {
    "purchase_option_group",
    "purchase_option_more",
    "dimensionPaymentType",
    "tradeupinline",
    "noTradeIn",
    "applecare-options",
    "engraving-option-select-",
    "ctofilter",
}


def is_config_dimension(name: str) -> bool:
    if name == "carrierModel":
        return True
    lowered = name.lower()
    if name in EXCLUDED_RADIO_NAMES:
        return False
    if lowered.startswith("acc_") or "payment" in lowered or "purchase_option" in lowered:
        return False
    return "dimension" in lowered

CARRIER_LABELS = {
    "unlocked": "Connect to any carrier later",
    "att": "AT&T",
    "boost-mobile": "Boost Mobile",
    "t-mobile": "T-Mobile",
    "verizon": "Verizon",
}

COLOR_LABEL_OVERRIDES = {
    "space_gray": "Space Gray",
    "cosmic-orange": "Cosmic Orange",
    "cosmicorange": "Cosmic Orange",
    "deep-blue": "Deep Blue",
    "deepblue": "Deep Blue",
    "starlight": "Starlight",
}


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
        CREATE TABLE IF NOT EXISTS sku_capture_cache (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          platform TEXT NOT NULL,
          catalog_url TEXT NOT NULL,
          catalog_path TEXT NOT NULL DEFAULT '',
          product_family TEXT NOT NULL DEFAULT '',
          product_name TEXT NOT NULL DEFAULT '',
          cache_kind TEXT NOT NULL,
          cache_key TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          hit_count INTEGER NOT NULL DEFAULT 0,
          UNIQUE(platform, catalog_url, cache_kind, cache_key)
        );
        CREATE INDEX IF NOT EXISTS idx_sku_capture_cache_url ON sku_capture_cache(catalog_url, cache_kind);
        """
    )


def load_catalog() -> list[dict[str, Any]]:
    return json.loads(CATALOG_JSON.read_text(encoding="utf-8"))


def price_to_cents(price: str) -> int | None:
    cleaned = re.sub(r"[^\d.]", "", price)
    if not cleaned:
        return None
    return int(round(float(cleaned) * 100))


def parse_price(text: str, *, min_cents: int = 100) -> tuple[str, int | None]:
    patterns = [
        r"Buy\s*\n?\s*(\$[\d,]+(?:\.\d{2})?)",
        r"One-time payment\s*\n?\s*(\$[\d,]+(?:\.\d{2})?)",
        r"From\s*\n?\s*(\$[\d,]+(?:\.\d{2})?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            price = match.group(1)
            return price, price_to_cents(price)
    for price in re.findall(r"\$[\d,]+(?:\.\d{2})?", text):
        cents = price_to_cents(price)
        if cents and cents >= min_cents:
            return price, cents
    return "", None


def humanize_value(value: str) -> str:
    if value in COLOR_LABEL_OVERRIDES:
        return COLOR_LABEL_OVERRIDES[value]
    cleaned = value.replace("_", " ").replace("-", " ")
    if re.fullmatch(r"\d+\s*gb", cleaned, re.I) or re.fullmatch(r"\d+gb", cleaned, re.I):
        return cleaned.upper().replace("GB", "GB")
    if re.fullmatch(r"\d+\s*tb", cleaned, re.I) or re.fullmatch(r"\d+tb", cleaned, re.I):
        return cleaned.upper().replace("TB", "TB")
    return cleaned.title()


def slugify_parts(parts: list[str]) -> str:
    return "-".join(parts)


def pick_configurable_roots(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configurables = [item for item in items if item.get("kind") == "configurable"]
    configurables.sort(key=lambda item: len(item.get("path") or ""))
    kept: list[dict[str, Any]] = []
    for item in configurables:
        path = item.get("path") or ""
        if any(path.startswith(other.get("path", "") + "/") for other in kept):
            continue
        kept.append(item)
    return kept


DIMENSION_ORDER_TOKENS = [
    "screensize",
    "casematerial",
    "case",
    "color",
    "finish",
    "chip",
    "capacity",
    "connection",
    "carrier",
    "band",
    "glass",
    "stand",
    "memory",
    "processor",
    "storage",
]


def dimension_sort_key(name: str) -> tuple[int, str]:
    lowered = name.lower()
    for idx, token in enumerate(DIMENSION_ORDER_TOKENS):
        if token in lowered:
            return (idx, name)
    return (len(DIMENSION_ORDER_TOKENS), name)


def dimension_step_name(name: str) -> str:
    lowered = name.lower()
    if "screensize" in lowered or "size" in lowered:
        return "model"
    if "color" in lowered or "finish" in lowered:
        return "finish"
    if "capacity" in lowered or "storage" in lowered:
        return "storage"
    if "connection" in lowered:
        return "connection"
    if "carrier" in lowered or name == "carrierModel":
        return "carrier"
    if "band" in lowered:
        return "band"
    if "case" in lowered:
        return "case"
    if "glass" in lowered:
        return "glass"
    if "stand" in lowered:
        return "stand"
    if "memory" in lowered:
        return "memory"
    if "processor" in lowered or "chip" in lowered:
        return "processor"
    return name.replace("dimension", "").lower() or "option"


def option_label(option: dict[str, str]) -> str:
    label = (option.get("label") or "").strip()
    value = option.get("value", "")
    if not label or len(label) > 48 or "footnote" in label.lower() or "$" in label:
        return humanize_value(value)
    return label.split("\n")[0].strip() or humanize_value(value)


def looks_like_screen_size(value: str) -> bool:
    normalized = value.replace("_", ".").replace("-", " ").lower()
    return bool(re.search(r"\b\d+(?:\.\d+)?\s*inch\b", normalized))


def normalize_display_label(value: str, label: str) -> str:
    match = re.search(r"(\d+(?:[._]\d+)?)\s*inch", f"{value} {label}".replace("_", "."), re.I)
    if not match:
        return label
    return f"{match.group(1)}-inch display"


def iphone_model_for_screen_size(catalog_name: str, screen_value: str) -> str:
    lowered = catalog_name.lower()
    normalized_size = screen_value.replace("_", ".").replace("-", ".").lower()
    if "iphone 17 pro" in lowered:
        return "iPhone 17 Pro Max" if "6.9" in normalized_size else "iPhone 17 Pro"
    if "iphone 16" in lowered and "plus" in lowered:
        return "iPhone 16 Plus" if "6.7" in normalized_size else "iPhone 16"
    if "iphone 15" in lowered and "plus" in lowered:
        return "iPhone 15 Plus" if "6.7" in normalized_size else "iPhone 15"
    return catalog_name


def clean_catalog_product_name(name: str) -> str:
    cleaned = re.sub(r"^Buy\s*-?\s*", "", name or "", flags=re.I).strip()
    cleaned = re.sub(r"\s*-\s*Apple\s*$", "", cleaned, flags=re.I).strip()
    return cleaned or name or "Unknown"


def ipad_model_for_screen_size(catalog_name: str, screen_value: str) -> str:
    base = clean_catalog_product_name(catalog_name)
    display = normalize_display_label(screen_value, screen_value).replace(" display", "")
    if "ipad air" in base.lower():
        return f"iPad Air {display}"
    if "ipad pro" in base.lower():
        return f"iPad Pro {display}"
    return f"{base} {display}".strip()


def normalize_carrier_label(value: str, label: str) -> str:
    combined = f"{value} {label}".upper()
    if "BOOST" in combined:
        return "Boost Mobile"
    if "TMOBILE" in combined or "T-MOBILE" in combined:
        return "T-Mobile"
    if "VERIZON" in combined:
        return "Verizon"
    if "ATT" in combined or "AT&T" in combined:
        return "AT&T"
    if "UNLOCKED" in combined or "CONNECT TO ANY CARRIER" in combined:
        return "Connect to any carrier later"
    return label


def normalize_connection_label(value: str, label: str) -> str:
    lowered = f"{value} {label}".lower()
    if "cell" in lowered:
        return "Wi-Fi + Cellular"
    if "wifi" in lowered or "wi-fi" in lowered:
        return "Wi-Fi"
    return label


def normalize_watch_connection_label(value: str, label: str) -> str:
    lowered = f"{value} {label}".lower()
    if "cell" in lowered:
        return "GPS + Cellular"
    if "gps" in lowered:
        return "GPS"
    return label


def normalize_watch_size(value: str, label: str) -> str:
    match = re.search(r"(\d+)\s*mm", f"{value} {label}", re.I)
    return f"{match.group(1)}mm" if match else label


def normalize_spec_label(value: str, label: str) -> str:
    combined = f"{value} {label}"
    match = re.search(r"(\d+)\s*(GB|TB)", combined, re.I)
    if match:
        return f"{match.group(1)}{match.group(2).upper()}"
    return re.sub(r"\s+Included\b", "", label, flags=re.I).strip()


def normalize_chip_label(value: str, label: str) -> str:
    cleaned = re.sub(r"\s+Included\b", "", label, flags=re.I).strip()
    cleaned = re.sub(r"\bM(\d)(Pro|Max|Ultra)\b", r"M\1 \2", cleaned)
    compact = value.lower().replace("_", "-")
    match = re.match(r"(m\d(?:pro|max|ultra)?)-(\d+)-(\d+)", compact)
    if match:
        chip, cpu, gpu = match.groups()
        chip_name = re.sub(r"\bM(\d)(Pro|Max|Ultra)\b", r"M\1 \2", humanize_value(chip))
        return f"{chip_name} {cpu}-core CPU {gpu}-core GPU"
    return cleaned


def map_record_fields(selection: dict[str, dict[str, str]], catalog_item: dict[str, Any]) -> dict[str, str]:
    model = catalog_item.get("name") or "Unknown"
    model_path = catalog_item.get("path", "").split("/")[-1] or catalog_item.get("name", "unknown")
    display = ""
    storage = "standard"
    color = ""
    carrier = "none"
    processor_parts: list[str] = []
    processor_values: list[str] = []
    memory = ""

    for dim_name, option in selection.items():
        step = dimension_step_name(dim_name)
        label = option_label(option)
        value = option.get("value", "")
        lowered_dim = dim_name.lower()
        if step == "model":
            if catalog_item.get("family") == "iPhone" and "iphone" in model.lower() and looks_like_screen_size(value):
                model = iphone_model_for_screen_size(model, value)
                display = normalize_display_label(value, label)
                model_path = iphone_url_segment(dim_name, value)
            elif catalog_item.get("family") == "iPad" and looks_like_screen_size(value):
                model = ipad_model_for_screen_size(model, value)
                display = normalize_display_label(value, label)
                model_path = value.replace("_", "-")
            elif catalog_item.get("family") == "Apple Watch" and re.search(r"\d+\s*mm", f"{value} {label}", re.I):
                size = normalize_watch_size(value, label)
                base_model = clean_catalog_product_name(catalog_item.get("name") or model)
                model = f"{base_model} {size}"
                model_path = slugify_parts([catalog_item.get("path", "").split("/")[-1] or "apple-watch", size])
            else:
                model = label
                model_path = value
            if "inch" in value.lower():
                display = normalize_display_label(value, label)
        elif step == "storage":
            storage = normalize_spec_label(value, label)
        elif step == "finish":
            if "finish" in lowered_dim and "color" not in lowered_dim:
                display = label
                model_path = slugify_parts([model_path, value])
            else:
                color = label
        elif step in {"connection", "carrier"}:
            if catalog_item.get("family") == "iPad":
                carrier = normalize_connection_label(value, label)
            elif catalog_item.get("family") == "Apple Watch":
                carrier = normalize_watch_connection_label(value, label)
            else:
                carrier = normalize_carrier_label(value, label)
        elif step == "processor":
            processor_label = normalize_chip_label(value, label)
            if processor_label:
                processor_parts = [
                    part for part in processor_parts
                    if not processor_label.startswith(part + " ")
                ]
            if processor_label and not any(part == processor_label or part.startswith(processor_label + " ") for part in processor_parts):
                processor_parts.append(processor_label)
            if catalog_item.get("family") == "Mac":
                processor_values = [
                    part for part in processor_values
                    if not value.startswith(part + "-")
                ]
                if not any(part == value or part.startswith(value + "-") for part in processor_values):
                    processor_values.append(value)
            else:
                model_path = slugify_parts([model_path, value])
        elif step == "memory":
            memory = normalize_spec_label(value, label)
            if catalog_item.get("family") != "Mac":
                model_path = slugify_parts([model_path, value])
        else:
            model_path = slugify_parts([model_path, value])

    if catalog_item.get("family") == "iPhone" and "iphone 16" in (catalog_item.get("name") or "").lower() and storage == "standard":
        storage = "128GB"
    if catalog_item.get("family") == "iPad":
        model = clean_catalog_product_name(model)
    if catalog_item.get("family") == "Apple Watch":
        model = clean_catalog_product_name(model)
    if catalog_item.get("family") == "Mac":
        base_model = clean_catalog_product_name(catalog_item.get("name") or model)
        model = " ".join([base_model, *processor_parts]).strip()
        mac_path_parts = [model_path, *processor_values]
        if memory:
            mac_path_parts.append(memory.lower())
        model_path = slugify_parts(mac_path_parts)

    path = slugify_parts([model_path, storage.lower().replace(" ", ""), color.lower().replace(" ", "_"), carrier.lower().replace(" ", "_")])
    path = re.sub(r"[^a-z0-9._/-]+", "-", path.lower()).strip("-")
    return {
        "model": model,
        "model_path": model_path,
        "display": display,
        "storage": storage,
        "color": color,
        "carrier": carrier,
        "path": path,
    }


async def discover_dimensions(page: Any) -> dict[str, list[dict[str, str]]]:
    return await page.evaluate(
        """
        () => {
          const excluded = new Set(%s);
          const groups = {};
          const isVisible = (node) => {
            if (!node) return false;
            const style = window.getComputedStyle(node);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            const rect = node.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          };
          for (const input of document.querySelectorAll('input[type="radio"]')) {
            if (!input.name || input.disabled) continue;
            const name = input.name;
            if (excluded.has(name)) continue;
            if (name !== 'carrierModel' && !name.toLowerCase().includes('dimension')) continue;
            if (/^acc_/i.test(name) || /payment|purchase_option/i.test(name)) continue;
            const labelNode = input.id ? document.querySelector(`label[for="${input.id}"]`) : input.closest('label');
            if (!isVisible(labelNode || input)) continue;
            const labelText = (labelNode?.innerText || input.getAttribute('aria-label') || input.value || '')
              .replace(/\\s+/g, ' ').trim();
            if (!groups[name]) groups[name] = [];
            if (groups[name].some(item => item.value === input.value)) continue;
            groups[name].push({
              value: input.value,
              label: labelText.split('\\n')[0].trim() || input.value,
              data_autom: input.getAttribute('data-autom') || ''
            });
          }
          return groups;
        }
        """
        % json.dumps(sorted(EXCLUDED_RADIO_NAMES))
    )


async def click_one(page: Any, name: str, option: dict[str, str]) -> None:
    value = option["value"]
    clicked = False
    for attempt in range(3):
        clicked = await page.evaluate(
            """
            ([name, value]) => {
              for (const input of document.querySelectorAll(`input[type="radio"][name="${name}"]`)) {
                if (input.disabled || input.value !== value) continue;
                const label = input.id ? document.querySelector(`label[for="${input.id}"]`) : input.closest('label');
                label?.scrollIntoView({ block: 'center', inline: 'center' });
                (label || input).click();
                input.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              }
              return false;
            }
            """,
            [name, value],
        )
        if clicked:
            break
        await page.wait_for_timeout(1200 + attempt * 800)
    if not clicked:
        tokens = ["carriermodel"] if name == "carrierModel" else [token for token in DIMENSION_ORDER_TOKENS if token in name.lower()]
        for attempt in range(3):
            clicked = await page.evaluate(
                """
                ([tokens, value]) => {
                  for (const input of document.querySelectorAll('input[type="radio"]')) {
                    if (input.disabled || input.value !== value) continue;
                    const lowered = input.name.toLowerCase();
                    if (tokens.length && !tokens.every((token) => lowered.includes(token))) continue;
                    const label = input.id ? document.querySelector(`label[for="${input.id}"]`) : input.closest('label');
                    label?.scrollIntoView({ block: 'center', inline: 'center' });
                    (label || input).click();
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                  }
                  return false;
                }
                """,
                [tokens, value],
            )
            if clicked:
                break
            await page.wait_for_timeout(1200 + attempt * 800)
    if not clicked:
        raise RuntimeError(f"option not found: {name}={value}")
    await page.wait_for_timeout(1200)


def steps_to_selection(steps: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {step["name"]: step for step in steps}


def selection_to_steps(selection: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    return [{"name": name, **option} for name, option in selection.items()]


def prefix_cache_key(prefix_steps: list[dict[str, str]]) -> str:
    signature = [{"name": step.get("name", ""), "value": step.get("value", "")} for step in prefix_steps]
    return json.dumps(signature, ensure_ascii=False, separators=(",", ":"))


def selection_path_key(selection: dict[str, dict[str, str]], catalog_item: dict[str, Any]) -> str:
    return map_record_fields(selection, catalog_item)["path"]


def cache_metadata(item: dict[str, Any], cache_kind: str, cache_key: str, payload: dict[str, Any]) -> tuple[Any, ...]:
    stamp = now()
    return (
        "apple",
        item.get("url", ""),
        item.get("path", ""),
        item.get("family", ""),
        item.get("name", ""),
        cache_kind,
        cache_key,
        json.dumps(payload, ensure_ascii=False),
        stamp,
        stamp,
    )


def upsert_cache(conn: sqlite3.Connection, item: dict[str, Any], cache_kind: str, cache_key: str, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO sku_capture_cache(platform, catalog_url, catalog_path, product_family, product_name,
          cache_kind, cache_key, payload_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(platform, catalog_url, cache_kind, cache_key) DO UPDATE SET
          catalog_path=excluded.catalog_path,
          product_family=excluded.product_family,
          product_name=excluded.product_name,
          payload_json=excluded.payload_json,
          updated_at=excluded.updated_at
        """,
        cache_metadata(item, cache_kind, cache_key, payload),
    )


def load_prefix_dimensions(item: dict[str, Any], prefix_steps: list[dict[str, str]]) -> dict[str, list[dict[str, str]]] | None:
    key = prefix_cache_key(prefix_steps)
    with db() as conn:
        row = conn.execute(
            """
            SELECT payload_json
            FROM sku_capture_cache
            WHERE platform='apple' AND catalog_url=? AND cache_kind='prefix_options' AND cache_key=?
            """,
            (item.get("url", ""), key),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE sku_capture_cache
            SET hit_count=hit_count + 1, updated_at=?
            WHERE platform='apple' AND catalog_url=? AND cache_kind='prefix_options' AND cache_key=?
            """,
            (now(), item.get("url", ""), key),
        )
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        return None
    dimensions = payload.get("dimensions")
    return dimensions if isinstance(dimensions, dict) else None


def store_prefix_dimensions(item: dict[str, Any], prefix_steps: list[dict[str, str]], dimensions: dict[str, list[dict[str, str]]]) -> None:
    with db() as conn:
        upsert_cache(
            conn,
            item,
            "prefix_options",
            prefix_cache_key(prefix_steps),
            {"prefix_steps": prefix_steps, "dimensions": dimensions},
        )


def load_cached_selection_paths(item: dict[str, Any], limit: int) -> list[dict[str, dict[str, str]]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT cache_key, payload_json
            FROM sku_capture_cache
            WHERE platform='apple' AND catalog_url=? AND cache_kind='selection_path'
            ORDER BY id ASC
            LIMIT ?
            """,
            (item.get("url", ""), limit),
        ).fetchall()
        conn.execute(
            """
            UPDATE sku_capture_cache
            SET hit_count=hit_count + 1, updated_at=?
            WHERE platform='apple' AND catalog_url=? AND cache_kind='selection_path'
            """,
            (now(), item.get("url", "")),
        )
    selections: list[dict[str, dict[str, str]]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        steps = payload.get("steps") or []
        if isinstance(steps, list):
            selections.append(steps_to_selection(steps))
    return selections


def load_cached_observed_options(item: dict[str, Any]) -> dict[str, list[str]]:
    with db() as conn:
        row = conn.execute(
            """
            SELECT payload_json
            FROM sku_capture_cache
            WHERE platform='apple' AND catalog_url=? AND cache_kind='observed_options' AND cache_key='all'
            """,
            (item.get("url", ""),),
        ).fetchone()
        if not row:
            return {}
        conn.execute(
            """
            UPDATE sku_capture_cache
            SET hit_count=hit_count + 1, updated_at=?
            WHERE platform='apple' AND catalog_url=? AND cache_kind='observed_options' AND cache_key='all'
            """,
            (now(), item.get("url", "")),
        )
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        return {}
    observed = payload.get("observed_options") or {}
    return observed if isinstance(observed, dict) else {}


def store_selection_cache(
    item: dict[str, Any],
    selections: list[dict[str, dict[str, str]]],
    observed_options: dict[str, list[str]],
) -> None:
    with db() as conn:
        for selection in selections:
            key = selection_path_key(selection, item)
            upsert_cache(conn, item, "selection_path", key, {"path": key, "steps": selection_to_steps(selection)})
        if observed_options:
            upsert_cache(conn, item, "observed_options", "all", {"observed_options": observed_options})


async def enumerate_selection_paths(
    context: Any, item: dict[str, Any], max_paths: int, use_cache: bool
) -> tuple[list[dict[str, dict[str, str]]], dict[str, list[str]]]:
    paths: list[dict[str, dict[str, str]]] = load_cached_selection_paths(item, max_paths) if use_cache else []
    seen_paths = {selection_path_key(selection, item) for selection in paths}
    cached_observed_options = load_cached_observed_options(item) if use_cache else {}
    observed_sets: dict[str, set[str]] = {
        name: set(values) for name, values in cached_observed_options.items()
    }

    async def dfs(prefix_steps: list[dict[str, str]]) -> None:
        if len(paths) >= max_paths:
            return
        dimensions = load_prefix_dimensions(item, prefix_steps) if use_cache else None
        if dimensions is None:
            page = await context.new_page()
            try:
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_timeout(3500)
                for step in prefix_steps:
                    try:
                        await click_one(page, step["name"], step)
                    except Exception:
                        return
                dimensions = await discover_dimensions(page)
                if use_cache:
                    store_prefix_dimensions(item, prefix_steps, dimensions)
            finally:
                await page.close()
        if dimensions is not None:
            for dim_name, opts in dimensions.items():
                observed_sets.setdefault(dim_name, set()).update(opt["value"] for opt in opts)
            selected = {step["name"] for step in prefix_steps}
            remaining = [
                dim_name
                for dim_name in sorted(dimensions.keys(), key=dimension_sort_key)
                if dim_name not in selected and is_config_dimension(dim_name)
            ]
            if not remaining:
                selection = steps_to_selection(prefix_steps)
                key = selection_path_key(selection, item)
                if key not in seen_paths:
                    paths.append(selection)
                    seen_paths.add(key)
                return
            next_dim = remaining[0]
            for option in dimensions.get(next_dim, []):
                if len(paths) >= max_paths:
                    return
                step = {"name": next_dim, **option}
                await dfs(prefix_steps + [step])

    if len(paths) < max_paths:
        await dfs([])
    observed_options = {name: sorted(values) for name, values in observed_sets.items()}
    if use_cache:
        store_selection_cache(item, paths, observed_options)
    return paths, observed_options


async def capture_selection_price(
    context: Any,
    item: dict[str, Any],
    selection: dict[str, dict[str, str]],
    observed_options: dict[str, list[str]],
    prefer_url_path: bool,
) -> dict[str, Any]:
    page = await context.new_page()
    strategy_used = "page_click"
    target_url = item["url"]
    try:
        if prefer_url_path:
            built = build_iphone_url(item["url"], selection)
            if built:
                await page.goto(built, wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_timeout(3500)
                if page.url.rstrip("/") == built.rstrip("/"):
                    strategy_used = "url_path"
                    target_url = page.url
                else:
                    await page.goto(item["url"], wait_until="domcontentloaded", timeout=90_000)
                    await page.wait_for_timeout(3000)
                    await click_selection(page, selection)
                    target_url = page.url
            else:
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_timeout(3000)
                await click_selection(page, selection)
                target_url = page.url
        else:
            await page.goto(item["url"], wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(3000)
            await click_selection(page, selection)
            target_url = page.url
        price_text, price_cents, _ = await read_price_from_page(page, min_cents=100)
        if not price_text:
            raise RuntimeError("price not found")
        record = make_record(
            run_id=0,
            catalog_item=item,
            selection=selection,
            url=target_url,
            price_text=price_text,
            price_cents=price_cents,
            ok=1,
            error="",
            strategy=strategy_used,
            observed_options=observed_options,
        )
        payload = json.loads(record["raw_json"])
        payload["title"] = await page.title()
        record["raw_json"] = json.dumps(payload, ensure_ascii=False)
        return record
    except Exception as exc:
        return make_record(
            run_id=0,
            catalog_item=item,
            selection=selection,
            url=target_url,
            price_text="",
            price_cents=None,
            ok=0,
            error=str(exc),
            strategy=strategy_used,
            observed_options=observed_options,
        )
    finally:
        await page.close()


def iphone_url_segment(name: str, value: str) -> str:
    lowered = name.lower()
    if "screensize" in lowered:
        mapping = {
            "6_3inch": "6.3-inch-display",
            "6_9inch": "6.9-inch-display",
            "6_1inch": "6.1-inch-display",
            "6_7inch": "6.7-inch-display",
            "6.1inch": "6.1-inch-display",
            "6.7inch": "6.7-inch-display",
        }
        return mapping.get(value, value.replace("_", "-"))
    if "color" in lowered or "finish" in lowered:
        mapping = {
            "cosmicorange": "cosmic-orange",
            "deepblue": "deep-blue",
            "spaceblack": "space-black",
            "cloudwhite": "cloud-white",
            "lightgold": "light-gold",
            "skyblue": "sky-blue",
            "lavender": "lavender",
            "sage": "sage",
            "mistblue": "mist-blue",
            "white": "white",
            "black": "black",
        }
        return mapping.get(value, value.replace("_", "-"))
    if "carrier" in lowered or name == "carrierModel":
        upper = value.upper()
        if upper.startswith("ATT") or "_ATT" in upper or upper == "ATT":
            return "att"
        if "BOOST" in upper:
            return "boost-mobile"
        if "TMOBILE" in upper or "T-MOBILE" in upper:
            return "t-mobile"
        if "VERIZON" in upper:
            return "verizon"
        if "UNLOCKED" in upper:
            return "unlocked"
        mapping = {
            "unlocked": "unlocked",
            "att": "att",
            "boostmobile": "boost-mobile",
            "boost-mobile": "boost-mobile",
            "tmobile": "t-mobile",
            "t-mobile": "t-mobile",
            "verizon": "verizon",
        }
        return mapping.get(value.lower(), value.replace("_", "-"))
    return value


def selection_dimension(selection: dict[str, dict[str, str]], *tokens: str) -> tuple[str, dict[str, str]] | tuple[None, None]:
    for name, option in selection.items():
        lowered = name.lower()
        if any(token in lowered for token in tokens):
            return name, option
    return None, None


def build_iphone_url(base_url: str, selection: dict[str, dict[str, str]]) -> str | None:
    parts: list[str] = []
    for tokens in (("screensize",), ("capacity",), ("color", "finish"), ("carrier",)):
        dim_name, option = selection_dimension(selection, *tokens)
        if not option or not dim_name:
            continue
        parts.append(iphone_url_segment(dim_name, option["value"]))
    if len(parts) < 4:
        return None
    return f"{base_url.rstrip('/')}/{'-'.join(parts)}"


def build_selection_path(selection: dict[str, dict[str, str]], strategy: str) -> list[dict[str, str]]:
    path: list[dict[str, str]] = []
    for name, option in selection.items():
        step = dimension_step_name(name)
        entry = {
            "step": step,
            "label": option_label(option),
            "value": option.get("value", ""),
            "selector": option.get("selector", ""),
            "data_autom": option.get("data_autom", ""),
            "page_section": name,
        }
        if strategy == "url_path":
            entry["url_segment"] = iphone_url_segment(name, option.get("value", ""))
        path.append(entry)
    path.extend(
        [
            {
                "step": "payment_option",
                "label": "Buy",
                "value": "fullPrice",
                "page_section": "Payment options",
            },
            {
                "step": "applecare",
                "label": "No AppleCare coverage",
                "value": "none",
                "page_section": "AppleCare coverage",
            },
        ]
    )
    return path


async def click_selection(page: Any, selection: dict[str, dict[str, str]], *, order: list[str] | None = None) -> None:
    names = order or list(selection.keys())
    for name in names:
        option = selection[name]
        await click_one(page, name, option)


async def read_price_from_page(page: Any, *, min_cents: int = 100) -> tuple[str, int | None, str]:
    selectors = [
        ".rc-prices-fullprice",
        "[data-autom='price']",
        ".as-price-currentprice",
        ".current_price",
        "[itemprop='price']",
    ]
    for selector in selectors:
        loc = page.locator(selector)
        try:
            if await loc.count() > 0:
                text = (await loc.first.inner_text(timeout=5_000)).strip()
                price_text, price_cents = parse_price(text, min_cents=1)
                if price_text:
                    return price_text, price_cents, text
        except Exception:
            continue

    try:
        await page.wait_for_selector(".rc-prices-fullprice, [data-autom='price'], .as-price-currentprice", timeout=12_000)
    except Exception:
        pass

    text = await page.locator("body").inner_text(timeout=30_000)
    price_text, price_cents = parse_price(text, min_cents=min_cents)
    if price_text:
        return price_text, price_cents, text

    script_price = await page.evaluate(
        """
        () => {
          const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')];
          for (const node of scripts) {
            try {
              const data = JSON.parse(node.textContent || '{}');
              const items = Array.isArray(data) ? data : [data];
              for (const item of items) {
                const offer = item.offers || (Array.isArray(item['@graph']) ? item['@graph'].find(x => x.offers)?.offers : null);
                const price = offer?.price || offer?.lowPrice;
                if (price) return '$' + Number(price).toFixed(2);
              }
            } catch (e) {}
          }
          return '';
        }
        """
    )
    if script_price:
        return script_price, price_to_cents(script_price), text
    return "", None, text


def make_record(
    *,
    run_id: int,
    catalog_item: dict[str, Any],
    selection: dict[str, dict[str, str]] | None,
    url: str,
    price_text: str,
    price_cents: int | None,
    ok: int,
    error: str,
    strategy: str,
    observed_options: dict[str, list[str]] | None = None,
    part_number: str = "",
) -> dict[str, Any]:
    if selection:
        mapped = map_record_fields(selection, catalog_item)
    else:
        url_parts = [part for part in urlparse(catalog_item.get("url", "")).path.split("/") if part]
        url_leaf = url_parts[-1] if url_parts else "unknown"
        fallback_model = catalog_item.get("name") or "Unknown"
        if catalog_item.get("kind") == "configurable":
            fallback_model = clean_catalog_product_name(fallback_model)
        mapped = {
            "model": fallback_model,
            "model_path": catalog_item.get("part_number") or url_leaf,
            "display": "",
            "storage": "standard",
            "color": "",
            "carrier": "none",
            "path": catalog_item.get("part_number") or url_leaf,
        }
    raw: dict[str, Any] = {
        "title": catalog_item.get("name"),
        "url": url,
        "price_text": price_text,
        "selection_strategy": strategy,
        "catalog_url": catalog_item.get("url"),
        "catalog_kind": catalog_item.get("kind"),
        "part_number": part_number or catalog_item.get("part_number") or "",
        "order_task_defaults": {
            "applecare": "none",
            "payment_option": "fullPrice",
            "quantity": 1,
            "stop_before_final_submit": True,
        },
    }
    if selection:
        raw["selection_path"] = build_selection_path(selection, strategy)
        if observed_options:
            raw["observed_options"] = observed_options
    return {
        "run_id": run_id,
        "platform": "apple",
        "product_family": catalog_item.get("family") or "Other",
        "model": mapped["model"],
        "model_path": mapped["model_path"],
        "display": mapped["display"],
        "storage": mapped["storage"],
        "color": mapped["color"],
        "carrier": mapped["carrier"],
        "path": mapped["path"],
        "url": url,
        "price_text": price_text,
        "price_cents": price_cents,
        "currency": "USD",
        "ok": ok,
        "error": error,
        "captured_at": now(),
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }


async def scrape_fixed_product(context: Any, item: dict[str, Any], sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        last_error = "price not found"
        for attempt in range(3):
            page = await context.new_page()
            try:
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_timeout(4500 + attempt * 1500)
                price_text, price_cents, _ = await read_price_from_page(page, min_cents=100)
                if not price_text:
                    raise RuntimeError("price not found")
                record = make_record(
                    run_id=0,
                    catalog_item=item,
                    selection=None,
                    url=page.url,
                    price_text=price_text,
                    price_cents=price_cents,
                    ok=1,
                    error="",
                    strategy="fixed_product_page",
                    part_number=item.get("part_number") or "",
                )
                record["raw_json"] = json.dumps(
                    {
                        **json.loads(record["raw_json"]),
                        "title": await page.title(),
                    },
                    ensure_ascii=False,
                )
                return record
            except Exception as exc:
                last_error = str(exc)
            finally:
                await page.close()
            await asyncio.sleep(1 + attempt)
        return make_record(
            run_id=0,
            catalog_item=item,
            selection=None,
            url=item["url"],
            price_text="",
            price_cents=None,
            ok=0,
            error=last_error,
            strategy="fixed_product_page",
            part_number=item.get("part_number") or "",
        )


async def scrape_configurable_product(
    context: Any,
    item: dict[str, Any],
    sem: asyncio.Semaphore,
    max_combos: int,
    use_cache: bool,
    skip_existing_sku_ok: bool,
) -> list[dict[str, Any]]:
    async with sem:
        records: list[dict[str, Any]] = []
        try:
            selections, observed_options = await enumerate_selection_paths(context, item, max_combos, use_cache)
            if not selections:
                raise RuntimeError("no configurable selections found")
            if skip_existing_sku_ok:
                existing_paths = existing_ok_sku_paths(item.get("url", ""))
                selections = [
                    selection for selection in selections
                    if selection_path_key(selection, item) not in existing_paths
                ]
                if not selections:
                    return []
            prefer_url_path = item.get("selection_strategy") == "url_path_or_page_click"
            for selection in selections:
                record = await capture_selection_price(
                    context,
                    item,
                    selection,
                    observed_options,
                    prefer_url_path=prefer_url_path,
                )
                records.append(record)
        except Exception as exc:
            records.append(
                make_record(
                    run_id=0,
                    catalog_item=item,
                    selection=None,
                    url=item["url"],
                    price_text="",
                    price_cents=None,
                    ok=0,
                    error=str(exc),
                    strategy="page_click",
                )
            )
        return records


async def scrape_configurable_product_with_timeout(
    context: Any,
    item: dict[str, Any],
    sem: asyncio.Semaphore,
    max_combos: int,
    use_cache: bool,
    skip_existing_sku_ok: bool,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    try:
        return await asyncio.wait_for(
            scrape_configurable_product(context, item, sem, max_combos, use_cache, skip_existing_sku_ok),
            timeout=max(30, timeout_seconds),
        )
    except asyncio.TimeoutError:
        return [
            make_record(
                run_id=0,
                catalog_item=item,
                selection=None,
                url=item["url"],
                price_text="",
                price_cents=None,
                ok=0,
                error=f"product capture timed out after {timeout_seconds}s",
                strategy=item.get("selection_strategy") or "page_click",
            )
        ]


def insert_record_rows(conn: sqlite3.Connection, run_id: int, records: list[dict[str, Any]]) -> None:
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


def update_run_progress(conn: sqlite3.Connection, run_id: int, *, status: str, note: str, finished: bool = False) -> dict[str, int]:
    counts = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(ok), 0) AS ok_count FROM sku_price_records WHERE run_id=?",
        (run_id,),
    ).fetchone()
    total = int(counts["total"] or 0)
    ok_count = int(counts["ok_count"] or 0)
    error_count = total - ok_count
    conn.execute(
        """UPDATE sku_price_runs
           SET status=?, finished_at=?, total=?, ok_count=?, error_count=?, note=?
           WHERE id=?""",
        (
            status,
            now() if finished else None,
            total,
            ok_count,
            error_count,
            note,
            run_id,
        ),
    )
    return {"total": total, "ok": ok_count, "error": error_count}


def append_records(run_id: int, records: list[dict[str, Any]], note: str, *, status: str = "running", finished: bool = False) -> dict[str, int]:
    with db() as conn:
        insert_record_rows(conn, run_id, records)
        return update_run_progress(conn, run_id, status=status, note=note, finished=finished)


def finish_run(run_id: int, note: str) -> dict[str, int]:
    with db() as conn:
        counts = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(ok), 0) AS ok_count FROM sku_price_records WHERE run_id=?",
            (run_id,),
        ).fetchone()
        total = int(counts["total"] or 0)
        ok_count = int(counts["ok_count"] or 0)
        status = "done" if ok_count == total else "partial"
        return update_run_progress(conn, run_id, status=status, note=note, finished=True)


def chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def export_docs() -> None:
    with db() as conn:
        runs = conn.execute("SELECT * FROM sku_price_runs ORDER BY id DESC").fetchall()
        records = conn.execute("SELECT * FROM sku_price_records ORDER BY id ASC").fetchall()
        skus = conn.execute("SELECT * FROM skus ORDER BY id ASC").fetchall()

    csv_path = DOCS_DIR / "APAGENT_ALL_SKU_PRICE_RECORDS.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "id",
                "run_id",
                "platform",
                "product_family",
                "model",
                "model_path",
                "display",
                "storage",
                "color",
                "carrier",
                "path",
                "url",
                "price_text",
                "price_cents",
                "currency",
                "ok",
                "error",
                "captured_at",
                "selection_strategy",
                "part_number",
            ]
        )
        for record in records:
            raw = json.loads(record["raw_json"] or "{}")
            writer.writerow(
                [
                    record["id"],
                    record["run_id"],
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
                    raw.get("selection_strategy", ""),
                    raw.get("part_number", ""),
                ]
            )

    md_path = DOCS_DIR / "APAGENT_CURRENT_SKU_LIST.md"
    lines = [
        "# APAgent Current SKU and Price Records",
        "",
        "This file is an exported index. Detailed Chinese instructions are in `APAGENT_SKU_PRICE_CAPTURE_GUIDE.md`.",
        "",
        "## Executable SKU Table",
        "",
        "| ID | Name | Product Key | Storage | Color | Connection | Part Number | Enabled |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for sku in skus:
        lines.append(
            f"| {sku['id']} | {sku['name']} | {sku['model_path']} | {sku['storage']} | {sku['color']} | {sku['carrier']} | {sku['part_number']} | {sku['enabled']} |"
        )
    lines.extend(
        [
            "",
            "## Capture Runs",
            "",
            "| Run ID | Source | Status | Total | OK | Error | Note |",
            "|---:|---|---|---:|---:|---:|---|",
        ]
    )
    for run in runs:
        lines.append(
            f"| {run['id']} | {run['source']} | {run['status']} | {run['total']} | {run['ok_count']} | {run['error_count']} | {run['note']} |"
        )
    lines.extend(
        [
            "",
            "## Price Records",
            "",
            f"Full CSV: `{csv_path}`",
            "",
            "| ID | Run | Family | Model | Storage | Color | Connection | Price | Path | Strategy |",
            "|---:|---:|---|---|---|---|---|---:|---|---|",
        ]
    )
    for record in records[-200:]:
        raw = json.loads(record["raw_json"] or "{}")
        lines.append(
            f"| {record['id']} | {record['run_id']} | {record['product_family']} | {record['model']} | {record['storage']} | {record['color']} | {record['carrier']} | {record['price_text']} | {record['path']} | {raw.get('selection_strategy', '')} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def existing_ok_by_catalog_url() -> dict[str, int]:
    counts: dict[str, int] = {}
    with db() as conn:
        rows = conn.execute(
            """
            SELECT json_extract(raw_json, '$.catalog_url') AS catalog_url, SUM(ok) AS ok_count
            FROM sku_price_records
            WHERE json_extract(raw_json, '$.catalog_url') IS NOT NULL
            GROUP BY catalog_url
            """
        ).fetchall()
        for row in rows:
            if row[0]:
                counts[str(row[0])] = int(row[1] or 0)
    return counts


def existing_ok_sku_paths(catalog_url: str) -> set[str]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT path
            FROM sku_price_records
            WHERE ok=1
              AND json_extract(raw_json, '$.catalog_url')=?
            """,
            (catalog_url,),
        ).fetchall()
    return {str(row["path"]) for row in rows if row["path"]}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Capture Apple catalog SKU prices and configuration matrices.")
    parser.add_argument("--catalog", default=str(CATALOG_JSON))
    parser.add_argument("--kind", choices=["fixed_product", "configurable", "all"], default="all")
    parser.add_argument("--family", default="", help="Optional family filter, e.g. iPhone or Accessories")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=25, help="Insert progress after each batch of catalog entries.")
    parser.add_argument("--max-combos-per-product", type=int, default=2000)
    parser.add_argument("--product-timeout-seconds", type=int, default=900, help="Timeout for one configurable product root.")
    parser.add_argument("--no-option-cache", action="store_true", help="Disable cached configurable option/path enumeration.")
    parser.add_argument("--skip-existing-sku-ok", action="store_true", help="For configurable products, skip selection paths that already have a successful price record.")
    parser.add_argument("--path-contains", default="", help="Only process catalog entries whose path contains this substring")
    parser.add_argument("--retry-failed-from-run", type=int, default=0, help="Re-scrape catalog entries that failed in a previous run id")
    parser.add_argument("--recapture-if-ok-below", type=int, default=0, help="Only capture configurable entries whose prior ok count is below this threshold")
    parser.add_argument("--skip-existing-ok", action="store_true", help="Skip catalog entries that already have at least one successful price record.")
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args()

    if args.export_only:
        export_docs()
        print(json.dumps({"exported": True}, ensure_ascii=False))
        return

    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    if args.retry_failed_from_run:
        with db() as conn:
            failed_urls = {
                row["url"]
                for row in conn.execute(
                    "SELECT url FROM sku_price_records WHERE run_id=? AND ok=0",
                    (args.retry_failed_from_run,),
                ).fetchall()
            }
        catalog = [item for item in catalog if item.get("url") in failed_urls]
        if not catalog:
            print(json.dumps({"error": "no failed catalog entries matched", "run_id": args.retry_failed_from_run}, ensure_ascii=False))
            return
    if args.family:
        catalog = [item for item in catalog if item.get("family") == args.family]
    if args.path_contains:
        catalog = [item for item in catalog if args.path_contains in (item.get("path") or "")]

    fixed_items = [item for item in catalog if item.get("kind") == "fixed_product"]
    configurable_items = pick_configurable_roots(catalog)

    if args.kind in {"fixed_product", "all"}:
        work_fixed = fixed_items
    else:
        work_fixed = []
    if args.kind in {"configurable", "all"}:
        work_configurable = configurable_items
    else:
        work_configurable = []

    if args.skip_existing_ok:
        ok_counts = existing_ok_by_catalog_url()
        work_fixed = [item for item in work_fixed if ok_counts.get(item.get("url", ""), 0) == 0]
        work_configurable = [item for item in work_configurable if ok_counts.get(item.get("url", ""), 0) == 0]

    if args.recapture_if_ok_below:
        ok_counts = existing_ok_by_catalog_url()
        work_configurable = [
            item for item in work_configurable if ok_counts.get(item.get("url", ""), 0) < args.recapture_if_ok_below
        ]

    if args.limit:
        work_fixed = work_fixed[: args.limit]
        work_configurable = work_configurable[: args.limit]

    with db() as conn:
        init_price_tables(conn)

    sem = asyncio.Semaphore(max(1, args.concurrency))
    batch_size = max(1, args.batch_size)
    all_records: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="en-US")

        if work_fixed:
            with db() as conn:
                cur = conn.execute(
                    "INSERT INTO sku_price_runs(platform, source, status, started_at, note) VALUES (?, ?, ?, ?, ?)",
                    ("apple", "APPLE_PRODUCT_CATALOG.fixed_product", "running", now(), f"fixed product pages 0/{len(work_fixed)}"),
                )
                fixed_run_id = int(cur.lastrowid)
            processed = 0
            for batch in chunks(work_fixed, batch_size):
                fixed_records = await asyncio.gather(*(scrape_fixed_product(context, item, sem) for item in batch))
                processed += len(batch)
                counts = append_records(
                    fixed_run_id,
                    fixed_records,
                    f"catalog fixed_product capture progress {processed}/{len(work_fixed)} entries",
                )
                all_records.extend(fixed_records)
                print(
                    json.dumps(
                        {
                            "fixed_run_id": fixed_run_id,
                            "processed": processed,
                            "planned": len(work_fixed),
                            **counts,
                        },
                        ensure_ascii=False,
                    )
                )
            counts = finish_run(fixed_run_id, f"catalog fixed_product capture ({len(work_fixed)} entries)")
            print(json.dumps({"fixed_run_id": fixed_run_id, "finished": True, **counts}, ensure_ascii=False))

        if work_configurable:
            with db() as conn:
                cur = conn.execute(
                    "INSERT INTO sku_price_runs(platform, source, status, started_at, note) VALUES (?, ?, ?, ?, ?)",
                    ("apple", "APPLE_PRODUCT_CATALOG.configurable", "running", now(), f"configurable matrices 0/{len(work_configurable)}"),
                )
                config_run_id = int(cur.lastrowid)
            processed = 0
            for batch in chunks(work_configurable, batch_size):
                config_records_nested = await asyncio.gather(
                    *(
                        scrape_configurable_product_with_timeout(
                            context,
                            item,
                            sem,
                            args.max_combos_per_product,
                            not args.no_option_cache,
                            args.skip_existing_sku_ok,
                            args.product_timeout_seconds,
                        )
                        for item in batch
                    )
                )
                config_records = [record for group in config_records_nested for record in group]
                processed += len(batch)
                counts = append_records(
                    config_run_id,
                    config_records,
                    f"catalog configurable matrix capture progress {processed}/{len(work_configurable)} roots",
                )
                all_records.extend(config_records)
                print(
                    json.dumps(
                        {
                            "config_run_id": config_run_id,
                            "products_processed": processed,
                            "products_planned": len(work_configurable),
                            **counts,
                        },
                        ensure_ascii=False,
                    )
                )
            counts = finish_run(config_run_id, f"catalog configurable matrix capture ({len(work_configurable)} roots)")
            print(json.dumps({"config_run_id": config_run_id, "finished": True, **counts}, ensure_ascii=False))

        await browser.close()

    export_docs()
    print(json.dumps({"records": len(all_records), "ok": sum(1 for x in all_records if x["ok"])}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
