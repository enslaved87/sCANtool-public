# sCANtool Public

Vehicle scan, identity, datalog, E92 full-read, and EARLY/LATE E92 flash write.

## What it does

- **Scan** — stored / pending / permanent DTCs, MIL + readiness (PID 01),
  Mode 02 freeze frame, multi-ECU (0x7E0–0x7E7), one-page report export
- **Identity** — VIN, split calibration IDs (never a scraped fake OS),
  ECU name, module voltage, RPM
- **Datalog** — pick PIDs, set rate, record CSV, live chart, mark events
- **E92 read** — early and late E92 full 4 MiB flash read using this
  product's own SRAM read kernel (`SCPB-R2`); the OBD session reconnects
  after the dump. The last 2 KiB of each 64 KiB window (`…F800`) is
  filled `0xFF` — a `$23` there machine-checks this SRAM reader.
  **Shadow password** reads 16 KiB of shadow flash and shows NVPWD
  (censorship password). Read-only; it does not program NVPWD.
- **Write (EARLY or LATE E92)** — **Write calibration** (LAS
  `0x40000` / `0x60000` + MAS), **Write entire** (cal + OS + HAS), or
  **Clone to this ECU**. **LATE** clone writes the chip except 4 KiB
  at `0x1F000` (left erased FF) and immobilizer/BCM; boot is last.
  **EARLY** clone is still cal + OS + HAS + VIN page only. The spare
  must already be the same family; it does not need the same OS ID
  or VIN. Advanced mode writes one dest. Dests in one job share the
  write helper and reset to stock when the last dest finishes. Other
  write modes leave VIN on the ECU. Write entire is not atomic. If a later dest
  fails and the helper is still alive, already-written dests are rolled
  back to the pre-write image. If the helper is silent, rollback cannot
  run — B+ off 8–10 s, then run **Write entire** again with the same
  image to heal mixed cal/OS/HAS. Image VIN (`0x100B4`) and live CAL
  are checked before dest 1.
  This reader's `…F800` holes (`0xFF` fill)
  are replaced from live flash on write so a self-read can go back.

## What it does not do

Write is refused on serial OBD-only adapters and on the demo adapter.
Read and write kernels are never resident together.

## Run (end user)

The Windows exe is **not** in this git tree. Download the zip from
[Releases](https://github.com/enslaved87/sCANtool-public/releases)
(`sCANtool-windows.zip`), unzip it, and double-click `sCANtool.exe`.
Current release is **v1.0.8** (LATE full-chip clone dests; skip `0x1F000`).
`LICENSE` is in that folder.

Kvaser / Peak / SLCAN adapters need their vendor driver installed on
the PC. The exe bundles Python and the app libraries; it does not
replace the adapter driver.

Logs, reports, and BIN dumps go to `Documents\sCANtool Public\`.
E92 full-read traces are `Documents\sCANtool Public\logs\e92_read_*.txt`.
The last adapter and bitrate are remembered there in `prefs.json`.

## Run (from source)

```
pip install -r requirements.txt
python scantool_public.py
```

Or double-click `Launch sCANtool Public.bat`.

Use **Demo (no adapter)** to click through the UI without hardware.

### Adapters

Scan / identity / datalog work on USB-CAN and serial OBD adapters
(python-can backends plus the common AT-command serial dongles).

E92 full-read and EARLY/LATE write need **raw CAN + ISO-TP**. Serial OBD-only
adapters cannot upload a kernel.

Hit **Refresh** after plugging a dongle in. Serial ports show twice:
scan/log mode vs raw CAN (SLCAN) so you choose the protocol.

## Hardware

- USB-CAN adapter on HS-CAN (or Demo for UI practice)
- Key ON, engine OFF
- Unplug every other tool from the bus
- Module voltage is read from CAN when the ECM reports it

A full E92 read takes several minutes. Do not key-off until the tool says
the ECM is back on stock OS.

A write can brick the module if power is lost. Confirm all three boxes on
the Write tab. Dests in one job share the write helper; stock OS returns
after the last dest. Power-cycle B+ if the helper is silent.
A hung write helper needs B+ off 8–10 s — software reset does nothing.
Do not key-off until the tool reconnects.

Write calibration is typically a few minutes. Write entire is about
fifteen minutes (one helper, send-all `$6C`). Do not key-off until the
tool says the ECM is back on stock OS.

## Kernels

The read kernel source is `scantool_public/vendor/e92/read_kernel/`.
The write kernel source is `scantool_public/vendor/e92/write_kernel/`.
Shipped blobs are `kernel.bin` and `write_kernel.bin` next to those
trees. Rebuild with PowerPC gcc if you change them:

```
make PREFIX=C:/SysGCC/powerpc-eabi/bin/powerpc-eabi-
```

## Windows exe (maintainers)

```
pip install -r requirements.txt pyinstaller
powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1
```

Output is `dist/sCANtool/sCANtool.exe` (onedir, Python + Qt + this
app). Zip that folder for a GitHub Release asset. `dist/`, `build/`,
and `release/` are not committed. A packed zip from this tree is
`release/sCANtool-windows.zip`.

## Git

This folder is the public repository root. Commit the Python package,
kernel *source*, and the shipped `kernel.bin` / `write_kernel.bin`.
Do not commit `tests/`, `dist/`, `build/`, `release/`, `outputs/`, or
compiler `.o` / `.elf` / `.map` files.

End users download `sCANtool-windows.zip` from GitHub Releases, not
from a clone. Attach that zip as a Release asset; do not commit it.

## Notes (v1.0.8)

- **LATE Clone to this ECU** writes sector dests including boot (last),
  VIN tiles, `0x1C000` 12 KiB, `0x20000`, `0x30000`, then cal/OS/HAS.
  `0x1F000` is not programmed (left FF). Immobilizer/BCM is not cloned.
  **EARLY** clone dests are unchanged until EARLY metal repeats those dests.

## Notes (v1.0.7)

- **Clone to this ECU** is not a full-chip copy. A full-chip clone is
  not possible with this write helper (`0x1F000` hangs; boot and
  `0x20000–0x3FFFF` have no proven dest). Clone writes cal, OS, HAS,
  and VIN. Immobilizer/BCM is a different module. The spare must
  already be the same EARLY or LATE family; it does not need the same
  OS ID or VIN. The Write tab states this on-screen and in the confirm
  dialog.

## Notes (v1.0.6)

- **Clone to this ECU** writes cal + OS + HAS + VIN onto a spare of the
  same EARLY/LATE family. Boot and `0x1F000` stay. Immobilizer/BCM
  is not cloned — the spare may not start the vehicle until the BCM is paired.
- Write tab shows image VIN/OS, skip-tail splice note, and time estimate.
  Write calibration can restamp System/Fuel/Speedo/EngineDiag CS (not Engine).
  After a successful write the tool reports live VIN / CAL / `$1A C0/C1` / `$09 06`.
- Identity tab shows `$09 06` CVN/CS words (stored, not planted).

## Notes (v1.0.5)

- EARLY and LATE dest-gate are both on (`EARLY_WRITE_GO` / `LATE_WRITE_GO`).
  LATE cal identity **198 s**; LATE write
  entire all dests verified **16 min 27 s** (HAS live `$23` preread). EARLY
  cal **198 s**, write entire **15 min 9 s**.
- SCPB-W1 is the same C90FL helper for both (3948 B). 5-byte algo 146 on LATE.

## Notes (v1.0.4)

- Write `$6C` is send-all, one SCPB dest ack (no per-frame recv). Host
  pacing is 0.5 ms/frame. Write calibration (LAS `0x40000` / `0x60000` +
  MAS) completed in **3 min 18 s** on metal. Write entire (cal + OS MID +
  HAS H0–H5) completed in **15 min 9 s** with all dests verified. The
  previous path was ~90 minutes.
- Dest 2+ keeps one write helper (`reuse_kernel`; `$11` after the last
  dest). The helper zeros LMSR+HSR before each `$6B`/`$6C`. VIN/CAL are
  checked before dest 1. Committed dests roll back if a later dest faults
  and the helper is alive.
- Write kernel pets Book-E TSR + DSPI_D companion in wait loops so the
  flash eMIOS11 ISR is not required to keep the module alive.

## Notes (v1.0.3)

- **Do not use v1.0.2.** The algo-146 AES table is generated from the
  blob in this tree.
- SCPB-R2 skip-tail FULLREADs (`0x11F800` / `0x3FF800` filled `0xFF`) are
  refused. Use a complete 4 MiB dump.

## Notes (v1.0.1)

- Write entire / calibration is dest-by-dest on **one write helper**.
  The helper zeros LMSR+HSR before each `$6B`/`$6C` so leftover select
  bits cannot over-erase neighbors. A mid-job dropout used to leave new
  cal/OS on old HAS with no warning. The tool rolls back dests that
  already dump-matched when the helper is still alive, and tells you to
  re-run the same job after a B+ cycle if it is not.
- Wrong 4 MiB file: image VIN and live CAL are compared before dest 1.
- Startup failures show a dialog (windowed exe has no console).
- Short ISO-TP frames no longer crash scan/identity.

## License

sCANtool Public is free software under the **GNU General Public License
v3.0 or later**. See `LICENSE`.

You may run, share, and modify it. If you distribute this program or a
modified version (including a closed product that includes it), you must
also provide the corresponding source under the same license.

There is **no warranty**. Flash write can brick a module. You assume
that risk. The Windows zip's corresponding source is this repository.
