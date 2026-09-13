# Read kernel (this product)

SRAM-only reader for early/late E92 (MPC5674F).

- Loads at `0x40001000` via stock `$34` / `$36` / `$36 80`
- Inherits the bootloader stack (does not rewrite `r1`)
- Talks FlexCAN_A that the bootloader already set to 500 kbit/s
- Answers `$1A $BB` with `5A BB SCPB`
- Answers `$23` with `63` + address + bytes (ISO-TP)
- Copies each `$23` block with byte loads into `0x40002000`, then
  streams from SRAM. Does **not** load the last 2 KiB of each 64 KiB
  window in the 4 MiB main image (`…F800`–`…FFFF`) — `$23` of `0x2F800`
  machine-checked this SRAM reader on metal (2026-09-06). Host prefills
  `0xFF`. Shadow (`0x00FFC000`, NVPWD at `0x00FFFDD8`) is not skipped.
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
