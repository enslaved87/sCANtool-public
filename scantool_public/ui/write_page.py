"""EARLY E92 write tab — one dest at a time on the live helper."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scantool_public.features.e92_write import (
    FLASH_SIZE,
    calibration_dests,
    entire_dests,
    writable_dests,
    write_kernel_present,
)
from scantool_public.features.write_gate import write_status
from scantool_public.paths import WRITE_KERNEL
from scantool_public.ui.widgets import (
    ActivityBar,
    PageBody,
    compact_button,
    group,
    hint_label,
    output_log,
    output_tree,
    set_button_role,
    title_label,
)


class WritePage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self._s = session
        self._path: Path | None = None
        self._busy = False
        self._connected = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = PageBody()
        outer.addWidget(body)

        body.add_control(title_label("Write"))
        body.add_control(
            hint_label(
                "EARLY E92 only. Standard mode matches what tuners already know: "
                "Write calibration (LAS + MAS) or Write entire (cal + OS + HAS). "
                "Dests in this job share one write helper; stock OS returns "
                "when the last dest finishes. "
                "This reader's …F800 holes (0xFF fill) are replaced from live "
                "flash on write so a self-read can go back. "
                "Do not key-off during a dest."
            )
        )

        k = QLabel(
            f"Write kernel: {'ready (SCPB-W1)' if write_kernel_present() else 'not built'} · {WRITE_KERNEL.name}"
        )
        k.setWordWrap(True)
        body.add_control(k)

        row = QHBoxLayout()
        self.btn_browse = compact_button("Open image…")
        self.btn_browse.clicked.connect(self._browse)
        row.addWidget(self.btn_browse)
        row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        body.add_control(wrap)

        self.file_lbl = QLabel("No image selected.")
        self.file_lbl.setObjectName("muted")
        self.file_lbl.setWordWrap(True)
        body.add_control(self.file_lbl)

        self.advanced = QCheckBox("Advanced — write one dest")
        self.advanced.stateChanged.connect(self._sync)
        body.add_control(self.advanced)

        drow = QHBoxLayout()
        drow.addWidget(QLabel("Dest"))
        self.dest = QComboBox()
        self.dest.setObjectName("writeDest")
        self.dest.setMaximumWidth(280)
        for d in writable_dests():
            self.dest.addItem(f"{d.name}  {d.size // 1024} KiB", d.addr)
        drow.addWidget(self.dest)
        drow.addStretch(1)
        self.dest_wrap = QWidget()
        self.dest_wrap.setLayout(drow)
        body.add_control(self.dest_wrap)

        pf, play = group("Confirm")
        self.pf_early = QCheckBox("This ECM is EARLY (2-byte seed)")
        self.pf_tools = QCheckBox("Other scan tools unplugged, key ON, engine OFF")
        self.pf_brick = QCheckBox("I understand a failed write can brick the module")
        self.pf_early.stateChanged.connect(self._sync)
        self.pf_tools.stateChanged.connect(self._sync)
        self.pf_brick.stateChanged.connect(self._sync)
        play.addWidget(self.pf_early)
        play.addWidget(self.pf_tools)
        play.addWidget(self.pf_brick)
        body.add_control(pf)

        brow = QHBoxLayout()
        self.btn_cal = compact_button("Write calibration", primary=True)
        self.btn_cal.clicked.connect(lambda: self._start("calibration"))
        self.btn_entire = compact_button("Write entire", primary=True)
        self.btn_entire.clicked.connect(lambda: self._start("entire"))
        self.btn_write = compact_button("Write dest", primary=True)
        self.btn_write.clicked.connect(lambda: self._start("dest"))
        self.btn_cancel = compact_button("Cancel", danger=True)
        self.btn_cancel.clicked.connect(self._s.cancel_write)
        brow.addWidget(self.btn_cal)
        brow.addWidget(self.btn_entire)
        brow.addWidget(self.btn_write)
        brow.addWidget(self.btn_cancel)
        brow.addStretch(1)
        bwrap = QWidget()
        bwrap.setLayout(brow)
        body.add_control(bwrap)

        self.activity = ActivityBar()
        body.add_control(self.activity)
        body.control_layout.addStretch(1)

        self.tree = output_tree(["Gate", "Status", "Why"])
        self.log = output_log()
        dest_lines = "One dest per write:\n" + "\n".join(
            f"  {d.name}  {d.size // 1024} KiB" for d in writable_dests()
        )
        self.log.setPlainText(dest_lines)
        body.add_output(self.tree, 1)
        body.add_output(self.log, 2)

        session.write_status_ready.connect(self._fill)
        session.write_progress.connect(self._on_prog)
        session.write_done.connect(self._on_done)
        session.log_line.connect(self._append)
        session.connected_changed.connect(self._on_conn)
        session.busy_changed.connect(self._on_busy)
        session.activity.connect(self._on_activity)
        self._fill(write_status())
        self._sync()

    def _on_conn(self, ok: bool) -> None:
        self._connected = ok
        self._sync()

    def _on_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync()

    def _writing(self) -> bool:
        return self._busy and self._s.current_op == "write"

    def _confirmed(self) -> bool:
        return (
            self.pf_early.isChecked()
            and self.pf_tools.isChecked()
            and self.pf_brick.isChecked()
        )

    def _sync(self) -> None:
        shipped = bool(write_status().get("shipped"))
        adv = self.advanced.isChecked()
        self.dest_wrap.setVisible(adv)
        self.btn_write.setVisible(adv)
        self.btn_cal.setVisible(not adv)
        self.btn_entire.setVisible(not adv)
        ready = (
            shipped
            and self._connected
            and not self._busy
            and self._path is not None
            and self._confirmed()
            and self._s.kind not in ("demo", "elm327")
        )
        writing = self._writing()
        self.btn_cal.setEnabled(ready)
        self.btn_entire.setEnabled(ready)
        self.btn_write.setEnabled(ready)
        self.btn_cancel.setEnabled(writing)
        if writing:
            set_button_role(self.btn_cal, "active", text="Writing…")
            set_button_role(self.btn_entire, "active", text="Writing…")
            set_button_role(self.btn_write, "active", text="Writing…")
        else:
            set_button_role(self.btn_cal, "primary", text="Write calibration")
            set_button_role(self.btn_entire, "primary", text="Write entire")
            set_button_role(self.btn_write, "primary", text="Write dest")

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open 4 MiB flash image", "", "BIN (*.bin);;All files (*.*)"
        )
        if not path:
            return
        p = Path(path)
        size = p.stat().st_size if p.is_file() else 0
        self._path = p
        ok = size == FLASH_SIZE
        self.file_lbl.setText(
            f"{p.name}  ({size:,} bytes)" + ("" if ok else f"  — need exactly {FLASH_SIZE:,}")
        )
        self.file_lbl.setObjectName("ok" if ok else "bad")
        self.file_lbl.style().unpolish(self.file_lbl)
        self.file_lbl.style().polish(self.file_lbl)
        if not ok:
            self._path = None
        self._sync()

    def _start(self, mode: str) -> None:
        if self._path is None or not self._confirmed():
            return
        if self._s.kind in ("demo", "elm327") or not self._connected:
            QMessageBox.information(
                self,
                "Need raw CAN",
                "Connect a raw CAN adapter. Demo cannot flash.",
            )
            return
        if mode == "calibration":
            dests = calibration_dests()
            title = "Write calibration"
            detail = (
                "Programs the calibration dests on one write helper:\n"
                + "\n".join(f"  • {d.name}  {d.size // 1024} KiB" for d in dests)
                + "\n\nStock OS returns after the last dest. "
                "Boot / VIN / 0x1F000 are not written. Do not key-off."
            )
        elif mode == "entire":
            dests = entire_dests()
            title = "Write entire"
            detail = (
                "Programs calibration, OS, and HAS on one write helper:\n"
                + "\n".join(f"  • {d.name}  {d.size // 1024} KiB" for d in dests)
                + "\n\nStock OS returns after the last dest. "
                "HAS tiles take several minutes each. Do not key-off. "
                "Boot / VIN / 0x1F000 stay untouched."
            )
        else:
            addr = int(self.dest.currentData())
            dests = tuple(d for d in writable_dests() if d.addr == addr)
            title = "Write dest"
            name = dests[0].name if dests else self.dest.currentText()
            detail = (
                f"Program one dest ({name}) from:\n{self._path}\n\n"
                "Do not key-off until this dest finishes."
            )
        if not dests:
            return
        if QMessageBox.question(self, title, f"{detail}\n\nImage:\n{self._path}") != (
            QMessageBox.StandardButton.Yes
        ):
            return
        self.activity.set_activity(f"Starting {title.lower()}…", active=True)
        self._s.start_write(self._path, [d.addr for d in dests])

    def _fill(self, doc: dict) -> None:
        self.tree.clear()
        for g in doc.get("gates") or []:
            item = QTreeWidgetItem(
                [
                    g.get("title") or g.get("id") or "",
                    "ready" if g.get("ready") else "blocked",
                    g.get("detail") or "",
                ]
            )
            self.tree.addTopLevelItem(item)
        self.tree.resizeColumnToContents(0)

    def _append(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _on_activity(self, info: dict) -> None:
        self.activity.set_activity(
            str(info.get("text") or "Ready."),
            pct=info.get("pct"),
            active=bool(info.get("active")),
        )

    def _on_prog(self, info: dict) -> None:
        text = str(info.get("text") or info.get("dest") or "Writing…")
        self.activity.set_activity(text, pct=int(info.get("pct") or 0), active=True)

    def _on_done(self, result: dict) -> None:
        if result.get("ok"):
            msg = f"write complete  dests={result.get('dests_done')}"
            self.activity.set_activity(msg, pct=100, active=False)
            self._append(msg)
        else:
            err = result.get("error") or "write failed"
            self.activity.set_activity(err, pct=0, active=False)
            self._append(err)
        self._sync()
