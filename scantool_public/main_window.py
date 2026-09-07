"""Public shell — compact session bar + five tabs."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from scantool_public import PRODUCT_NAME, __version__
from scantool_public.features.identity import vehicle_header
from scantool_public.prefs import load_prefs, match_adapter, save_prefs
from scantool_public.session import Session, adapters
from scantool_public.ui.datalog_page import DatalogPage
from scantool_public.ui.e92_page import E92Page
from scantool_public.ui.identity_page import IdentityPage
from scantool_public.ui.scan_page import ScanPage
from scantool_public.ui.widgets import compact_button, set_button_role
from scantool_public.ui.write_page import WritePage


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{PRODUCT_NAME}  {__version__}")
        self.resize(1180, 760)
        self._session = Session()
        self._adapters: list[dict] = []
        self._busy = False
        self._bar_connected = False

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("sessionBar")
        brow = QHBoxLayout(bar)
        brow.setContentsMargins(10, 6, 10, 6)
        brow.setSpacing(8)
        brand = QLabel(f"<b>{PRODUCT_NAME}</b>")
        brand.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        brow.addWidget(brand)
        self.adapter = QComboBox()
        self.adapter.setMinimumWidth(220)
        self.adapter.setMaximumWidth(360)
        brow.addWidget(self.adapter)
        self.bitrate = QComboBox()
        self.bitrate.addItems(["500000", "250000", "33333"])
        self.bitrate.setCurrentText("500000")
        self.bitrate.setMaximumWidth(100)
        brow.addWidget(self.bitrate)
        self.btn_refresh = compact_button("Refresh")
        self.btn_refresh.clicked.connect(self._refresh_adapters)
        brow.addWidget(self.btn_refresh)
        self.btn_connect = compact_button("Connect", primary=True)
        self.btn_connect.clicked.connect(self._connect)
        brow.addWidget(self.btn_connect)
        self.btn_disc = compact_button("Disconnect")
        self.btn_disc.clicked.connect(self._session.disconnect)
        brow.addWidget(self.btn_disc)
        self.ident = QLabel("No vehicle")
        self.ident.setObjectName("muted")
        brow.addWidget(self.ident, stretch=1)
        root.addWidget(bar)

        tabs = QTabWidget()
        tabs.addTab(ScanPage(self._session), "Scan")
        tabs.addTab(IdentityPage(self._session), "Identity")
        tabs.addTab(DatalogPage(self._session), "Datalog")
        tabs.addTab(E92Page(self._session), "E92 read")
        tabs.addTab(WritePage(self._session), "Write")
        root.addWidget(tabs, stretch=1)

        status = QStatusBar()
        self.setStatusBar(status)
        self._status = QLabel("Disconnected")
        status.addWidget(self._status, stretch=1)

        self._session.connected_changed.connect(self._on_conn)
        self._session.status_changed.connect(self._status.setText)
        self._session.identity_ready.connect(self._on_ident)
        self._session.e92_progress.connect(self._refresh_ident)
        self._session.busy_changed.connect(self._on_busy)
        self._session.error.connect(self._on_error)

        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction("Refresh adapters", self._refresh_adapters)
        file_menu.addSeparator()
        file_menu.addAction("Quit", self.close)
        help_menu = self.menuBar().addMenu("Help")
        help_menu.addAction("About", self._about)

        self._refresh_adapters()
        self._on_conn(False)

    def _refresh_adapters(self) -> None:
        prefs = load_prefs()
        current = self.adapter.currentText()
        self._adapters = adapters()
        self.adapter.clear()
        for a in self._adapters:
            self.adapter.addItem(a["label"])
        idx = match_adapter(self._adapters, prefs)
        if idx == 0 and current:
            for i, a in enumerate(self._adapters):
                if a.get("label") == current:
                    idx = i
                    break
        if 0 <= idx < self.adapter.count():
            self.adapter.setCurrentIndex(idx)
        br = str(int(prefs.get("bitrate") or 500_000))
        if self.bitrate.findText(br) >= 0:
            self.bitrate.setCurrentText(br)

    def _selected(self) -> dict:
        i = self.adapter.currentIndex()
        if 0 <= i < len(self._adapters):
            return self._adapters[i]
        return {"kind": "demo", "channel": 0}

    def _connect(self) -> None:
        a = self._selected()
        try:
            bitrate = int(self.bitrate.currentText())
        except ValueError:
            bitrate = 500_000
        save_prefs(
            {
                "adapter_kind": a.get("kind") or "",
                "adapter_interface": a.get("interface") or a.get("kind") or "",
                "adapter_channel": str(a.get("channel") if a.get("channel") is not None else ""),
                "bitrate": bitrate,
            }
        )
        self._session.connect_adapter(a, bitrate=bitrate)

    def _sync_connect_button(self) -> None:
        connecting = self._busy and self._session.current_op == "connect"
        connected = self._bar_connected
        self.btn_connect.setEnabled((not connected) and not self._busy)
        self.btn_disc.setEnabled(connected and not self._busy)
        self.adapter.setEnabled((not connected) and not self._busy)
        self.bitrate.setEnabled((not connected) and not self._busy)
        self.btn_refresh.setEnabled((not connected) and not self._busy)
        if connected:
            set_button_role(self.btn_connect, "connected", text="Connected")
        elif connecting:
            set_button_role(self.btn_connect, "active", text="Connecting…")
        else:
            set_button_role(self.btn_connect, "primary", text="Connect")

    def _on_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync_connect_button()

    def _on_conn(self, ok: bool) -> None:
        self._bar_connected = bool(ok)
        self._sync_connect_button()
        self._refresh_ident()

    def _on_ident(self, _info: dict) -> None:
        self._refresh_ident()

    def _refresh_ident(self, *_args) -> None:
        connected = self._bar_connected
        reading = (not connected) and bool(self._session.last_identity)
        self.ident.setText(
            vehicle_header(
                self._session.last_identity,
                connected=connected,
                reading=reading,
            )
        )

    def _on_error(self, msg: str) -> None:
        self._status.setText(msg)
        QMessageBox.warning(self, PRODUCT_NAME, msg)

    def _about(self) -> None:
        QMessageBox.information(
            self,
            f"About {PRODUCT_NAME}",
            f"{PRODUCT_NAME} {__version__}\n\n"
            "Generic OBD-II scan (MIL, readiness, freeze frame, multi-ECU), "
            "VIN/identity, configurable datalog, E92 full-read, and EARLY E92 flash write "
            "(one dest at a time on a shared write helper).\n\n"
            "Adapters: Kvaser, PEAK, Vector, CANable (SLCAN/gs_usb), "
            "ELM327/OBDLink, SocketCAN, USB2CAN, IXXAT, J2534, neoVI, and more.\n"
            "ELM327 is scan/log only. E92 read/write needs raw CAN.\n\n"
            "Write is EARLY only. LATE modules, boot, VIN, and 0x1F000 are refused.\n\n"
            f"Copyright (C) 2026 enslaved87\n"
            "This program is free software under the GNU GPL v3 or later. "
            "It comes with ABSOLUTELY NO WARRANTY. See LICENSE in the source "
            "tree (https://github.com/enslaved87/sCANtool-public).",
        )

    def closeEvent(self, event) -> None:
        self._session.shutdown()
        super().closeEvent(event)
