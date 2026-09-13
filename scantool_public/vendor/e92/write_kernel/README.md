# Write kernel SCPB-W1

This product's C90FL SRAM helper. Uploaded only for EARLY dest-gate writes.

Geometry from the public C90FL SSD (AN4521):

- FMC bases `0xC3F88000` and `0xC3F8C000`
- DONE `0x400`, PEG `0x200`
- HAS HSR = `(addr - 0x100000) >> 19` (512 KiB logical pair)
- Dual-module HAS erase **MOD1 then MOD0**; program arms both
- MOD1 interlock at `sector + 16`
- MID: Flash B L0 @ 0x80000 = LSEL0; Flash B M0 @ 0xC0000 = **MSEL0**
  (LMSR bit 16)
- C90FL MCR bits, not MPC57xx
- Alive banner `5A BB SCPB`

Build: `make` → `../write_kernel.bin`

Host waits for the SCPB dest ack after each $6B/$6C. Transfer ACK is not programmed flash.

Clears EE at `_start` and pets Book-E TSR + DSPI_D companion watchdog
from erase/program/CAN wait loops (same 6-word pair as KernelMPC5674F).
The bootloader eMIOS11 ISR in flash is no longer required to keep the
module alive. Dest-gate still refuses boot / VIN / `0x1F000`.
