"""Decoder for the binary values FM's interface sends to its engine (events).

Each value is a present flag (0x01, or 0x00 for null) followed by a type byte:

    0x0a record  u32 count, then count x (4-byte property code, value)
    0x0b list    u32 count, then count x value
    0x22 ref     u8 count, then count x (u32 code, i32 value)   (an interop reference)
    0x1a string  u32 length, UTF-8 bytes
    0x02 int32   0x01 int32 (as seen)   0x12 int16   0x11 int8   0x03 bool

Unknown types stop decoding of the enclosing value (returned as None).
"""
from __future__ import annotations

import struct


class _Reader:
    def __init__(self, data: bytes):
        self.b, self.i = data, 0

    def take(self, n: int) -> bytes:
        if self.i + n > len(self.b):
            raise ValueError("truncated")
        out = self.b[self.i:self.i + n]
        self.i += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]


def code_str(raw: bytes) -> str:
    return raw.decode("latin1") if all(32 <= c < 127 for c in raw) else "#" + raw[::-1].hex()


def _value(r: _Reader):
    if r.u8() == 0:
        return None
    kind = r.u8()
    if kind == 0x0a:
        out = {}
        for _ in range(r.u32()):
            key = code_str(r.take(4))
            out[key] = _value(r)
        return out
    if kind == 0x0b:
        return [_value(r) for _ in range(r.u32())]
    if kind == 0x22:
        pairs = {}
        for _ in range(r.u8()):
            code, val = struct.unpack("<Ii", r.take(8))
            pairs[code_str(struct.pack("<I", code))] = val
        if "ferd" in pairs:
            return {"ref": f"{pairs['ferd'] & 0xffffffff:08x}", "pairs": pairs}
        return {"pairs": pairs}
    if kind == 0x1a:
        return r.take(r.u32()).decode("utf-8", "replace")
    if kind in (0x02, 0x01):
        return struct.unpack("<i", r.take(4))[0]
    if kind == 0x12:
        return struct.unpack("<h", r.take(2))[0]
    if kind == 0x11:
        return struct.unpack("<b", r.take(1))[0]
    if kind == 0x03:
        return bool(r.u8())
    raise ValueError(f"unknown value type 0x{kind:02x}")


def decode(hex_data: str):
    """The decoded value, or None if it cannot be decoded."""
    try:
        return _value(_Reader(bytes.fromhex(hex_data)))
    except (ValueError, struct.error):
        return None


def target_pairs(hex_target: str) -> dict:
    """A serialized target ("u8 count + (u32 code, i32 value) pairs") as {code: value}."""
    try:
        b = bytes.fromhex(hex_target)
        out = {}
        for k in range(b[0]):
            code, val = struct.unpack("<Ii", b[1 + 8 * k:9 + 8 * k])
            out[code_str(struct.pack("<I", code))] = val
        return out
    except (ValueError, IndexError, struct.error):
        return {}


# --- encoding (only what the interface sends when an option is clicked) ---------------
def _code(c: str) -> bytes:
    b = c.encode("latin1")
    assert len(b) == 4
    return b


def _int(v: int) -> bytes:
    if -128 <= v < 128:
        return b"\x01\x11" + struct.pack("<b", v)
    if -32768 <= v < 32768:
        return b"\x01\x12" + struct.pack("<h", v)
    return b"\x01\x02" + struct.pack("<i", v)


def speak_choice(person: str, guid: int, option_id: int) -> str:
    """Data of a SpeakTo choice event, byte for byte as FM's interface builds it:
    {h1J: person, Vpcz: -1, p3fs: option, pQuB: 0, vQuB: {}, tdca: {}}."""
    ref = b"\x01\x22\x03" + b"".join(_code(c) + struct.pack("<i", v) for c, v in (
        (" 011", 0x1c), ("diuG", guid), ("ferd", int(person, 16) - (1 << 32) if int(person, 16) >= 1 << 31 else int(person, 16))))
    empty = b"\x01\x0a" + struct.pack("<I", 0)
    body = (_code("h1J ") + ref + _code("Vpcz") + _int(-1) + _code("p3fs") + _int(option_id)
            + _code("pQuB") + b"\x01\x02" + struct.pack("<i", 0) + _code("vQuB") + empty + _code("tdca") + empty)
    return (b"\x01\x0a" + struct.pack("<I", 6) + body).hex()


def _ref_record(record: str) -> bytes:
    """{ferd: record} as actions carry their people/teams."""
    v = int(record, 16)
    v = v - (1 << 32) if v >= 1 << 31 else v
    return b"\x01\x0a" + struct.pack("<I", 1) + _code("ferd") + b"\x01\x01" + struct.pack("<i", v)


def action_target(guid: int, code: str) -> str:
    """The action object an action event goes to: (' 011'=1, Guid, dica=<action code>)."""
    pairs = [(" 011", 1), ("diuG", guid), ("dica", struct.unpack("<i", _code(code))[0])]
    return (bytes([len(pairs)]) + b"".join(_code(c) + struct.pack("<i", v) for c, v in pairs)).hex()


def action_data(team: str, npc: str, human: str, record: str, records: list[str], extra: dict | None = None) -> str:
    """Data of an action event (event 44), byte for byte as the interface builds it:
    {aetT: team, aCPN: npc, muhH: human manager, ferd: record, Lerd: [records], ...extra}."""
    fields = [("aetT", _ref_record(team)), ("aCPN", _ref_record(npc)), ("muhH", _ref_record(human)),
              ("ferd", _ref_record(record)),
              ("Lerd", b"\x01\x0b" + struct.pack("<I", len(records)) + b"".join(_ref_record(r) for r in records))]
    for k, v in (extra or {}).items():
        fields.append((k, _int(v)))
    return (b"\x01\x0a" + struct.pack("<I", len(fields)) + b"".join(_code(k) + v for k, v in fields)).hex()
