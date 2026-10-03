from __future__ import annotations

import os
import sys
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")


class TestGuiSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_window_tabs_and_demo_scan(self):
        from PySide6.QtWidgets import QApplication

        from scantool_public.main_window import MainWindow

        win = MainWindow()
        from PySide6.QtWidgets import QTabWidget

        tw = win.findChild(QTabWidget)
        self.assertIsNotNone(tw)
        titles = [tw.tabText(i) for i in range(tw.count())]
        self.assertEqual(titles, ["Scan", "Identity", "Datalog", "E92 read", "Write"])
        self.assertEqual(win.bitrate.currentData(), 500_000)
        self.assertEqual(win.bitrate.itemText(0), "500 kbit/s")

        # Demo is first adapter
        win.adapter.setCurrentIndex(0)
        win._connect()
        deadline = time.time() + 3
        while time.time() < deadline and not win._session.connected:
            QApplication.processEvents()
            time.sleep(0.05)
        self.assertTrue(win._session.connected)

        deadline = time.time() + 4
        while time.time() < deadline and "VIN" not in win.ident.text():
            QApplication.processEvents()
            time.sleep(0.05)
        self.assertIn("VIN", win.ident.text())
        from scantool_public.transport.adapter import DemoTransport

        self.assertIn(DemoTransport.VIN, win.ident.text())
        # Exclusive-bus drop (E92 read) must not wipe the header.
        win._session.connected_changed.emit(False)
        QApplication.processEvents()
        self.assertIn(DemoTransport.VIN, win.ident.text())
        self.assertIn("reading", win.ident.text())
        win._session.connected_changed.emit(True)
        QApplication.processEvents()

        win._session.run_scan()
        scanned = {}

        def grab(rep):
            scanned.update(rep)

        win._session.scan_ready.connect(grab)
        deadline = time.time() + 8
        while time.time() < deadline and not scanned:
            QApplication.processEvents()
            time.sleep(0.05)
        self.assertIn("dtcs", scanned)
        codes = [x["code"] for x in scanned["dtcs"]["stored"]]
        self.assertIn("P0300", codes)
        self.assertTrue(scanned.get("mil"))
        self.assertGreaterEqual(len(scanned.get("modules") or []), 2)
        self.assertEqual((scanned.get("freeze_frame") or {}).get("dtc"), "P0300")

        from scantool_public.ui.scan_page import ScanPage

        scan_page = win.findChild(ScanPage)
        self.assertIsNotNone(scan_page)
        self.assertTrue(scan_page.btn_export.isEnabled())
        self.assertGreater(scan_page.ready_table.rowCount(), 0)
        detail = scan_page.detail.toPlainText()
        self.assertIn("trigger", detail.lower())
        self.assertIn("OBD standard", detail)

        win._session.probe_identity()
        ident = {}

        def grab_id(info):
            ident.update(info)

        win._session.identity_ready.connect(grab_id)
        deadline = time.time() + 4
        while time.time() < deadline and not ident:
            QApplication.processEvents()
            time.sleep(0.05)
        from scantool_public.transport.adapter import DemoTransport

        self.assertEqual(ident.get("vin"), DemoTransport.VIN)
        self.assertEqual(
            ident.get("cal_ids"),
            [DemoTransport.CAL_PRIMARY, DemoTransport.CAL_SECONDARY],
        )
        self.assertEqual(ident.get("os_id"), "")

        from scantool_public.ui.datalog_page import DatalogPage
        from scantool_public.ui.identity_page import IdentityPage

        ipage = win.findChild(IdentityPage)
        self.assertIsNotNone(ipage)
        from scantool_public.transport.adapter import DemoTransport

        self.assertIn(DemoTransport.CAL_PRIMARY, ipage.cal.text())
        self.assertIn(DemoTransport.CAL_SECONDARY, ipage.cal.text())

        dpage = win.findChild(DatalogPage)
        self.assertIsNotNone(dpage)
        self.assertTrue(hasattr(dpage, "chart"))
        self.assertTrue(hasattr(dpage, "btn_mark"))

        rows = []
        marks = []

        def grab_row(row):
            if row.get("event"):
                marks.append(row["event"])
            elif row.get("values"):
                rows.append(row)

        win._session.datalog_row.connect(grab_row)
        dpage.hz.setValue(10.0)
        dpage._start()
        deadline = time.time() + 4
        while time.time() < deadline and not rows:
            QApplication.processEvents()
            time.sleep(0.05)
        self.assertTrue(rows, "expected a datalog sample")
        dpage._mark()
        deadline = time.time() + 2
        while time.time() < deadline and not marks:
            QApplication.processEvents()
            time.sleep(0.05)
        self.assertTrue(marks)
        win._session.stop_datalog()
        deadline = time.time() + 2
        while time.time() < deadline and dpage._running:
            QApplication.processEvents()
            time.sleep(0.05)
        self.assertFalse(dpage._running)

        from PySide6.QtCore import Qt

        for i in range(dpage.plist.count()):
            dpage.plist.item(i).setCheckState(Qt.CheckState.Unchecked)
        dpage._start()
        self.assertFalse(dpage._running)
        self.assertIn("at least one PID", dpage.path_lbl.text())

        win.close()
        QApplication.processEvents()

    def test_gui_copy_has_no_camaro(self):
        import inspect

        from PySide6.QtWidgets import QWidget

        from scantool_public.features.write_gate import write_status
        from scantool_public.main_window import MainWindow
        from scantool_public.ui.write_page import WritePage

        win = MainWindow()
        parts: list[str] = []
        for w in win.findChildren(QWidget):
            for name in (
                "text",
                "toolTip",
                "statusTip",
                "whatsThis",
                "windowTitle",
                "placeholderText",
            ):
                fn = getattr(w, name, None)
                if not callable(fn):
                    continue
                try:
                    val = fn()
                except Exception:
                    continue
                if val:
                    parts.append(str(val))
        blob = "\n".join(parts).lower()
        self.assertNotIn("camaro", blob)
        page = win.findChild(WritePage)
        self.assertIsNotNone(page)
        self.assertEqual(page.pf_early.text(), "This ECM is EARLY or LATE E92")
        clone_copy = (
            page.clone_scope_lbl.text() + " " + page.btn_clone.toolTip()
        ).lower()
        self.assertIn("not a full-chip", clone_copy)
        self.assertIn("boot", clone_copy)
        self.assertIn("bcm", clone_copy)
        self.assertNotIn("camaro", inspect.getsource(MainWindow._about).lower())
        self.assertIn("0x1f000", inspect.getsource(MainWindow._about).lower())
        gates = " ".join(
            f"{g.get('title') or ''} {g.get('detail') or ''}"
            for g in write_status().get("gates") or []
        ).lower()
        self.assertNotIn("camaro", gates)
        win.close()


if __name__ == "__main__":
    unittest.main()
