"""Private conversations between the manager and one person.

The engine assembles who the person is (FM facts, social situation, origin,
established life details, memories) and the conversation so far; the model
writes the reply plus structured intents; the engine checks and clamps those
intents before anything is stored. Football facts only ever come from the
engine; the model may add small life details, which become canon.
"""
from __future__ import annotations

from . import memory as mem
from .career import Career
from .model import ModelError, Provider

HISTORY_TURNS = 24          # messages of context sent to the model
MAX_DELTA = 3               # per message, per relationship dimension
MAX_REPLY_CHARS = 600

REPLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reply", "body_language", "mood", "relation_delta", "memory", "new_canon", "end"],
    "properties": {
        "reply": {"type": "string", "description": "what the character says back"},
        "body_language": {"type": "string",
                          "description": "the visible non-verbal reaction while answering, a short phrase"},
        "mood": {"type": "string", "enum": ["happy", "neutral", "worried", "annoyed", "angry", "sad", "excited"]},
        "relation_delta": {
            "type": "object",
            "additionalProperties": False,
            "required": ["trust", "liking", "respect"],
            "properties": {k: {"type": "integer", "minimum": -MAX_DELTA, "maximum": MAX_DELTA}
                           for k in ("trust", "liking", "respect")},
        },
        "memory": {"type": "string", "description": "one third-person sentence worth remembering, or empty"},
        "new_canon": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False, "required": ["key", "value"],
                      "properties": {"key": {"type": "string"}, "value": {"type": "string"}}},
        },
        "end": {"type": "boolean", "description": "true if the character wants to end the conversation"},
    },
}

# Snapshot fields that are FM facts: the model may never redefine them as canon.
FACT_KEYS = {"name", "age", "birthday", "birth_date", "nationality", "position", "team", "club", "contract",
             "height", "brother", "sister", "family", "birthplace", "hometown", "language", "languages"}

LANGUAGE_NAMES = {"zh": "Simplified Chinese", "en": "English"}


def _lines(items) -> str:
    return "\n".join(f"- {i}" for i in items if i)


def _english_level(languages: str) -> str:
    """'英语(流利), 荷兰语(流利)' -> 'fluent'; basic/none otherwise."""
    for part in (languages or "").split(","):
        if "英语" in part or "English" in part:
            if "流利" in part or "Fluent" in part:
                return "fluent"
            if "基础" in part or "Basic" in part:
                return "basic"
            return "competent"
    return "none"


def build_system(snap: dict, career: Career, canon: list[dict], memories: list[dict], relation: dict,
                 language: str) -> str:
    social = snap.get("social") or {}
    origin = snap.get("origin") or {}
    friends = [f"{f['name']} ({f['relation']})" for f in social.get("favoured") or []]
    english = _english_level(snap.get("languages", ""))

    facts = _lines([
        f"Name: {snap.get('name')}",
        f"Age: {snap.get('age')}, born {snap.get('birth_date')}" if snap.get("age") else None,
        f"Club: {snap.get('team_name') or career.team_name}, joined {snap.get('joined')}" if snap.get("joined") else None,
        f"Position: {snap.get('position')}" if snap.get("position") else None,
        f"Nationality: {snap.get('nationalities') or snap.get('nationality')}",
        f"Languages: {snap.get('languages')}",
        f"Height: {snap.get('height_cm')} cm" if snap.get("height_cm") else None,
        f"Standing in the squad: {social.get('influence') or snap.get('squad_status')}",
        f"Career stage: {snap.get('career_stage')}" if snap.get("career_stage") else None,
        f"Fitness: {snap.get('fitness')}" if snap.get("fitness") else None,
        f"Average rating this season: {snap.get('average_rating'):.2f}" if isinstance(snap.get("average_rating"), float) else None,
        f"Contract: {snap.get('contract_status')}" if snap.get("contract_status") else None,
        f"Playing time: {snap.get('playing_time_feeling')}" if snap.get("playing_time_feeling") else None,
        f"Trained at: {snap.get('trained_at')}" if snap.get("trained_at") else None,
    ])
    feelings = ", ".join(f"{k} {relation.get(k, 0):+d} ({mem.relation_words(relation.get(k, 0))})"
                         for k in ("trust", "liking", "respect"))
    situation = _lines([
        f"Close to: {', '.join(friends)}" if friends else "No particularly close people at the club",
        f"View of the manager (FM): {social.get('view_of_manager')}" if social.get("view_of_manager") else None,
        f"Your own feelings toward the manager from your dealings so far (scale -100..100): {feelings}",
        *[f"Feeling: {h}" for h in social.get("happiness") or []],
    ])
    background = _lines([
        f"Born in {origin.get('birth_city')}, {origin.get('birth_region')}" if origin.get("birth_city") else
        (f"From {origin.get('birth_region')}" if origin.get("birth_region") else None),
        f"Favoured clubs: {', '.join(origin.get('favoured_clubs'))}" if origin.get("favoured_clubs") else None,
        f"Career story: {snap.get('profile') or snap.get('bio')}" if (snap.get("profile") or snap.get("bio")) else None,
    ])
    canon_lines = _lines(f"{c['key']}: {c['value']}" for c in canon) or "- (none yet)"
    memory_lines = _lines(f"[{m['game_date']}] {m['text']}" for m in memories) or "- (none yet)"
    out_lang = LANGUAGE_NAMES.get(language, "Simplified Chinese")

    return f"""You are {snap.get('name')}, a real person in the world of Football Manager 27. You are texting \
privately with {career.manager_name}, the manager of {career.team_name}, face to face at the club. Today is \
{career.date} in the game.

Stay fully in character. You are not an AI and you never mention games, prompts or models.

FACTS (true in this world; never contradict them):
{facts}

YOUR SITUATION AT THE CLUB:
{situation}

BACKGROUND:
{background}

LIFE DETAILS YOU HAVE ALREADY MENTIONED (canon; stay consistent):
{canon_lines}

WHAT YOU REMEMBER (conversations, matches, training, things you heard; strongest first in your mind):
{memory_lines}

HOW TO WRITE:
- Write what you say in {out_lang}: spoken, short and natural, usually one to three sentences.
- body_language: what the manager can see while you answer, as a short phrase in {out_lang} without a subject \
(for example a small smile, avoiding eye contact, arms folded, leaning forward). It must match how you really \
feel, including when your words are more polite than your feelings. Use an empty string if nothing stands out.
- Your English level is "{english}". The club speaks English with you; if your English is basic, write like \
someone with limited English: short, simple sentences, sometimes searching for words. Still write in {out_lang}.
- Your personality ("{snap.get('personality') or 'unknown'}") and how you handle the media \
("{snap.get('media_handling') or 'unknown'}") shape your tone. Your background gives you topics, never a stereotype.
- Only state football facts that appear above (results, contracts, transfers, injuries, selection, other \
people's words). If the manager asks about something you do not know, say so or react naturally; do not invent it.
- You may share small everyday life details (family moments, hobbies, the city, training anecdotes) that fit the \
facts. If you introduce a new lasting detail (a pet's name, a hobby, a habit), add it to new_canon.
- Let your feelings and memories shape your tone: warmer when they are positive, guarded or cold when wary or \
hostile; bring up things you remember when they are relevant, without reciting them.
- relation_delta: how the manager's latest message changed your trust, liking and respect for them, each from \
-{MAX_DELTA} to {MAX_DELTA}; usually 0 or ±1, larger only for something significant.
- memory: one sentence in third person about anything worth remembering from this exchange (a promise, a \
conflict, personal news), or an empty string.
- end: true only if you want to stop talking now."""


def build_prompt(history: list[dict], manager_name: str, person_name: str, new_text: str) -> str:
    lines = []
    for m in history[-HISTORY_TURNS:]:
        who = manager_name if m["sender"] == "manager" else person_name
        lines.append(f"[{m.get('game_date') or ''}] {who}: {m['text']}")
    transcript = "\n".join(lines) or "(no earlier messages)"
    return f"""Conversation so far:
{transcript}

New message from {manager_name}:
{new_text}

Reply as {person_name}."""


def validate(result: dict) -> dict:
    """Clamp and clean the model's intents; the reply text is required."""
    reply = str(result.get("reply") or "").strip()
    if not reply:
        raise ModelError("empty reply")
    delta = result.get("relation_delta") or {}
    clean_delta = {}
    for k in ("trust", "liking", "respect"):
        try:
            v = int(delta.get(k, 0))
        except (TypeError, ValueError):
            v = 0
        clean_delta[k] = max(-MAX_DELTA, min(MAX_DELTA, v))
    canon = []
    for c in result.get("new_canon") or []:
        key, value = str(c.get("key", "")).strip(), str(c.get("value", "")).strip()
        if key and value and key.lower() not in FACT_KEYS and len(key) <= 40 and len(value) <= 200:
            canon.append({"key": key, "value": value})
    return {
        "reply": reply[:MAX_REPLY_CHARS],
        "body_language": str(result.get("body_language") or "").strip()[:60],
        "mood": result.get("mood") if result.get("mood") in REPLY_SCHEMA["properties"]["mood"]["enum"] else "neutral",
        "relation_delta": clean_delta,
        "memory": str(result.get("memory") or "").strip()[:300],
        "new_canon": canon[:3],
        "end": bool(result.get("end")),
    }


def converse(model: Provider, career: Career, snap: dict, text: str, language: str) -> dict:
    """Stores the manager's message, asks the model, stores and returns the checked reply.
    The reply's memory, relationship change and new life details are kept."""
    store = career.store
    person_id = snap["id"]
    history = store.messages(person_id, limit=HISTORY_TURNS)
    canon = store.canon(person_id)
    memories = mem.recall(store.memories(person_id), career.date)
    relation = store.relation(person_id)
    system = build_system(snap, career, canon, memories, relation, language)
    prompt = build_prompt(history, career.manager_name, snap.get("name", ""), text)
    # Ask first: if the model fails, nothing is stored and the manager can resend.
    result = validate(model.complete(system, prompt, REPLY_SCHEMA))
    meta = {k: result[k] for k in ("body_language", "mood", "relation_delta", "memory", "new_canon", "end")}
    store.add_message(person_id, "manager", text, career.date)
    message_id = store.add_message(person_id, "person", result["reply"], career.date, meta)
    if result["memory"]:
        strong = max(abs(v) for v in result["relation_delta"].values()) >= 2 or result["mood"] in ("angry", "excited", "sad")
        store.add_memory(person_id, result["memory"], career.date, "conversation", 2.0 if strong else 1.0, "conversation")
    new_relation = store.apply_relation(person_id, result["relation_delta"], career.date)
    for c in result["new_canon"]:
        store.add_canon(person_id, c["key"], c["value"], career.date, "conversation")
    return {"id": message_id, **result, "relation": new_relation}
