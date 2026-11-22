"""The first team as the world engine sees it, read once per game day.

For every first-team player: name, portrait id, position, whether he is at
the club (players out on loan are listed by FM but play elsewhere), his
social group in the dressing room, influence, view of the manager and FM's
own happiness sentences.
"""
from __future__ import annotations

import threading

from .bridge import Bridge, BridgeUnavailable, _text
from .career import Career


class Roster:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self._cache: tuple[str, str, list[dict]] | None = None   # (career key, date, players)
        self._lock = threading.Lock()

    def players(self, career: Career) -> list[dict]:
        with self._lock:
            c = self._cache
            if c and c[0] == career.key and c[1] == career.date:
                return c[2]
            try:
                social = {p["id"]: p for p in self.bridge.dressing_room(career.team_id).get("players") or []}
                out = []
                for ref in self.bridge.team(career.team_id)["squad"]:
                    raw = self.bridge.query(ref, ["eman", "dinu", "SspP", "maeT"])
                    team = raw.get("maeT") if isinstance(raw.get("maeT"), dict) else {}
                    s = social.get(ref) or {}
                    out.append({"id": ref, "name": (_text(raw.get("eman")) or ref).replace("​", ""),
                                "uid": raw.get("dinu") if isinstance(raw.get("dinu"), int) else None,
                                "position": _text(raw.get("SspP")) or "",
                                "at_club": team.get("ref") in (None, career.team_id),
                                "group": s.get("social_group"), "influence": s.get("influence") or "",
                                "view_of_manager": s.get("view_of_manager") or "",
                                "happiness": s.get("happiness") or []})
            except BridgeUnavailable:
                return c[2] if c else []
            self._cache = (career.key, career.date, out)
            return out

    def at_club(self, career: Career) -> list[dict]:
        return [p for p in self.players(career) if p["at_club"]]

    def by_name(self, career: Career) -> dict[str, dict]:
        return {p["name"]: p for p in self.players(career)}

    def by_id(self, career: Career) -> dict[str, dict]:
        return {p["id"]: p for p in self.players(career)}
