"""Write surface — EARLY E92 dest-gate only.

Public write uses this product's SRAM helper (SCPB-W1). LATE modules,
boot / VIN / 0x1F000, and any path that is not raw CAN stay refused.
"""

from __future__ import annotations

from dataclasses import dataclass


WRITE_SHIPPED = True


class WriteBlocked(RuntimeError):
    """Write refused. Never treat this as a retryable I/O error."""


@dataclass(frozen=True)
class WriteGate:
    id: str
    title: str
    ready: bool
    detail: str


GATES: tuple[WriteGate, ...] = (
    WriteGate(
        "dual_module_erase",
        "Dual-module C90FL erase",
        True,
        "HAS erase is MOD1 then MOD0. Interlock at base and base+16.",
    ),
    WriteGate(
        "c90fl_mcr_bits",
        "C90FL MCR bit positions",
        True,
        "DONE=bit10, PEG=bit9, ERS=bit2.",
    ),
    WriteGate(
        "has_hsr_512k",
        "HAS HSR stride 512 KiB",
        True,
        "Kernel uses >>19. >>18 is refused.",
    ),
    WriteGate(
        "own_write_kernel",
        "Own write kernel (SCPB-W1)",
        True,
        "This product uploads its own SRAM helper. Read and write kernels stay exclusive.",
    ),
    WriteGate(
        "late_commit",
        "Programmed NOR, not transfer ACK",
        True,
        "Host waits for the SCPB dest ack after each $6B/$6C. Transfer ACK is not enough.",
    ),
    WriteGate(
        "kernel_mutex",
        "Read and write kernels exclusive",
        True,
        "Write closes the OBD session, uploads W1 only, then resets to stock OS.",
    ),
    WriteGate(
        "one_dest",
        "One dest at a time",
        True,
        "One dest per kernel. Dest 2+ reuse is not dump-matched. "
        "Reset to stock after each dest. Power-cycle B+ if the helper is silent.",
    ),
    WriteGate(
        "metal_signoff",
        "EARLY dest-gate",
        True,
        "LAS, MAS, OS MID, and HAS dests are enabled for EARLY modules. "
        "Boot, VIN, 0x1F000, and LATE stay refused.",
    ),
)


def write_status() -> dict:
    return {
        "shipped": WRITE_SHIPPED,
        "blocked": not WRITE_SHIPPED,
        "summary": (
            "EARLY E92 flash write (SCPB-W1) on proven dests."
            if WRITE_SHIPPED
            else "Flash write is reserved and disabled."
        ),
        "gates": [
            {
                "id": g.id,
                "title": g.title,
                "ready": g.ready,
                "detail": g.detail,
            }
            for g in GATES
        ],
    }


def request_write(*args, **kwargs):
    """Public write entry. Empty calls stay blocked so a miswired button cannot flash."""
    if not WRITE_SHIPPED:
        raise WriteBlocked("Flash write is not shipped in this product.")
    if not args and not kwargs:
        raise WriteBlocked("No write image.")
    if kwargs.get("dest") is None and (not args or args[0] is None):
        raise WriteBlocked("One dest per kernel.")
    from scantool_public.features.e92_write import execute_write

    return execute_write(*args, **kwargs)


def assert_read_only() -> None:
    """Read path must not share a kernel file with write."""
    from scantool_public.paths import READ_KERNEL, WRITE_KERNEL

    if READ_KERNEL.resolve() == WRITE_KERNEL.resolve():
        raise WriteBlocked("Read kernel path is the write kernel.")
