from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "apagent.db"
GIFT_CARD_PATH = BASE_DIR / "gift_card.txt"
DEFAULT_CDP = "http://127.0.0.1:9223"
APPLE_PRO_URL = "https://www.apple.com/shop/buy-iphone/iphone-17-pro"
APPLE_HOMEPOD_MINI_URL = "https://www.apple.com/shop/buy-homepod/homepod-mini"

app = FastAPI(title="APAgent", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5184", "http://localhost:5184"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR.mkdir(parents=True, exist_ok=True)


class SKUIn(BaseModel):
    name: str
    model_path: str = "6.9-inch-display"
    storage: str = "256gb"
    color: str = "silver"
    carrier: str = "unlocked"
    part_number: str = "MFXG4LL/A"
    applecare: str = "none"
    payment_option: str = "fullPrice"
    option_config: str = ""
    enabled: bool = True


class IdentityIn(BaseModel):
    name: str
    first_name: str
    last_name: str
    email: str
    phone: str
    street: str
    street2: str = ""
    city: str
    state: str
    zip_code: str
    card_number: str = ""
    exp_month: str = ""
    exp_year: str = ""
    cvv: str = ""
    enabled: bool = True


class TaskIn(BaseModel):
    name: str
    sku_id: int
    identity_id: int
    cdp_url: str = DEFAULT_CDP
    payment_method: str = Field(default="credit_card", pattern="^(credit_card|gift_card)$")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
            CREATE TABLE IF NOT EXISTS skus (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              model_path TEXT NOT NULL,
              storage TEXT NOT NULL,
              color TEXT NOT NULL,
              carrier TEXT NOT NULL,
              part_number TEXT NOT NULL,
              applecare TEXT NOT NULL,
              payment_option TEXT NOT NULL,
              option_config TEXT NOT NULL DEFAULT '',
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS identities (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              first_name TEXT NOT NULL,
              last_name TEXT NOT NULL,
              email TEXT NOT NULL,
              phone TEXT NOT NULL,
              street TEXT NOT NULL,
              street2 TEXT NOT NULL DEFAULT '',
              city TEXT NOT NULL,
              state TEXT NOT NULL,
              zip_code TEXT NOT NULL,
              card_number TEXT NOT NULL DEFAULT '',
              exp_month TEXT NOT NULL DEFAULT '',
              exp_year TEXT NOT NULL DEFAULT '',
              cvv TEXT NOT NULL DEFAULT '',
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              sku_id INTEGER NOT NULL,
              identity_id INTEGER NOT NULL,
              cdp_url TEXT NOT NULL,
              payment_method TEXT NOT NULL DEFAULT 'credit_card',
              status TEXT NOT NULL,
              current_stage TEXT NOT NULL DEFAULT '',
              logs TEXT NOT NULL DEFAULT '[]',
              error TEXT NOT NULL DEFAULT '',
              review_url TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT,
              FOREIGN KEY (sku_id) REFERENCES skus(id),
              FOREIGN KEY (identity_id) REFERENCES identities(id)
            );
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
        ensure_column(conn, "skus", "option_config", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "order_tasks", "payment_method", "TEXT NOT NULL DEFAULT 'credit_card'")
        conn.execute("UPDATE skus SET option_config=? WHERE option_config=''", (default_option_config(),))
        stamp = now()
        if conn.execute("SELECT COUNT(*) FROM skus").fetchone()[0] == 0:
            conn.execute(
                """INSERT INTO skus(name, model_path, storage, color, carrier, part_number, applecare,
                   payment_option, option_config, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    "iPhone 17 Pro Max 256GB Silver Unlocked",
                    "6.9-inch-display",
                    "256gb",
                    "silver",
                    "unlocked",
                    "MFXG4LL/A",
                    "none",
                    "fullPrice",
                    default_option_config(),
                    stamp,
                    stamp,
                ),
            )
        if not row(conn.execute("SELECT * FROM skus WHERE part_number=?", ("MY5H2LL/A",))):
            conn.execute(
                """INSERT INTO skus(name, model_path, storage, color, carrier, part_number, applecare,
                   payment_option, option_config, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    "HomePod mini White",
                    "homepod-mini",
                    "",
                    "white",
                    "",
                    "MY5H2LL/A",
                    "none",
                    "fullPrice",
                    homepod_option_config(),
                    stamp,
                    stamp,
                ),
            )
        import_identity_from_file(conn)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {item["name"] for item in rows(conn.execute(f"PRAGMA table_info({table})"))}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def default_option_config() -> str:
    return json.dumps(
        {
            "models": [
                {"label": "iPhone 17 Pro Max", "value": "6.9-inch-display"},
                {"label": "iPhone 17 Pro", "value": "6.3-inch-display"},
            ],
            "storages": ["256gb", "512gb", "1tb", "2tb"],
            "colors": ["silver", "cosmic-orange", "deep-blue"],
            "carriers": [
                {"label": "Connect later", "value": "unlocked"},
                {"label": "AT&T", "value": "att"},
                {"label": "T-Mobile", "value": "t-mobile"},
                {"label": "Verizon", "value": "verizon"},
                {"label": "Boost Mobile", "value": "boost-mobile"},
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def homepod_option_config() -> str:
    return json.dumps(
        {
            "product_family": "HomePod",
            "product_url": "https://www.apple.com/shop/buy-homepod/homepod-mini/white",
            "colors": ["white", "midnight", "orange", "yellow", "blue"],
            "notes": "HomePod mini uses buy-homepod URL and does not require iPhone carrier/storage options.",
        },
        ensure_ascii=False,
        indent=2,
    )


def import_identity_from_file(conn: sqlite3.Connection) -> None:
    info_path = Path(r"C:\workspace\apple_info.txt")
    if not info_path.exists() or conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0]:
        return
    parsed = parse_info(info_path.read_text(encoding="utf-8-sig"))
    if not parsed.get("first_name") or not parsed.get("email"):
        return
    stamp = now()
    conn.execute(
        """INSERT INTO identities(name, first_name, last_name, email, phone, street, street2, city, state,
           zip_code, card_number, exp_month, exp_year, cvv, enabled, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (
            parsed["name"],
            parsed["first_name"],
            parsed["last_name"],
            parsed["email"],
            parsed["phone"],
            parsed["street"],
            parsed.get("street2", ""),
            parsed["city"],
            parsed["state"],
            parsed["zip_code"],
            parsed["card_number"],
            parsed["exp_month"],
            parsed["exp_year"],
            parsed["cvv"],
            stamp,
            stamp,
        ),
    )


def parse_info(text: str) -> dict[str, str]:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", text)
    zip_candidates = re.findall(r"\b\d{5}(?:-\d{4})?\b", text)
    zip_code = ""
    state_zip = re.search(r"\b[A-Z]{2}\s+(\d{5}(?:-\d{4})?)\b", text, re.I)
    if state_zip:
        zip_code = state_zip.group(1)
    elif zip_candidates:
        # Prefer the last ZIP-like token because addresses can start with a 5-digit street number.
        zip_code = zip_candidates[-1]
    full_name = next((x for x in lines if re.fullmatch(r"[A-Za-z]+\s+[A-Za-z]+(?:\s+[A-Za-z]+)?", x)), "")
    parts = full_name.split()
    phone = ""
    card = ""
    for line in lines:
        digits = re.sub(r"\D", "", line)
        if len(digits) in (15, 16) and not card:
            card = digits
        if len(digits) == 13 and digits.startswith("86") and not phone:
            phone = digits[-10:]
        if len(digits) == 11 and digits.startswith("1"):
            phone = digits[-10:]
        elif len(digits) == 10 and not phone:
            phone = digits
    dates = re.findall(r"\b(0?[1-9]|1[0-2])\s*/\s*(\d{2,4})\b", text)
    exp_month = exp_year = ""
    if dates:
        exp_month, exp_year = dates[-1]
        exp_month = exp_month.zfill(2)
        exp_year = f"20{exp_year}" if len(exp_year) == 2 else exp_year
    cvv = ""
    for line in lines:
        if "/" in line:
            continue
        nums = re.findall(r"\b\d{3,4}\b", line)
        if nums:
            cvv = nums[-1]
    return {
        "name": full_name or "Default Buyer",
        "first_name": parts[0] if parts else "",
        "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
        "email": email_match.group(0) if email_match else "",
        "phone": phone,
        "street": "19030 se highway 212",
        "street2": "",
        "city": "Damascus",
        "state": "OR",
        "zip_code": zip_code or "97089",
        "card_number": card,
        "exp_month": exp_month,
        "exp_year": exp_year,
        "cvv": cvv,
    }


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "db": str(DB_PATH)}


def mask_identity(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    card = str(out.get("card_number") or "")
    out["card_number_masked"] = f"**** **** **** {card[-4:]}" if card else ""
    out["has_card"] = bool(card and out.get("exp_month") and out.get("exp_year") and out.get("cvv"))
    out.pop("card_number", None)
    out.pop("cvv", None)
    return out


@app.get("/api/skus")
def get_skus() -> list[dict[str, Any]]:
    with db() as conn:
        return rows(conn.execute("SELECT * FROM skus ORDER BY id DESC"))


@app.post("/api/skus")
def create_sku(payload: SKUIn) -> dict[str, Any]:
    stamp = now()
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO skus(name, model_path, storage, color, carrier, part_number, applecare,
               payment_option, option_config, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload.name,
                payload.model_path,
                payload.storage,
                payload.color,
                payload.carrier,
                payload.part_number,
                payload.applecare,
                payload.payment_option,
                payload.option_config,
                int(payload.enabled),
                stamp,
                stamp,
            ),
        )
        return row(conn.execute("SELECT * FROM skus WHERE id=?", (cur.lastrowid,))) or {}


@app.put("/api/skus/{sku_id}")
def update_sku(sku_id: int, payload: SKUIn) -> dict[str, Any]:
    stamp = now()
    with db() as conn:
        if not row(conn.execute("SELECT * FROM skus WHERE id=?", (sku_id,))):
            raise HTTPException(status_code=404, detail="sku not found")
        conn.execute(
            """UPDATE skus SET name=?, model_path=?, storage=?, color=?, carrier=?, part_number=?,
               applecare=?, payment_option=?, option_config=?, enabled=?, updated_at=? WHERE id=?""",
            (
                payload.name,
                payload.model_path,
                payload.storage,
                payload.color,
                payload.carrier,
                payload.part_number,
                payload.applecare,
                payload.payment_option,
                payload.option_config,
                int(payload.enabled),
                stamp,
                sku_id,
            ),
        )
        return row(conn.execute("SELECT * FROM skus WHERE id=?", (sku_id,))) or {}


@app.delete("/api/skus/{sku_id}")
def delete_sku(sku_id: int) -> dict[str, str]:
    with db() as conn:
        if not row(conn.execute("SELECT * FROM skus WHERE id=?", (sku_id,))):
            raise HTTPException(status_code=404, detail="sku not found")
        used = conn.execute("SELECT COUNT(*) FROM order_tasks WHERE sku_id=?", (sku_id,)).fetchone()[0]
        if used:
            raise HTTPException(status_code=409, detail="sku is used by tasks")
        conn.execute("DELETE FROM skus WHERE id=?", (sku_id,))
    return {"status": "deleted"}


@app.get("/api/identities")
def get_identities() -> list[dict[str, Any]]:
    with db() as conn:
        return [mask_identity(x) for x in rows(conn.execute("SELECT * FROM identities ORDER BY id DESC"))]


@app.post("/api/identities")
def create_identity(payload: IdentityIn) -> dict[str, Any]:
    stamp = now()
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO identities(name, first_name, last_name, email, phone, street, street2, city, state,
               zip_code, card_number, exp_month, exp_year, cvv, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload.name,
                payload.first_name,
                payload.last_name,
                payload.email,
                payload.phone,
                payload.street,
                payload.street2,
                payload.city,
                payload.state,
                payload.zip_code,
                payload.card_number,
                payload.exp_month,
                payload.exp_year,
                payload.cvv,
                int(payload.enabled),
                stamp,
                stamp,
            ),
        )
        return mask_identity(row(conn.execute("SELECT * FROM identities WHERE id=?", (cur.lastrowid,))) or {})


@app.put("/api/identities/{identity_id}")
def update_identity(identity_id: int, payload: IdentityIn) -> dict[str, Any]:
    stamp = now()
    with db() as conn:
        current = row(conn.execute("SELECT * FROM identities WHERE id=?", (identity_id,)))
        if not current:
            raise HTTPException(status_code=404, detail="identity not found")
        card_number = payload.card_number or current.get("card_number", "")
        cvv = payload.cvv or current.get("cvv", "")
        conn.execute(
            """UPDATE identities SET name=?, first_name=?, last_name=?, email=?, phone=?, street=?,
               street2=?, city=?, state=?, zip_code=?, card_number=?, exp_month=?, exp_year=?,
               cvv=?, enabled=?, updated_at=? WHERE id=?""",
            (
                payload.name,
                payload.first_name,
                payload.last_name,
                payload.email,
                payload.phone,
                payload.street,
                payload.street2,
                payload.city,
                payload.state,
                payload.zip_code,
                card_number,
                payload.exp_month,
                payload.exp_year,
                cvv,
                int(payload.enabled),
                stamp,
                identity_id,
            ),
        )
        return mask_identity(row(conn.execute("SELECT * FROM identities WHERE id=?", (identity_id,))) or {})


@app.delete("/api/identities/{identity_id}")
def delete_identity(identity_id: int) -> dict[str, str]:
    with db() as conn:
        if not row(conn.execute("SELECT * FROM identities WHERE id=?", (identity_id,))):
            raise HTTPException(status_code=404, detail="identity not found")
        used = conn.execute("SELECT COUNT(*) FROM order_tasks WHERE identity_id=?", (identity_id,)).fetchone()[0]
        if used:
            raise HTTPException(status_code=409, detail="identity is used by tasks")
        conn.execute("DELETE FROM identities WHERE id=?", (identity_id,))
    return {"status": "deleted"}


@app.get("/api/tasks")
def get_tasks() -> list[dict[str, Any]]:
    with db() as conn:
        return rows(
            conn.execute(
                """SELECT t.*, s.name AS sku_name, i.name AS identity_name
                   FROM order_tasks t
                   JOIN skus s ON s.id=t.sku_id
                   JOIN identities i ON i.id=t.identity_id
                   ORDER BY t.id DESC"""
            )
        )


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    with db() as conn:
        task = row(conn.execute("SELECT * FROM order_tasks WHERE id=?", (task_id,)))
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    task["logs"] = json.loads(task.get("logs") or "[]")
    return task


@app.get("/api/sku-price-runs")
def get_sku_price_runs() -> list[dict[str, Any]]:
    with db() as conn:
        return rows(conn.execute("SELECT * FROM sku_price_runs ORDER BY id DESC LIMIT 50"))


@app.get("/api/sku-price-records")
def get_sku_price_records(run_id: int | None = None, limit: int = 300) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    with db() as conn:
        if run_id:
            return rows(
                conn.execute(
                    "SELECT * FROM sku_price_records WHERE run_id=? ORDER BY id ASC LIMIT ?",
                    (run_id, limit),
                )
            )
        latest = row(conn.execute("SELECT id FROM sku_price_runs ORDER BY id DESC LIMIT 1"))
        if not latest:
            return []
        return rows(
            conn.execute(
                "SELECT * FROM sku_price_records WHERE run_id=? ORDER BY id ASC LIMIT ?",
                (latest["id"], limit),
            )
        )


@app.post("/api/tasks")
def create_task(payload: TaskIn) -> dict[str, Any]:
    stamp = now()
    validate_payment_method(payload.payment_method)
    with db() as conn:
        if not row(conn.execute("SELECT * FROM skus WHERE id=?", (payload.sku_id,))):
            raise HTTPException(status_code=404, detail="sku not found")
        if not row(conn.execute("SELECT * FROM identities WHERE id=?", (payload.identity_id,))):
            raise HTTPException(status_code=404, detail="identity not found")
        cur = conn.execute(
            """INSERT INTO order_tasks(name, sku_id, identity_id, cdp_url, payment_method, status, current_stage,
               logs, created_at) VALUES (?, ?, ?, ?, ?, 'created', 'created', '[]', ?)""",
            (payload.name, payload.sku_id, payload.identity_id, payload.cdp_url, payload.payment_method, stamp),
        )
        return row(conn.execute("SELECT * FROM order_tasks WHERE id=?", (cur.lastrowid,))) or {}


@app.put("/api/tasks/{task_id}")
def update_task(task_id: int, payload: TaskIn) -> dict[str, Any]:
    validate_payment_method(payload.payment_method)
    with db() as conn:
        task = row(conn.execute("SELECT * FROM order_tasks WHERE id=?", (task_id,)))
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task["status"] == "running":
            raise HTTPException(status_code=409, detail="running task cannot be edited")
        if not row(conn.execute("SELECT * FROM skus WHERE id=?", (payload.sku_id,))):
            raise HTTPException(status_code=404, detail="sku not found")
        if not row(conn.execute("SELECT * FROM identities WHERE id=?", (payload.identity_id,))):
            raise HTTPException(status_code=404, detail="identity not found")
        conn.execute(
            "UPDATE order_tasks SET name=?, sku_id=?, identity_id=?, cdp_url=?, payment_method=? WHERE id=?",
            (payload.name, payload.sku_id, payload.identity_id, payload.cdp_url, payload.payment_method, task_id),
        )
        return row(conn.execute("SELECT * FROM order_tasks WHERE id=?", (task_id,))) or {}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int) -> dict[str, str]:
    with db() as conn:
        task = row(conn.execute("SELECT * FROM order_tasks WHERE id=?", (task_id,)))
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task["status"] == "running":
            raise HTTPException(status_code=409, detail="running task cannot be deleted")
        conn.execute("DELETE FROM order_tasks WHERE id=?", (task_id,))
    return {"status": "deleted"}


@app.post("/api/tasks/{task_id}/run")
def run_task(task_id: int, bg: BackgroundTasks) -> dict[str, str]:
    with db() as conn:
        task = row(conn.execute("SELECT * FROM order_tasks WHERE id=?", (task_id,)))
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task["status"] == "running":
            raise HTTPException(status_code=409, detail="task already running")
        conn.execute(
            "UPDATE order_tasks SET status='running', current_stage='queued', started_at=?, error='', logs='[]' WHERE id=?",
            (now(), task_id),
        )
    bg.add_task(run_order_task_sync, task_id)
    return {"status": "started"}


def task_log(task_id: int, stage: str, message: str) -> None:
    with db() as conn:
        task = row(conn.execute("SELECT logs FROM order_tasks WHERE id=?", (task_id,)))
        logs = json.loads((task or {}).get("logs") or "[]")
        logs.append({"time": now(), "stage": stage, "message": sanitize_sensitive(message)})
        conn.execute(
            "UPDATE order_tasks SET current_stage=?, logs=? WHERE id=?",
            (stage, json.dumps(logs, ensure_ascii=False), task_id),
        )


def sanitize_sensitive(message: str) -> str:
    value = str(message or "")
    try:
        gift_code = read_gift_card_code()
        if gift_code:
            value = value.replace(gift_code, "[GIFT_CARD_REDACTED]")
    except Exception:
        pass
    value = re.sub(r'\b\d{12,19}\b', "[CARD_REDACTED]", value)
    value = re.sub(r'fill\("([^"]{10,})"\)', 'fill("[REDACTED]")', value)
    return value


def elapsed(started_at: float) -> str:
    return f"{time.perf_counter() - started_at:.1f}s"


def run_order_task_sync(task_id: int) -> None:
    asyncio.run(run_order_task(task_id))


def validate_payment_method(value: str) -> None:
    if value not in {"credit_card", "gift_card"}:
        raise HTTPException(status_code=422, detail="payment_method must be credit_card or gift_card")


def build_product_url(sku: dict[str, Any]) -> str:
    option_config = parse_json_object(sku.get("option_config") or "")
    explicit_url = option_config.get("product_url")
    if explicit_url:
        return str(explicit_url)
    if sku.get("model_path") == "homepod-mini":
        return f"{APPLE_HOMEPOD_MINI_URL}/{sku.get('color') or 'white'}"
    return f"{APPLE_PRO_URL}/{sku['model_path']}-{sku['storage']}-{sku['color']}-{sku['carrier']}"


def parse_json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_gift_card_code() -> str:
    if not GIFT_CARD_PATH.exists():
        raise RuntimeError("gift card file not found")
    for line in GIFT_CARD_PATH.read_text(encoding="utf-8-sig").splitlines():
        code = line.strip()
        if code:
            return code
    raise RuntimeError("gift card file is empty")


async def add_product_to_bag(page: Any, part_number: str) -> str:
    try:
        await page.wait_for_function("() => Boolean(window.acStore && window.acStore.addItem)", timeout=15_000)
        return await page.evaluate("(part) => window.acStore.addItem(part)", part_number)
    except Exception:
        await click_visible_any(page, r"Add to Bag")
        await wait_until_text(page, r"Added to Bag|Go to Bag|Checkout|Check Out", timeout=15_000)
        return "clicked Add to Bag"


async def apply_gift_card_payment(page: Any) -> None:
    code = read_gift_card_code()
    open_button = page.locator(
        "#checkout\\.billing\\.billingOptions\\.selectedBillingOptions\\.giftCard\\.giftCardInput\\.resetFields"
    )
    gift_input = page.locator(
        "#checkout\\.billing\\.billingOptions\\.selectedBillingOptions\\.giftCard\\.giftCardInput\\.giftCard"
    ).first
    if await open_button.count():
        await open_button.first.scroll_into_view_if_needed(timeout=10_000)
        await open_button.first.click(timeout=10_000, force=True)
    elif not await is_visible(gift_input):
        loc = page.get_by_text(re.compile(r"Enter your gift card number|Apply an Apple Gift Card", re.I)).first
        await loc.scroll_into_view_if_needed(timeout=10_000)
        await loc.click(timeout=10_000, force=True)
    try:
        await gift_input.wait_for(state="visible", timeout=10_000)
    except Exception:
        raise RuntimeError("gift card input did not expand")
    await gift_input.fill(code, timeout=10_000)
    await page.locator(
        "#checkout\\.billing\\.billingOptions\\.selectedBillingOptions\\.giftCard\\.giftCardInput\\.applyGiftCard"
    ).click(timeout=20_000)
    await wait_until_text(page, r"Gift Card \d+|remaining balance|another form of payment|invalid gift card|not valid|could not apply", timeout=20_000)
    body = await page.locator("body").inner_text(timeout=30_000)
    assert_no_gift_card_balance_error(body)
    if re.search(r"invalid gift card|not valid|problem with this gift card|could not apply", body, re.I):
        raise RuntimeError("gift card could not be applied")
    if "Please choose a payment method" in body and not re.search(r"Gift Card \d+", body, re.I):
        raise RuntimeError("gift card was not applied")


def assert_no_gift_card_balance_error(body: str) -> None:
    patterns = [
        r"remaining balance",
        r"another form of payment",
        r"cover the remaining",
        r"additional payment",
    ]
    if any(re.search(pattern, body, re.I) for pattern in patterns):
        raise RuntimeError("gift card balance insufficient")


async def run_order_task(task_id: int) -> None:
    try:
        run_started = time.perf_counter()
        with db() as conn:
            task = row(conn.execute("SELECT * FROM order_tasks WHERE id=?", (task_id,)))
            sku = row(conn.execute("SELECT * FROM skus WHERE id=?", (task["sku_id"],)))
            ident = row(conn.execute("SELECT * FROM identities WHERE id=?", (task["identity_id"],)))
        if not task or not sku or not ident:
            raise RuntimeError("missing task dependencies")
        from playwright.async_api import async_playwright

        step_started = time.perf_counter()
        task_log(task_id, "connect_browser", "Connecting browser")
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(task["cdp_url"])
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            task_log(task_id, "connect_browser", f"Browser connected in {elapsed(step_started)}")

            product_url = build_product_url(sku)
            step_started = time.perf_counter()
            task_log(task_id, "open_product", product_url)
            await page.goto(product_url, wait_until="domcontentloaded", timeout=90_000)
            await wait_for_product_ready(page, sku)
            task_log(task_id, "open_product", f"Product page ready in {elapsed(step_started)}")

            if sku.get("model_path") != "homepod-mini":
                step_started = time.perf_counter()
                task_log(task_id, "select_applecare", "Selecting No AppleCare coverage")
                try:
                    await page.locator('input[data-autom="noapplecare"], input[name="applecare-options"]').last.check(force=True, timeout=10_000)
                except Exception:
                    task_log(task_id, "select_applecare", "AppleCare option not visible; continuing")
                task_log(task_id, "select_applecare", f"AppleCare step finished in {elapsed(step_started)}")

            step_started = time.perf_counter()
            task_log(task_id, "add_to_bag", f"Adding part {sku['part_number']}")
            result = await add_product_to_bag(page, sku["part_number"])
            task_log(task_id, "add_to_bag", f"{result}; finished in {elapsed(step_started)}")

            step_started = time.perf_counter()
            task_log(task_id, "bag", "Opening bag and setting quantity to 1")
            await page.goto("https://www.apple.com/shop/bag", wait_until="domcontentloaded", timeout=90_000)
            await wait_until_text(page, r"Check Out|Checkout|Your Bag|Bag", timeout=30_000)
            await normalize_bag_quantity(page)
            task_log(task_id, "bag", f"Bag ready in {elapsed(step_started)}")

            step_started = time.perf_counter()
            task_log(task_id, "checkout", "Clicking checkout")
            await click_visible(page, "button", r"Check Out")
            await wait_for_checkout_ready(page)
            task_log(task_id, "checkout", f"Checkout loaded in {elapsed(step_started)}")

            step_started = time.perf_counter()
            task_log(task_id, "fulfillment", f"Applying ZIP {ident['zip_code']}")
            zip_input = page.locator("#checkout\\.fulfillment\\.deliveryTab\\.delivery\\.deliveryLocation\\.address\\.postalCode")
            if await zip_input.count() and await zip_input.first.is_visible(timeout=1000):
                await zip_input.fill(ident["zip_code"])
                await page.locator("#checkout\\.fulfillment\\.deliveryTab\\.delivery\\.deliveryLocation\\.address\\.calculate").click()
            await page.get_by_text(re.compile(r"Continue with other payment options|other payment options", re.I)).first.wait_for(
                state="visible", timeout=30_000
            )
            await click_visible_any(page, r"Continue with other payment options|other payment options")
            await page.locator("#checkout\\.shipping\\.addressSelector\\.newAddress\\.address\\.firstName").wait_for(
                state="visible", timeout=45_000
            )
            task_log(task_id, "fulfillment", f"Fulfillment finished in {elapsed(step_started)}")

            step_started = time.perf_counter()
            task_log(task_id, "shipping", "Filling shipping/contact fields")
            await fill_id(page, "checkout.shipping.addressSelector.newAddress.address.firstName", ident["first_name"])
            await fill_id(page, "checkout.shipping.addressSelector.newAddress.address.lastName", ident["last_name"])
            await fill_id(page, "checkout.shipping.addressSelector.newAddress.address.street", ident["street"])
            await fill_id(page, "checkout.shipping.addressSelector.newAddress.address.zipLookup.postalCode", ident["zip_code"])
            await wait_for_city_state_ready(page)
            city_select = page.locator("#checkout\\.shipping\\.addressSelector\\.newAddress\\.address\\.zipLookup\\.zipLookupCityState")
            if await city_select.count():
                await city_select.select_option("Other")
                await wait_for_city_state_ready(page)
            await fill_id(page, "checkout.shipping.addressSelector.newAddress.address.zipLookup.city", ident["city"])
            await fill_id(page, "checkout.shipping.addressSelector.newAddress.address.zipLookup.state", ident["state"])
            await fill_id(page, "checkout.shipping.addressContactEmail.address.emailAddress", ident["email"])
            await fill_id(page, "checkout.shipping.addressContactPhone.address.fullDaytimePhone", only_digits(ident["phone"])[-10:])
            await page.locator("#rs-checkout-continue-button-bottom").click()
            await wait_for_billing_ready(page)
            task_log(task_id, "shipping", f"Shipping/contact finished in {elapsed(step_started)}")

            step_started = time.perf_counter()
            if task.get("payment_method", "credit_card") == "gift_card":
                task_log(task_id, "payment", "Applying gift card")
                await apply_gift_card_payment(page)
            else:
                task_log(task_id, "payment", "Filling payment fields")
                await page.locator("#checkout\\.billing\\.billingoptions\\.credit").check(force=True)
                await page.locator("#checkout\\.billing\\.billingOptions\\.selectedBillingOptions\\.creditCard\\.cardInputs\\.cardInput-0\\.cardNumber").wait_for(state="visible", timeout=20_000)
                await fill_id(page, "checkout.billing.billingOptions.selectedBillingOptions.creditCard.cardInputs.cardInput-0.cardNumber", only_digits(ident["card_number"]))
                await fill_id(page, "checkout.billing.billingOptions.selectedBillingOptions.creditCard.cardInputs.cardInput-0.expiration", f"{ident['exp_month']}/{str(ident['exp_year'])[-2:]}")
                await fill_id(page, "checkout.billing.billingOptions.selectedBillingOptions.creditCard.cardInputs.cardInput-0.securityCode", ident["cvv"])
            task_log(task_id, "payment", f"Payment step finished in {elapsed(step_started)}")

            step_started = time.perf_counter()
            task_log(task_id, "review", "Continue to review; stop before final submission")
            await page.locator("#rs-checkout-continue-button-bottom").click()
            await wait_until_text(page, r"Place Order|Review Your Order|Place Your Order|valid card number|remaining balance|another form of payment", timeout=30_000)
            body = await page.locator("body").inner_text(timeout=30_000)
            if task.get("payment_method", "credit_card") == "gift_card":
                assert_no_gift_card_balance_error(body)
            if "Please enter a valid card number" in body:
                raise RuntimeError("Apple rejected the card number as invalid")
            if re.search(r"Place Order|Review Your Order|Place Your Order", body, re.I):
                with db() as conn:
                    conn.execute(
                        "UPDATE order_tasks SET status='ready_for_submit', current_stage='ready_for_submit', review_url=?, finished_at=? WHERE id=?",
                        (page.url, now(), task_id),
                    )
                task_log(task_id, "ready_for_submit", "Reached final review/submit stage. Place Order was not clicked.")
            else:
                with db() as conn:
                    conn.execute(
                        "UPDATE order_tasks SET status='blocked', current_stage='blocked', review_url=?, finished_at=? WHERE id=?",
                        (page.url, now(), task_id),
                    )
                task_log(task_id, "blocked", "Stopped before review; inspect visible browser for required input.")
            task_log(task_id, "summary", f"Total task time {elapsed(run_started)}")
            await browser.close()
    except Exception as exc:
        error = sanitize_sensitive(str(exc))
        with db() as conn:
            conn.execute(
                "UPDATE order_tasks SET status='failed', current_stage='failed', error=?, finished_at=? WHERE id=?",
                (error, now(), task_id),
            )
        task_log(task_id, "failed", error)


def only_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


async def wait_for_product_ready(page: Any, sku: dict[str, Any]) -> None:
    if sku.get("model_path") == "homepod-mini":
        await wait_until_text(page, r"Add to Bag|HomePod mini|\$99", timeout=25_000)
        return
    await wait_until_text(page, r"Add to Bag|AppleCare|No AppleCare|Buy iPhone", timeout=30_000)


async def normalize_bag_quantity(page: Any) -> None:
    for _ in range(8):
        qtys = page.locator('input[id$="itemQuantity"], select[id$="itemQuantity"]')
        count = await qtys.count()
        if count <= 1:
            break
        try:
            await click_visible_any(page, r"Remove")
            await wait_for_item_count_below(page, count, timeout=12_000)
        except Exception:
            break
    qty = page.locator('input[id$="itemQuantity"], select[id$="itemQuantity"]').first
    if await qty.count():
        tag_name = (await qty.evaluate("el => el.tagName")).lower()
        if tag_name == "select":
            await qty.select_option("1")
        else:
            await qty.fill("1")
            await qty.press("Enter")
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass


async def wait_for_item_count_below(page: Any, current_count: int, timeout: int = 10_000) -> None:
    deadline = time.perf_counter() + (timeout / 1000)
    while time.perf_counter() < deadline:
        if await page.locator('input[id$="itemQuantity"], select[id$="itemQuantity"]').count() < current_count:
            return
        await page.wait_for_timeout(250)


async def wait_for_checkout_ready(page: Any) -> None:
    try:
        await page.wait_for_url(re.compile(r"checkout|secure\d*\.store\.apple", re.I), timeout=30_000)
    except Exception:
        pass
    await wait_for_any_visible(
        page,
        [
            "#checkout\\.fulfillment\\.deliveryTab\\.delivery\\.deliveryLocation\\.address\\.postalCode",
            "text=/Continue with other payment options|other payment options/i",
        ],
        timeout=45_000,
    )


async def wait_for_city_state_ready(page: Any) -> None:
    city = page.locator("#checkout\\.shipping\\.addressSelector\\.newAddress\\.address\\.zipLookup\\.city")
    state = page.locator("#checkout\\.shipping\\.addressSelector\\.newAddress\\.address\\.zipLookup\\.state")
    try:
        await city.wait_for(state="visible", timeout=8_000)
        await state.wait_for(state="visible", timeout=8_000)
    except Exception:
        await page.wait_for_timeout(600)


async def wait_for_billing_ready(page: Any) -> None:
    await wait_for_any_visible(
        page,
        [
            "#checkout\\.billing\\.billingoptions\\.credit",
            "#checkout\\.billing\\.billingOptions\\.selectedBillingOptions\\.giftCard\\.giftCardInput\\.giftCard",
            "text=/Payment|Billing|Credit Card|Apple Gift Card|Apply an Apple Gift Card/i",
        ],
        timeout=45_000,
    )


async def wait_until_text(page: Any, pattern: str, timeout: int = 30_000) -> None:
    regex = re.compile(pattern, re.I)
    deadline = time.perf_counter() + (timeout / 1000)
    last_count = 0
    while time.perf_counter() < deadline:
        locs = page.get_by_text(regex)
        try:
            last_count = await locs.count()
            for idx in range(last_count):
                if await locs.nth(idx).is_visible(timeout=200):
                    return
        except Exception:
            pass
        await page.wait_for_timeout(250)
    raise RuntimeError(f"timed out waiting for visible text: {pattern}; matches={last_count}")


async def wait_for_any_visible(page: Any, selectors: list[str], timeout: int = 30_000) -> str:
    deadline = time.perf_counter() + (timeout / 1000)
    last_error = ""
    while time.perf_counter() < deadline:
        for selector in selectors:
            loc = page.locator(selector).first
            try:
                if await loc.count() and await loc.is_visible(timeout=250):
                    return selector
            except Exception as exc:
                last_error = str(exc)
        await page.wait_for_timeout(250)
    raise RuntimeError(f"timed out waiting for visible selector: {selectors}; {last_error}")


async def is_visible(locator: Any) -> bool:
    try:
        return await locator.is_visible(timeout=500)
    except Exception:
        return False


async def fill_id(page: Any, element_id: str, value: str) -> None:
    await page.locator("#" + element_id.replace(".", "\\.")).fill(value or "", timeout=45_000)


async def click_visible(page: Any, role: str, pattern: str) -> None:
    locs = page.get_by_role(role, name=re.compile(pattern, re.I))
    for idx in range(await locs.count()):
        loc = locs.nth(idx)
        try:
            if await loc.is_visible(timeout=1000) and await loc.is_enabled(timeout=1000):
                await loc.click(timeout=10_000)
                return
        except Exception:
            continue
    raise RuntimeError(f"visible {role} not found: {pattern}")


async def click_visible_any(page: Any, pattern: str) -> None:
    for role in ("button", "link"):
        try:
            await click_visible(page, role, pattern)
            return
        except Exception:
            pass
    locs = page.get_by_text(re.compile(pattern, re.I))
    for idx in range(await locs.count()):
        loc = locs.nth(idx)
        try:
            if await loc.is_visible(timeout=1000):
                await loc.click(timeout=10_000)
                return
        except Exception:
            continue
    raise RuntimeError(f"visible control not found: {pattern}")
