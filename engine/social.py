"""Social media: FM's own posts, posts about what happens, and the manager arguing with them.

- FM's own posts (fans, clubs, media, pundits under some news items) are
  read with the news (story.py) and kept as "social_post" events.
- For each new weighty event (a club match, a press headline, weighty news)
  a batch of posts is written by the model, but only when the manager opens
  the social feed (refresh): nothing is spent while nobody looks.
- The manager can reply to any post or post himself; the author and others
  answer. A row that blows up becomes news (which journalists and players
  then know about), and supporters' mood moves.

Posts are events ("social_post", keyed), so they follow saves like the rest.
"""
from __future__ import annotations

import threading
from datetime import date as Date

from . import story
from .bridge import BridgeUnavailable
from .career import Career
from .model import ModelError, Provider

MAX_AGE_DAYS = 5            # events older than this get no posts
MAX_EVENTS_PER_REFRESH = 2  # one model call each
FAN_MOOD_LIMIT = 10

AUTHOR = {"type": "object", "additionalProperties": False, "required": ["kind", "name", "handle", "text", "likes"],
          "properties": {"kind": {"type": "string", "enum": ["fan", "pundit", "journalist", "player", "media"]},
                         "name": {"type": "string"}, "handle": {"type": "string"}, "text": {"type": "string"},
                         "likes": {"type": "integer"}}}
POSTS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["posts"],
                "properties": {"posts": {"type": "array", "items": AUTHOR}}}
REPLY_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["replies", "uproar", "fan_mood_delta", "headline"],
                "properties": {"replies": {"type": "array", "items": AUTHOR},
                               "uproar": {"type": "integer", "minimum": 0, "maximum": 3,
                                          "description": "0 nobody cares … 3 it blows up"},
                               "fan_mood_delta": {"type": "integer", "minimum": -2, "maximum": 2},
                               "headline": {"type": "string", "description": "a news headline if uproar >= 2, else empty"}}}

RULES = """Write in Simplified Chinese, like real social media: short, informal, emotional; hashtags like #莱斯特城# \
are fine. Fans have made-up names and short handles; they are opinionated, often unfair, some loyal, some toxic \
(no slurs). Pundits and media are calmer and verified. Players may only be the real players listed. Facts (results, \
scores, scorers, ratings, injuries, quotes) must appear in the context; never invent them. Judge players by their \
position (do not expect defenders or defensive midfielders to score). likes: fans 0-300, pundits/media/players \
1000-50000."""


def _age(career: Career, d: str | None) -> int:
    try:
        return (Date.fromisoformat(career.date) - Date.fromisoformat(d or "")).days
    except ValueError:
        return 999


def _clean_author(a: dict, players: dict[str, str]) -> dict | None:
    kind, name, text = a.get("kind"), str(a.get("name") or "").strip()[:40], str(a.get("text") or "").strip()[:400]
    if not name or not text:
        return None
    ref = None
    if kind == "player":
        ref = players.get(name)
        if not ref:  # only real players may post as players
            kind = "fan"
    likes = a.get("likes") if isinstance(a.get("likes"), int) else 0
    return {"kind": kind, "name": name, "handle": str(a.get("handle") or "").strip()[:20], "ref": ref,
            "verified": kind in ("pundit", "journalist", "player", "media"), "text": text,
            "likes": max(0, min(likes, 200000))}


class SocialFeed:
    def __init__(self, story_engine: story.StoryEngine):
        self.story = story_engine
        self.bridge = story_engine.bridge
        self.lock = threading.Lock()

    # --- reading -----------------------------------------------------------------
    def posts(self, career: Career, limit: int = 80) -> list[dict]:
        items = [{"key": e["event_key"], "date": e["game_date"], **(e["payload"] or {})}
                 for e in career.store.events("social_post", limit=limit)]
        items.sort(key=lambda p: (p["date"] or "", p.get("likes") or 0), reverse=True)
        return items

    def pending(self, career: Career) -> list[dict]:
        """Recent weighty events that have no posts yet."""
        out = []
        done = {(e["payload"] or {}).get("source") for kind in ("social_post", "social_post_none")
                for e in career.store.events(kind, limit=500)}
        for kind in ("match", "press_answer", "news"):
            for e in career.store.events(kind, limit=40):
                p = e["payload"] or {}
                key = e.get("event_key") or f"{kind}:{e['game_date']}:{p.get('question', '')[:20]}"
                if key in done or _age(career, e["game_date"]) > MAX_AGE_DAYS:
                    continue
                if kind == "match" and not p.get("club"):
                    continue
                if kind == "press_answer" and not p.get("headline"):
                    continue
                if kind == "news" and (p.get("weight") or 1) < 1.5:
                    continue
                out.append({"key": key, "kind": kind, "date": e["game_date"], "payload": p})
        out.sort(key=lambda x: x["date"] or "", reverse=True)
        return out

    def fan_mood(self, career: Career) -> int:
        try:
            return int(career.store.get_meta("fan_mood", "0") or 0)
        except ValueError:
            return 0

    # --- context -----------------------------------------------------------------
    def _players(self, career: Career) -> dict[str, str]:
        out = {}
        try:
            for ref in self.bridge.team(career.team_id)["squad"]:
                out[self.story.name(ref)] = ref
        except BridgeUnavailable:
            pass
        return out

    def _describe(self, ev: dict) -> str:
        p = ev["payload"]
        if ev["kind"] == "match":
            goals = "、".join(f"{g['name']} {g['minute']}" for g in p.get("goals") or [])
            return (f"Match {ev['date']}: {p.get('competition')} {p.get('home_name')} {p['score'][0]}-{p['score'][1]} "
                    f"{p.get('away_name')} (the manager's team {p.get('result')}). Goals: {goals or 'none'}. "
                    f"Player of the match: {p.get('player_of_match_name') or 'n/a'}.")
        if ev["kind"] == "press_answer":
            return (f"Press conference {ev['date']}: asked 「{p.get('question')}」, the manager said 「{p.get('answer')}」. "
                    f"Headline: {p.get('headline')}")
        return f"News {ev['date']}: {p.get('title')}. {(p.get('body') or '')[:300]}"

    def _context(self, career: Career, players: dict[str, str]) -> str:
        mine = [f"{p['author']['name']}：「{p['text']}」→ 主教练回复「{r['text']}」"
                for p in self.posts(career, 40) for r in p.get("replies") or [] if r.get("kind") == "manager"][:5]
        return (f"The manager: {career.manager_name}, {career.team_name}. Today: {career.date}.\n"
                f"Supporters' mood toward the manager (-10 furious .. 10 adoring): {self.fan_mood(career)}\n"
                "Recent results and news:\n" + "\n".join(f"- {x}" for x in story.recent_world(career)) +
                "\nReal first-team players (only these may post as players): " + "、".join(players) +
                ("\nWhat the manager wrote on social media lately:\n" + "\n".join(f"- {x}" for x in mine) if mine else ""))

    # --- writing -----------------------------------------------------------------
    def refresh(self, career: Career, model: Provider) -> int:
        """Writes posts for up to MAX_EVENTS_PER_REFRESH new events. Returns how many posts were added."""
        if not self.lock.acquire(blocking=False):
            return 0
        try:
            players = self._players(career)
            added = 0
            for ev in self.pending(career)[:MAX_EVENTS_PER_REFRESH]:
                system = (f"You write the social media reaction to one football event in the world of Football "
                          f"Manager 27. {RULES} Write 5 to 8 posts from different people with different views.")
                prompt = f"{self._context(career, players)}\n\nTHE EVENT:\n{self._describe(ev)}"
                try:
                    out = model.complete(system, prompt, POSTS_SCHEMA)
                except ModelError:
                    break
                for i, a in enumerate(out.get("posts") or []):
                    post = _clean_author(a, players)
                    if not post:
                        continue
                    text, likes = post.pop("text"), post.pop("likes")
                    if career.store.add_event("social_post", ev["date"] or career.date, {
                            "origin": "engine", "source": ev["key"], "about": self._describe(ev)[:200],
                            "author": post, "text": text, "likes": likes, "replies": []},
                            event_key=f"social:{ev['key']}:{i}"):
                        added += 1
                if not added:  # the model wrote nothing usable: mark the event so it is not retried forever
                    career.store.add_event("social_post_none", career.date, {"source": ev["key"]})
            return added
        finally:
            self.lock.release()

    def reply(self, career: Career, model: Provider, key: str | None, text: str) -> dict:
        """The manager replies to a post (key) or posts himself (key None); others answer."""
        store = career.store
        players = self._players(career)
        manager = {"kind": "manager", "name": career.manager_name, "handle": "", "verified": True}
        if key:
            ev = store.event_by_key(key)
            if not ev or ev["kind"] != "social_post":
                raise ValueError("no such post")
            post = ev["payload"]
        else:
            key = f"social:manager:{career.date}:{len(store.events('social_post', limit=1000))}"
            post = {"origin": "manager", "source": "manager", "about": "", "author": manager, "text": text,
                    "likes": 0, "replies": []}
        thread = "\n".join([f"{post['author']['name']}: {post['text']}"] +
                           [f"{r['name']}: {r['text']}" for r in post.get("replies") or []])
        if post["origin"] != "manager" or post.get("replies"):
            thread += f"\n{career.manager_name} (the manager, verified): {text}"
        system = (f"You write the replies on social media after the manager of {career.team_name} joined in. {RULES} "
                  "Write 1 to 3 replies: usually the person he answered first, then others piling on (agreeing, mocking, "
                  "defending him). A manager arguing with fans is news if it gets heated or insulting.")
        try:
            out = model.complete(system, f"{self._context(career, players)}\n\nTHE THREAD:\n{thread}", REPLY_SCHEMA)
        except ModelError:
            raise
        replies = [r for r in (_clean_author(a, players) for a in out.get("replies") or []) if r][:3]
        mine = {"kind": "manager", "name": career.manager_name, "handle": "", "verified": True, "text": text,
                "likes": 0, "date": career.date}
        if post["origin"] == "manager" and not post.get("replies"):
            post["likes"] = sum(r["likes"] for r in replies) + 500
        else:
            post.setdefault("replies", []).append(mine)
        post.setdefault("replies", []).extend(r | {"date": career.date} for r in replies)
        if store.event_by_key(key):
            store.update_event(key, post)
        else:
            store.add_event("social_post", career.date, post, event_key=key)
        self._consequences(career, post, text, out)
        return {"key": key, "post": post}

    def _consequences(self, career: Career, post: dict, text: str, out: dict) -> None:
        store = career.store
        delta = out.get("fan_mood_delta") if isinstance(out.get("fan_mood_delta"), int) else 0
        mood = max(-FAN_MOOD_LIMIT, min(FAN_MOOD_LIMIT, self.fan_mood(career) + max(-2, min(2, delta))))
        store.set_meta("fan_mood", str(mood))
        uproar = out.get("uproar") if isinstance(out.get("uproar"), int) else 0
        headline = str(out.get("headline") or "").strip()[:80]
        if uproar >= 2 and headline:
            store.add_event("news", career.date, {
                "id": None, "title": headline, "weight": 1.5 + 0.5 * (uproar - 2), "people": [],
                "body": f"{career.manager_name}在社交媒体上回复{post['author']['name']}：「{text[:200]}」", "from": None,
                "origin": "social"}, event_key=f"news:social:{career.date}:{abs(hash(text)) % 10**8}")
            for ref in self._players(career).values():  # the dressing room hears about it
                store.add_memory(ref, f"主教练在社交媒体上和人吵了起来，上了新闻：「{headline}」", career.date,
                                 "social", 1.0, "news", f"social:{headline}")
