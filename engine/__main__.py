"""Run the world engine:  python3 -m engine

    python3 -m engine                 start the local API (127.0.0.1:27271)
    python3 -m engine career          show the loaded career
    python3 -m engine person <id>     show a person snapshot (cached per game day)
    python3 -m engine chat <id>       text that person in the terminal (Ctrl+D to stop)
"""
import json
import sys

from . import dialogue, snapshot
from .bridge import Bridge, BridgeUnavailable
from .career import CareerTracker
from .model import ModelError, provider_from_settings
from .server import load_settings, serve


def main(argv: list[str]) -> int:
    if len(argv) == 1:
        serve()
        return 0
    try:
        bridge = Bridge()
        career = CareerTracker(bridge).current()
        if argv[1] == "career":
            out = career.summary()
        elif argv[1] == "person" and len(argv) >= 3:
            out = snapshot.get(bridge, career, argv[2], refresh="--refresh" in argv)
        elif argv[1] == "chat" and len(argv) == 3:
            return chat(bridge, career, argv[2])
        else:
            print(__doc__)
            return 2
    except BridgeUnavailable as e:
        print(f"bridge unavailable: {e}", file=sys.stderr)
        return 1
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def chat(bridge, career, person_id: str) -> int:
    settings = load_settings()
    model = provider_from_settings(settings)
    snap = snapshot.get(bridge, career, person_id)
    print(f"{snap.get('name')} · {career.team_name} · {career.date}  (Ctrl+D to stop)")
    for m in career.store.messages(person_id, limit=10):
        who = career.manager_name if m["sender"] == "manager" else snap.get("name")
        print(f"  {who}: {m['text']}")
    while True:
        try:
            text = input(f"{career.manager_name}> ").strip()
        except EOFError:
            print()
            return 0
        if not text:
            continue
        try:
            r = dialogue.converse(model, career, snap, text, settings["language"])
        except ModelError as e:
            print(f"  (model error: {e})")
            continue
        d = r["relation_delta"]
        print(f"{snap.get('name')}: {r['reply']}")
        print(f"  [{r['mood']} · trust {d['trust']:+d} liking {d['liking']:+d} respect {d['respect']:+d}"
              + (f" · memory: {r['memory']}" if r["memory"] else "")
              + (f" · canon: {r['new_canon']}" if r["new_canon"] else "") + "]")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
