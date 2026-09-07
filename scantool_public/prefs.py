"""Last-used adapter / UI prefs. Lives in Documents, never next to source."""

from __future__ import annotations

import json
from typing import Any

from scantool_public.paths import PREFS_PATH, ensure_user_dirs

_DEFAULTS: dict[str, Any] = {
    "adapter_kind": "demo",
    "adapter_interface": "demo",
    "adapter_channel": "0",
    "bitrate": 500_000,
    "datalog_hz": 5.0,
    "datalog_preset": "engine",
}


def load_prefs() -> dict[str, Any]:
    doc = dict(_DEFAULTS)
    try:
        if PREFS_PATH.is_file():
            raw = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                doc.update({k: raw[k] for k in raw if k in _DEFAULTS or k in raw})
    except Exception:
        return dict(_DEFAULTS)
    return doc


def save_prefs(update: dict[str, Any]) -> dict[str, Any]:
    doc = load_prefs()
    doc.update(update)
    try:
        ensure_user_dirs()
        PREFS_PATH.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass
    return doc


def adapter_key(spec: dict) -> tuple[str, str, str]:
    return (
        str(spec.get("kind") or ""),
        str(spec.get("interface") or spec.get("kind") or ""),
        str(spec.get("channel") if spec.get("channel") is not None else ""),
    )


def match_adapter(adapters: list[dict], prefs: dict[str, Any]) -> int:
    """Index of the remembered adapter, or 0 (Demo) if it is not plugged in."""
    want = (
        str(prefs.get("adapter_kind") or ""),
        str(prefs.get("adapter_interface") or prefs.get("adapter_kind") or ""),
        str(prefs.get("adapter_channel") if prefs.get("adapter_channel") is not None else ""),
    )
    if not want[0] or want[0] == "demo":
        return 0
    for i, spec in enumerate(adapters):
        if adapter_key(spec) == want:
            return i
    return 0
