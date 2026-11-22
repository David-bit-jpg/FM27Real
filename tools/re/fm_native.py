"""Offline analysis of FM's native engine (game_plugin, arm64 slice). Read-only.

Builds a map of the engine from its machine code, without running the game:

- functions: every function start (LC_FUNCTION_STARTS survives symbol stripping);
- codes: every 32-bit constant built with MOVZ/MOVK that reads as a 4-character
  code (FM's property codes like 'aroM' and event codes like 'BJ78'), with the
  functions that use it;
- classes: C++ RTTI type names (N11game_plugin...E) and the vtables that point to
  them, so functions can be named after the class whose vtable holds them.

    python3 tools/re/fm_native.py <game_plugin arm64 slice> <out.json>

The result is a JSON index used by docs/ENGINE_MAP.md and later tools.
"""
from __future__ import annotations

import json
import re
import struct
import sys
from bisect import bisect_right

import numpy as np


def load_commands(data: bytes):
    magic, cputype, _, _, ncmds, _, _, _ = struct.unpack_from("<IiiIIIII", data, 0)
    assert magic == 0xFEEDFACF, "not a thin 64-bit Mach-O"
    off, out = 32, []
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, off)
        out.append((cmd, off, size))
        off += size
    return out


def segments(data: bytes):
    segs, sects = [], {}
    for cmd, off, _ in load_commands(data):
        if cmd != 0x19:  # LC_SEGMENT_64
            continue
        segname = data[off + 8:off + 24].rstrip(b"\0").decode()
        vmaddr, vmsize, fileoff, filesize, _, _, nsects, _ = struct.unpack_from("<QQQQiiII", data, off + 24)
        segs.append((segname, vmaddr, vmsize, fileoff, filesize))
        so = off + 72
        for _ in range(nsects):
            sect = data[so:so + 16].rstrip(b"\0").decode()
            addr, size, foff = struct.unpack_from("<QQI", data, so + 32)
            sects[(segname, sect)] = (addr, size, foff)
            so += 80
    return segs, sects


def function_starts(data: bytes, text_vm: int) -> list[int]:
    for cmd, off, _ in load_commands(data):
        if cmd == 0x26:  # LC_FUNCTION_STARTS
            dataoff, datasize = struct.unpack_from("<II", data, off + 8)
            out, addr, i, end = [], text_vm, dataoff, dataoff + datasize
            while i < end:
                delta = shift = 0
                while True:
                    b = data[i]
                    i += 1
                    delta |= (b & 0x7F) << shift
                    shift += 7
                    if b < 0x80:
                        break
                if delta == 0:
                    break
                addr += delta
                out.append(addr)
            return out
    return []


def fourcc(v: int) -> str | None:
    b = struct.pack("<I", v)
    if all(0x20 <= c < 0x7F for c in b) and sum(chr(c).isalnum() for c in b) >= 3:
        return b.decode()
    return None


def scan_codes(data: bytes, sects: dict, funcs: list[int]) -> dict[str, set[int]]:
    """4-character constants built by MOVZ (lsl 0) + MOVK (lsl 16) on the same register."""
    addr, size, foff = sects[("__TEXT", "__text")]
    ins = np.frombuffer(data, dtype="<u4", count=size // 4, offset=foff)
    is_mov = ((ins >> 23) & 0x3F) == 0x25
    opc = (ins >> 29) & 3
    hw = (ins >> 21) & 3
    imm = (ins >> 5) & 0xFFFF
    rd = ins & 31
    movz0 = np.nonzero(is_mov & (opc == 2) & (hw == 0))[0]
    movk1 = is_mov & (opc == 3) & (hw == 1)
    out: dict[str, set[int]] = {}
    n = len(ins)
    for i in movz0:
        for j in range(i + 1, min(i + 6, n)):
            if movk1[j] and rd[j] == rd[i]:
                code = fourcc(int(imm[i]) | (int(imm[j]) << 16))
                if code:
                    pc = addr + 4 * int(i)
                    k = bisect_right(funcs, pc) - 1
                    out.setdefault(code, set()).add(funcs[k] if k >= 0 else pc)
                break
    return out


def rtti(data: bytes, segs, sects) -> dict[str, list[int]]:
    """C++ class names (RTTI) -> vtable addresses (chained-fixup pointers decoded by their low 36 bits)."""
    names = {}
    for key in (("__TEXT", "__const"), ("__TEXT", "__cstring")):
        if key not in sects:
            continue
        addr, size, foff = sects[key]
        blob = data[foff:foff + size]
        for m in re.finditer(rb"(?:N\d+\w+?E|\d+[A-Za-z_]\w+)\0", blob):
            s = m.group(0)[:-1]
            if len(s) > 6 and (s.startswith(b"N") or s[0:1].isdigit()):
                names[addr + m.start()] = s.decode("latin1")
    ptrs: dict[int, list[int]] = {}
    for segname, vmaddr, vmsize, fileoff, filesize in segs:
        if not segname.startswith("__DATA"):
            continue
        words = np.frombuffer(data, dtype="<u8", count=filesize // 8, offset=fileoff)
        target = words & 0xFFFFFFFFF  # chained fixup: target vmaddr in the low 36 bits
        for i in np.nonzero(np.isin(target, np.fromiter(names.keys(), dtype=np.uint64)))[0]:
            ptrs.setdefault(int(target[i]), []).append(vmaddr + 8 * int(i))
    # typeinfo object: [vtable-for-typeinfo, name ptr]; its address is name-ptr slot - 8.
    typeinfo = {}
    for name_addr, slots in ptrs.items():
        for slot in slots:
            typeinfo[slot - 8] = names[name_addr]
    vtables: dict[str, list[int]] = {}
    for segname, vmaddr, vmsize, fileoff, filesize in segs:
        if not segname.startswith("__DATA"):
            continue
        words = np.frombuffer(data, dtype="<u8", count=filesize // 8, offset=fileoff)
        target = words & 0xFFFFFFFFF
        for i in np.nonzero(np.isin(target, np.fromiter(typeinfo.keys(), dtype=np.uint64)))[0]:
            # vtable: [offset-to-top][typeinfo][virtual functions...]
            if i > 0 and words[i - 1] == 0:
                vtables.setdefault(typeinfo[int(target[i])], []).append(vmaddr + 8 * (int(i) + 1))
    return vtables


def vtable_functions(data: bytes, segs, vt_addr: int, text_lo: int, text_hi: int, limit: int = 64) -> list[int]:
    for segname, vmaddr, vmsize, fileoff, filesize in segs:
        if vmaddr <= vt_addr < vmaddr + filesize:
            out, off = [], fileoff + (vt_addr - vmaddr)
            for k in range(limit):
                (w,) = struct.unpack_from("<Q", data, off + 8 * k)
                t = w & 0xFFFFFFFFF
                if not (text_lo <= t < text_hi):
                    break
                out.append(t)
            return out
    return []


def main() -> None:
    path, out_path = sys.argv[1], sys.argv[2]
    data = open(path, "rb").read()
    segs, sects = segments(data)
    text_vm = next(v for s, v, *_ in segs if s == "__TEXT")
    funcs = function_starts(data, text_vm)
    taddr, tsize, _ = sects[("__TEXT", "__text")]
    print(f"{len(funcs)} functions", file=sys.stderr)
    codes = scan_codes(data, sects, funcs)
    print(f"{len(codes)} distinct 4-character codes", file=sys.stderr)
    vtables = rtti(data, segs, sects)
    print(f"{len(vtables)} classes with vtables", file=sys.stderr)
    owner: dict[int, list[str]] = {}
    classes = {}
    for name, vts in vtables.items():
        fns = []
        for vt in vts:
            fns += vtable_functions(data, segs, vt, taddr, taddr + tsize)
        classes[name] = {"vtables": [hex(v) for v in vts], "functions": [hex(f) for f in fns]}
        for f in fns:
            owner.setdefault(f, []).append(name)
    index = {
        "functions": len(funcs),
        "codes": {c: sorted(hex(f) for f in fs) for c, fs in sorted(codes.items())},
        "classes": classes,
        "function_owners": {hex(f): names for f, names in owner.items()},
    }
    json.dump(index, open(out_path, "w"), indent=0)
    print(f"written {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
