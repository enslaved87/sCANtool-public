"""DTC scan, MIL/readiness, freeze frame, multi-ECU, one-page report."""

from __future__ import annotations

import time
from pathlib import Path

from scantool_public.paths import REPORTS_DIR, ensure_user_dirs
from scantool_public.transport.obd import ObdClient, format_pid, pid_catalog


DEFAULT_SNAPSHOT = ("0C", "0D", "05", "11", "42", "1C", "1F", "21", "30", "31")


def _dtc_count(dtcs: dict) -> int:
    return sum(len(dtcs.get(k) or []) for k in ("stored", "pending", "permanent"))


def run_scan(client: ObdClient) -> dict:
    modules_meta = client.discover_modules()
    modules: list[dict] = []
    merged: dict[str, list[dict]] = {"stored": [], "pending": [], "permanent": []}
    primary_monitor: dict = {}
    freeze = None

    for meta in modules_meta:
        peer = ObdClient(client.t, txid=meta["txid"], rxid=meta["rxid"])
        dtcs = peer.read_dtcs()
        monitor = peer.read_monitor_status(functional=False)
        for key in merged:
            for item in dtcs.get(key) or []:
                rec = dict(item)
                rec["ecu"] = meta["label"]
                rec["txid"] = meta["txid"]
                merged[key].append(rec)
        entry = {
            "txid": meta["txid"],
            "rxid": meta["rxid"],
            "label": meta["label"],
            "dtcs": dtcs,
            "monitor": monitor,
            "count": _dtc_count(dtcs),
            "mil": bool((monitor or {}).get("mil")) or bool(dtcs.get("stored")),
        }
        modules.append(entry)
        if meta["txid"] == 0x7E0 or not primary_monitor:
            primary_monitor = monitor
            if freeze is None:
                try:
                    freeze = peer.read_freeze_frame()
                except Exception:
                    freeze = None

    live = {}
    catalog = pid_catalog().get("pids") or {}
    for hex_pid in DEFAULT_SNAPSHOT:
        pid = int(hex_pid, 16)
        value = client.read_pid(pid)
        live[hex_pid] = {
            "name": (catalog.get(hex_pid) or {}).get("name") or hex_pid,
            "value": value,
            "text": format_pid(pid, value),
        }

    stored = merged.get("stored") or []
    mil = bool((primary_monitor or {}).get("mil")) or bool(stored)
    return {
        "dtcs": merged,
        "live": live,
        "mil": mil,
        "count": _dtc_count(merged),
        "monitor": primary_monitor or {},
        "freeze_frame": freeze,
        "modules": modules,
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def clear_codes(client: ObdClient) -> bool:
    return client.clear_dtcs()


def format_report(report: dict, *, identity: dict | None = None) -> str:
    ident = identity or {}
    lines = [
        "sCANtool Public — scan report",
        report.get("when") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "",
    ]
    vin = ident.get("vin") or report.get("vin") or "—"
    cals = ident.get("cal_ids") or []
    if not cals and ident.get("cal_id"):
        cals = [ident["cal_id"]]
    lines.append(f"VIN: {vin}")
    lines.append("CAL IDs: " + (" · ".join(cals) if cals else "—"))
    if ident.get("ecu_name"):
        lines.append(f"ECU name: {ident['ecu_name']}")
    v = ident.get("voltage_v")
    if isinstance(v, (int, float)):
        lines.append(f"Module voltage: {v:.2f} V")
    lines.append("")

    mods = report.get("modules") or []
    if mods:
        lines.append("Modules")
        for m in mods:
            mon = m.get("monitor") or {}
            mil = "MIL ON" if m.get("mil") else "MIL off"
            ready = mon.get("ready") or []
            not_ready = mon.get("not_ready") or []
            n_av = len(mon.get("available") or [])
            lines.append(
                f"  {m.get('label')}  0x{m.get('txid', 0):03X}  {mil}  "
                f"{m.get('count') or 0} code(s)  "
                f"ready {len(ready)}/{n_av or len(ready)+len(not_ready)}"
            )
            if not_ready:
                lines.append("    not ready: " + ", ".join(not_ready))
        lines.append("")

    mon = report.get("monitor") or {}
    if mon:
        ign = mon.get("ignition") or "spark"
        lines.append(
            f"I/M readiness ({ign} ignition): MIL {'ON' if mon.get('mil') else 'off'}  "
            f"PID 01 count {mon.get('dtc_count', '—')}"
        )
        rows = mon.get("rows") or []
        if rows:
            for row in rows:
                lines.append(f"  {row.get('name')}: {row.get('status')}")
        else:
            if mon.get("not_ready"):
                lines.append("  Incomplete: " + ", ".join(mon["not_ready"]))
            if mon.get("ready"):
                lines.append("  Complete: " + ", ".join(mon["ready"]))
        lines.append("")

    dtcs = report.get("dtcs") or {}
    any_code = False
    for status in ("stored", "pending", "permanent"):
        items = dtcs.get(status) or []
        if not items:
            continue
        any_code = True
        lines.append(status.title())
        for item in items:
            ecu = item.get("ecu") or ""
            name = item.get("name") or ""
            extra = f"  {name}" if name else ""
            who = f"  [{ecu}]" if ecu else ""
            lines.append(f"  {item.get('code')}{who}{extra}")
        lines.append("")
    if not any_code:
        lines.append("No diagnostic trouble codes.")
        lines.append("")

    ff = report.get("freeze_frame")
    if ff:
        lines.append(f"Freeze frame  (trigger {ff.get('dtc') or '—'})")
        if ff.get("name"):
            lines.append(f"  {ff['name']}")
        for item in (ff.get("values") or {}).values():
            lines.append(f"  {item.get('text') or ''}")
        lines.append("")

    live = report.get("live") or {}
    if live:
        lines.append("Live snapshot")
        for item in live.values():
            lines.append(f"  {item.get('text') or ''}")
        lines.append("")

    lines.append("Read only. Codes were not modified by this report.")
    return "\n".join(lines).rstrip() + "\n"


def save_report(report: dict, path: Path | None = None, *, identity: dict | None = None) -> Path:
    ensure_user_dirs()
    dest = path or (REPORTS_DIR / f"scan_{time.strftime('%Y%m%d-%H%M%S')}.txt")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(format_report(report, identity=identity), encoding="utf-8")
    return dest
