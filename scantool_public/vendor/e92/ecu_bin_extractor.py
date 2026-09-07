"""E92 ECM full-flash read over a raw CAN adapter.

Programming session, security access, optional read-kernel upload,
then $23 ReadMemoryByAddress. No third-party source is imported.
"""

from __future__ import annotations

import subprocess
import struct
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Iterable

import can
from isotp import Address, AddressingMode, CanStack

# --- paths (absolute, Windows-friendly) ---

# Optional read kernel and seed-key modules live next to this file.
_VENDOR_DIR = Path(__file__).resolve().parent
VENDOR_ROOT = _VENDOR_DIR
KERNEL_BIN = _VENDOR_DIR / "kernel.bin"

DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "sCANtool Public" / "reads"

# Public: do not lock reads to one vehicle.
EXPECTED_OS_ID = 0
EXPECTED_VIN_PREFIX = ""

TESTER_ID = 0x7E0
ECU_ID = 0x7E8
GATEWAY_BC_ID = 0x101

# Services whose positive response echoes the request sub-function byte
_UDS_SUBFN_ECHO_SIDS = frozenset({0x10, 0x11, 0x27, 0x1A, 0x3E})

KERNEL_LOAD_ADDR = 0x40001000
FLASH_SIZE = 0x400000
READ_BLOCK = 2048
READ_BLOCK_TIMEOUT_S = 15.0  # 2 KB $23 multi-frame reply; vehicle bus needs margin
READ_BLOCK_RETRIES = 3
ALGO_E92A_LATE = 146
ALGO_E92_EARLY = 513

# Shadow-region constants (not part of the 4 MiB dump).
SHADOW_BASE = 0x00FFC000
SHADOW_SIZE = 0x4000
NVPWD_CPU_ADDR = 0x00FFFDD8
NVPWD_SHADOW_OFF = 0x3DD8
NVPWD_LEN = 8
GM_CENSORSHIP_PASSWORD = bytes.fromhex("455055AA455055AA")
DEFAULT_CENSORSHIP_PASSWORD = bytes.fromhex("55AA55AA55AA55AA")


def classify_nvpwd(raw: bytes) -> str:
    if raw == DEFAULT_CENSORSHIP_PASSWORD:
        return "factory_default"
    if raw == GM_CENSORSHIP_PASSWORD:
        return "oem_epu"
    if raw == b"\xFF" * NVPWD_LEN:
        return "erased_ff"
    if raw == b"\x00" * NVPWD_LEN:
        return "zeroed"
    return "custom"


def parse_shadow_censorship(shadow_blob: bytes):
    raw = shadow_blob[NVPWD_SHADOW_OFF : NVPWD_SHADOW_OFF + NVPWD_LEN]
    return type(
        "SC",
        (),
        {
            "raw": raw,
            "label": classify_nvpwd(raw),
            "hex_spaced": " ".join(f"{b:02X}" for b in raw),
        },
    )()

# Vehicle safety limits (MEC / session hygiene)
MAX_UNLOCK_ATTEMPTS = 2  # initial + one kernel re-upload after crash
UNLOCK_COOLDOWN_S = 60.0
READ_STALL_ABORT_S = 120.0  # ~60 blocks @ 2s TP; abort if no progress
MIN_BATTERY_V = 12.0  # block unlock below this (key-on engine-off)
WARN_BATTERY_V = 12.4
BATTERY_CACHE_GRACE_S = 180.0  # bench: reuse recent good $42 if one poll misses

# Last 2 KiB of each 64 KiB at addr >= 0x10000. Metal 2026-09-06: asking
# the SRAM reader to $23 0x2F800 killed the kernel (PARTIAL_4pct stopped
# at 0x2F800). Prefill 0xFF — those bytes exist on a complete dump; this
# reader cannot fetch them. Keep True.
SKIP_BOUNDARY_MASK = 0xF800
SKIP_F800_TAILS = True


class E92Variant(Enum):
    LATE = auto()   # 2017+ E92A, 5-byte seed, algo 146
    EARLY = auto()  # pre-2017, 2-byte seed, algo 513
    UNKNOWN = auto()


LogFn = Callable[[str], None]


def _is_full_vin(vin: str) -> bool:
    """True when probe returned a complete 17-character VIN."""
    v = (vin or "").strip()
    return len(v) >= 17 and v[:17].isalnum()


def _is_valid_vin(vin: str) -> bool:
    """True for a readable 17-character alphanumeric VIN."""
    return _is_full_vin(vin)


def _is_valid_osid(osid: str) -> bool:
    """True when probe returned a usable calibration/OS ID."""
    digits = "".join(c for c in (osid or "") if c.isdigit())
    return len(digits) >= 7


def _decode_vin_from_1a90(raw: bytes) -> str:
    if len(raw) < 19:
        return ""
    return bytes(raw[2:19]).decode("ascii", errors="replace").strip("\x00")[:17]


def _decode_vin_from_22f190(raw: bytes) -> str:
    if len(raw) <= 3:
        return ""
    return bytes(raw[3:20]).decode("ascii", errors="replace").strip("\x00")[:17]


def _is_erased_vin_payload(payload: bytes) -> bool:
    """True when ECM VIN storage is blank (0xFF/0x00 fill — common on junkyard donors)."""
    chunk = payload[:17]
    if len(chunk) < 17:
        return False
    return all(b in (0xFF, 0x00) for b in chunk)


def sanitize_probe_vin(vin: str) -> str:
    """Drop erased/garbage VIN payloads — bench unlock uses OSID instead."""
    v = (vin or "").strip()
    return v[:17] if _is_valid_vin(v) else ""


def probe_ready_for_read(vin: str, osid: str) -> bool:
    """Bench/junkyard: valid OSID alone is enough to unlock and FULL READ."""
    return _is_valid_vin(vin) or _is_valid_osid(osid)


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _parse_cal_ids_09_04(raw: bytes) -> list[str]:
    """Split $09 $04 into individual 7–8 digit CAL IDs.

    Do **not** scrape a mid-string 16-byte window. Adjacent 8-digit CAL IDs
    concatenated or sliced from the middle are not an OS number.
    """
    if not raw:
        return []
    blob = raw
    if len(blob) >= 2 and blob[0] == 0x49 and blob[1] == 0x04:
        blob = blob[2:]
        if blob and blob[0] <= 0x10:
            blob = blob[1:]
    digits = "".join(chr(b) for b in blob if 0x30 <= b <= 0x39)
    out: list[str] = []
    i = 0
    while i < len(digits):
        # Prefer 8-digit calibration IDs, accept a trailing 7.
        take = 8 if i + 8 <= len(digits) else (7 if i + 7 <= len(digits) else 0)
        if take < 7:
            break
        piece = digits[i : i + take]
        if piece not in out:
            out.append(piece)
        i += take
    return out


def _parse_osid_09_04(raw: bytes) -> str:
    """First CAL id only — never a concatenated scrape."""
    ids = _parse_cal_ids_09_04(raw)
    return ids[0] if ids else ""


def _is_sim_bus(bus: can.BusABC | None) -> bool:
    return type(bus).__name__ == "_FilteredRecvBus"


@dataclass
class PreflightResult:
    ok: bool
    messages: list[str] = field(default_factory=list)


@dataclass
class BusDiagnoseSnapshot:
    """Captured at end of ``diagnose_bus`` for session meta export."""

    verdict: str = ""
    passive_data_frames: int = 0
    passive_top_ids: dict[str, int] = field(default_factory=dict)
    rpm_responders: list[str] = field(default_factory=list)
    ecm_ping: str = ""
    quick_vin: str = ""
    can_error_frames: int = 0


@dataclass
class ReadProgress:
    """Last flash-read position — useful when a FULL READ aborts mid-dump."""

    bytes_read: int = 0
    total_bytes: int = FLASH_SIZE
    last_addr: int = 0
    skipped_blocks: int = 0
    pct: int = 0
    pass_number: int = 1
    pass_total: int = 1
    phase: str = "read"  # read | verify


@dataclass
class ReadResult:
    ok: bool
    data: bytes = b""
    path: Path | None = None
    checksum: int = 0
    os_id: str = ""
    vin: str = ""
    skipped_blocks: int = 0
    error: str = ""
    partial_path: Path | None = None
    # Optional extras (shadow / region reads)
    region: str = "flash"  # flash | shadow
    nvpwd_hex: str = ""
    nvpwd_class: str = ""
    sidecar_path: Path | None = None


@dataclass
class KvaserChannelInfo:
    channel: int
    device_name: str
    serial: int = 0
    is_virtual: bool = False

    @property
    def label(self) -> str:
        tag = "VIRTUAL — not vehicle OBD" if self.is_virtual else "REAL"
        return f"ch{self.channel}: {self.device_name} ({tag})"


@dataclass
class KvaserHardwareReport:
    """Windows + CANlib view of attached Kvaser hardware."""

    real_channels: list[KvaserChannelInfo] = field(default_factory=list)
    virtual_channels: list[KvaserChannelInfo] = field(default_factory=list)
    phantom_leaf_count: int = 0
    leaf_usb_present: bool = False
    messages: list[str] = field(default_factory=list)

    @property
    def has_real_leaf(self) -> bool:
        return bool(self.real_channels)

    @property
    def ok_for_vehicle(self) -> bool:
        return self.has_real_leaf and not (
            self.phantom_leaf_count > 0 and not self.leaf_usb_present
        )


def diagnose_kvaser_hardware() -> KvaserHardwareReport:
    """Detect real Leaf vs Virtual-only / phantom (unplugged) Kvaser entries."""
    report = KvaserHardwareReport()
    for info in list_kvaser_channel_details():
        if info.is_virtual:
            report.virtual_channels.append(info)
        else:
            report.real_channels.append(info)

    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-PnpDevice | Where-Object { $_.InstanceId -like '*VID_0BFD*' } "
                "| Select-Object FriendlyName, Status, Problem, Present "
                "| ConvertTo-Json -Compress",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
        ).strip()
        if out:
            import json

            raw = json.loads(out)
            rows = raw if isinstance(raw, list) else [raw]
            for row in rows:
                name = str(row.get("FriendlyName") or "")
                problem = str(row.get("Problem") or "")
                present = row.get("Present")
                if "Leaf" in name and problem == "CM_PROB_PHANTOM":
                    report.phantom_leaf_count += 1
                if "Leaf" in name and present is True:
                    report.leaf_usb_present = True
    except Exception:
        pass

    if report.has_real_leaf:
        report.messages.append(
            f"Adapter hardware OK — {len(report.real_channels)} real channel(s) visible."
        )
        return report

    if report.virtual_channels:
        report.messages.append(
            "CANlib sees only Virtual CAN Driver — not a physical Leaf on USB."
        )
    else:
        report.messages.append("No CAN channels reported.")

    if report.phantom_leaf_count:
        report.messages.append(
            f"Windows has {report.phantom_leaf_count} ghost adapter entries "
            f"(CM_PROB_PHANTOM, Present=False) — the adapter is unplugged or the USB link failed."
        )
    elif not report.leaf_usb_present:
        report.messages.append(
            "No USB-CAN adapter is present — check cable, OBD power, and USB port."
        )

    report.messages.append(
        "Fix: unplug Leaf USB → wait 10s → direct laptop USB port (not hub) → replug → "
        "Check Device Manager for the adapter (Status=OK). "
        "If ghost devices remain, uninstall hidden entries and replug. "
        "Try another USB port if the adapter does not enumerate."
    )
    return report


@dataclass
class BusContentionReport:
    """Processes that may hold or fight over the CAN adapter."""

    python_scan_jobs: list[str] = field(default_factory=list)
    extra_jobs: list[str] = field(default_factory=list)

    @property
    def risky(self) -> bool:
        return len(self.python_scan_jobs) > 1 or bool(self.extra_jobs)

    @property
    def note(self) -> str:
        if len(self.python_scan_jobs) > 1:
            return (
                "Multiple Python PIDs listed — often Windows launcher parent+child "
                "with only one tool window. Risky only if two separate windows are open."
            )
        return ""


def check_bus_contention(*, exclude_pid: int | None = None) -> BusContentionReport:
    """Detect other apps that commonly contend for the CAN adapter."""
    report = BusContentionReport()
    seen_scan_pids: set[int] = set()
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process "
                "| Where-Object { $_.Name -match 'python' } "
                "| Select-Object ProcessId, Name, CommandLine "
                "| ForEach-Object { \"$($_.ProcessId)|$($_.Name)|$($_.CommandLine)\" }",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln or ln.count("|") < 2:
                continue
            pid_s, _name, cmd = ln.split("|", 2)
            try:
                pid = int(pid_s)
            except ValueError:
                continue
            if exclude_pid is not None and pid == exclude_pid:
                continue
            cmd_l = cmd.lower()
            if any(k in cmd_l for k in ("scantool", "live_probe", "ecu_bin", "ecu_read", "pre_read")):
                if pid in seen_scan_pids:
                    continue
                seen_scan_pids.add(pid)
                report.python_scan_jobs.append(f"pid={pid} {cmd[:140]}")
    except Exception:
        pass
    return report


def list_kvaser_channel_details() -> list[KvaserChannelInfo]:
    try:
        configs = can.detect_available_configs(interfaces=["kvaser"])
    except Exception:
        return []
    out: list[KvaserChannelInfo] = []
    for c in configs:
        name = str(c.get("device_name") or "unknown")
        out.append(
            KvaserChannelInfo(
                channel=int(c.get("channel", 0)),
                device_name=name,
                serial=int(c.get("serial") or 0),
                is_virtual="virtual" in name.lower(),
            )
        )
    return out


def find_vehicle_channel(
    *,
    bitrate: int = 500_000,
    listen_s: float = 2.0,
    log: LogFn | None = None,
) -> tuple[int | None, dict[int, int]]:
    """Sniff each real (non-virtual) CAN channel; return best ch + frame counts."""
    _log = log or (lambda _m: None)
    channels = [c for c in list_kvaser_channel_details() if not c.is_virtual]
    if not channels:
        _log("No real CAN channels found (virtual only?)")
        return None, {}

    best_ch: int | None = None
    counts: dict[int, int] = {}
    for info in channels:
        ch = info.channel
        try:
            bus = open_kvaser_bus(ch, bitrate)
        except Exception as exc:
            _log(f"ch{ch}: open failed ({exc}) — close other apps holding the adapter")
            counts[ch] = -1
            continue
        frames = 0
        end = time.time() + listen_s
        while time.time() < end:
            msg = bus.recv(timeout=0.1)
            if msg is None:
                continue
            if IsoTpUdsClient._is_error_frame(msg):
                continue
            frames += 1
        bus.shutdown()
        counts[ch] = frames
        _log(f"ch{ch} ({info.device_name}): {frames} data frames in {listen_s:.1f}s")
        if frames and (best_ch is None or frames > counts.get(best_ch, 0)):
            best_ch = ch
    return best_ch, counts


def _load_keylib():
    """Local seed-key module only — no external key library."""
    import gm5byte_key as bundled

    class _BundledAdapter:
        ALGO_E92A_LATE = bundled.ALGO_E92A_LATE
        PASSWORD_MAP = {bundled.ALGO_E92A_LATE: bundled.E92A_ALGO_146_BLOB}

        @staticmethod
        def parse_password_blob(blob):
            return bundled.parse_password_blob(blob)

        @staticmethod
        def derive_key_from_algo(algo, seed):
            return bundled.derive_key_from_algo(algo, seed)

    return _BundledAdapter()


def _vin_model_year(vin: str) -> int:
    if len(vin) < 10:
        return 0
    c = vin[9].upper()
    table = "ABCDEFGHJKLMNPRSTVWXY"
    if c in "IOQUZ":
        return 0
    if c.isdigit():
        return 2000 + int(c) if int(c) <= 9 else 0
    try:
        return 2010 + table.index(c)
    except ValueError:
        return 0


def classify_variant(vin: str = "", seed_len: int = 0) -> E92Variant:
    year = _vin_model_year(vin)
    if year >= 2018:
        return E92Variant.LATE
    if year and year <= 2016:
        return E92Variant.EARLY
    if seed_len >= 5:
        return E92Variant.LATE
    if seed_len == 2:
        return E92Variant.EARLY
    # Default late-style when year cannot be read.
    return E92Variant.LATE


class _UdsRxFilterBus(can.BusABC):
    """ISO-TP bus shim: TX to real bus; RX only passes ``rx_id`` (drops broadcast flood)."""

    def __init__(self, bus: can.BusABC, *, rx_id: int, uds: "IsoTpUdsClient"):
        self._bus = bus
        self._rx_id = rx_id
        self._uds = uds
        self.channel_info = getattr(bus, "channel_info", "uds-filter")

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self._bus.send(msg, timeout)

    def recv(self, timeout: float | None = None) -> can.Message | None:
        deadline = time.time() + (timeout if timeout is not None else 0.0)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            msg = self._bus.recv(timeout=min(0.005, remaining))
            if msg is None:
                return None
            if IsoTpUdsClient._is_error_frame(msg):
                self._uds.error_frame_count += 1
                continue
            if msg.arbitration_id == self._rx_id:
                return msg


class IsoTpUdsClient:
    """Blocking ISO-TP client on 0x7E0 / 0x7E8."""

    def __init__(self, bus: can.BusABC, *, tx_id: int = TESTER_ID, rx_id: int = ECU_ID):
        self.bus = bus
        self.tx_id = tx_id
        self.rx_id = rx_id
        addr = Address(AddressingMode.Normal_11bits, txid=tx_id, rxid=rx_id)
        # ISO-TP TX speed (live plant lesson 2026-07-21/23):
        # - This EARLY bench ECU answers multi-frame with FC ``30 00 0A`` → STmin=10 ms.
        # - Honoring 10 ms × ~580 CF ≈ 6–9 s per 4 KiB $36 → ~15–20 min cal plant.
        # - live cal-write capture uses FC STmin 0xF2 (~200 µs) and finishes ~45 s.
        # - ``override_receiver_stmin`` ignores ECU STmin for *our* CF spacing (ISO-TP
        #   still waits for FC; BS=0 still applies). 200 µs matches that capture; measured
        #   ~30 KB/s multi-frame TX without buffer overflow.
        # - stmin/blocksize below apply to FCs *we* send when receiving multi-frame.
        # - TX pump: see ``_wait_tx_complete`` (state-aware; not 20 ms process stalls).
        isotp_params = {
            "stmin": 0,
            "blocksize": 0,
            "tx_data_length": 8,
            "rx_flowcontrol_timeout": 1000,
            "rx_consecutive_frame_timeout": 1000,
            "wftmax": 0,
            "can_fd": False,
            # Force short CF spacing; ECU FC 0x0A would otherwise throttle hard.
            "override_receiver_stmin": 0.0002,
        }
        try:
            self.stack = CanStack(
                _UdsRxFilterBus(bus, rx_id=rx_id, uds=self),
                address=addr,
                params=isotp_params,
            )
        except (TypeError, ValueError):
            # Older/newer isotp may reject some keys — fall back without override.
            try:
                isotp_params.pop("override_receiver_stmin", None)
                self.stack = CanStack(
                    _UdsRxFilterBus(bus, rx_id=rx_id, uds=self),
                    address=addr,
                    params=isotp_params,
                )
            except (TypeError, ValueError):
                self.stack = CanStack(
                    _UdsRxFilterBus(bus, rx_id=rx_id, uds=self), address=addr
                )
        self.error_frame_count = 0
        self.tx_overflow_count = 0

    @staticmethod
    def _is_error_frame(msg: can.Message | None) -> bool:
        return msg is not None and bool(getattr(msg, "is_error_frame", False))

    def _recv_data_frame(self, timeout_s: float) -> can.Message | None:
        """``bus.recv`` that skips CAN error frames."""
        end = time.time() + timeout_s
        while time.time() < end:
            msg = self.bus.recv(timeout=min(0.1, max(0.01, end - time.time())))
            if msg is None:
                return None
            if self._is_error_frame(msg):
                self.error_frame_count += 1
                continue
            return msg
        return None

    def send_broadcast(self, arb_id: int, data: bytes | Iterable[int]) -> None:
        payload = bytes(data)
        msg = can.Message(arbitration_id=arb_id, data=payload, is_extended_id=False)
        self.bus.send(msg)

    def wake_gateway(self, *, repeats: int = 5, gap_s: float = 0.15) -> None:
        """Some gateways need 0x101 TesterPresent before the ECM answers on 0x7E0."""
        tp = bytes([0xFE, 0x01, 0x3E, 0x00, 0, 0, 0, 0])
        for _ in range(repeats):
            self.send_broadcast(GATEWAY_BC_ID, tp)
            time.sleep(gap_s)

    def send_single_frame(self, payload: bytes | Iterable[int]) -> None:
        """Send a UDS single-frame on the physical tester ID (bypass ISO-TP stack)."""
        req = bytes(payload)
        if len(req) > 7:
            raise ValueError(f"single-frame UDS max 7 bytes, got {len(req)}")
        frame = bytes([len(req)]) + req + bytes(7 - len(req))
        self.bus.send(
            can.Message(arbitration_id=self.tx_id, data=frame, is_extended_id=False)
        )

    def recv_isotp_payload(self, timeout_s: float = 5.0) -> bytes:
        """Receive an ISO-TP response (single- or multi-frame) from ``rx_id``."""
        payload = bytearray()
        end = time.time() + timeout_s
        while time.time() < end:
            msg = self._recv_data_frame(0.1)
            if msg is None or msg.arbitration_id != self.rx_id:
                continue
            data = bytes(msg.data)
            if not data:
                continue
            pci = data[0] >> 4
            if pci == 0:
                ln = data[0] & 0x0F
                return data[1 : 1 + ln]
            if pci == 1:
                total = ((data[0] & 0x0F) << 8) | data[1]
                payload.extend(data[2:])
                fc = bytes([0x30, 0x00, 0x00, 0, 0, 0, 0, 0])
                self.bus.send(
                    can.Message(arbitration_id=self.tx_id, data=fc, is_extended_id=False)
                )
                seq = 1
                while len(payload) < total and time.time() < end:
                    cf = self._recv_data_frame(0.5)
                    if cf is None or cf.arbitration_id != self.rx_id:
                        continue
                    cfd = bytes(cf.data)
                    if not cfd or (cfd[0] >> 4) != 2:
                        continue
                    if (cfd[0] & 0x0F) != (seq & 0x0F):
                        continue
                    payload.extend(cfd[1:])
                    seq += 1
                return bytes(payload[:total])
        return b""

    def uds_request_fallback(
        self,
        payload: bytes | Iterable[int],
        timeout_s: float = 5.0,
        *,
        progress: LogFn | None = None,
        label: str = "",
    ) -> tuple[bool, bytes]:
        """ISO-TP stack first; raw single-frame + manual RX if the stack times out."""
        req = bytes(payload)
        tag = label or " ".join(f"${b:02X}" for b in req)
        if progress:
            progress(f"    [{_ts()}] TX {tag} (ISO-TP, {min(timeout_s, 3.0):.0f}s)")
        stack_t = min(timeout_s, 3.0)
        ok, raw = self.uds_request(payload, timeout_s=stack_t)
        if ok or raw:
            if progress:
                status = "OK" if ok else f"NRC/err {raw.hex() if raw else '?'}"
                progress(f"    [{_ts()}] RX {tag}: {status}")
            return ok, raw
        if progress:
            progress(f"    [{_ts()}] ISO-TP timeout — retry raw single-frame {tag}")
        if len(req) > 7:
            return False, b""
        self.send_single_frame(req)
        raw = self.recv_isotp_payload(timeout_s=min(timeout_s, 4.0))
        if progress:
            status = "OK" if raw and raw[0] == ((req[0] + 0x40) & 0xFF) else (
                raw.hex() if raw else "timeout"
            )
            progress(f"    [{_ts()}] RX {tag} (raw): {status}")
        if not raw:
            return False, b""
        if raw[0] == 0x7F and len(raw) >= 3 and raw[1] == req[0]:
            return False, raw
        expected = (req[0] + 0x40) & 0xFF
        if raw[0] == expected:
            if (
                req[0] in _UDS_SUBFN_ECHO_SIDS
                and len(req) >= 2
                and len(raw) >= 2
                and raw[1] != req[1]
            ):
                return False, raw
            return True, raw
        return False, raw

    def ping_ecm(self, timeout_s: float = 3.0) -> str:
        """Return ``ok``, ``nrc_XX``, or ``timeout`` for a $3E 00 TesterPresent."""
        ok, raw = self.uds_request_fallback([0x3E, 0x00], timeout_s=timeout_s)
        if ok:
            return "ok"
        if raw and raw[0] == 0x7F and len(raw) >= 3:
            return f"nrc_{raw[2]:02X}"
        return "timeout"

    def sniff_bus(
        self,
        duration_s: float = 2.0,
        *,
        progress: LogFn | None = None,
        label: str = "listen",
    ) -> list[tuple[int, bytes]]:
        """Collect data frames for bus-alive diagnostics (error frames counted separately).

        Uses raw ``bus.recv`` only — never ``_pump_stack``. ISO-TP ``CanStack.process``
        calls ``bus.recv`` and discards non-0x7E8 traffic, which would show 0 frames
        on a live vehicle bus even when thousands of broadcast frames are present.
        """
        frames: list[tuple[int, bytes]] = []
        err_before = self.error_frame_count
        error_frames = 0
        start = time.time()
        end = start + duration_s
        last_tick = start
        while time.time() < end:
            msg = self._recv_data_frame(0.1)
            if msg is not None:
                frames.append((msg.arbitration_id, bytes(msg.data)))
            error_frames = self.error_frame_count - err_before
            now = time.time()
            if progress and now - last_tick >= 0.5:
                elapsed = now - start
                progress(
                    f"    [{_ts()}] {label}: {elapsed:.1f}s / {duration_s:.1f}s, "
                    f"{len(frames)} data frames, {error_frames} error frames"
                )
                last_tick = now
        if error_frames and progress:
            progress(
                f"    [{_ts()}] {label}: {error_frames} CAN error frames "
                f"(0x000) — bus fault, NOT real traffic"
            )
        return frames

    def drain_rx(self, settle_s: float = 0.05) -> None:
        """Drop stale RX without starving passive sniff (raw recv only)."""
        end = time.time() + settle_s
        while time.time() < end:
            self._recv_data_frame(0.01)

    def flush(self) -> None:
        """Clear ISO-TP state and stale RX frames (required between read sessions)."""
        try:
            self.stack.reset()
        except Exception:
            pass
        self.drain_rx(0.15)

    def _wait_tx_complete(self, timeout_s: float) -> bool:
        """Pump ISO-TP until multi-frame TX finishes (kernel / plant $36).

        Speed lessons from live plants:
        - ECU may advertise FC STmin=10 ms (``30 00 0A`` on this EARLY donor).
          Honoring that alone ≈ 6–9 s per 4 KiB $36. Stack is constructed with
          override_receiver_stmin=200 µs to ignore that throttle.
        - Old pump used ``rx_timeout=0.02`` every process → ~20 min plant.
        - Blind ``rx_timeout=0.0`` never sees FlowControl (WAIT_FC starves).
        - **State-aware pump:** block briefly only while WAIT_FC; during
          TRANSMIT_CF spin ``do_rx=False, do_tx=True`` so we do not pay RX
          timeout between every consecutive frame.
        - Adapter can raise TX buffer overflow if CF blast is unbounded; back off
          1 ms and continue (counted in ``tx_overflow_count``).
        """
        end = time.time() + timeout_s
        wait_fc = getattr(self.stack, "TxState", None)
        wait_fc_val = getattr(wait_fc, "WAIT_FC", None) if wait_fc is not None else None
        while time.time() < end:
            if not self.stack.transmitting():
                return True
            try:
                st = getattr(self.stack, "tx_state", None)
                if wait_fc_val is not None and st == wait_fc_val:
                    # Need FlowControl — short block, RX enabled.
                    self.stack.process(rx_timeout=0.002, do_rx=True, do_tx=True)
                else:
                    # CF / SF path: spin TX; no RX wait between frames.
                    self.stack.process(rx_timeout=0.0, do_rx=False, do_tx=True)
            except Exception as exc:
                msg = str(exc).lower()
                if "overflow" in msg or "buffer" in msg or "-13" in msg:
                    self.tx_overflow_count += 1
                    time.sleep(0.001)
                # Other stack/bus glitches: keep pumping until timeout.
            # If still transmitting but not WAIT_FC, occasional RX poll for late FC
            # edge cases (some stacks re-enter WAIT_FC mid-block).
            if self.stack.transmitting():
                st2 = getattr(self.stack, "tx_state", None)
                if wait_fc_val is not None and st2 == wait_fc_val:
                    try:
                        self.stack.process(rx_timeout=0.001, do_rx=True, do_tx=True)
                    except Exception:
                        pass
        return not self.stack.transmitting()

    def uds_request(self, payload: bytes | Iterable[int], timeout_s: float = 5.0) -> tuple[bool, bytes]:
        """Send UDS request; return (success, full positive response bytes)."""
        req = bytes(payload)
        self.stack.send(req)
        if len(req) > 7:
            # TX budget scales with payload; plant ~4 KB ISO-TP should finish in <1 s
            # once the pump is tight. Cap so a wedged stack fails fast.
            tx_budget = min(max(timeout_s, 2.0), 30.0)
            if len(req) > 512:
                tx_budget = min(max(timeout_s, 5.0), 45.0)
            if not self._wait_tx_complete(tx_budget):
                return False, b""
        else:
            self._pump_uds(0.0)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                # Prefer short polls so NRC-78 / positive responses surface quickly.
                raw = self._recv_until(
                    timeout_s=min(0.25, max(0.02, deadline - time.time()))
                )
            except Exception:
                return False, b""
            if not raw:
                continue
            if raw[0] == 0x7F and len(raw) >= 3 and raw[1] == req[0]:
                if raw[2] == 0x78:
                    time.sleep(0.05)
                    continue
                return False, raw
            expected = (req[0] + 0x40) & 0xFF
            if raw[0] != expected:
                # Stale ISO-TP payload (e.g. $68 after $28 broadcast before $27 seed).
                continue
            if (
                req[0] in _UDS_SUBFN_ECHO_SIDS
                and len(req) >= 2
                and len(raw) >= 2
                and raw[1] != req[1]
            ):
                continue
            if req[:2] == bytes([0x1A, 0x90]) and len(raw) < 19:
                continue
            if req[:2] == bytes([0x09, 0x04]) and len(raw) < 12:
                continue
            if req[:2] == bytes([0x27, 0x01]) and len(raw) < 4:
                continue
            return True, raw
        return False, b""

    def _pump_uds(self, timeout_s: float) -> None:
        """Drive ISO-TP on UDS-filtered RX (safe on a busy vehicle bus)."""
        try:
            self.stack.process(rx_timeout=max(0.0, timeout_s), do_rx=True, do_tx=True)
        except Exception:
            pass

    def _pump_tx(self) -> None:
        try:
            self.stack.process(rx_timeout=0.0, do_rx=False, do_tx=True)
        except Exception:
            pass

    def _recv_stack_only(self, timeout_s: float) -> bytes:
        """Receive via CanStack only — avoids racing manual ISO-TP with the stack."""
        end = time.time() + timeout_s
        while time.time() < end:
            remaining = end - time.time()
            self._pump_uds(min(0.15, remaining))
            try:
                data = self.stack.recv()
                if data:
                    return bytes(data)
            except Exception:
                pass
        return b""

    def _recv_until(self, timeout_s: float) -> bytes:
        end = time.time() + timeout_s
        while time.time() < end:
            remaining = end - time.time()
            raw = self._recv_stack_only(timeout_s=min(0.2, remaining))
            if raw:
                return raw
            # Small single-frame fallbacks only (probe/VIN). Never use during $23 MF reads.
            if timeout_s <= 4.0:
                raw = self.recv_isotp_payload(timeout_s=min(0.15, remaining))
                if raw:
                    return raw
        return b""

    def uds_read_memory_block(
        self,
        addr: int,
        length: int,
        *,
        timeout_s: float = READ_BLOCK_TIMEOUT_S,
    ) -> tuple[bool, bytes]:
        """UDS $23 — raw ISO-TP (SF TX, FF/FC/CF RX).

        can-isotp missed SCPB-R2 First Frames (CFs while idle). The kernel
        answers $1A BB as a single frame; $23 is always a 2 KB multi-frame.
        """
        if length <= 0 or length > READ_BLOCK:
            raise ValueError(f"read block size must be 1..{READ_BLOCK}, got {length}")
        req = bytes([0x23]) + struct.pack(">I", addr) + struct.pack(">H", length)
        try:
            self.stack.reset()
        except Exception:
            pass
        self.drain_rx(0.02)
        self.send_single_frame(req)
        raw = self.recv_isotp_payload(timeout_s=timeout_s)
        if not raw:
            return False, b""
        if raw[0] == 0x7F and len(raw) >= 3 and raw[1] == 0x23:
            return False, raw
        need = 5 + length
        if raw[0] != 0x63 or len(raw) < need:
            return False, raw
        data = raw[5 : 5 + length]
        if len(data) != length:
            return False, raw
        # Trailing CFs after we already have `total`. Cap so a stuck
        # stream cannot hang the host (emulator: 1 FF + 293 CF per 2 KiB).
        deadline = time.time() + 0.25
        quiet_until = time.time() + 0.03
        while time.time() < deadline and time.time() < quiet_until:
            msg = self._recv_data_frame(0.02)
            if msg is not None and msg.arbitration_id == self.rx_id:
                quiet_until = min(deadline, time.time() + 0.03)
        return True, data

    def wait_kernel_ack(self, timeout_s: float = 3.0) -> bool:
        """Poll for a kernel-alive positive response on 0x7E8."""
        end = time.time() + timeout_s
        while time.time() < end:
            # ISO-TP poll (preferred) and legacy single-frame fallback
            self.stack.send(bytes([0x1A, 0xBB]))
            self._pump_tx()
            raw = self._recv_until(timeout_s=0.3)
            if raw and len(raw) >= 2 and raw[0] == 0x5A and raw[1] == 0xBB:
                return True
            poll = bytes([0x02, 0x1A, 0xBB, 0, 0, 0, 0, 0])
            self.bus.send(can.Message(arbitration_id=self.tx_id, data=poll, is_extended_id=False))
            m = self._recv_data_frame(0.1)
            if m is None or m.arbitration_id != self.rx_id:
                continue
            data = bytes(m.data)
            if len(data) >= 4 and data[0] == 0x5A and data[1] == 0xBB:
                return True
        return False


class E92BinExtractor:
    """Full E92A flash read orchestrator."""

    def __init__(
        self,
        bus: can.BusABC,
        *,
        log: LogFn | None = None,
        detail_log: LogFn | None = None,
        variant: E92Variant = E92Variant.LATE,
        stop_check: Callable[[], bool] | None = None,
        kernel_path: Path | None = None,
    ):
        self.bus = bus
        self.uds = IsoTpUdsClient(bus)
        self.log = log or (lambda m: None)
        self.detail_log = detail_log or self.log
        self.variant = variant
        self._stop = stop_check or (lambda: False)
        self.kernel_path = Path(kernel_path) if kernel_path else KERNEL_BIN
        self._require_early_kernel = False
        self._keylib = None
        self.last_diagnose = BusDiagnoseSnapshot()
        self.last_read_progress = ReadProgress()
        self.last_read_result: ReadResult | None = None
        self._active_read_buf: bytearray | None = None
        self._last_partial_snapshot: bytes = b""
        self._kernel_alive = False
        self._extkern_active = False
        self._write_kernel_read_mode = ""
        self._cached_vin = ""
        self._cached_osid = ""
        self._unlock_attempts = 0
        self._unlock_blocked_until = 0.0
        self._read_pass_number = 1
        self._read_pass_total = 1
        self._read_phase = "read"
        self._last_battery_v: float | None = None
        self._last_battery_ts = 0.0

    def _simulation(self) -> bool:
        return _is_sim_bus(self.bus)

    def _pause(self, seconds: float) -> None:
        if self._simulation():
            return
        time.sleep(seconds)

    def _keylib_mod(self):
        if self._keylib is None:
            self._keylib = _load_keylib()
        return self._keylib

    def _prepare_session(self) -> None:
        """Drop to default session and flush ISO-TP between back-to-back operations."""
        if self._kernel_alive:
            return
        self.uds.flush()
        self.uds.uds_request([0x10, 0x01], timeout_s=2.0)
        self.uds.flush()

    def is_kernel_alive(self, timeout_s: float = 1.0) -> bool:
        """Ping $1A BB. Never trust a stale _kernel_alive flag — after an
        …F800 crash the stock bootloader answers $23 with NRC 31 while
        this flag was still True and re-upload was skipped."""
        if self.uds.wait_kernel_ack(timeout_s):
            self._kernel_alive = True
            return True
        self._kernel_alive = False
        return False

    @staticmethod
    def _decode_pid42(data: bytes) -> float | None:
        """Parse Mode 01 PID $42 (control-module voltage) from a CAN payload."""
        # Possible layouts: PCI 41 42 HI LO  |  41 42 HI LO  | ISO-TP SF 04 41 42 HI LO
        if len(data) < 4:
            return None
        for i in range(0, min(3, len(data) - 3)):
            if data[i] == 0x41 and data[i + 1] == 0x42:
                rest = data[i + 2 :]
                if len(rest) >= 2:
                    return ((rest[0] << 8) | rest[1]) / 1000.0
                if rest:
                    return rest[0] / 10.0
        return None

    def read_battery_voltage_v(self, *, wake: bool = True) -> float | None:
        """Battery / module voltage from CAN only. Never a USB PSU."""
        if self._simulation():
            return 12.6
        if wake and not self._kernel_alive:
            self._prepare_session()
        # Functional then physical — cars answer one or both.
        for arb, frame in (
            (0x7DF, [0x02, 0x01, 0x42, 0, 0, 0, 0, 0]),
            (TESTER_ID, [0x02, 0x01, 0x42, 0, 0, 0, 0, 0]),
        ):
            self.uds.send_broadcast(arb, frame)
            end = time.time() + 1.2
            while time.time() < end:
                msg = self.uds._recv_data_frame(0.12)
                if msg is None:
                    continue
                if not (ECU_ID <= msg.arbitration_id <= 0x7EF):
                    continue
                v = self._decode_pid42(bytes(msg.data))
                if v is None:
                    continue
                if v >= 1.0:
                    self._last_battery_v = v
                    self._last_battery_ts = time.time()
                return v
        # ISO-TP physical (some gateways only complete $42 this way)
        try:
            ok, raw = self.uds.uds_request([0x01, 0x42], timeout_s=1.2)
        except Exception:
            ok, raw = False, b""
        if ok and raw:
            v = self._decode_pid42(raw)
            if v is None and len(raw) >= 3 and raw[0] == 0x41 and raw[1] == 0x42:
                v = self._decode_pid42(bytes([0x03, *raw]))
            if v is not None:
                if v >= 1.0:
                    self._last_battery_v = v
                    self._last_battery_ts = time.time()
                return v
        return None

    def check_battery_voltage(self, *, min_v: float = MIN_BATTERY_V) -> bool:
        """Report CAN voltage. Never blocks a read — no USB PSU, no hard fail.

        Real cars expose PID $42. Bare bench ECMs often answer 0.000 V or
        nothing. That is not a reason to refuse a read.
        """
        v: float | None = None
        for attempt in range(1, 3):
            v = self.read_battery_voltage_v(wake=(attempt == 1))
            if v is not None:
                break
            if attempt < 2:
                self._pause(0.25)
        if v is not None and v < 1.0:
            self.log(f"Battery (CAN $42): {v:.2f} V — not a usable reading, continuing")
            return True
        if v is None:
            self.log("Battery (CAN $42): no reply — continuing without a voltage gate")
            return True
        self.log(f"Battery (CAN $42): {v:.1f} V")
        if v < min_v:
            self.log(f"WARN: CAN voltage {v:.1f} V is below {min_v:.1f} V — continuing anyway")
        elif v < WARN_BATTERY_V:
            self.log(f"WARN: CAN voltage marginal ({v:.1f} V)")
        return True

    def abort_and_recover(self) -> bool:
        """Cancel an in-flight read and return ECM to stock OS."""
        self.log("Abort+Recover: cancelling read and restoring ECM…")
        return self.recover_to_stock()

    @staticmethod
    def run_preflight(
        *,
        other_tools_clear: bool,
        key_on: bool,
        engine_off: bool,
        simulation: bool = False,
    ) -> PreflightResult:
        if simulation:
            msgs = [
                "SIMULATION MODE — virtual E92 ECM (no adapter, no vehicle).",
                "Practice the full unlock → kernel → 4 MB read sequence safely.",
            ]
            if not KERNEL_BIN.is_file():
                msgs.append(f"ERROR: Kernel missing: {KERNEL_BIN}")
                return PreflightResult(ok=False, messages=msgs)
            return PreflightResult(ok=True, messages=msgs)

        msgs: list[str] = []
        ok = True
        if not other_tools_clear:
            msgs.append("WARN: Disconnect other scan tools before ECU read (bus/security conflict).")
            ok = False
        if not key_on:
            msgs.append("WARN: Ignition ON (key run, engine OFF) required.")
            ok = False
        if not engine_off:
            msgs.append("WARN: Engine must be OFF during read.")
            ok = False
        if not KERNEL_BIN.is_file():
            msgs.append(f"ERROR: Kernel missing: {KERNEL_BIN}")
            ok = False
            ok = False
        if ok:
            msgs.append("Preflight OK — key ON, engine OFF, other tools unplugged.")
        return PreflightResult(ok=ok, messages=msgs)

    @staticmethod
    def list_kvaser_channels() -> list[str]:
        return [f"kvaser:{c.channel}" for c in list_kvaser_channel_details()]

    def _request_timeout(self, default_s: float, *, probe: bool = False) -> float:
        if self._simulation():
            return default_s
        return max(default_s, 4.0 if probe else 8.0)

    def _step(self, msg: str) -> None:
        self.log(f"[{_ts()}] {msg}")

    def _bus_trace(self, msg: str) -> None:
        """Sniff ticks → verbose only; brief tick also in session summary."""
        self.detail_log(msg)
        if "data frames" in msg:
            brief = msg.strip().split("] ", 1)[-1] if "] " in msg else msg.strip()
            self._step(brief)

    @staticmethod
    def _summarize_frames(frames: list[tuple[int, bytes]]) -> dict[int, int]:
        counts: dict[int, int] = {}
        for arb_id, _ in frames:
            counts[arb_id] = counts.get(arb_id, 0) + 1
        return counts

    @staticmethod
    def _obd_rpm_responders(frames: list[tuple[int, bytes]]) -> list[int]:
        seen: list[int] = []
        for arb_id, data in frames:
            if len(data) >= 3 and data[1] == 0x41 and data[2] == 0x0C and arb_id not in seen:
                seen.append(arb_id)
        return seen

    def diagnose_bus(self) -> str:
        """Listen for HS-CAN traffic, OBD RPM poll, ECM ping, quick VIN read. Returns verdict."""
        self._step("=== Bus diagnose START ===")
        contention = check_bus_contention()
        if contention.risky:
            self._step("WARN: bus contention — close other CAN tools before FULL READ")
            for ln in contention.python_scan_jobs + contention.extra_jobs:
                self.log(f"  {ln}")
            if contention.note:
                self.log(f"  ({contention.note})")
        if self._simulation():
            try:
                from e92a_ecu_simulator import get_active_simulator

                sim = get_active_simulator()
                if sim is not None and sim.is_bricked:
                    self._step("SIM BRICKED — ECM CAN silent (erase drill)")
                    self.last_diagnose = BusDiagnoseSnapshot(verdict="no_bus")
                    return "no_bus"
            except ImportError:
                pass
            self._step("SIM ECM — quiet passive bus is normal; will probe UDS directly")
        self.uds.flush()
        self.uds.error_frame_count = 0
        self._step("Diagnose takes ~15s — frame ticks appear below (not frozen)")
        pulse = self.uds.sniff_bus(0.4, progress=self._bus_trace, label="quick-pulse")
        self._step(f"Quick pulse: {len(pulse)} data frames in 0.4s (raw, pre-wake)")

        self._step("Step 1/5: Gateway wake (0x101 TesterPresent ×6)")
        self.uds.wake_gateway(repeats=6, gap_s=0.15)
        self._step("Step 1/5: Gateway wake done (~1s)")

        self._step("Step 2/5: Passive listen 1.5s (is anything on HS-CAN?)")
        passive = self.uds.sniff_bus(1.5, progress=self._bus_trace, label="passive")
        pcounts = self._summarize_frames(passive)
        if pcounts:
            self._step(f"Passive done: {len(passive)} frames / {len(pcounts)} IDs")
            for arb_id in sorted(pcounts, key=lambda a: -pcounts[a])[:8]:
                self.log(f"  0x{arb_id:03X}  x{pcounts[arb_id]}")
        elif passive:
            self._step(f"Passive: {len(passive)} frames (unexpected empty summary)")
        else:
            self._step("Passive: 0 frames — key RUN? wrong channel? second app on the adapter?")
            self._step("  → Use channel 0. Close other CAN tools. Confirm the bus channel first.")

        self._step("Step 3/5: OBD RPM poll 0x7DF $01 $0C + listen 1.5s")
        self.uds.send_broadcast(0x7DF, [0x02, 0x01, 0x0C, 0, 0, 0, 0, 0])
        active = self.uds.sniff_bus(1.5, progress=self._bus_trace, label="RPM poll")
        rpm_ids = self._obd_rpm_responders(active)
        if rpm_ids:
            self._step(f"RPM responders: {', '.join(f'0x{i:03X}' for i in rpm_ids)}")
        else:
            self._step("No RPM response — is key in RUN? Other tools unplugged?")

        self._step("Step 4/5: Gateway wake + default session $10 $01 + $3E ping")
        self.uds.wake_gateway(repeats=4)
        self.uds.uds_request([0x10, 0x01], timeout_s=3.0)
        ping = self.uds.ping_ecm(timeout_s=3.0)
        self._step(f"ECM $3E ping result: {ping}")

        self._step("Step 5/5: Quick VIN read $1A $90")
        ok_vin, raw_vin = self.uds.uds_request_fallback(
            [0x1A, 0x90], timeout_s=4.0, progress=self.detail_log, label="VIN $1A $90"
        )
        quick_vin = ""
        if ok_vin and len(raw_vin) >= 19:
            payload = bytes(raw_vin[2:19])
            quick_vin = _decode_vin_from_1a90(raw_vin)
            if _is_valid_vin(quick_vin):
                self.log(f"  Quick VIN read: {quick_vin}")
            elif _is_erased_vin_payload(payload):
                quick_vin = ""
                self.log(f"  Quick VIN read: erased/blank ({payload.hex()}) — junkyard donor OK")
            else:
                quick_vin = ""
                self.log(f"  Quick VIN read: invalid payload ({payload.hex()})")
        elif ok_vin and raw_vin:
            self.log(
                f"  Quick VIN truncated ({len(raw_vin)} B, need 19+) — ISO-TP reassembly issue"
            )

        if self.uds.error_frame_count:
            self._step(
                f"CAN error frames this session: {self.uds.error_frame_count} "
                f"(ignored — not application traffic)"
            )
        if _is_valid_vin(quick_vin):
            verdict = "ecm_ok"
            self.log("VERDICT: ECM OK — run Probe or FULL READ")
        elif self._simulation():
            verdict = "sim_ok"
            self.log("VERDICT: SIM ECM — run Probe (passive listen optional in sim)")
        elif ping in ("ok",) or ping.startswith("nrc_"):
            verdict = "uds_ok"
            self.log("VERDICT: ECM answering UDS — run Probe")
        elif 0x7E8 in rpm_ids:
            verdict = "obd_only"
            self.log("VERDICT: OBD OK, UDS stuck — key OFF 60s → RUN, wait 30s")
        elif pcounts:
            verdict = "bus_only"
            self.log("VERDICT: Real CAN data present, ECM not answering UDS — other tools unplugged? key RUN?")
        else:
            verdict = "no_bus"
            self.log("VERDICT: No real CAN data (error frames or silence)")
            self.log("  → Close other CAN tools; unplug the adapter 10s; one app only")
            self.log("  → Key RUN, connect channel 0 — RPM/MAP on the bus proves wiring is fine")

        self.last_diagnose = BusDiagnoseSnapshot(
            verdict=verdict,
            passive_data_frames=len(passive),
            passive_top_ids={f"0x{k:03X}": v for k, v in sorted(pcounts.items(), key=lambda kv: -kv[1])[:8]},
            rpm_responders=[f"0x{i:03X}" for i in rpm_ids],
            ecm_ping=ping,
            quick_vin=quick_vin,
            can_error_frames=self.uds.error_frame_count,
        )
        return verdict

    def probe_identity(self) -> tuple[str, str]:
        """Best-effort VIN + OSID without unlocking."""
        vin = ""
        osid = ""
        t = self._request_timeout(3.0, probe=True)

        self._step("=== Probe START ===")
        if self.is_kernel_alive(0.5):
            self.log(
                "Kernel already running — stock UDS identity unavailable; "
                f"using cached VIN={self._cached_vin or '?'} OSID={self._cached_osid or '?'}"
            )
            return self._cached_vin, self._cached_osid
        self.uds.flush()
        if not self._simulation():
            self._step("Wake gateway + default session $10 $01")
            self.uds.wake_gateway(repeats=6, gap_s=0.15)
            self.uds.uds_request([0x10, 0x01], timeout_s=t)
        else:
            self.uds.drain_rx(0.2)

        vin = self._read_probe_vin(timeout_s=t)

        self._step("Read OSID: $09 $04")
        ok2, raw2 = (
            self.uds.uds_request([0x09, 0x04], timeout_s=t)
            if self._simulation()
            else self.uds.uds_request_fallback(
                [0x09, 0x04], timeout_s=t, progress=self.detail_log, label="OSID $09 $04"
            )
        )
        if ok2 and len(raw2) >= 8:
            cals = _parse_cal_ids_09_04(raw2)
            osid = cals[0] if cals else ""
            if cals:
                self._step(
                    f"CAL IDs from $09 $04: {' · '.join(cals)} "
                    f"(first used as tag; not a scraped OS)"
                )
        elif ok2 and raw2:
            self._step(f"OSID $09 $04 truncated ({len(raw2)} B) — retrying fallbacks")
        if not osid:
            self._step("OSID fallback: $1A $B4")
            ok3, raw3 = (
                self.uds.uds_request([0x1A, 0xB4], timeout_s=t)
                if self._simulation()
                else self.uds.uds_request_fallback(
                    [0x1A, 0xB4], timeout_s=t, progress=self.detail_log, label="OSID $1A $B4"
                )
            )
            if ok3 and len(raw3) > 2:
                osid = bytes(raw3[2:]).decode("ascii", errors="replace").strip("\x00")
                if osid.upper().startswith("KERN") or "KERNEL" in osid.upper():
                    self.log("  OSID $1A $B4 looks like a kernel banner — not stock ECM")
                    osid = ""
                else:
                    self._step(f"OSID from $1A $B4: {osid}")

        if not vin and not osid and not self._simulation():
            self._step("No identity — OBD RPM poll 0x7DF")
            self.uds.send_broadcast(0x7DF, [0x02, 0x01, 0x0C, 0, 0, 0, 0, 0])
            sniff = self.uds.sniff_bus(1.5, progress=self.detail_log, label="RPM fallback")
            rpm_ids = self._obd_rpm_responders(sniff)
            if rpm_ids:
                self.log(f"  RPM on {', '.join(f'0x{i:03X}' for i in rpm_ids)} — bus OK, ECM UDS stuck")
            else:
                self.log("  No RPM either — wiring/key/other-tool/channel issue")

        self.log(f"Probe: VIN={vin or '?'} OSID={osid or '?'}")
        if _is_valid_vin(vin):
            self._cached_vin = vin
        else:
            self._cached_vin = ""
        if osid and "KERNEL" not in osid.upper() and not osid.upper().startswith("KERN"):
            self._cached_osid = osid
        if not probe_ready_for_read(vin, osid):
            self.log("  FIX: Real adapter, channel 0, key OFF 60s → RUN, wait 30s, Bus Diagnose, Probe")
        elif not _is_valid_vin(vin) and _is_valid_osid(osid):
            self.log(
                f"  Bench donor — erased/invalid VIN, OSID {osid} OK — FULL READ allowed"
            )
        return sanitize_probe_vin(vin), osid

    def _read_probe_vin(self, *, timeout_s: float) -> str:
        """Read VIN via $1A $90, then $22 $F1 $90 if blank/invalid."""
        vin = ""
        self._step("Read VIN: $1A $90")
        ok, raw = (
            self.uds.uds_request([0x1A, 0x90], timeout_s=timeout_s)
            if self._simulation()
            else self.uds.uds_request_fallback(
                [0x1A, 0x90], timeout_s=timeout_s, progress=self.detail_log, label="VIN $1A $90"
            )
        )
        if ok and len(raw) >= 19:
            payload = bytes(raw[2:19])
            candidate = _decode_vin_from_1a90(raw)
            if _is_valid_vin(candidate):
                vin = candidate
                self._step(f"VIN from $1A $90: {vin}")
                if not self._simulation() and EXPECTED_VIN_PREFIX and not vin.startswith(EXPECTED_VIN_PREFIX):
                    self.log("  VIN prefix does not match the optional expected prefix")
            elif _is_erased_vin_payload(payload):
                self._step(f"VIN from $1A $90: erased/blank ({payload.hex()})")
            else:
                self._step(f"VIN from $1A $90: invalid ({payload.hex()})")

        if not _is_valid_vin(vin):
            self._step("VIN fallback: $22 $F1 $90")
            ok_fb, raw_fb = (
                self.uds.uds_request([0x22, 0xF1, 0x90], timeout_s=timeout_s)
                if self._simulation()
                else self.uds.uds_request_fallback(
                    [0x22, 0xF1, 0x90],
                    timeout_s=timeout_s,
                    progress=self.detail_log,
                    label="VIN $22 F190",
                )
            )
            if ok_fb and len(raw_fb) > 3:
                payload_fb = bytes(raw_fb[3:20])
                candidate = _decode_vin_from_22f190(raw_fb)
                if _is_valid_vin(candidate):
                    vin = candidate
                    self._step(f"VIN from $22 F190: {vin}")
                elif _is_erased_vin_payload(payload_fb):
                    self._step(f"VIN from $22 F190: erased/blank ({payload_fb.hex()})")
                else:
                    self._step(f"VIN from $22 F190: invalid ({payload_fb.hex()})")
        return vin if _is_valid_vin(vin) else ""

    def _block_unlock_retry(self) -> None:
        if not self._simulation():
            self._unlock_blocked_until = time.time() + UNLOCK_COOLDOWN_S

    def _unlock_preflight(self) -> bool:
        if self._simulation():
            return True
        now = time.time()
        if now < self._unlock_blocked_until:
            wait = int(self._unlock_blocked_until - now) + 1
            self.log(f"SAFETY: unlock cooldown active — wait {wait}s (MEC / lockout protection)")
            return False
        vin = sanitize_probe_vin(self._cached_vin or "")
        self._cached_vin = vin
        osid = (self._cached_osid or "").strip()
        unlock_limit = MAX_UNLOCK_ATTEMPTS
        bench_donor = not vin and _is_valid_osid(osid)
        if bench_donor or (vin and EXPECTED_VIN_PREFIX and not vin.startswith(EXPECTED_VIN_PREFIX)):
            unlock_limit = max(unlock_limit, 4)
        if self._unlock_attempts >= unlock_limit:
            self.log(
                "SAFETY: max unlock attempts this session — key OFF 60s, restart sCANtool, Probe again"
            )
            return False
        if not probe_ready_for_read(vin, osid):
            self.log(
                "SAFETY: run Probe first — need valid 17-character VIN or readable OSID before unlock"
            )
            return False
        if bench_donor:
            self.log(f"SAFETY: bench donor — erased/invalid VIN, unlock on OSID {osid}")
        elif EXPECTED_VIN_PREFIX and not vin.startswith(EXPECTED_VIN_PREFIX):
            self.log(f"SAFETY: VIN {vin[:17]} is not the optional expected prefix; unlock after Probe OK")
        if not self.check_battery_voltage():
            return False
        return True

    def _security_unlock(self) -> bool:
        if not self._unlock_preflight():
            return False
        self._unlock_attempts += 1
        self.uds.flush()
        self.detail_log("  $10 02 programmingSession")
        ok, raw = self.uds.uds_request([0x10, 0x02], timeout_s=10.0)
        if not ok:
            self.log(f"ERR: $10 02 failed ({raw.hex() if raw else 'timeout'})")
            self._block_unlock_retry()
            return False
        self._pause(0.02)

        self.detail_log("  $28 00 disableNormalCommunication (broadcast)")
        self.uds.send_broadcast(GATEWAY_BC_ID, [0xFE, 0x01, 0x28, 0x00, 0, 0, 0, 0])
        self._pause(0.05)
        self.uds.drain_rx(0.15)

        self.detail_log("  $27 01 request seed")
        ok, raw = False, b""
        for sa_try in range(1, 5):
            ok, raw = self.uds.uds_request([0x27, 0x01], timeout_s=10.0)
            if ok and raw and raw[0] == 0x67 and len(raw) >= 4:
                break
            nrc37 = bool(raw) and len(raw) >= 3 and raw[0] == 0x7F and raw[2] == 0x37
            self.detail_log(
                f"  $27 01 attempt {sa_try}: {raw.hex() if raw else 'timeout'}"
                + (" — delay not expired, wait" if nrc37 else "")
            )
            if not nrc37:
                break
            self._pause(2.5 * sa_try)
        if not ok or raw[0] != 0x67 or len(raw) < 4:
            hint = ""
            if raw and raw[0] == 0x68:
                hint = " — stale $28 ACK; drained RX and retried"
            self.log(f"ERR: seed request failed ({raw.hex() if raw else 'timeout'}){hint}")
            self._block_unlock_retry()
            return False

        seed_bytes = raw[2:]
        seed_len = len(seed_bytes)
        self.variant = classify_variant(seed_len=seed_len)
        self.detail_log(f"  SEED ({seed_len}B): {seed_bytes.hex()}")
        if getattr(self, "_require_early_kernel", False) and seed_len != 2:
            self.log("ERR: EARLY write requires a 2-byte seed — LATE modules are refused")
            self._exit_programming_session()
            self._block_unlock_retry()
            return False

        if all(b == 0 for b in seed_bytes):
            self.detail_log("  seed=0 — already unlocked, skipping key")
            return True

        if seed_len >= 5 or self.variant == E92Variant.LATE:
            keylib = self._keylib_mod()
            mac, _, _ = keylib.derive_key_from_algo(ALGO_E92A_LATE, seed_bytes[:5])
            key5 = bytes(mac)
            self.detail_log(f"  KEY (algo {ALGO_E92A_LATE}): {key5.hex()}")
            ok, raw = self.uds.uds_request([0x27, 0x02, *key5], timeout_s=10.0)
        elif seed_len == 2 or self.variant == E92Variant.EARLY:
            from gm2byte_key import derive_e92_early_key

            seed16 = (seed_bytes[0] << 8) | seed_bytes[1]
            key16 = derive_e92_early_key(seed16)
            self.detail_log(f"  KEY (algo {ALGO_E92_EARLY}): {key16:04X}")
            ok, raw = self.uds.uds_request(
                [0x27, 0x02, (key16 >> 8) & 0xFF, key16 & 0xFF],
                timeout_s=10.0,
            )
        else:
            self.log(f"ERR: unsupported seed length {seed_len}B — cannot unlock")
            self._block_unlock_retry()
            return False

        if not ok:
            self.log(f"ERR: key rejected ({raw.hex() if raw else 'timeout'}) — wrong key increments MEC; stop and power-cycle.")
            self._block_unlock_retry()
            return False
        who = (
            f"E92 EARLY algo {ALGO_E92_EARLY}"
            if seed_len == 2 or self.variant == E92Variant.EARLY
            else f"E92A LATE algo {ALGO_E92A_LATE}"
        )
        self.detail_log(f"  Security unlocked ({who})")

        self.detail_log("  $A5 01 / $A5 03 programming mode")
        self.uds.send_broadcast(GATEWAY_BC_ID, [0xFE, 0x02, 0xA5, 0x01, 0, 0, 0, 0])
        self._pause(0.05)
        self.uds.send_broadcast(GATEWAY_BC_ID, [0xFE, 0x02, 0xA5, 0x03, 0, 0, 0, 0])
        # After a power cycle, $34 NRC 22 if we request download too soon.
        self._pause(0.25)
        return True

    def _exit_programming_session(self) -> None:
        """Drop back to default session so the ECM resumes normal broadcast traffic."""
        self.detail_log("  Recovery: $10 01 default session (dash/modules should wake back up)")
        self.uds.uds_request([0x10, 0x01], timeout_s=5.0)
        self.uds.wake_gateway(repeats=3, gap_s=0.1)
        self._pause(1.0)

    def recover_to_stock(self) -> bool:
        """Best-effort return ECM to stock OS after a stuck kernel / prog session."""
        self._kernel_alive = False
        self._extkern_active = False
        self._write_kernel_read_mode = ""
        self.log("=== ECM recovery (stock OS) ===")
        self.uds.wake_gateway(repeats=6, gap_s=0.15)
        for i in range(3):
            ok, raw = self.uds.uds_request([0x10, 0x01], timeout_s=5.0)
            self.detail_log(f"  $10 01 attempt {i + 1}: {'OK' if ok else raw.hex() if raw else 'timeout'}")
        ok, raw = self.uds.uds_request([0x11, 0x01], timeout_s=5.0)
        self.detail_log(f"  $11 01 reset: {'OK' if ok else raw.hex() if raw else 'timeout'}")
        self.uds.flush()
        self._pause(30.0 if not self._simulation() else 0.1)
        self.uds.wake_gateway(repeats=6, gap_s=0.15)
        verdict = self.diagnose_bus()
        self.log(f"  post-recovery diagnose: {verdict}")
        return verdict in ("ecm_ok", "uds_ok", "sim_ok")

    def upload_kernel(self, *, require_early: bool = False) -> bool:
        kernel_path = self.kernel_path
        if not kernel_path.is_file():
            self.log(f"ERR: kernel not found: {kernel_path}")
            return False
        if self.is_kernel_alive(0.5) and not self._extkern_active:
            self.log("Kernel already alive — skip unlock/re-upload")
            return True
        kdata = kernel_path.read_bytes()
        self.log(f"KERNEL: {len(kdata)} bytes @ 0x{KERNEL_LOAD_ADDR:X}")
        self._require_early_kernel = require_early

        alive = False
        try:
            if not self._security_unlock():
                return False

            self.detail_log("  $34 RequestDownload")
            ok, raw = False, b""
            for attempt in range(1, 5):
                ok, raw = self.uds.uds_request([0x34, 0x00, 0x00, 0x10, 0x00], timeout_s=15.0)
                if ok:
                    break
                nrc22 = bool(raw) and raw[:3] == bytes([0x7F, 0x34, 0x22])
                self.detail_log(
                    f"  $34 attempt {attempt}: {raw.hex() if raw else 'timeout'}"
                    + (" — retry" if nrc22 and attempt < 4 else "")
                )
                if not nrc22:
                    break
                self._pause(0.4 * attempt)
            if not ok:
                self.log(f"ERR: RequestDownload failed ({raw.hex() if raw else 'timeout'})")
                return False

            td = bytearray([0x36, 0x00])
            td += struct.pack(">I", KERNEL_LOAD_ADDR)
            td += kdata
            self.detail_log(
                f"  $36 00 TransferData ({len(td):,} B over ISO-TP — "
                "expect 10–30s, dash may go quiet; normal in prog mode)"
            )
            ok, raw = self.uds.uds_request(bytes(td), timeout_s=45.0)
            if not ok:
                self.log(f"ERR: TransferData failed ({raw.hex() if raw else 'timeout'})")
                return False

            self.detail_log("  $36 80 downloadAndExecute")
            exec_req = bytes([0x36, 0x80]) + struct.pack(">I", KERNEL_LOAD_ADDR)
            self.uds.uds_request(exec_req, timeout_s=1.0)
            self._pause(1.0)

            if self.uds.wait_kernel_ack(3.0):
                self.log("=== KERNEL ALIVE ===")
                self._kernel_alive = True
                self._extkern_active = False
                alive = True
                return True
            self.log("ERR: no kernel ACK — kernel did not start")
            return False
        finally:
            if not alive and not self._simulation():
                self._exit_programming_session()

    def _should_skip_boundary(self, addr: int) -> bool:
        """Last 2 KiB of each 64 KiB window at addr >= 0x10000 (…F800–…FFFF).

        Prefill 0xFF — this is a kernel crash guard, not erased firmware.
        Shadow @ 0x00FFC000 is a different map and is not skipped here.
        """
        if not SKIP_F800_TAILS:
            return False
        if addr >= SHADOW_BASE:
            return False
        return addr >= 0x10000 and (addr & 0xFFFF) == SKIP_BOUNDARY_MASK

    def _read_flash_sim_direct(
        self,
        *,
        start: int,
        length: int,
        progress_every: int,
    ) -> tuple[bytes, int, int] | None:
        """In-process read when an optional simulator bus is attached."""
        if not self._simulation():
            return None
        try:
            from e92a_ecu_simulator import get_active_simulator
        except ImportError:
            return None
        sim = get_active_simulator()
        if sim is None:
            return None
        # Shadow region lives in ShadowFlashStore, not main flash image
        if start >= SHADOW_BASE and start + length <= SHADOW_BASE + SHADOW_SIZE + 0x1000:
            try:
                blob = sim.shadow.cpu_read(start, length)
            except Exception:
                off = start - SHADOW_BASE
                blob = bytes(sim.shadow.blob[off : off + length])
            if len(blob) < length:
                blob = blob + b"\xFF" * (length - len(blob))
            checksum = sum(blob) & 0xFFFFFFFF
            self.log(
                f"SIM bulk shadow read: 0x{start:08X}+{length:#x} "
                f"({length:,} B) from shadow store"
            )
            return bytes(blob[:length]), checksum, 0
        buf = bytearray(length)
        checksum = 0
        skipped = 0
        pos = 0
        addr = start
        self.log(f"SIM bulk read: {length:,} B direct from flash store (skips ISO-TP per block)")
        while pos < length:
            if self._stop():
                raise InterruptedError("Read cancelled")
            blk = min(READ_BLOCK, length - pos)
            if self._should_skip_boundary(addr):
                for i in range(blk):
                    buf[pos + i] = 0xFF
                    checksum = (checksum + 0xFF) & 0xFFFFFFFF
                skipped += 1
            else:
                data = sim.flash[addr : addr + blk]
                buf[pos : pos + blk] = data
                for b in data:
                    checksum = (checksum + b) & 0xFFFFFFFF
            pos += blk
            addr += blk
            pct = 100 * pos // length if length else 0
            self.last_read_progress = ReadProgress(
                bytes_read=pos,
                total_bytes=length,
                last_addr=addr,
                skipped_blocks=skipped,
                pct=pct,
                pass_number=self._read_pass_number,
                pass_total=self._read_pass_total,
                phase=self._read_phase,
            )
            if pos % progress_every == 0 or pct >= 100:
                self.log(f"  {pos:,} / {length:,} ({pct}%) [SIM bulk]")
        return bytes(buf), checksum, skipped

    def _reupload_kernel_after_crash(self) -> bool:
        """Drop to default session, wait out bootloader lockout, re-upload."""
        self._kernel_alive = False
        self._extkern_active = False
        for attempt in range(1, 6):
            self.detail_log(f"  re-upload {attempt}/5: $10 01 + 5s wait")
            self.uds.uds_request([0x10, 0x01], timeout_s=2.0)
            wait_end = time.time() + 5.0
            next_tp = time.time()
            while time.time() < wait_end:
                if time.time() >= next_tp:
                    self.uds.send_broadcast(GATEWAY_BC_ID, [0xFE, 0x01, 0x3E, 0x00, 0, 0, 0, 0])
                    next_tp = time.time() + 2.0
                self._pause(0.2)
            if self.upload_kernel():
                return True
        return False

    def read_flash(
        self,
        *,
        start: int = 0,
        length: int = FLASH_SIZE,
        progress_every: int = 65536,
    ) -> tuple[bytes, int, int]:
        """Return (data, checksum, skipped_blocks)."""
        bulk = self._read_flash_sim_direct(
            start=start, length=length, progress_every=progress_every
        )
        if bulk is not None:
            return bulk

        buf = bytearray(length)
        self._active_read_buf = buf
        checksum = 0
        skipped = 0
        last_tp = time.time()
        last_progress_at = time.time()
        pos = 0
        addr = start

        try:
            while pos < length:
                if self._stop():
                    raise InterruptedError("Read cancelled")

                if (
                    not self._simulation()
                    and time.time() - last_progress_at > READ_STALL_ABORT_S
                ):
                    raise RuntimeError(
                        f"Read stalled >{int(READ_STALL_ABORT_S)}s — aborting (ECM recovery will run)"
                    )

                if time.time() - last_tp >= 2.0:
                    self.uds.send_broadcast(GATEWAY_BC_ID, [0xFE, 0x01, 0x3E, 0x00, 0, 0, 0, 0])
                    last_tp = time.time()

                blk = min(READ_BLOCK, length - pos)

                if self._should_skip_boundary(addr):
                    for i in range(blk):
                        buf[pos + i] = 0xFF
                        checksum = (checksum + 0xFF) & 0xFFFFFFFF
                    self.detail_log(f"  PREFILL 0x{addr:X} (flash boundary skip)")
                    skipped += 1
                    pos += blk
                    addr += blk
                    continue

                ok, data = False, b""
                nrc = b""
                for attempt in range(1, READ_BLOCK_RETRIES + 1):
                    ok, data = self.uds.uds_read_memory_block(
                        addr, blk, timeout_s=READ_BLOCK_TIMEOUT_S
                    )
                    if ok:
                        break
                    if data and data[0] == 0x7F:
                        nrc = data
                        break
                    self.detail_log(f"  retry {attempt}/{READ_BLOCK_RETRIES} @ 0x{addr:X}")
                    self.uds.flush()
                    self._pause(0.08)

                if ok and len(data) == blk:
                    buf[pos : pos + blk] = data
                    for b in data:
                        checksum = (checksum + b) & 0xFFFFFFFF
                else:
                    ret = nrc.hex() if nrc else "timeout"
                    self.detail_log(f"  SKIP 0x{addr:X} — filling 0xFF (ret={ret})")
                    for i in range(blk):
                        buf[pos + i] = 0xFF
                        checksum = (checksum + 0xFF) & 0xFFFFFFFF
                    skipped += 1
                    if skipped >= 64:
                        raise RuntimeError("Too many skipped blocks — abort (power-cycle ECU and retry)")
                    if not self._reupload_kernel_after_crash():
                        raise RuntimeError("Kernel re-upload failed — power-cycle ECU")

                pos += blk
                addr += blk
                last_progress_at = time.time()
                pct = 100 * pos // length if length else 0
                self.last_read_progress = ReadProgress(
                    bytes_read=pos,
                    total_bytes=length,
                    last_addr=addr,
                    skipped_blocks=skipped,
                    pct=pct,
                    pass_number=self._read_pass_number,
                    pass_total=self._read_pass_total,
                    phase=self._read_phase,
                )
                if pos % progress_every == 0 or pct >= 100:
                    self.log(
                        f"  {pos:,} / {length:,} ({pct}%) "
                        f"[pass {self._read_pass_number}/{self._read_pass_total}]"
                    )

            self._last_partial_snapshot = b""
            return bytes(buf), checksum, skipped
        finally:
            if pos:
                self._last_partial_snapshot = bytes(buf[:pos])
            self._active_read_buf = None

    def ecu_reset(self) -> None:
        self.detail_log("  ECU reset $11 01")
        self.uds.uds_request([0x11, 0x01], timeout_s=2.0)
        self.uds.flush()
        self._kernel_alive = False
        self._pause(2.0)
        self.detail_log("  Reset sent — allow ECU ~30s to resume normal traffic")

    def _partial_read_bytes(self) -> bytes:
        if self._last_partial_snapshot:
            return self._last_partial_snapshot
        prog = self.last_read_progress
        if not prog.bytes_read or self._active_read_buf is None:
            return b""
        return bytes(self._active_read_buf[: prog.bytes_read])

    def _save_partial_read(self, data: bytes, *, osid: str, reason: str) -> Path | None:
        if not data:
            return None
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%y%m%d-%H%M%S")
        tag = osid.replace(" ", "_")[:16] if osid else "E92A"
        sim = "_SIM" if self._simulation() else ""
        prog = self.last_read_progress
        out = (
            DEFAULT_OUTPUT_DIR
            / f"E92_{tag}_PARTIAL{sim}_{prog.pct}pct_{ts}.bin"
        )
        out.write_bytes(data)
        self.log(f"Partial dump ({len(data):,} B, {reason}) → {out}")
        return out

    def read_flash_only(
        self,
        output_path: Path | None = None,
        *,
        verify_os: int | None = EXPECTED_OS_ID,
    ) -> ReadResult:
        """Dump flash when the read kernel is already alive (no re-unlock)."""
        if not self.is_kernel_alive(1.0):
            return ReadResult(ok=False, error="Kernel not alive — run Upload Kernel or FULL READ first")
        return self.full_read(output_path=output_path, verify_os=verify_os)

    def _verify_double_read(
        self,
        first: bytes,
        *,
        verify_os: int | None,
        vin: str,
        osid: str,
    ) -> ReadResult:
        """Second full pass; must match first byte-for-byte."""
        self.log("VERIFY: second 4 MB pass (~7 min) — comparing to first read…")
        self._read_pass_number = 2
        self._read_phase = "verify"
        self.last_read_progress = ReadProgress(
            pass_number=2,
            pass_total=self._read_pass_total,
            phase="verify",
        )
        second, checksum2, skipped2 = self.read_flash()
        if len(first) != len(second):
            raise RuntimeError(
                f"Double-read length mismatch {len(first):,} vs {len(second):,}"
            )
        mismatch = -1
        for i, (a, b) in enumerate(zip(first, second)):
            if a != b:
                mismatch = i
                break
        if mismatch >= 0:
            raise RuntimeError(
                f"Double-read mismatch at 0x{mismatch:X} "
                f"({first[mismatch]:02X} vs {second[mismatch]:02X})"
            )
        c1 = sum(first) & 0xFFFFFFFF
        self.log(
            f"VERIFY: PASS — both passes identical ({len(first):,} B, "
            f"checksum 0x{c1:08X}, skipped={skipped2})"
        )
        return ReadResult(
            ok=True,
            data=first,
            checksum=c1,
            os_id=osid,
            vin=vin,
            skipped_blocks=skipped2,
        )

    def full_read(
        self,
        output_path: Path | None = None,
        *,
        verify_os: int | None = EXPECTED_OS_ID,
        verify_double: bool = False,
    ) -> ReadResult:
        vin = ""
        osid = ""
        try:
            self.last_read_progress = ReadProgress()
            if not self._simulation() and not self.check_battery_voltage():
                result = ReadResult(ok=False, error="Battery voltage too low for unlock/read")
                self.last_read_result = result
                return result
            if self.is_kernel_alive(1.0):
                vin = self._cached_vin or EXPECTED_VIN_PREFIX
                osid = self._cached_osid or str(EXPECTED_OS_ID)
                self.log(
                    "Kernel already alive — skipping session reset, probe, and re-upload. "
                    f"VIN={vin} OSID={osid}"
                )
            else:
                self._prepare_session()
                vin, osid = self.probe_identity()
                if verify_os and osid:
                    digits = "".join(c for c in osid if c.isdigit())
                    if digits and str(verify_os) not in digits and digits[:8] != str(verify_os):
                        self.log(f"WARN: OSID {osid} does not match expected {verify_os}")

                if not self.upload_kernel():
                    result = ReadResult(ok=False, error="Kernel upload failed", vin=vin, os_id=osid)
                    self.last_read_result = result
                    return result

            self._read_pass_total = 2 if verify_double and not self._simulation() else 1
            self._read_pass_number = 1
            self._read_phase = "read"
            if verify_double and not self._simulation():
                self.log("Double-read verify ON — expect ~14 min total on vehicle bus")
            if self._simulation():
                self.log(f"Reading 0x{FLASH_SIZE:X} bytes (SIM — fast local drill)...")
            else:
                eta = "~14 min" if verify_double else "~7 min"
                self.log(f"Reading 0x{FLASH_SIZE:X} bytes ({eta})...")
            data, checksum, skipped = self.read_flash()
            if verify_double:
                verified = self._verify_double_read(
                    data, verify_os=verify_os, vin=vin, osid=osid
                )
                data = verified.data
                checksum = verified.checksum
                skipped = verified.skipped_blocks
            self.ecu_reset()

            out = output_path
            if out is None:
                DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                ts = time.strftime("%y%m%d-%H%M%S")
                tag = osid.replace(" ", "_")[:16] if osid else "E92A"
                sim = "_SIM" if self._simulation() else ""
                vtag = "_VERIFY2" if verify_double and not self._simulation() else ""
                out = DEFAULT_OUTPUT_DIR / f"E92_{tag}_FULLREAD{sim}{vtag}_{ts}.bin"

            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            self.log(f"Saved {len(data):,} bytes → {out}")
            self.log(f"Checksum: 0x{checksum:08X}  skipped_blocks={skipped}")

            result = ReadResult(
                ok=True,
                data=data,
                path=out,
                checksum=checksum,
                os_id=osid,
                vin=vin,
                skipped_blocks=skipped,
                region="flash",
            )
            self.last_read_result = result
            return result
        except InterruptedError as e:
            partial_path = self._save_partial_read(
                self._partial_read_bytes(), osid=osid, reason="cancelled"
            )
            result = ReadResult(
                ok=False,
                error=str(e),
                vin=vin,
                os_id=osid,
                partial_path=partial_path,
                region="flash",
            )
            self.last_read_result = result
            return result
        except Exception as e:
            partial_path = None
            if self.last_read_progress.bytes_read:
                partial_path = self._save_partial_read(
                    self._partial_read_bytes(),
                    osid=osid,
                    reason=type(e).__name__,
                )
            result = ReadResult(
                ok=False,
                error=f"{type(e).__name__}: {e}",
                vin=vin,
                os_id=osid,
                partial_path=partial_path,
                region="flash",
            )
            self.last_read_result = result
            return result
        finally:
            self.uds.flush()
            if not self._simulation() and self._kernel_alive:
                self.log("Kernel still in RAM — recovering ECM to stock OS (required before key OFF).")
                self.recover_to_stock()
                self.log("If the car will not start: key OFF, disconnect battery 10+ minutes.")

    def full_read_shadow(
        self,
        output_path: Path | None = None,
        *,
        verify_os: int | None = EXPECTED_OS_ID,
        keep_kernel: bool = False,
    ) -> ReadResult:
        """Shadow-region read (read-only) — 16 KiB @ 0x00FFC000.

        Same path as a full flash read: programming session → unlock →
        RAM read kernel → UDS $23 over the shadow map.

        Does not program NVPWD or erase/program flash.

        ``keep_kernel=True`` skips post-read ECM recovery so a full read can
        follow in the same session (recover before key-off).
        """
        vin = ""
        osid = ""
        try:
            self.last_read_progress = ReadProgress(
                total_bytes=SHADOW_SIZE, phase="shadow_read"
            )
            if not self._simulation() and not self.check_battery_voltage():
                result = ReadResult(
                    ok=False,
                    error="Battery voltage too low for unlock/read",
                    region="shadow",
                )
                self.last_read_result = result
                return result

            if self.is_kernel_alive(1.0):
                vin = self._cached_vin or EXPECTED_VIN_PREFIX
                osid = self._cached_osid or str(EXPECTED_OS_ID)
                self.log(
                    "Kernel already alive — shadow read will reuse session. "
                    f"VIN={vin} OSID={osid}"
                )
            else:
                self._prepare_session()
                vin, osid = self.probe_identity()
                if verify_os and osid:
                    digits = "".join(c for c in osid if c.isdigit())
                    if digits and str(verify_os) not in digits and digits[:8] != str(verify_os):
                        self.log(f"WARN: OSID {osid} does not match expected {verify_os}")
                if not self.upload_kernel():
                    result = ReadResult(
                        ok=False,
                        error="Kernel upload failed",
                        vin=vin,
                        os_id=osid,
                        region="shadow",
                    )
                    self.last_read_result = result
                    return result

            self._read_pass_total = 1
            self._read_pass_number = 1
            self._read_phase = "shadow_read"
            self.log(
                f"Shadow READ: 0x{SHADOW_BASE:08X}+0x{SHADOW_SIZE:X} "
                f"({SHADOW_SIZE:,} B) — read-only, no NVPWD write"
            )
            data, checksum, skipped = self.read_flash(
                start=SHADOW_BASE, length=SHADOW_SIZE, progress_every=4096
            )
            if len(data) != SHADOW_SIZE:
                raise RuntimeError(
                    f"Shadow length mismatch: got {len(data)}, expected {SHADOW_SIZE}"
                )

            nvpwd_hex = ""
            nvpwd_class = ""
            try:
                cens = parse_shadow_censorship(data)
                nvpwd_hex = getattr(cens, "hex_spaced", None) or " ".join(
                    f"{b:02X}" for b in cens.raw
                )
                nvpwd_class = cens.label
                self.log(
                    f"NVPWD @ 0x{NVPWD_CPU_ADDR:08X}: {nvpwd_hex}  class={nvpwd_class}"
                )
            except Exception as exc:
                self.log(f"WARN: NVPWD parse failed: {exc}")

            out = output_path
            if out is None:
                DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                ts = time.strftime("%y%m%d-%H%M%S")
                tag = osid.replace(" ", "_")[:16] if osid else "E92A"
                sim = "_SIM" if self._simulation() else ""
                out = DEFAULT_OUTPUT_DIR / f"E92_{tag}_SHADOW{sim}_{ts}.bin"

            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            self.log(f"Saved shadow {len(data):,} bytes → {out}")
            self.log(f"Checksum: 0x{checksum:08X}  skipped_blocks={skipped}")

            # Sidecar JSON next to the dump
            import json
            from datetime import datetime, timezone

            sidecar = out.with_suffix(out.suffix + ".meta.json")
            meta = {
                "schema": "e92_shadow_read_v1",
                "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "region": "shadow",
                "cpu_base": f"0x{SHADOW_BASE:08X}",
                "size": SHADOW_SIZE,
                "nvpwd_cpu": f"0x{NVPWD_CPU_ADDR:08X}",
                "nvpwd_hex": nvpwd_hex,
                "nvpwd_class": nvpwd_class,
                "os_id": osid,
                "vin": vin,
                "path": str(out),
                "checksum": f"0x{checksum:08X}",
                "skipped_blocks": skipped,
                "read_only": True,
                "map": "shadow",
                "procedure": "session+unlock+E92 read kernel+$23 @ shadow base",
            }
            sidecar.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
            self.log(f"Sidecar → {sidecar}")

            if not keep_kernel and not self._simulation():
                self.ecu_reset()

            result = ReadResult(
                ok=True,
                data=data,
                path=out,
                checksum=checksum,
                os_id=osid,
                vin=vin,
                skipped_blocks=skipped,
                region="shadow",
                nvpwd_hex=nvpwd_hex,
                nvpwd_class=nvpwd_class,
                sidecar_path=sidecar,
            )
            self.last_read_result = result
            return result
        except InterruptedError as e:
            result = ReadResult(
                ok=False,
                error=str(e),
                vin=vin,
                os_id=osid,
                region="shadow",
            )
            self.last_read_result = result
            return result
        except Exception as e:
            result = ReadResult(
                ok=False,
                error=f"{type(e).__name__}: {e}",
                vin=vin,
                os_id=osid,
                region="shadow",
            )
            self.last_read_result = result
            return result
        finally:
            self.uds.flush()
            if (
                not keep_kernel
                and not self._simulation()
                and self._kernel_alive
            ):
                self.log(
                    "Kernel still in RAM after shadow read — recovering ECM to stock OS."
                )
                self.recover_to_stock()


def open_kvaser_bus(channel: int = 0, bitrate: int = 500_000) -> can.BusABC:
    return can.Bus(interface="kvaser", channel=channel, bitrate=bitrate, receive_own_messages=False)


def is_simulation_bus(bus: can.BusABC | None) -> bool:
    """True when ``bus`` is an optional in-process simulator adapter."""
    return _is_sim_bus(bus)


def bench_test_kvaser(channel: int = 0, bitrate: int = 500_000, log: LogFn | None = None) -> bool:
    """Open bus and send a harmless OBD broadcast (no ECU required)."""
    _log = log or print
    try:
        bus = open_kvaser_bus(channel, bitrate)
        msg = can.Message(
            arbitration_id=0x7DF,
            data=[0x02, 0x01, 0x0C, 0, 0, 0, 0, 0],
            is_extended_id=False,
        )
        bus.send(msg)
        _log(f"adapter ch={channel} @ {bitrate}: send OK")
        bus.shutdown()
        return True
    except Exception as e:
        _log(f"adapter bench test failed: {e}")
        return False