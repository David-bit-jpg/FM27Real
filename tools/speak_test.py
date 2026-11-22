"""Lists the FM dialog on screen (player conversation, press conference) and can
choose one of its options the way the interface does.

    python3 tools/speak_test.py            # show options
    python3 tools/speak_test.py 5          # choose option p3fs=5
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.bridge import Bridge  # noqa: E402
from engine.fmvalue import speak_choice, target_pairs  # noqa: E402
from engine.native import NativeSync  # noqa: E402

b = Bridge()
n = NativeSync(b)
n._refresh_options()
target = n.speak_target
person = next(iter(sorted(n.by_person.items(), key=lambda kv: -kv[1]["at"])), (None, None))[0]
if not person:  # player conversation: the person is in the conversation session
    session = b.last_target(23)
    ferd = target_pairs(session or "").get("lCZi")
    person = f"{ferd & 0xffffffff:08x}" if isinstance(ferd, int) and ferd != -1 else None
print("dialog:", target)
print("person:", person, b.name(person) if person else "")
for pid, o in sorted(n.options.items()):
    print(f"  {pid:>6}  event={o.get('event')}  [{o.get('topic') or '话题'}] {o['text']}")
if len(sys.argv) > 1 and target and person:
    pid = int(sys.argv[1])
    opt = n.options[pid]
    guid = int(b.status()["guid"], 16)
    ok = b.send_event(opt["event"], target, speak_choice(person, guid, pid))
    print("sent:", ok)
