"""Full reads keep …F800 tails. Only two 8-byte ECC holes stay 0xFF."""
from scantool_public.paths import add_vendor_to_path

add_vendor_to_path()
from ecu_bin_extractor import SKIP_F800_TAILS, read_pieces  # noqa: E402


def test_ordinary_tail_is_one_read():
    pieces = read_pieces(0x2EF800, 0x800)
    assert pieces == [("read", 0x2EF800, 0x800)]


def test_ecc_block_reads_up_to_the_hole():
    assert read_pieces(0x1F800, 0x800) == [
        ("read", 0x1F800, 0x7F8),
        ("ff", 0x1FFF8, 8),
    ]
    assert read_pieces(0x2F800, 0x800) == [
        ("read", 0x2F800, 0x7F8),
        ("ff", 0x2FFF8, 8),
    ]


def test_block_before_the_hole_is_not_skipped():
    assert read_pieces(0x2F000, 0x800) == [("read", 0x2F000, 0x800)]


def test_host_no_longer_prefills_whole_tails():
    assert SKIP_F800_TAILS is False
