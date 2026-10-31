from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = BASE_DIR.parents[0]
DOCS_DIR = WORKSPACE_DIR / "docs"
DB_PATH = BASE_DIR / "data" / "apagent.db"

BAD_IPHONE_SIZE_MODEL_RE = re.compile(r"^\d+(?:\s+\d+)?inch$", re.I)


def load_raw(raw_json: str) -> dict[str, Any]:
    if not raw_json:
        return {}
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        return {"_raw_json_error": "invalid json"}


def price_record_key(row: sqlite3.Row) -> tuple[str, ...]:
    return (
        row["platform"],
        row["product_family"],
        row["model"],
        row["model_path"],
        row["storage"],
        row["color"],
        row["carrier"],
        row["path"],
    )


def is_bad_iphone_model(row: sqlite3.Row) -> bool:
    return row["product_family"] == "iPhone" and bool(BAD_IPHONE_SIZE_MODEL_RE.match(row["model"].replace(" ", "")))


def promotion_state(row: sqlite3.Row, raw: dict[str, Any]) -> str:
    if not row["ok"] or row["price_cents"] is None:
        return "not_promotable"
    if is_bad_iphone_model(row):
        return "blocked_bad_model"
    if raw.get("part_number"):
        return "direct_part_number"
    if raw.get("selection_path"):
        return "selection_path_required"
    return "needs_order_path"


def record_score(row: sqlite3.Row, raw: dict[str, Any]) -> tuple[int, int, int, int, int]:
    return (
        1 if row["ok"] and row["price_cents"] is not None else 0,
        0 if is_bad_iphone_model(row) else 1,
        1 if raw.get("part_number") else 0,
        1 if raw.get("selection_path") else 0,
        int(row["run_id"]),
    )


@dataclass
class Analysis:
    rows: list[sqlite3.Row]
    raws: dict[int, dict[str, Any]]
    canonical: list[sqlite3.Row]
    canonical_raws: dict[int, dict[str, Any]]
    duplicate_keys: int
    duplicate_rows: int
    cache_stats: list[sqlite3.Row]


def analyze(conn: sqlite3.Connection) -> Analysis:
    rows = conn.execute("SELECT * FROM sku_price_records ORDER BY id ASC").fetchall()
    raws = {int(row["id"]): load_raw(row["raw_json"]) for row in rows}
    grouped: dict[tuple[str, ...], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[price_record_key(row)].append(row)

    canonical = []
    for group in grouped.values():
        canonical.append(max(group, key=lambda row: record_score(row, raws[int(row["id"])])))
    canonical.sort(key=lambda row: (row["product_family"], row["model"], row["storage"], row["color"], row["carrier"], row["id"]))

    duplicate_counts = [len(group) for group in grouped.values() if len(group) > 1]
    cache_stats: list[sqlite3.Row] = []
    try:
        cache_stats = conn.execute(
            """
            SELECT cache_kind, COUNT(*) AS count, COALESCE(SUM(hit_count), 0) AS hits
            FROM sku_capture_cache
            GROUP BY cache_kind
            ORDER BY cache_kind
            """
        ).fetchall()
    except sqlite3.OperationalError:
        cache_stats = []
    return Analysis(
        rows=rows,
        raws=raws,
        canonical=canonical,
        canonical_raws={int(row["id"]): raws[int(row["id"])] for row in canonical},
        duplicate_keys=len(duplicate_counts),
        duplicate_rows=sum(count - 1 for count in duplicate_counts),
        cache_stats=cache_stats,
    )


def write_canonical_csv(analysis: Analysis, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
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
        "promotion_state",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in analysis.canonical:
            raw = analysis.canonical_raws[int(row["id"])]
            writer.writerow(
                {
                    **{field: row[field] for field in fields if field in row.keys()},
                    "selection_strategy": raw.get("selection_strategy", ""),
                    "part_number": raw.get("part_number", ""),
                    "promotion_state": promotion_state(row, raw),
                }
            )


def write_report(analysis: Analysis, path: Path) -> None:
    rows = analysis.rows
    canonical = analysis.canonical
    latest_run = max((int(row["run_id"]) for row in rows), default=0)
    latest_rows = [row for row in rows if int(row["run_id"]) == latest_run]
    canonical_states = Counter(promotion_state(row, analysis.canonical_raws[int(row["id"])]) for row in canonical)
    latest_errors = Counter(
        (row["product_family"], row["error"] or "unknown")
        for row in latest_rows
        if not row["ok"] or row["price_cents"] is None
    )
    family_counts = Counter(row["product_family"] for row in latest_rows)
    family_ok = Counter(row["product_family"] for row in latest_rows if row["ok"] and row["price_cents"] is not None)
    strategy_counts = Counter(analysis.raws[int(row["id"])].get("selection_strategy") or "<empty>" for row in rows)
    bad_iphone_count = sum(1 for row in latest_rows if is_bad_iphone_model(row))

    lines = [
        "# APAgent SKU Price QA",
        "",
        "This report is generated from `APAgent/data/apagent.db` by `APAgent/scripts/sku_price_qa.py`.",
        "",
        "## Summary",
        "",
        f"- Total price records: `{len(rows)}`",
        f"- Latest run: `{latest_run}`",
        f"- Latest run records: `{len(latest_rows)}`",
        f"- Latest run priced OK: `{sum(1 for row in latest_rows if row['ok'] and row['price_cents'] is not None)}`",
        f"- Latest run errors/no-price: `{sum(1 for row in latest_rows if not row['ok'] or row['price_cents'] is None)}`",
        f"- Duplicate semantic keys across all runs: `{analysis.duplicate_keys}`",
        f"- Duplicate extra rows across all runs: `{analysis.duplicate_rows}`",
        f"- Canonical records after de-duplication: `{len(canonical)}`",
        f"- Latest run bad iPhone size-model rows: `{bad_iphone_count}`",
        "",
        "## Promotion States",
        "",
        "| State | Count | Meaning |",
        "|---|---:|---|",
    ]
    meanings = {
        "direct_part_number": "Can be promoted to `skus` with a part number.",
        "selection_path_required": "Has price and selection path; ordering must replay page selections or extract part number.",
        "needs_order_path": "Has price but lacks part number and selection path.",
        "blocked_bad_model": "Data is priced but model normalization is wrong.",
        "not_promotable": "Missing successful price.",
    }
    for state, count in canonical_states.most_common():
        lines.append(f"| {state} | {count} | {meanings[state]} |")

    lines.extend(["", "## Latest Run By Family", "", "| Family | Total | Priced OK | Error/No Price |", "|---|---:|---:|---:|"])
    for family, total in sorted(family_counts.items()):
        ok = family_ok[family]
        lines.append(f"| {family} | {total} | {ok} | {total - ok} |")

    lines.extend(["", "## Top Latest Run Errors", "", "| Count | Family | Error |", "|---:|---|---|"])
    for (family, error), count in latest_errors.most_common(20):
        lines.append(f"| {count} | {family} | {error[:140]} |")

    lines.extend(["", "## Selection Strategy Coverage", "", "| Strategy | Count |", "|---|---:|"])
    for strategy, count in strategy_counts.most_common():
        lines.append(f"| {strategy} | {count} |")

    if analysis.cache_stats:
        lines.extend(["", "## Capture Cache", "", "| Kind | Entries | Hits |", "|---|---:|---:|"])
        for row in analysis.cache_stats:
            lines.append(f"| {row['cache_kind']} | {row['count']} | {row['hits']} |")

    lines.extend(
        [
            "",
            "## Recommended Next Capture Order",
            "",
            "1. Re-run iPhone configurable capture after model normalization is fixed.",
            "2. Promote fixed-product records with `part_number` directly into a review queue.",
            "3. For configurable Mac/iPad/Watch, enumerate only currently enabled options after each selection.",
            "4. Stop treating unavailable combinations as failed SKUs; record them as unavailable option edges.",
            "5. Validate one representative SKU per family through Add to Bag before expanding the matrix.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze APAgent Apple SKU price records and export a canonical candidate set.")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--report", default=str(DOCS_DIR / "APAGENT_SKU_PRICE_QA.md"))
    parser.add_argument("--canonical-csv", default=str(DOCS_DIR / "APAGENT_CANONICAL_SKU_PRICE_RECORDS.csv"))
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    analysis = analyze(conn)
    write_canonical_csv(analysis, Path(args.canonical_csv))
    write_report(analysis, Path(args.report))
    print(
        json.dumps(
            {
                "records": len(analysis.rows),
                "canonical": len(analysis.canonical),
                "report": args.report,
                "canonical_csv": args.canonical_csv,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
