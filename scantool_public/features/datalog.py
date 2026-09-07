"""Configurable OBD-II datalog — PID set, rate, CSV."""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from scantool_public.paths import PRESETS_DIR, ensure_user_dirs
from scantool_public.transport.obd import ObdClient, format_pid, pid_catalog


def default_pids() -> list[str]:
    return list((pid_catalog().get("presets") or {}).get("engine") or ["0C", "0D", "05"])


def preset_names() -> list[str]:
    return list((pid_catalog().get("presets") or {}).keys())


def pids_for_preset(name: str) -> list[str]:
    presets = pid_catalog().get("presets") or {}
    return list(presets.get(name) or default_pids())


def pid_meta(hex_pid: str) -> dict:
    return dict((pid_catalog().get("pids") or {}).get(hex_pid.upper()) or {"name": hex_pid})


class CsvLog:
    def __init__(self, path: Path, pids: list[str]):
        ensure_user_dirs()
        self.path = path
        self.pids = [p.upper() for p in pids]
        self._fh = path.open("w", newline="", encoding="utf-8")
        self._w = csv.writer(self._fh)
        headers = ["time_s"] + [f"{p}_{pid_meta(p).get('name', p).replace(' ', '_')}" for p in self.pids] + ["event"]
        self._w.writerow(headers)
        self._t0 = time.time()
        self._pending_event = ""

    def mark(self, note: str) -> None:
        text = (note or "").strip() or "mark"
        t = time.time() - self._t0
        row = [f"{t:.3f}"] + [""] * len(self.pids) + [text]
        self._w.writerow(row)
        self._fh.flush()

    def write_row(self, values: dict[str, float | None], event: str = "") -> None:
        t = time.time() - self._t0
        row = [f"{t:.3f}"]
        for p in self.pids:
            v = values.get(p)
            row.append("" if v is None else f"{v:.4g}")
        row.append(event or "")
        self._w.writerow(row)
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def poll_row(client: ObdClient, pids: list[str]) -> dict:
    values: dict[str, float | None] = {}
    texts: dict[str, str] = {}
    for hex_pid in pids:
        pid = int(hex_pid, 16)
        v = client.read_pid(pid)
        values[hex_pid.upper()] = v
        texts[hex_pid.upper()] = format_pid(pid, v)
    return {"values": values, "texts": texts, "ts": time.time()}


def save_preset(name: str, pids: list[str]) -> Path:
    ensure_user_dirs()
    path = PRESETS_DIR / f"{name}.json"
    path.write_text(json.dumps({"pids": pids}, indent=2) + "\n", encoding="utf-8")
    return path


def load_user_presets() -> dict[str, list[str]]:
    ensure_user_dirs()
    out: dict[str, list[str]] = {}
    for p in PRESETS_DIR.glob("*.json"):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            out[p.stem] = list(doc.get("pids") or [])
        except Exception:
            continue
    return out
