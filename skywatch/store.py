"""Subscriber database and the already-sent ledger.

SQLite because this is a single-process daemon on one machine and the whole
dataset is a few hundred rows. The 'sent' table is what stops a restart, a clock
change or an overlapping recompute from firing the same alert twice.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    transport   TEXT NOT NULL,
    target      TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    elevation_m REAL NOT NULL DEFAULT 0,
    tz          TEXT NOT NULL DEFAULT 'UTC',
    place       TEXT,
    settings    TEXT NOT NULL DEFAULT '{}',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (transport, target)
);
CREATE TABLE IF NOT EXISTS sent (
    sub_id    INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    sent_at   TEXT NOT NULL,
    PRIMARY KEY (sub_id, event_key)
);
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sent_time ON sent (sent_at);
"""


@contextmanager
def _conn():
    with _LOCK:
        cx = sqlite3.connect(config.DB_PATH, timeout=30)
        cx.row_factory = sqlite3.Row
        try:
            yield cx
            cx.commit()
        finally:
            cx.close()


def init() -> None:
    with _conn() as cx:
        cx.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- subscribers ------------------------------------------------------------


def upsert_subscriber(transport: str, target: str, **fields) -> int:
    target = str(target)
    with _conn() as cx:
        row = cx.execute("SELECT id FROM subscribers WHERE transport=? AND target=?",
                         (transport, target)).fetchone()
        if row is None:
            cur = cx.execute(
                "INSERT INTO subscribers (transport, target, created_at, updated_at) "
                "VALUES (?,?,?,?)", (transport, target, _now(), _now()))
            sub_id = cur.lastrowid
        else:
            sub_id = row["id"]
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            cx.execute(f"UPDATE subscribers SET {cols}, updated_at=? WHERE id=?",
                       (*fields.values(), _now(), sub_id))
    return sub_id


def get_subscriber(transport: str, target: str) -> dict | None:
    with _conn() as cx:
        row = cx.execute("SELECT * FROM subscribers WHERE transport=? AND target=?",
                         (transport, str(target))).fetchone()
    return _hydrate(row)


def get_by_id(sub_id: int) -> dict | None:
    with _conn() as cx:
        row = cx.execute("SELECT * FROM subscribers WHERE id=?", (sub_id,)).fetchone()
    return _hydrate(row)


def active_subscribers() -> list[dict]:
    with _conn() as cx:
        rows = cx.execute(
            "SELECT * FROM subscribers WHERE active=1 AND lat IS NOT NULL").fetchall()
    return [_hydrate(r) for r in rows]


def _hydrate(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    try:
        overrides = json.loads(d.get("settings") or "{}")
    except json.JSONDecodeError:
        overrides = {}
    d["settings"] = {**config.DEFAULTS, **overrides}
    d["overrides"] = overrides
    return d


def update_settings(sub_id: int, **changes) -> dict:
    with _conn() as cx:
        row = cx.execute("SELECT settings FROM subscribers WHERE id=?", (sub_id,)).fetchone()
        current = json.loads(row["settings"]) if row and row["settings"] else {}
        current.update({k: v for k, v in changes.items() if v is not None})
        cx.execute("UPDATE subscribers SET settings=?, updated_at=? WHERE id=?",
                   (json.dumps(current), _now(), sub_id))
    return {**config.DEFAULTS, **current}


def set_active(sub_id: int, active: bool) -> None:
    with _conn() as cx:
        cx.execute("UPDATE subscribers SET active=?, updated_at=? WHERE id=?",
                   (1 if active else 0, _now(), sub_id))


def delete_subscriber(sub_id: int) -> None:
    with _conn() as cx:
        cx.execute("DELETE FROM subscribers WHERE id=?", (sub_id,))
        cx.execute("DELETE FROM sent WHERE sub_id=?", (sub_id,))


# --- sent ledger ------------------------------------------------------------


def was_sent(sub_id: int, event_key: str) -> bool:
    with _conn() as cx:
        return cx.execute("SELECT 1 FROM sent WHERE sub_id=? AND event_key=?",
                          (sub_id, event_key)).fetchone() is not None


def mark_sent(sub_id: int, event_key: str) -> None:
    with _conn() as cx:
        cx.execute("INSERT OR REPLACE INTO sent (sub_id, event_key, sent_at) VALUES (?,?,?)",
                   (sub_id, event_key, _now()))


def prune_sent(older_than_days: int = 7) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    with _conn() as cx:
        cur = cx.execute("DELETE FROM sent WHERE sent_at < ?", (cutoff,))
        return cur.rowcount


# --- misc key/value ---------------------------------------------------------


def get_state(key: str, default: str | None = None) -> str | None:
    with _conn() as cx:
        row = cx.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    with _conn() as cx:
        cx.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?,?)",
                   (key, str(value)))
