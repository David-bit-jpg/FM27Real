"""Native FM interactions -> world memory.

FM's own conversation screen keeps its options and settles morale itself.
This module watches what happens there and writes it into the world, the
same way phone chats are written:

- while a conversation is open, the topic/option tree (SpeakToReference)
  is cached, so each option's sentence is known;
- choosing an option sends event 34 to the conversation (with the option's
  id, p3fs, and the person); FM then logs an audit event naming the action
  (e.g. PraisePlayerPerformance);
- the exchange becomes a pair of messages in that person's thread (marked as
  an FM conversation), a memory, a relationship change and a world event,
  together with the body language FM shows afterwards.

Every step is best effort: anything unreadable is skipped, never raised.
"""
from __future__ import annotations

import time

from .bridge import Bridge, BridgeUnavailable, _text
from .career import Career
from .fmvalue import decode, target_pairs

SPEAK_TO_TYPE = 102        # SpeakToReference: topics and options
CONVERSATION_TYPE = 23     # ConversationEditSessionReference: the open conversation
EVENT_CHOOSE = 34          # a topic or option was chosen
PENDING_SECONDS = 8        # wait this long for FM's audit event before writing

# Relationship change by the kind of FM action (matched on the audit name).
ACTION_EFFECTS = (
    ("Praise", {"liking": 1, "respect": 1}),
    ("Advice", {"respect": 1}),
    ("Warn", {"liking": -1}),
    ("Discipline", {"liking": -2, "trust": -1}),
    ("Concern", {"trust": 1}),
    ("Promise", {"trust": 1}),
)


class NativeSync:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.since: int | None = None
        self.speak_target: str | None = None
        self.speak_checked = 0.0
        self.options: dict[int, dict] = {}   # p3fs -> {"text", "topic"}
        self.topic: str | None = None
        self.pending: list[dict] = []

    # --- polling -----------------------------------------------------------------
    def poll(self, career: Career) -> None:
        if self.since is None:  # start with what happens from now on
            seen = self.bridge.events(0)
            self.since = max((e["seq"] for e in seen), default=0)
            return
        self._refresh_options()
        for ev in self.bridge.events(self.since):
            self.since = max(self.since, ev["seq"])
            try:
                self._handle(ev)
            except Exception:  # an unexpected payload must never stop the sync
                continue
        now = time.monotonic()
        for p in [p for p in self.pending if now - p["at"] > PENDING_SECONDS]:
            self._write(career, p)
            self.pending.remove(p)

    def _refresh_options(self) -> None:
        """Caches the option sentences of the conversation currently on screen."""
        target = self.bridge.last_target(SPEAK_TO_TYPE)
        if not target:
            return
        if target == self.speak_target and time.monotonic() - self.speak_checked < 2:
            return
        self.speak_target, self.speak_checked = target, time.monotonic()
        tree = self.bridge.query_target(target, ["itca"]).get("itca")
        for group in ((tree or {}).get("props") or {}).get("N2w4") or []:
            props = (group or {}).get("props") or {}
            head = ((props.get("DwoR") or {}).get("props")) or {}
            topic = _text(head.get("lWh9"))
            if isinstance(head.get("p3fs"), int):
                self.options[head["p3fs"]] = {"text": topic, "topic": None}
            for child in props.get("T3E4") or []:
                c = (child or {}).get("props") or {}
                if isinstance(c.get("p3fs"), int):
                    self.options[c["p3fs"]] = {"text": _text(c.get("lWh9")), "topic": topic}

    def _handle(self, ev: dict) -> None:
        data = decode(ev.get("data") or "")
        if not isinstance(data, dict):
            return
        if ev["id"] == EVENT_CHOOSE and target_pairs(ev.get("target", "")).get(" 011") == SPEAK_TO_TYPE:
            person = (data.get("h1J ") or {}).get("ref")
            option = self.options.get(data.get("p3fs")) if isinstance(data.get("p3fs"), int) else None
            if not person:
                return
            if option and option["topic"] is None:  # a topic was chosen; the sentence comes next
                self.topic = option["text"]
                return
            self.pending.append({"person": person, "line": option["text"] if option else None,
                                 "topic": (option or {}).get("topic") or self.topic, "action": None,
                                 "at": time.monotonic(), "option_id": data.get("p3fs")})
            return
        action = data.get("eman")
        targets = [t.get("ref") for t in data.get("TSDv") or [] if isinstance(t, dict)]
        if isinstance(action, str) and targets:
            for p in self.pending:
                if p["person"] in targets and not p["action"]:
                    p["action"] = action
                    p["at"] = 0  # complete: write on this poll

    # --- writing -----------------------------------------------------------------------
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
