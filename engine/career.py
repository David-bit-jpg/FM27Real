"""Which career and save are loaded, and the world memory that goes with them.

A career is identified by the human manager's FM unique id plus the career's
start (database and start date, e.g. "英格兰 - 2026-7-13").

World memory follows FM's saves, so loading an older save never brings back
memories of things that have not happened yet:

    careers/<career>/memory/<save name>.sqlite          live memory of that save file
    careers/<career>/checkpoints/<save name>/<file time>_<date>.sqlite
                                                         snapshot taken when FM saved

FM's save files (games/<save name>.fm) are watched; their modification time
identifies a save exactly (FM's in-game save counter only refreshes on load).

- The save file is rewritten: a checkpoint is written, keyed by the file time.
- "Save as" (another save file is written instead): that save continues
  from the current memory.
- The engine restarts while the same game keeps running (FM's session id is
  the one stored in memory): memory simply continues.
- A save is loaded (FM's session id changes): the checkpoint with the same
  file time is restored; without one, the newest checkpoint not later than
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

from .bridge import DATA_DIR, FM_DIR, Bridge, BridgeUnavailable, _text
from .store import Store

CAREERS_DIR = DATA_DIR / "careers"
GAMES_DIR = FM_DIR / "games"
SETTLE_SECONDS = 3  # a save file must be unchanged this long before it counts as written
IGNORED_SAVES = ("last save overwrite backup",)
KEEP_CHECKPOINTS = 30  # per save file


@dataclass
class Career:
    key: str
    manager_id: str
    manager_name: str
    manager_uid: int | None
    team_id: str | None    # None while the manager has no club
    team_name: str         # "无俱乐部（待业）" while he has none, so prompts read naturally
    save_name: str
    save_count: int | None
    save_time: float | None  # modification time of the save file the memory matches
    start: str
    date: str
    session: str  # FM's id for the loaded game; changes on every load
    store: Store
    clock: str = ""  # FM's in-game time ("2028-3-7 09:00"); moves only when the game advances

    @property
    def unemployed(self) -> bool:
        return not self.team_id

    def summary(self) -> dict:
        return {
            "key": self.key, "date": self.date, "save": self.save_name, "save_count": self.save_count,
            "start": self.start,
            "manager": {"id": self.manager_id, "name": self.manager_name, "uid": self.manager_uid},
            "team": {"id": self.team_id, "name": self.team_name}, "unemployed": self.unemployed,
        }

NO_CLUB = "无俱乐部（待业）"


def _save_name(raw: str) -> str:
    """'"David Wang-莱斯特城" (已保存 102 次)' -> 'David Wang-莱斯特城'."""
    m = re.match(r'\s*"(.*?)"', raw or "")
    return (m.group(1) if m else (raw or "")).strip() or "未命名存档"


def _file_name(save: str) -> str:
    """A save name that is safe as a file name."""
    return re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", save)[:120]


class CareerTracker:
    """Tracks the loaded career and save; re-reads the game record at most every few seconds."""

    def __init__(self, bridge: Bridge, refresh_seconds: float = 3.0, root: Path = CAREERS_DIR,
                 games: Path = GAMES_DIR):
        self.bridge = bridge
        self.refresh_seconds = refresh_seconds
        self.root = root
        self.games = games
        self._career: Career | None = None
        self._checked = 0.0
        self._lock = threading.Lock()

    # --- paths --------------------------------------------------------------------
    def _memory_path(self, key: str, save: str) -> Path:
        return self.root / key / "memory" / f"{_file_name(save)}.sqlite"

    def _checkpoint_dir(self, key: str, save: str) -> Path:
        return self.root / key / "checkpoints" / _file_name(save)

    def _save_file_time(self, save: str) -> float | None:
        try:
            return (self.games / f"{save}.fm").stat().st_mtime
        except OSError:
            return None

    def _written_saves(self, since: float) -> list[tuple[float, str]]:
        """Save files modified after `since` and settled, newest first."""
        out = []
        now = time.time()
        for f in self.games.glob("*.fm"):
            try:
                t = f.stat().st_mtime
            except OSError:
                continue
            if t > since + 0.5 and now - t >= SETTLE_SECONDS and f.stem not in IGNORED_SAVES:
                out.append((t, f.stem))
        return sorted(out, reverse=True)

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
            c.date = game["date"]
            c.clock = game.get("datetime") or ""
            if game.get("team") != c.team_id:  # sacked, resigned or hired: the career goes on at the new club
                self._job_change(c, game.get("team"))
            if session != c.session:
                self._on_load(c, save, count, game["date"], session)
            else:
                written = self._written_saves(c.save_time or 0)
                if written:
                    t, name = written[0]
                    if name != c.save_name:
                        self._on_save_as(c, name, None)
                    c.save_time = t
                    self._checkpoint(c)
            c.store.set_meta("last_date", c.date)
            return c

    def _open(self, game: dict, session: str) -> Career:
        manager = self.bridge.query(game["manager"], ["eman", "dinu"])
        uid = manager.get("dinu") if isinstance(manager.get("dinu"), int) else None
        start = game.get("start") or ""
        if not uid or not start or not game.get("manager"):
            # Right after a save loads these can be missing; never fall back to another
            # id (that would split the career across databases). Try again shortly.
            raise BridgeUnavailable("career still loading")
        key = f"{uid}-{hashlib.sha1(start.encode()).hexdigest()[:8]}"
        if self._career:  # only now that the new career is known: work still running on the old one ends cleanly
            old, self._career = self._career, None
            threading.Timer(120, old.store.close).start()
        save = _save_name(game.get("save_name", ""))
        count = game.get("save_count") if isinstance(game.get("save_count"), int) else None
        self._adopt_legacy(key, save)
        career = Career(
            key=key, manager_id=game["manager"], manager_name=_text(manager.get("eman")) or "",
            manager_uid=uid, team_id=game["team"], team_name=self.bridge.name(game["team"]) or NO_CLUB,
            save_name=save, save_count=count, save_time=None, start=start, date=game["date"], session="",
            store=Store(self._memory_path(key, save)),
        )
        career.clock = game.get("datetime") or ""
        self._on_load(career, save, count, game["date"], session)
        for k, v in (("manager_name", career.manager_name), ("team_name", career.team_name),
                     ("save_name", save), ("start", start)):
            career.store.set_meta(k, v)
        return career

    def _job_change(self, c: Career, team: str | None) -> None:
        old_id, old_name = c.team_id, c.team_name
        c.team_id, c.team_name = team, (self.bridge.name(team) if team else "") or NO_CLUB
        c.store.set_meta("team_name", c.team_name)
        if old_id:  # remember the last club and its players (they may still get in touch)
            c.store.set_meta("former_team", old_id)
            c.store.set_meta("former_team_name", old_name)
        c.store.add_event("job_change", c.date, {"from": old_id, "from_name": old_name, "to": team,
                                                "to_name": c.team_name if team else None},
                          event_key=f"job_change:{c.date}:{team}")

    def _adopt_legacy(self, key: str, save: str) -> None:
        """Before memory followed saves, a career had one database: careers/<key>.sqlite."""
        legacy = self.root / f"{key}.sqlite"
        target = self._memory_path(key, save)
        if legacy.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), target)

    # --- save events -----------------------------------------------------------------
    def _checkpoint(self, c: Career) -> None:
        if c.save_time is None:
            return
        folder = self._checkpoint_dir(c.key, c.save_name)
        c.store.backup_to(folder / f"{int(c.save_time)}_{c.date}.sqlite")
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

    def _on_load(self, c: Career, save: str, count: int | None, date: str, session: str) -> None:
        """A save was loaded: bring memory to the state that matches it."""
        target = self._memory_path(c.key, save)
        if session and target.exists():
            # The engine restarted while the same game kept running (same FM session):
            # memory already matches the game; nothing to restore.
            probe = c.store if c.store.path == target else Store(target)
            same = probe.get_meta("session") == session
            if probe is not c.store:
                probe.close()
            if same:
                if c.store.path != target:
                    c.store.close()
                    c.store = Store(target)
                c.save_name, c.save_count, c.date, c.session = save, count, date, session
                c.save_time = self._save_file_time(save)
                return
        folder = self._checkpoint_dir(c.key, save)
        file_time = self._save_file_time(save)
        exact = []
        if file_time is not None:
            for p in folder.glob("*.sqlite"):
                stamp = p.stem.split("_", 1)[0]
                if stamp.isdigit() and abs(int(stamp) - file_time) <= 2:
                    exact.append(p)
        source = sorted(exact)[-1] if exact else None
        if source is None:
            last = None
            if target.exists():
                probe = c.store if c.store.path == target else Store(target)
                last = probe.get_meta("last_date")
                if probe is not c.store:
                    probe.close()
            if last and last > date:  # memory is ahead of this save: rewind
                earlier = sorted((p for p in folder.glob("*.sqlite") if p.stem.split("_", 1)[-1] <= date),
                                 key=lambda p: p.stem.split("_", 1)[-1])
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
        c.save_time = file_time
        c.store.set_meta("last_date", date)
        c.store.set_meta("session", session)
