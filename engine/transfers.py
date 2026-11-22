"""Transfer interest and rumours, from FM's own transfer market (no model call).

FM keeps, per player, the clubs interested in him (person `SVMO`: `  61` club, `toOT`
转会/租借, `gJA8` their stance) and, per competition, the media's transfer rumours
(competition `72iU`: `srep` player, `lCCC` his club, `cbOT` the club linked, `SdeM`
outlet, `XfxH` the story, `ET5o` fee, `! 71` "25岁门将"). Once a game day this module:

- records new interest in our players ("transfer_interest" events), as news and as a
  memory for the player (he can be asked about it; he may like it or not);
- turns rumours about our players, and about players linked with our club, into news
  ("news" events with origin "rumour"), so journalists ask about them, fans react on
  social media and they enter the world memory everyone talks from;
- keeps a few headline rumours from our league and the league above as background.
"""
from __future__ import annotations

from .bridge import Bridge, BridgeUnavailable, _text
from .career import Career

INTEREST = "SVMO"
RUMOURS = "72iU"
LEAGUE = "  47"          # team -> its league
LEAGUE_ABOVE = "RH9R"    # team -> the division above (where most suitors are)
BACKGROUND_RUMOURS = 3   # league rumours kept per day that are not about us


def _clean(v) -> str:
    return (_text(v) or "").replace("​", "").strip()


def _ref(v) -> str | None:
    return v.get("ref") if isinstance(v, dict) else None


class Transfers:
    def __init__(self, bridge: Bridge, roster, story):
        self.bridge = bridge
        self.roster = roster
        self.story = story

    def sync(self, career: Career) -> None:
        store = career.store
        if store.get_meta("transfers_on") == career.date:
            return
        try:
            self._interest(career)
            self._rumours(career)
        except BridgeUnavailable:
            return
        store.set_meta("transfers_on", career.date)

    # --- clubs interested in our players -------------------------------------------------
    def _interest(self, career: Career) -> None:
        store = career.store
        for p in self.roster.at_club(career):
            items = self.bridge.query(p["id"], [INTEREST]).get(INTEREST) or []
            for item in items if isinstance(items, list) else []:
                props = (item or {}).get("props") or {}
                club = _ref(props.get("  61"))
                if not club:
                    continue
                kind = _clean(props.get("toOT")) or "转会"
                stance = _clean(props.get("gJA8"))
                key = f"interest:{p['id']}:{club}:{kind}"
                if store.event_by_key(key):
                    continue
                club_name = self.story.name(club)
                payload = {"person": p["id"], "name": p["name"], "club": club, "club_name": club_name,
                           "kind": kind, "stance": stance}
                if not store.add_event("transfer_interest", career.date, payload, event_key=key):
                    continue
                title = f"{club_name}对{p['name']}产生{'租借' if '租' in kind else '转会'}兴趣"
                store.add_event("news", career.date, {
                    "id": key, "title": title, "body": f"{title}。" + (f"（{stance}）" if stance else ""),
                    "people": [p["id"]], "names": {p["id"]: p["name"]}, "weight": 1.6, "origin": "transfer",
                    "clubs": {club: club_name}}, event_key=f"news:{key}")
                store.add_memory(p["id"], f"听说{club_name}对他有{kind}兴趣" + (f"（{stance}）" if stance else ""),
                                 career.date, "transfer", 1.5, "transfer", key)

    # --- the media's transfer rumours -------------------------------------------------------
    def _rumours(self, career: Career) -> None:
        store = career.store
        team = self.bridge.query(career.team_id, [LEAGUE, LEAGUE_ABOVE, "  61"])
        our_club = _ref(team.get("  61"))
        squad = {p["id"]: p["name"] for p in self.roster.players(career)}
        background = 0
        for comp in dict.fromkeys(c for c in (_ref(team.get(LEAGUE)), _ref(team.get(LEAGUE_ABOVE))) if c):
            for item in self.bridge.query(comp, [RUMOURS]).get(RUMOURS) or []:
                props = (item or {}).get("props") or {}
                text = _clean(props.get("XfxH"))
                player, from_club, to_club = _ref(props.get("srep")), _ref(props.get("lCCC")), _ref(props.get("cbOT"))
                if not text or not player:
                    continue
                ours = player in squad or our_club in (from_club, to_club)
                if not ours and background >= BACKGROUND_RUMOURS:
                    continue
                key = f"rumour:{player}:{to_club}:{props.get('toOT')}"
                if store.event_by_key(f"news:{key}"):
                    continue
                story, _, outlet = text.rpartition("(")
                story, outlet = (story.strip() or text), outlet.rstrip(") ").strip()
                name = squad.get(player) or self.story.name(player)
                payload = {"id": key, "title": story[:120], "body": story, "outlet": outlet,
                           "people": [player], "names": {player: name}, "origin": "rumour",
                           "weight": 1.7 if ours else 1.0, "fee": _clean(props.get("ET5o")),
                           "clubs": {c: self.story.name(c) for c in (from_club, to_club) if c},
                           "about_us": ours, "linked_in": our_club == to_club}
                if not store.add_event("news", career.date, payload, event_key=f"news:{key}"):
                    continue
                if not ours:
                    background += 1
                    continue
                if player in squad:
                    store.add_memory(player, f"媒体传闻（{outlet}）：{story}", career.date, "transfer", 1.6, "rumour", key)
