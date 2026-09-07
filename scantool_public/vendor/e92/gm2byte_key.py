"""2-byte seed-key derivation (early E92 / pre-2017 ECMs, algo 513).

Bench vector: seed 0x8B3A -> key 0x22DC.
"""

from __future__ import annotations

from typing import Sequence

ALGO_E92_EARLY = 513

# Algo 513 bytecode (13 bytes).
E92_EARLY_ALGO_513: tuple[int, ...] = (
    0xFE,
    0x7E,
    0xA4,
    0x42,
    0x6B,
    0xFD,
    0x04,
    0x75,
    0x37,
    0xFF,
    0x2A,
    0xFE,
    0x40,
)

BENCH_SEED_E92_EARLY = 0x8B3A
BENCH_KEY_E92_EARLY = 0x22DC


def compute_key(seed: int, algo_data: Sequence[int] | None = None) -> int:
    """Compute 16-bit key from 16-bit seed using 13-byte bytecode program."""
    if seed == 0xFFFF:
        return 0xFFFF
    algo = list(algo_data or E92_EARLY_ALGO_513)
    if len(algo) != 13:
        raise ValueError(f"algo bytecode must be 13 bytes, got {len(algo)}")

    key = seed & 0xFFFF

    def op_rol8(_hb: int, _lb: int) -> None:
        nonlocal key
        key = ((key << 8) & 0xFF00) | ((key >> 8) & 0x00FF)

    def op_add(hb: int, lb: int) -> None:
        nonlocal key
        key = (key + ((hb << 8) | lb)) & 0xFFFF

    def op_comp(hb: int, lb: int) -> None:
        nonlocal key
        if hb >= lb:
            key = (~key) & 0xFFFF
        else:
            key = ((~key) + 1) & 0xFFFF

    def op_rot_lt(hb: int, _lb: int) -> None:
        nonlocal key
        key = ((key << hb) | (key >> (16 - hb))) & 0xFFFF

    def op_rot_rt(_hb: int, lb: int) -> None:
        nonlocal key
        key = ((key >> lb) | (key << (16 - lb))) & 0xFFFF

    def op_sub(hb: int, lb: int) -> None:
        nonlocal key
        key = (key - ((hb << 8) | lb)) & 0xFFFF

    def op_swap_add(hb: int, lb: int) -> None:
        nonlocal key
        swapped = ((key & 0xFF00) >> 8) | ((key & 0xFF) << 8)
        arg = (hb << 8) | lb if hb >= lb else (lb << 8) | hb
        key = (swapped + arg) & 0xFFFF

    def op_swap_arg_or(hb: int, lb: int) -> None:
        nonlocal key
        key = (key | ((lb << 8) | hb)) & 0xFFFF

    def op_swap_arg_add(hb: int, lb: int) -> None:
        nonlocal key
        key = (key + ((lb << 8) | hb)) & 0xFFFF

    def op_swap_arg_sub(hb: int, lb: int) -> None:
        nonlocal key
        key = (key - ((lb << 8) | hb)) & 0xFFFF

    opcodes = {
        0x05: op_rol8,
        0x14: op_add,
        0x2A: op_comp,
        0x37: op_swap_arg_add,
        0x4C: op_rot_lt,
        0x52: op_swap_arg_or,
        0x6B: op_rot_rt,
        0x75: op_swap_arg_add,
        0x7E: op_swap_add,
        0x98: op_sub,
        0xF8: op_swap_arg_sub,
    }

    byte1 = 1
    while True:
        opcode = algo[byte1]
        if opcode in opcodes:
            opcodes[opcode](algo[byte1 + 1], algo[byte1 + 2])
        if byte1 >= 10:
            break
        byte1 += 3
    return key


def derive_e92_early_key(seed: int | bytes) -> int:
    """Return 16-bit unlock key for E92 EARLY (algo 513)."""
    if isinstance(seed, (bytes, bytearray)):
        if len(seed) < 2:
            raise ValueError("EARLY seed must be at least 2 bytes")
        seed16 = (seed[0] << 8) | seed[1]
    else:
        seed16 = int(seed) & 0xFFFF
    return compute_key(seed16, E92_EARLY_ALGO_513)