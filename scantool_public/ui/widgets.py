"""Compact two-column page chrome. Buttons hug content; output panes scroll."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

CONTROL_WIDTH = 400
BUTTON_MAX = 220


def restyle(widget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def set_button_role(btn: QPushButton, role: str, *, text: str | None = None) -> None:
    if text is not None:
        btn.setText(text)
    btn.setObjectName(role)
    restyle(btn)


def compact_button(text: str, *, primary: bool = False, danger: bool = False) -> QPushButton:
    btn = QPushButton(text)
    btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    btn.setMaximumWidth(BUTTON_MAX)
    if primary:
        btn.setObjectName("primary")
    if danger:
        btn.setObjectName("danger")
    return btn


def hint_label(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    lab.setMaximumWidth(CONTROL_WIDTH - 16)
    lab.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return lab


def title_label(text: str) -> QLabel:
    lab = QLabel(f"<b>{text}</b>")
    lab.setMaximumWidth(CONTROL_WIDTH - 16)
    return lab


def output_log(*, min_height: int = 180) -> QPlainTextEdit:
    log = QPlainTextEdit()
    log.setObjectName("outputLog")
    log.setReadOnly(True)
    log.setMaximumBlockCount(2000)
    log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
    log.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    log.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    log.setMinimumHeight(min_height)
    log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return log


def output_table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setAlternatingRowColors(True)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    table.horizontalHeader().setStretchLastSection(True)
    table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return table


def output_tree(headers: list[str]) -> QTreeWidget:
    tree = QTreeWidget()
    tree.setHeaderLabels(headers)
    tree.setRootIsDecorated(False)
    tree.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    tree.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return tree


def group(title: str) -> tuple[QGroupBox, QVBoxLayout]:
    box = QGroupBox(title)
    box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(8, 12, 8, 8)
    lay.setSpacing(6)
    return box, lay


def card() -> tuple[QFrame, QVBoxLayout]:
    box = QFrame()
    box.setObjectName("card")
    box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(10, 8, 10, 8)
    lay.setSpacing(6)
    return box, lay


class PageBody(QWidget):
    """Left control column (capped width) + right stretching output pane."""

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 12, 12, 12)
        row.setSpacing(12)

        self.controls = QWidget()
        self.controls.setObjectName("controlColumn")
        self.controls.setMaximumWidth(CONTROL_WIDTH)
        self.controls.setMinimumWidth(280)
        self.controls.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.control_layout = QVBoxLayout(self.controls)
        self.control_layout.setContentsMargins(0, 0, 0, 0)
        self.control_layout.setSpacing(8)
        row.addWidget(self.controls, 0)

        self.output = QWidget()
        self.output.setObjectName("outputPane")
        self.output.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.output_layout = QVBoxLayout(self.output)
        self.output_layout.setContentsMargins(0, 0, 0, 0)
        self.output_layout.setSpacing(8)
        row.addWidget(self.output, 1)

    def add_control(self, widget: QWidget) -> None:
        self.control_layout.addWidget(widget)

    def add_output(self, widget: QWidget, stretch: int = 1) -> None:
        self.output_layout.addWidget(widget, stretch)


class ActivityBar(QWidget):
    """Always-on progress + a live sentence about the worker thread."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setMaximumWidth(CONTROL_WIDTH - 16)
        self.bar.setTextVisible(True)
        self.label = QLabel("Ready.")
        self.label.setObjectName("muted")
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(CONTROL_WIDTH - 16)
        lay.addWidget(self.bar)
        lay.addWidget(self.label)

    def set_activity(self, text: str, *, pct: int | None = None, active: bool = False) -> None:
        self.label.setText(text or "Ready.")
        if active and pct is None:
            self.bar.setRange(0, 0)
            return
        self.bar.setRange(0, 100)
        self.bar.setValue(max(0, min(100, int(pct if pct is not None else (100 if not active else 0)))))

    def idle(self, text: str = "Ready.") -> None:
        self.set_activity(text, pct=0, active=False)
