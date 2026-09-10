"""5-byte seed-key derivation (AES-128 + SHA-256 pipeline).

Algo 146 (0x92) is late E92 security access.

Bench vector: seed 8785EEC106 -> key 08B3B3656D.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Mapping

ALGO_E92A_LATE = 146
ALGO_T87A = 135

# Algo 146 password blob.
E92A_ALGO_146_BLOB = (
    "01sgqbD6nsKDz8SawCanylLyqwtoFUeMsY2Y6FxEi4rP0A9QCSAP8Ivi0OzQk="
)

def _precomputed_aes_keys_from_blob() -> tuple[bytes, ...]:
    """SHA-256 chain of the algo-146 secret, one AES key per seed[4] 0..10.

    Built at import so the table cannot diverge from `compute_key` by a
    transcribed byte (2026-09-10: old table[8] had E8E5E3DB vs E8E53BDB).
    """
    rec = parse_password_blob(E92A_ALGO_146_BLOB)
    keys: list[bytes] = []
    for tail in range(11):
        max_seed = 255 - tail
        if rec.min_seed > max_seed:
            keys.append(b"\x00" * 16)
            continue
        digest = rec.secret
        for _ in range(max_seed - rec.min_seed):
            digest = hashlib.sha256(digest).digest()
        keys.append(digest[:16])
    return tuple(keys)

BENCH_SEED_E92A = bytes.fromhex("8785EEC106")
BENCH_KEY_E92A = bytes.fromhex("08B3B3656D")

_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)

_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


@dataclass(frozen=True)
class PasswordRecord:
    secret: bytes
    min_seed: int
    algo_id: int


def _bytes_to_state(block: bytes) -> list[list[int]]:
    return [[block[row + 4 * col] for col in range(4)] for row in range(4)]


def _state_to_bytes(state: list[list[int]]) -> bytes:
    out = bytearray(16)
    for row in range(4):
        for col in range(4):
            out[row + 4 * col] = state[row][col]
    return bytes(out)


def _xtime(value: int) -> int:
    value <<= 1
    if value & 0x100:
        value ^= 0x11B
    return value & 0xFF


def _gf_mul(a: int, b: int) -> int:
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


def _mix_single_column(col: list[int]) -> None:
    a0, a1, a2, a3 = col
    col[0] = _gf_mul(a0, 2) ^ _gf_mul(a1, 3) ^ a2 ^ a3
    col[1] = a0 ^ _gf_mul(a1, 2) ^ _gf_mul(a2, 3) ^ a3
    col[2] = a0 ^ a1 ^ _gf_mul(a2, 2) ^ _gf_mul(a3, 3)
    col[3] = _gf_mul(a0, 3) ^ a1 ^ a2 ^ _gf_mul(a3, 2)


def _expand_key(key: bytes) -> list[list[int]]:
    words: list[list[int]] = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = words[i - 1].copy()
        if i % 4 == 0:
            temp = [_SBOX[b] for b in temp[1:] + temp[:1]]
            temp[0] ^= _RCON[i // 4]
        words.append([(words[i - 4][j] ^ temp[j]) & 0xFF for j in range(4)])
    return [sum((words[i + j] for j in range(4)), []) for i in range(0, 44, 4)]


def _aes128_encrypt(key: bytes, block: bytes) -> bytes:
    round_keys = _expand_key(key)
    state = _bytes_to_state(block)
    for col in range(4):
        for row in range(4):
            state[row][col] ^= round_keys[0][row + 4 * col]
    for rnd in range(1, 10):
        for row in range(4):
            for col in range(4):
                state[row][col] = _SBOX[state[row][col]]
        for row in range(1, 4):
            state[row] = state[row][row:] + state[row][:row]
        for col in range(4):
            column = [state[row][col] for row in range(4)]
            _mix_single_column(column)
            for row in range(4):
                state[row][col] = column[row]
        for col in range(4):
            for row in range(4):
                state[row][col] ^= round_keys[rnd][row + 4 * col]
    for row in range(4):
        for col in range(4):
            state[row][col] = _SBOX[state[row][col]]
    for row in range(1, 4):
        state[row] = state[row][row:] + state[row][:row]
    for col in range(4):
        for row in range(4):
            state[row][col] ^= round_keys[10][row + 4 * col]
    return _state_to_bytes(state)


def parse_password_blob(blob: str) -> PasswordRecord:
    if len(blob) < 62 or blob[:2] not in {"01", "03"}:
        raise ValueError("invalid password blob prefix")
    raw = base64.b64decode(blob[2:], validate=True)
    if len(raw) != 44:
        raise ValueError("decoded blob must be 44 bytes")
    return PasswordRecord(
        secret=raw[:32],
        min_seed=int.from_bytes(raw[32:34], "big"),
        algo_id=int.from_bytes(raw[34:36], "big"),
    )


def _normalize_seed(seed: bytes | list[int] | tuple[int, ...]) -> bytes:
    b = bytes(seed) if not isinstance(seed, (bytes, bytearray)) else bytes(seed)
    if len(b) != 5:
        raise ValueError("seed must be exactly 5 bytes")
    return b


def derive_key_from_blob(blob: str, seed: bytes, algo: int) -> tuple[bytes, int, bytes]:
    record = parse_password_blob(blob)
    if algo != record.algo_id:
        raise ValueError(f"algorithm mismatch: blob={record.algo_id} requested={algo}")
    seed_tail = seed[4]
    max_seed = 255 - seed_tail
    if record.min_seed > max_seed:
        raise ValueError("seed tail not allowed by blob min_seed")
    iterations = max_seed - record.min_seed
    digest = record.secret
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()
    aes_key = digest[:16]
    block = bytearray([0xFF] * 16)
    block[11:16] = seed
    mac = _aes128_encrypt(aes_key, bytes(block))[:5]
    return mac, iterations, aes_key


E92A_PRECOMPUTED_AES_KEYS: tuple[bytes, ...] = _precomputed_aes_keys_from_blob()


def derive_key_from_algo(
    algo: int,
    seed: bytes | list[int] | tuple[int, ...],
    *,
    password_map: Mapping[int, str] | None = None,
) -> tuple[bytes, int, bytes]:
    seed_b = _normalize_seed(seed)
    if password_map is None:
        if algo == ALGO_E92A_LATE:
            return derive_key_from_blob(E92A_ALGO_146_BLOB, seed_b, algo)
        raise ValueError(f"no embedded password blob for algorithm {algo}")
    blob = password_map.get(algo)
    if not blob:
        raise ValueError(f"no password blob for algorithm {algo}")
    return derive_key_from_blob(blob, seed_b, algo)


def compute_key(
    seed: bytes | list[int] | tuple[int, ...],
    algo: int = ALGO_E92A_LATE,
) -> bytes:
    """Return 5-byte key for seed (default algo 146 E92A)."""
    mac, _, _ = derive_key_from_algo(algo, seed)
    return mac


def compute_key_precomputed(seed: bytes | list[int] | tuple[int, ...]) -> bytes:
    """Fast path using the precomputed AES key table."""
    seed_b = _normalize_seed(seed)
    tail = seed_b[4]
    if tail > 10:
        raise ValueError(f"seed[4]={tail} out of precomputed table range 0..10")
    block = bytearray([0xFF] * 16)
    block[11:16] = seed_b
    return _aes128_encrypt(E92A_PRECOMPUTED_AES_KEYS[tail], bytes(block))[:5]


__all__ = [
    "ALGO_E92A_LATE",
    "ALGO_T87A",
    "BENCH_KEY_E92A",
    "BENCH_SEED_E92A",
    "compute_key",
    "compute_key_precomputed",
    "derive_key_from_algo",
    "derive_key_from_blob",
    "parse_password_blob",
]