"""Group chats the manager creates with players.

The manager names a group and picks its members. When he writes, the model
decides who answers (not everyone does: some reply at once, some only send
an emoji, some stay silent, as fits the topic and each person), each in his
own voice. What the manager says becomes a temporary memory for every
member, weighted by how much it mattered.

Groups are events ("group"); messages live in the messages table under the
thread id "group:<id>", with the author in their meta.
"""
from __future__ import annotations

import itertools
import time

from . import memory as mem
from . import persona
from . import snapshot, story
from .bridge import Bridge
from .career import Career
from .dialogue import _english_level
from .model import Provider
from .interactions import KIND_NAMES, TONES

HISTORY = 30

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["replies", "importance", "memory", "fm_talks"],
    "properties": {
        "replies": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "text"],
            "properties": {"name": {"type": "string", "description": "exact name of the member answering"},
                           "text": {"type": "string"}}}},
        "importance": {"type": "integer", "minimum": 0, "maximum": 3,
                       "description": "how much the manager's message matters to the members (0 small talk … 3 big)"},
        "memory": {"type": "string", "description": "one third-person sentence the members will remember, or empty"},
        "fm_talks": {"type": "array", "description": "members the manager's message really praises, criticises, "
                     "reassures, etc. as a football manager (at most 2), with the kind of interaction",
                     "items": {"type": "object", "additionalProperties": False, "required": ["name", "kind", "tone"],
                               "properties": {"name": {"type": "string"}, "kind": {"type": "string", "enum": KIND_NAMES},
                                              "tone": {"type": "string", "enum": TONES}}}},
    },
}


class Groups:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.ids = itertools.count(int(time.time()))

    def list(self, career: Career) -> list[dict]:
        out = []
        for e in career.store.events("group", limit=100):
            g = e["payload"] or {}
            last = career.store.messages(f"group:{g['id']}", limit=1)
            out.append(g | {"last": last[0]["text"] if last else ""})
        return list(reversed(out))

    def get(self, career: Career, gid: str) -> dict | None:
        e = career.store.event_by_key(f"group:{gid}")
        return e["payload"] if e else None

    def create(self, career: Career, name: str, members: list[str]) -> dict:
        members = [m for m in dict.fromkeys(members) if isinstance(m, str) and len(m) == 8][:20]
        if not members:
            raise ValueError("pick at least one member")
        gid = str(next(self.ids))
        names = {m: self.bridge.name(m) for m in members}
        g = {"id": gid, "name": (name or "、".join(list(names.values())[:3]))[:40], "members": members, "names": names,
             "created": career.date}
        career.store.add_event("group", career.date, g, event_key=f"group:{gid}")
        return g

    def _persona(self, career: Career, ref: str) -> str:
        snap = snapshot.get(self.bridge, career, ref)
        rel = career.store.relation(ref)
        recent = [m["text"] for m in mem.recall(career.store.memories(ref), career.date, n=4)]
        voice = persona.cues(snap)
        return (f"{persona.brief(snap, career.team_name)}; English {_english_level(snap.get('languages', ''))}; "
                f"voice: {voice}; "
                f"feelings toward the manager trust {rel['trust']:+d} liking {rel['liking']:+d}; "
                f"remembers: {' / '.join(recent) or 'nothing special'}")

    def send(self, career: Career, model: Provider, gid: str, text: str, language: str) -> list[dict]:
        g = self.get(career, gid)
        if not g:
            raise ValueError("no such group")
        store, thread = career.store, f"group:{gid}"
        history = store.messages(thread, limit=HISTORY)
        transcript = "\n".join(f"{career.manager_name if m['sender'] == 'manager' else (m.get('meta') or {}).get('name')}: "
                               f"{m['text']}" for m in history) or "(no messages yet)"
        personas = "\n".join(f"- {self._persona(career, m)}" for m in g["members"])
        out_lang = "English" if language == "en" else "Simplified Chinese"
        system = f"""You play the members of a group chat that {career.manager_name}, manager of {career.team_name}, \
created ("{g['name']}"). Today is {career.date} in Football Manager 27.
MEMBERS:
{personas}
RECENT AT THE CLUB:
{chr(10).join('- ' + x for x in story.recent_world(career))}
Decide who answers the manager's new message. Not everyone answers: usually one to three people, those it concerns \
or who like to talk; some only send an emoji or a sticker-like reply; the shy or the unhappy may stay silent. Each \
writes in his own voice: his age (teenagers use slang and emoji, veterans are dry and measured), where he is from, \
his English level, personality and media style, his standing (leaders speak first and for the group), the clubs \
he has played for (old clubs and teammates come up when relevant) and how he feels about the manager; players may \
also banter with each other. Write in {out_lang}, short like a real group chat. Only football facts from above; never \
invent results, injuries or transfers."""
        prompt = f"Group chat so far:\n{transcript}\n\nNew message from {career.manager_name}:\n{text}"
        result = model.complete(system, prompt, SCHEMA)
        store.add_message(thread, "manager", text, career.date)
        by_name = {n: ref for ref, n in g["names"].items()}
        replies = []
        for r in result.get("replies") or []:
            ref = by_name.get(str(r.get("name") or "").strip())
            body = str(r.get("text") or "").strip()[:500]
            if not ref or not body:
                continue
            store.add_message(thread, "person", body, career.date, {"author": ref, "name": g["names"][ref]})
            replies.append({"author": ref, "name": g["names"][ref], "text": body})
        importance = result.get("importance") if isinstance(result.get("importance"), int) else 1
        memory = str(result.get("memory") or "").strip()[:300] or (
            f"主教练在群「{g['name']}」里说：「{text[:120]}」" if importance >= 1 else "")
        if memory:
            for ref in g["members"]:
                store.add_memory(ref, memory, career.date, "group", mem.IMPORTANCE_WEIGHT.get(max(0, min(3, importance)), 1.0),
                                 "group")
        self.last_talks = [(by_name.get(str(t.get("name") or "").strip()), t.get("kind"), t.get("tone"))
                           for t in (result.get("fm_talks") or [])[:2]]
        return replies
