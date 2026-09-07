"""Generic OBD-II (SAE J1979) request / decode. Physical 0x7E0 / functional 0x7DF."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .adapter import FUNCTIONAL_ID, PHYSICAL_REQ, PHYSICAL_RSP, CanFrame, Transport
from .isotp_lite import isotp_request

_DATA = Path(__file__).resolve().parents[1] / "data"

_CAL_DIGITS = re.compile(r"\d{7,8}")

_CONTINUOUS = ((0, "Misfire"), (1, "Fuel system"), (2, "Components"))
_SPARK_MONITORS = (
    (0, "Catalyst"),
    (1, "Heated catalyst"),
    (2, "EVAP"),
    (3, "Secondary air"),
    (4, "A/C refrigerant"),
    (5, "O2 sensor"),
    (6, "O2 heater"),
    (7, "EGR/VVT"),
)
_COMPRESSION_MONITORS = (
    (0, "NMHC catalyst"),
    (1, "NOx aftertreatment"),
    (3, "Boost pressure"),
    (5, "Exhaust gas sensor"),
    (6, "PM filter"),
    (7, "EGR/VVT"),
)
_OBD_STANDARDS = {
    1: "OBD-II (CARB)",
    2: "OBD (federal)",
    3: "OBD and OBD-II",
    4: "OBD-I",
    5: "Not OBD compliant",
    6: "EOBD",
    7: "EOBD and OBD-II",
    8: "EOBD and OBD",
    9: "EOBD, OBD and OBD-II",
    10: "JOBD",
    11: "JOBD and OBD-II",
    12: "JOBD and EOBD",
    13: "JOBD, EOBD and OBD-II",
    17: "EMD",
    18: "EMD+",
    20: "HD OBD",
    21: "WWH-OBD",
    34: "OBD, OBD-II, HD OBD",
}

ECU_NAMES = {
    0x7E0: "Engine",
    0x7E1: "Transmission",
    0x7E2: "Module 7E2",
    0x7E3: "Module 7E3",
    0x7E4: "Module 7E4",
    0x7E5: "Module 7E5",
    0x7E6: "Module 7E6",
    0x7E7: "Module 7E7",
}


def _load_json(name: str) -> dict:
    path = _DATA / name
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


_PIDS = _load_json("pids.json")
_DTC_NAMES = _load_json("dtc_names.json")


def pid_catalog() -> dict:
    return _PIDS


def dtc_name(code: str) -> str:
    return str(_DTC_NAMES.get(code) or "")


def ecu_label(txid: int) -> str:
    return ECU_NAMES.get(int(txid), f"0x{int(txid):03X}")


def split_cal_ids(source: bytes | str) -> list[str]:
    """Split Mode 09 PID 04 into individual CAL IDs.

    Adjacent 8-digit CAL IDs concatenated or sliced from the middle are not
    an OS number. Never do that.
    """
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
        if len(raw) >= 2 and raw[0] == 0x49 and raw[1] == 0x04:
            rest = raw[2:]
            if rest and rest[0] <= 0x10:
                rest = rest[1:]
            raw = rest
        text = "".join(chr(b) for b in raw if 32 <= b < 127)
    else:
        text = str(source or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    found = _CAL_DIGITS.findall(digits)
    out: list[str] = []
    for item in found:
        if item not in out:
            out.append(item)
    return out


def decode_pid(pid: int, payload: bytes) -> float | None:
    """payload is the data bytes after 41 <pid>."""
    spec = (_PIDS.get("pids") or {}).get(f"{pid:02X}")
    if not spec:
        if not payload:
            return None
        if len(payload) == 1:
            return float(payload[0])
        return float((payload[0] << 8) | payload[1])
    n = int(spec.get("bytes") or 1)
    raw = payload[:n]
    if len(raw) < n:
        return None
    value = raw[0] if n == 1 else (raw[0] << 8) | raw[1]
    scale = float(spec.get("scale") or 1)
    div = float(spec.get("div") or 1)
    offset = float(spec.get("offset") or 0)
    return value * scale / div + offset


def format_pid(pid: int, value: float | None) -> str:
    spec = (_PIDS.get("pids") or {}).get(f"{pid:02X}") or {}
    name = spec.get("name") or f"PID {pid:02X}"
    units = spec.get("units") or ""
    if value is None:
        return f"{name}: —"
    if pid == 0x1C:
        label = _OBD_STANDARDS.get(int(value), f"standard {int(value)}")
        return f"{name}: {label}"
    if pid == 0x1F:
        secs = int(value)
        return f"{name}: {secs // 60} min {secs % 60} s"
    if abs(value) >= 100:
        num = f"{value:.0f}"
    elif abs(value) >= 10:
        num = f"{value:.1f}"
    else:
        num = f"{value:.2f}"
    return f"{name}: {num} {units}".strip()


def decode_dtc_pair(a: int, b: int) -> str:
    first = (a >> 6) & 0x03
    prefix = "PCBU"[first]
    d1 = (a >> 4) & 0x03
    d2 = a & 0x0F
    return f"{prefix}{d1}{d2:X}{b:02X}"


def decode_dtcs(frames: list[bytes], *, pos_sid: int = 0x43) -> list[str]:
    """Parse single-frame and simple consecutive-frame mode 03/07/0A replies."""
    codes: list[str] = []
    pending: bytes = b""
    for data in frames:
        if not data:
            continue
        # ISO-TP first frame: 1X LL SID ...
        if data[0] >> 4 == 1 and len(data) >= 3:
            pending = data[2:]
            continue
        if data[0] >> 4 == 2:
            pending += data[1:]
            continue
        pci = data[0]
        body = data[1 : 1 + pci] if pci < 8 else data[1:]
        pending = body if not pending else pending
        blob = pending
        pending = b""
        if not blob or blob[0] != pos_sid:
            continue
        pairs = blob[2:] if len(blob) > 2 and blob[1] <= 0x20 else blob[1:]
        for i in range(0, len(pairs) - 1, 2):
            a, b = pairs[i], pairs[i + 1]
            if a == 0 and b == 0:
                continue
            code = decode_dtc_pair(a, b)
            if code not in codes:
                codes.append(code)
    return codes


def decode_monitor_status(payload: bytes) -> dict:
    """Mode 01 PID 01 — MIL, stored-count, readiness monitors."""
    if len(payload) < 4:
        return {
            "mil": False,
            "dtc_count": 0,
            "available": [],
            "ready": [],
            "not_ready": [],
            "raw": "",
        }
    a, b, c, d = payload[0], payload[1], payload[2], payload[3]
    available: list[str] = []
    ready: list[str] = []
    not_ready: list[str] = []
    rows: list[dict] = []
    compression = bool(b & 0x08)
    for bit, name in _CONTINUOUS:
        avail = bool(b & (1 << bit))
        incomplete = bool(b & (1 << (bit + 4)))
        if avail:
            available.append(name)
            (not_ready if incomplete else ready).append(name)
        rows.append(
            {
                "name": name,
                "group": "continuous",
                "available": avail,
                "complete": bool(avail and not incomplete),
                "status": (
                    "not supported" if not avail else ("incomplete" if incomplete else "complete")
                ),
            }
        )
    extras = _COMPRESSION_MONITORS if compression else _SPARK_MONITORS
    for bit, name in extras:
        avail = bool(c & (1 << bit))
        incomplete = bool(d & (1 << bit))
        if avail:
            available.append(name)
            (not_ready if incomplete else ready).append(name)
        rows.append(
            {
                "name": name,
                "group": "once-per-trip",
                "available": avail,
                "complete": bool(avail and not incomplete),
                "status": (
                    "not supported" if not avail else ("incomplete" if incomplete else "complete")
                ),
            }
        )
    return {
        "mil": bool(a & 0x80),
        "dtc_count": int(a & 0x7F),
        "ignition": "compression" if compression else "spark",
        "available": available,
        "ready": ready,
        "not_ready": not_ready,
        "rows": rows,
        "raw": f"{a:02X} {b:02X} {c:02X} {d:02X}",
    }


def decode_vin_payload(frames: list[bytes]) -> str:
    """Assemble mode 09 PID 02 VIN from one or more CAN frames."""
    chunks: list[bytes] = []
    for data in frames:
        if not data:
            continue
        if data[0] >> 4 == 1:
            chunks.append(data[2:])
        elif data[0] >> 4 == 2:
            chunks.append(data[1:])
        elif len(data) >= 5 and data[1] == 0x49 and data[2] == 0x02:
            chunks.append(data[3:])
        elif len(data) >= 4 and data[0] == 0x49:
            chunks.append(data[1:])
    blob = b"".join(chunks)
    # 49 02 01 + VIN
    if len(blob) >= 3 and blob[0] == 0x49 and blob[1] == 0x02:
        blob = blob[3:] if blob[2] <= 0x10 else blob[2:]
    text = "".join(chr(b) for b in blob if 32 <= b < 127)
    # VIN is 17 alphanumeric
    cleaned = "".join(c for c in text if c.isalnum())
    return cleaned[:17]


def decode_ascii_info(frames: list[bytes], *, pid: int) -> str:
    chunks: list[bytes] = []
    for data in frames:
        if not data:
            continue
        if data[0] >> 4 == 1:
            chunks.append(data[2:])
        elif data[0] >> 4 == 2:
            chunks.append(data[1:])
        elif len(data) >= 4 and data[1] == 0x49:
            chunks.append(data[2:])
    blob = b"".join(chunks)
    if len(blob) >= 3 and blob[0] == 0x49 and blob[1] == pid:
        blob = blob[3:] if blob[2] <= 0x10 else blob[2:]
    return "".join(chr(b) for b in blob if 32 <= b < 127).strip()


def _ascii_from_uds(frames: list[bytes]) -> str:
    blob = b""
    for data in frames:
        if not data:
            continue
        if data[0] >> 4 == 1:
            blob += data[2:]
        elif data[0] >> 4 == 2:
            blob += data[1:]
        else:
            pci = data[0]
            body = data[1 : 1 + pci] if pci < 8 else data[1:]
            blob += body
    # strip SID+sub
    if len(blob) >= 2 and blob[0] in (0x5A, 0x62):
        blob = blob[2:]
    return "".join(chr(b) for b in blob if 32 <= b < 127)


class ObdClient:
    def __init__(self, transport: Transport, txid: int = PHYSICAL_REQ, rxid: int = PHYSICAL_RSP):
        self.t = transport
        self.txid = int(txid)
        self.rxid = int(rxid)

    def request_frames(
        self,
        mode: int,
        pid: int | None = None,
        *,
        timeout: float = 0.6,
        functional: bool = True,
    ) -> list[CanFrame]:
        if pid is None:
            body = bytes([0x01, mode])
        else:
            body = bytes([0x02, mode, pid])
        frame = body.ljust(8, b"\x00")
        dest = FUNCTIONAL_ID if functional else self.txid
        prepare = getattr(self.t, "prepare_obd", None)
        if callable(prepare):
            try:
                prepare(dest, None if functional else self.rxid)
            except Exception:
                pass
        self.t.send(dest, frame)
        replies: list[CanFrame] = []
        end = time.time() + timeout
        last = 0.0
        quiet = 0.07
        while time.time() < end:
            msg = self.t.recv(min(0.06, max(0.0, end - time.time())))
            if msg is None:
                if replies and (time.time() - last) > quiet:
                    break
                continue
            arb = msg.arbitration_id
            if not (PHYSICAL_RSP <= arb <= 0x7EF):
                continue
            if not functional and arb != self.rxid:
                continue
            replies.append(msg)
            last = time.time()
        return replies

    def request(
        self,
        mode: int,
        pid: int | None = None,
        *,
        timeout: float = 0.6,
        functional: bool = True,
    ) -> list[bytes]:
        return [fr.data for fr in self.request_frames(mode, pid, timeout=timeout, functional=functional)]

    def read_pid(
        self,
        pid: int,
        *,
        mode: int = 0x01,
        functional: bool | None = None,
        timeout: float = 0.4,
    ) -> float | None:
        if functional is None:
            functional = mode == 0x01 and self.txid == PHYSICAL_REQ
        frames = self.request(mode, pid, timeout=timeout, functional=functional)
        pos = 0x40 | mode
        for data in frames:
            if len(data) >= 4 and data[1] == pos and data[2] == pid:
                return decode_pid(pid, data[3:])
            if len(data) >= 3 and data[0] == pos and data[1] == pid:
                return decode_pid(pid, data[2:])
        return None

    def read_monitor_status(self, *, functional: bool | None = None) -> dict:
        if functional is None:
            functional = self.txid == PHYSICAL_REQ
        frames = self.request_frames(0x01, 0x01, timeout=0.4, functional=functional)
        for fr in frames:
            data = fr.data
            if len(data) >= 7 and data[1] == 0x41 and data[2] == 0x01:
                return decode_monitor_status(data[3:7])
            if len(data) >= 6 and data[0] == 0x41 and data[1] == 0x01:
                return decode_monitor_status(data[2:6])
        return decode_monitor_status(b"")

    def read_freeze_frame(self) -> dict | None:
        frames = self.request(0x02, 0x02, timeout=0.45, functional=False)
        dtc = ""
        for data in frames:
            if len(data) >= 5 and data[1] == 0x42 and data[2] == 0x02:
                dtc = decode_dtc_pair(data[3], data[4])
            elif len(data) >= 4 and data[0] == 0x42 and data[1] == 0x02:
                dtc = decode_dtc_pair(data[2], data[3])
        if not dtc or dtc in {"P0000", "C0000", "B0000", "U0000"}:
            return None
        catalog = pid_catalog().get("pids") or {}
        values: dict[str, dict] = {}
        for hex_pid in ("0C", "0D", "05", "0B", "11", "04", "42"):
            pid = int(hex_pid, 16)
            value = self.read_pid(pid, mode=0x02, functional=False, timeout=0.3)
            values[hex_pid] = {
                "name": (catalog.get(hex_pid) or {}).get("name") or hex_pid,
                "value": value,
                "text": format_pid(pid, value),
            }
        return {"dtc": dtc, "name": dtc_name(dtc), "values": values}

    def discover_modules(self) -> list[dict]:
        found: dict[int, dict] = {}
        for fr in self.request_frames(0x01, 0x00, timeout=0.4, functional=True):
            if PHYSICAL_RSP <= fr.arbitration_id <= 0x7EF:
                tx = fr.arbitration_id - 8
                found[tx] = {"txid": tx, "rxid": fr.arbitration_id, "label": ecu_label(tx)}
        if not found:
            for tx in (0x7E0, 0x7E1):
                peer = ObdClient(self.t, txid=tx, rxid=tx + 8)
                if peer.request_frames(0x01, 0x00, timeout=0.22, functional=False):
                    found[tx] = {"txid": tx, "rxid": tx + 8, "label": ecu_label(tx)}
        if not found:
            found[0x7E0] = {"txid": 0x7E0, "rxid": 0x7E8, "label": ecu_label(0x7E0)}
        return [found[k] for k in sorted(found)]

    def read_dtcs(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for key, mode, pos in (
            ("stored", 0x03, 0x43),
            ("pending", 0x07, 0x47),
            ("permanent", 0x0A, 0x4A),
        ):
            frames = self.request(mode, None, timeout=0.55, functional=self.txid == PHYSICAL_REQ)
            codes = decode_dtcs(frames, pos_sid=pos)
            out[key] = [{"code": c, "name": dtc_name(c)} for c in codes]
        return out

    def clear_dtcs(self) -> bool:
        frames = self.request(0x04, None, timeout=1.0, functional=True)
        return any(len(d) >= 2 and d[1] == 0x44 for d in frames)

    def uds_request(self, payload: bytes, *, timeout: float = 1.2) -> bytes:
        """ISO-TP UDS on this client's physical pair (default 0x7E0 / 0x7E8)."""
        transact = getattr(self.t, "obd_transact", None)
        if callable(transact):
            try:
                raw = transact(payload, timeout=timeout)
                if raw:
                    return raw
            except Exception:
                pass
        try:
            return isotp_request(self.t, payload, txid=self.txid, rxid=self.rxid, timeout=timeout)
        except Exception:
            return b""

    def read_cal_ids(self) -> list[str]:
        raw = self.uds_request(bytes([0x09, 0x04]))
        return split_cal_ids(raw)

    def read_calid(self) -> str:
        return " · ".join(self.read_cal_ids())

    def read_ecu_name(self) -> str:
        raw = self.uds_request(bytes([0x09, 0x0A]))
        if len(raw) >= 3 and raw[0] == 0x49 and raw[1] == 0x0A:
            raw = raw[3:]
            if raw[:1] and raw[0] <= 0x10:
                raw = raw[1:]
        return "".join(chr(b) for b in raw if 32 <= b < 127).strip()

    def read_vin(self) -> str:
        raw = self.uds_request(bytes([0x09, 0x02]))
        if len(raw) >= 3 and raw[0] == 0x49 and raw[1] == 0x02:
            raw = raw[3:]
            if raw and raw[0] <= 0x10:
                raw = raw[1:]
        cleaned = "".join(chr(b) for b in raw if str.isalnum(chr(b)))
        if len(cleaned) >= 17:
            return cleaned[:17]
        # Some controllers answer $1A $90; $22 F190 is often NRC 31.
        return self.read_gm_vin()

    def read_gm_vin(self) -> str:
        for payload in (bytes([0x1A, 0x90]), bytes([0x22, 0xF1, 0x90])):
            raw = self.uds_request(payload)
            if len(raw) >= 2 and raw[0] == 0x7F:
                continue
            if len(raw) >= 2 and raw[0] in (0x5A, 0x62):
                raw = raw[2:]
            cleaned = "".join(chr(b) for b in raw if str.isalnum(chr(b)))
            if len(cleaned) >= 17:
                return cleaned[:17]
        return ""
