"""A 12-hole no-skip read is genuine. A 63-hole skip-tailed read is not."""
from scantool_public.paths import add_vendor_to_path

add_vendor_to_path()
from ecu_bin_extractor import KNOWN_ERASED_TAIL_PAGES, genuine_tail_set  # noqa: E402


def test_known_twelve_are_genuine():
    assert len(KNOWN_ERASED_TAIL_PAGES) == 12
    assert genuine_tail_set(KNOWN_ERASED_TAIL_PAGES)


def test_skip_tailed_sixty_three_is_not_genuine():
    pages = [sec * 0x10000 + 0xF800 for sec in range(63)]
    assert genuine_tail_set(pages) is False
