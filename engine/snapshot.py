"""Person snapshots: everything the engine knows about someone from FM.

Facts only (from the bridge): profile, social state, origin and portrait.
Cached per game day in the career database, so a person is read from the
game at most once a day unless refreshed explicitly.
"""
from __future__ import annotations

from .bridge import GRAPHICS, Bridge
from .career import Career


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
    team_id = snap.get("team")
    if isinstance(team_id, str):
        snap["team_name"] = bridge.name(team_id)
    uid = snap.get("uid")
    snap["has_face"] = isinstance(uid, int) and (GRAPHICS / f"faces/face_{uid}.png").exists()
    for local_path in ("face", "icon"):  # served by the engine as /face/<uid>; no file paths to the UI
        snap.pop(local_path, None)
    return snap


def get(bridge: Bridge, career: Career, person_id: str, refresh: bool = False) -> dict:
    cached, date = career.store.get_snapshot(person_id)
    if cached and date == career.date and not refresh:
        return cached
    snap = build(bridge, person_id)
    career.store.put_snapshot(person_id, snap, career.date)
    return snap
