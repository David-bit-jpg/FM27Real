"""Real changes to FM's game data, made through FM's own In-Game Editor.

The world decides, by plain rules, what should change in FM because of what
happened in it; the changes are small, capped and cooled down. They are
queued as "edit_plan" events and carried out when the game advances: the
editor is opened on the person, the tabs are clicked to the field, the value
is written exactly as the editor writes it, and the editor is closed (see
docs/PROTOCOL.md). Every change is logged as an "edit_done" event with the
value before and after and the reason. A setting switches it all off.

Rules (all fields are under 属性 → 隐藏属性):
- the manager's reputation drops after a social media row that became news;
- a player the manager has treated very well for a long while grows a little
  more professional; one treated very badly loses some temperament.

Nothing here runs without the In-Game Editor; any failure closes the editor
and leaves the game as it was.
"""
from __future__ import annotations

import struct
import time
from datetime import date as Date

from .bridge import Bridge, BridgeUnavailable
from .career import Career

MAX_PER_TICK = 1            # editor sessions per game-time step (each is visible for a moment)
COOLDOWN_DAYS = 14          # per person and field
SEASON_CAP = 3              # most a hidden attribute moves through our rules per season
TABS = {"person": [["职员属性", "球员属性"], ["隐藏属性"]]}
REPUTATION = ("当前声望", "国内声望", "国际声望")
LIMITS = {**{f: (1, 10000) for f in REPUTATION}}  # hidden attributes: 1..20


def _days(a: str | None, b: str | None) -> int:
    try:
        return abs((Date.fromisoformat(b) - Date.fromisoformat(a)).days)
    except (TypeError, ValueError):
        return 999


def encode(field: str, value: int) -> str:
    """As the editor sends it: reputation int16, attributes int8."""
    if field in REPUTATION:
        return (b"\x01\x12" + struct.pack("<h", value)).hex()
    return (b"\x01\x11" + struct.pack("<b", value)).hex()


class GameEdits:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge

    # --- planning (rules) ------------------------------------------------------------
    def _recent(self, career: Career, record: str, field: str) -> list[dict]:
        return [e for e in career.store.events("edit_done", limit=500)
                if (e["payload"] or {}).get("record") == record and (e["payload"] or {}).get("field") == field]

    def _plan(self, career: Career, record: str, name: str, field: str, delta: int, reason: str, key: str) -> None:
        store = career.store
        done = self._recent(career, record, field)
        if done and _days(done[-1]["game_date"], career.date) < COOLDOWN_DAYS:
            return
        if field not in LIMITS:
            season = [e for e in done if _days(e["game_date"], career.date) < 300]
            if abs(sum((e["payload"] or {}).get("delta", 0) for e in season) + delta) > SEASON_CAP:
                return
        pending = [e for e in store.events("edit_plan", limit=200) if (e["payload"] or {}).get("status") == "pending"
                   and (e["payload"] or {}).get("record") == record and (e["payload"] or {}).get("field") == field]
        if pending:
            return
        store.add_event("edit_plan", career.date, {"record": record, "name": name, "kind": "person", "field": field,
                                                   "delta": delta, "reason": reason, "status": "pending"},
                        event_key=f"edit:{key}")

    def plan(self, career: Career, roster) -> None:
        """Turns what happened into planned edits (once a game day)."""
        store = career.store
        if store.get_meta("edits_planned_on") == career.date:
            return
        store.set_meta("edits_planned_on", career.date)
        for e in store.events("news", limit=50):
            p = e["payload"] or {}
            if p.get("origin") == "social" and _days(e["game_date"], career.date) <= 2:
                drop = -int(20 * (p.get("weight") or 1.5))
                self._plan(career, career.manager_id, career.manager_name, "当前声望", drop,
                           f"社交媒体风波上了新闻：「{p.get('title')}」", f"rep:{e.get('event_key')}")
        for p in roster.at_club(career):
            rel = store.relation(p["id"])
            warmth = rel["trust"] + rel["liking"]
            if warmth >= 60:
                self._plan(career, p["id"], p["name"], "职业", +1,
                           f"主教练长期关照他（信任+喜爱 {warmth}）", f"prof:{p['id']}:{career.date}")
            elif warmth <= -60:
                self._plan(career, p["id"], p["name"], "情绪控制", -1,
                           f"和主教练关系很差（信任+喜爱 {warmth}）", f"temp:{p['id']}:{career.date}")

    # --- carrying out ----------------------------------------------------------------
    def pending(self, career: Career) -> list[dict]:
        return [e for e in career.store.events("edit_plan", limit=200) if (e["payload"] or {}).get("status") == "pending"]

    def run(self, career: Career, busy: bool = False) -> int:
        """Carries out up to MAX_PER_TICK planned edits. Never while FM shows a dialog (busy)."""
        if busy:
            return 0
        done = 0
        for e in self.pending(career)[:MAX_PER_TICK]:
            plan = e["payload"]
            result = self._apply(career, plan)
            career.store.update_event(e["event_key"], plan | result)
            done += result.get("status") == "done"
        return done

    def _apply(self, career: Career, plan: dict) -> dict:
        b = self.bridge
        if not b.editor_open(plan["record"], plan.get("kind", "person")):
            return {"status": "failed", "error": "editor did not open"}  # nothing opened: nothing to close
        try:
            time.sleep(2.5)
            for choices in TABS[plan.get("kind", "person")]:
                if not any(b.ui_click(t) for t in choices):
                    raise RuntimeError(f"tab {choices} not found")
                time.sleep(2)
            field = b.editor_fields().get(plan["field"])
            if not field:
                raise RuntimeError(f"field {plan['field']} not on screen")
            before = int(str(field["value"]).strip())
            lo, hi = LIMITS.get(plan["field"], (1, 20))
            after = max(lo, min(hi, before + int(plan["delta"])))
            if after != before:
                if not b.editor_set(field["key"], encode(plan["field"], after)):
                    raise RuntimeError("write refused")
                time.sleep(1.5)
                now = b.editor_fields().get(plan["field"], {}).get("value")
                if str(now).strip() != str(after):
                    raise RuntimeError(f"value is {now} after writing {after}")
            career.store.add_event("edit_done", career.date, {
                "record": plan["record"], "name": plan.get("name"), "field": plan["field"], "before": before,
                "after": after, "delta": after - before, "reason": plan.get("reason")})
            return {"status": "done", "before": before, "after": after}
        except (RuntimeError, ValueError, BridgeUnavailable) as err:
            return {"status": "failed", "error": str(err)}
        finally:
            try:
                b.editor_close()
            except BridgeUnavailable:
                pass

    def log(self, career: Career, limit: int = 50) -> list[dict]:
        out = [{"date": e["game_date"], **(e["payload"] or {})} for e in career.store.events("edit_done", limit=limit)]
        out.reverse()
        return out
