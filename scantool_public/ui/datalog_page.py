"""Datalog tab — PID picker on the left, table/chart on the right."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scantool_public.features.datalog import (
    default_pids,
    pid_catalog,
    pid_meta,
    pids_for_preset,
    preset_names,
)
from scantool_public.paths import LOGS_DIR, ensure_user_dirs
from scantool_public.prefs import load_prefs, save_prefs
from scantool_public.ui.chart import LiveChart
from scantool_public.ui.widgets import (
    ActivityBar,
    PageBody,
    compact_button,
    hint_label,
    output_table,
    set_button_role,
    title_label,
)


class DatalogPage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self._s = session
        self._running = False
        self._connected = False
        self._busy = False
        self._hydrating = True
        prefs = load_prefs()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = PageBody()
        outer.addWidget(body)

        body.add_control(title_label("Datalog"))
        body.add_control(hint_label("Choose channels, set a rate, record a CSV. Mark drops an event."))

        row = QHBoxLayout()
        row.addWidget(QLabel("Preset"))
        self.preset = QComboBox()
        self.preset.setMaximumWidth(140)
        self.preset.addItems(preset_names())
        want_preset = str(prefs.get("datalog_preset") or "engine")
        if want_preset in preset_names():
            self.preset.setCurrentText(want_preset)
        self.preset.currentTextChanged.connect(self._on_preset)
        row.addWidget(self.preset)
        row.addWidget(QLabel("Hz"))
        self.hz = QDoubleSpinBox()
        self.hz.setRange(0.5, 20.0)
        self.hz.setValue(float(prefs.get("datalog_hz") or 5.0))
        self.hz.setSingleStep(0.5)
        self.hz.setMaximumWidth(80)
        row.addWidget(self.hz)
        row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        body.add_control(wrap)

        brow = QHBoxLayout()
        self.btn_start = compact_button("Start log", primary=True)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = compact_button("Stop")
        self.btn_stop.clicked.connect(self._s.stop_datalog)
        self.btn_mark = compact_button("Mark")
        self.btn_mark.setToolTip("Write a MARK event into the CSV and on the chart")
        self.btn_mark.clicked.connect(self._mark)
        brow.addWidget(self.btn_start)
        brow.addWidget(self.btn_stop)
        brow.addWidget(self.btn_mark)
        brow.addStretch(1)
        bwrap = QWidget()
        bwrap.setLayout(brow)
        body.add_control(bwrap)

        self.plist = QListWidget()
        self.plist.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        catalog = pid_catalog().get("pids") or {}
        for hex_pid, spec in catalog.items():
            item = QListWidgetItem(f"{hex_pid}  {spec.get('name') or ''}")
            item.setData(32, hex_pid)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.plist.addItem(item)
        body.add_control(self.plist)
        body.control_layout.setStretch(body.control_layout.count() - 1, 1)

        self.path_lbl = QLabel("")
        self.path_lbl.setObjectName("muted")
        self.path_lbl.setWordWrap(True)
        body.add_control(self.path_lbl)
        self.activity = ActivityBar()
        body.add_control(self.activity)

        self.table = output_table(["PID", "Value", "Label"])
        self.chart = LiveChart()
        body.add_output(self.table, 1)
        body.add_output(self.chart, 1)

        session.datalog_row.connect(self._on_row)
        session.datalog_stopped.connect(self._on_stop)
        session.connected_changed.connect(self._on_conn)
        session.busy_changed.connect(self._on_busy)
        session.activity.connect(self._on_activity)
        self._hydrating = True
        self._apply_preset(self.preset.currentText())
        self._hydrating = False
        self._sync()

    def _on_preset(self, name: str) -> None:
        self._apply_preset(name)
        if not self._hydrating:
            save_prefs({"datalog_preset": name})

    def _on_conn(self, ok: bool) -> None:
        self._connected = ok
        if not ok:
            self._running = False
        self._sync()

    def _on_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync()

    def _sync(self) -> None:
        self.btn_start.setEnabled(self._connected and not self._running and not self._busy)
        self.btn_stop.setEnabled(self._running)
        self.btn_mark.setEnabled(self._running)
        if self._running:
            set_button_role(self.btn_start, "active", text="Logging…")
        else:
            set_button_role(self.btn_start, "primary", text="Start log")

    def _on_activity(self, info: dict) -> None:
        self.activity.set_activity(
            str(info.get("text") or "Ready."),
            pct=info.get("pct"),
            active=bool(info.get("active")) or self._running,
        )

    def _checked(self) -> list[str]:
        out = []
        for i in range(self.plist.count()):
            item = self.plist.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(str(item.data(32)))
        return out or default_pids()

    def _apply_preset(self, name: str) -> None:
        want = set(pids_for_preset(name))
        for i in range(self.plist.count()):
            item = self.plist.item(i)
            pid = str(item.data(32))
            item.setCheckState(
                Qt.CheckState.Checked if pid in want else Qt.CheckState.Unchecked
            )

    def _start(self) -> None:
        ensure_user_dirs()
        pids = self._checked()
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = LOGS_DIR / f"datalog_{ts}.csv"
        hz = float(self.hz.value())
        save_prefs({"datalog_hz": hz, "datalog_preset": self.preset.currentText()})
        self._s.start_datalog(pids, hz, path)
        self._running = True
        self.path_lbl.setText(str(path))
        self._sync()
        self.table.setRowCount(len(pids))
        names = {p: str(pid_meta(p).get("name") or "") for p in pids}
        for i, p in enumerate(pids):
            self.table.setItem(i, 0, QTableWidgetItem(p))
            self.table.setItem(i, 1, QTableWidgetItem("…"))
            self.table.setItem(i, 2, QTableWidgetItem(names[p]))
        self.chart.reset(pids[:4], names)

    def _mark(self) -> None:
        self._s.mark_event("")

    def _on_row(self, row: dict) -> None:
        if row.get("event"):
            ts = float(row.get("ts") or time.time())
            self.chart.add_mark(ts)
            self.path_lbl.setText(f"Marked {row['event']}")
            return
        values = row.get("values") or {}
        texts = row.get("texts") or {}
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            pid = item.text() if item else ""
            self.table.setItem(i, 1, QTableWidgetItem(texts.get(pid) or "—"))
        ts = float(row.get("ts") or time.time())
        self.chart.add_row(ts, values)

    def _on_stop(self, path: str) -> None:
        self._running = False
        self._sync()
        if path:
            self.path_lbl.setText(f"Saved {path}")
