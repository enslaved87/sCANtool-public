"""Application entry."""

from __future__ import annotations

import os
import sys

from scantool_public.paths import ensure_user_dirs


def apply_chrome(app) -> None:
    from scantool_public.theme import apply

    apply(app)


def run(argv: list[str] | None = None) -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    from PySide6.QtWidgets import QApplication

    from scantool_public import PRODUCT_NAME
    from scantool_public.main_window import MainWindow

    ensure_user_dirs()
    app = QApplication(argv or sys.argv)
    app.setApplicationName(PRODUCT_NAME)
    apply_chrome(app)
    win = MainWindow()
    win.show()
    return app.exec()
