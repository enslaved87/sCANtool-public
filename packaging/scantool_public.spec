# -*- mode: python ; coding: utf-8 -*-
"""Onedir Windows build. Qt Widgets only — do not collect_all(PySide6)."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent
PKG = ROOT / "scantool_public"

datas = [
    (str(PKG / "data"), "scantool_public/data"),
    (str(PKG / "vendor" / "e92" / "kernel.bin"), "scantool_public/vendor/e92"),
    (str(PKG / "vendor" / "e92" / "write_kernel.bin"), "scantool_public/vendor/e92"),
    (str(PKG / "vendor" / "e92" / "gm2byte_key.py"), "scantool_public/vendor/e92"),
    (str(PKG / "vendor" / "e92" / "gm5byte_key.py"), "scantool_public/vendor/e92"),
    (str(PKG / "vendor" / "e92" / "ecu_bin_extractor.py"), "scantool_public/vendor/e92"),
]
binaries = []
hidden = [
    "can.interfaces.kvaser",
    "can.interfaces.pcan",
    "can.interfaces.slcan",
    "can.interfaces.serial",
    "can.interfaces.usb2can",
    "can.interfaces.vector",
    "serial",
    "serial.tools.list_ports",
    "isotp",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "ecu_bin_extractor",
    "gm2byte_key",
    "gm5byte_key",
]

for pkg in ("can", "isotp", "serial"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h

a = Analysis(
    [str(ROOT / "scantool_public.py")],
    pathex=[str(ROOT), str(PKG / "vendor" / "e92")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "numpy",
        "pandas",
        "tkinter",
        "IPython",
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DRender",
        "PySide6.QtBluetooth",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtDesigner",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPositioning",
        "PySide6.QtPrintSupport",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.QtQuickWidgets",
        "PySide6.QtRemoteObjects",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtSpatialAudio",
        "PySide6.QtSql",
        "PySide6.QtSvg",
        "PySide6.QtTest",
        "PySide6.QtTextToSpeech",
        "PySide6.QtUiTools",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngine",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebSockets",
        "PySide6.QtXml",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sCANtool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="sCANtool",
)
