"""Client for the FM27Real bridge's local query API.

The bridge (injected into Football Manager 27) answers on 127.0.0.1 and writes
its URL and token to api.json in the data folder (a new token every launch).
This module batches queries, maps the property codes we have identified to
readable fields, and resolves portraits from FM's graphics folder.

Command line (tools/fm27.py):

    python3 tools/fm27.py status | game | seen
    python3 tools/fm27.py person 01049638
    python3 tools/fm27.py squad 18000230
    python3 tools/fm27.py dressing-room 18000230
    python3 tools/fm27.py raw 01049638 PersonReference > setford.json

Record ids are 8 hex digits: kind byte + index (01 person, 03 club, 0a nation,
18 team, 19 competition). Property codes are 4 characters, mostly English
words reversed ("eman" = name). See docs/PROTOCOL.md.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FM_DIR = Path.home() / "Library/Application Support/Sports Interactive/Football Manager 27"
DATA_DIR = FM_DIR / "FM27Real"
GRAPHICS = FM_DIR / "graphics"
BATCH = 40  # properties per request; large batches answer too slowly

# The game-level record (GameReference): a UI record with a single pair
# (" 011" = 0x36), the same in every session observed.
GAME_TARGET = "012030313136000000"
GAME_FIELDS = {
    "date": "1kBR",          # current game date, "2028-03-07"
    "datetime": "   8",      # "2028-3-7 09:00"
    "manager": "MuCG",       # the human manager (person ref)
    "team": "tcMC",          # the human manager's team (team ref)
    "save_name": " 811",     # "\"David Wang-莱斯特城\" (已保存 102 次)"
    "save_count": " 221",    # 102: how many times this save file has been saved
    "start": " 421",         # "英格兰 - 2026-7-13": database and career start
}

# Codes confirmed against a player's in-game profile (Tommy Setford, 2028-03-07).
PERSON_FIELDS = {
    "name": "eman",
    "surname": "TkaX",
    "initials": "u36b",
    "uid": "dinu",
    "age": "  51",
    "height_cm": "  81",
    "nationality": "Ytan",
    "nationalities": "Ttan",
    "languages": "cpsl",
    "position": "SspP",
    "personality": "tnsP",
    "media_handling": "HdeM",
    "squad_status": "tHV ",
    "career_stage": "OBmX",
    "fitness": "UPJg",
    "playing_time_feeling": "*522",
    "contract_status": "BTaP",
    "trained_at": "CghP",
    "average_rating": "9wPC",
    "birth_date": "bodP",
    "joined": "d7d ",
    "team": "maeT",
    "bio": "aC5W",
    "profile": "cdbp",
    "history": "dkwi",
}

TEAM_FIELDS = {
    "name": "eman",
    "squad": "lpLF",   # first-team players
    "staff": " Vj5",   # manager first, then coaches and other staff
}

# Social model (confirmed against Leicester's dynamics screen, 2028-03-07).
# Values are FM "dynamic references": {"dyn": type, "props": {code: value}}.
PERSON_SOCIAL = {
    "influence": "tHV ",        # 球队领袖 / 极具影响力球员 / 有影响力球员 / 其他
    "favoured": "NCdZ",         # favoured people: nNhU = [{srep: person, nrtS: relation}]
    "manager_relation": "nhlr", # ohlr = view of the manager, phlr = relationship kind
    "happiness": ["4g1d", "5g1d", "6g1d", "7g1d", "8g1d"],  # per category; csed = sentence
}
PERSON_ORIGIN = {
    "birth_city": "bfoc",       # city ref
    "birth_region": "bfor",     # region ref
    "favoured_clubs": "lcfP",   # dyn; each slot has smti = [club name]
}
TEAM_SOCIAL = {
    "groups": "0vQT",            # Li3E / Mi3E / Ni3E = member lists of social groups 1-3
    "manager_support": "Nk1A",   # each: XsfK = rating word, Qk1A/Vk1A = values,
    "cohesion": "Ok1A",          #   2bMM = summary, Sk1A = [{CGSQ: reason}]
    "atmosphere": "Pk1A",
}
_MARKUP = re.compile(r"(male|female)#\d|\|/?b\||</?b>|<[^>]+>")


def _text(value):
    """FM link markup arrives as {"text", "links"}; keep the visible text."""
    if isinstance(value, dict) and "text" in value:
        value = value["text"]
    if isinstance(value, str):
        return _MARKUP.sub("", value.replace("\u200b", "")).strip()
    return value


def _props(value) -> dict:
    return value.get("props", {}) if isinstance(value, dict) else {}


def _refs(value) -> list[str]:
    return [x["ref"] for x in value if isinstance(x, dict) and "ref" in x] if isinstance(value, list) else []


class BridgeUnavailable(Exception):
    """The game is not running with the bridge, or no career is loaded."""


class Bridge:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self.url = self.token = None

    def _load_api(self) -> None:
        try:
            api = json.loads((self.data_dir / "api.json").read_text())
        except (OSError, ValueError) as e:
            raise BridgeUnavailable("bridge has not started (no api.json)") from e
        self.url, self.token = api["url"], api["token"]

    def _get(self, path: str, timeout: float = 30) -> object:
        # The token changes every game launch: re-read api.json once on failure.
        for attempt in (0, 1):
            if not self.url or attempt:
                self._load_api()
            req = urllib.request.Request(self.url + path, headers={"X-Token": self.token})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 503:
                    raise BridgeUnavailable("no career loaded") from e
                if e.code != 401 or attempt:
                    raise
            except (urllib.error.URLError, ConnectionError) as e:
                if attempt:
                    raise BridgeUnavailable("game not running") from e
        raise BridgeUnavailable("bridge unreachable")

    def status(self) -> dict:
        return self._get("/status")

    def seen(self) -> list[str]:
        return self._get("/seen")

    def query(self, record: str, codes: list[str]) -> dict:
        """Values of the given property codes for one record (missing = not applicable)."""
        out: dict = {}
        for i in range(0, len(codes), BATCH):
            chunk = ",".join(urllib.parse.quote(c, safe="#") for c in codes[i : i + BATCH])
            out.update(self._get(f"/query?id={record}&props={chunk}"))
        return out

    def query_target(self, target_hex: str, codes: list[str]) -> dict:
        """Like query(), for a serialized target (e.g. a "target" value or GAME_TARGET)."""
        out: dict = {}
        for i in range(0, len(codes), BATCH):
            chunk = ",".join(urllib.parse.quote(c, safe="#") for c in codes[i : i + BATCH])
            out.update(self._get(f"/query?target={target_hex}&props={chunk}"))
        return out

    def game(self) -> dict:
        """Current date, the human manager and team, save name and career start."""
        raw = self.query_target(GAME_TARGET, list(GAME_FIELDS.values()))
        g = {}
        for field, code in GAME_FIELDS.items():
            v = raw.get(code)
            g[field] = v["ref"] if isinstance(v, dict) and "ref" in v else _text(v)
        return g

    def type_codes(self, type_name: str) -> list[str]:
        """Every property code of a reference type, from catalog.tsv."""
        prefix = f"FM.UI.{type_name}\t"
        codes = []
        with open(self.data_dir / "catalog.tsv", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(prefix):
                    codes.append("#" + line.split("\t")[3])
        return codes

    def query_all(self, record: str, type_name: str) -> dict:
        return self.query(record, self.type_codes(type_name))

    # --- readable records -----------------------------------------------------
    def person(self, record: str) -> dict:
        raw = self.query(record, list(PERSON_FIELDS.values()))
        p = {"id": record}
        for field, code in PERSON_FIELDS.items():
            if code in raw:
                p[field] = raw[code]["ref"] if isinstance(raw[code], dict) and "ref" in raw[code] else _text(raw[code])
        uid = p.get("uid")
        if isinstance(uid, int):
            for kind, name in (("face", f"faces/face_{uid}.png"), ("icon", f"iconfaces/iconface_{uid}.png")):
                path = GRAPHICS / name
                if path.exists():
                    p[kind] = str(path)
        return p

    def team(self, record: str) -> dict:
        raw = self.query(record, list(TEAM_FIELDS.values()))
        refs = lambda v: [x["ref"] for x in v if isinstance(x, dict) and "ref" in x] if isinstance(v, list) else []
        return {
            "id": record,
            "name": _text(raw.get(TEAM_FIELDS["name"])),
            "squad": refs(raw.get(TEAM_FIELDS["squad"])),
            "staff": refs(raw.get(TEAM_FIELDS["staff"])),
        }

    def name(self, record: str) -> str:
        return _text(self.query(record, ["eman"]).get("eman")) or record

    def person_origin(self, record: str) -> dict:
        """Birthplace and favoured clubs (names resolved)."""
        raw = self.query(record, list(PERSON_ORIGIN.values()))
        out = {}
        for field in ("birth_city", "birth_region"):
            v = raw.get(PERSON_ORIGIN[field])
            if isinstance(v, dict) and "ref" in v:
                out[field] = self.name(v["ref"])
        clubs = []
        for slot in _props(raw.get(PERSON_ORIGIN["favoured_clubs"])).values():
            clubs += [_text(n) for n in _props(slot).get("smti") or []]
        out["favoured_clubs"] = [c for c in clubs if c]
        return out

    def person_social(self, record: str) -> dict:
        """Influence, favoured people, view of the manager and happiness concerns."""
        codes = [PERSON_SOCIAL["influence"], PERSON_SOCIAL["favoured"], PERSON_SOCIAL["manager_relation"]]
        raw = self.query(record, codes + PERSON_SOCIAL["happiness"])
        favoured = []
        for f in _props(raw.get(PERSON_SOCIAL["favoured"])).get("nNhU") or []:
            p = _props(f)
            ref = (p.get("srep") or {}).get("ref")
            if ref:
                favoured.append({"id": ref, "name": self.name(ref), "relation": _text(p.get("nrtS"))})
        rel = _props(raw.get(PERSON_SOCIAL["manager_relation"]))
        happiness = [_text(_props(raw.get(c)).get("csed")) for c in PERSON_SOCIAL["happiness"]]
        return {
            "influence": _text(raw.get(PERSON_SOCIAL["influence"])),
            "favoured": favoured,
            "view_of_manager": _text(rel.get("ohlr")),
            "happiness": [h for h in happiness if h and h != "-"],
        }

    def dressing_room(self, team_record: str) -> dict:
        """Team dynamics, social groups and every first-team player's social state."""
        raw = self.query(team_record, list(TEAM_SOCIAL.values()))
        groups = _props(raw.get(TEAM_SOCIAL["groups"]))
        group_lists = [_refs(groups.get(code)) for code in ("Li3E", "Mi3E", "Ni3E")]
        dynamics = {}
        for key in ("manager_support", "cohesion", "atmosphere"):
            p = _props(raw.get(TEAM_SOCIAL[key]))
            dynamics[key] = {
                "rating": _text(p.get("XsfK")),
                "value": p.get("Vk1A"),
                "summary": _text(p.get("2bMM")),
                "reasons": [_text(_props(r).get("CGSQ")) for r in p.get("Sk1A") or []],
            }
        team = self.team(team_record)
        players = []
        for ref in team["squad"]:
            group = next((i + 1 for i, members in enumerate(group_lists) if ref in members), None)
            players.append({"id": ref, "name": self.name(ref), "social_group": group, **self.person_social(ref)})
        return {"team": team["name"], "dynamics": dynamics,
                "social_groups": [[self.name(r) for r in g] for g in group_lists if g], "players": players}

    def squad(self, team_record: str) -> dict:
        t = self.team(team_record)
        t["squad"] = [self.person(r) for r in t["squad"]]
        return t


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    b = Bridge()
    cmd, args = argv[1], argv[2:]
    if cmd == "status":
        out = b.status()
    elif cmd == "game":
        out = b.game()
    elif cmd == "seen":
        out = b.seen()
    elif cmd == "person" and args:
        out = b.person(args[0])
    elif cmd == "team" and args:
        out = b.team(args[0])
    elif cmd == "squad" and args:
        out = b.squad(args[0])
    elif cmd == "dressing-room" and args:
        out = b.dressing_room(args[0])
    elif cmd == "raw" and len(args) == 2:
        out = b.query_all(args[0], args[1])
    else:
        print(__doc__)
        return 2
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0
