"""What the manager says anywhere becomes a real FM conversation, run in the background.

When a phone chat, a group chat or a press answer amounts to one of FM's own
player interactions (praise, criticism, reassurance, patience about playing
time, ...), it is queued here. After the game advances, the conversation is
held in the background exactly as FM's own conversation screen would hold it
(see docs/ENGINE_MAP.md, "Background conversations"): the session is built for
the manager, the player and the topic; the option closest to the manager's tone
(and words) is chosen; if the player pushes back, the conversation continues in
the same tone; then the chat ends. FM settles morale, happiness and the
relationship itself. The player's replies and body language are written back
into world memory and into his thread, so both sides remember it.

No model call is made here; the kind of interaction comes with the reply that
produced it. Cooldowns keep it from becoming a morale pump.
"""
from __future__ import annotations

import struct
import time
from datetime import date as Date

from .bridge import Bridge, BridgeUnavailable, _text
from .career import Career
from .fmvalue import speak_choice

# kind -> (FM topic code, description for the model)
KINDS = {
    "praise_form": (1025, "praise his recent form"),
    "praise_last_match": (1026, "praise his last match"),
    "praise_training": (1007, "praise his training"),
    "criticise_training": (1001, "criticise his training"),
    "criticise_form": (1002, "criticise his recent form"),
    "criticise_last_match": (1003, "criticise his last match"),
    "encourage_youngster": (1006, "encourage a young player"),
    "reassure_injury": (1057, "reassure him about an injury"),
    "ask_patience_playing_time": (1061, "ask for patience about playing time"),
    "ask_dressing_room_advice": (1039, "ask his advice about the dressing room"),
    "praise_loan": (1032, "praise his loan spell"),
    "criticise_loan": (1036, "criticise his loan spell"),
}
KIND_ZH = {"praise_form": "表扬近期状态", "praise_last_match": "表扬上一场的表现", "praise_training": "表扬训练",
           "criticise_training": "批评训练", "criticise_form": "批评近期状态", "criticise_last_match": "批评上一场的表现",
           "encourage_youngster": "鼓励年轻球员", "reassure_injury": "安慰伤病", "ask_patience_playing_time": "谈出场时间",
           "ask_dressing_room_advice": "征求更衣室建议", "praise_loan": "表扬租借表现", "criticise_loan": "批评租借表现"}
KIND_NAMES = ["none", *KINDS]
TONES = ["warm", "firm"]
PREFER = {  # option group heads FM uses, in order of preference by tone
    "warm": ("积极", "表扬上次表现", "训练水平", "鼓励", "说服", "祝贺", "寻求建议", "承诺", "妥协", "退让"),
    "firm": ("坚持", "批评", "否定", "威胁", "训练水平", "说服"),
}
NULL = -8388609              # FM's null record (0xff7fffff)
MAX_STEPS = 4                # choices per conversation
MAX_PER_TICK = 2
PERSON_COOLDOWN_DAYS = 3
KIND_COOLDOWN_DAYS = 14

SCHEMA_FIELD = {  # added to reply schemas
    "type": "object", "additionalProperties": False, "required": ["kind", "tone"],
    "properties": {
        "kind": {"type": "string", "enum": KIND_NAMES,
                 "description": "if the manager's message amounts to one of these real football-manager interactions "
                                "with the player, which one: " + "; ".join(f"{k} = {v[1]}" for k, v in KINDS.items())
                                + "; otherwise none"},
        "tone": {"type": "string", "enum": TONES, "description": "the manager's tone: warm or firm"},
    },
}


def _i32(rec: str) -> int:
    v = int(rec, 16)
    return v - (1 << 32) if v >= 1 << 31 else v


def _days(a: str | None, b: str | None) -> int:
    try:
        return abs((Date.fromisoformat(b) - Date.fromisoformat(a)).days)
    except (TypeError, ValueError):
        return 999


def _clean(v) -> str:
    return (_text(v) or "").replace("​", "").strip()


class FMTalks:
    def __init__(self, bridge: Bridge, roster=None):
        self.bridge = bridge
        self.roster = roster

    # --- queueing ------------------------------------------------------------------
    def queue(self, career: Career, person: str, kind: str, tone: str, words: str, source: str) -> bool:
        if kind not in KINDS or not person:
            return False
        if self.roster and person not in {p["id"] for p in self.roster.at_club(career)}:
            return False  # only players at the club
        store = career.store
        done = [e for e in store.events("fm_talk", limit=300) if (e["payload"] or {}).get("person") == person]
        if any(_days(e["game_date"], career.date) < PERSON_COOLDOWN_DAYS for e in done):
            return False
        if any((e["payload"] or {}).get("kind") == kind and _days(e["game_date"], career.date) < KIND_COOLDOWN_DAYS
               for e in done):
            return False
        pending = [e for e in store.events("fm_talk_plan", limit=200) if (e["payload"] or {}).get("status") == "pending"
                   and (e["payload"] or {}).get("person") == person]
        if pending:
            return False
        return store.add_event("fm_talk_plan", career.date, {
            "person": person, "kind": kind, "tone": tone if tone in TONES else "warm", "words": words[:300],
            "source": source, "status": "pending"}, event_key=f"talk:{person}:{kind}:{career.clock or career.date}")

    def pending(self, career: Career) -> list[dict]:
        return [e for e in career.store.events("fm_talk_plan", limit=200) if (e["payload"] or {}).get("status") == "pending"]

    # --- running -------------------------------------------------------------------
    def run(self, career: Career, busy: bool = False) -> int:
        if busy:
            return 0
        n = 0
        for e in self.pending(career)[:MAX_PER_TICK]:
            plan = e["payload"]
            try:
                result = self._talk(career, plan)
            except BridgeUnavailable:
                return n
            except Exception as err:  # never let one conversation stop the world
                result = {"status": "failed", "error": str(err)}
            career.store.update_event(e["event_key"], plan | result)
            n += result.get("status") == "done"
        return n

    def _session(self, career: Career, person: str, topic: int, guid: int) -> str:
        pairs = [(" 011", 23), ("edv2", NULL), ("rCj4", 1), ("diuG", guid), ("Eh I", -1),
                 ("YPbP", _i32(career.manager_id)), ("gxgT", topic), ("lCZi", _i32(person)), ("hIQv", NULL)]
        return (bytes([len(pairs)]) + b"".join(c.encode() + struct.pack("<i", v) for c, v in pairs)).hex()

    def _read(self, speak: str) -> tuple[list[str], str, list[dict]]:
        tree = (self.bridge.query_target(speak, ["itca"]).get("itca") or {}).get("props") or {}
        npc = (tree.get("nzfT") or {}).get("props") or {}
        says = [_clean((x.get("props") or {}).get("nrtS")) for x in npc.get("xj09") or []]
        body = _clean(((npc.get("gmyy") or {}).get("props") or {}).get("Xr0u"))
        groups = []
        for gr in tree.get("N2w4") or []:
            p = (gr or {}).get("props") or {}
            head = (p.get("DwoR") or {}).get("props") or {}
            children = [{"text": _clean((c.get("props") or {}).get("lWh9")), "id": (c.get("props") or {}).get("p3fs"),
                         "event": (c.get("props") or {}).get("neve")} for c in p.get("T3E4") or []]
            groups.append({"head": _clean(head.get("lWh9")), "id": head.get("p3fs"), "event": head.get("neve"),
                           "children": [c for c in children if isinstance(c["id"], int)]})
        return [s for s in says if s], body, groups

    @staticmethod
    def _pick(groups: list[dict], tone: str, words: str) -> dict | None:
        usable = [g for g in groups if g["head"] != "结束聊天" and (g["children"] or isinstance(g["id"], int))]
        if not usable:
            return None
        order = PREFER.get(tone, PREFER["warm"])
        usable.sort(key=lambda g: order.index(g["head"]) if g["head"] in order else len(order))
        g = usable[0]
        if not g["children"]:
            return {"text": g["head"], "id": g["id"], "event": g["event"]}
        chars = set(words)
        return max(g["children"], key=lambda c: len(chars & set(c["text"])))  # closest to the manager's words

    def _talk(self, career: Career, plan: dict) -> dict:
        b = self.bridge
        person, topic = plan["person"], KINDS[plan["kind"]][0]
        guid = int(b.status().get("guid") or "0", 16)
        session = self._session(career, person, topic, guid)
        hold_session, values = b.hold_target(session, ["I69g"])
        speak = (values.get("I69g") or {}).get("target")
        holds = [h for h in (hold_session,) if h]
        exchange = []
        try:
            if not speak:
                return {"status": "failed", "error": "no conversation"}
            h2, _ = b.hold_target(speak, ["itca"])  # watched, as FM's conversation screen watches it
            if h2:
                holds.append(h2)
            says, body, groups = self._read(speak)
            opening = says
            for _ in range(MAX_STEPS):
                option = self._pick(groups, plan["tone"], plan.get("words") or "")
                if not option:
                    break
                if not b.send_event(option["event"] if isinstance(option["event"], int) else 34, speak,
                                    speak_choice(person, guid, option["id"])):
                    break
                time.sleep(2.0)
                says, body, groups = self._read(speak)
                exchange.append({"manager": option["text"], "player": says, "body": body})
            end = next((g for g in groups if g["head"] == "结束聊天"), None)
            if end:
                d = speak_choice(person, guid, 0).replace("703366730111" + "00", "70336673" + "0100")
                b.send_event(end["event"] if isinstance(end["event"], int) else 31, speak, d)
        finally:
            for h in holds:
                b.release(h)
        if not exchange:
            return {"status": "failed", "error": "FM offered nothing to say (cooldown or not applicable)"}
        self._remember(career, plan, opening, exchange)
        return {"status": "done", "exchange": exchange}

    def _remember(self, career: Career, plan: dict, opening: list[str], exchange: list[dict]) -> None:
        store, person = career.store, plan["person"]
        meta = {"source": "fm_background", "topic": KIND_ZH.get(plan["kind"], plan["kind"])}
        for s in opening:
            store.add_message(person, "person", s, career.date, meta)
        for x in exchange:
            store.add_message(person, "manager", x["manager"], career.date, meta)
            for s in x["player"]:
                store.add_message(person, "person", s, career.date, meta | {"body_language": x["body"]})
        last = exchange[-1]
        store.add_memory(person, f"主教练当面找他谈话（{KIND_ZH.get(plan['kind'], plan['kind'])}）：「{exchange[0]['manager']}」；"
                                 f"他最后说「{' '.join(last['player'])[:120]}」，{last['body'] or '神情如常'}",
                         career.date, "fm_conversation", 1.8, "fm_conversation")
        store.add_event("fm_talk", career.date, {"person": person, "kind": plan["kind"], "source": plan.get("source"),
                                                 "exchange": exchange, "body": last["body"]})

    def log(self, career: Career, limit: int = 30) -> list[dict]:
        out = [{"date": e["game_date"], **(e["payload"] or {})} for e in career.store.events("fm_talk", limit=limit)]
        out.reverse()
        return out
