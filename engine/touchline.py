"""Touchline shouts during a match -> news, the feed, social media and memories.

FM's match screen sends the shout as an engine event whose data names the shout
(FM.UI.TeamTouchlineShoutReference: `fman` name, `csed` description). The exact
event id is not recorded yet, so every event FM's interface sends to a target
type we do not already know is written to match-events.jsonl (for mapping) and
checked for a reference that carries a shout name. Each shout found becomes a
"touchline" event for that day. Once the match is in the world memory, the day's
shouts are folded into a news item (origin "touchline", so social media and the
feed pick it up) and into the memories of the players who were on the pitch.
No model call.
"""
from __future__ import annotations

import json
import struct
import time

from .bridge import DATA_DIR, Bridge, _text
from .career import Career
from .fmvalue import decode, target_pairs

KNOWN_TYPES = {102, 23, 81, 28, 70, 1, 5, 50}   # dialogs, records, news, actions, fixtures
NEVER_QUERY = {42, 43}                           # the editor's own targets (see PROTOCOL.md safety notes)
LOG = DATA_DIR / "match-events.jsonl"


def _clean(v) -> str:
    return (_text(v) or "").replace("​", "").strip()


def _target_hex(pairs: dict) -> str:
    return (bytes([len(pairs)]) + b"".join(c.encode("latin-1") + struct.pack("<i", v & 0xffffffff if v >= 0 else v)
                                           for c, v in pairs.items() if isinstance(v, int))).hex()


def _refs(value, out: list) -> list:
    """Every reference target inside a decoded event value."""
    if isinstance(value, dict):
        if isinstance(value.get("pairs"), dict) and value["pairs"]:
            out.append({"pairs": value["pairs"], "target": value.get("target") or _target_hex(value["pairs"])})
        for v in value.values():
            _refs(v, out)
    elif isinstance(value, list):
        for v in value:
            _refs(v, out)
    return out


class Touchline:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.names: dict[str, str] = {}

    def observe(self, career: Career, ev: dict) -> None:
        pairs = target_pairs(ev.get("target", ""))
        kind = pairs.get(" 011")
        if kind in KNOWN_TYPES or kind in NEVER_QUERY:
            return
        data = decode(ev.get("data") or "")
        try:
            with LOG.open("a") as f:
                f.write(json.dumps({"t": time.time(), "date": career.date, "id": ev.get("id"), "target": pairs,
                                    "data": data}, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        shout = None
        for ref in _refs(data, []) + ([{"target": ev["target"], "pairs": pairs}] if ev.get("target") else []):
            if (ref.get("pairs") or {}).get(" 011") in NEVER_QUERY | {28}:  # 28: people and clubs, never shouts
                continue
            name = self._shout_name(ref["target"])
            if name:
                shout = name
                break
        if shout:
            career.store.add_event("touchline", career.date, {"shout": shout, "clock": career.clock, "id": ev.get("id")})

    def _shout_name(self, target: str) -> str | None:
        if target in self.names:
            return self.names[target] or None
        try:
            v = self.bridge.query_target(target, ["fman", "csed", "C4sZ", "hUFo"])
        except Exception:
            v = {}
        # only TeamTouchlineShoutReference has C4sZ / hUFo next to its name
        is_shout = isinstance(v.get("C4sZ"), int) or v.get("hUFo") is not None
        name = (_clean(v.get("fman")) or "") if is_shout else ""
        self.names[target] = name
        return name or None

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
            for ref in (p.get("ratings") or {}):
                store.add_memory(ref, f"对阵{p.get('away_name') if p.get('we_home') else p.get('home_name')}的比赛中，"
                                      f"主教练在场边冲大家喊：{said}", day, "touchline", 1.3, "touchline", key)
