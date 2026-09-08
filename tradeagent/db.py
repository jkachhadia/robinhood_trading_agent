"""SQLite store. One connection per process; WAL mode; schema created on first use."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL,
    symbol TEXT NOT NULL,
    instrument TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    side TEXT NOT NULL,
    is_exit INTEGER NOT NULL DEFAULT 0,
    horizon TEXT,
    confidence REAL,
    reward_risk REAL,
    max_qty REAL,
    max_notional REAL,
    sizing_note TEXT,
    approval_source TEXT,
    approved_at TEXT,
    rejection_reasons TEXT,
    session_id TEXT,
    mode_at_creation TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_key ON proposals(instrument_key, side);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT,
    headless INTEGER NOT NULL DEFAULT 0,
    tool_name TEXT NOT NULL,
    tool_use_id TEXT,
    action TEXT NOT NULL,
    rule TEXT,
    reason TEXT,
    proposal_id INTEGER,
    input_hash TEXT,
    input_json TEXT,
    mode TEXT,
    dry_run INTEGER
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    session_id TEXT,
    proposal_id INTEGER,
    tool_name TEXT NOT NULL,
    tool_use_id TEXT,
    symbol TEXT NOT NULL,
    instrument TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    side TEXT NOT NULL,
    is_exit INTEGER NOT NULL DEFAULT 0,
    qty REAL NOT NULL,
    order_type TEXT,
    limit_price REAL,
    fill_price REAL,
    notional REAL,
    broker_order_id TEXT,
    status TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 0,
    input_json TEXT,
    response_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(trading_date);
CREATE INDEX IF NOT EXISTS idx_orders_key ON orders(instrument_key);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    session_id TEXT,
    kind TEXT NOT NULL,
    equity REAL,
    buying_power REAL,
    cash REAL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);

CREATE TABLE IF NOT EXISTS positions (
    instrument_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    instrument TEXT NOT NULL,
    qty REAL,
    avg_cost REAL,
    market_value REAL,
    updated_at TEXT NOT NULL,
    source TEXT,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS quotes (
    instrument_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    price REAL,
    bid REAL,
    ask REAL,
    ts TEXT NOT NULL,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT,
    tool_name TEXT NOT NULL,
    match_hash TEXT,
    input_json TEXT,
    response_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_hash ON reviews(match_hash);

CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT,
    tool_name TEXT NOT NULL,
    tool_use_id TEXT,
    ok INTEGER NOT NULL DEFAULT 1,
    input_json TEXT,
    response_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_tool ON raw_responses(tool_name, ts);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    kind TEXT,
    headless INTEGER NOT NULL DEFAULT 0,
    mode TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS daily (
    trading_date TEXT PRIMARY KEY,
    day_open_equity REAL,
    week_open_equity REAL,
    halted_reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


class Store:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or paths.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ---- generic helpers -------------------------------------------------
    def insert(self, table: str, row: dict) -> int:
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
        return int(cur.lastrowid)

    def upsert(self, table: str, row: dict, key: str) -> None:
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != key)
        self.conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT({key}) DO UPDATE SET {updates}",
            list(row.values()),
        )

    def update(self, table: str, row_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*fields.values(), row_id])

    def one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def kv_get(self, key: str, default=None):
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        if r is None:
            return default
        try:
            return json.loads(r["value"])
        except (TypeError, json.JSONDecodeError):
            return r["value"]

    def kv_set(self, key: str, value) -> None:
        self.upsert("kv", {"key": key, "value": json.dumps(value, default=str), "updated_at": now_iso()}, "key")
