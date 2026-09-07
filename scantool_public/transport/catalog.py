"""Adapter families we try to discover.

Raw-CAN interfaces can do ISO-TP (E92 full-read).
ELM327-class dongles do scan / identity / datalog only.
"""

from __future__ import annotations

# python-can interface name → public label + capability
RAW_CAN_INTERFACES: tuple[tuple[str, str], ...] = (
    ("kvaser", "Kvaser"),
    ("pcan", "PEAK PCAN"),
    ("vector", "Vector"),
    ("gs_usb", "gs_usb / candleLight / CANable"),
    ("slcan", "SLCAN"),
    ("socketcan", "SocketCAN"),
    ("usb2can", "USB2CAN"),
    ("ixxat", "IXXAT"),
    ("neovi", "Intrepid neoVI"),
    ("canalystii", "CANalyst-II"),
    ("seeedstudio", "Seeed USB-CAN"),
    ("cantact", "CANtact"),
    ("robotell", "Robotell"),
    ("iscan", "iSCAN"),
    ("nixnet", "NI-XNET"),
    ("etas", "ETAS"),
    ("j2534", "J2534 PassThru"),
    ("udp_multicast", "UDP multicast CAN"),
    ("virtual", "python-can virtual"),
)


def supports_e92_read(spec: dict) -> bool:
    return bool(spec.get("raw_can")) and not spec.get("virtual")
