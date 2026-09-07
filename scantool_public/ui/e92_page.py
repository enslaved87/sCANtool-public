"""E92 full-read tab — early/late, read kernel only."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from scantool_public.features.e92_read import kernel_present
from scantool_public.paths import READ_KERNEL, READS_DIR
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


class E92Page(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self._s = session
        self._busy = False
        self._connected = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = PageBody()
        outer.addWidget(body)

        body.add_control(title_label("E92 full read"))
        body.add_control(
            hint_label(
                "Early (2-byte / algo 513) and late (5-byte / algo 146) E92 ECMs. "
                "Uploads the proven read kernel, dumps 4 MiB or the 16 KiB shadow "
                "(NVPWD / censorship password), then returns the ECM to stock. "
                "Needs raw CAN. Serial OBD dongles cannot do this."
            )
        )

        k = QLabel(
            f"Read kernel: {'ready (SCPB-R2)' if kernel_present() else 'not built'} · {READ_KERNEL.name}"
        )
        k.setWordWrap(True)
        body.add_control(k)
        dest = QLabel(f"Saves to {READS_DIR}")
        dest.setObjectName("muted")
        dest.setWordWrap(True)
        body.add_control(dest)

        pf, play = group("Preflight")
        self.pf_tools = QCheckBox("Other scan tools unplugged")
        self.pf_key = QCheckBox("Key ON, engine OFF")
        self.verify = QCheckBox("Double-read verify (about 2× longer)")
        self.pf_tools.stateChanged.connect(self._sync)
        self.pf_key.stateChanged.connect(self._sync)
        play.addWidget(self.pf_tools)
        play.addWidget(self.pf_key)
        play.addWidget(self.verify)
        body.add_control(pf)

        row = QHBoxLayout()
        self.btn_read = compact_button("Full read", primary=True)
        self.btn_read.clicked.connect(self._start)
        self.btn_shadow = compact_button("Shadow password")
        self.btn_shadow.setToolTip(
            "Read 16 KiB shadow flash and show NVPWD (censorship password). Read-only."
        )
        self.btn_shadow.clicked.connect(self._start_shadow)
        self.btn_cancel = compact_button("Cancel", danger=True)
        self.btn_cancel.clicked.connect(self._s.cancel_e92_read)
        row.addWidget(self.btn_read)
        row.addWidget(self.btn_shadow)
        row.addWidget(self.btn_cancel)
        row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        body.add_control(wrap)

        self.nvpwd_lbl = QLabel("Shadow NVPWD: not read yet.")
        self.nvpwd_lbl.setObjectName("muted")
        self.nvpwd_lbl.setWordWrap(True)
        self.nvpwd_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.add_control(self.nvpwd_lbl)

        self.activity = ActivityBar()
        body.add_control(self.activity)
        body.control_layout.addStretch(1)

        self.log = output_log()
        body.add_output(self.log, 1)

        session.log_line.connect(self._append)
        session.e92_progress.connect(self._on_prog)
        session.e92_done.connect(self._on_done)
        session.connected_changed.connect(self._on_conn)
        session.busy_changed.connect(self._on_busy)
        session.activity.connect(self._on_activity)
        self._connected = False
        self._sync()

    def _on_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync()

    def _on_conn(self, ok: bool) -> None:
        self._connected = ok
        self._sync()

    def _reading(self) -> bool:
        return self._busy and self._s.current_op in ("e92_read", "e92_shadow")

    def _sync(self) -> None:
        raw = self._connected and self._s.kind not in ("demo", "elm327")
        ready = raw and self.pf_tools.isChecked() and self.pf_key.isChecked()
        reading = self._reading()
        self.btn_read.setEnabled(ready and not self._busy)
        self.btn_shadow.setEnabled(ready and not self._busy)
        self.btn_cancel.setEnabled(reading)
        if reading and self._s.current_op == "e92_shadow":
            set_button_role(self.btn_shadow, "active", text="Reading…")
            set_button_role(self.btn_read, "primary", text="Full read")
        elif reading:
            set_button_role(self.btn_read, "active", text="Reading…")
            set_button_role(self.btn_shadow, "", text="Shadow password")
        else:
            set_button_role(self.btn_read, "primary", text="Full read")
            set_button_role(self.btn_shadow, "", text="Shadow password")

    def _start(self) -> None:
        if not (self.pf_tools.isChecked() and self.pf_key.isChecked()):
            return
        if self._s.kind in ("demo", "elm327") or not self._connected:
            QMessageBox.information(
                self,
                "Need raw CAN",
                "Connect a raw CAN adapter. Demo and serial OBD cannot dump flash.",
            )
            return
        if QMessageBox.question(
            self,
            "Start full read",
            "Upload the read kernel and dump 4 MiB.\n\n"
            "Key ON, engine OFF. Do not key-off until the ECM is back on stock OS.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.activity.set_activity("Starting full read…", active=True)
        self._s.e92_full_read(verify_double=self.verify.isChecked())

    def _start_shadow(self) -> None:
        if not (self.pf_tools.isChecked() and self.pf_key.isChecked()):
            return
        if self._s.kind in ("demo", "elm327") or not self._connected:
            QMessageBox.information(
                self,
                "Need raw CAN",
                "Connect a raw CAN adapter. Demo and serial OBD cannot read shadow.",
            )
            return
        if QMessageBox.question(
            self,
            "Read shadow password",
            "Upload the read kernel and dump 16 KiB of shadow flash.\n\n"
            "This only reads NVPWD (censorship password). It does not write it.\n"
            "Key ON, engine OFF. Do not key-off until the ECM is back on stock OS.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.activity.set_activity("Starting shadow read…", active=True)
        self._s.e92_shadow_read()

    def _append(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _on_activity(self, info: dict) -> None:
        self.activity.set_activity(
            str(info.get("text") or "Ready."),
            pct=info.get("pct"),
            active=bool(info.get("active")),
        )

    def _on_prog(self, info: dict) -> None:
        pct = info.get("pct")
        phase = str(info.get("phase") or "read")
        addr = int(info.get("addr") or 0)
        label = "Shadow read" if "shadow" in phase else f"Full read ({phase})"
        self.activity.set_activity(
            f"{label}  0x{addr:X}",
            pct=int(pct) if pct is not None else None,
            active=True,
        )

    def _on_done(self, result: dict) -> None:
        if result.get("region") == "shadow":
            if result.get("ok"):
                pwd = result.get("nvpwd_hex") or "(unparsed)"
                klass = result.get("nvpwd_class") or "?"
                recon = "  ·  bus reconnected" if result.get("reconnected") else ""
                self.nvpwd_lbl.setText(f"Shadow NVPWD: {pwd}  ({klass})")
                self.nvpwd_lbl.setObjectName("ok")
                self.nvpwd_lbl.style().unpolish(self.nvpwd_lbl)
                self.nvpwd_lbl.style().polish(self.nvpwd_lbl)
                msg = f"NVPWD {pwd}  class={klass}  saved {result.get('path')}{recon}"
                self.activity.set_activity(msg, pct=100, active=False)
                self._append(msg)
            else:
                err = result.get("error") or "shadow read failed"
                self.activity.set_activity(err, pct=0, active=False)
                self._append(err)
                if result.get("reconnected"):
                    self._append("bus reconnected after failed shadow read")
            self._sync()
            return
        if result.get("ok"):
            recon = "  ·  bus reconnected" if result.get("reconnected") else ""
            msg = (
                f"saved {result.get('path')}  VIN={result.get('vin')}  "
                f"CAL/OS tag={result.get('os_id')}{recon}"
            )
            self.activity.set_activity(msg, pct=100, active=False)
            self._append(msg)
        else:
            err = result.get("error") or "read failed"
            self.activity.set_activity(err, pct=0, active=False)
            self._append(err)
            if result.get("reconnected"):
                self._append("bus reconnected after failed read")
        self._sync()
