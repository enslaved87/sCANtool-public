# sCANtool Public

Vehicle scan, identity, datalog, E92 full-read, and EARLY E92 flash write.

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
- **Write (EARLY only)** — Standard mode is **Write calibration** (LAS
  `0x40000` / `0x60000` + MAS) or **Write entire** (cal + OS + HAS).
  Advanced mode writes one dest. Dests in one job share the write helper
  and reset to stock when the last dest finishes. Boot, VIN, and
  `0x1F000` are not written.
  LATE modules are refused. This reader's `…F800` holes (`0xFF` fill)
  are replaced from live flash on write so a self-read can go back.

## What it does not do

Write is refused on LATE E92, on serial OBD-only adapters, and on the
demo adapter. Read and write kernels are never resident together.

## Run (end user)

Unzip the Windows build and double-click `sCANtool.exe`.

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

E92 full-read and EARLY write need **raw CAN + ISO-TP**. Serial OBD-only
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

Attach `release/sCANtool-windows.zip` as a GitHub Release asset.

## License

sCANtool Public is free software under the **GNU General Public License
v3.0 or later**. See `LICENSE`.

You may run, share, and modify it. If you distribute this program or a
modified version (including a closed product that includes it), you must
also provide the corresponding source under the same license.

There is **no warranty**. Flash write can brick a module. You assume
that risk. The Windows zip's corresponding source is this repository.
