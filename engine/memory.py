"""What a person recalls: weighted memories that fade with game time.

A memory's strength is its weight halved every HALF_LIFE_DAYS of game time,
scaled by weight (heavy memories fade more slowly): a stunning goal or a
broken promise is remembered for months, small talk for days.
"""
from __future__ import annotations

from datetime import date

HALF_LIFE_DAYS = 14.0
RECALL = 12  # memories given to the model per conversation
FORGET_BELOW = 0.05  # memories weaker than this are forgotten for good (pruned once a game day)
IMPORTANCE_WEIGHT = {0: 0.4, 1: 1.0, 2: 2.0, 3: 3.0}  # small talk fades in weeks, life-changing talk in months
PROMISE_WEIGHT = 4.0  # a promise lasts about half a season


def _days_between(a: str | None, b: str | None) -> float:
    try:
        return abs((date.fromisoformat(b) - date.fromisoformat(a)).days)
    except (TypeError, ValueError):
        return 0.0


def strength(memory: dict, today: str) -> float:
    weight = float(memory.get("weight") or 1.0)
    half_life = HALF_LIFE_DAYS * max(1.0, weight)
    return weight * 0.5 ** (_days_between(memory.get("game_date"), today) / half_life)


def recall(memories: list[dict], today: str, n: int = RECALL, floor: float = 0.15) -> list[dict]:
    """The n strongest memories still above the floor, in date order."""
    scored = [(strength(m, today), i, m) for i, m in enumerate(memories)]
    kept = sorted((s for s in scored if s[0] >= floor), reverse=True)[:n]
    return [m for _, _, m in sorted(kept, key=lambda s: (s[2].get("game_date") or "", s[1]))]


def relation_words(value: int) -> str:
    if value >= 40:
        return "strong"
    if value >= 15:
        return "positive"
    if value > -15:
        return "neutral"
    if value > -40:
        return "wary"
    return "hostile"


def forget(store, today: str) -> int:
    """Deletes memories that have faded below FORGET_BELOW. Returns how many were forgotten."""
    gone = []
    for row in store.all_memories():
        if strength(row, today) < FORGET_BELOW:
            gone.append(row["id"])
    store.delete_memories(gone)
    return len(gone)
