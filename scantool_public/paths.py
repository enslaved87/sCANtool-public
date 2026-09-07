"""Filesystem roots for this product."""

from __future__ import annotations

import sys
from pathlib import Path


def _package_dir() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = Path(meipass) / "scantool_public"
            if bundled.is_dir():
                return bundled
            return Path(meipass)
    return Path(__file__).resolve().parent


PACKAGE_DIR = _package_dir()
ROOT = PACKAGE_DIR.parent
DATA_DIR = PACKAGE_DIR / "data"
VENDOR_E92 = PACKAGE_DIR / "vendor" / "e92"
READ_KERNEL = VENDOR_E92 / "kernel.bin"
WRITE_KERNEL = VENDOR_E92 / "write_kernel.bin"
USER_DOCS = Path.home() / "Documents" / "sCANtool Public"
READS_DIR = USER_DOCS / "reads"
WRITES_DIR = USER_DOCS / "writes"
LOGS_DIR = USER_DOCS / "logs"
PRESETS_DIR = USER_DOCS / "datalog_presets"
REPORTS_DIR = USER_DOCS / "reports"
PREFS_PATH = USER_DOCS / "prefs.json"


def ensure_user_dirs() -> None:
    for p in (READS_DIR, WRITES_DIR, LOGS_DIR, PRESETS_DIR, REPORTS_DIR):
        p.mkdir(parents=True, exist_ok=True)


def add_vendor_to_path() -> None:
    """Let the vendored E92 reader import gm5byte_key / gm2byte_key."""
    vendor = str(VENDOR_E92)
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
