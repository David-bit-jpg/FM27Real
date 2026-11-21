"""Run the world engine:  python3 -m engine

    python3 -m engine                 start the local API (127.0.0.1:27271)
    python3 -m engine career          show the loaded career
    python3 -m engine person <id>     show a person snapshot (cached per game day)
"""
import json
import sys

from . import snapshot
from .bridge import Bridge, BridgeUnavailable
from .career import CareerTracker
from .server import serve


def main(argv: list[str]) -> int:
    if len(argv) == 1:
        serve()
        return 0
    try:
        bridge = Bridge()
        career = CareerTracker(bridge).current()
        if argv[1] == "career":
            out = career.summary()
        elif argv[1] == "person" and len(argv) == 3:
            out = snapshot.get(bridge, career, argv[2], refresh="--refresh" in argv)
        else:
            print(__doc__)
            return 2
    except BridgeUnavailable as e:
        print(f"bridge unavailable: {e}", file=sys.stderr)
        return 1
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
