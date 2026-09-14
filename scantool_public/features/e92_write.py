"""EARLY E92 flash write using this product's SCPB-W1 SRAM helper.

One dest at a time on the live helper. Boot, VIN page, and 0x1F000 are refused.
HAS dests 0x100000–0x380000 are enabled (HAS_PUBLIC_GO).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from scantool_public.features.write_gate import WriteBlocked
from scantool_public.paths import WRITE_KERNEL, WRITES_DIR, add_vendor_to_path, ensure_user_dirs

FLASH_SIZE = 0x400000
CHUNK = 0x1000
MAS_55AA_OFF = 0xBFFF8
MAS_55AA = b"\x55\xAA"
VIN_ADDR = 0x100B4
KERNEL_LOAD_ADDR = 0x40001000
TESTER_ID = 0x7E0
ECU_ID = 0x7E8
# Host pacing. Kernel copies $6C into SRAM then programs after the last
# 8 B — ACK is one SCPB at the end. Unpaced 512-frame blast overflowed
# Leaf TX FIFO (2026-08). Old 3 ms/frame + 300 ms cmd + per-frame recv
# made write-entire ~90 min. Send-all, then one ack.
# Budgets: cal 128 chunks < ~5 min wall; write-entire 960 chunks < ~15 min
# (erase + kernel $23 verify still have to fit).
CMD_GAP_S = 0.05
FRAME_GAP_S = 0.0005
BURST_GAP_S = 0.008
BURST_EVERY = 64
SKIP_TAIL_OFF = 0xF800
SKIP_TAIL_SIZE = 0x800
# This reader's class holes (subset of skip_tail_windows). R2 $23 fills them 0xFF.
SKIP_TAIL_CLASS = (0x11F800, 0x3FF800)
# KernelMPC5674F: E92 byte-load machine-check windows. Not the 2 KiB …F800
# crash-guard. R2 still skips whole tails until a one-dest self-read of
# 0x2F800 (excluding these 8 B) is proven on metal.
ECC_HOLES: tuple[tuple[int, int], ...] = ((0x0001FFF8, 8), (0x0002FFF8, 8))


def in_ecc_hole(addr: int) -> bool:
    for lo, n in ECC_HOLES:
        if lo <= int(addr) < lo + n:
            return True
    return False
READ_N = 2048
HUNG_KERNEL_MSG = (
    "Hung kernel: power-cycle B+ 8-10 s. Software reset is useless while the helper is silent."
)
# Dual-module HAS dests (0x100000–0x380000). Off refuses them.
HAS_PUBLIC_GO = True
EARLY_WRITE_GO = True
LATE_WRITE_GO = True
# Dest 2+ keeps the live W1 (Write entire / calibration). Kernel zeros
# LMSR+HSR before each $6B/$6C so leftover select bits cannot over-erase
# neighbors (HAS H0 2026-09-06). Reset to stock after the last dest.
ALLOW_REUSE_KERNEL = True

LogFn = Callable[[str], None]


@dataclass(frozen=True)
class Dest:
    addr: int
    size: int
    name: str
    dual_module: bool = False


# VIN lives at 0x100B4 in the 0x10000 sector. Clone writes this page only;
# it stops before 0x1F000 (mute/brick class). Boot 0x0 stays out.
VIN_DEST = Dest(0x10000, 0xF000, "VIN 0x10000")

PROVEN_DESTS: tuple[Dest, ...] = (
    Dest(0x40000, 0x20000, "LAS 0x40000"),
    Dest(0x60000, 0x20000, "LAS 0x60000"),
    Dest(0x80000, 0x40000, "MAS 0x80000"),
    Dest(0xC0000, 0x40000, "OS MID 0xC0000"),
    Dest(0x100000, 0x80000, "HAS H0", True),
    Dest(0x180000, 0x80000, "HAS H1", True),
    Dest(0x200000, 0x80000, "HAS H2", True),
    Dest(0x280000, 0x80000, "HAS H3", True),
    Dest(0x300000, 0x80000, "HAS H4", True),
    Dest(0x380000, 0x80000, "HAS H5", True),
)


@dataclass
class WritePlan:
    ok: bool
    dests: tuple[Dest, ...]
    image: bytes
    variant: str


@dataclass
class WriteOutcome:
    ok: bool
    error: str = ""
    dests_done: int = 0
    path: str = ""
    preread: bytes = b""


def write_kernel_present() -> bool:
    return WRITE_KERNEL.is_file() and WRITE_KERNEL.stat().st_size > 100


def dest_covers_flash(dests: tuple[Dest, ...] | list[Dest]) -> bool:
    pos = 0x40000
    for dest in sorted(dests, key=lambda d: d.addr):
        if dest.addr != pos:
            return False
        pos += dest.size
    return pos == FLASH_SIZE


def dest_by_addr(addr: Dest | int, *, clone: bool = False) -> Dest:
    if isinstance(addr, Dest):
        addr = addr.addr
    if isinstance(addr, (tuple, list)):
        raise WriteBlocked("One dest per kernel.")
    try:
        want = int(addr)
    except (TypeError, ValueError) as exc:
        raise WriteBlocked("One dest per kernel.") from exc
    if want == VIN_DEST.addr:
        if not clone:
            raise WriteBlocked("VIN page is clone-only.")
        return VIN_DEST
    if want < 0x40000 or want == 0x1F000:
        raise WriteBlocked(f"Refused dest 0x{want:X}.")
    for dest in PROVEN_DESTS:
        if dest.addr == want:
            return dest
    raise WriteBlocked(f"Refused dest 0x{want:X}.")


def writable_dests() -> tuple[Dest, ...]:
    if HAS_PUBLIC_GO:
        return PROVEN_DESTS
    return tuple(d for d in PROVEN_DESTS if not d.dual_module)


def calibration_dests() -> tuple[Dest, ...]:
    """LAS 0x40000 / 0x60000 + MAS — what tuners call write calibration."""
    want = {0x40000, 0x60000, 0x80000}
    return tuple(d for d in writable_dests() if d.addr in want)


def entire_dests() -> tuple[Dest, ...]:
    """Cal + OS MID + HAS. Boot / VIN / 0x1F000 stay out."""
    dests = writable_dests()
    if not dest_covers_flash(dests):
        raise WriteBlocked("Write entire dests do not tile 0x40000–4 MiB.")
    return dests


def clone_dests() -> tuple[Dest, ...]:
    """Write entire plus VIN page. Boot, 0x1F000, and 0x20000–0x3FFFF stay out."""
    return tuple(entire_dests()) + (VIN_DEST,)


def clone_scope() -> dict:
    """What Clone to this ECU writes. Not a full-chip copy.

    SCPB-W1 has no proven boot dest. Metal $6C at 0x1F000 hangs (no dest
    ACK). 0x20000 is refused with this helper. Immobilizer lives in BCM.
    """
    dests = clone_dests()
    return {
        "full_chip": False,
        "dests": dests,
        "writes": (
            "Calibration (LAS 0x40000 / 0x60000 + MAS)",
            "Operating system (OS MID 0xC0000)",
            "High flash (HAS 0x100000–end of chip)",
            "VIN page from the image (0x10000–0x1EFFF)",
        ),
        "does_not_write": (
            "Boot block (0x00000–0x0FFFF) — no proven dest on this helper",
            "4 KiB at 0x1F000 — this helper does not complete that dest",
            "Low flash 0x20000–0x3FFFF — not a proven dest on this helper",
            "Immobilizer / BCM — a different module on the vehicle",
        ),
        "need": (
            "The spare must already be the same EARLY or LATE E92 family.",
            "The spare does not need the same OS ID or VIN — those come from the image.",
            "After clone, pair the vehicle BCM or the engine may not start.",
        ),
        "impossible": (
            "A full-chip copy is not possible with this write helper.",
            "A different-family module cannot be turned into this one.",
        ),
        "summary": (
            "Clone is not a full-chip copy. It writes calibration, OS, HAS, "
            "and VIN from the image. It does not write boot, 0x1F000, "
            "0x20000–0x3FFFF, or the immobilizer/BCM. The spare must already "
            "be the same EARLY or LATE family."
        ),
    }


def clone_confirm_text(*, minutes: tuple[int, int] | None = None) -> str:
    scope = clone_scope()
    lines = [
        scope["summary"],
        "",
        "Writes:",
        *[f"  • {x}" for x in scope["writes"]],
        "",
        "Does not write:",
        *[f"  • {x}" for x in scope["does_not_write"]],
        "",
        "Required:",
        *[f"  • {x}" for x in scope["need"]],
        "",
        "Not possible:",
        *[f"  • {x}" for x in scope["impossible"]],
        "",
        "Dests in this job:",
        *[f"  • {d.name}  {d.size // 1024} KiB" for d in scope["dests"]],
    ]
    if minutes:
        lo, hi = minutes
        lines.append(
            f"\nExpect about {lo}–{hi} minutes. Solid B+. Do not key-off."
        )
    return "\n".join(lines)


def describe_image(image: bytes) -> dict:
    """Preview for the Write tab. No bus."""
    vin = image_vin(image)
    holes = skip_tail_holes(image) if len(image) == FLASH_SIZE else ()
    cal = ""
    if len(image) >= 0x40018:
        raw = bytes(image[0x40010:0x40018])
        cal = "".join(chr(b) for b in raw if 0x30 <= b <= 0x39)
    os_ascii = ""
    if len(image) >= 0xC0118:
        raw = bytes(image[0xC0110:0xC0118])
        os_ascii = "".join(chr(b) for b in raw if 0x30 <= b <= 0x39)
    lo_c, hi_c = estimate_write_minutes(calibration_dests())
    try:
        lo_e, hi_e = estimate_write_minutes(entire_dests())
    except WriteBlocked:
        lo_e, hi_e = 12, 16
    try:
        lo_k, hi_k = estimate_write_minutes(clone_dests())
    except WriteBlocked:
        lo_k, hi_k = lo_e, hi_e + 1
    return {
        "vin": vin,
        "cal_ascii": cal,
        "os_ascii": os_ascii,
        "skip_tails": len(holes),
        "skip_tail_splice": bool(holes),
        "cal_minutes": (lo_c, hi_c),
        "entire_minutes": (lo_e, hi_e),
        "clone_minutes": (lo_k, hi_k),
        "clone_full_chip": False,
        "size": len(image),
    }


def image_vin(image: bytes) -> str:
    if len(image) < VIN_ADDR + 17:
        return ""
    raw = bytes(image[VIN_ADDR : VIN_ADDR + 17])
    if raw == b"\xff" * 17 or raw == b"\x00" * 17:
        return ""
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return ""
    vin = "".join(c for c in text if c.isalnum())
    return vin.upper() if len(vin) == 17 else ""


def image_ecu_warnings(image: bytes, live_vin: str, live_osid: str) -> tuple[str, ...]:
    """Wrong-file checks. Boot is not written, so OS/boot pairing matters."""
    out: list[str] = []
    img_vin = image_vin(image)
    live = "".join(c for c in (live_vin or "") if c.isalnum()).upper()
    if len(live) == 17:
        if not img_vin:
            out.append(f"Image has no VIN at 0x{VIN_ADDR:X}; ECU VIN is {live}.")
        elif img_vin != live:
            out.append(f"Image VIN {img_vin} != ECU VIN {live}.")
    osid = (live_osid or "").strip()
    if len(osid) >= 7:
        needle = osid.encode("ascii", "ignore")
        if needle and needle not in image:
            out.append(
                f"ECU CAL {osid} is not in this image. Boot on the ECU is not rewritten."
            )
    return tuple(out)


def estimate_write_minutes(dests: tuple[Dest, ...] | list[Dest]) -> tuple[int, int]:
    """Host-side band. HAS erase/program dominates the upper end."""
    chunks = sum(max(1, d.size // CHUNK) for d in dests)
    host_s = chunks * chunk_host_s()
    host_s += sum(40.0 if d.dual_module else 8.0 for d in dests)
    lo = max(1, int(host_s / 60))
    hi = max(lo + 1, int(host_s * 2.2 / 60) + (8 if any(d.dual_module for d in dests) else 2))
    return lo, hi


def blank_probe_addrs(dest: Dest) -> tuple[int, ...]:
    if dest.dual_module:
        return (dest.addr, dest.addr + 0x10)
    return (dest.addr,)


def neighbor_probes(dest: Dest) -> tuple[tuple[str, int, int], ...]:
    items = [
        ("boot", 0x0, 16),
        ("vin", VIN_ADDR, 17),
        ("las_40000", 0x40000, 16),
        ("las_60000", 0x60000, 16),
        ("mas", 0x80000, 16),
        ("bfff8", MAS_55AA_OFF, 2),
        ("mid", 0xC0000, 16),
        ("has_h0", 0x100000, 16),
    ]
    lo, hi = dest.addr, dest.addr + dest.size
    out: list[tuple[str, int, int]] = []
    for name, addr, n in items:
        if lo <= addr < hi:
            continue
        out.append((name, addr, n))
    if dest.dual_module:
        prev_has = dest.addr - dest.size
        nxt = dest.addr + dest.size
        if prev_has >= 0x100000:
            out.append((f"has_{prev_has:x}", prev_has, 16))
        if nxt < FLASH_SIZE:
            out.append((f"has_{nxt:x}", nxt, 16))
    return tuple(out)


def skip_tail_windows(lo: int = 0x10000, hi: int = FLASH_SIZE) -> tuple[int, ...]:
    """Last 2 KiB of each 64 KiB window. SCPB-R2 $23 fills these 0xFF."""
    if hi <= lo:
        return ()
    out: list[int] = []
    window = lo & ~0xFFFF
    if window < 0x10000:
        window = 0x10000
    while window < hi:
        tail = window + SKIP_TAIL_OFF
        if lo <= tail < hi:
            out.append(tail)
        window += 0x10000
    return tuple(out)


def _tail_is_reader_hole(block: bytes, tail_addr: int) -> bool:
    """True when the 2 KiB is this reader's 0xFF fill (55AA in MAS does not count)."""
    if len(block) != SKIP_TAIL_SIZE:
        return False
    buf = bytearray(block)
    if tail_addr <= MAS_55AA_OFF < tail_addr + SKIP_TAIL_SIZE:
        i = MAS_55AA_OFF - tail_addr
        buf[i : i + 2] = b"\xff\xff"
    return bytes(buf) == b"\xff" * SKIP_TAIL_SIZE


def skip_tail_holes(image: bytes, lo: int = 0x10000, hi: int | None = None) -> tuple[int, ...]:
    if hi is None:
        hi = min(len(image), FLASH_SIZE)
    found: list[int] = []
    for addr in skip_tail_windows(lo, hi):
        if addr + SKIP_TAIL_SIZE > len(image):
            found.append(addr)
            continue
        if _tail_is_reader_hole(image[addr : addr + SKIP_TAIL_SIZE], addr):
            found.append(addr)
    return tuple(found)


def splice_skip_tails(payload: bytes, dest: Dest, preread: bytes, log: LogFn) -> bytes:
    """Replace this reader's 0xFF …F800 fill with live NOR from W1 preread.

    SCPB-R2 cannot $23 those 2 KiB. SCPB-W1 $23 has no flash_hole, so the
    dest preread is the real tail. Programming the 0xFF fill after $6B
    would blank them.
    """
    if len(payload) != dest.size or len(preread) != dest.size:
        raise WriteBlocked(
            f"{dest.name} splice size payload={len(payload)} preread={len(preread)}"
        )
    buf = bytearray(payload)
    n = 0
    for tail in skip_tail_windows(dest.addr, dest.addr + dest.size):
        off = tail - dest.addr
        if not _tail_is_reader_hole(bytes(buf[off : off + SKIP_TAIL_SIZE]), tail):
            continue
        live = preread[off : off + SKIP_TAIL_SIZE]
        buf[off : off + SKIP_TAIL_SIZE] = live
        n += 1
        if live == b"\xff" * SKIP_TAIL_SIZE:
            log(f"  skip-tail 0x{tail:X} is FF in image and live")
        else:
            log(f"  skip-tail 0x{tail:X} filled from live NOR (reader hole)")
    if n:
        log(f"  spliced {n} skip-tail(s) on {dest.name}")
    return bytes(buf)


def prepare_image(raw: bytes, *, mas_marker: bool = True) -> bytes:
    if len(raw) != FLASH_SIZE:
        raise WriteBlocked("Image must be exactly 4 MiB.")
    holes = skip_tail_holes(raw)
    class_holes = tuple(a for a in SKIP_TAIL_CLASS if a in holes)
    if class_holes:
        raise WriteBlocked(
            "Image looks like an SCPB-R2 skip-tail FULLREAD "
            f"({', '.join(hex(a) for a in class_holes)}). Use a complete 4 MiB dump."
        )
    out = bytearray(raw)
    if mas_marker:
        out[MAS_55AA_OFF : MAS_55AA_OFF + 2] = MAS_55AA
    return bytes(out)


def _is_early(variant: str, seed_len: int) -> bool:
    if seed_len >= 5:
        return False
    if seed_len == 2:
        return True
    return (variant or "").strip().lower() == "early"


def _is_late(variant: str, seed_len: int) -> bool:
    if seed_len == 2:
        return False
    if seed_len >= 5:
        return True
    return (variant or "").strip().lower() == "late"


def _unsupported_write_target(variant: str, vin: str = "") -> bool:
    """Refuse non-E92 passenger VIN families. Not a product name."""
    v = (variant or "").strip().lower()
    vin = (vin or "").strip().upper()
    if v == "unsupported":
        return True
    return len(vin) >= 3 and vin[:3] == "1G1"


def plan_write(
    image: bytes,
    *,
    dest: Dest | int | None = None,
    variant: str,
    seed_len: int = 0,
    clone: bool = False,
) -> WritePlan:
    if dest is None:
        raise WriteBlocked("One dest per kernel.")
    if _unsupported_write_target(variant):
        raise WriteBlocked("Write refused for this module.")
    if _is_late(variant, seed_len):
        if not LATE_WRITE_GO:
            raise WriteBlocked("LATE write is not enabled.")
        kind = "late"
    elif _is_early(variant, seed_len):
        if not EARLY_WRITE_GO:
            raise WriteBlocked("EARLY write is not enabled.")
        kind = "early"
    else:
        raise WriteBlocked("Flash write is EARLY or LATE E92 only.")
    if isinstance(dest, Dest):
        chosen = dest
    else:
        chosen = dest_by_addr(dest, clone=clone)
    prepared = prepare_image(image, mas_marker=(kind == "early" and not clone))
    return WritePlan(ok=True, dests=(chosen,), image=prepared, variant=kind)


def after_erase_decision(
    *,
    dest: Dest,
    blank_ok: bool,
    neighbors_ok: bool,
    stop: bool,
) -> str:
    """What to do after $6B. Neighbor fail is not permission to leave a blank dest."""
    del dest
    if not blank_ok:
        return "abort_no_program"
    if stop or not neighbors_ok:
        return "restore_then_abort"
    return "program"


def abort_restore_plan(
    *,
    erased: bool,
    dirty: bool,
    blank_ok: bool,
    verified: bool,
    has_preread: bool,
    allow_restore: bool = True,
) -> str:
    """What to do when execute_write faults after $6B.

    verified means dump-match passed, or preread was already written back.
    It is not "$6C ACKs arrived" — a dest can be fully transmitted and still
    mismatch. blank_ok is the post-erase probe and is stale once any $6C ran.
    allow_restore is False when $23 itself failed (state unknown — do not touch).
    AND-only NOR cannot overlay preread onto a dest that already saw program.
    """
    if verified or not allow_restore or not erased or not has_preread:
        return "none"
    if dirty:
        return "erase_then_program"
    if blank_ok:
        return "program"
    return "none"


def restore_preread(
    bus,
    dest: Dest,
    preread: bytes,
    plan: str,
    ext,
    log: LogFn,
) -> None:
    """Write preread back. Dirty dests are erased first — AND-only NOR."""
    if plan == "none":
        return
    if plan == "erase_then_program":
        log(f"$6B {dest.name} before preread restore (AND-only NOR)")
        erase_dest(bus, dest.addr, log, timeout_s=_erase_timeout(dest))
        time.sleep(0.4)
        for addr in blank_probe_addrs(dest):
            shot = _read_mem(ext, addr, 16)
            if shot != b"\xff" * 16:
                raise WriteBlocked(
                    f"{dest.name} did not blank before preread restore @ 0x{addr:X}"
                )
    elif plan != "program":
        raise WriteBlocked(f"unknown abort restore plan {plan!r}")
    log(f"$6C {dest.name} from preread after fault")
    program_payload(bus, dest, preread, log)


def kernel_reset_allowed(kernel_alive: bool) -> bool:
    return bool(kernel_alive)


def parse_ack(frame: bytes) -> tuple[bool, int, str]:
    data = bytes(frame or b"")
    if len(data) >= 8 and data[4:8] == b"SCPB":
        addr = int.from_bytes(data[:4], "big")
        return True, addr, "ok"
    if len(data) >= 4 and data[:3] == b"ERR":
        snap = int.from_bytes(data[4:8], "big") if len(data) >= 8 else 0
        return False, snap, f"err {data[3]:02X}"
    return False, 0, "unrecognized"


def _sf(payload: bytes) -> bytes:
    body = bytes([len(payload) & 0x0F]) + payload
    return body.ljust(8, b"\x00")[:8]


def _raw_send(bus, data: bytes) -> None:
    import can

    msg = can.Message(
        arbitration_id=TESTER_ID,
        data=bytes(data)[:8].ljust(8, b"\x00"),
        is_extended_id=False,
    )
    last = None
    for attempt in range(16):
        try:
            bus.send(msg)
            return
        except Exception as exc:
            last = exc
            time.sleep(0.004 * (attempt + 1))
    raise WriteBlocked(f"CAN TX failed: {last}")


def _raw_recv(bus, timeout_s: float) -> bytes:
    end = time.time() + timeout_s
    while time.time() < end:
        remaining = max(0.01, end - time.time())
        msg = bus.recv(timeout=min(0.2, remaining))
        if msg is None:
            continue
        if getattr(msg, "is_error_frame", False):
            continue
        if int(getattr(msg, "arbitration_id", 0)) != ECU_ID:
            continue
        return bytes(msg.data)
    return b""


def _wait_ack(bus, expect_addr: int, timeout_s: float, log: LogFn) -> None:
    raw = _raw_recv(bus, timeout_s)
    ok, addr, why = parse_ack(raw)
    if not ok:
        raise WriteBlocked(f"SCPB ack failed at 0x{expect_addr:X}: {why or 'timeout'}")
    if addr != expect_addr:
        log(f"  ack addr 0x{addr:X} (expected 0x{expect_addr:X})")


def chunk_host_s() -> float:
    """Host-side $6C budget: send every frame, then one SCPB ack.

    Do not recv() per frame. A 15 ms timeout × 512 frames is ~8 s of
    dead wait on an ACK that only arrives after the last store.
    """
    frames = CHUNK // 8
    bursts = frames // BURST_EVERY
    return CMD_GAP_S + frames * FRAME_GAP_S + bursts * BURST_GAP_S


def program_4k(bus, addr: int, chunk: bytes, log: LogFn) -> None:
    """$6C 4 KiB: send every data frame, then one SCPB ack."""
    if len(chunk) != CHUNK:
        raise WriteBlocked(f"chunk {len(chunk)} != {CHUNK}")
    _raw_send(bus, _sf(bytes([0x6C]) + addr.to_bytes(4, "big")))
    time.sleep(CMD_GAP_S)
    for i in range(0, CHUNK, 8):
        _raw_send(bus, chunk[i : i + 8])
        time.sleep(FRAME_GAP_S)
        if ((i // 8) % BURST_EVERY) == BURST_EVERY - 1:
            time.sleep(BURST_GAP_S)
    _wait_ack(bus, addr, 60.0, log)


def erase_dest(bus, addr: int, log: LogFn, timeout_s: float = 60.0) -> None:
    _raw_send(bus, _sf(bytes([0x6B]) + addr.to_bytes(4, "big")))
    _wait_ack(bus, addr, timeout_s, log)


def _read_mem(ext, addr: int, n: int) -> bytes:
    try:
        ext.uds.stack.reset()
    except Exception:
        pass
    try:
        ext.uds.drain_rx(0.05)
    except Exception:
        pass
    out = bytearray()
    left = n
    cur = addr
    while left > 0:
        take = min(left, READ_N)
        ok, data = ext.uds.uds_read_memory_block(cur, take, timeout_s=15.0)
        if not ok or len(data) != take:
            return bytes(out)
        out += data
        cur += take
        left -= take
        time.sleep(0.005)
    return bytes(out)


def program_payload(bus, dest: Dest, payload: bytes, log: LogFn) -> None:
    if len(payload) != dest.size:
        raise WriteBlocked(f"{dest.name} payload {len(payload)} != {dest.size}")
    for off in range(0, dest.size, CHUNK):
        chunk = payload[off : off + CHUNK]
        if len(chunk) != CHUNK:
            raise WriteBlocked(f"short chunk at 0x{dest.addr + off:X}")
        program_4k(bus, dest.addr + off, chunk, log)


def _erase_timeout(dest: Dest) -> float:
    if dest.dual_module:
        return 180.0
    if dest.size >= 0x40000:
        return 120.0
    return 60.0


def rollback_committed_dests(bus, items: list, ext, log: LogFn) -> tuple[int, int]:
    """Erase+program preread for dests already dump-matched in this job.

    Returns (restored, skipped). Skipped means mixed flash may remain —
    re-run the same Write entire/calibration job after B+ cycle to heal.
    """
    if not items:
        return 0, 0
    log(
        f"rolling back {len(items)} dest(s) already programmed — "
        "a mixed OS/HAS image can be a no-start"
    )
    restored = 0
    for dest, blob in reversed(list(items)):
        if not isinstance(dest, Dest):
            dest = dest_by_addr(dest)
        if not blob or len(blob) != dest.size:
            log(f"rollback skip {dest.name}: no preread")
            return restored, len(items) - restored
        try:
            if ext is None or not ext.is_kernel_alive(0.5):
                log(f"rollback {dest.name} skipped. {HUNG_KERNEL_MSG}")
                return restored, len(items) - restored
            log(f"rollback {dest.name} from preread")
            restore_preread(bus, dest, blob, "erase_then_program", ext, log)
            restored += 1
        except Exception as exc:
            log(f"rollback {dest.name} failed: {exc}")
            return restored, len(items) - restored
    return restored, 0


def _rollback_note(restored: int, skipped: int, had_committed: bool) -> str:
    if not had_committed:
        return ""
    if skipped:
        return (
            f" Rolled back {restored} dest(s); {skipped} still on the new image. "
            "Mixed cal/OS/HAS can be a no-start. Power-cycle B+ 8–10 s, then run "
            "Write entire (or Write calibration) again with the same image to repair."
        )
    return f" Rolled back {restored} dest(s) already written."


def reset_to_stock_best_effort(bus, log: LogFn) -> None:
    """$11 from a live W1. Used when a multi-dest job aborts with the helper still up."""
    if bus is None:
        return
    try:
        _raw_send(bus, _sf(bytes([0x11, 0x01])))
        time.sleep(2.0)
    except Exception as exc:
        log(f"reset: {exc}")


def _try_reset_to_stock(ext, bus, log: LogFn) -> None:
    alive = False
    if ext is not None:
        try:
            alive = bool(ext.is_kernel_alive(0.5))
        except Exception:
            alive = False
    if not kernel_reset_allowed(alive):
        log(HUNG_KERNEL_MSG)
        return
    try:
        _raw_send(bus, _sf(bytes([0x11, 0x01])))
        time.sleep(2.0)
    except Exception as exc:
        log(f"reset: {exc}")
    if ext is not None:
        try:
            ext.recover_to_stock()
        except Exception as exc:
            log(f"recover: {exc}")


def execute_write(
    *args,
    image: bytes | None = None,
    path: str | Path | None = None,
    spec: dict | None = None,
    bitrate: int = 500_000,
    variant: str = "early",
    seed_len: int = 0,
    dest: Dest | int | None = None,
    reuse_kernel: bool = False,
    reset: bool = True,
    allow_image_mismatch: bool = False,
    rollback: list | None = None,
    log: LogFn | None = None,
    progress: Callable[[dict], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    bus=None,
    **_kwargs,
) -> WriteOutcome:
    """Program exactly one dest. reuse_kernel keeps W1 for dest 2+ of this job."""
    _log = log or (lambda _m: None)
    stop = stop_check or (lambda: False)
    spec = spec or {}
    if args and image is None and path is None:
        first = args[0]
        if isinstance(first, (bytes, bytearray)):
            image = bytes(first)
        else:
            path = first

    clone = bool(_kwargs.get("clone"))
    restamp = bool(_kwargs.get("restamp"))
    if dest is None:
        raise WriteBlocked("One dest per kernel.")
    if reuse_kernel and not ALLOW_REUSE_KERNEL:
        raise WriteBlocked(
            "One dest per kernel. Dest 2+ reuse is not dump-matched."
        )
    chosen = dest_by_addr(dest, clone=clone)
    if chosen.dual_module and not HAS_PUBLIC_GO:
        raise WriteBlocked(
            f"{chosen.name} refused: HAS_PUBLIC_GO is off. Dual-module HAS "
            "is gated separately from LAS/MAS."
        )

    if image is None:
        if not path:
            raise WriteBlocked("No write image.")
        raw = Path(path).read_bytes()
    else:
        raw = image

    if restamp:
        from scantool_public.features.cal_cs import restamp_calibration_image

        raw, notes = restamp_calibration_image(raw)
        for note in notes:
            _log(note)

    plan = plan_write(
        raw, dest=chosen, variant=variant, seed_len=seed_len, clone=clone
    )
    if len(plan.dests) != 1:
        raise WriteBlocked("One dest per kernel.")
    dest = plan.dests[0]
    if not write_kernel_present():
        raise WriteBlocked(f"Write kernel missing: {WRITE_KERNEL}")

    from scantool_public.transport.adapter import open_raw_bus
    from scantool_public.transport.catalog import supports_e92_read as _raw_ok

    if spec and not _raw_ok(spec):
        raise WriteBlocked("E92 write needs a raw CAN adapter.")

    add_vendor_to_path()
    from ecu_bin_extractor import E92BinExtractor, E92Variant  # type: ignore

    ensure_user_dirs()
    own_bus = bus is None
    ext = None
    erased = False
    blank_ok = False
    verified = False
    dirty = False
    allow_restore = True
    preread = b""
    done = 0
    try:
        def restore_after_fault() -> None:
            # AND-only NOR cannot overlay. Dirty dests re-erase before preread $6C.
            plan = abort_restore_plan(
                erased=erased,
                dirty=dirty,
                blank_ok=blank_ok,
                verified=verified,
                has_preread=bool(preread),
                allow_restore=allow_restore,
            )
            if plan not in ("erase_then_program", "program"):
                if erased and dirty and not verified and not allow_restore:
                    _log(
                        f"{dest.name} not restored: $23 unusable after fault. "
                        "Dest may match neither image. Power-cycle B+ if the helper is silent."
                    )
                return
            try:
                if ext is None or not ext.is_kernel_alive(0.5):
                    _log(
                        f"{dest.name} abort restore skipped (kernel not alive). {HUNG_KERNEL_MSG}"
                    )
                    return
                _log(f"{dest.name} abort restore plan={plan}")
                restore_preread(bus, dest, preread, plan, ext, _log)
            except Exception as rec:
                _log(
                    f"abort restore failed — {dest.name} may be blank. "
                    f"Do not key-off. {rec}"
                )

        if bus is None:
            bus = open_raw_bus(spec, bitrate)
        ext = E92BinExtractor(
            bus,
            log=_log,
            detail_log=_log,
            variant=E92Variant.LATE if plan.variant == "late" else E92Variant.EARLY,
            stop_check=stop,
            kernel_path=WRITE_KERNEL,
        )
        def tick(text: str, pct: int | None = None, **extra) -> None:
            if progress:
                payload = {"dest": dest.name, "text": text, **extra}
                if pct is not None:
                    payload["pct"] = pct
                progress(payload)

        alive = False
        try:
            alive = bool(ext.is_kernel_alive(0.5))
        except Exception:
            alive = False
        if alive and not reuse_kernel:
            raise WriteBlocked(
                "A kernel is already in SRAM. Power-cycle B+ 8–10 s and wait for stock VIN."
            )
        if alive and reuse_kernel:
            _log(f"reusing live write kernel for {dest.name}")
            tick(f"Reusing write kernel for {dest.name}", 3)
        else:
            vin, osid = ext.probe_identity()
            _log(f"identity VIN={vin or '—'}  CAL={osid or '—'}")
            tick(f"Identity VIN={vin or '—'}  CAL={osid or '—'}", 1)
            if _unsupported_write_target(plan.variant, vin or ""):
                raise WriteBlocked("Write refused for this module.")
            warns = image_ecu_warnings(plan.image, vin or "", osid or "")
            for w in warns:
                _log(f"image check: {w}")
            if warns and not allow_image_mismatch:
                raise WriteBlocked("Image does not match this ECU. " + " ".join(warns))
            tick(f"Uploading write kernel for {dest.name}", 3)
            if not ext.upload_kernel(require_early=False):
                raise WriteBlocked("Write kernel did not start.")
        if _unsupported_write_target(plan.variant, getattr(ext, "_cached_vin", "") or ""):
            raise WriteBlocked("Write refused for this module.")
        if stop():
            raise WriteBlocked("Write cancelled.")

        guards: dict[str, bytes] = {}
        for name, addr, n in neighbor_probes(dest):
            shot = _read_mem(ext, addr, n)
            if len(shot) != n:
                raise WriteBlocked(f"{dest.name} guard {name} $23 got {len(shot)}")
            guards[name] = shot

        tick(f"Reading live {dest.name} before erase", 8)
        _log(f"preread {dest.name} @ 0x{dest.addr:X} ({dest.size // 1024} KiB)")
        preread = _read_mem(ext, dest.addr, dest.size)
        if len(preread) != dest.size:
            raise WriteBlocked(f"{dest.name} preread {len(preread)} B — abort without erase")
        if dest.addr <= MAS_55AA_OFF < dest.addr + dest.size:
            buf = bytearray(preread)
            off = MAS_55AA_OFF - dest.addr
            buf[off : off + 2] = MAS_55AA
            preread = bytes(buf)

        tick(f"Erasing {dest.name} @ 0x{dest.addr:X}", 12)
        _log(f"erase {dest.name}")
        erase_dest(bus, dest.addr, _log, timeout_s=_erase_timeout(dest))
        erased = True
        time.sleep(0.4)

        blank_ok = True
        for addr in blank_probe_addrs(dest):
            shot = _read_mem(ext, addr, 16)
            if len(shot) != 16:
                shot = _read_mem(ext, addr, 16)
            if len(shot) != 16:
                blank_ok = False
                allow_restore = False
                raise WriteBlocked(
                    f"{dest.name} blank probe $23 got {len(shot)} B @ 0x{addr:X} — "
                    "dest state unknown, refusing $6C"
                )
            if shot != b"\xff" * 16:
                blank_ok = False
                _log(f"  blank fail @ 0x{addr:X} {shot.hex() if shot else 'empty'}")

        held = True
        for name, addr, n in neighbor_probes(dest):
            now = _read_mem(ext, addr, n)
            if now != guards[name]:
                held = False
                _log(f"  neighbor {name} changed")

        action = after_erase_decision(
            dest=dest,
            blank_ok=blank_ok,
            neighbors_ok=held,
            stop=stop(),
        )
        if action == "abort_no_program":
            raise WriteBlocked(
                f"{dest.name} did not blank after $6B — refusing $6C"
            )
        if action == "restore_then_abort":
            _log(f"$6C {dest.name} from preread before abort")
            dirty = True
            program_payload(bus, dest, preread, _log)
            verified = True
            why = "cancelled" if stop() else "neighbor $23 changed after erase"
            raise WriteBlocked(f"{dest.name} restored from preread ({why})")

        payload = plan.image[dest.addr : dest.addr + dest.size]
        payload = splice_skip_tails(payload, dest, preread, _log)
        if dest.addr <= MAS_55AA_OFF < dest.addr + dest.size:
            buf = bytearray(payload)
            off = MAS_55AA_OFF - dest.addr
            buf[off : off + 2] = MAS_55AA
            payload = bytes(buf)
        total = dest.size
        written = 0
        dirty = True
        for off in range(0, dest.size, CHUNK):
            # Mid-dest cancel still finishes the tile — never leave a blank tail.
            addr = dest.addr + off
            chunk = payload[off : off + CHUNK]
            program_4k(bus, addr, chunk, _log)
            written += CHUNK
            inner = int(written * 100 / total) if total else 0
            tick(
                f"Programming {dest.name}  0x{addr:X}",
                15 + int(inner * 0.75),
                addr=addr,
                dest=dest.name,
                bytes=written,
                total=total,
                inner_pct=inner,
            )
        tick(f"Verifying {dest.name} (programmed NOR, not transfer ACK)", 92)
        # LAS 128 KiB: full dump-match (dest-2+ metal 2026-09-13). Larger
        # dests: 4 KiB head/tail + probes so write-entire stays < ~15 min.
        if dest.size <= 0x20000:
            dump = _read_mem(ext, dest.addr, dest.size)
            if len(dump) != dest.size:
                allow_restore = False
                _log(
                    f"{dest.name} post-read {len(dump)} B of {dest.size} — $23 incomplete. "
                    "Not restoring: NOR may already hold the image. "
                    "Power-cycle B+ if the helper is silent."
                )
                raise WriteBlocked(
                    f"{dest.name} post-read {len(dump)} B of {dest.size} — verify incomplete"
                )
            if dump != payload:
                _log(
                    f"{dest.name} dump-match failed — erase then restore preread (AND-only NOR)"
                )
                raise WriteBlocked(f"{dest.name} post dump-match failed")
            mark_src = dump
        else:
            head = _read_mem(ext, dest.addr, CHUNK)
            tail = _read_mem(ext, dest.addr + dest.size - CHUNK, CHUNK)
            if head != payload[:CHUNK] or tail != payload[-CHUNK:]:
                raise WriteBlocked(f"{dest.name} head/tail $23 mismatch")
            for addr in blank_probe_addrs(dest):
                off = addr - dest.addr
                shot = _read_mem(ext, addr, 16)
                if shot != payload[off : off + 16]:
                    raise WriteBlocked(f"{dest.name} probe $23 mismatch @ 0x{addr:X}")
            mark_src = payload
            _log(f"{dest.name} fast verify (4 KiB head/tail + probes)")
        if dest.addr <= MAS_55AA_OFF < dest.addr + dest.size:
            live_mark = _read_mem(ext, MAS_55AA_OFF, 2)
            mark = live_mark if len(live_mark) == 2 else mark_src[
                MAS_55AA_OFF - dest.addr : MAS_55AA_OFF - dest.addr + 2
            ]
            if mark != MAS_55AA:
                _log(
                    f"{dest.name} lost 55AA at 0xBFFF8 ({mark.hex()}) — "
                    "erase then restore preread (AND-only NOR)"
                )
                raise WriteBlocked(f"{dest.name} lost 55AA at 0xBFFF8 ({mark.hex()})")
        verified = True
        done = 1
        _log(f"programmed {dest.name}")

        if reset:
            tick(f"Resetting {dest.name} to stock OS", 98)
            _log("reset to stock OS")
            _try_reset_to_stock(ext, bus, _log)
        else:
            _log(f"{dest.name} done — write kernel stays resident")
        tick(f"Finished {dest.name}", 100)
        note = ""
        if path:
            note = str(path)
        elif WRITES_DIR:
            note = str(WRITES_DIR)
        return WriteOutcome(ok=True, dests_done=done, path=note, preread=preread)
    except WriteBlocked as exc:
        restore_after_fault()
        n_ok, n_skip = rollback_committed_dests(bus, list(rollback or ()), ext, _log)
        _try_reset_to_stock(ext, bus, _log)
        extra = _rollback_note(n_ok, n_skip, bool(rollback))
        if extra:
            raise WriteBlocked(f"{exc}{extra}") from exc
        raise
    except Exception as exc:
        restore_after_fault()
        n_ok, n_skip = rollback_committed_dests(bus, list(rollback or ()), ext, _log)
        _try_reset_to_stock(ext, bus, _log)
        extra = _rollback_note(n_ok, n_skip, bool(rollback))
        raise WriteBlocked(f"{type(exc).__name__}: {exc}{extra}") from exc
    finally:
        if own_bus and bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass
