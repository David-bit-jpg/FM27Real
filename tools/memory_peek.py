"""Read-only look at the world memory the engine is using (never modifies anything).

    python3 tools/memory_peek.py                 # counts per kind
    python3 tools/memory_peek.py events press_answer
    python3 tools/memory_peek.py person 010a8129

Diagnostics must never construct a CareerTracker: opening a career runs the
load logic, which can swap memory files under the running engine.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.bridge import DATA_DIR  # noqa: E402

files = [p for p in (DATA_DIR / "careers").glob("*/memory/*.sqlite") if ".before-load" not in p.name]
path = max(files, key=lambda p: p.stat().st_mtime)
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
print(path.relative_to(DATA_DIR))
args = sys.argv[1:]
if not args:
    print("events:", db.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind").fetchall())
    print("memories:", db.execute("SELECT kind, COUNT(*) FROM memories GROUP BY kind").fetchall())
    print("messages:", db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
elif args[0] == "events":
    for date, payload in db.execute("SELECT game_date, payload FROM events WHERE kind = ? ORDER BY id DESC LIMIT 10",
                                    (args[1],)):
        print(date, json.dumps(json.loads(payload), ensure_ascii=False)[:300])
elif args[0] == "person":
    for row in db.execute("SELECT game_date, sender, text FROM messages WHERE person_id = ? ORDER BY id", (args[1],)):
        print(*row)
    for row in db.execute("SELECT game_date, weight, text FROM memories WHERE person_id = ? ORDER BY id", (args[1],)):
        print("  memory:", *row)
