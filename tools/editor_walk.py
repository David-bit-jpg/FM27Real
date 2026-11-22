"""Read-only walk of FM's In-Game Editor tree while the editor is open.

Start from the editor root the game itself opened (record type 42, the most
recent one), follow only the child targets the game returns (sections and
fields), and write every field's path, name, value, hint and options to
editor_tree.json in the data folder. Never writes anything to the game.

    python3 tools/editor_walk.py            # root = the editor open in game
    python3 tools/editor_walk.py 400        # at most 400 nodes
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.bridge import DATA_DIR, Bridge, _text  # noqa: E402

ROOT_TYPE = 42
LISTS = ["sces", "slif", "csCe", "csGe", "csNe", "csPe", "csSe", "sloc", "swor", "S7Sx"]
FIELD = ["eman", "lvtp", "tnih", "cSde", "nrtS", "posi", "tfpo", "jVPv", "YN4y", "T4di", "6Qge", "Xbz1", "lXFi",
         " 001", "itpo", "mTyl", "VxaM", "VniM"]


def path_of(target: str) -> list[int]:
    b = bytes.fromhex(target)
    out = []
    for k in range(b[0]):
        code, value = struct.unpack("<Ii", b[1 + 8 * k:9 + 8 * k])
        if code < 16:
            out.append(value)
    return out


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    b = Bridge()
    root = b.last_target(ROOT_TYPE)
    if not root:
        sys.exit("No editor open: open the In-Game Editor on someone in the game first.")
    seen, queue, nodes = {root}, [root], []
    while queue and len(nodes) < limit:
        t = queue.pop(0)
        v = b.query_target(t, FIELD + LISTS)
        node = {"target": t, "path": path_of(t), "name": _text(v.get("eman")), "value": v.get("lvtp"),
                "hint": _text(v.get("tnih")), "desc": _text(v.get("cSde")), "options": v.get("T4di"),
                "extra": {k: v.get(k) for k in ("posi", "tfpo", "jVPv", "YN4y", "6Qge", "Xbz1", "lXFi", " 001", "itpo",
                                                "mTyl", "VxaM", "VniM") if v.get(k) not in (None, "", 0, False)}}
        nodes.append(node)
        for key in LISTS:
            for child in v.get(key) or []:
                ct = child.get("target") if isinstance(child, dict) else None
                if ct and ct not in seen:
                    seen.add(ct)
                    queue.append(ct)
        print(f"{node['path']} {node['name'] or ''} = {json.dumps(node['value'], ensure_ascii=False)[:60]}")
    out = DATA_DIR / "editor_tree.json"
    out.write_text(json.dumps(nodes, ensure_ascii=False, indent=1))
    print(f"{len(nodes)} nodes -> {out}")


if __name__ == "__main__":
    main()
