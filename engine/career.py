"""Which career and save are loaded, and the world memory that goes with them.

A career is identified by the human manager's FM unique id plus the career's
start (database and start date, e.g. "英格兰 - 2026-7-13").

World memory follows FM's saves, so loading an older save never brings back
memories of things that have not happened yet:

    careers/<career>/memory/<save name>.sqlite          live memory of that save file
    careers/<career>/checkpoints/<save name>/<count>_<date>.sqlite
                                                         snapshot taken when FM saved

- FM saves (the save count goes up): a checkpoint is written.
- "Save as" a new name: the new save starts from the current memory.
- A save is loaded (FM's session id changes): the checkpoint with the same
  save count is restored; without one, the newest checkpoint not later than
  the game date; without that, memory starts empty. The memory being
  replaced is kept as a .before-load copy.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .bridge import DATA_DIR, Bridge, BridgeUnavailable, _text
from .store import Store

CAREERS_DIR = DATA_DIR / "careers"
KEEP_CHECKPOINTS = 30  # per save file


@dataclass
class Career:
    key: str
    manager_id: str
    manager_name: str
    manager_uid: int | None
    team_id: str
    team_name: str
    save_name: str
    save_count: int | None
    start: str
    date: str
    session: str  # FM's id for the loaded game; changes on every load
    store: Store

    def summary(self) -> dict:
        return {
            "key": self.key, "date": self.date, "save": self.save_name, "save_count": self.save_count,
            "start": self.start,
            "manager": {"id": self.manager_id, "name": self.manager_name, "uid": self.manager_uid},
            "team": {"id": self.team_id, "name": self.team_name},
        }


def _save_name(raw: str) -> str:
    """'"David Wang-莱斯特城" (已保存 102 次)' -> 'David Wang-莱斯特城'."""
    m = re.match(r'\s*"(.*?)"', raw or "")
    return (m.group(1) if m else (raw or "")).strip() or "未命名存档"


def _file_name(save: str) -> str:
    """A save name that is safe as a file name."""
    return re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", save)[:120]


class CareerTracker:
    """Tracks the loaded career and save; re-reads the game record at most every few seconds."""

    def __init__(self, bridge: Bridge, refresh_seconds: float = 3.0, root: Path = CAREERS_DIR):
        self.bridge = bridge
        self.refresh_seconds = refresh_seconds
        self.root = root
        self._career: Career | None = None
        self._checked = 0.0
        self._lock = threading.Lock()

    # --- paths --------------------------------------------------------------------
    def _memory_path(self, key: str, save: str) -> Path:
        return self.root / key / "memory" / f"{_file_name(save)}.sqlite"

    def _checkpoint_dir(self, key: str, save: str) -> Path:
        return self.root / key / "checkpoints" / _file_name(save)

    # --- tracking -----------------------------------------------------------------
    def current(self) -> Career:
        with self._lock:
            if self._career and time.monotonic() - self._checked < self.refresh_seconds:
                return self._career
            game = self.bridge.game()  # raises BridgeUnavailable when no career is loaded
            session = self.bridge.status().get("guid") or ""
            self._checked = time.monotonic()
            c = self._career
            if not (c and c.manager_id == game["manager"] and c.start == game["start"]):
                self._career = self._open(game, session)
                return self._career
            save = _save_name(game.get("save_name", ""))
            count = game.get("save_count") if isinstance(game.get("save_count"), int) else None
            if session != c.session:
                self._on_load(c, save, count, game["date"], session)
            elif save != c.save_name:
                self._on_save_as(c, save, count)
            elif count is not None and count != c.save_count:
                c.save_count = count
                self._checkpoint(c)
            c.date = game["date"]
            c.store.set_meta("last_date", c.date)
            return c

    def _open(self, game: dict, session: str) -> Career:
        if self._career:
            self._career.store.close()
            self._career = None
        manager = self.bridge.query(game["manager"], ["eman", "dinu"])
        uid = manager.get("dinu") if isinstance(manager.get("dinu"), int) else None
        start = game.get("start") or ""
        if not uid or not start or not game.get("manager"):
            # Right after a save loads these can be missing; never fall back to another
            # id (that would split the career across databases). Try again shortly.
            raise BridgeUnavailable("career still loading")
        key = f"{uid}-{hashlib.sha1(start.encode()).hexdigest()[:8]}"
        save = _save_name(game.get("save_name", ""))
        count = game.get("save_count") if isinstance(game.get("save_count"), int) else None
        self._adopt_legacy(key, save)
        career = Career(
            key=key, manager_id=game["manager"], manager_name=_text(manager.get("eman")) or "",
            manager_uid=uid, team_id=game["team"], team_name=self.bridge.name(game["team"]),
            save_name=save, save_count=count, start=start, date=game["date"], session="",
            store=Store(self._memory_path(key, save)),
        )
        self._on_load(career, save, count, game["date"], session)
        for k, v in (("manager_name", career.manager_name), ("team_name", career.team_name),
                     ("save_name", save), ("start", start)):
            career.store.set_meta(k, v)
        return career

    def _adopt_legacy(self, key: str, save: str) -> None:
        """Before memory followed saves, a career had one database: careers/<key>.sqlite."""
        legacy = self.root / f"{key}.sqlite"
        target = self._memory_path(key, save)
        if legacy.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), target)

    # --- save events -----------------------------------------------------------------
    def _checkpoint(self, c: Career) -> None:
        if c.save_count is None:
            return
        folder = self._checkpoint_dir(c.key, c.save_name)
        for old in folder.glob(f"{c.save_count:05d}_*.sqlite"):  # this count was saved again: new timeline
            old.unlink()
        c.store.backup_to(folder / f"{c.save_count:05d}_{c.date}.sqlite")
        checkpoints = sorted(folder.glob("*.sqlite"))
        for old in checkpoints[:-KEEP_CHECKPOINTS]:
            old.unlink()

    def _on_save_as(self, c: Career, save: str, count: int | None) -> None:
        """Saved under a new name: that save continues from the current memory."""
        target = self._memory_path(c.key, save)
        if target.exists():  # overwriting an existing save file overwrites its memory too
            target.replace(target.with_suffix(".before-load.sqlite"))
        c.store.backup_to(target)
        c.store.close()
        c.store = Store(target)
        c.save_name, c.save_count = save, count
        c.store.set_meta("save_name", save)
        self._checkpoint(c)

    def _on_load(self, c: Career, save: str, count: int | None, date: str, session: str) -> None:
        """A save was loaded: bring memory to the state that matches it."""
        target = self._memory_path(c.key, save)
        folder = self._checkpoint_dir(c.key, save)
        exact = sorted(folder.glob(f"{count:05d}_*.sqlite")) if count is not None else []
        source = exact[-1] if exact else None
        if source is None:
            last = None
            if target.exists():
                probe = c.store if c.store.path == target else Store(target)
                last = probe.get_meta("last_date")
                if probe is not c.store:
                    probe.close()
            if last and last > date:  # memory is ahead of this save: rewind
                earlier = [p for p in sorted(folder.glob("*.sqlite")) if p.stem.split("_", 1)[-1] <= date]
                source = earlier[-1] if earlier else "empty"
        if source is not None:
            c.store.close()
            if target.exists():
                shutil.copy2(target, target.with_suffix(".before-load.sqlite"))
                target.unlink()
            if source != "empty":
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            c.store = Store(target)
            c.store.add_event("save_loaded", date, {"save": save, "count": count,
                                                    "restored": Path(source).name if source != "empty" else None})
        elif c.store.path != target:
            c.store.close()
            c.store = Store(target)
        c.save_name, c.save_count, c.date, c.session = save, count, date, session
        c.store.set_meta("last_date", date)
