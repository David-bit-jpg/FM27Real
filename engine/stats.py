"""Player statistics from FM, with the meaning of each code.

Codes confirmed against FM's own screens (a player's season stats page,
2026-11-21) and checked for sense across positions. Codes that did not hold
up (e.g. 1xSW, cgr , 2FCz) are left out rather than guessed.

- Season, per competition: person 0HgJ (SeasonStatsRowReference).
- Each of the last five matches: rows of the last-five-matches form
  (ZaLo / E2cn / phIw -> XkGV), one per fixture.
"""
from __future__ import annotations

from .bridge import Bridge, BridgeUnavailable

# code -> (label, format); format "pct" adds %, "f2" two decimals.
SEASON = {
    "pAvo": ("出场", "int"), "oGvo": ("进球", "int"), "sAaO": ("助攻", "int"), "RAvo": ("平均评分", "f2"),
    "WOVM": ("预期进球", "f2"), "gSgI": ("预期助攻", "f2"), "CTzM": ("传球成功率", "pct"),
    "QE2w": ("射正率", "pct"),
    "COj0": ("争顶成功", "int"), "ouS8": ("争顶成功率", "pct"), "2nyA": ("抢断成功", "int"),
    "rbpf": ("抢断成功率", "pct"), "V8eK": ("每90分钟犯规", "f2"),
}
# In each last-five-matches row.
MATCH = {"oGvo": "goals", "sAaO": "assists", "WOVM": "xg", "l75x": "conceded", "e9UE": "saves"}


def _fmt(v, kind: str) -> str:
    if kind == "pct":
        return f"{v:g}%"
    if kind == "f2":
        return f"{float(v):.2f}"
    return str(int(v))


def season_values(bridge: Bridge, person: str) -> dict:
    """The confirmed season numbers by code ({} when FM has no row)."""
    try:
        row = bridge.query(person, ["0HgJ"]).get("0HgJ")
    except BridgeUnavailable:
        return {}
    props = (row or {}).get("props") or {} if isinstance(row, dict) else {}
    return {c: props[c] for c in SEASON if isinstance(props.get(c), (int, float)) and not isinstance(props.get(c), bool)}


def season(bridge: Bridge, person: str) -> str:
    """'本赛季（联赛）：出场 7，进球 0，……' or '' when FM has no row."""
    try:
        row = bridge.query(person, ["0HgJ"]).get("0HgJ")
    except BridgeUnavailable:
        return ""
    props = (row or {}).get("props") or {} if isinstance(row, dict) else {}
    parts = [f"{label} {_fmt(props[code], kind)}" for code, (label, kind) in SEASON.items()
             if isinstance(props.get(code), (int, float)) and not isinstance(props.get(code), bool)
             and (props[code] or code in ("pAvo", "oGvo", "sAaO"))]  # zeros only for the basics
    return "本赛季：" + "，".join(parts) if parts and props.get("pAvo") else ""


def match_row(props: dict) -> dict:
    """The per-match stats in one last-five-matches row."""
    return {name: props[code] for code, name in MATCH.items()
            if isinstance(props.get(code), (int, float)) and not isinstance(props.get(code), bool)}
