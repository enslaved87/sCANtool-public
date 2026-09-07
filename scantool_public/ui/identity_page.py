"""Identity tab — VIN and split calibration IDs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from scantool_public.ui.widgets import (
    ActivityBar,
    PageBody,
    compact_button,
    group,
    hint_label,
    output_log,
    set_button_role,
    title_label,
)


class IdentityPage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self._s = session
        self._connected = False
        self._busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = PageBody()
        outer.addWidget(body)

        body.add_control(title_label("Identity"))
        body.add_control(
            hint_label(
                "Mode 09 VIN, calibration IDs, and ECU name. "
                "Each CAL is listed separately — they are not scraped into an OS number."
            )
        )

        row = QHBoxLayout()
        self.btn = compact_button("Probe", primary=True)
        self.btn.clicked.connect(self._s.probe_identity)
        row.addWidget(self.btn)
        row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        body.add_control(wrap)

        box, form_host = group("Module")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.vin = QLabel("—")
        self.cal = QLabel("—")
        self.name = QLabel("—")
        self.volt = QLabel("—")
        self.rpm = QLabel("—")
        for label, w in (
            ("VIN", self.vin),
            ("Calibration IDs", self.cal),
            ("ECU name", self.name),
            ("Module voltage", self.volt),
            ("RPM", self.rpm),
        ):
            w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            w.setWordWrap(True)
            form.addRow(label, w)
        form_host.addLayout(form)
        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        form_host.addWidget(self.note)
        body.add_control(box)
        self.activity = ActivityBar()
        body.add_control(self.activity)
        body.control_layout.addStretch(1)

        self.log = output_log()
        self.log.setPlainText("Probe results also appear here.")
        body.add_output(self.log, 1)

        session.identity_ready.connect(self._on_id)
        session.connected_changed.connect(self._on_conn)
        session.busy_changed.connect(self._on_busy)
        session.activity.connect(self._on_activity)
        self._sync()

    def _on_conn(self, ok: bool) -> None:
        self._connected = ok
        if not ok and not self._s.last_identity:
            self.vin.setText("—")
            self.cal.setText("—")
            self.name.setText("—")
            self.volt.setText("—")
            self.rpm.setText("—")
            self.note.setText("")
        self._sync()

    def _on_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync()

    def _sync(self) -> None:
        probing = self._busy and self._s.current_op in ("identity", "connect")
        self.btn.setEnabled(self._connected and not self._busy)
        if probing:
            set_button_role(self.btn, "active", text="Probing…")
        else:
            set_button_role(self.btn, "primary", text="Probe")

    def _on_activity(self, info: dict) -> None:
        self.activity.set_activity(
            str(info.get("text") or "Ready."),
            pct=info.get("pct"),
            active=bool(info.get("active")),
        )

    def _on_id(self, info: dict) -> None:
        self.vin.setText(info.get("vin") or "—")
        cals = info.get("cal_ids") or []
        if cals:
            self.cal.setText("\n".join(cals))
        else:
            self.cal.setText(info.get("cal_id") or "—")
        self.name.setText(info.get("ecu_name") or "—")
        v = info.get("voltage_v")
        self.volt.setText(f"{v:.2f} V" if isinstance(v, (int, float)) else "—")
        r = info.get("rpm")
        self.rpm.setText(f"{r:.0f}" if isinstance(r, (int, float)) else "—")
        self.note.setText(info.get("note") or "")
        lines = [
            f"VIN  {self.vin.text()}",
            f"CAL  {self.cal.text().replace(chr(10), ' · ')}",
            f"ECU  {self.name.text()}",
            f"V    {self.volt.text()}",
            f"RPM  {self.rpm.text()}",
        ]
        if info.get("note"):
            lines.append(str(info["note"]))
        self.log.setPlainText("\n".join(lines))
