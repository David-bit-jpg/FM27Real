"""Which career is loaded, and its database.

A career is identified by the human manager's FM unique id plus the career's
start (database and start date, e.g. "英格兰 - 2026-7-13"): stable across
loads and saves, and different for every new career.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass

from .bridge import DATA_DIR, Bridge, _text
from .store import Store


@dataclass
class Career:
    key: str
    manager_id: str
    manager_name: str
    manager_uid: int | None
    team_id: str
    team_name: str
    save_name: str
    start: str
    date: str
    store: Store

    def summary(self) -> dict:
        return {
            "key": self.key, "date": self.date, "save": self.save_name, "start": self.start,
            "manager": {"id": self.manager_id, "name": self.manager_name, "uid": self.manager_uid},
            "team": {"id": self.team_id, "name": self.team_name},
        }


def _save_name(raw: str) -> str:
    """'"David Wang-莱斯特城" (已保存 102 次)' -> 'David Wang-莱斯特城'."""
    m = re.match(r'\s*"(.*?)"', raw or "")
    return m.group(1) if m else (raw or "")


class CareerTracker:
    """Caches the loaded career; re-reads the game record at most every few seconds."""

    def __init__(self, bridge: Bridge, refresh_seconds: float = 3.0):
        self.bridge = bridge
        self.refresh_seconds = refresh_seconds
        self._career: Career | None = None
        self._checked = 0.0
        self._stores: dict[str, Store] = {}
        self._lock = threading.Lock()

    def current(self) -> Career:
        with self._lock:
            if self._career and time.monotonic() - self._checked < self.refresh_seconds:
                return self._career
            game = self.bridge.game()  # raises BridgeUnavailable when no career is loaded
            self._checked = time.monotonic()
            c = self._career
            if c and c.manager_id == game["manager"] and c.start == game["start"]:
                self._update_date(c, game["date"])
                return c
            self._career = self._open(game)
            return self._career

    def _open(self, game: dict) -> Career:
        manager = self.bridge.query(game["manager"], ["eman", "dinu"])
        uid = manager.get("dinu") if isinstance(manager.get("dinu"), int) else None
        start = game.get("start") or ""
        key = f"{uid or game['manager']}-{hashlib.sha1(start.encode()).hexdigest()[:8]}"
        store = self._stores.get(key) or Store(DATA_DIR / "careers" / f"{key}.sqlite")
        self._stores[key] = store
        career = Career(
            key=key, manager_id=game["manager"], manager_name=_text(manager.get("eman")) or "",
            manager_uid=uid, team_id=game["team"], team_name=self.bridge.name(game["team"]),
            save_name=_save_name(game.get("save_name", "")), start=start, date=game["date"], store=store,
        )
        for k, v in (("manager_name", career.manager_name), ("team_name", career.team_name),
                     ("save_name", career.save_name), ("start", start)):
            store.set_meta(k, v)
        self._update_date(career, game["date"])
        return career

    @staticmethod
    def _update_date(career: Career, date: str) -> None:
        last = career.store.get_meta("last_date")
        if last and date < last:
            # An older copy of this career was loaded: the timeline diverges here.
            career.store.add_event("timeline_rewind", date, {"from": last, "to": date})
        career.date = date
        if not last or date != last:
            career.store.set_meta("last_date", date)
