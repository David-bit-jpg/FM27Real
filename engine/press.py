"""Typed press conferences on top of FM's own.

FM runs the press conference (journalists, their questions, the answer
options and what each answer does to morale and media relations). While a
question is on screen, the manager answers by typing instead of clicking:

1. The question is rewritten from the world (recent results, ratings, news,
   what was said before) so it is sharp and specific, while keeping FM's
   subject (FM's answer options belong to it).
2. The manager types; the journalist may press with up to MAX_FOLLOW_UPS
   follow-ups.
3. The whole answer is matched to the closest of FM's options, which is
   submitted to FM exactly as a click would be, so FM settles everything.
4. The exchange, in the manager's own words, goes into the journalist's
   thread and memory, into the memory of our players it was about, and
   into a "press_answer" event for the news feed.

If anything fails, the question is handed back to FM's own options.
"""
from __future__ import annotations

import threading
import time

from . import memory as mem
from . import story
from .bridge import Bridge, BridgeUnavailable, _text
from .career import Career
from .fmvalue import speak_choice
from .model import ModelError, Provider
from .native import NativeSync, TONES
from .stats import season
from .interactions import KIND_NAMES, TONES

MAX_FOLLOW_UPS = 1   # at most one follow-up per question, and only when the answer dodges

QUESTION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["question"],
    "properties": {"question": {"type": "string", "description": "the journalist's question, as they would ask it"}},
}

ANSWER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["follow_up", "reply", "body_language", "option", "memory", "headline", "player_effects"],
    "properties": {
        "follow_up": {"type": "boolean", "description": "true to press the manager with one more question"},
        "reply": {"type": "string", "description": "the follow-up question, or a short closing remark / note-taking"},
        "body_language": {"type": "string"},
        "option": {"type": "integer", "description": "id of the FM answer option closest in tone and meaning"},
        "memory": {"type": "string", "description": "one sentence, third person, what the journalist will remember"},
        "headline": {"type": "string", "description": "a headline the journalist might write, or empty"},
        "player_effects": {"type": "array", "description": "our players the manager publicly praised or criticised "
                           "in his answer (at most 2), with the kind of interaction it amounts to",
                           "items": {"type": "object", "additionalProperties": False, "required": ["name", "kind", "tone"],
                                     "properties": {"name": {"type": "string"},
                                                    "kind": {"type": "string", "enum": KIND_NAMES},
                                                    "tone": {"type": "string", "enum": TONES}}}},
    },
}


def _lines(items) -> str:
    return "\n".join(f"- {x}" for x in items if x) or "- (nothing)"


class PressRoom:
    def __init__(self, bridge: Bridge, native: NativeSync):
        self.bridge = bridge
        self.native = native
        self.lock = threading.Lock()
        self.key: tuple | None = None        # (journalist, FM question) being handled
        self.rewritten: dict[tuple, str] = {}
        self.thread: list[dict] = []         # this question's exchange: {"who": journalist|manager, "text"}
        self.body = ""
        self.dismissed: set[tuple] = set()   # questions handed back to FM's options
        self.busy = False
        self.error = ""
        self.journalists: dict[str, dict] = {}
        self.conference: tuple[str, list[dict]] = ("", [])  # (dialog target, this conference's Q&As)
        self.positions: tuple[str, list[str]] = ("", [])    # (game date, "name: position" for the first team)

    # --- state -------------------------------------------------------------------
    def _sync(self) -> dict | None:
        cur = self.native.current
        key = (cur["person"], cur["question"]) if cur else None
        if key != self.key:
            self.key, self.thread, self.error, self.body = key, [], "", (cur or {}).get("body", "")
        return cur

    def active(self) -> bool:
        with self.lock:
            cur = self._sync()
            return bool(cur) and self.key not in self.dismissed

    def status(self) -> dict:
        """For the in-game overlay: a question is on screen; it was handed to FM's options."""
        with self.lock:
            cur = self._sync()
            return {"active": bool(cur) and self.key not in self.dismissed,
                    "dismissed": bool(cur) and self.key in self.dismissed}

    def resume(self) -> None:
        with self.lock:
            if self.key:
                self.dismissed.discard(self.key)

    def _asked_here(self, cur: dict) -> list[dict]:
        target, items = self.conference
        if target != cur.get("target"):
            self.conference = (cur.get("target") or "", [])
            return []
        return items

    def journalist(self, person: str) -> dict:
        if person not in self.journalists:
            raw = self.bridge.query(person, ["eman", "SdeM", "dinu"])
            outlet = (raw.get("SdeM") or {}).get("ref") if isinstance(raw.get("SdeM"), dict) else None
            self.journalists[person] = {"id": person, "name": (_text(raw.get("eman")) or person).replace("​", ""),
                                        "outlet": self.bridge.name(outlet) if outlet else "",
                                        "uid": raw.get("dinu") if isinstance(raw.get("dinu"), int) else None}
        return self.journalists[person]

    def state(self, career: Career, model: Provider | None) -> dict:
        with self.lock:
            cur = self._sync()
            if not cur or self.key in self.dismissed:
                return {"active": False}
            key = self.key
            question = self.rewritten.get(key)
            thread = list(self.thread)
        j = self.journalist(cur["person"])
        if question is None and model is not None and not self.busy:
            threading.Thread(target=self._rewrite, args=(career, model, cur, j, key), daemon=True).start()
        return {"active": True, "journalist": j, "question": question or cur["question"], "ready": question is not None,
                "original": cur["question"], "body_language": self.body or cur.get("body", ""),
                "thread": thread, "busy": self.busy, "error": self.error,
                "follow_ups_left": MAX_FOLLOW_UPS - sum(1 for t in thread if t["who"] == "journalist")}

    def dismiss(self) -> None:
        with self.lock:
            if self.key:
                self.dismissed.add(self.key)

    def _squad_positions(self, career: Career) -> list[str]:
        """Every first-team player's position (FM's own wording), read once per game day."""
        if self.positions[0] != career.date:
            lines = []
            try:
                for ref in self.bridge.team(career.team_id)["squad"]:
                    raw = self.bridge.query(ref, ["eman", "SspP"])
                    name, pos = (_text(raw.get("eman")) or "").replace("\u200b", ""), _text(raw.get("SspP")) or ""
                    if name:
                        lines.append(f"{name}: {pos}" if pos else name)
            except BridgeUnavailable:
                return self.positions[1]
            self.positions = (career.date, lines)
        return self.positions[1]

    # --- model steps -------------------------------------------------------------------
    def _context(self, career: Career, cur: dict, j: dict) -> str:
        store = career.store
        about = [l for l in cur.get("links") or [] if l.startswith("01")]
        player_lines = []
        for ref in about[:3]:
            name = self.bridge.name(ref)
            recent = [m["text"] for m in store.memories(ref)[-4:] if m.get("kind") in ("match", "news", "press")]
            numbers = season(self.bridge, ref)
            player_lines.append(f"{name}: " + " / ".join([numbers] + recent if numbers else recent) if (recent or numbers) else name)
        said = [f"[{e['game_date']}] {e['payload'].get('journalist', {}).get('outlet', '')}问「{e['payload'].get('question')}」"
                f"，主教练答「{e['payload'].get('answer')}」" for e in store.events("press_answer", limit=6)]
        with_me = [m["text"] for m in mem.recall(store.memories(cur["person"]), career.date, n=5)]
        here = [f"{x['outlet']}：「{x['question']}」→ 主教练：「{x['answer']}」" for x in self._asked_here(cur)]
        return f"""CLUB AND RECENT EVENTS:
{_lines(story.recent_world(career))}

PEOPLE THE QUESTION IS ABOUT:
{_lines(player_lines)}

FIRST-TEAM POSITIONS (judge every player by his position):
{_lines(self._squad_positions(career))}

WHAT THE MANAGER SAID IN RECENT PRESS CONFERENCES:
{_lines(said)}

WHAT THIS JOURNALIST REMEMBERS ABOUT THE MANAGER:
{_lines(with_me)}

ALREADY ASKED AND ANSWERED IN THIS PRESS CONFERENCE (do not ask these again):
{_lines(here)}"""

    def _rewrite(self, career: Career, model: Provider, cur: dict, j: dict, key: tuple) -> str | None:
        with self.lock:
            if self.busy:
                return None
            self.busy = True
        try:
            system = f"""You are {j['name']}, a football journalist for {j['outlet'] or 'a media outlet'}, at \
{career.manager_name}'s press conference ({career.team_name}). Today is {career.date}.
Rewrite the question below so it is pointed and specific, the way a real reporter asks.
STRICT RULES ON FACTS:
- Every fact you mention (a result, score, rating, run of form, injury, transfer, quote) must appear word for \
word in the context or in the original question. If it is not there, do not mention it.
- Never put words in the manager's mouth: quote him only from "WHAT THE MANAGER SAID IN RECENT PRESS \
CONFERENCES". If that section is empty, do not claim he said anything.
- Do not count streaks or compute records yourself; only repeat ones stated in the context.
- If the context offers nothing relevant, keep close to the original question and only sharpen the wording.
- Judge players by their position: do not blame defensive midfielders, defenders or goalkeepers for not \
scoring; for them talk about defending, passing, positioning or their rating. Goals matter mainly for forwards \
and attacking midfielders.
- Do not repeat a question or angle already asked in this press conference; if the original question covers \
the same ground, find a different angle within its subject.
Keep the same subject as the original question. One or two sentences, in Simplified Chinese. A tabloid is \
blunter than a broadsheet or broadcaster, but equally bound by the facts."""
            prompt = f"{self._context(career, cur, j)}\n\nORIGINAL QUESTION:\n{cur['question']}"
            q = str(model.complete(system, prompt, QUESTION_SCHEMA).get("question") or "").strip()[:400]
        except ModelError as e:
            with self.lock:
                self.error = str(e)
            return None
        finally:
            with self.lock:
                self.busy = False
        with self.lock:
            self.rewritten[key] = q or cur["question"]
        return self.rewritten[key]

    def answer(self, career: Career, model: Provider, text: str) -> dict:
        with self.lock:
            cur = self._sync()
            if not cur or self.key in self.dismissed:
                return {"done": True, "error": "no question on screen"}
            if self.busy:
                return {"error": "busy"}
            self.busy = True
            key, question = self.key, self.rewritten.get(self.key) or cur["question"]
            self.thread.append({"who": "manager", "text": text})
            thread = list(self.thread)
        try:
            j = self.journalist(cur["person"])
            asked = sum(1 for t in thread if t["who"] == "journalist")
            options = "\n".join(f"{pid}: [{TONES.get(o.get('tone'), '')}] {o['text']}"
                                for pid, o in sorted(cur["options"].items()))
            exchange = "\n".join(f"{'Journalist' if t['who'] == 'journalist' else 'Manager'}: {t['text']}" for t in thread)
            system = f"""You are {j['name']}, a football journalist for {j['outlet'] or 'a media outlet'}, \
questioning {career.manager_name} ({career.team_name}) at a press conference. Today is {career.date}.
- Follow up only if the manager clearly dodged the question or contradicted the facts; a clear answer, even a \
short or unfriendly one, ends the question. Never press on a point already answered in this press conference. Judge \
players by their position (do not expect defensive players to score). \
({MAX_FOLLOW_UPS - asked} follow-up(s) left; with none left, follow_up must be false.)
- reply: your follow-up question, or, when done, a short remark or a note of what you are writing (Simplified \
Chinese, spoken, short). Only facts stated in the context, the question or the manager's own words; never \
invent facts, records, streaks or quotes.
- body_language: how you look while reacting, a short phrase in Simplified Chinese without a subject.
- option: the id of FM's answer option whose tone and meaning are closest to everything the manager said.
- memory: one sentence in Simplified Chinese, third person, what you will remember about the manager's answer.
- headline: if the answer is newsworthy, the headline you would write (Simplified Chinese), else empty."""
            prompt = (f"{self._context(career, cur, j)}\n\nQUESTION:\n{question}\n\nEXCHANGE SO FAR:\n{exchange}"
                      f"\n\nFM'S ANSWER OPTIONS (choose one id):\n{options}")
            r = model.complete(system, prompt, ANSWER_SCHEMA)
        except ModelError as e:
            with self.lock:
                self.thread.pop()
                self.error = str(e)
                self.busy = False
            return {"error": str(e)}
        reply = str(r.get("reply") or "").strip()[:400]
        follow = bool(r.get("follow_up")) and asked < MAX_FOLLOW_UPS
        with self.lock:
            if self.key != key:  # FM moved on meanwhile (e.g. an option was clicked)
                self.busy = False
                return {"done": True}
            if reply:
                self.thread.append({"who": "journalist", "text": reply})
            self.body = str(r.get("body_language") or "")[:60] or self.body
            self.busy = False
            thread = list(self.thread)
        if follow:
            return {"done": False, "reply": reply, "body_language": self.body}
        option = r.get("option") if r.get("option") in cur["options"] else None
        if option is None:  # fall back to the neutral option, else any
            option = next((pid for pid, o in cur["options"].items() if o.get("tone") == 2), next(iter(cur["options"])))
        with self.lock:
            self._asked_here(cur).append({"outlet": j["outlet"] or j["name"], "question": question,
                                          "answer": " ".join(t["text"] for t in thread if t["who"] == "manager")[:300]})
        self.last_effects = [(t.get("name"), t.get("kind"), t.get("tone"), " ".join(x["text"] for x in thread
                              if x["who"] == "manager")) for t in (r.get("player_effects") or [])[:2]]
        sent = self._submit(cur, option)
        self._remember(career, cur, j, question, thread, cur["options"][option], r, sent)
        with self.lock:
            if not sent:
                self.dismissed.add(key)
                self.error = "没能提交给 FM，请用原版选项回答这个问题"
        return {"done": True, "reply": reply, "submitted": sent, "option": cur["options"][option]["text"]}

    # --- FM and memory -------------------------------------------------------------------
    def _submit(self, cur: dict, option: int) -> bool:
        event = cur["options"][option].get("event")
        if not isinstance(event, int):
            return False
        try:
            guid = int(self.bridge.status().get("guid") or "0", 16)
            ok = self.bridge.send_event(event, cur["target"], speak_choice(cur["person"], guid, option))
        except (BridgeUnavailable, ValueError):
            return False
        if ok:  # the native sync must not record this question again
            asked = self.native.by_person.get(cur["person"])
            if asked:
                asked["options"] = {}
        return ok

    def _remember(self, career: Career, cur: dict, j: dict, question: str, thread: list[dict], option: dict,
                  r: dict, sent: bool) -> None:
        store = career.store
        meta = {"source": "press", "topic": "新闻发布会", "outlet": j["outlet"]}
        store.add_event("journalist", career.date, j, event_key=f"journalist:{j['id']}")
        store.add_message(j["id"], "person", question, career.date, meta)
        for t in thread:
            store.add_message(j["id"], "manager" if t["who"] == "manager" else "person", t["text"], career.date, meta)
        words = " ".join(t["text"] for t in thread if t["who"] == "manager")
        store.add_memory(j["id"], str(r.get("memory") or f"在发布会上问主教练「{question}」，主教练说「{words}」")[:300],
                         career.date, "press", 1.8, "press")
        tone = option.get("tone")
        store.apply_relation(j["id"], {1: {"liking": 1}, 4: {"liking": -1, "respect": -1}}.get(tone, {}), career.date)
        squad = self.native._squad(career)
        for player in [l for l in cur.get("links") or [] if l in squad]:
            store.add_memory(player, f"主教练在新闻发布会上被{j['outlet']}记者问到和他有关的问题：「{question}」；"
                                     f"主教练说：「{words[:200]}」", career.date, "press", 1.5, "press")
        store.add_event("press_answer", career.date, {
            "journalist": j, "question": question, "original": cur["question"], "answer": words,
            "thread": thread, "fm_option": option["text"], "tone": TONES.get(tone, ""), "submitted": sent,
            "headline": str(r.get("headline") or "")[:120], "about": cur.get("links") or []})
