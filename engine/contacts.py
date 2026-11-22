"""People who get in touch on their own: decided by code, written only when opened.

Whenever the world records something (a match, news, a press answer), this
module decides, with plain rules and no model, whether someone would reach
out to the manager about it:

- after a defeat or a big result, a journalist from the roster (the one
  who likes the manager least, after a defeat) wants a reaction;
- the player who played worst wants to talk about it;
- the player of the match shares his joy (sometimes);
- a player who missed the last two club matches asks about playing time;
- a player in weighty news (an injury, praise, unrest) talks about it;
- a player the manager talked about at a press conference reacts.

Each becomes an unread "contact" with a short preview. The model writes the
opening message only when the manager opens it (dialogue.open_contact);
from there it is an ordinary conversation. At most MAX_PER_DAY new
contacts per game day, one unread per person, never twice for the same
reason.
"""
from __future__ import annotations

import hashlib
import random
from datetime import date as Date

from .career import Career
from .story import StoryEngine

MAX_PER_DAY = 3
REPORT_EVERY_DAYS = 3     # a teammate tells on someone at most this often
COMPLAINT_EVERY_DAYS = 2  # an unhappy player raises his issue himself at most this often
UNHAPPY_WORDS = ("不满", "不开心", "失望", "不高兴", "沮丧", "担心", "不满意", "unhappy", "frustrat", "concern")
SETTINGS = ("吃饭的时候", "训练间隙", "更衣室里", "大巴上", "健身房里", "理疗室里", "队内聚餐时")
MAX_AGE_DAYS = 3          # older events never trigger contacts (e.g. after the first run)
POOR_RATING = 6.0
INJURY_WORDS = ("伤", "缺席", "injur")


FUTURE_EVERY_DAYS = 5   # a journalist asks the manager without a club about his plans
JOB_WORDS = ("面试", "职位", "空缺", "解雇", "下课", "帅位", "招聘", "主教练", "求职", "离任")


def media_contact(career: Career) -> dict | None:
    """A media outlet seen in FM's news, as a contact of its own (id "ee" + 6 hex: never an FM record)."""
    outlets = [o for o in ((e["payload"] or {}).get("outlet") for e in career.store.events("news", limit=300)) if o]
    if not outlets:
        return None
    outlet = random.choice(outlets[-40:])
    ref = "ee" + hashlib.sha1(outlet.encode()).hexdigest()[:6]
    j = {"id": ref, "name": f"{outlet}记者", "outlet": outlet, "uid": None, "media": True}
    career.store.add_event("journalist", career.date, j, event_key=f"journalist:{ref}")
    return {"person": ref, "name": j["name"], "role": "journalist", "outlet": outlet, "uid": None}


def _age(career: Career, d: str | None) -> int:
    try:
        return (Date.fromisoformat(career.date) - Date.fromisoformat(d or "")).days
    except ValueError:
        return 999


class ContactEngine:
    def __init__(self, story: StoryEngine, roster=None):
        self.story = story
        self.bridge = story.bridge
        self.roster = roster  # players out on loan never get in touch first (they can still be messaged)

    # --- polling -----------------------------------------------------------------
    def poll(self, career: Career) -> None:
        store = career.store
        cursor = int(store.get_meta("contacts_cursor", "0") or 0)
        events = store.events_after(cursor)
        candidates: list[tuple[float, dict]] = []
        if store.get_meta("dressing_room_checked") != career.date:  # once a game day
            store.set_meta("dressing_room_checked", career.date)
            try:
                candidates += self._between_jobs(career) if career.unemployed else self._dressing_room(career)
            except Exception:
                pass
        if not events and not candidates:
            return
        if events:
            store.set_meta("contacts_cursor", str(events[-1]["id"]))
        for e in events:
            if _age(career, e["game_date"]) > MAX_AGE_DAYS:
                continue
            try:
                candidates += self._from_event(career, e)
            except Exception:  # one odd event must not stop the rest
                continue
        today = [c for c in store.events("contact", limit=200) if (c["payload"] or {}).get("created") == career.date]
        room = MAX_PER_DAY - len(today)
        unread = {(c["payload"] or {}).get("person") for c in store.events("contact", limit=200)
                  if (c["payload"] or {}).get("status") == "unread"}
        away = {p["id"] for p in self.roster.players(career) if not p["at_club"]} if self.roster else set()
        for _, c in sorted(candidates, key=lambda x: -x[0]):
            if room <= 0:
                break
            if c["person"] in unread or (c.get("role") == "player" and c["person"] in away):
                continue
            c |= {"status": "unread", "created": career.date}
            if store.add_event("contact", career.date, c, event_key=f"contact:{c['source']}:{c['person']}"):
                unread.add(c["person"])
                room -= 1

    # --- the dressing room: unhappy players, and teammates who tell the manager -----------
    def _dressing_room(self, career: Career) -> list[tuple[float, dict]]:
        if not self.roster:
            return []
        store = career.store
        players = self.roster.at_club(career)
        unhappy = [(p, [h for h in p["happiness"] if any(w in h for w in UNHAPPY_WORDS)]) for p in players]
        unhappy = [(p, issues) for p, issues in unhappy if issues]
        if not unhappy:
            return []
        out = []
        last_report = store.get_meta("last_report") or ""
        if not last_report or _age(career, last_report) >= REPORT_EVERY_DAYS:
            subject, issues = random.choice(unhappy)
            def closeness(p):
                rel = store.relation(p["id"])
                return (rel["trust"] + rel["liking"], p["group"] == subject["group"], "领袖" in p["influence"])
            informants = sorted((p for p in players if p["id"] != subject["id"]), key=closeness, reverse=True)
            if informants:
                spy = informants[0]
                where = random.choice(SETTINGS)
                out.append((1.7, self._person(spy["id"]) | {
                    "source": f"report:{career.date}:{subject['id']}",
                    "preview": f"{spy['name']}有话想私下跟你说",
                    "reason": f"You are close to the manager. {where}, you heard your teammate {subject['name']} "
                              f"({subject['position']}) talk about something he is unhappy about. FM's own words for "
                              f"his situation: 「{issues[0]}」. Tell the manager quietly what {subject['name']} said "
                              f"and what he seems to want; you may add your own view. Do not invent any other facts."}))
                store.set_meta("last_report", career.date)
        last_complaint = store.get_meta("last_complaint") or ""
        if not last_complaint or _age(career, last_complaint) >= COMPLAINT_EVERY_DAYS:
            p, issues = random.choice(unhappy)
            out.append((1.5, self._person(p["id"]) | {
                "source": f"complaint:{career.date}:{p['id']}",
                "preview": f"{p['name']}想和你谈谈",
                "reason": f"Something bothers you at the club. FM's own words for your situation: 「{issues[0]}」. "
                          f"You raise it with the manager (training, your position, playing time, how you are treated "
                          f"— whatever this is about), in your own way."}))
            store.set_meta("last_complaint", career.date)
        return out

    # --- between jobs: the manager without a club ---------------------------------------
    def _between_jobs(self, career: Career) -> list[tuple[float, dict]]:
        store = career.store
        out = []
        last = store.get_meta("last_future_question") or ""
        if not last or _age(career, last) >= FUTURE_EVERY_DAYS:
            j = self._journalist(career, prefer_hostile=False)
            if j:
                former = store.get_meta("former_team_name") or ""
                out.append((1.6, j | {
                    "source": f"future:{career.date}", "preview": f"{j['outlet']} 的{j['name']}想问问你的下一步",
                    "reason": f"The manager is currently without a club" + (f" (his last club: {former})" if former else "")
                              + ". You want to know his plans: is he applying for jobs, which kind of club, has anyone "
                              "called, what did he learn from his last job. Use only vacancies and sackings that appear "
                              "in the recent news; never invent offers or interviews."}))
                store.set_meta("last_future_question", career.date)
        return out

    def _former_player(self, career: Career, team: str | None) -> str | None:
        """The player of a club the manager left who liked him most (he may still write)."""
        squad = self.bridge.team(team)["squad"] if team else []
        warm = sorted(squad, key=lambda r: -(career.store.relation(r)["trust"] + career.store.relation(r)["liking"]))
        return next((r for r in warm if career.store.relation(r)["liking"] > 0), None)

    def _person(self, ref: str, role: str = "player") -> dict:
        return {"person": ref, "name": self.story.name(ref), "role": role}

    # --- rules -------------------------------------------------------------------
    def _from_event(self, career: Career, e: dict) -> list[tuple[float, dict]]:
        p, key, kind = e["payload"] or {}, e.get("event_key") or f"event{e['id']}", e["kind"]
        out: list[tuple[float, dict]] = []
        if kind == "match" and p.get("club"):
            line = f"{p.get('competition')}：{p.get('home_name')} {p['score'][0]}-{p['score'][1]} {p.get('away_name')}"
            opp = p.get("away_name") if p.get("we_home") else p.get("home_name")
            ratings = p.get("ratings") or {}
            gd = abs(p["score"][0] - p["score"][1])
            if p.get("result") == "输了" or gd >= 3:
                j = self._journalist(career, prefer_hostile=p.get("result") == "输了")
                if j:
                    out.append((2.0 + gd * 0.3, j | {
                        "source": key, "preview": f"{j['outlet']} 的{j['name']}想就{line}问你几句",
                        "reason": f"The match {line} just ended (the manager's team {p.get('result')}). You want his "
                                  f"reaction and a quote for your story."}))
            if ratings:
                worst, rating = min(ratings.items(), key=lambda kv: kv[1])
                if rating <= POOR_RATING:
                    out.append((1.8, self._person(worst) | {
                        "source": key, "preview": f"{self.story.name(worst)}想聊聊对{opp}那场的表现",
                        "reason": f"You played poorly in {line} (rating {rating}). You want to talk to the manager about "
                                  f"it: apologise, explain, or ask what he wants from you — whatever fits your character."}))
            best = p.get("player_of_match")
            if best in ratings and random.random() < 0.5:
                out.append((1.2, self._person(best) | {
                    "source": key, "preview": f"{self.story.name(best)}给你发来消息",
                    "reason": f"You were named player of the match in {line}. You are happy and want to share it "
                              f"with the manager."}))
            out += self._playing_time(career, key)
        elif kind == "news" and p.get("origin") in ("rumour", "transfer") and p.get("about_us", True):
            squad = set(self.bridge.team(career.team_id)["squad"])
            title = p.get("title") or ""
            ours = [r for r in p.get("people") or [] if r in squad][:1]
            clubs = "、".join((p.get("clubs") or {}).values())
            if ours and random.random() < 0.6:  # the player himself: flattered, unsettled, or loyal, as fits him
                ref = ours[0]
                out.append((1.4, self._person(ref) | {
                    "source": key, "preview": f"{self.story.name(ref)}想和你聊聊转会传闻",
                    "reason": f"Transfer talk about you: 「{title}」 ({clubs}). You want to know where you stand with "
                              f"the manager, or tell him how you feel about it: flattered, tempted, unsettled or "
                              f"loyal, whatever fits your age, ambitions, contract and happiness at the club."}))
            j = self._journalist(career, prefer_hostile=False)
            if j and (ours or p.get("linked_in")):
                out.append((1.3, j | {
                    "source": key + ":j", "preview": f"{j['outlet']} 的{j['name']}想问你转会传闻",
                    "reason": f"Transfer rumour: 「{title}」 ({p.get('outlet') or 'the media'}). You want the manager's "
                              f"comment for your story: is it true, is the player for sale, is the club interested?"}))
        elif kind == "job_change":
            if p.get("from"):  # he left his club: someone from the old dressing room writes
                ref = self._former_player(career, p["from"])
                if ref:
                    out.append((2.0, self._person(ref) | {
                        "source": key, "preview": f"{self.story.name(ref)}给你发来消息",
                        "reason": f"Your manager {career.manager_name} has just left {p.get('from_name')}"
                                  + (f" for {p.get('to_name')}" if p.get("to") else "") + ". You write to him about it, "
                                  "as fits your relationship and your character."}))
            j = self._journalist(career, prefer_hostile=False)
            if j:
                what = (f"has just taken over at {p.get('to_name')}" if p.get("to") else
                        f"is no longer the manager of {p.get('from_name')}")
                out.append((1.8, j | {
                    "source": key + ":j", "preview": f"{j['outlet']} 的{j['name']}想采访你",
                    "reason": f"{career.manager_name} {what}. You want his first words for your story."}))
        elif kind == "news" and career.unemployed and any(w in (p.get("title") or "") for w in JOB_WORDS):
            j = self._journalist(career, prefer_hostile=False)
            if j:
                out.append((1.4, j | {
                    "source": key, "preview": f"{j['outlet']} 的{j['name']}想问你：「{p.get('title')}」",
                    "reason": f"News: 「{p.get('title')}」. The manager is without a club; you ask whether he is "
                              f"interested or has been contacted. Never claim he was offered anything."}))
        elif kind == "news" and p.get("origin") == "manager_talk":
            j = self._journalist(career, prefer_hostile=False)
            if j:
                out.append((1.7, j | {
                    "source": key, "preview": f"{j['outlet']} 的{j['name']}想听你回应：「{p.get('title')}」",
                    "reason": f"Another manager talked about {career.manager_name} or his team: 「{p.get('title')}」. "
                              f"What he said: {(p.get('body') or '')[:500]}. You want the manager's response."}))
        elif kind == "news" and (p.get("weight") or 1) >= 1.5:
            squad = set(self.bridge.team(career.team_id)["squad"])
            title = p.get("title") or ""
            for ref in [r for r in p.get("people") or [] if r in squad][:1]:
                topic = "injury" if any(w in title.lower() for w in INJURY_WORDS) else "news"
                out.append((1.6 if topic == "injury" else 1.0, self._person(ref) | {
                    "source": key, "preview": f"{self.story.name(ref)}发来消息：关于「{title}」",
                    "reason": f"News about you: 「{title}」. You want to tell the manager how you feel about it."}))
        elif kind == "press_answer":
            squad = set(self.bridge.team(career.team_id)["squad"])
            for ref in [r for r in p.get("about") or [] if r in squad][:1]:
                out.append((2.2, self._person(ref) | {
                    "source": key, "preview": f"{self.story.name(ref)}看到了你在发布会上说的话",
                    "reason": f"At a press conference the manager was asked 「{p.get('question')}」 and said "
                              f"「{p.get('answer')}」. It concerns you, and you react to what he said publicly."}))
        return out

    def _journalist(self, career: Career, prefer_hostile: bool) -> dict | None:
        roster = [e["payload"] for e in career.store.events("journalist", limit=200) if (e["payload"] or {}).get("id")]
        if not roster:  # no journalist known by name yet: an outlet from the news gets in touch itself
            return media_contact(career)
        if prefer_hostile:
            roster.sort(key=lambda j: career.store.relation(j["id"])["liking"])
            pick = roster[0]
        else:
            pick = random.choice(roster)
        return {"person": pick["id"], "name": pick.get("name") or pick["id"], "role": "journalist",
                "outlet": pick.get("outlet") or "", "uid": pick.get("uid")}

    def _playing_time(self, career: Career, key: str) -> list[tuple[float, dict]]:
        """A regular (two or more of the four matches before) left out of the last two asks about it."""
        recent = [(e["payload"] or {}).get("ratings") or {} for e in career.store.events("match", limit=6)]
        if len(recent) < 4:
            return []
        last_two, before = recent[-2:], recent[-6:-2]
        played_lately = set(last_two[0]) | set(last_two[1])
        regulars = [r for r in self.bridge.team(career.team_id)["squad"]
                    if sum(r in m for m in before) >= 2 and r not in played_lately]
        if not regulars:
            return []
        ref = random.choice(regulars)
        return [(1.4, self._person(ref) | {
            "source": f"playing-time:{ref}:{len(recent)}:{key}", "preview": f"{self.story.name(ref)}想和你谈谈出场时间",
            "reason": "You have not played in the last two matches. You want to talk to the manager about your "
                      "playing time (calm or frustrated, as fits your character and situation)."})]
