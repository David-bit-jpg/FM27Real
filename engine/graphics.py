"""FM's own pictures for the panel: player portraits and club / competition logos.

Logos come from the graphics packs in FM's graphics folder: every pack folder
has a config.xml mapping its files to "graphics/pictures/club/<uid>/logo"
(or comp/<uid>/logo), the same way FM finds them. The index is built once.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

from .bridge import GRAPHICS

_MAP = re.compile(r'<record from="([^"]+)" to="graphics/pictures/(club|comp)/(\d+)/logo"')
_index: dict[tuple[str, int], Path] | None = None
_lock = threading.Lock()


def _build() -> dict[tuple[str, int], Path]:
    out: dict[tuple[str, int], Path] = {}
    for cfg in GRAPHICS.glob("**/config.xml"):
        if "faces" in cfg.parts or "kits" in cfg.parts:
            continue
        try:
            text = cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name, kind, uid in _MAP.findall(text):
            for ext in (".png", ".jpg"):
                f = cfg.parent / f"{name}{ext}"
                if f.is_file():
                    out.setdefault((kind, int(uid)), f)
                    break
    return out


def logo(kind: str, uid: int) -> Path | None:
    """The logo file of a club or competition (kind "club" / "comp"), if FM has one."""
    global _index
    with _lock:
        if _index is None:
            _index = _build()
    return _index.get((kind, uid))


def face(uid: int) -> Path | None:
    f = GRAPHICS / f"faces/face_{uid}.png"
    return f if f.is_file() else None
