"""SQLite: отзывы и вопросы (items), товары, настройки, журнал действий."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,          -- id отзыва/вопроса в WB
    kind          TEXT NOT NULL,             -- feedback | question
    marketplace   TEXT NOT NULL DEFAULT 'wb', -- wb | ozon | ym
    created_at    TEXT NOT NULL,             -- дата появления на площадке (UTC ISO)
    fetched_at    TEXT NOT NULL,             -- когда забрали в базу
    nm_id         INTEGER,
    product_name  TEXT,
    supplier_article TEXT,
    brand         TEXT,
    user_name     TEXT,
    rating        INTEGER,                   -- только у отзывов
    text          TEXT,
    pros          TEXT,
    cons          TEXT,
    has_media     INTEGER DEFAULT 0,
    -- результат анализа
    scenario      TEXT,
    theme         TEXT,
    tone          TEXT,
    priority      TEXT,
    has_question  INTEGER DEFAULT 0,
    has_wish      INTEGER DEFAULT 0,         -- в отзыве есть пожелание или просьба что-то изменить
    is_sample     INTEGER DEFAULT 0,         -- 1 = тестовый набор, на площадку не отправляется
    summary       TEXT,
    route_reason  TEXT,
    analyzed_by   TEXT,                      -- llm | rules
    -- ответ
    draft         TEXT,
    status        TEXT NOT NULL,             -- new | manager | scheduled | sent | sent_auto | rejected | skipped | answered_in_wb | error
    send_after    TEXT,                      -- для автоотправки с задержкой
    answer_text   TEXT,
    answered_at   TEXT,
    answered_by   TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_items_status ON items(status);
CREATE INDEX IF NOT EXISTS ix_items_created ON items(created_at);

CREATE TABLE IF NOT EXISTS products (
    nm_id INTEGER PRIMARY KEY, title TEXT, vendor_code TEXT, brand TEXT, description TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, level TEXT, event TEXT, item_id TEXT, details TEXT
);
"""

DEFAULT_SETTINGS = {
    "poll_interval_min": 30,           # проверка WB каждые N минут
    "auto_reply_enabled": False,       # режим автоматической отправки (по умолчанию выключен)
    "auto_min_rating": 5,              # автоответ только на отзывы с оценкой не ниже
    "auto_scenarios": ["Благодарность"],
    "auto_delay_min": 60,              # задержка перед автоотправкой: менеджер успевает отменить
    "auto_daily_limit": 100,
    "auto_skip_media": False,          # не отвечать автоматически на отзывы с фото/видео
    "signature": "С уважением, команда Paperform",
    "stop_words": ["http", "www", ".ru", ".com", "@", "telegram", "телеграм", "whatsapp", "ватсап", "вотсап",
                    "viber", "вайбер", "позвоните", "звоните", "компенсац", "промокод", "скидк", "бесплатно вышлем"],
    "last_poll_at": None,
    "last_poll_result": None,
    "token_alert": None,
    "token_alerts": {},            # {"wb"|"ozon"|"ym": текст} — площадки не затирают друг друга
    "snapshot_loaded": False,          # в базе сохранённая выгрузка WB, демо-опрос её не трогает
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


@contextmanager
def session():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with session() as con:
        con.executescript(SCHEMA)
        cols = {row[1] for row in con.execute("PRAGMA table_info(items)")}
        if "has_wish" not in cols:
            con.execute("ALTER TABLE items ADD COLUMN has_wish INTEGER DEFAULT 0")
        if "marketplace" not in cols:
            con.execute("ALTER TABLE items ADD COLUMN marketplace TEXT NOT NULL DEFAULT 'wb'")
        if "is_sample" not in cols:
            con.execute("ALTER TABLE items ADD COLUMN is_sample INTEGER DEFAULT 0")
        for k, v in DEFAULT_SETTINGS.items():
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))


def get_settings() -> dict:
    with session() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: json.loads(r["value"]) for r in rows})
    return s


def set_setting(key: str, value):
    with session() as con:
        con.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)))


def log(event: str, item_id: str | None = None, details: str = "", level: str = "info"):
    with session() as con:
        con.execute("INSERT INTO log(at, level, event, item_id, details) VALUES (?, ?, ?, ?, ?)",
                    (now_iso(), level, event, item_id, details))


def item_exists(con, item_id: str) -> bool:
    return con.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is not None


def insert_item(con, it: dict):
    cols = ", ".join(it.keys())
    q = ", ".join("?" for _ in it)
    con.execute(f"INSERT OR IGNORE INTO items({cols}) VALUES ({q})", tuple(it.values()))


def update_item(item_id: str, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with session() as con:
        con.execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), item_id))


def get_item(item_id: str):
    with session() as con:
        return con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def product_info(nm_id) -> dict | None:
    if not nm_id:
        return None
    with session() as con:
        r = con.execute("SELECT * FROM products WHERE nm_id=?", (nm_id,)).fetchone()
    return dict(r) if r else None
