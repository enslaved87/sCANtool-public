"""Dark professional chrome — Fusion palette, check PNGs in data/."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from scantool_public.paths import DATA_DIR

BG = "#070b12"
PANEL = "#0e1520"
CARD = "#141c2b"
GRID = "#1e2a3c"
FG = "#e2e8f0"
MUTED = "#64748b"
ACCENT = "#38bdf8"
OK = "#22c55e"
WARN = "#f59e0b"
BAD = "#ef4444"
DIM = "#94a3b8"

_CHECK_ON = (DATA_DIR / "check_on.png").resolve().as_posix()
_CHECK_OFF = (DATA_DIR / "check_off.png").resolve().as_posix()

SHEET = f"""
QMainWindow, QWidget {{
    background: {BG}; color: {FG};
    font-family: 'Segoe UI'; font-size: 9.5pt;
}}
QMenuBar {{ background: {PANEL}; color: {FG}; border-bottom: 1px solid {GRID}; }}
QMenu {{ background: {PANEL}; color: {FG}; border: 1px solid {GRID}; }}
QMenu::item:selected {{ background: {GRID}; }}
QTabWidget::pane {{ border: 1px solid {GRID}; background: {BG}; }}
QTabBar::tab {{
    background: {CARD}; color: {DIM}; padding: 7px 14px;
    border: 1px solid {GRID}; border-bottom: none; margin-right: 2px;
}}
QTabBar::tab:selected {{ background: {GRID}; color: {FG}; font-weight: 600; }}
QPushButton {{
    background: {CARD}; color: {FG}; border: 1px solid {GRID};
    border-radius: 3px; padding: 4px 12px; min-height: 22px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton#primary {{
    background: {ACCENT}; color: #041018; border-color: {ACCENT}; font-weight: 600;
}}
QPushButton#primary:hover {{ background: #7dd3fc; }}
QPushButton#danger {{
    background: #3f1d1d; color: {BAD}; border-color: #7f1d1d;
}}
QPushButton#connected {{
    background: {OK}; color: #041018; border-color: {OK}; font-weight: 600;
}}
QPushButton#active {{
    background: {WARN}; color: #041018; border-color: {WARN}; font-weight: 600;
}}
QPushButton:disabled {{ color: {MUTED}; background: {PANEL}; }}
QPushButton#connected:disabled {{
    background: {OK}; color: #041018; border-color: {OK};
}}
QPushButton#active:disabled {{
    background: {WARN}; color: #041018; border-color: {WARN};
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {CARD}; color: {FG}; border: 1px solid {GRID};
    border-radius: 3px; padding: 3px 6px; min-height: 22px;
}}
QComboBox QAbstractItemView {{
    background: {PANEL}; color: {FG}; selection-background-color: {GRID};
}}
QTableWidget, QTreeWidget, QListWidget, QPlainTextEdit, QTextEdit {{
    background: {CARD}; color: {FG}; border: 1px solid {GRID};
    gridline-color: {GRID}; alternate-background-color: {PANEL};
    selection-background-color: {GRID}; selection-color: {FG};
}}
QHeaderView::section {{
    background: {PANEL}; color: {DIM}; padding: 4px 8px;
    border: none; border-bottom: 1px solid {GRID}; border-right: 1px solid {GRID};
}}
QProgressBar {{
    border: 1px solid {GRID}; border-radius: 3px; height: 14px;
    background: {PANEL}; color: {FG}; text-align: center;
}}
QProgressBar::chunk {{ background: {ACCENT}; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {GRID}; color: {DIM}; }}
QWidget#sessionBar {{ background: {PANEL}; border-bottom: 1px solid {GRID}; }}
QWidget#controlColumn {{ background: {BG}; }}
QWidget#outputPane {{ background: {BG}; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#ok {{ color: {OK}; font-weight: 600; }}
QLabel#warn {{ color: {WARN}; font-weight: 600; }}
QLabel#bad {{ color: {BAD}; font-weight: 600; }}
QFrame#card {{
    background: {CARD}; border: 1px solid {GRID}; border-radius: 6px;
}}
QGroupBox {{
    color: {DIM}; border: 1px solid {GRID}; border-radius: 6px;
    margin-top: 10px; background: {CARD}; font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}
QCheckBox {{ color: {FG}; spacing: 8px; }}
QCheckBox::indicator, QListWidget::indicator, QListView::indicator {{
    width: 16px; height: 16px;
    border: none;
    background: transparent;
}}
QCheckBox::indicator:unchecked, QListWidget::indicator:unchecked,
QListView::indicator:unchecked {{
    image: url("{_CHECK_OFF}");
}}
QCheckBox::indicator:checked, QListWidget::indicator:checked,
QListView::indicator:checked {{
    image: url("{_CHECK_ON}");
}}
QScrollBar:vertical {{ background: {PANEL}; width: 12px; }}
QScrollBar::handle:vertical {{ background: {GRID}; min-height: 24px; border-radius: 4px; }}
QScrollBar:horizontal {{ background: {PANEL}; height: 12px; }}
QScrollBar::handle:horizontal {{ background: {GRID}; min-width: 24px; border-radius: 4px; }}
QWidget#liveChart {{ background: {CARD}; }}
QToolTip {{ background: {PANEL}; color: {FG}; border: 1px solid {GRID}; }}
"""


def apply(app: QApplication) -> None:
    app.setStyle(QStyleFactory.create("Fusion"))
    pal = QPalette()
    window = QColor(BG)
    base = QColor(CARD)
    text = QColor(FG)
    hi = QColor(ACCENT)
    pal.setColor(QPalette.ColorRole.Window, window)
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Base, base)
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(PANEL))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(PANEL))
    pal.setColor(QPalette.ColorRole.ToolTipText, text)
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.Button, QColor(CARD))
    pal.setColor(QPalette.ColorRole.ButtonText, text)
    pal.setColor(QPalette.ColorRole.Highlight, hi)
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#041018"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(MUTED))
    pal.setColor(QPalette.ColorRole.BrightText, QColor(BAD))
    app.setPalette(pal)
    app.setStyleSheet(SHEET)
