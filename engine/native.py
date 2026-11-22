"""Native FM interactions -> world memory.

FM's own dialog screens (player conversations, press conferences, media
questions) keep their options and settle morale themselves. They all use
the same "SpeakTo" system, which this module watches:

- the tree on screen (SpeakToReference.itca) is cached: option groups
  (N2w4, each DwoR heading with T3E4 options: lWh9 sentence, p3fs id,
  izOE tone 1 positive / 2 neutral / 4 negative), the person being spoken
  with and what they said (nzfT: h1J person, xj09[].nrtS speech bubble,
  gmyy.Xr0u their body language) and extra actions (1VLL: "no comment",
  "storm out", each with its own event id in neve);
- choosing an option sends an event to the SpeakTo target with the person
  (h1J) and the option (p3fs); the event id is the option's neve
  (34 in player conversations, another id for press answers);
- player conversations: FM also logs an audit event naming the action
  (e.g. PraisePlayerPerformance), which sets the relationship change.

Player conversations become a pair of messages in that person's thread, a
memory, a relationship change and an "fm_conversation" event. Press
answers become the question and answer in the journalist's thread, a
memory for the journalist and for our players the question was about, a
"press_answer" event, and the journalist joins the roster of known
journalists (later steps let them contact the manager).

Every step is best effort: anything unreadable is skipped, never raised.
"""
from __future__ import annotations

import time

from .bridge import Bridge, BridgeUnavailable, _text
from .career import Career
from .fmvalue import decode, target_pairs

SPEAK_TO_TYPE = 102        # SpeakToReference: topics and options
CONVERSATION_TYPE = 23     # ConversationEditSessionReference: the open conversation
PRESS_TYPE = 81            # PressConferenceEditSessionReference: the press conference in progress
CLOSED_SECONDS = 30              # a dialog FM closed is not read for this long (the same target may be reused later)
LEAVE_EVENTS = {31, 827217993}   # end chat / leave the press conference, storm out
PRESS_ATTENDED = 876038760       # sent by FM's interface when the manager leaves a press conference
MATCH_TYPES = {61, 64}     # match and match-tactics targets: a match is being played or has just ended
MATCH_QUIET_SECONDS = 240  # the engine leaves FM alone this long after any match activity
PENDING_SECONDS = 8        # wait this long for FM's audit event before writing
ACTIVE_SECONDS = 90        # after any dialog activity, re-read the dialog quickly
TONES = {1: "积极", 2: "中立", 4: "消极"}

# Relationship change by the kind of FM action (matched on the audit name).
ACTION_EFFECTS = (
    ("Praise", {"liking": 1, "respect": 1}),
    ("Advice", {"respect": 1}),
    ("Warn", {"liking": -1}),
    ("Discipline", {"liking": -2, "trust": -1}),
    ("Concern", {"trust": 1}),
    ("Promise", {"trust": 1}),
)
# A journalist's feelings after an answer, by tone; extra actions by their text.
PRESS_TONE_EFFECTS = {1: {"liking": 1}, 2: {}, 4: {"liking": -1, "respect": -1}}
PRESS_ACTION_EFFECTS = (("暴怒", {"liking": -3, "respect": -2}), ("无可奉告", {"liking": -1}),
                        ("Storm", {"liking": -3, "respect": -2}), ("No comment", {"liking": -1}))


def _props(v) -> dict:
    return (v or {}).get("props") or {} if isinstance(v, dict) else {}


def _clean(v) -> str:
    return (_text(v) or "").replace("​", "").strip()


class NativeSync:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.since: int | None = None
        self.speak_target: str | None = None
        self.speak_checked = 0.0
        self.active_until = 0.0
        self.options: dict[int, dict] = {}   # p3fs -> {"text", "topic", "tone"} (latest tree)
        self.by_person: dict[str, dict] = {}  # person -> latest tree spoken with them (question, options, ...)
        self.extra: dict[int, str] = {}      # event id -> extra action text ("无可奉告", ...)
        self.topic: str | None = None
        self.pending: list[dict] = []
        self.audits: list[tuple[float, str, list]] = []  # recent (time, action, people): FM may log before or after
        self.squad: set[str] = set()
        self.current: dict | None = None     # the press question on screen right now (see _refresh_options)
        self.pinned_speak: str | None = None    # a dialog held in the background (press.py), read instead of the screen's
        self.pinned_session: str | None = None
        self.observers: list = []               # called with every event FM's interface sends
        self.closed: dict[tuple, float] = {}      # SpeakTo targets FM has just closed (left, walked out) -> when
        self.closed_at = 0.0
        self.match_seen = 0.0                   # last time the match screen talked to the engine (see match_quiet)

    @staticmethod
    def _key(target: str | None) -> tuple | None:
        pairs = target_pairs(target or "")
        return tuple(sorted(pairs.items())) if pairs else None

    def match_quiet(self) -> bool:
        """True during a match and for a few minutes after it, while FM settles the result: the
        engine then reads nothing and sends nothing (crashes happened right at the end of matches)."""
        return time.monotonic() - self.match_seen < MATCH_QUIET_SECONDS

    # --- polling -----------------------------------------------------------------
    def poll(self, career: Career) -> None:
        if self.since is None:  # start with what happens from now on
            seen = self.bridge.events(0)
            self.since = max((e["seq"] for e in seen), default=0)
            return
        events = self.bridge.events(self.since)
        for ev in events:  # first: a dialog FM has just closed must not be read again (that crashed the game)
            pairs = target_pairs(ev.get("target", ""))
            if ev.get("id") in LEAVE_EVENTS and pairs.get(" 011") == SPEAK_TO_TYPE or ev.get("id") == PRESS_ATTENDED:
                key = tuple(sorted(pairs.items())) if pairs.get(" 011") == SPEAK_TO_TYPE else self._key(self.speak_target)
                if key:
                    self.closed[key] = time.monotonic()
                self.current, self.closed_at = None, time.monotonic()
        self._refresh_options()
        for ev in events:
            self.since = max(self.since, ev["seq"])
            if target_pairs(ev.get("target", "")).get(" 011") in MATCH_TYPES:
                self.match_seen = time.monotonic()
            for observe in self.observers:
                try:
                    observe(career, ev)
                except Exception:
                    pass
            try:
                self._handle(career, ev)
            except Exception:  # an unexpected payload must never stop the sync
                continue
        now = time.monotonic()
        for p in [p for p in self.pending if now - p["at"] > PENDING_SECONDS]:
            self.pending.remove(p)
            try:
                self._write(career, p)
            except BridgeUnavailable:
                pass

    def _refresh_options(self) -> None:
        """Caches the dialog currently on screen (quickly while a dialog is in use)."""
        target = self.pinned_speak or self.bridge.last_target(SPEAK_TO_TYPE)
        if not target:
            return
        if time.monotonic() - self.closed.get(self._key(target), -1e9) < CLOSED_SECONDS:  # FM just closed it
            self.current = None
            return
        now = time.monotonic()
        # Around a match only a dialog the interface has just opened (e.g. the post-match press conference)
        # is read; an older one is left alone while FM settles the match.
        if self.match_quiet() and target == self.speak_target and now >= self.active_until:
            return
        if target != self.speak_target:
            self.active_until = now + ACTIVE_SECONDS
        elif now - self.speak_checked < (0.5 if now < self.active_until else 5):
            return
        self.speak_target, self.speak_checked = target, now
        raw = self.bridge.query_target(target, ["itca", "1VLL"])
        tree, raw_extra = _props(raw.get("itca")), raw.get("1VLL") or []
        options: dict[int, dict] = {}
        for group in tree.get("N2w4") or []:
            props = _props(group)
            head = _props(props.get("DwoR"))
            topic = _clean(head.get("lWh9"))
            tone = head.get("izOE") if isinstance(head.get("izOE"), int) else None
            if isinstance(head.get("p3fs"), int):
                options[head["p3fs"]] = {"text": topic, "topic": None, "tone": tone, "event": head.get("neve")}
            for child in props.get("T3E4") or []:
                c = _props(child)
                if isinstance(c.get("p3fs"), int):
                    options[c["p3fs"]] = {"text": _clean(c.get("lWh9")), "topic": topic, "tone": tone,
                                          "event": c.get("neve")}
        self.options.update(options)
        for item in raw_extra:
            p = _props(item)
            if isinstance(p.get("neve"), int) and _clean(p.get("lWh9")):
                self.extra[p["neve"]] = _clean(p.get("lWh9"))
        npc = _props(tree.get("nzfT"))
        person = (npc.get("h1J ") or {}).get("ref") if isinstance(npc.get("h1J "), dict) else None
        current = None
        if person:
            bubbles = [_props(b).get("nrtS") for b in npc.get("xj09") or []]
            question = " ".join(_clean(b) for b in bubbles if _clean(b))
            links = [l for b in bubbles if isinstance(b, dict) for l in b.get("links") or []]
            entry = {"question": question, "links": links, "options": options,
                     "body": _clean(_props(npc.get("gmyy")).get("Xr0u")), "at": now}
            old = self.by_person.get(person)
            if old and old.get("question") == question and not old.get("options"):
                entry["options"] = {}  # this question was already answered
            self.by_person[person] = entry
            if question and entry["options"] and person in self._press_journalists():
                current = {"person": person, "question": question, "links": links, "body": entry["body"],
                           "options": entry["options"], "target": target,
                           "extra": {e: t for e, t in self.extra.items()}}
        self.current = current

    def _press_journalists(self) -> set[str]:
        """Journalists in the press conference that is open (empty when none is)."""
        session = self.pinned_session or self.bridge.last_target(PRESS_TYPE)
        if not session:
            return set()
        out = set()
        for item in self.bridge.query_target(session, ["gM8V"]).get("gM8V") or []:
            ferd = target_pairs((item or {}).get("target") or "").get("srep") if isinstance(item, dict) else None
            if isinstance(ferd, int):
                out.add(f"{ferd & 0xffffffff:08x}")
        return out

    def _handle(self, career: Career, ev: dict) -> None:
        if target_pairs(ev.get("target", "")).get(" 011") == SPEAK_TO_TYPE:
            self.active_until = time.monotonic() + ACTIVE_SECONDS
            data = decode(ev.get("data") or "")
            if not isinstance(data, dict):
                return
            person = (data.get("h1J ") or {}).get("ref") if isinstance(data.get("h1J "), dict) else None
            if not person:
                return
            if ev["id"] in self.extra:
                return self._press_action(career, person, self.extra[ev["id"]])
            option_id = data.get("p3fs") if isinstance(data.get("p3fs"), int) else None
            asked = self.by_person.get(person)
            if asked and asked.get("question") and option_id in (asked.get("options") or {}):
                return self._press_answer(career, person, asked, option_id)
            option = self.options.get(option_id) if option_id is not None else None
            if option and option.get("tone") is not None:
                return  # a press-style answer whose question was never on screen (e.g. auto-answered on leaving)
            if option and option["topic"] is None:  # a topic was chosen; the sentence comes next
                self.topic = option["text"]
                return
            if option_id is None:
                return
            now = time.monotonic()
            earlier = next((a for t, a, people in reversed(self.audits) if now - t < 10 and person in people), None)
            self.pending.append({"person": person, "line": option["text"] if option else None,
                                 "topic": (option or {}).get("topic") or self.topic, "action": earlier,
                                 "at": 0 if earlier else now, "option_id": option_id})
            return
        data = decode(ev.get("data") or "")
        if not isinstance(data, dict):
            return
        action = data.get("eman")
        targets = [t.get("ref") for t in data.get("TSDv") or [] if isinstance(t, dict)]
        if isinstance(action, str) and targets:
            self.audits = [a for a in self.audits if time.monotonic() - a[0] < 30] + [(time.monotonic(), action, targets)]
            for p in self.pending:
                if p["person"] in targets and not p["action"]:
                    p["action"] = action
                    p["at"] = 0  # complete: write on this poll

    # --- press -------------------------------------------------------------------------
    def _journalist(self, career: Career, person: str) -> dict:
        """Name and media outlet; the journalist joins the roster the first time."""
        raw = self.bridge.query(person, ["eman", "SdeM", "dinu"])
        outlet_ref = (raw.get("SdeM") or {}).get("ref") if isinstance(raw.get("SdeM"), dict) else None
        info = {"id": person, "name": _clean(raw.get("eman")) or person,
                "outlet": self.bridge.name(outlet_ref) if outlet_ref else "",
                "uid": raw.get("dinu") if isinstance(raw.get("dinu"), int) else None}
        career.store.add_event("journalist", career.date, info, event_key=f"journalist:{person}")
        return info

    def _squad(self, career: Career) -> set[str]:
        key = (career.key, career.team_id, career.date)  # another save or club: read again
        if not self.squad or getattr(self, "_squad_key", None) != key:
            try:
                self.squad = set(self.bridge.team(career.team_id)["squad"])
                self._squad_key = key
            except BridgeUnavailable:
                pass
        return self.squad

    def _press_answer(self, career: Career, person: str, asked: dict, option_id: int) -> None:
        option = asked["options"][option_id]
        asked["options"] = {}  # answered: the same tree is never used twice
        store, j = career.store, self._journalist(career, person)
        question, answer, tone = asked["question"], option["text"], TONES.get(option.get("tone"), "")
        meta = {"source": "fm_press", "topic": "新闻发布会", "tone": tone}
        store.add_message(person, "person", question, career.date, meta | {"body_language": asked.get("body")})
        store.add_message(person, "manager", answer, career.date, meta)
        store.add_memory(person, f"在新闻发布会上问主教练：「{question}」；主教练{('（' + tone + '）') if tone else ''}回答："
                                 f"「{answer}」", career.date, "fm_press", 1.5, "press")
        effect = PRESS_TONE_EFFECTS.get(option.get("tone"), {})
        if effect:
            store.apply_relation(person, effect, career.date)
        for player in [l for l in asked.get("links") or [] if l in self._squad(career)]:
            store.add_memory(player, f"主教练在新闻发布会上被{j['outlet']}记者问到和他有关的问题：「{question}」；"
                                     f"主教练回答：「{answer}」", career.date, "fm_press", 1.2, "press")
        store.add_event("press_answer", career.date, {"journalist": j, "question": question, "answer": answer,
                                                      "tone": tone, "about": asked.get("links") or []})

    def _press_action(self, career: Career, person: str, action: str) -> None:
        store, j = career.store, self._journalist(career, person)
        asked = self.by_person.get(person) or {}
        question = asked.get("question") or ""
        store.add_message(person, "person", question or "（提问）", career.date, {"source": "fm_press", "topic": "新闻发布会"})
        store.add_message(person, "manager", f"（{action}）", career.date, {"source": "fm_press", "topic": "新闻发布会"})
        store.add_memory(person, f"在新闻发布会上问主教练「{question}」，主教练的反应是：{action}", career.date,
                         "fm_press", 2.0, "press")
        for key, effect in PRESS_ACTION_EFFECTS:
            if key in action:
                store.apply_relation(person, effect, career.date)
                break
        store.add_event("press_action", career.date, {"journalist": j, "question": question, "action": action})

    # --- player conversations ------------------------------------------------------------
    def _reaction(self) -> tuple[str, dict]:
        """FM's body language after the exchange, plus the raw session for later study."""
        session = self.bridge.last_target(CONVERSATION_TYPE)
        if not session:
            return "", {}
        codes = self.bridge.type_codes("ConversationEditSessionReference")
        raw = self.bridge.query_target(session, codes)
        body = ((raw.get("U0DX") or {}).get("props") or {}).get("Xr0u")
        return _text(body) or "", raw

    def _write(self, career: Career, p: dict) -> None:
        try:
            body, session = self._reaction()
        except BridgeUnavailable:
            body, session = "", {}
        store = career.store
        line = p["line"] or (f"（{p['topic']}）" if p["topic"] else "（FM 谈话）")
        meta = {"source": "fm", "topic": p["topic"], "action": p["action"], "body_language": body}
        store.add_message(p["person"], "manager", line, career.date, meta)
        store.add_message(p["person"], "person", f"（{body}）" if body else "（听完点了点头）", career.date,
                          {"source": "fm", "body_language": body})
        topic = f"（{p['topic']}）" if p["topic"] else ""
        memory = f"主教练当面找他谈话{topic}，说：「{line}」" + (f"；他的反应：{body}" if body else "")
        store.add_memory(p["person"], memory, career.date, "fm_conversation", 1.5, "fm_conversation")
        delta: dict = {}
        for key, effect in ACTION_EFFECTS:
            if p["action"] and key in p["action"]:
                delta = effect
                break
        if delta:
            store.apply_relation(p["person"], delta, career.date)
        store.add_event("fm_conversation", career.date,
                        {"person": p["person"], "line": p["line"], "topic": p["topic"], "action": p["action"],
                         "option_id": p["option_id"], "body_language": body, "session": session})
