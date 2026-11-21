"""Story engine: what happens in FM becomes weighted world events and memories.

Two sources, both read from FM itself (nothing is invented here):

- Matches. Every first-team player's last-five-matches form lists the
  fixtures he played; each finished fixture of our team is read once
  (score, competition, goals with scorer/assister/minute, every player's
  rating, player of the match). It becomes a "match" event and a memory for
  each of our players: what the result was, how he played, his goals,
  assists, a very good or very poor rating, or that he did not play.
- News. The manager's inbox (FM news items). Each item becomes a "news"
  event, weighted by what it is about (an injury or an unhappy player
  matters, a routine scouting summary hardly does); people linked in the
  item who are in our squad remember it.

It runs only when FM's in-game clock moves (each hour the game advances), and
uses no model: all of this is plain code. The model is only called when the
manager talks to someone, and these events are handed to it then.

Events carry a weight so later steps (proactive messages, social media,
press questions) can pick the stories that matter. Everything is keyed, so
running again never duplicates; memory follows saves like everything else.
"""
from __future__ import annotations

from .bridge import Bridge, BridgeUnavailable, _text
from .career import Career
from .fmvalue import target_pairs

FORM_FIELDS = ("ZaLo", "E2cn", "phIw")  # a player's last-five-matches form (different competitions)
FIXTURE_FIELDS = ["etdf", "Tmhf", "Twaf", "shxf", "saxf", "srxf", "poNF", "kT1 ", "zKyn", "6dsa", "LmjO"]
NEWS_FIELDS = ["ltit", "ydob", "AMqC", "spin", "dinu"]
GOOD_RATING = 8.0
POOR_RATING = 6.0
MEMORY_DAYS = 90     # older results are kept as events, not memories
NO_SHOW_DAYS = 21    # "did not play" is remembered only for recent matches (squads change)

# (keywords in the title, weight). First match wins; default 1.
NEWS_WEIGHTS = (
    (("不满", "抱怨", "愤怒", "罢训", "要求离队", "unhappy", "complain"), 2.0),
    (("伤", "缺席", "injur"), 1.5),
    (("转会", "报价", "签约", "租借", "合同", "续约", "transfer", "bid", "contract", "loan"), 1.5),
    (("最佳", "称赞", "赞誉", "焦点", "心悦诚服", "成长", "award", "praise"), 1.5),
    (("新闻发布会", "采访", "press"), 1.0),
    (("训练日程", "比赛准备", "队伍选择建议", "对手报告", "球探报告", "战术建议", "考察完成", "招募", "汇总", "报告", "建议", "体能测试", "职责", "财务", "候选名单", "可参加",
      "schedule", "recruitment", "report", "summary"), 0.3),
)


def clean(text) -> str:
    """FM text without its zero-width break hints."""
    return (_text(text) or "").replace("​", "").strip()


def _links(value) -> list[str]:
    return list(value.get("links") or []) if isinstance(value, dict) else []


def _ref(value) -> str | None:
    return value.get("ref") if isinstance(value, dict) else None


def news_weight(title: str) -> float:
    low = title.lower()
    for words, weight in NEWS_WEIGHTS:
        if any(w in low for w in words):
            return weight
    return 1.0


class StoryEngine:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.names: dict[str, str] = {}
        self.clock: tuple[str, str] | None = None   # (career key, FM in-game time) last processed
        self.matches_checked: tuple[str, str] | None = None   # (career key, game date)
        self.new_news = False

    # --- polling -----------------------------------------------------------------
    def poll(self, career: Career) -> None:
        """Runs only when FM's in-game clock has moved (the player pressed continue);
        while the game stands still nothing is read. No model is used here."""
        clock = (career.key, career.clock or career.date)
        if clock == self.clock:
            return
        self.clock = clock
        self._news(career)
        # Matches: once per game day, and again when news arrives (results come with news).
        if self.matches_checked != (career.key, career.date) or self.new_news:
            self.new_news = False
            self._matches(career)
            self.matches_checked = (career.key, career.date)

    def name(self, ref: str | None) -> str:
        if not ref:
            return ""
        if ref not in self.names:
            try:
                self.names[ref] = clean(self.bridge.query(ref, ["eman"]).get("eman")) or ref
            except BridgeUnavailable:
                return ref
        return self.names[ref]

    # --- news --------------------------------------------------------------------
    def _news(self, career: Career) -> None:
        items = self.bridge.query(career.manager_id, ["SWEN"]).get("SWEN") or []
        store = career.store
        known = {e["event_key"] for e in store.events("news", limit=500)}
        squad = set(self.bridge.team(career.team_id)["squad"])
        for item in items:
            target = (item or {}).get("target") if isinstance(item, dict) else None
            news_id = target_pairs(target or "").get("dinu")
            if news_id is None or f"news:{news_id}" in known:
                continue
            try:
                v = self.bridge.query_target(target, NEWS_FIELDS)
            except BridgeUnavailable:
                return
            title, body = clean(v.get("ltit")), clean(v.get("ydob"))
            if not title:
                continue
            date = v.get("AMqC") if isinstance(v.get("AMqC"), str) else career.date
            people = list(dict.fromkeys(r for r in _links(v.get("ltit")) + _links(v.get("ydob"))
                                        if r.startswith("01")))
            weight = news_weight(title)
            payload = {"id": news_id, "title": title, "body": body[:1500], "from": _ref(v.get("spin")),
                       "people": people, "names": {r: self.name(r) for r in people[:6]}, "weight": weight}
            if not store.add_event("news", date, payload, event_key=f"news:{news_id}"):
                continue
            self.new_news = True
            if weight < 1:
                continue
            for person in people:
                if person in squad:
                    store.add_memory(person, f"新闻上说：「{title}」", date, "news", weight * 0.8, "news",
                                     f"news:{news_id}")

    # --- matches -----------------------------------------------------------------
    def _fixtures(self, squad: list[str]) -> dict[tuple, str]:
        """Finished fixtures our players appear in: (date, home, away) -> target."""
        out: dict[tuple, str] = {}
        for ref in squad:
            raw = self.bridge.query(ref, list(FORM_FIELDS))
            for field in FORM_FIELDS:
                for row in ((raw.get(field) or {}).get("props") or {}).get("XkGV") or []:
                    fixture = ((row or {}).get("props") or {}).get("QSIW") or {}
                    target = fixture.get("target") if isinstance(fixture, dict) else None
                    pairs = target_pairs(target or "")
                    key = (pairs.get("etdf"), pairs.get("Tmhf"), pairs.get("Twaf"))
                    if target and None not in key:
                        out.setdefault(key, target)
        return out

    def _matches(self, career: Career) -> None:
        squad = self.bridge.team(career.team_id)["squad"]
        store = career.store
        known = {e["event_key"] for e in store.events("match", limit=500)}
        for key, target in sorted(self._fixtures(squad).items()):
            event_key = "match:%s:%08x:%08x" % (key[0], key[1] & 0xffffffff, key[2] & 0xffffffff)
            if event_key in known:
                continue
            v = self.bridge.query_target(target, FIXTURE_FIELDS)
            if not str(v.get("shxf") or "").strip().isdigit():  # not played yet
                continue
            self._record_match(career, set(squad), event_key, v)

    def _record_match(self, career: Career, squad: set[str], event_key: str, v: dict) -> None:
        store = career.store
        date = v.get("etdf") if isinstance(v.get("etdf"), str) else career.date
        home, away = _ref(v.get("Tmhf")), _ref(v.get("Twaf"))
        hg, ag = int(v["shxf"]), int(str(v.get("saxf") or "0").strip() or 0)
        comp = clean(v.get("poNF"))
        line = f"{comp}：{self.name(home)} {hg}-{ag} {self.name(away)}"

        side: dict[str, str] = {}      # player -> "home" | "away"
        ratings: dict[str, float] = {}
        for field, which in (("kT1 ", "home"), ("zKyn", "away")):
            for row in v.get(field) or []:
                p = (row or {}).get("props") or {}
                ref, rating = _ref(p.get("rylp")), p.get("  52")
                if ref and isinstance(rating, (int, float)) and rating > 0:  # 0 = in the squad, no rating
                    ratings[ref] = round(float(rating), 1)
                    side[ref] = which
        goals: dict[str, list[str]] = {}
        assists: dict[str, int] = {}
        goal_list = []
        for row in v.get("6dsa") or []:
            p = (row or {}).get("props") or {}
            scorer, helper, minute = _ref(p.get("rylp")), _ref(p.get("I2f0")), clean(p.get("t2lQ"))
            if scorer:
                goals.setdefault(scorer, []).append(minute)
                goal_list.append({"scorer": scorer, "name": self.name(scorer), "assist": helper, "minute": minute})
            if helper:
                assists[helper] = assists.get(helper, 0) + 1
        best = _ref(((v.get("LmjO") or {}).get("props") or {}).get("yanz"))

        def result_for(which: str) -> str:
            ours, theirs = (hg, ag) if which == "home" else (ag, hg)
            return "赢了" if ours > theirs else "输了" if ours < theirs else "打平"

        club = career.team_id in (home, away)
        we = "home" if home == career.team_id else "away"
        played = [r for r in ratings if r in squad]
        if not club and not played:
            return
        gd = abs(hg - ag)
        weight = (1.0 + (1.0 if gd >= 3 else 0) + (0.5 if result_for(we) == "输了" else 0)) if club else 0.5
        payload = {
            "home": home, "away": away, "home_name": self.name(home), "away_name": self.name(away),
            "score": [hg, ag], "competition": comp, "club": club, "we_home": club and we == "home",
            "result": result_for(we) if club else None,
            "goals": goal_list, "player_of_match": best, "player_of_match_name": self.name(best),
            "ratings": {r: ratings[r] for r in played}, "weight": weight,
        }
        if not store.add_event("match" if club else "match_other", date, payload, event_key=event_key):
            return
        if self._age_days(career, date) > MEMORY_DAYS:
            return  # old results stay as events only

        for ref in squad:
            if ref not in ratings:
                if club and self._age_days(career, date) <= NO_SHOW_DAYS:
                    store.add_memory(ref, f"{line}，球队{result_for(we)}；他没有出场。", date, "match", 0.5,
                                     "match", event_key)
                continue
            rating = ratings[ref]
            mine = result_for(side[ref])
            head = f"{line}，球队{mine}" if club else f"他代表{self.name(home if side[ref] == 'home' else away)}出战，{line}，{mine}"
            parts = [f"他出场，评分 {rating:.1f}"]
            w = 1.0
            if ref in goals:
                n = len(goals[ref])
                parts.append(f"进了{n}球（{'、'.join(m for m in goals[ref] if m)}）")
                w = max(w, 2.0 if n >= 2 else 1.5)
            if assists.get(ref):
                parts.append(f"助攻{assists[ref]}次")
                w = max(w, 1.3)
            if ref == best:
                parts.append("被评为全场最佳")
                w = 2.0
            elif rating >= GOOD_RATING:
                parts.append("表现非常出色")
                w = max(w, 1.8)
            elif rating <= POOR_RATING:
                parts.append("表现很差")
                w = max(w, 1.5)
            store.add_memory(ref, f"{head}；{'，'.join(parts)}。", date, "match", w, "match", event_key)

    @staticmethod
    def _age_days(career: Career, date: str) -> int:
        from datetime import date as d
        try:
            return (d.fromisoformat(career.date) - d.fromisoformat(date)).days
        except ValueError:
            return 0


def recent_world(career: Career, days: int = 10, limit: int = 8) -> list[str]:
    """Short lines about recent results and the weightiest news, for prompts."""
    from datetime import date, timedelta
    try:
        since = (date.fromisoformat(career.date) - timedelta(days=days)).isoformat()
    except ValueError:
        since = None
    lines = []
    for e in career.store.events("match", since=since, limit=5):
        p = e["payload"] or {}
        scorers = "、".join(f"{g['name']} {g['minute']}" for g in p.get("goals") or [])
        lines.append(f"[{e['game_date']}] {p.get('competition')}：{p.get('home_name')} "
                     f"{p['score'][0]}-{p['score'][1]} {p.get('away_name')}（{p.get('result')}）"
                     + (f"，进球：{scorers}" if scorers else ""))
    news = [e for e in career.store.events("news", since=since, limit=200) if (e["payload"] or {}).get("weight", 1) >= 1]
    news.sort(key=lambda e: ((e["payload"] or {}).get("weight", 1), e["game_date"] or ""), reverse=True)
    for e in news[: max(0, limit - len(lines))]:
        lines.append(f"[{e['game_date']}] 新闻：{e['payload']['title']}")
    return lines
