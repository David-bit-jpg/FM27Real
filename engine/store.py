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
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Columns added after the first schema (older databases get them here)."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(memories)")}
        for name, decl in (("weight", "REAL NOT NULL DEFAULT 1"), ("kind", "TEXT"), ("event_key", "TEXT")):
            if name not in cols:
                self._db.execute(f"ALTER TABLE memories ADD COLUMN {name} {decl}")
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS memories_event ON memories (person_id, event_key) "
                         "WHERE event_key IS NOT NULL")
        ecols = {r[1] for r in self._db.execute("PRAGMA table_info(events)")}
        if "event_key" not in ecols:
            self._db.execute("ALTER TABLE events ADD COLUMN event_key TEXT")
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_key ON events (event_key) WHERE event_key IS NOT NULL")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def backup_to(self, path: Path) -> None:
        """Consistent copy of the whole database (safe while in use)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            dst = sqlite3.connect(path)
            try:
                self._db.backup(dst)
            finally:
                dst.close()

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

    def recent_threads(self, limit: int = 20) -> list[tuple[str, str]]:
        """(person id, last message text), most recent conversation first."""
        rows = self._exec(
            "SELECT person_id, text FROM messages WHERE id IN "
            "(SELECT MAX(id) FROM messages GROUP BY person_id) ORDER BY id DESC LIMIT ?", (limit,))
        return [(r["person_id"], r["text"]) for r in rows]

    # --- canon --------------------------------------------------------------------
    def canon(self, person_id: str) -> list[dict]:
        return [dict(r) for r in self._exec(
            "SELECT key, value, game_date FROM canon WHERE person_id = ? ORDER BY id", (person_id,))]

    def add_canon(self, person_id: str, key: str, value: str, game_date: str | None, source: str) -> bool:
        """Adds a life detail; an existing key is never overwritten (canon stays canon)."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO canon (person_id, key, value, game_date, source) VALUES (?, ?, ?, ?, ?)",
                (person_id, key, value, game_date, source))
            self._db.commit()
            return cur.rowcount > 0

    # --- memories -----------------------------------------------------------------
    def add_memory(self, person_id: str, text: str, game_date: str | None, source: str, weight: float = 1.0,
                   kind: str | None = None, event_key: str | None = None) -> bool:
        """Stores a memory; with an event_key, the same event is stored only once per person."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO memories (person_id, game_date, text, source, weight, kind, event_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (person_id, game_date, text, source, weight, kind, event_key))
            self._db.commit()
            return cur.rowcount > 0

    def memories(self, person_id: str, limit: int = 300) -> list[dict]:
        rows = self._exec("SELECT game_date, text, source, weight, kind FROM memories WHERE person_id = ? "
                          "ORDER BY id DESC LIMIT ?", (person_id, limit))
        return [dict(r) for r in reversed(rows)]

    # --- relations ----------------------------------------------------------------
    RELATION_LIMIT = 100  # each score stays within -100..100

    def relation(self, person_id: str) -> dict:
        rows = self._exec("SELECT trust, liking, respect, updated_date FROM relations WHERE person_id = ?",
                          (person_id,))
        if not rows:
            return {"trust": 0, "liking": 0, "respect": 0, "updated_date": None}
        r = dict(rows[0])
        return {k: int(round(r[k] or 0)) for k in ("trust", "liking", "respect")} | {"updated_date": r["updated_date"]}

    def apply_relation(self, person_id: str, delta: dict, game_date: str | None) -> dict:
        r = self.relation(person_id)
        lim = self.RELATION_LIMIT
        for k in ("trust", "liking", "respect"):
            r[k] = max(-lim, min(lim, (r[k] or 0) + int(delta.get(k) or 0)))
        self._exec(
            "INSERT INTO relations (person_id, trust, liking, respect, updated_date) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (person_id) DO UPDATE SET trust = excluded.trust, liking = excluded.liking, "
            "respect = excluded.respect, updated_date = excluded.updated_date",
            (person_id, r["trust"], r["liking"], r["respect"], game_date))
        r["updated_date"] = game_date
        return r

    # --- events -----------------------------------------------------------------
    def add_event(self, kind: str, game_date: str | None, payload: dict, event_key: str | None = None) -> bool:
        """Records a world event; with an event_key it is recorded only once."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO events (game_date, kind, payload, event_key) VALUES (?, ?, ?, ?)",
                                   (game_date, kind, json.dumps(payload, ensure_ascii=False), event_key))
            self._db.commit()
            return cur.rowcount > 0

    def events_after(self, event_id: int, limit: int = 500) -> list[dict]:
        """Events recorded after the given row id, oldest first (with their ids)."""
        rows = self._exec("SELECT id, game_date, kind, payload, event_key FROM events WHERE id > ? ORDER BY id LIMIT ?",
                          (event_id, limit))
        return [dict(r) | {"payload": json.loads(r["payload"]) if r["payload"] else None} for r in rows]

    def event_by_key(self, event_key: str) -> dict | None:
        rows = self._exec("SELECT id, game_date, kind, payload, event_key FROM events WHERE event_key = ?", (event_key,))
        return dict(rows[0]) | {"payload": json.loads(rows[0]["payload"]) if rows[0]["payload"] else None} if rows else None

    def update_event(self, event_key: str, payload: dict) -> None:
        self._exec("UPDATE events SET payload = ? WHERE event_key = ?", (json.dumps(payload, ensure_ascii=False), event_key))

    def events(self, kind: str | None = None, since: str | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT game_date, kind, payload, event_key FROM events WHERE 1=1", []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if since:
            sql += " AND game_date >= ?"
            args.append(since)
        rows = self._exec(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [dict(r) | {"payload": json.loads(r["payload"]) if r["payload"] else None} for r in reversed(rows)]
