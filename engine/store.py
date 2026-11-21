"""Per-career SQLite storage.

One database per career (careers/<key>.sqlite in the data folder), so
different careers never mix. Copies of the same career share a database;
moving the game date backwards (loading an older copy) is recorded as an
event so later steps can handle diverging timelines.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- Cached snapshot of a person (FM facts + social + origin), refreshed per game day.
CREATE TABLE IF NOT EXISTS people (
    id            TEXT PRIMARY KEY,   -- FM record id, e.g. 01049638
    uid           INTEGER,            -- FM unique id (portrait file name)
    name          TEXT,
    snapshot      TEXT,               -- JSON
    snapshot_date TEXT                -- game date of the snapshot
);
-- Private conversation with one person (each person has their own thread).
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY,
    person_id TEXT NOT NULL,
    game_date TEXT,
    real_time TEXT,
    sender    TEXT NOT NULL CHECK (sender IN ('manager', 'person')),
    text      TEXT NOT NULL,
    meta      TEXT                    -- JSON: intents, mood, ...
);
CREATE INDEX IF NOT EXISTS messages_person ON messages (person_id, id);
-- What a person remembers (third-person summaries, with the game date).
CREATE TABLE IF NOT EXISTS memories (
    id        INTEGER PRIMARY KEY,
    person_id TEXT NOT NULL,
    game_date TEXT,
    text      TEXT NOT NULL,
    source    TEXT                    -- conversation, gossip, event
);
-- Our own relationship scores (FM's are read live from the bridge).
CREATE TABLE IF NOT EXISTS relations (
    person_id    TEXT PRIMARY KEY,
    trust        REAL NOT NULL DEFAULT 0,
    liking       REAL NOT NULL DEFAULT 0,
    respect      REAL NOT NULL DEFAULT 0,
    updated_date TEXT
);
-- Life details that became canon once said (pets, hobbies, ...).
CREATE TABLE IF NOT EXISTS canon (
    id        INTEGER PRIMARY KEY,
    person_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    game_date TEXT,
    source    TEXT,
    UNIQUE (person_id, key)
);
-- World events (gossip, proactive messages, timeline changes, ...).
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    game_date TEXT,
    kind      TEXT NOT NULL,
    payload   TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            self._db.commit()
            return rows

    # --- meta -------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        rows = self._exec("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        self._exec("INSERT INTO meta (key, value) VALUES (?, ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    # --- people -----------------------------------------------------------------
    def get_snapshot(self, person_id: str) -> tuple[dict | None, str | None]:
        rows = self._exec("SELECT snapshot, snapshot_date FROM people WHERE id = ?", (person_id,))
        if not rows or not rows[0]["snapshot"]:
            return None, None
        return json.loads(rows[0]["snapshot"]), rows[0]["snapshot_date"]

    def put_snapshot(self, person_id: str, snapshot: dict, game_date: str) -> None:
        self._exec(
            "INSERT INTO people (id, uid, name, snapshot, snapshot_date) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET uid = excluded.uid, name = excluded.name, "
            "snapshot = excluded.snapshot, snapshot_date = excluded.snapshot_date",
            (person_id, snapshot.get("uid"), snapshot.get("name"), json.dumps(snapshot, ensure_ascii=False), game_date),
        )

    # --- messages ---------------------------------------------------------------
    def messages(self, person_id: str, limit: int = 200) -> list[dict]:
        rows = self._exec(
            "SELECT id, game_date, real_time, sender, text, meta FROM messages "
            "WHERE person_id = ? ORDER BY id DESC LIMIT ?", (person_id, limit))
        out = [dict(r) | {"meta": json.loads(r["meta"]) if r["meta"] else None} for r in reversed(rows)]
        return out

    def add_message(self, person_id: str, sender: str, text: str, game_date: str | None,
                    meta: dict | None = None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages (person_id, game_date, real_time, sender, text, meta) VALUES (?, ?, ?, ?, ?, ?)",
                (person_id, game_date, _now(), sender, text, json.dumps(meta, ensure_ascii=False) if meta else None))
            self._db.commit()
            return int(cur.lastrowid)

    # --- canon and memories -------------------------------------------------------
    def canon(self, person_id: str) -> list[dict]:
        return [dict(r) for r in self._exec(
            "SELECT key, value, game_date FROM canon WHERE person_id = ? ORDER BY id", (person_id,))]

    def memories(self, person_id: str, limit: int = 30) -> list[dict]:
        rows = self._exec("SELECT game_date, text, source FROM memories WHERE person_id = ? "
                          "ORDER BY id DESC LIMIT ?", (person_id, limit))
        return [dict(r) for r in reversed(rows)]

    # --- events -----------------------------------------------------------------
    def add_event(self, kind: str, game_date: str | None, payload: dict) -> None:
        self._exec("INSERT INTO events (game_date, kind, payload) VALUES (?, ?, ?)",
                   (game_date, kind, json.dumps(payload, ensure_ascii=False)))
