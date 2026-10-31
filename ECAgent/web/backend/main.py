from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parents[2]
EXPORTS_DIR = BASE_DIR / "exports"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "ecagent.db"

app = FastAPI(title="ECAgent Price Console", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174", "http://127.0.0.1:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/exports", StaticFiles(directory=EXPORTS_DIR), name="exports")


class BrandIn(BaseModel):
    name: str
    aliases: str = ""
    enabled: bool = True


class SeriesIn(BaseModel):
    name: str
    keywords: str = ""
    exclude_keywords: str = ""
    enabled: bool = True


class PlatformIn(BaseModel):
    code: str
    name: str
    enabled: bool = True


class RunRequest(BaseModel):
    brand_id: int | None = None
    series_id: int | None = None
    brand: str = "杜蕾斯"
    series: str = "001"
    keyword: str = "杜蕾斯 001"
    platforms: list[str] = Field(default_factory=lambda: ["jd", "tmall", "taobao"])
    limit: int = 8


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(x) for x in cur.fetchall()]


def row(cur: sqlite3.Cursor) -> dict[str, Any] | None:
    item = cur.fetchone()
    return dict(item) if item else None


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS brands (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              aliases TEXT NOT NULL DEFAULT '',
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS series (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              keywords TEXT NOT NULL DEFAULT '',
              exclude_keywords TEXT NOT NULL DEFAULT '',
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS platforms (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              code TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS price_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              brand_id INTEGER,
              series_id INTEGER,
              brand TEXT NOT NULL,
              series TEXT NOT NULL,
              keyword TEXT NOT NULL,
              platforms TEXT NOT NULL,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              output_dir TEXT,
              error TEXT NOT NULL DEFAULT '',
              FOREIGN KEY (brand_id) REFERENCES brands(id),
              FOREIGN KEY (series_id) REFERENCES series(id)
            );

            CREATE TABLE IF NOT EXISTS product_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL,
              platform TEXT NOT NULL,
              brand TEXT NOT NULL,
              series TEXT NOT NULL,
              keyword TEXT NOT NULL,
              rank_position INTEGER,
              product_id TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL DEFAULT '',
              product_url TEXT NOT NULL DEFAULT '',
              shop_name TEXT NOT NULL DEFAULT '',
              shop_type TEXT NOT NULL DEFAULT '',
              store_label TEXT NOT NULL DEFAULT '',
              price_raw TEXT NOT NULL DEFAULT '',
              final_price REAL,
              list_price REAL,
              piece_count INTEGER,
              unit_price REAL,
              sales_text TEXT NOT NULL DEFAULT '',
              location TEXT NOT NULL DEFAULT '',
              main_product_name TEXT NOT NULL DEFAULT '',
              product_combo TEXT NOT NULL DEFAULT '',
              detail_screenshot_path TEXT NOT NULL DEFAULT '',
              source_page_screenshot_path TEXT NOT NULL DEFAULT '',
              raw_json TEXT NOT NULL DEFAULT '{}',
              captured_at TEXT NOT NULL,
              FOREIGN KEY (run_id) REFERENCES price_runs(id)
            );
            """
        )
        seed_defaults(conn)


def seed_defaults(conn: sqlite3.Connection) -> None:
    stamp = now()
    for name in ["杜蕾斯", "杰士邦", "冈本", "名流", "赤尾", "beU"]:
        conn.execute(
            "INSERT OR IGNORE INTO brands(name, aliases, enabled, created_at, updated_at) VALUES (?, '', 1, ?, ?)",
            (name, stamp, stamp),
        )
    for name in ["001", "003", "AiR", "超薄", "经典", "持久", "玻尿酸", "情趣"]:
        conn.execute(
            "INSERT OR IGNORE INTO series(name, keywords, exclude_keywords, enabled, created_at, updated_at) VALUES (?, ?, '', 1, ?, ?)",
            (name, name, stamp, stamp),
        )
    for code, name in [("jd", "京东"), ("tmall", "天猫"), ("taobao", "淘宝")]:
        conn.execute(
            "INSERT OR IGNORE INTO platforms(code, name, enabled, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
            (code, name, stamp, stamp),
        )


@app.on_event("startup")
def startup() -> None:
    init_db()


def get_brand_series(req: RunRequest) -> tuple[str, str]:
    brand = req.brand.strip()
    series = req.series.strip()
    with db() as conn:
        if req.brand_id:
            found = row(conn.execute("SELECT * FROM brands WHERE id=?", (req.brand_id,)))
            if not found:
                raise HTTPException(status_code=404, detail="brand not found")
            brand = str(found["name"])
        if req.series_id:
            found = row(conn.execute("SELECT * FROM series WHERE id=?", (req.series_id,)))
            if not found:
                raise HTTPException(status_code=404, detail="series not found")
            series = str(found["name"])
    return brand, series


def write_entity(table: str, payload: BrandIn | SeriesIn | PlatformIn, item_id: int | None = None) -> dict[str, Any]:
    stamp = now()
    data = payload.model_dump()
    data["enabled"] = 1 if data.get("enabled") else 0
    with db() as conn:
        if table == "brands":
            if item_id:
                conn.execute(
                    "UPDATE brands SET name=?, aliases=?, enabled=?, updated_at=? WHERE id=?",
                    (data["name"], data["aliases"], data["enabled"], stamp, item_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO brands(name, aliases, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (data["name"], data["aliases"], data["enabled"], stamp, stamp),
                )
                item_id = int(cur.lastrowid)
        elif table == "series":
            if item_id:
                conn.execute(
                    "UPDATE series SET name=?, keywords=?, exclude_keywords=?, enabled=?, updated_at=? WHERE id=?",
                    (data["name"], data["keywords"], data["exclude_keywords"], data["enabled"], stamp, item_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO series(name, keywords, exclude_keywords, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (data["name"], data["keywords"], data["exclude_keywords"], data["enabled"], stamp, stamp),
                )
                item_id = int(cur.lastrowid)
        elif table == "platforms":
            if item_id:
                conn.execute(
                    "UPDATE platforms SET code=?, name=?, enabled=?, updated_at=? WHERE id=?",
                    (data["code"], data["name"], data["enabled"], stamp, item_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO platforms(code, name, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (data["code"], data["name"], data["enabled"], stamp, stamp),
                )
                item_id = int(cur.lastrowid)
        result = row(conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)))
        if not result:
            raise HTTPException(status_code=404, detail="item not found")
        return result


def list_entities(table: str) -> list[dict[str, Any]]:
    with db() as conn:
        return rows(conn.execute(f"SELECT * FROM {table} ORDER BY id"))


def delete_entity(table: str, item_id: int) -> dict[str, str]:
    with db() as conn:
        conn.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
    return {"status": "deleted"}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "db": str(DB_PATH)}


@app.get("/api/brands")
def brands() -> list[dict[str, Any]]:
    return list_entities("brands")


@app.post("/api/brands")
def create_brand(payload: BrandIn) -> dict[str, Any]:
    return write_entity("brands", payload)


@app.put("/api/brands/{item_id}")
def update_brand(item_id: int, payload: BrandIn) -> dict[str, Any]:
    return write_entity("brands", payload, item_id)


@app.delete("/api/brands/{item_id}")
def remove_brand(item_id: int) -> dict[str, str]:
    return delete_entity("brands", item_id)


@app.get("/api/series")
def series() -> list[dict[str, Any]]:
    return list_entities("series")


@app.post("/api/series")
def create_series(payload: SeriesIn) -> dict[str, Any]:
    return write_entity("series", payload)


@app.put("/api/series/{item_id}")
def update_series(item_id: int, payload: SeriesIn) -> dict[str, Any]:
    return write_entity("series", payload, item_id)


@app.delete("/api/series/{item_id}")
def remove_series(item_id: int) -> dict[str, str]:
    return delete_entity("series", item_id)


@app.get("/api/platforms")
def platforms() -> list[dict[str, Any]]:
    return list_entities("platforms")


@app.post("/api/platforms")
def create_platform(payload: PlatformIn) -> dict[str, Any]:
    return write_entity("platforms", payload)


@app.put("/api/platforms/{item_id}")
def update_platform(item_id: int, payload: PlatformIn) -> dict[str, Any]:
    return write_entity("platforms", payload, item_id)


@app.delete("/api/platforms/{item_id}")
def remove_platform(item_id: int) -> dict[str, str]:
    return delete_entity("platforms", item_id)


@app.get("/api/runs")
def runs(limit: int = 50) -> list[dict[str, Any]]:
    with db() as conn:
        return rows(conn.execute("SELECT * FROM price_runs ORDER BY id DESC LIMIT ?", (limit,)))


@app.post("/api/runs")
def create_run(req: RunRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    brand, series = get_brand_series(req)
    keyword = req.keyword.strip() or f"{brand} {series}".strip()
    platforms = [x for x in req.platforms if x in {"jd", "tmall", "taobao"}]
    if not platforms:
        raise HTTPException(status_code=400, detail="no supported platforms selected")
    stamp = now()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO price_runs(brand_id, series_id, brand, series, keyword, platforms, status, started_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (req.brand_id, req.series_id, brand, series, keyword, ",".join(platforms), stamp),
        )
        run_id = int(cur.lastrowid)
    background_tasks.add_task(run_capture, run_id, brand, series, keyword, platforms, max(1, min(req.limit, 30)))
    return {"id": run_id, "status": "running"}


@app.get("/api/products")
def products(
    platform: str | None = None,
    brand: str | None = None,
    series: str | None = None,
    run_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where = []
    args: list[Any] = []
    if platform:
        where.append("platform=?")
        args.append(platform)
    if brand:
        where.append("brand=?")
        args.append(brand)
    if series:
        where.append("series=?")
        args.append(series)
    if run_id:
        where.append("run_id=?")
        args.append(run_id)
    sql = "SELECT * FROM product_snapshots"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY captured_at DESC, id DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        return rows(conn.execute(sql, args))


@app.get("/api/trends")
def trends(
    platform: str | None = None,
    brand: str | None = None,
    series: str | None = None,
    product_id: str | None = None,
) -> dict[str, Any]:
    where = ["final_price IS NOT NULL"]
    args: list[Any] = []
    if platform:
        where.append("platform=?")
        args.append(platform)
    if brand:
        where.append("brand=?")
        args.append(brand)
    if series:
        where.append("series=?")
        args.append(series)
    if product_id:
        where.append("product_id=?")
        args.append(product_id)
    sql = f"""
      SELECT strftime('%Y-%m-%d %H:%M', captured_at) AS point_time,
             platform, product_id, title, shop_name,
             MIN(final_price) AS min_price,
             AVG(final_price) AS avg_price,
             COUNT(*) AS sample_count
      FROM product_snapshots
      WHERE {' AND '.join(where)}
      GROUP BY point_time, platform, product_id
      ORDER BY point_time, platform, min_price
    """
    with db() as conn:
        items = rows(conn.execute(sql, args))
    return {"items": items}


def run_capture(run_id: int, brand: str, series: str, keyword: str, platforms: list[str], limit: int) -> None:
    output_dir = EXPORTS_DIR / f"run_{run_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        for platform in platforms:
            script = BASE_DIR / f"cloak_{platform}_probe.py"
            if not script.is_file():
                raise FileNotFoundError(f"missing probe script: {script}")
            out_dir = output_dir / platform
            cmd = [
                sys.executable,
                str(script),
                "--keyword",
                keyword,
                "--limit",
                str(limit),
                "--out-dir",
                str(out_dir),
            ]
            subprocess.run(cmd, cwd=str(BASE_DIR), check=True)
            import_probe_result(run_id, platform, brand, series, keyword, out_dir)
        with db() as conn:
            conn.execute(
                "UPDATE price_runs SET status='done', finished_at=?, output_dir=? WHERE id=?",
                (now(), str(output_dir), run_id),
            )
    except Exception as exc:
        with db() as conn:
            conn.execute(
                "UPDATE price_runs SET status='failed', finished_at=?, output_dir=?, error=? WHERE id=?",
                (now(), str(output_dir), f"{type(exc).__name__}: {exc}", run_id),
            )


def import_probe_result(run_id: int, platform: str, brand: str, series: str, keyword: str, out_dir: Path) -> None:
    path = out_dir / f"{platform}_results.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    screenshot = str(data.get("screenshot") or "")
    captured = now()
    with db() as conn:
        for item in data.get("rows") or []:
            product_id = str(item.get("sku_id") or item.get("item_id") or item.get("product_id") or "")
            title = str(item.get("title") or "")
            final_price = as_float(item.get("final_price"))
            piece_count = parse_piece_count(title)
            unit_price = round(final_price / piece_count, 4) if final_price is not None and piece_count else None
            shop_name = str(item.get("shop_name") or "")
            conn.execute(
                """
                INSERT INTO product_snapshots(
                  run_id, platform, brand, series, keyword, rank_position, product_id,
                  title, product_url, shop_name, shop_type, store_label, price_raw,
                  final_price, list_price, piece_count, unit_price, sales_text, location,
                  main_product_name, product_combo, detail_screenshot_path,
                  source_page_screenshot_path, raw_json, captured_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                """,
                (
                    run_id,
                    platform,
                    brand,
                    series,
                    keyword,
                    item.get("rank_position"),
                    product_id,
                    title,
                    item.get("product_url") or "",
                    shop_name,
                    item.get("shop_type") or "",
                    store_label(platform, str(item.get("shop_type") or ""), shop_name),
                    item.get("price_raw") or "",
                    final_price,
                    None,
                    piece_count,
                    unit_price,
                    item.get("sales_text") or "",
                    item.get("location") or "",
                    title[:80],
                    infer_combo(title),
                    screenshot,
                    json.dumps(item, ensure_ascii=False),
                    item.get("captured_at") or captured,
                ),
            )


def as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_piece_count(text: str) -> int | None:
    text = re.sub(r"\b00[13]\b", "", text or "")
    values: list[int] = []
    for a, b in re.findall(r"(\d{1,3})\s*[xX*×]\s*(\d{1,3})\s*(?:只|片|枚|个)", text):
        total = int(a) * int(b)
        if 1 <= total <= 2000:
            values.append(total)
    for m in re.finditer(r"(?:共|合计)?\s*(\d{1,4})\s*(?:只|片|枚|个)", text):
        total = int(m.group(1))
        if 1 <= total <= 2000:
            values.append(total)
    return max(values) if values else None


def infer_combo(title: str) -> str:
    patterns = [
        r"[^，,。；;]*\d{1,4}\s*(?:只|片|枚|个)[^，,。；;]*",
        r"【[^】]*】",
    ]
    for pattern in patterns:
        m = re.search(pattern, title or "")
        if m:
            return m.group(0).strip()
    return ""


def store_label(platform: str, shop_type: str, shop_name: str) -> str:
    if platform == "jd" and shop_type == "self_operated":
        return "京东自营"
    if platform == "jd" and shop_type == "flagship":
        return "京东官旗"
    if platform in {"tmall", "taobao"} and shop_type == "self_operated":
        return "天猫自营"
    if platform in {"tmall", "taobao"} and shop_type == "flagship":
        return "天猫官旗"
    return shop_name or platform


init_db()
