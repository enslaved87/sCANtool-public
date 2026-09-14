"""LAS cal CS1/CS2 restamp. System/Fuel/Speedo/EngineDiag only.

Engine (384 KiB spanning MAS) is not restamped here — FF holes would
mint a wrong CVN. CS1 before CS2. firmware_ve stays false.
"""
from __future__ import annotations


def crc16_arc(data: bytes, init: int = 0) -> int:
    crc = init & 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def crc16_swap(crc: int) -> int:
    crc &= 0xFFFF
    return ((crc & 0xFF) << 8) | (crc >> 8)


def a274_u16_sum(data: bytes) -> int:
    if len(data) & 1:
        data = data + b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total = (total + ((data[i] << 8) | data[i + 1])) & 0xFFFF
    return total


def _two_hole(mod: bytes) -> bytes:
    return bytes(mod[2:0x20]) + bytes(mod[0x22:])


LAS40000_MODULES: tuple[tuple[str, int, int], ...] = (
    ("System", 0x40000, 12288),
    ("Fuel", 0x43000, 20480),
    ("Speedo", 0x48000, 4096),
    ("EngineDiag", 0x49000, 94208),
)


def restamp_module(span: bytearray) -> dict:
    """CS1 ARC+swap at +0x20, then CS2 wordsum-0 at +0x00."""
    if len(span) < 0x24:
        return {"ok": False, "error": "short"}
    if span.count(0xFF) > len(span) * 3 // 4:
        return {"ok": False, "error": "mostly FF"}
    crc = crc16_arc(_two_hole(bytes(span)))
    stored = crc16_swap(crc)
    span[0x20] = (stored >> 8) & 0xFF
    span[0x21] = stored & 0xFF
    body = a274_u16_sum(bytes(span[2:]))
    cs2 = (-body) & 0xFFFF
    span[0] = (cs2 >> 8) & 0xFF
    span[1] = cs2 & 0xFF
    return {"ok": True, "cs1": stored, "cs2": cs2}


def restamp_calibration_image(image: bytes) -> tuple[bytes, list[str]]:
    """Restamp LAS 0x40000 inner modules. Engine 0x60000 is not touched."""
    notes: list[str] = []
    if len(image) < 0x60000:
        return bytes(image), ["cal CS: image too short"]
    out = bytearray(image)
    for name, start, size in LAS40000_MODULES:
        if start + size > len(out):
            notes.append(f"cal CS {name}: skipped (doesn't fit)")
            continue
        span = bytearray(out[start : start + size])
        rec = restamp_module(span)
        if not rec.get("ok"):
            notes.append(f"cal CS {name}: skipped ({rec.get('error')})")
            continue
        out[start : start + size] = span
        notes.append(
            f"cal CS {name}: CS1={rec['cs1']:04X} CS2={rec['cs2']:04X}"
        )
    return bytes(out), notes
