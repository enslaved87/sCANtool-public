#!/usr/bin/env python3
"""Launch sCANtool Public.

Copyright (C) 2026 enslaved87
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scantool_public.app import run

if __name__ == "__main__":
    raise SystemExit(run())
