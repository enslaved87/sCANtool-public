"""C90FL / HAS map used by SCPB-W1. Kept in Python so tests can check
the same numbers without uploading anything."""

from __future__ import annotations

FMC0 = 0xC3F88000
FMC1 = 0xC3F8C000
HAS_BASE = 0x100000
HAS_STRIDE = 0x80000  # 512 KiB logical
MCR_DONE = 0x400
MCR_PEG = 0x200
MCR_ERS = 0x4
MCR_EHV = 0x1
MCR_PGM = 0x10


def select_bits(addr: int) -> int:
    addr &= 0xFFFFFFFF
    if addr < 0x20000:
        return 1 << (addr >> 14)
    if addr < 0x30000:
        return 1 << 8
    if addr < 0x40000:
        return 1 << 9
    if addr < 0x60000:
        return 1 << 16
    if addr < 0x80000:
        return 1 << 17
    if addr < 0xC0000:
        return 1  # MID0, MOD1 LMSR bit 0
    if addr < HAS_BASE:
        return 1 << 16  # Flash B M0 MSEL0; stock W1 blob still has li r3,2
    return 1 << ((addr - HAS_BASE) >> 19)


def primary_fmc(addr: int) -> int:
    if addr < 0x80000:
        return FMC0
    if addr < HAS_BASE:
        return FMC1
    return FMC0


def hsr_index(addr: int) -> int:
    return (addr - HAS_BASE) >> 19
