from __future__ import annotations

import unittest
from pathlib import Path

from scantool_public.features.e92_read import kernel_present
from scantool_public.paths import READ_KERNEL, VENDOR_E92


class TestOwnReadKernel(unittest.TestCase):
    def test_shipped_kernel_is_ours(self):
        self.assertTrue(kernel_present(), "kernel.bin should be built from read_kernel/")
        blob = READ_KERNEL.read_bytes()
        self.assertGreater(len(blob), 800)
        self.assertLess(len(blob), 4096)
        self.assertIn(b"SCPB-R2e", blob)
        self.assertNotIn(b"FLASHY", blob)
        self.assertNotIn(b"FLSHY", blob)
        self.assertNotIn(b"kidturbo", blob)
        # wrteei 0 is the first insn so the flash eMIOS11 ISR cannot run.
        self.assertEqual(blob[:4], bytes.fromhex("7c000146"))
        self.assertIn(bytes.fromhex("3c604000"), blob)

    def test_sources_live_here(self):
        src = VENDOR_E92 / "read_kernel"
        for name in ("main.c", "start.S", "kernel.ld", "Makefile", "mpc5674_flexcan.h"):
            self.assertTrue((src / name).is_file(), name)
        text = (src / "main.c").read_text(encoding="utf-8")
        self.assertIn("scpb_main", text)
        self.assertNotIn("FLASHY", text)
        self.assertNotIn("FLSHY", text)
        self.assertIn("0x0001FFF8u", text)
        self.assertIn("0x0002FFF8u", text)
        self.assertNotIn("((addr & 0xFFFFu) >= 0xF800u)", text)


if __name__ == "__main__":
    unittest.main()
