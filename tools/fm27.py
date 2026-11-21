#!/usr/bin/env python3
"""Client for the FM27Real bridge's local query API.

The bridge (injected into Football Manager 27) answers on 127.0.0.1 and writes
its URL and token to api.json in the data folder. This module batches queries,
maps the property codes we have identified to readable fields, and resolves
portraits from FM's graphics folder.

    python3 tools/fm27.py status
    python3 tools/fm27.py seen
    python3 tools/fm27.py person 01049638
    python3 tools/fm27.py squad 18000230
    python3 tools/fm27.py raw 01049638 PersonReference > setford.json

Record ids are 8 hex digits: kind byte + index (01 person, 03 club, 0a nation,
18 team, 19 competition). Property codes are 4 characters, mostly English
words reversed ("eman" = name). See docs/PROTOCOL.md.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

FM_DIR = Path.home() / "Library/Application Support/Sports Interactive/Football Manager 27"
DATA_DIR = FM_DIR / "FM27Real"
GRAPHICS = FM_DIR / "graphics"
BATCH = 40  # properties per request; large batches answer too slowly

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


def _text(value):
    """FM link markup arrives as {"text", "links"}; keep the visible text."""
    if isinstance(value, dict) and "text" in value:
        return value["text"].replace("​", "")
    if isinstance(value, str):
        return value.replace("​", "")
    return value


class Bridge:
    def __init__(self, data_dir: Path = DATA_DIR):
        api = json.loads((data_dir / "api.json").read_text())
        self.url = api["url"]
        self.token = api["token"]
        self.data_dir = data_dir

    def _get(self, path: str, timeout: float = 30) -> object:
        req = urllib.request.Request(self.url + path, headers={"X-Token": self.token})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

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
    elif cmd == "seen":
        out = b.seen()
    elif cmd == "person" and args:
        out = b.person(args[0])
    elif cmd == "team" and args:
        out = b.team(args[0])
    elif cmd == "squad" and args:
        out = b.squad(args[0])
    elif cmd == "raw" and len(args) == 2:
        out = b.query_all(args[0], args[1])
    else:
        print(__doc__)
        return 2
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
