"""Who a person is, and how that shapes the way he talks, posts and reacts.

Built from FM's own facts only (age, nationality, birthplace, languages,
personality, media handling, squad standing, where he was trained, the clubs
and loans in his career story, honours and caps). No model call: the rules
here turn those facts into voice notes the prompts pass to the model, so a
teenager from the academy, a veteran who has played for five clubs, and an
outspoken foreign star never sound alike.

  lines(snap)  -> background + voice notes, for one-to-one prompts
  brief(snap)  -> one compact line, for prompts with many people
"""
from __future__ import annotations

import re

HOME = {"英格兰", "威尔士", "苏格兰", "北爱尔兰", "England", "Wales", "Scotland", "Northern Ireland"}
_ZW = re.compile("[​‌‍]")

PERSONALITY = [  # FM personality word (substring) -> how it sounds
    ("职业典范", "a model professional: precise, self-disciplined, never careless with words"),
    ("相当专业", "very professional: measured, focused on work and standards"),
    ("完美主义", "a perfectionist: self-critical, picks at details, never satisfied"),
    ("自我鞭策", "driven: talks about improving, pushes himself and others"),
    ("雄心", "ambitious: talks about trophies, the next level, his future; can sound impatient"),
    ("尽忠", "loyal and devoted to the club: speaks of 'we', the badge and the fans"),
    ("兢兢业业", "hard-working and humble: few frills, credits teammates"),
    ("坚决", "determined: resilient, competitive, does not dwell on setbacks"),
    ("意志", "strong-willed: stubborn, firm opinions"),
    ("精神领袖", "a spirited leader: loud, rallying, emotional"),
    ("随遇而安", "easy-going: relaxed, jokes, not too bothered"),
    ("懒散", "casual: relaxed about work, deflects with humour"),
    ("脾气", "temperamental: quick to take offence, emotional swings"),
    ("不安", "unsettled: restless, hints at frustration"),
    ("唯利是图", "mercenary: thinks about money and contracts first"),
    ("忠诚", "loyal: values trust and long relationships"),
    ("平衡", "balanced: ordinary and even-tempered; his age, background and mood colour him more than temperament"),
]
MEDIA = [  # FM media-handling word (substring) -> how he speaks in public
    ("亲善媒体", "media-friendly: polite, open, happy to chat"),
    ("冷静稳健", "calm and level-headed: careful, sensible, never dramatic"),
    ("坦率直言", "outspoken: says what he thinks, can make headlines"),
    ("擅长心理战", "likes mind games: needles opponents, provocative quips"),
    ("闪烁其词", "evasive: vague answers, avoids committing to anything"),
    ("不苟言笑", "reserved: few words, serious, no jokes"),
    ("易怒", "volatile: can flare up when pushed"),
    ("对抗", "confrontational: bristles at criticism"),
]


def clean(text) -> str:
    return _ZW.sub("", str(text or "")).strip()


def _match(table, value) -> list[str]:
    v = clean(value)
    return [desc for key, desc in table if key in v]


def career_facts(snap: dict) -> dict:
    """Clubs he played for, loans, honours and caps, from FM's career story and its club links."""
    text = clean(snap.get("history") or snap.get("profile") or "")
    team = snap.get("team")
    clubs, loans, faced = [], [], []
    m = re.search(r"租借到(.+?)期间", text)
    loan_part = m.group(1) if m else ""
    for c in snap.get("career_clubs") or []:
        name = clean(c.get("name"))
        if not name or c.get("id") == team:
            continue
        at = text.find(name)
        before = text[max(0, at - 8):at] if at >= 0 else ""
        core = re.sub(r"(FC|AFC|足球俱乐部)$", "", name).strip()
        if (core and core in loan_part) or re.search(r"租借到", before):
            loans.append(name)
        elif at >= 0 and re.search(r"(与|对阵|对|击败|战胜)$", before):
            faced.append(name)
        else:
            clubs.append(name)
    honours = []
    m = re.search(r"个人荣誉有[:：]\s*(.+?)(?:\n|$)", text)
    if m:
        honours = [h.strip(" .。") + ")" for h in m.group(1).split(")") if "(" in h][:4]
    caps = None
    nat = clean(snap.get("nationality"))
    m = re.search(r"代表" + re.escape(nat) + r".{0,24}?(?:至今)?出场(\d+)次", text) if nat else None
    if m:
        caps = (nat, int(m.group(1)))
    elif nat and "国脚" in clean(snap.get("bio")):
        caps = (nat, None)
    return {"clubs": clubs, "loans": loans, "faced": faced, "honours": honours, "caps": caps}


def _age_voice(age) -> str | None:
    if not isinstance(age, int):
        return None
    if age <= 19:
        return ("a teenager: still finding his voice, a bit nervous and deferential with the manager, uses young "
                "people's slang and emoji, gaming and social media, talks about family and the academy")
    if age <= 22:
        return "young and hungry: wants minutes and a pathway, eager to impress, casual with teammates, some emoji"
    if age <= 29:
        return "in his prime: confident, cares about his status, trophies and his next contract"
    if age <= 32:
        return ("experienced: calm, speaks with authority, has seen dressing rooms come and go, thinks about his "
                "family and his legacy")
    return ("a veteran near the end of his career: reflective, mentors the youngsters, wry humour about his age, "
            "thinks about life after football")


def voice(snap: dict, club_name: str | None = None) -> list[str]:
    """How his background shapes the way he talks (no model call)."""
    out = []
    age = _age_voice(snap.get("age"))
    if age:
        out.append(f"Age {snap.get('age')}: {age}")
    nat = clean(snap.get("nationality"))
    origin = snap.get("origin") or {}
    where = origin.get("birth_city") or origin.get("birth_region") or nat
    nats = {n.strip() for n in clean(snap.get("nationalities") or nat).split(",") if n.strip()} | {nat}
    born_home = any(h in clean(origin.get("birth_region")) for h in HOME)
    if nat and not (nats & HOME) and not born_home:
        out.append(f"Foreign player from {where} ({nat}): may mention home, family back there, adapting to England "
                   f"(food, weather, the language), his national team")
    elif where:
        out.append(f"From {where}: local references, his hometown and the clubs he grew up around"
                   + (f"; represents {nat} internationally" if nat not in HOME else ""))
    if origin.get("favoured_clubs"):
        out.append(f"Grew up supporting / has a soft spot for: {', '.join(origin['favoured_clubs'][:3])}")
    for d in _match(PERSONALITY, snap.get("personality"))[:2]:
        out.append(f"Personality — {d}")
    for d in _match(MEDIA, snap.get("media_handling"))[:2]:
        out.append(f"In public — {d}")
    trained = clean(snap.get("trained_at"))
    club = clean(club_name or snap.get("team_name"))
    if club and club in trained:
        out.append(f"Academy graduate of {club}: emotionally tied to the club, knows the staff since he was a kid")
    elif trained and "并非" not in trained:
        out.append(f"Came through the youth system at {re.sub('(在|青训培养)', '', trained)}")
    facts = career_facts(snap)
    former = facts["clubs"]
    if former:
        out.append(f"Former clubs: {', '.join(former[:5])} — he may compare with how things were there, mention old "
                   f"teammates or fans, and feel something when facing them")
    if facts["loans"]:
        out.append(f"Was loaned to {', '.join(facts['loans'][:3])}: knows what it is to fight for a place")
    if facts["honours"]:
        out.append(f"Honours: {'; '.join(facts['honours'])}")
    if facts["caps"]:
        nat_, n = facts["caps"]
        out.append(f"International for {nat_}" + (f" ({n} caps)" if n else "") + ": proud of it, international breaks matter to him")
    status = clean((snap.get("social") or {}).get("influence") or snap.get("squad_status"))
    if "领袖" in status:
        out.append("A dressing-room leader: speaks up first, speaks for the group, others listen")
    elif "极具影响力" in status:
        out.append("Very influential in the dressing room: his opinion carries weight")
    elif status in ("其他", "") and isinstance(snap.get("age"), int) and snap["age"] <= 23:
        out.append("Not a big voice in the dressing room yet: follows the senior players")
    return out


def lines(snap: dict, club_name: str | None = None) -> list[str]:
    """Background facts plus voice notes, for one-to-one prompts."""
    out = voice(snap, club_name)
    story = clean(snap.get("history") or snap.get("profile") or snap.get("bio"))
    if story:
        out.append("Career story (FM): " + re.sub(r"\s+", " ", story)[:600])
    return out


def brief(snap: dict, club_name: str | None = None) -> str:
    """One compact line: age, origin, personality, standing, former clubs."""
    origin = snap.get("origin") or {}
    facts = career_facts(snap)
    club = clean(club_name or snap.get("team_name"))
    former = facts["clubs"][:3]
    bits = [
        f"{snap.get('age')}y" if snap.get("age") else None,
        clean(snap.get("position")) or None,
        f"from {origin.get('birth_city') or clean(snap.get('nationality'))}" if (origin.get("birth_city") or snap.get("nationality")) else None,
        clean(snap.get("personality")) or None,
        clean(snap.get("media_handling")) or None,
        clean((snap.get("social") or {}).get("influence") or snap.get("squad_status")) or None,
        "academy graduate" if club and club in clean(snap.get("trained_at")) else None,
        f"ex-{'/'.join(former)}" if former else None,
        f"{facts['caps'][0]} international" if facts["caps"] else None,
    ]
    return f"{snap.get('name')}: " + ", ".join(b for b in bits if b)


def cues(snap: dict) -> str:
    """The voice in a few words: age, temperament and public manner (for prompts with many people)."""
    parts = []
    age = _age_voice(snap.get("age"))
    if age:
        parts.append(age.split(":")[0])
    parts += [d.split(":")[0] for d in _match(PERSONALITY, snap.get("personality"))[:1] if not d.startswith("balanced")]
    parts += [d.split(":")[0] for d in _match(MEDIA, snap.get("media_handling"))[:2]]
    return ", ".join(parts)
