"""Social media, Instagram style: posts about our club with pictures, and comments.

A post is one account's post about one thing, with a picture made from FM's
own graphics (a match card with both clubs' logos, a player's portrait, a
club logo) and the comments under it. Accounts: our club, media outlets,
pundits, our players (only real ones), fans.

- FM's own posts under news items about our club become one post each
  (the club / media post is the post, the rest are its comments).
- When the manager opens the feed, each new weighty event about the club
  (a club match, a press headline, weighty news) gets a post with comments,
  plus posts by the players involved; now and then a player posts about his
  life or training. One model call each, cached; nothing is spent unseen.
- Posts and comments may @mention the manager: those are notifications.
- The manager comments or posts; answers are written at once but appear only
  after some game hours (FM's clock), as notifications.
- A row that blows up becomes news; supporters' mood moves.
"""
from __future__ import annotations

import itertools
import random
import threading
from datetime import date as Date, datetime, timedelta

from . import story
from .bridge import BridgeUnavailable, _text
from .career import Career
from .model import ModelError, Provider

MAX_AGE_DAYS = 5            # events older than this get no posts
MAX_EVENTS_PER_REFRESH = 2  # one model call each
LIFE_POST_DAYS = 3          # a player life post at most this often
FAN_MOOD_LIMIT = 10
KINDS = ["club", "media", "pundit", "player", "fan", "journalist"]

PERSON = {"type": "object", "additionalProperties": False,
          "required": ["kind", "name", "handle", "text", "likes", "mentions_manager"],
          "properties": {"kind": {"type": "string", "enum": KINDS}, "name": {"type": "string"},
                         "handle": {"type": "string"}, "text": {"type": "string"}, "likes": {"type": "integer"},
                         "mentions_manager": {"type": "boolean", "description": "the text @mentions the manager"}}}
POST = {"type": "object", "additionalProperties": False,
        "required": ["kind", "name", "handle", "text", "likes", "mentions_manager", "picture", "comments"],
        "properties": PERSON["properties"] | {
            "picture": {"type": "string", "description": "'match' (match card), 'club' (club logo), or the exact "
                                                         "name of one of our players (his photo)"},
            "comments": {"type": "array", "items": PERSON}}}
EVENT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["posts"],
                "properties": {"posts": {"type": "array", "items": POST}}}
REPLY_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["replies", "uproar", "fan_mood_delta", "headline"],
                "properties": {"replies": {"type": "array", "items": PERSON},
                               "uproar": {"type": "integer", "minimum": 0, "maximum": 3,
                                          "description": "0 nobody cares … 3 it blows up"},
                               "fan_mood_delta": {"type": "integer", "minimum": -2, "maximum": 2},
                               "headline": {"type": "string", "description": "a news headline if uproar >= 2, else empty"}}}

RULES = """Write in Simplified Chinese, like real Instagram: short, informal, emotional, emoji and hashtags \
(#莱斯特城#) welcome. Fans have made-up names and short handles; opinionated, often unfair, some loyal, some toxic \
(no slurs). Pundits, media and the club are verified; the club account sounds official and upbeat. Players may only \
be the real players listed, writing in first person about themselves. People sometimes @mention the manager \
(write "@{manager}" in the text and set mentions_manager). Facts (results, scores, scorers, ratings, stats, \
injuries, quotes) must appear in the context; never invent them. Judge players by their position (no blaming \
defenders or defensive midfielders for not scoring). likes: fans 0-500, players and pundits 2000-80000, the club \
10000-150000."""


def _age(career: Career, d: str | None) -> int:
    try:
        return (Date.fromisoformat(career.date) - Date.fromisoformat(d or "")).days
    except ValueError:
        return 999


def clock(career: Career) -> datetime:
    """FM's in-game time ("2028-3-7 09:00")."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(career.clock or career.date, fmt)
        except ValueError:
            continue
    return datetime.strptime(career.date, "%Y-%m-%d")


def _visible(item: dict, now: datetime) -> bool:
    after = item.get("visible_after")
    return not after or datetime.fromisoformat(after) <= now


class SocialFeed:
    def __init__(self, story_engine: story.StoryEngine):
        self.story = story_engine
        self.bridge = story_engine.bridge
        self.lock = threading.Lock()
        self.clubs: dict[str, int | None] = {}   # team record -> club unique id (for logos)
        self.ids = itertools.count(int(datetime.now().timestamp()))

    # --- people and pictures ---------------------------------------------------------
    def club_uid(self, team: str | None) -> int | None:
        if not team:
            return None
        if team not in self.clubs:
            try:
                club = self.bridge.query(team, ["  61"]).get("  61")
                ref = club.get("ref") if isinstance(club, dict) else (team if team.startswith("03") else None)
                uid = self.bridge.query(ref, ["dinu"]).get("dinu") if ref else None
                self.clubs[team] = uid if isinstance(uid, int) else None
            except BridgeUnavailable:
                return None
        return self.clubs[team]

    def _players(self, career: Career) -> dict[str, dict]:
        """Our first team by name: record and portrait id."""
        out = {}
        try:
            for ref in self.bridge.team(career.team_id)["squad"]:
                raw = self.bridge.query(ref, ["eman", "dinu"])
                name = (_text(raw.get("eman")) or "").replace("​", "")
                if name:
                    out[name] = {"ref": ref, "uid": raw.get("dinu") if isinstance(raw.get("dinu"), int) else None}
        except BridgeUnavailable:
            pass
        return out

    def _author(self, career: Career, a: dict, players: dict[str, dict]) -> dict | None:
        kind, name = a.get("kind"), str(a.get("name") or "").strip()[:40]
        text = str(a.get("text") or "").strip()[:500]
        if not name or not text:
            return None
        ref = uid = club = None
        if kind == "player":
            p = players.get(name)
            if not p:
                kind = "fan"  # only real players post as players
            else:
                ref, uid = p["ref"], p["uid"]
        if kind == "club":
            name, club = career.team_name, self.club_uid(career.team_id)
        likes = a.get("likes") if isinstance(a.get("likes"), int) else 0
        return {"author": {"kind": kind, "name": name, "handle": str(a.get("handle") or "").strip()[:20], "ref": ref,
                           "uid": uid, "club_uid": club, "verified": kind != "fan"},
                "text": text, "likes": max(0, min(likes, 300000)), "mentions_manager": bool(a.get("mentions_manager"))}

    def _picture(self, career: Career, hint: str, ev: dict | None, players: dict[str, dict]) -> dict | None:
        p = (ev or {}).get("payload") or {}
        if hint == "match" and ev and ev["kind"] == "match":
            return {"type": "match", "home": {"name": p.get("home_name"), "club_uid": self.club_uid(p.get("home"))},
                    "away": {"name": p.get("away_name"), "club_uid": self.club_uid(p.get("away"))},
                    "score": p.get("score"), "competition": p.get("competition"), "date": ev.get("date")}
        if hint in players and players[hint]["uid"]:
            return {"type": "face", "uid": players[hint]["uid"], "name": hint}
        return {"type": "logo", "club_uid": self.club_uid(career.team_id), "name": career.team_name}

    # --- reading -----------------------------------------------------------------
    def _all(self, career: Career, limit: int = 300) -> list[dict]:
        return [{"key": e["event_key"], "date": e["game_date"], **(e["payload"] or {})}
                for e in career.store.events("social_post", limit=limit) if (e["payload"] or {}).get("v") == 2]

    def posts(self, career: Career, limit: int = 80) -> list[dict]:
        """What the manager can see now (answers appear only once their game time has come)."""
        now = clock(career)
        out = []
        for p in self._all(career):
            if not _visible(p, now):
                continue
            out.append(p | {"comments": [c for c in p.get("comments") or [] if _visible(c, now)]})
        out.sort(key=lambda p: (p.get("posted") or p["date"] or "", p.get("likes") or 0), reverse=True)
        return out[:limit]

    def notifications(self, career: Career) -> list[dict]:
        """Visible, unseen: @mentions of the manager and answers to him."""
        out = []
        for p in self.posts(career, 300):
            mine = {c["id"] for c in p.get("comments") or [] if c["author"]["kind"] == "manager"}
            if p.get("mentions_manager") and not p.get("seen"):
                out.append({"key": p["key"], "who": p["author"]["name"], "text": p["text"], "type": "mention"})
            for c in p.get("comments") or []:
                if c.get("seen") or c["author"]["kind"] == "manager":
                    continue
                if c.get("mentions_manager") or c.get("reply_to") in mine or (
                        p["author"]["kind"] == "manager" and c.get("answer")):
                    out.append({"key": p["key"], "who": c["author"]["name"], "text": c["text"],
                                "type": "reply" if c.get("reply_to") in mine or c.get("answer") else "mention"})
        return out

    def mark_seen(self, career: Career) -> None:
        now = clock(career)
        for p in self._all(career):
            changed = False
            if _visible(p, now) and p.get("mentions_manager") and not p.get("seen"):
                p["seen"] = changed = True
            for c in p.get("comments") or []:
                if _visible(c, now) and not c.get("seen"):
                    c["seen"] = changed = True
            if changed:
                key = p.pop("key")
                p.pop("date", None)
                career.store.update_event(key, p)

    def pending(self, career: Career) -> list[dict]:
        """Recent weighty events about the club that have no posts yet."""
        out = []
        done = {p.get("source") for p in self._all(career, 1000)} | {
            (e["payload"] or {}).get("source") for e in career.store.events("social_post_none", limit=500)}
        for kind in ("match", "press_answer", "news"):
            for e in career.store.events(kind, limit=40):
                p = e["payload"] or {}
                key = e.get("event_key") or f"{kind}:{e['game_date']}:{p.get('question', '')[:20]}"
                if key in done or _age(career, e["game_date"]) > MAX_AGE_DAYS:
                    continue
                if (kind == "match" and not p.get("club")) or (kind == "press_answer" and not p.get("headline")) or \
                        (kind == "news" and ((p.get("weight") or 1) < 1.5 or p.get("origin") == "social")):
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
    def _describe(self, ev: dict) -> str:
        p = ev["payload"]
        if ev["kind"] == "match":
            goals = "、".join(f"{g['name']} {g['minute']}" for g in p.get("goals") or [])
            ratings = "、".join(f"{self.story.name(r)} {v}" for r, v in sorted((p.get("ratings") or {}).items(),
                                                                             key=lambda kv: -kv[1]))
            return (f"Match {ev['date']}: {p.get('competition')} {p.get('home_name')} {p['score'][0]}-{p['score'][1]} "
                    f"{p.get('away_name')} (the manager's team {p.get('result')}). Goals: {goals or 'none'}. "
                    f"Player of the match: {p.get('player_of_match_name') or 'n/a'}. Our ratings: {ratings}.")
        if ev["kind"] == "press_answer":
            return (f"Press conference {ev['date']}: asked 「{p.get('question')}」, the manager said 「{p.get('answer')}」. "
                    f"Headline: {p.get('headline')}")
        return f"News {ev['date']}: {p.get('title')}. {(p.get('body') or '')[:300]}"

    def _context(self, career: Career, players: dict[str, dict]) -> str:
        mine = [f"「{c['text']}」" for p in self._all(career, 60) for c in p.get("comments") or []
                if c["author"]["kind"] == "manager"][:5]
        return (f"The manager: {career.manager_name} (handle @{career.manager_name}), {career.team_name}. "
                f"Today: {career.date}.\n"
                f"Supporters' mood toward the manager (-10 furious .. 10 adoring): {self.fan_mood(career)}\n"
                "Recent results and news:\n" + "\n".join(f"- {x}" for x in story.recent_world(career)) +
                "\nOur real first-team players (only these may post as players): " + "、".join(players) +
                ("\nWhat the manager wrote on social media lately:\n" + "\n".join(f"- {x}" for x in mine) if mine else ""))

    # --- FM's own posts ---------------------------------------------------------------
    def ingest_fm(self, career: Career) -> None:
        """FM's posts under news about our club: the club/media post with the rest as its comments."""
        store = career.store
        try:
            squad = set(self.bridge.team(career.team_id)["squad"])
            club = self.bridge.query(career.team_id, ["  61"]).get("  61")
        except BridgeUnavailable:
            return
        ours = squad | {career.team_id} | ({club["ref"]} if isinstance(club, dict) and club.get("ref") else set())
        for e in store.events("news", limit=300):
            p = e["payload"] or {}
            posts = p.get("fm_posts") or []
            key = f"social:fm:{p.get('id')}"
            if not posts or p.get("id") is None or store.event_by_key(key):
                continue
            links = set(p.get("people") or []) | {l for x in posts for l in x.get("links") or []}
            if not links & ours:
                store.add_event("social_post_none", e["game_date"], {"source": key}, event_key=key)
                continue
            main = next((x for x in posts if x["kind"] in ("club", "media", "competition", "pundit")), posts[0])
            def built(x):
                return {"author": {"kind": x["kind"] if x["kind"] != "competition" else "club", "name": x["name"],
                                   "handle": x.get("handle") or "", "ref": x.get("ref"), "uid": None,
                                   "club_uid": self.club_uid(x["ref"]) if x["kind"] == "club" and x.get("ref") else None,
                                   "verified": x.get("verified", False)},
                        "text": x["text"], "likes": x.get("likes") or 0, "mentions_manager": False}
            picture = None
            if main["kind"] == "club" and main.get("ref"):
                picture = {"type": "logo", "club_uid": self.club_uid(main["ref"]), "name": main["name"]}
            comments = [self._comment(built(x), date=e["game_date"], seen=True) for x in posts if x is not main]
            store.add_event("social_post", e["game_date"], {
                "v": 2, "origin": "fm", "source": key, "about": p.get("title") or "", **built(main),
                "picture": picture, "comments": comments, "posted": f"{e['game_date']}T12:00:00"}, event_key=key)

    # --- writing -----------------------------------------------------------------
    def _comment(self, built: dict, **extra) -> dict:
        return {"id": next(self.ids), **built, **extra}

    def _store_post(self, career: Career, source: str, about: str, built: dict, picture: dict | None,
                    comments: list[dict], posted: str, key: str, origin: str = "engine") -> bool:
        return career.store.add_event("social_post", career.date, {
            "v": 2, "origin": origin, "source": source, "about": about, **built, "picture": picture,
            "comments": comments, "posted": posted}, event_key=key)

    def refresh(self, career: Career, model: Provider) -> int:
        """Posts for up to MAX_EVENTS_PER_REFRESH new events (and maybe a player's life post)."""
        if not self.lock.acquire(blocking=False):
            return 0
        try:
            players = self._players(career)
            added = 0
            now = clock(career).isoformat()
            for ev in self.pending(career)[:MAX_EVENTS_PER_REFRESH]:
                system = (f"You write the Instagram reaction to one event at {career.team_name} in the world of "
                          f"Football Manager 27. {RULES.replace('{manager}', career.manager_name)} Write 1 to 3 posts: "
                          f"usually one by the club or a media outlet with a picture, and posts by players involved "
                          f"(the player of the match, someone who played badly, a player in the news). Each post has "
                          f"4 to 8 comments from different people with different views.")
                prompt = f"{self._context(career, players)}\n\nTHE EVENT:\n{self._describe(ev)}"
                try:
                    out = model.complete(system, prompt, EVENT_SCHEMA)
                except ModelError:
                    break
                n = 0
                for i, raw in enumerate(out.get("posts") or []):
                    built = self._author(career, raw, players)
                    if not built:
                        continue
                    hint = str(raw.get("picture") or "")
                    if built["author"]["kind"] == "player":
                        hint = built["author"]["name"]
                    comments = [self._comment(c, date=career.date) for c in
                                (self._author(career, x, players) for x in raw.get("comments") or []) if c]
                    if self._store_post(career, ev["key"], self._describe(ev)[:200], built,
                                        self._picture(career, hint, ev, players), comments, now,
                                        f"social:{ev['key']}:{i}"):
                        n += 1
                added += n
                if not n:  # nothing usable: do not retry this event forever
                    career.store.add_event("social_post_none", career.date, {"source": ev["key"]})
            added += self._life_post(career, model, players, now)
            return added
        finally:
            self.lock.release()

    def _life_post(self, career: Career, model: Provider, players: dict[str, dict], now: str) -> int:
        last = max((p.get("date") or "" for p in self._all(career) if p.get("source", "").startswith("life:")),
                   default="")
        if last and _age(career, last) < LIFE_POST_DAYS or not players:
            return 0
        recent = {p["author"]["name"] for p in self._all(career) if p["author"]["kind"] == "player"
                  and _age(career, p.get("date")) <= LIFE_POST_DAYS}
        name = random.choice([n for n in players if n not in recent] or list(players))
        canon = career.store.canon(players[name]["ref"])
        memories = [m["text"] for m in career.store.memories(players[name]["ref"])[-4:]]
        system = (f"You write one Instagram post by {name}, a player at {career.team_name}, about his everyday life "
                  f"or training (not a match result), with 3 to 6 comments. "
                  f"{RULES.replace('{manager}', career.manager_name)}")
        prompt = (f"{self._context(career, players)}\n\nAbout {name}:\n" +
                  "\n".join(f"- {c['key']}: {c['value']}" for c in canon) + "\n" +
                  "\n".join(f"- {m}" for m in memories) + f"\n\nThe post is by {name} (kind player, picture '{name}').")
        try:
            out = model.complete(system, prompt, EVENT_SCHEMA)
        except ModelError:
            return 0
        for raw in (out.get("posts") or [])[:1]:
            raw = raw | {"kind": "player", "name": name}
            built = self._author(career, raw, players)
            if not built:
                return 0
            comments = [self._comment(c, date=career.date) for c in
                        (self._author(career, x, players) for x in raw.get("comments") or []) if c]
            return int(self._store_post(career, f"life:{career.date}:{name}", "", built,
                                        self._picture(career, name, None, players), comments, now,
                                        f"social:life:{career.date}:{players[name]['ref']}"))
        return 0

    def reply(self, career: Career, model: Provider, key: str | None, text: str, reply_to: int | None = None) -> dict:
        """The manager comments (on a post, or answering a comment) or posts himself.
        Answers are written now and appear after a few game hours."""
        store = career.store
        players = self._players(career)
        now = clock(career)
        me = {"kind": "manager", "name": career.manager_name, "handle": career.manager_name, "ref": career.manager_id,
              "uid": career.manager_uid, "club_uid": None, "verified": True}
        new_post = not key
        if key:
            ev = store.event_by_key(key)
            if not ev or ev["kind"] != "social_post" or (ev["payload"] or {}).get("v") != 2:
                raise ValueError("no such post")
            post = ev["payload"]
        else:
            key = f"social:manager:{now.isoformat()}"
            post = {"v": 2, "origin": "manager", "source": "manager", "about": "", "author": me, "text": text,
                    "likes": 0, "mentions_manager": False, "picture": None, "comments": [], "posted": now.isoformat()}
        target = next((c for c in post.get("comments") or [] if c.get("id") == reply_to), None)
        thread = "\n".join([f"[post] {post['author']['name']}: {post['text']}"] +
                           [f"{c['author']['name']}: {c['text']}" for c in post.get("comments") or []])
        if not new_post:
            thread += (f"\n{career.manager_name} (the manager) answers "
                       f"{target['author']['name'] if target else post['author']['name']}: {text}")
        system = (f"You write the answers on Instagram after the manager of {career.team_name} joined in. "
                  f"{RULES.replace('{manager}', career.manager_name)} Write 1 to 4 answers: usually the person he "
                  f"answered first, then others piling on (agreeing, mocking, defending him). A manager arguing with "
                  f"fans is news if it gets heated or insulting.")
        out = model.complete(system, f"{self._context(career, players)}\n\nTHE THREAD:\n{thread}", REPLY_SCHEMA)
        mine = None
        if new_post:
            post["likes"] = random.randint(800, 6000)
        else:
            mine = self._comment({"author": me, "text": text, "likes": 0, "mentions_manager": False},
                                 date=career.date, reply_to=reply_to, seen=True)
            post.setdefault("comments", []).append(mine)
        delay = 0
        for a in out.get("replies") or []:
            built = self._author(career, a, players)
            if not built:
                continue
            delay += random.randint(1, 5)  # answers trickle in over game hours
            post["comments"].append(self._comment(built, date=career.date, reply_to=mine["id"] if mine else None,
                                                  answer=True, visible_after=(now + timedelta(hours=delay)).isoformat()))
        if store.event_by_key(key):
            store.update_event(key, post)
        else:
            store.add_event("social_post", career.date, post, event_key=key)
        self._consequences(career, post, text, out)
        return {"key": key, "answers_in_hours": delay}

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
                "origin": "social"}, event_key=f"news:social:{career.date}:{next(self.ids)}")
            for p in self._players(career).values():  # the dressing room hears about it
                store.add_memory(p["ref"], f"主教练在社交媒体上和人吵了起来，上了新闻：「{headline}」", career.date,
                                 "social", 1.0, "news", f"social:{headline}")
