"""Minimal ISO-TP (ISO 15765-2) request/response on 11-bit IDs.

Enough for VIN / CALID / $1A $90. Not a full stack — kernel upload stays
in the vendored E92 reader.
"""

from __future__ import annotations

import time

from .adapter import PHYSICAL_REQ, PHYSICAL_RSP, Transport


def isotp_request(
    transport: Transport,
    payload: bytes,
    *,
    txid: int = PHYSICAL_REQ,
    rxid: int = PHYSICAL_RSP,
    timeout: float = 1.2,
) -> bytes:
    payload = bytes(payload)
    if len(payload) <= 7:
        transport.send(txid, bytes([len(payload)]) + payload)
    else:
        raise ValueError("host multi-frame TX not needed for public identity")

    deadline = time.time() + timeout
    assembled = bytearray()
    expected = 0
    next_sn = 1
    while time.time() < deadline:
        msg = transport.recv(min(0.08, max(0.0, deadline - time.time())))
        if msg is None:
            if assembled and expected and len(assembled) >= expected:
                break
            continue
        if msg.arbitration_id != rxid:
            continue
        data = bytes(msg.data)
        if len(data) < 2:
            continue
        pci = data[0]
        kind = pci >> 4
        if kind == 0:  # single
            n = pci & 0x0F
            return data[1 : 1 + n]
        if kind == 1:  # first
            expected = ((pci & 0x0F) << 8) | data[1]
            assembled = bytearray(data[2:8])
            next_sn = 1
            transport.send(txid, bytes([0x30, 0x00, 0x00, 0, 0, 0, 0, 0]))
            continue
        if kind == 2 and assembled:  # consecutive
            if (pci & 0x0F) != (next_sn & 0x0F):
                continue
            assembled.extend(data[1:8])
            next_sn += 1
            if expected and len(assembled) >= expected:
                return bytes(assembled[:expected])
    return bytes(assembled[:expected] if expected else assembled)
