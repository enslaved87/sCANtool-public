"""Scan tab — compact controls, scrolling tables."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scantool_public.features.scan import save_report
from scantool_public.paths import REPORTS_DIR, ensure_user_dirs
from scantool_public.ui.widgets import (
    ActivityBar,
    PageBody,
    compact_button,
    group,
    hint_label,
    output_log,
    output_table,
    set_button_role,
    title_label,
)


class ScanPage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self._s = session
        self._report: dict = {}
        self._connected = False
        self._busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = PageBody()
        outer.addWidget(body)

        body.add_control(title_label("Scan"))
        body.add_control(
            hint_label(
                "J1979 scan of modules 0x7E8–0x7EF: stored, pending, and permanent "
                "codes, MIL, I/M readiness, freeze frame."
            )
        )

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_scan = compact_button("Scan", primary=True)
        self.btn_scan.clicked.connect(self._s.run_scan)
        self.btn_clear = compact_button("Clear codes…", danger=True)
        self.btn_clear.clicked.connect(self._clear)
        self.btn_export = compact_button("Export")
        self.btn_export.clicked.connect(self._export)
        row.addWidget(self.btn_scan)
        row.addWidget(self.btn_clear)
        row.addWidget(self.btn_export)
        row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        body.add_control(wrap)

        summary, slay = group("Summary")
        self.summary = QLabel("Not scanned.")
        self.summary.setWordWrap(True)
        self.modules = QLabel("Modules appear after a scan.")
        self.modules.setObjectName("muted")
        self.modules.setWordWrap(True)
        slay.addWidget(self.summary)
        slay.addWidget(self.modules)
        body.add_control(summary)
        self.activity = ActivityBar()
        body.add_control(self.activity)
        body.control_layout.addStretch(1)

        self.ready_table = output_table(["Monitor", "Group", "Status"])
        self.table = output_table(["ECU", "Code", "Status", "Description"])
        self.detail = output_log(min_height=120)
        self.detail.setPlainText("Freeze frame and inspection snapshot appear here after a scan.")
        body.add_output(self.ready_table, 1)
        body.add_output(self.table, 2)
        body.add_output(self.detail, 1)

        session.scan_ready.connect(self._on_scan)
        session.connected_changed.connect(self._on_conn)
        session.busy_changed.connect(self._on_busy)
        session.activity.connect(self._on_activity)
        self._sync()

    def _on_conn(self, ok: bool) -> None:
        self._connected = ok
        self._sync()

    def _on_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync()

    def _sync(self) -> None:
        scanning = self._busy and self._s.current_op in ("scan", "clear")
        self.btn_scan.setEnabled(self._connected and not self._busy)
        self.btn_clear.setEnabled(self._connected and not self._busy)
        self.btn_export.setEnabled(bool(self._report))
        if scanning:
            set_button_role(
                self.btn_scan,
                "active",
                text="Clearing…" if self._s.current_op == "clear" else "Scanning…",
            )
        else:
            set_button_role(self.btn_scan, "primary", text="Scan")

    def _on_activity(self, info: dict) -> None:
        self.activity.set_activity(
            str(info.get("text") or "Ready."),
            pct=info.get("pct"),
            active=bool(info.get("active")),
        )

    def _clear(self) -> None:
        if QMessageBox.question(
            self,
            "Clear codes",
            "Clear stored diagnostic trouble codes?\n\nThis does not fix the fault.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._s.clear_dtcs()

    def _export(self) -> None:
        if not self._report:
            return
        ensure_user_dirs()
        suggested = str(REPORTS_DIR / "scan_report.txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export scan report", suggested, "Text (*.txt)"
        )
        if not path:
            return
        dest = save_report(self._report, path=Path(path), identity=self._s.last_identity)
        self.detail.appendPlainText(f"Saved report → {dest}")

    def _on_scan(self, report: dict) -> None:
        self._report = report
        dtcs = report.get("dtcs") or {}
        rows: list[tuple[str, str, str, str]] = []
        for status, items in dtcs.items():
            for item in items:
                rows.append(
                    (
                        item.get("ecu") or "",
                        item.get("code") or "",
                        status,
                        item.get("name") or "",
                    )
                )
        self.table.setRowCount(len(rows))
        for i, (ecu, code, status, name) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(ecu))
            self.table.setItem(i, 1, QTableWidgetItem(code))
            self.table.setItem(i, 2, QTableWidgetItem(status))
            self.table.setItem(i, 3, QTableWidgetItem(name))

        n = report.get("count") or 0
        mil = bool(report.get("mil"))
        when = report.get("when") or ""
        self.summary.setText(
            f"{n} code(s)  ·  {'MIL ON' if mil else 'MIL off'}"
            + (f"  ·  {when}" if when else "")
        )
        self.summary.setObjectName("bad" if mil else "ok")
        self.summary.style().unpolish(self.summary)
        self.summary.style().polish(self.summary)

        mods = report.get("modules") or []
        if mods:
            bits = []
            for m in mods:
                mil_s = "MIL" if m.get("mil") else "ok"
                bits.append(
                    f"{m.get('label')} 0x{m.get('txid', 0):03X} ({mil_s}, {m.get('count') or 0})"
                )
            self.modules.setText("\n".join(bits))
        else:
            self.modules.setText("No modules answered.")

        mon = report.get("monitor") or {}
        rrows = mon.get("rows") or []
        self.ready_table.setRowCount(len(rrows))
        for i, rec in enumerate(rrows):
            self.ready_table.setItem(i, 0, QTableWidgetItem(str(rec.get("name") or "")))
            self.ready_table.setItem(i, 1, QTableWidgetItem(str(rec.get("group") or "")))
            self.ready_table.setItem(i, 2, QTableWidgetItem(str(rec.get("status") or "")))

        lines: list[str] = []
        ff = report.get("freeze_frame")
        if ff:
            vals = "  ".join((v.get("text") or "") for v in (ff.get("values") or {}).values())
            trigger = ff.get("dtc") or "—"
            name = ff.get("name") or ""
            extra = f"  {name}" if name else ""
            lines.append(f"Freeze frame  trigger {trigger}{extra}")
            if vals:
                lines.append(vals)
        else:
            lines.append("No freeze frame stored.")
        live = report.get("live") or {}
        texts = [v.get("text") or "" for v in live.values() if v.get("text")]
        lines.append("")
        lines.append("Inspection snapshot")
        lines.append("  ".join(texts) if texts else "No live PIDs on this pass.")
        self.detail.setPlainText("\n".join(lines))
        self._sync()
