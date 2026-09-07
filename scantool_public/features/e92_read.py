"""Early / late E92 full-flash READ only.

Uploads this product's SRAM read kernel (SCPB-R2). A write kernel is
never loaded from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from scantool_public.features.write_gate import WriteBlocked, assert_read_only
from scantool_public.paths import READ_KERNEL, READS_DIR, add_vendor_to_path, ensure_user_dirs


LogFn = Callable[[str], None]


@dataclass
class E92ReadOutcome:
    ok: bool
    path: Path | None = None
    vin: str = ""
    os_id: str = ""
    checksum: int = 0
    error: str = ""
    variant: str = ""
    skipped_blocks: int = 0
    region: str = "flash"
    nvpwd_hex: str = ""
    nvpwd_class: str = ""


def kernel_present() -> bool:
    return READ_KERNEL.is_file() and READ_KERNEL.stat().st_size > 100


def _load_extractor():
    add_vendor_to_path()
    from ecu_bin_extractor import (  # type: ignore
        E92BinExtractor,
        E92Variant,
        classify_variant,
        open_kvaser_bus,
    )

    return E92BinExtractor, E92Variant, classify_variant, open_kvaser_bus


def classify_e92(vin: str = "", seed_len: int = 0) -> str:
    add_vendor_to_path()
    from ecu_bin_extractor import E92Variant, classify_variant  # type: ignore

    v = classify_variant(vin=vin, seed_len=seed_len)
    if v == E92Variant.EARLY:
        return "early"
    if v == E92Variant.LATE:
        return "late"
    return "unknown"


def probe_e92(bus, log: LogFn | None = None) -> dict:
    """VIN + OS on an already-open python-can bus. Read-only UDS/OBD."""
    assert_read_only()
    E92BinExtractor, *_ = _load_extractor()
    ext = E92BinExtractor(bus, log=log or (lambda _m: None), detail_log=log or (lambda _m: None))
    vin, osid = ext.probe_identity()
    variant = classify_e92(vin=vin)
    return {"vin": vin, "os_id": osid, "variant": variant}


def full_read_kvaser(
    *,
    channel: int,
    bitrate: int,
    output_dir: Path | None = None,
    verify_double: bool = False,
    log: LogFn | None = None,
    stop_check: Callable[[], bool] | None = None,
    progress: Callable[[dict], None] | None = None,
) -> E92ReadOutcome:
    """Back-compat wrapper: Kvaser channel only."""
    return full_read(
        spec={"kind": "kvaser", "interface": "kvaser", "channel": channel, "raw_can": True},
        bitrate=bitrate,
        output_dir=output_dir,
        verify_double=verify_double,
        log=log,
        stop_check=stop_check,
        progress=progress,
    )


def full_read(
    *,
    spec: dict,
    bitrate: int,
    output_dir: Path | None = None,
    verify_double: bool = False,
    log: LogFn | None = None,
    stop_check: Callable[[], bool] | None = None,
    progress: Callable[[dict], None] | None = None,
    bus=None,
) -> E92ReadOutcome:
    """Unlock + upload *read* kernel + 4 MiB dump. Always recovers to stock OS."""
    assert_read_only()
    if not kernel_present():
        return E92ReadOutcome(ok=False, error=f"Read kernel missing: {READ_KERNEL}")

    from scantool_public.transport.adapter import open_raw_bus

    E92BinExtractor, E92Variant, _classify_unused, _open_kvaser = _load_extractor()
    ensure_user_dirs()
    dest = output_dir or READS_DIR
    dest.mkdir(parents=True, exist_ok=True)
    _log = log or (lambda _m: None)
    own_bus = bus is None
    try:
        if bus is None:
            bus = open_raw_bus(spec, bitrate)
        ext = E92BinExtractor(
            bus,
            log=_log,
            detail_log=_log,
            variant=E92Variant.LATE,
            stop_check=stop_check,
        )
        # Progress: poll last_read_progress while full_read runs (same thread).
        def _tick() -> None:
            if progress is None:
                return
            p = ext.last_read_progress
            progress(
                {
                    "bytes": p.bytes_read,
                    "total": p.total_bytes or 1,
                    "pct": p.pct,
                    "addr": p.last_addr,
                    "phase": p.phase,
                }
            )

        orig_log = ext.log

        def wrapped(msg: str) -> None:
            orig_log(msg)
            _tick()

        ext.log = wrapped
        result = ext.full_read(None, verify_os=None, verify_double=verify_double)
        _tick()
        variant = classify_e92(vin=result.vin)
        if result.ok and result.path:
            # Rename pending to VIN/OS tagged file (full_read already wrote a name
            # when output_path is set — keep whatever it saved).
            return E92ReadOutcome(
                ok=True,
                path=result.path,
                vin=result.vin,
                os_id=result.os_id,
                checksum=result.checksum,
                variant=variant,
                skipped_blocks=result.skipped_blocks,
            )
        return E92ReadOutcome(
            ok=False,
            path=result.partial_path,
            vin=result.vin,
            os_id=result.os_id,
            error=result.error or "read failed",
            variant=variant,
        )
    except WriteBlocked:
        raise
    except Exception as exc:
        return E92ReadOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if own_bus and bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass


def read_shadow(
    *,
    spec: dict,
    bitrate: int,
    output_dir: Path | None = None,
    log: LogFn | None = None,
    stop_check: Callable[[], bool] | None = None,
    progress: Callable[[dict], None] | None = None,
    bus=None,
) -> E92ReadOutcome:
    """Unlock + upload *read* kernel + 16 KiB shadow. Parse NVPWD. Recover to stock."""
    assert_read_only()
    if not kernel_present():
        return E92ReadOutcome(ok=False, error=f"Read kernel missing: {READ_KERNEL}", region="shadow")

    from scantool_public.transport.adapter import open_raw_bus

    E92BinExtractor, E92Variant, _classify_unused, _open_kvaser = _load_extractor()
    ensure_user_dirs()
    dest = output_dir or READS_DIR
    dest.mkdir(parents=True, exist_ok=True)
    _log = log or (lambda _m: None)
    own_bus = bus is None
    try:
        if bus is None:
            bus = open_raw_bus(spec, bitrate)
        ext = E92BinExtractor(
            bus,
            log=_log,
            detail_log=_log,
            variant=E92Variant.LATE,
            stop_check=stop_check,
        )

        def _tick() -> None:
            if progress is None:
                return
            p = ext.last_read_progress
            progress(
                {
                    "bytes": p.bytes_read,
                    "total": p.total_bytes or 1,
                    "pct": p.pct,
                    "addr": p.last_addr,
                    "phase": p.phase or "shadow_read",
                }
            )

        orig_log = ext.log

        def wrapped(msg: str) -> None:
            orig_log(msg)
            _tick()

        ext.log = wrapped
        result = ext.full_read_shadow(None, verify_os=None, keep_kernel=False)
        _tick()
        variant = classify_e92(vin=result.vin)
        if result.ok and result.path:
            return E92ReadOutcome(
                ok=True,
                path=result.path,
                vin=result.vin,
                os_id=result.os_id,
                checksum=result.checksum,
                variant=variant,
                skipped_blocks=result.skipped_blocks,
                region="shadow",
                nvpwd_hex=result.nvpwd_hex,
                nvpwd_class=result.nvpwd_class,
            )
        return E92ReadOutcome(
            ok=False,
            path=result.partial_path,
            vin=result.vin,
            os_id=result.os_id,
            error=result.error or "shadow read failed",
            variant=variant,
            region="shadow",
        )
    except WriteBlocked:
        raise
    except Exception as exc:
        return E92ReadOutcome(ok=False, error=f"{type(exc).__name__}: {exc}", region="shadow")
    finally:
        if own_bus and bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass


def refuse_write() -> None:
    raise WriteBlocked("The read path never uploads a write kernel. Use the Write tab.")
