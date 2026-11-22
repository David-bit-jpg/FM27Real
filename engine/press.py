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

import struct
import threading
import time
from datetime import date as Date

from . import memory as mem
from . import story
from .bridge import Bridge, BridgeUnavailable, _text
from .career import Career
from .fmvalue import speak_choice, target_pairs
from .model import ModelError, Provider
from .native import NativeSync, TONES
from .stats import season
from .interactions import KIND_NAMES, TONES as TALK_TONES

PRESS_SESSION_TYPE = 81          # PressConferenceEditSessionReference
EV_PRESS_START = 863384693
EV_PRESS_ATTENDED = 876038760    # to the manager, with the invitation's news id (FM's interface sends it on leaving)
EV_NEWS_ACTIONED = 75            # to the invitation news item
INVITE_WORDS = ("新闻发布会即将开始", "press conference")
INVITE_DAYS = 2
END_IDLE_SECONDS = 10            # no new question this long after an answer: FM is done
ABANDON_SECONDS = 900
MAX_AUTO_ANSWERS = 12


def _i32(rec: str) -> int:
    v = int(rec, 16)
    return v - (1 << 32) if v >= 1 << 31 else v


def _days(a: str | None, b: str | None) -> int:
    try:
        return abs((Date.fromisoformat(b) - Date.fromisoformat(a)).days)
    except (TypeError, ValueError):
        return 999


def _null_choice(person: str, guid: int) -> str:
    """The SpeakTo record with no option chosen (p3fs null), as FM sends on start and leave."""
    return speak_choice(person, guid, 0).replace("703366730111" + "00", "70336673" + "0100")


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
                                                    "tone": {"type": "string", "enum": TALK_TONES}}}},
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
        self.bg: dict | None = None          # a press conference held in the background (see background_open)
        self.finished: dict | None = None    # the last background press conference, once over

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
            if cur and self.key in self.dismissed:  # answered with FM's own options; the manager may switch back
                return {"active": False, "dismissed": True, "journalist": self.journalist(cur["person"]),
                        "question": self.rewritten.get(self.key) or cur["question"]}
            if not cur:
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
            recent = [m["text"] for m in store.memories(ref)[-6:] if m.get("kind") in ("match", "news", "press", "transfer", "rumour")]
            numbers = season(self.bridge, ref)
            player_lines.append(f"{name}: " + " / ".join([numbers] + recent if numbers else recent) if (recent or numbers) else name)
        said = [f"[{e['game_date']}] {e['payload'].get('journalist', {}).get('outlet', '')}问「{e['payload'].get('question')}」"
                f"，主教练答「{e['payload'].get('answer')}」" for e in store.events("press_answer", limit=6)]
        with_me = [m["text"] for m in mem.recall(store.memories(cur["person"]), career.date, n=5)]
        here = [f"{x['outlet']}：「{x['question']}」→ 主教练：「{x['answer']}」" for x in self._asked_here(cur)]
        transfer = [f"[{e['game_date']}] {(e['payload'] or {}).get('title')}" + (f"（{e['payload'].get('outlet')}）" if (e['payload'] or {}).get('outlet') else "")
                    for e in store.events("news", limit=200)
                    if (e["payload"] or {}).get("origin") in ("rumour", "transfer") and (e["payload"] or {}).get("about_us", True)][-6:]
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

TRANSFER TALK ABOUT THE CLUB (FM's own rumours and interest; usable as facts):
{_lines(transfer)}

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
- Home or away, the stadium and the opponent of the next match come only from the original question or the \
context ("at <our stadium>" means a home match); never guess them.
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
                                          "answer": " ".join(t["text"] for t in thread if t["who"] == "manager")[:300],
                                          "tone": TONES.get(cur["options"][option].get("tone"), "")})
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
        if ok and self.bg:
            self.bg["answered"] += 1
            self.bg["last_question"] = time.monotonic()
            self.bg["dirty"] = True
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

    # --- background press conferences (FM's own, without opening FM's screen) -----------------
    # Recorded from FM's interface (docs/PROTOCOL.md, "Press conference"): the session target
    # (type 81: Guid, sQvO = the invitation news id, YPbP = the manager) gives the SpeakTo tree in
    # I69g; FM acts on it while it is held open. Start = event 863384693, answers = event 943147586
    # (speak_choice), leave = event 31, then FM's interface marks the invitation as attended
    # (event 876038760 to the manager with the news id, event 75 to the news item).
    def invitation(self, career: Career) -> dict | None:
        """The press conference FM has invited the manager to and that is still to be held."""
        done = {(e["payload"] or {}).get("news") for e in career.store.events("press_bg", limit=50)
                if (e["payload"] or {}).get("status") == "done"}
        best = None
        for item in self.bridge.query(career.manager_id, ["SWEN"]).get("SWEN") or []:
            target = (item or {}).get("target") if isinstance(item, dict) else None
            pairs = target_pairs(target or "")
            news_id = pairs.get("dinu")
            if news_id is None or news_id in done:
                continue
            v = self.bridge.query_target(target, ["ltit", "AMqC"])
            title = (_text(v.get("ltit")) or "").replace("​", "")
            if not any(w in title for w in INVITE_WORDS):
                continue
            date = v.get("AMqC") if isinstance(v.get("AMqC"), str) else career.date
            if _days(date, career.date) > INVITE_DAYS:
                continue
            if not best or date > best["date"]:
                best = {"news": news_id, "title": title, "date": date, "target": target}
        return best

    def background_open(self, career: Career) -> dict:
        with self.lock:
            if self.bg:
                return {"ok": True, "already": True}
        inv = self.invitation(career)
        if not inv:
            return {"ok": False, "error": "没有待开的新闻发布会"}
        b = self.bridge
        guid = int(b.status().get("guid") or "0", 16)
        pairs = [(" 011", PRESS_SESSION_TYPE), ("diuG", guid), ("sQvO", inv["news"]), ("YPbP", _i32(career.manager_id))]
        session = (bytes([len(pairs)]) + b"".join(c.encode() + struct.pack("<i", v) for c, v in pairs)).hex()
        holds = []
        try:
            h1, values = b.hold_target(session, ["I69g", "gM8V"])
            if h1:
                holds.append(h1)
            speak = (values.get("I69g") or {}).get("target")
            if not speak:
                raise RuntimeError("FM did not open the press conference")
            h2, values2 = b.hold_target(speak, ["itca", "1VLL"])
            if h2:
                holds.append(h2)
            first = self._first_journalist(values, values2)
            if not first:
                raise RuntimeError("no journalists")
            if not b.send_event(EV_PRESS_START, speak, _null_choice(first, guid)):
                raise RuntimeError("FM did not start the press conference")
        except Exception as err:
            for h in holds:
                b.release(h)
            return {"ok": False, "error": str(err)}
        with self.lock:
            self.bg = {"session": session, "speak": speak, "holds": holds, "guid": guid, "invitation": inv,
                       "journalist": first, "started": time.monotonic(), "last_question": time.monotonic(),
                       "answered": 0}
            self.native.pinned_speak, self.native.pinned_session = speak, session
            self.native.speak_checked = 0.0
            self.finished = None
        career.store.add_event("press_bg", career.date, {"news": inv["news"], "title": inv["title"], "status": "open"},
                               event_key=f"press_bg:{inv['news']}")
        self._save_live(career)
        return {"ok": True}

    @staticmethod
    def _first_journalist(session_values: dict, speak_values: dict) -> str | None:
        npc = ((((speak_values.get("itca") or {}).get("props") or {}).get("nzfT") or {}).get("props") or {})
        h = npc.get("h1J ")
        if isinstance(h, dict) and h.get("ref"):
            return h["ref"]
        for item in session_values.get("gM8V") or []:
            ferd = target_pairs((item or {}).get("target") or "").get("srep") if isinstance(item, dict) else None
            if isinstance(ferd, int):
                return f"{ferd & 0xffffffff:08x}"
        return None

    def _save_live(self, career: Career) -> None:
        """Keeps the open conference (its held targets) across an engine restart."""
        import json
        bg = self.bg
        career.store.set_meta("press_bg_live", json.dumps({k: v for k, v in bg.items()
                                                          if k not in ("started", "last_question")}) if bg else "")

    def _restore_live(self, career: Career) -> None:
        import json
        raw = career.store.get_meta("press_bg_live")
        if self.bg or not raw:
            return
        try:
            bg = json.loads(raw)
        except ValueError:
            return
        # Press conferences are no longer started from the phone: a conference left over from an
        # earlier version is let go (its held targets released, nothing answered), so FM's own
        # invitation can be attended normally.
        for h in bg.get("holds") or []:
            try:
                self.bridge.release(h)
            except Exception:
                pass
        career.store.set_meta("press_bg_live", "")
        if (bg.get("invitation") or {}).get("news"):
            career.store.update_event(f"press_bg:{bg['invitation']['news']}", {"news": bg["invitation"]["news"],
                                                                              "status": "released"})

    def background_tick(self, career: Career) -> None:
        """Called often by the engine: ends a background press conference once FM has no more questions."""
        with self.lock:
            self._restore_live(career)
            bg = self.bg
            if not bg:
                return
            now = time.monotonic()
            if self.native.current:
                bg["last_question"] = now
                return
            idle = now - bg["last_question"]
        if (bg["answered"] and idle > END_IDLE_SECONDS) or idle > ABANDON_SECONDS:
            self.background_close(career)

    def background_close(self, career: Career, early: bool = False) -> dict:
        with self.lock:
            self._restore_live(career)
            bg = self.bg
            if not bg:
                return {"ok": True}
            self.bg = None
        career.store.set_meta("press_bg_live", "")
        b, guid, speak, inv = self.bridge, bg["guid"], bg["speak"], bg["invitation"]
        left = 0
        try:
            if early:  # as FM's interface does when the manager walks out: the rest is answered neutrally
                for _ in range(MAX_AUTO_ANSWERS):
                    self.native.speak_checked = 0.0
                    self.native._refresh_options()
                    cur = self.native.current
                    if not cur:
                        break
                    option = next((pid for pid, o in cur["options"].items() if o.get("tone") == 2), next(iter(cur["options"])))
                    self._submit(cur, option)
                    left += 1
                    time.sleep(1.2)
            b.send_event(31, speak, _null_choice(bg["journalist"], guid))
            manager = (bytes([3]) + b" 011" + struct.pack("<i", 0x1c) + b"diuG" + struct.pack("<i", guid)
                       + b"ferd" + struct.pack("<i", _i32(career.manager_id))).hex()
            b.send_event(EV_PRESS_ATTENDED, manager, (b"\x01\x01" + struct.pack("<i", inv["news"])).hex())
            b.send_event(EV_NEWS_ACTIONED, inv["target"], "0100")
        finally:
            for h in bg["holds"]:
                b.release(h)
            with self.lock:
                self.native.pinned_speak = self.native.pinned_session = None
                self.native.current = None
        target, items = self.conference
        self.finished = {"title": inv["title"], "answers": list(items), "auto": left}
        career.store.update_event(f"press_bg:{inv['news']}", {"news": inv["news"], "title": inv["title"],
                                                              "status": "done", "answers": len(items), "auto": left})
        career.store.add_event("press_conference", career.date, {"title": inv["title"], "answers": list(items),
                                                                 "walked_out": early},
                               event_key=f"press_conference:{inv['news']}")
        return {"ok": True, "auto_answered": left}

    def background_state(self, career: Career) -> dict:
        with self.lock:
            self._restore_live(career)
            bg = self.bg
            if bg and bg.pop("dirty", None):
                self._save_live(career)
        if bg:
            return {"background": True, "title": bg["invitation"]["title"], "answered": bg["answered"]}
        if self.finished:
            return {"background": False, "finished": self.finished}
        return {"background": False}

    def leave_early(self, career: Career) -> dict:
        """The manager leaves FM's press conference early: as FM does, the remaining questions get
        neutral answers (FM's own options), then FM's screen shows the end of the conference."""
        if self.bg:
            return self.background_close(career, early=True)
        answered = 0
        for _ in range(MAX_AUTO_ANSWERS):
            self.native.speak_checked = 0.0
            self.native._refresh_options()
            cur = self.native.current
            if not cur:
                break
            option = next((pid for pid, o in cur["options"].items() if o.get("tone") == 2), next(iter(cur["options"])))
            if not self._submit(cur, option):
                return {"ok": False, "error": "FM 没有接受回答，请用原版界面离场", "answered": answered}
            answered += 1
            time.sleep(1.5)
        with self.lock:
            self.dismissed.clear()
        career.store.add_event("press_conference", career.date, {"title": "新闻发布会", "answers": list(self.conference[1]),
                                                                 "walked_out": True, "auto": answered})
        return {"ok": True, "auto_answered": answered}
