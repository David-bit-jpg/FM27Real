"""Touchline shouts during a match -> news, the feed, social media and memories.

FM's match screen sends each shout to the engine as event 1417105520 whose data is
the shout itself (FM.UI.TeamTouchlineShoutReference: fman name, csed meaning, rylp
the player for an individual shout). The shout is read from that data only; nothing
is queried (querying objects of a match that is ending crashed the game). Once the
match is in the world memory, the day's shouts become a news item (origin
"touchline": social media and the feed pick it up) and memories for the players.
No model call.
"""
from __future__ import annotations

import json
import struct
import time

from .bridge import DATA_DIR, Bridge, _text
from .career import Career
from .fmvalue import decode, target_pairs

SHOUT_EVENT = 1417105520                         # match screen -> engine: a touchline shout (recorded 2026-11-22)
NEVER_QUERY = {42, 43}                           # the editor's own targets (see PROTOCOL.md safety notes)
LOG = DATA_DIR / "match-events.jsonl"


def _clean(v) -> str:
    return (_text(v) or "").replace("​", "").strip()


class Touchline:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.names: dict[str, str] = {}

    def observe(self, career: Career, ev: dict) -> None:
        """Purely passive: the shout arrives with its own data (TeamTouchlineShoutReference: fman name,
        csed description, rylp the player for an individual shout). Nothing is ever queried here: querying
        objects of a match that is ending crashed the game (2026-11-22)."""
        pairs = target_pairs(ev.get("target", ""))
        if pairs.get(" 011") in NEVER_QUERY:
            return
        data = decode(ev.get("data") or "")
        shout = data if isinstance(data, dict) and "fman" in data and "hUFo" in data else None
        if ev.get("id") != SHOUT_EVENT and not shout:
            return
        if not shout:
            return
        player = (shout.get("rylp") or {}).get("ref") if isinstance(shout.get("rylp"), dict) else None
        career.store.add_event("touchline", career.date, {
            "shout": _clean(shout.get("fman")), "meaning": _clean(shout.get("csed")), "player": player,
            "clock": career.clock})
        try:
            with LOG.open("a") as f:
                f.write(json.dumps({"t": time.time(), "date": career.date, "shout": _clean(shout.get("fman")),
                                    "player": player}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def settle(self, career: Career) -> None:
        """Folds each match day's shouts into news and memories once the match is recorded."""
        store = career.store
        shouts: dict[str, list[str]] = {}
        for e in store.events("touchline", limit=200):
            shouts.setdefault(e["game_date"], []).append((e["payload"] or {}).get("shout"))
        for day, items in shouts.items():
            key = f"news:touchline:{day}"
            if store.event_by_key(key):
                continue
            match = next((m for m in store.events("match", since=day, limit=5) if m["game_date"] == day), None)
            if not match:
                continue
            p = match["payload"] or {}
            counts: dict[str, int] = {}
            for s in items:
                if s:
                    counts[s] = counts.get(s, 0) + 1
            if not counts:
                continue
            said = "、".join(f"「{s}」" + (f"×{n}" if n > 1 else "") for s, n in counts.items())
            line = f"{p.get('home_name')} {p['score'][0]}-{p['score'][1]} {p.get('away_name')}"
            title = f"场边：{career.manager_name}在{line}一战中{said}"
            store.add_event("news", day, {"id": f"touchline:{day}", "title": title[:120],
                                          "body": f"{line}。比赛中{career.manager_name}在场边的指令：{said}。",
                                          "people": list((p.get("ratings") or {}).keys())[:6], "origin": "touchline",
                                          "weight": 1.6, "shouts": counts}, event_key=key)
            for e in store.events("touchline", limit=200):  # a shout at one player: he remembers it himself
                q = e["payload"] or {}
                if e["game_date"] == day and q.get("player"):
                    store.add_memory(q["player"], f"比赛中主教练在场边单独冲他喊：「{q.get('shout')}」（{q.get('meaning') or ''}）",
                                     day, "touchline", 1.4, "touchline", f"{key}:{q['player']}:{q.get('shout')}")
            for ref in (p.get("ratings") or {}):
                store.add_memory(ref, f"对阵{p.get('away_name') if p.get('we_home') else p.get('home_name')}的比赛中，"
                                      f"主教练在场边冲大家喊：{said}", day, "touchline", 1.3, "touchline", key)
