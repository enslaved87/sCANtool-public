"""CAN adapters — Kvaser (live) and Demo (offline UI / tests)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

FUNCTIONAL_ID = 0x7DF
PHYSICAL_REQ = 0x7E0
PHYSICAL_RSP = 0x7E8


def _isotp_info(pid: int, text: bytes) -> list[bytes]:
    """Mode 09 multi-frame: 49 <pid> 01 + payload."""
    payload = bytes([0x49, pid, 0x01]) + text
    first = bytes([0x10, len(payload)]) + payload[:6]
    frames = [first.ljust(8, b"\x00")]
    rest = payload[6:]
    seq = 1
    while rest:
        chunk = rest[:7]
        rest = rest[7:]
        frames.append(bytes([0x20 | (seq & 0x0F)]) + chunk.ljust(7, b"\x00"))
        seq += 1
    return frames


@dataclass(frozen=True)
class CanFrame:
    arbitration_id: int
    data: bytes
    timestamp: float = 0.0


class Transport(Protocol):
    name: str

    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, arbitration_id: int, data: bytes) -> None: ...
    def recv(self, timeout: float) -> CanFrame | None: ...


def _serial_ports() -> list[tuple[str, str]]:
    try:
        from serial.tools import list_ports

        return [(p.device, p.description or p.device) for p in list_ports.comports()]
    except Exception:
        return []


def _detect_interface(iface: str) -> list[dict]:
    import contextlib
    import os

    try:
        import can
    except Exception:
        return []
    try:
        with open(os.devnull, "w") as devnull, contextlib.redirect_stderr(devnull):
            configs = can.detect_available_configs(interfaces=[iface])
    except Exception:
        return []
    out: list[dict] = []
    for c in configs or []:
        ch = c.get("channel", 0)
        name = str(c.get("device_name") or c.get("name") or iface)
        virtual = "virtual" in str(name).lower() or iface == "virtual"
        out.append(
            {
                "kind": iface,
                "interface": iface,
                "channel": ch,
                "label": f"{name} · {iface} {ch}",
                "virtual": virtual,
                "raw_can": True,
                "obd": True,
            }
        )
    return out


def list_adapters() -> list[dict]:
    """Discover every adapter we can see. Demo is always first."""
    from .catalog import RAW_CAN_INTERFACES

    found: list[dict] = [
        {
            "kind": "demo",
            "interface": "demo",
            "channel": 0,
            "label": "Demo (no adapter)",
            "virtual": True,
            "raw_can": False,
            "obd": True,
        }
    ]
    seen: set[tuple] = set()
    for iface, _title in RAW_CAN_INTERFACES:
        if iface in ("slcan",):
            continue  # listed from COM ports so the user picks the right mode
        for spec in _detect_interface(iface):
            key = (spec["interface"], str(spec["channel"]))
            if key in seen:
                continue
            seen.add(key)
            found.append(spec)
    for port, desc in _serial_ports():
        found.append(
            {
                "kind": "elm327",
                "interface": "elm327",
                "channel": port,
                "label": f"ELM327 / OBDLink · {port} ({desc}) — scan & log",
                "virtual": False,
                "raw_can": False,
                "obd": True,
            }
        )
        found.append(
            {
                "kind": "slcan",
                "interface": "slcan",
                "channel": port,
                "label": f"SLCAN / CANable · {port} ({desc}) — raw CAN + E92",
                "virtual": False,
                "raw_can": True,
                "obd": True,
            }
        )
    return found


def open_transport(spec: dict, bitrate: int = 500_000):
    """Build a Transport from a list_adapters() spec."""
    kind = str(spec.get("kind") or "demo")
    if kind == "demo":
        return DemoTransport()
    if kind == "elm327":
        from .elm327 import Elm327Transport

        return Elm327Transport(port=str(spec.get("channel") or ""), bitrate=bitrate)
    return PythonCanTransport(
        interface=str(spec.get("interface") or kind),
        channel=spec.get("channel", 0),
        bitrate=bitrate,
        extra=dict(spec.get("bus_kwargs") or {}),
    )


def open_raw_bus(spec: dict, bitrate: int = 500_000):
    """python-can Bus for ISO-TP / E92 read. Raises if the spec is OBD-only."""
    if not spec.get("raw_can"):
        raise RuntimeError(
            "This adapter is OBD-only (ELM327/OBDLink). "
            "E92 full-read needs raw CAN: Kvaser, PEAK, Vector, CANable SLCAN/gs_usb, "
            "SocketCAN, USB2CAN, IXXAT, J2534, …"
        )
    import can

    kwargs = {
        "interface": str(spec.get("interface") or spec.get("kind")),
        "channel": spec.get("channel", 0),
        "bitrate": int(bitrate),
        "receive_own_messages": False,
    }
    kwargs.update(spec.get("bus_kwargs") or {})
    return can.Bus(**kwargs)


class PythonCanTransport:
    """Any python-can interface (Kvaser, PCAN, Vector, SLCAN, gs_usb, …)."""

    def __init__(self, interface: str, channel=0, bitrate: int = 500_000, extra: dict | None = None):
        self.interface = interface
        self.channel = channel
        self.bitrate = bitrate
        self.extra = extra or {}
        self.name = interface
        self._bus = None

    def open(self) -> None:
        import can

        kwargs = {
            "interface": self.interface,
            "channel": self.channel,
            "bitrate": self.bitrate,
            "receive_own_messages": False,
        }
        kwargs.update(self.extra)
        self._bus = can.Bus(**kwargs)

    def close(self) -> None:
        bus = self._bus
        self._bus = None
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass

    def send(self, arbitration_id: int, data: bytes) -> None:
        if self._bus is None:
            raise RuntimeError("adapter not connected")
        import can

        payload = bytes(data[:8]).ljust(8, b"\x00")
        self._bus.send(
            can.Message(
                arbitration_id=int(arbitration_id),
                data=payload,
                is_extended_id=False,
            )
        )

    def recv(self, timeout: float) -> CanFrame | None:
        if self._bus is None:
            return None
        msg = self._bus.recv(timeout=timeout)
        if msg is None:
            return None
        if getattr(msg, "is_error_frame", False):
            return None
        return CanFrame(
            arbitration_id=int(msg.arbitration_id),
            data=bytes(msg.data),
            timestamp=float(getattr(msg, "timestamp", 0.0) or time.time()),
        )


class KvaserTransport(PythonCanTransport):
    def __init__(self, channel: int = 0, bitrate: int = 500_000):
        super().__init__("kvaser", channel=channel, bitrate=bitrate)


class DemoTransport:
    """Deterministic OBD-II responder for UI click-through and tests."""

    name = "demo"
    VIN = "1G0TEST0000000001"
    CAL_PRIMARY = "10000001"
    CAL_SECONDARY = "10000002"

    def __init__(self, **_kwargs):
        self._q: list[CanFrame] = []
        self._t0 = time.time()
        self._open = False

    def open(self) -> None:
        self._open = True
        self._q.clear()
        self._t0 = time.time()

    def close(self) -> None:
        self._open = False
        self._q.clear()

    def send(self, arbitration_id: int, data: bytes) -> None:
        if not self._open:
            raise RuntimeError("adapter not connected")
        payload = bytes(data)
        if len(payload) < 2:
            return
        length = payload[0]
        mode = payload[1] if length >= 1 else 0
        pid = payload[2] if length >= 2 and len(payload) > 2 else 0
        now = time.time()
        dest = int(arbitration_id)
        if dest == 0x7E1:
            for blob in self._tcm_reply(mode, pid):
                self._q.append(CanFrame(0x7E9, blob, now))
            return
        if dest == FUNCTIONAL_ID:
            for blob in self._reply(mode, pid):
                self._q.append(CanFrame(PHYSICAL_RSP, blob, now))
            for blob in self._tcm_reply(mode, pid):
                self._q.append(CanFrame(0x7E9, blob, now))
            return
        for blob in self._reply(mode, pid):
            self._q.append(CanFrame(PHYSICAL_RSP, blob, now))

    def recv(self, timeout: float) -> CanFrame | None:
        if self._q:
            return self._q.pop(0)
        if timeout > 0:
            time.sleep(min(timeout, 0.01))
        return None

    def _rpm(self) -> int:
        elapsed = time.time() - self._t0
        return 800 + int(200 * (0.5 + 0.5 * (elapsed % 4) / 4))

    def _reply(self, mode: int, pid: int) -> list[bytes]:
        if mode == 0x01 and pid == 0x00:
            # claim 01–20
            return [bytes([0x06, 0x41, 0x00, 0x98, 0x3B, 0x80, 0x13, 0x00])]
        if mode == 0x01 and pid == 0x01:
            # MIL on, 2 DTCs; misfire/fuel/components + several spark tests
            return [bytes([0x06, 0x41, 0x01, 0x82, 0x07, 0xE5, 0x21, 0x00])]
        if mode == 0x01 and pid == 0x20:
            return [bytes([0x06, 0x41, 0x20, 0x80, 0x00, 0x00, 0x01, 0x00])]
        if mode == 0x01 and pid == 0x40:
            return [bytes([0x06, 0x41, 0x40, 0x40, 0x00, 0x00, 0x00, 0x00])]
        if mode == 0x01:
            return [self._live(pid)]
        if mode == 0x02 and pid == 0x02:
            return [bytes([0x04, 0x42, 0x02, 0x03, 0x00, 0x00, 0x00, 0x00])]
        if mode == 0x02:
            live = bytearray(self._live(pid))
            if len(live) > 1 and live[1] == 0x41:
                live[1] = 0x42
            return [bytes(live)]
        if mode == 0x03:
            # P0300 + P0171
            return [bytes([0x06, 0x43, 0x02, 0x03, 0x00, 0x01, 0x71, 0x00])]
        if mode == 0x07:
            return [bytes([0x06, 0x47, 0x01, 0x03, 0x00, 0x00, 0x00, 0x00])]
        if mode == 0x0A:
            return [bytes([0x02, 0x4A, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])]
        if mode == 0x04:
            return [bytes([0x01, 0x44, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])]
        if mode == 0x09 and pid == 0x02:
            return _isotp_info(0x02, self.VIN.encode("ascii"))
        if mode == 0x09 and pid == 0x04:
            # Two 8-digit CALs — never scrape into a fake OS.
            return _isotp_info(0x04, (self.CAL_PRIMARY + self.CAL_SECONDARY).encode("ascii"))
        if mode == 0x09 and pid == 0x0A:
            return _isotp_info(0x0A, b"E92 ECM")
        if mode == 0x09 and pid == 0x00:
            return [bytes([0x06, 0x49, 0x00, 0x55, 0x00, 0x00, 0x00, 0x00])]
        return []

    def _tcm_reply(self, mode: int, pid: int) -> list[bytes]:
        if mode == 0x01 and pid == 0x00:
            return [bytes([0x06, 0x41, 0x00, 0x88, 0x00, 0x00, 0x01, 0x00])]
        if mode == 0x01 and pid == 0x01:
            return [bytes([0x06, 0x41, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00])]
        if mode == 0x01 and pid == 0x42:
            return [bytes([0x04, 0x41, 0x42, 0x33, 0x5C, 0x00, 0x00, 0x00])]
        if mode == 0x03:
            return [bytes([0x02, 0x43, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])]
        if mode == 0x07:
            return [bytes([0x04, 0x47, 0x01, 0x07, 0x00, 0x00, 0x00, 0x00])]
        if mode == 0x0A:
            return [bytes([0x02, 0x4A, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])]
        if mode == 0x04:
            return [bytes([0x01, 0x44, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])]
        if mode == 0x09 and pid == 0x04:
            return _isotp_info(0x04, b"24269102")
        if mode == 0x09 and pid == 0x0A:
            return _isotp_info(0x0A, b"TCM")
        return []

    def _live(self, pid: int) -> bytes:
        rpm = self._rpm()
        table = {
            0x04: (1, (int(35 * 255 / 100),)),
            0x05: (1, (90 + 40,)),
            0x06: (1, (128,)),
            0x07: (1, (132,)),
            0x0B: (1, (38,)),
            0x0C: (2, ((rpm * 4) >> 8, (rpm * 4) & 0xFF)),
            0x0D: (1, (0,)),
            0x0E: (1, (64 + 12,)),
            0x0F: (1, (28 + 40,)),
            0x10: (2, (0x04, 0xB0)),
            0x11: (1, (int(8 * 255 / 100),)),
            0x1C: (1, (1,)),  # OBD-II CARB
            0x1F: (2, (0x00, 0x2A)),
            0x21: (2, (0x00, 0x2C)),  # 44 km with MIL
            0x2F: (1, (int(45 * 255 / 100),)),
            0x30: (1, (3,)),
            0x31: (2, (0x00, 0x78)),  # 120 km since clear
            0x42: (2, (0x33, 0x90)),  # 13.2 V
            0x46: (1, (22 + 40,)),
            0x5C: (1, (95 + 40,)),
        }
        nbytes, vals = table.get(pid, (1, (0,)))
        return bytes([2 + nbytes, 0x41, pid, *vals]).ljust(8, b"\x00")
