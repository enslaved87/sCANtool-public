"""Algo-146 precomputed AES table must match the SHA-256 chain for tails 0..10.

2026-09-10: hand-transcribed table[8] was E8E5E3DB (wrong). Table is now
generated from the embedded blob so the two paths cannot diverge.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scantool_public" / "vendor" / "e92"))

from gm5byte_key import (  # noqa: E402
    ALGO_E92A_LATE,
    BENCH_KEY_E92A,
    BENCH_SEED_E92A,
    E92A_PRECOMPUTED_AES_KEYS,
    compute_key,
    compute_key_precomputed,
)


def test_bench_vector():
    assert compute_key(BENCH_SEED_E92A, algo=ALGO_E92A_LATE) == BENCH_KEY_E92A


def test_precomputed_table_is_16_bytes_each():
    assert len(E92A_PRECOMPUTED_AES_KEYS) == 11
    assert all(len(k) == 16 for k in E92A_PRECOMPUTED_AES_KEYS)
    assert E92A_PRECOMPUTED_AES_KEYS[8].hex().upper() == "995F00F6493D1DF10F2982CDE8E53BDB"


def test_precomputed_matches_sha_chain_all_tails():
    for tail in range(11):
        seed = bytes([0x87, 0x85, 0xEE, 0xC1, tail])
        assert compute_key(seed, algo=ALGO_E92A_LATE) == compute_key_precomputed(seed)
