"""Application entry."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def apply_chrome(app) -> None:
    from scantool_public.theme import apply

    apply(app)


def _crash_log(text: str) -> Path | None:
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    candidates = [Path(tmp) / "scantool-public-crash.txt"]
    try:
        from scantool_public.paths import LOGS_DIR

        candidates.insert(0, LOGS_DIR / "startup_crash.txt")
    except Exception:
        pass
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return path
        except Exception:
            continue
    return None


def _alert(title: str, body: str) -> None:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv[:1])
        QMessageBox.critical(None, title, body)
        return
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, body, title, 0x10)
    except Exception:
        pass


def run(argv: list[str] | None = None) -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    argv = list(argv) if argv is not None else sys.argv

    def _hook(typ, val, tb) -> None:
        text = "".join(traceback.format_exception(typ, val, tb))
        path = _crash_log(text)
        extra = f"\n\nLog: {path}" if path else ""
        _alert("sCANtool Public error", f"{typ.__name__}: {val}{extra}")

    sys.excepthook = _hook
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        from scantool_public import PRODUCT_NAME
        from scantool_public.main_window import MainWindow
        from scantool_public.paths import ensure_user_dirs

        app = QApplication(argv)
        app.setApplicationName(PRODUCT_NAME)
        try:
            ensure_user_dirs()
        except OSError as exc:
            QMessageBox.warning(
                None,
                PRODUCT_NAME,
                "Could not create Documents\\sCANtool Public folders "
                f"({exc}). Prefs and dumps may not save.",
            )
        apply_chrome(app)
        win = MainWindow()
        win.show()
        return app.exec()
    except Exception as exc:
        text = traceback.format_exc()
        path = _crash_log(text)
        extra = f"\n\nLog: {path}" if path else ""
        _alert(
            "sCANtool Public failed to start",
            f"{type(exc).__name__}: {exc}{extra}",
        )
        return 1
