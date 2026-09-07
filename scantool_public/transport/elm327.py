"""ELM327 / STN11xx / OBDLink over serial — OBD request/response, not raw CAN.

These dongles are the most common consumer adapters. They cannot upload an
E92 read kernel (no reliable ISO-TP / $34/$36). Scan, VIN, and datalog only.
"""

from __future__ import annotations

import time

from .adapter import FUNCTIONAL_ID, PHYSICAL_RSP, CanFrame

_BAUDS = (115200, 38400, 230400, 9600)
_SKIP_LINES = {
    "OK",
    "SEARCHING...",
    "STOPPED",
    "BUS INIT: OK",
    "ELM327",
}


def _hex_bytes(text: str) -> bytes:
    cleaned = "".join(c for c in text if c in "0123456789abcdefABCDEF")
    if len(cleaned) < 2:
        return b""
    if len(cleaned) % 2:
        cleaned = cleaned[:-1]
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return b""


def _is_11bit_id(token: str) -> bool:
    return len(token) == 3 and all(c in "0123456789abcdefABCDEF" for c in token)


def _skip_line(line: str) -> bool:
    if not line or line.startswith(">"):
        return True
    up = line.upper()
    if up in _SKIP_LINES or up.startswith("ELM") or up.startswith("STN") or up.startswith("ATI"):
        return True
    if any(tag in up for tag in ("ERROR", "UNABLE", "NO DATA", "STOPPED")):
        return True
    if up.startswith("BUS INIT") and "OK" not in up:
        return True
    return False


def _as_isotp_frames(payload: bytes) -> list[bytes]:
    if not payload:
        return []
    if len(payload) <= 7:
        return [(bytes([len(payload)]) + payload).ljust(8, b"\x00")]
    frames = [ (bytes([0x10, len(payload) & 0xFF]) + payload[:6]).ljust(8, b"\x00") ]
    rest = payload[6:]
    sn = 1
    while rest:
        chunk = rest[:7]
        rest = rest[7:]
        frames.append((bytes([0x20 | (sn & 0x0F)]) + chunk).ljust(8, b"\x00"))
        sn += 1
    return frames


def parse_elm_frames(blob: str) -> list[tuple[int, bytes]]:
    """[(arbitration_id, 8-byte CAN payload), ...] from ELM text."""
    line_frames: list[tuple[int, bytes]] = []
    saw_header = False
    leftover: list[bytes] = []
    for raw_line in blob.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if _skip_line(line):
            continue
        tokens = [t for t in line.replace(",", " ").split() if t]
        if not tokens:
            continue
        if _is_11bit_id(tokens[0]):
            arb = int(tokens[0], 16)
            if 0x7E0 <= arb <= 0x7EF or arb == 0x7DF:
                saw_header = True
                data = _hex_bytes("".join(tokens[1:]))
                if len(data) >= 2:
                    line_frames.append((arb if arb >= PHYSICAL_RSP else PHYSICAL_RSP, data[:8].ljust(8, b"\x00")))
                continue
        data = _hex_bytes("".join(tokens))
        if data:
            leftover.append(data)
    if saw_header and line_frames:
        return line_frames
    payload = b"".join(leftover)
    if len(payload) > 7:
        return [(PHYSICAL_RSP, fr) for fr in _as_isotp_frames(payload)]
    if len(payload) >= 2:
        return [(PHYSICAL_RSP, payload[:8].ljust(8, b"\x00"))]
    return []


def parse_elm_reply(blob: str) -> list[bytes]:
    """Turn ELM text into ISO-TP-ish 8-byte CAN payloads (7E8 style)."""
    return [data for _arb, data in parse_elm_frames(blob)]


def parse_elm_payload(blob: str) -> bytes:
    """Full decoded payload (VIN/CAL) without 8-byte chopping."""
    frames = parse_elm_frames(blob)
    if not frames:
        return b""
    # Prefer assembled ISO-TP if we wrapped a long line; otherwise strip PCI.
    chunks: list[bytes] = []
    expected = 0
    assembled = bytearray()
    for _arb, data in frames:
        if len(data) < 2:
            continue
        kind = data[0] >> 4
        if kind == 1:
            expected = ((data[0] & 0x0F) << 8) | data[1]
            assembled = bytearray(data[2:8])
            continue
        if kind == 2 and assembled:
            assembled.extend(data[1:8])
            continue
        if kind == 0 and (data[0] & 0x0F) <= 7:
            n = data[0] & 0x0F
            chunks.append(data[1 : 1 + n])
            continue
        chunks.append(bytes(b for b in data if b))
    if assembled:
        return bytes(assembled[:expected] if expected else assembled)
    return b"".join(chunks)


class Elm327Transport:
    name = "elm327"

    def __init__(self, port: str, bitrate: int = 500_000, baud: int | None = None):
        self.port = port
        self.bitrate = bitrate
        self.baud = baud
        self._ser = None
        self._q: list[CanFrame] = []
        self._hdr: int | None = None

    def open(self) -> None:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("pyserial is required for ELM327 / OBDLink adapters") from exc
        last = None
        bauds = (self.baud,) if self.baud else _BAUDS
        for baud in bauds:
            ser = None
            try:
                ser = serial.Serial(
                    self.port,
                    baudrate=baud,
                    timeout=0.35,
                    write_timeout=1.0,
                    rtscts=False,
                    dsrdtr=False,
                )
                time.sleep(0.2)
                try:
                    ser.dtr = True
                    ser.rts = True
                except Exception:
                    pass
                ser.reset_input_buffer()
                if self._init(ser):
                    self._ser = ser
                    self.baud = baud
                    return
                ser.close()
            except Exception as exc:
                last = exc
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
        raise RuntimeError(f"No ELM327/STN on {self.port} ({last})")

    def _write(self, ser, cmd: str, *, wait: float = 1.6) -> str:
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        ser.write((cmd.strip() + "\r").encode("ascii", errors="ignore"))
        ser.flush()
        end = time.time() + wait
        chunks: list[bytes] = []
        saw_prompt = False
        while time.time() < end:
            n = ser.in_waiting
            if n:
                piece = ser.read(n)
                chunks.append(piece)
                if b">" in piece:
                    saw_prompt = True
                    # Cheap clones keep streaming "SEARCHING..." then data.
                    extra = time.time() + 0.08
                    while time.time() < extra:
                        more = ser.in_waiting
                        if more:
                            chunks.append(ser.read(more))
                            extra = time.time() + 0.05
                        else:
                            break
                    break
            else:
                time.sleep(0.015)
        blob = b"".join(chunks).decode("ascii", errors="replace")
        if not saw_prompt and "SEARCHING" in blob.upper():
            # Give the bus more time on first protocol detect.
            more = self._drain(ser, 2.4)
            blob += more
        return blob

    def _drain(self, ser, wait: float) -> str:
        end = time.time() + wait
        chunks: list[bytes] = []
        while time.time() < end:
            n = ser.in_waiting
            if n:
                chunks.append(ser.read(n))
                if b">" in chunks[-1]:
                    break
            else:
                time.sleep(0.02)
        return b"".join(chunks).decode("ascii", errors="replace")

    def _init(self, ser) -> bool:
        hello = self._write(ser, "ATZ", wait=2.8)
        blob = hello.upper()
        looks = any(tag in blob for tag in ("ELM", "STN", "OBD", "OK", "ATI", "V1.", "ELM327"))
        if not looks and ">" not in hello and "ATZ" not in blob:
            hello = self._write(ser, "ATZ", wait=2.8)
            blob = hello.upper()
            if ">" not in hello and "ATZ" not in blob:
                return False
        # Quiet, headers on (so we can see 7E8 vs 7E9), long messages for VIN.
        for cmd, wait in (
            ("ATE0", 0.7),
            ("ATL0", 0.5),
            ("ATS0", 0.5),
            ("ATH1", 0.5),
            ("ATAL", 0.5),
            ("ATSP0", 1.0),
        ):
            self._write(ser, cmd, wait=wait)
        if self.bitrate >= 400_000:
            self._write(ser, "ATSP6", wait=1.2)  # ISO 15765-4, 11-bit 500 k
        elif self.bitrate >= 200_000:
            self._write(ser, "ATSP8", wait=1.2)  # 11-bit 250 k
        self._write(ser, "ATST64", wait=0.5)
        self._hdr = FUNCTIONAL_ID
        return True

    def prepare_obd(self, dest: int, rxid: int | None = None) -> None:
        if self._ser is None:
            return
        hdr = FUNCTIONAL_ID if dest == FUNCTIONAL_ID else int(dest)
        if hdr == self._hdr:
            return
        self._write(self._ser, f"ATSH{hdr:03X}", wait=0.4)
        if rxid is not None:
            # Filter is optional — cheap clones often ignore ATCRA.
            try:
                self._write(self._ser, f"ATCRA{int(rxid):03X}", wait=0.3)
            except Exception:
                pass
        self._hdr = hdr

    def obd_transact(self, payload: bytes, timeout: float = 3.0) -> bytes:
        """Mode+PID hex → assembled payload. Used for VIN/CAL on metal ELMs."""
        if self._ser is None:
            return b""
        reply = self._write(self._ser, bytes(payload).hex().upper(), wait=max(1.2, float(timeout)))
        raw = parse_elm_payload(reply)
        if raw:
            return raw
        # Fall back to concatenated data bytes if PCI wrap hid the SID.
        frames = parse_elm_reply(reply)
        return b"".join(bytes(fr[1:8]) for fr in frames if fr)

    def close(self) -> None:
        ser = self._ser
        self._ser = None
        self._q.clear()
        self._hdr = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def send(self, arbitration_id: int, data: bytes) -> None:
        if self._ser is None:
            raise RuntimeError("adapter not connected")
        payload = bytes(data)
        if len(payload) < 2:
            return
        n = payload[0]
        body = payload[1 : 1 + n] if n < 8 else payload[1:]
        # ELM wants mode+pid hex without PCI
        hex_cmd = body.hex().upper()
        self.prepare_obd(int(arbitration_id), None)
        wait = 3.2 if hex_cmd in {"0100", "0101"} else 1.8
        reply = self._write(self._ser, hex_cmd, wait=wait)
        now = time.time()
        for arb, frame in parse_elm_frames(reply):
            self._q.append(CanFrame(arb, frame, now))

    def recv(self, timeout: float) -> CanFrame | None:
        if self._q:
            return self._q.pop(0)
        if timeout > 0:
            time.sleep(min(timeout, 0.02))
        return None
