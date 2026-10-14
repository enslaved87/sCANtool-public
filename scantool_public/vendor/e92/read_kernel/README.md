# Read kernel (this product)

SRAM-only reader for early/late E92 (MPC5674F).

- Loads at `0x40001000` via stock `$34` / `$36` / `$36 80`
- Inherits the bootloader stack (does not rewrite `r1`)
- Talks FlexCAN_A that the bootloader already set to 500 kbit/s
- Answers `$1A $BB` with `5A BB SCPB`
- Image id string is `SCPB-R2e` (ECC-hole-only reader). Older
  `SCPB-R2` binaries still FF-fill whole `…F800` tails.
- Answers `$23` with `63` + address + bytes (ISO-TP)
- Copies each `$23` block with byte loads into `0x40002000`, then
  streams from SRAM. Only the two 8-byte ECC holes (`0x1FFF8`,
  `0x2FFF8`) are returned as `0xFF`. Host `$23` splits those holes
  (`read_pieces`). Metal 2026-09-27: `$23` of `0x2F800` that stops
  before `0x2FFF8` does not kill this reader. Shadow (`0x00FFC000`,
  NVPWD at `0x00FFFDD8`) is not skipped.
- `$11` ACKs then hits SIU `SRCR` so the ECM returns to stock
- **Never** erases or programs flash
- Clears EE at `_start` and pets the Book-E TSR plus the DSPI_D
  companion watchdog from C wait loops (~12.5 ms). Does **not** leave
  the bootloader's flash eMIOS11 ISR running.

Build (Windows, SysGCC):

```
make
```

Output is `../kernel.bin`.
