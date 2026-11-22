"""Person snapshots: everything the engine knows about someone from FM.

Facts only (from the bridge): profile, social state, origin and portrait.
Cached per game day in the career database, so a person is read from the
game at most once a day unless refreshed explicitly.
"""
from __future__ import annotations

from .bridge import GRAPHICS, Bridge
from .career import Career


def media(career: Career, person_id: str) -> dict:
    """A media outlet's contact (see contacts.media_contact): not an FM record, nothing to read from the game."""
    e = career.store.event_by_key(f"journalist:{person_id}")
    p = (e or {}).get("payload") or {}
    return {"id": person_id, "name": p.get("name") or "记者", "role": "journalist", "outlet": p.get("outlet") or "",
            "uid": None, "has_face": False, "media": True}


def build(bridge: Bridge, person_id: str) -> dict:
    snap = bridge.person(person_id)
    snap["social"] = bridge.person_social(person_id)
    snap["origin"] = bridge.person_origin(person_id)
    try:  # journalists: their media outlet
        media = bridge.query(person_id, ["SdeM"]).get("SdeM")
        if isinstance(media, dict) and media.get("ref"):
            snap["role"], snap["outlet"] = "journalist", bridge.name(media["ref"])
    except Exception:
        pass
    if not snap.get("role"):
        try:
            from .stats import season
            snap["season"] = season(bridge, person_id)
        except Exception:
            pass
    try:  # clubs named in his career story (played for, loaned to, or faced), resolved once a day
        raw = bridge.query(person_id, ["dkwi"]).get("dkwi") or {}
        links = list(dict.fromkeys(l for l in (raw.get("links") or []) if isinstance(l, str) and l.startswith("18")))
        snap["career_clubs"] = [{"id": l, "name": bridge.name(l)} for l in links[:12]]
    except Exception:
        pass
    team_id = snap.get("team")
    if isinstance(team_id, str):
        snap["team_name"] = bridge.name(team_id)
    uid = snap.get("uid")
    snap["has_face"] = isinstance(uid, int) and (GRAPHICS / f"faces/face_{uid}.png").exists()
    for local_path in ("face", "icon"):  # served by the engine as /face/<uid>; no file paths to the UI
        snap.pop(local_path, None)
    return snap


def get(bridge: Bridge, career: Career, person_id: str, refresh: bool = False) -> dict:
    if person_id.startswith("ee"):
        return media(career, person_id)
    cached, date = career.store.get_snapshot(person_id)
    if cached and date == career.date and not refresh:
        return cached
    snap = build(bridge, person_id)
    career.store.put_snapshot(person_id, snap, career.date)
    return snap
