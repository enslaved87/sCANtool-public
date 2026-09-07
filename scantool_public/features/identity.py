"""VIN / calibration / battery identity probes (generic OBD plus maker VIN)."""

from __future__ import annotations

from scantool_public.transport.obd import ObdClient, decode_pid, split_cal_ids


def probe_identity(client: ObdClient) -> dict:
    vin = client.read_vin()
    cal_ids = client.read_cal_ids()
    name = client.read_ecu_name()
    voltage = client.read_pid(0x42)
    rpm = client.read_pid(0x0C)
    # Never digit-scrape CAL IDs into a fake OS number.
    return {
        "vin": vin,
        "cal_ids": cal_ids,
        "cal_id": " · ".join(cal_ids),
        "os_id": "",
        "ecu_name": name,
        "voltage_v": voltage,
        "rpm": rpm,
        "present": bool(vin or cal_ids or name or voltage is not None),
        "note": (
            "Calibration IDs are Mode 09 ASCII — not a scraped OS number."
            if cal_ids
            else ""
        ),
    }


def snapshot_pids(client: ObdClient, pids: list[int]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for pid in pids:
        out[f"{pid:02X}"] = client.read_pid(pid)
    return out


def vehicle_header(info: dict | None, *, connected: bool, reading: bool = False) -> str:
    """Session-bar label. Keep last VIN across exclusive-bus E92 reads."""
    info = info or {}
    vin = (info.get("vin") or "").strip()
    cals = info.get("cal_ids") or []
    cal = cals[0] if cals else (info.get("cal_id") or "")
    if vin or cal:
        extra = f"  ·  {cal}" if cal else ""
        more = f" +{len(cals) - 1}" if len(cals) > 1 else ""
        text = f"VIN {vin or '—'}{extra}{more}"
        if reading:
            return text + "  ·  reading"
        return text
    if connected:
        return "Connected…"
    return "No vehicle"


def format_voltage(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.1f} V"


def decode_live(pid: int, payload: bytes) -> float | None:
    return decode_pid(pid, payload)


__all__ = [
    "probe_identity",
    "snapshot_pids",
    "vehicle_header",
    "format_voltage",
    "decode_live",
    "split_cal_ids",
]
