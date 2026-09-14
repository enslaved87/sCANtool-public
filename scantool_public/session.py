"""Single-owner CAN session. All bus I/O runs on one worker thread."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from scantool_public.features import datalog as dlog
from scantool_public.features import identity as ident
from scantool_public.features import scan as scanfeat
from scantool_public.features.e92_read import full_read, kernel_present, read_shadow
from scantool_public.features.write_gate import WriteBlocked, request_write, write_status
from scantool_public.paths import LOGS_DIR, ensure_user_dirs
from scantool_public.transport.adapter import list_adapters, open_raw_bus, open_transport
from scantool_public.transport.catalog import supports_e92_read
from scantool_public.transport.obd import ObdClient

_BUSY_CMDS = frozenset({"connect", "identity", "scan", "clear", "e92_read", "e92_shadow", "write"})


class Session(QObject):
    connected_changed = Signal(bool)
    status_changed = Signal(str)
    log_line = Signal(str)
    identity_ready = Signal(dict)
    scan_ready = Signal(dict)
    snapshot_ready = Signal(dict)
    datalog_row = Signal(dict)
    datalog_stopped = Signal(str)
    e92_progress = Signal(dict)
    e92_done = Signal(dict)
    write_status_ready = Signal(dict)
    write_progress = Signal(dict)
    write_done = Signal(dict)
    busy_changed = Signal(bool)
    activity = Signal(dict)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="scanpub-io", daemon=True)
        self._alive = True
        self._transport = None
        self._kind = ""
        self._spec: dict = {}
        self._last_spec: dict = {}
        self._channel = 0
        self._bitrate = 500_000
        self._last_bitrate = 500_000
        self._logging = False
        self._log_pids: list[str] = []
        self._log_hz = 5.0
        self._csv = None
        self._mark_n = 0
        self._cancel_read = threading.Event()
        self._reconnect_after_read = False
        self._last_identity: dict = {}
        self._last_scan: dict = {}
        self._current_op = ""
        self._thread.start()

    # --- public commands (GUI thread) ---
    def connect_adapter(self, spec, channel: int = 0, bitrate: int = 500_000) -> None:
        if isinstance(spec, str):
            spec = {
                "kind": spec,
                "interface": spec,
                "channel": channel,
                "raw_can": spec not in ("demo", "elm327"),
                "obd": True,
                "virtual": spec == "demo",
            }
        self._q.put(("connect", spec, bitrate))

    def disconnect(self) -> None:
        self._reconnect_after_read = False
        self._last_identity = {}
        self._cancel_read.set()
        self._q.put(("disconnect",))

    def probe_identity(self) -> None:
        self._q.put(("identity",))

    def run_scan(self) -> None:
        self._q.put(("scan",))

    def clear_dtcs(self) -> None:
        self._q.put(("clear",))

    def snapshot(self, pids: list[str]) -> None:
        self._q.put(("snapshot", pids))

    def start_datalog(self, pids: list[str], hz: float, path: Path) -> None:
        self._q.put(("datalog_start", pids, hz, path))

    def stop_datalog(self) -> None:
        self._logging = False
        self._q.put(("datalog_stop",))

    def mark_event(self, note: str = "") -> None:
        self._q.put(("mark", note))

    def e92_full_read(self, *, verify_double: bool) -> None:
        self._cancel_read.clear()
        self._reconnect_after_read = True
        self._q.put(("e92_read", verify_double))

    def e92_shadow_read(self) -> None:
        self._cancel_read.clear()
        self._reconnect_after_read = True
        self._q.put(("e92_shadow",))

    def cancel_e92_read(self) -> None:
        self._cancel_read.set()

    def query_write_status(self) -> None:
        self._q.put(("write_status",))

    def attempt_write(self) -> None:
        self._q.put(("write", None))

    def start_write(
        self,
        path: Path,
        dest_addrs,
        allow_image_mismatch: bool = False,
        *,
        clone: bool = False,
        restamp: bool = False,
    ) -> None:
        self._cancel_read.clear()
        self._reconnect_after_read = True
        if isinstance(dest_addrs, int):
            dest_addrs = (dest_addrs,)
        self._q.put(
            (
                "write",
                path,
                tuple(int(a) for a in dest_addrs),
                bool(allow_image_mismatch),
                bool(clone),
                bool(restamp),
            )
        )

    def cancel_write(self) -> None:
        self._cancel_read.set()

    def shutdown(self) -> None:
        self._logging = False
        self._reconnect_after_read = False
        self._cancel_read.set()
        self._alive = False
        self._q.put(("stop",))
        self._thread.join(timeout=2.0)
        self._close_transport()

    @property
    def connected(self) -> bool:
        return self._transport is not None

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def last_identity(self) -> dict:
        return dict(self._last_identity)

    @property
    def last_scan(self) -> dict:
        return dict(self._last_scan)

    @property
    def current_op(self) -> str:
        return self._current_op

    def _emit_activity(self, text: str, *, pct: int | None = None, active: bool = True) -> None:
        self.activity.emit(
            {
                "op": self._current_op,
                "text": text,
                "pct": pct,
                "active": active,
            }
        )
        self.status_changed.emit(text)

    # --- worker ---
    def _run(self) -> None:
        while self._alive:
            try:
                item = self._q.get(timeout=0.05)
            except queue.Empty:
                if self._logging:
                    self._poll_log()
                continue
            cmd = item[0]
            try:
                if cmd == "stop":
                    break
                busy = cmd in _BUSY_CMDS
                if busy:
                    self._current_op = cmd
                    self.busy_changed.emit(True)
                try:
                    if cmd == "connect":
                        self._do_connect(item[1], item[2])
                    elif cmd == "disconnect":
                        self._close_transport()
                        self.connected_changed.emit(False)
                        self._emit_activity("Disconnected", pct=0, active=False)
                    elif cmd == "identity":
                        self._do_identity()
                    elif cmd == "scan":
                        self._do_scan()
                    elif cmd == "clear":
                        self._do_clear()
                    elif cmd == "snapshot":
                        self._do_snapshot(item[1])
                    elif cmd == "datalog_start":
                        self._do_log_start(*item[1:])
                    elif cmd == "datalog_stop":
                        self._do_log_stop()
                    elif cmd == "mark":
                        self._do_mark(item[1])
                    elif cmd == "e92_read":
                        self._do_e92(item[1])
                    elif cmd == "e92_shadow":
                        self._do_e92_shadow()
                    elif cmd == "write_status":
                        self.write_status_ready.emit(write_status())
                    elif cmd == "write":
                        self._do_write(
                            item[1] if len(item) > 1 else None,
                            item[2] if len(item) > 2 else None,
                            allow_image_mismatch=bool(item[3]) if len(item) > 3 else False,
                            clone=bool(item[4]) if len(item) > 4 else False,
                            restamp=bool(item[5]) if len(item) > 5 else False,
                        )
                finally:
                    if busy:
                        self._current_op = ""
                        self.busy_changed.emit(False)
            except WriteBlocked as exc:
                self.error.emit(str(exc))
            except Exception as exc:
                self.error.emit(f"{type(exc).__name__}: {exc}")
                self.log_line.emit(f"error: {exc}")

    def _client(self) -> ObdClient:
        if self._transport is None:
            raise RuntimeError("Connect an adapter first.")
        return ObdClient(self._transport)

    def _do_connect(self, spec: dict, bitrate: int) -> None:
        self._emit_activity("Opening adapter…", active=True)
        self._close_transport()
        t = open_transport(spec, bitrate)
        t.open()
        self._transport = t
        self._spec = dict(spec)
        self._last_spec = dict(spec)
        self._kind = str(spec.get("kind") or "")
        self._channel = spec.get("channel", 0)
        self._bitrate = bitrate
        self._last_bitrate = bitrate
        self.connected_changed.emit(True)
        label = spec.get("label") or f"{self._kind} {self._channel} @ {bitrate}"
        self.log_line.emit(f"opened {label}")
        self._emit_activity(f"Connected · {label}", pct=100, active=False)
        try:
            self._do_identity()
        except Exception as exc:
            self.log_line.emit(f"identity: {exc}")

    def _close_transport(self) -> None:
        self._logging = False
        t = self._transport
        self._transport = None
        self._kind = ""
        self._spec = {}
        # Keep _last_spec / _last_bitrate so E92 read can reopen the same adapter.
        if t is not None:
            try:
                t.close()
            except Exception:
                pass
        if self._csv is not None:
            self._csv.close()
            self._csv = None

    def _do_identity(self) -> None:
        self._emit_activity("Reading VIN and calibration IDs…", active=True)
        info = ident.probe_identity(self._client())
        self._last_identity = dict(info)
        self.identity_ready.emit(info)
        cals = info.get("cal_ids") or []
        cal = cals[0] if cals else (info.get("cal_id") or "")
        bits = [x for x in (info.get("vin"), cal) if x]
        summary = " · ".join(bits) if bits else "No identity reply"
        self._emit_activity(summary, pct=100, active=False)

    def _do_scan(self) -> None:
        self._emit_activity("Scanning DTCs, MIL, and readiness…", active=True)
        report = scanfeat.run_scan(self._client())
        self._last_scan = dict(report)
        self.scan_ready.emit(report)
        n = report.get("count") or 0
        mods = len(report.get("modules") or [])
        mil = "MIL on" if report.get("mil") else "MIL off"
        self._emit_activity(
            f"Scan complete · {n} code(s) · {mods} module(s) · {mil}",
            pct=100,
            active=False,
        )

    def _do_clear(self) -> None:
        self._emit_activity("Clearing stored codes…", active=True)
        ok = scanfeat.clear_codes(self._client())
        self._emit_activity(
            "Codes cleared" if ok else "Clear not acknowledged",
            pct=100,
            active=False,
        )
        if ok:
            self._do_scan()

    def _do_snapshot(self, pids: list[str]) -> None:
        row = dlog.poll_row(self._client(), pids)
        self.snapshot_ready.emit(row)

    def _do_log_start(self, pids: list[str], hz: float, path: Path) -> None:
        ensure_user_dirs()
        self._log_pids = [p.upper() for p in pids]
        self._log_hz = max(0.5, min(20.0, float(hz)))
        self._csv = dlog.CsvLog(path, self._log_pids)
        self._mark_n = 0
        self._logging = True
        self._emit_activity(
            f"Logging {len(self._log_pids)} PIDs at {self._log_hz:g} Hz → {path.name}",
            active=True,
        )

    def _do_log_stop(self) -> None:
        self._logging = False
        path = ""
        if self._csv is not None:
            path = str(self._csv.path)
            self._csv.close()
            self._csv = None
        self.datalog_stopped.emit(path)
        self._emit_activity("Log stopped.", pct=100, active=False)

    def _do_mark(self, note: str) -> None:
        if self._csv is None:
            return
        self._mark_n += 1
        text = (note or "").strip() or f"MARK-{self._mark_n}"
        self._csv.mark(text)
        self.datalog_row.emit({"values": {}, "texts": {}, "ts": time.time(), "event": text})
        self.status_changed.emit(f"Marked {text}")

    def _poll_log(self) -> None:
        if self._transport is None:
            return
        try:
            row = dlog.poll_row(self._client(), self._log_pids)
        except Exception as exc:
            self.log_line.emit(f"log poll: {exc}")
            time.sleep(0.2)
            return
        if self._csv is not None:
            self._csv.write_row(row["values"])
        self.datalog_row.emit(row)
        # pace
        time.sleep(1.0 / max(self._log_hz, 0.5))

    def _do_e92(self, verify_double: bool) -> None:
        spec = dict(self._last_spec or self._spec)
        if not supports_e92_read(spec):
            self.e92_done.emit(
                {
                    "ok": False,
                    "error": (
                        "E92 full-read needs a raw CAN adapter (Kvaser, PEAK, Vector, "
                        "CANable SLCAN/gs_usb, SocketCAN, USB2CAN, IXXAT, J2534, …). "
                        "ELM327/OBDLink and Demo cannot upload the read kernel."
                    ),
                    "reconnected": bool(self._transport is not None),
                }
            )
            return
        if not kernel_present():
            self.e92_done.emit({"ok": False, "error": "Read kernel is not installed."})
            return
        # Exclusive bus: close the OBD session so ISO-TP owns the adapter.
        br = int(self._last_bitrate or self._bitrate or 500_000)
        self._close_transport()
        self.connected_changed.emit(False)
        self._emit_activity("E92 read — taking exclusive bus", active=True)
        self.log_line.emit("released OBD session for full-read")
        ensure_user_dirs()
        log_path = LOGS_DIR / time.strftime("e92_read_%Y%m%d-%H%M%S.txt")
        try:
            log_path.write_text("released OBD session for full-read\n", encoding="utf-8")
        except Exception:
            log_path = None

        def log(msg: str) -> None:
            self.log_line.emit(msg)
            self._emit_activity(msg, active=True)
            if log_path is None:
                return
            try:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(msg + "\n")
            except Exception:
                pass

        def prog(info: dict) -> None:
            self.e92_progress.emit(info)
            phase = str(info.get("phase") or "read")
            addr = int(info.get("addr") or 0)
            pct = info.get("pct")
            self._emit_activity(
                f"Full read ({phase})  0x{addr:X}",
                pct=int(pct) if pct is not None else None,
                active=True,
            )

        out = full_read(
            spec=spec,
            bitrate=br,
            verify_double=verify_double,
            log=log,
            stop_check=self._cancel_read.is_set,
            progress=prog,
        )
        reconnected = False
        if self._reconnect_after_read and spec:
            try:
                self._do_connect(spec, br)
                reconnected = True
                self.log_line.emit("reconnected after E92 read")
            except Exception as exc:
                self.connected_changed.emit(False)
                self.log_line.emit(f"reconnect failed: {exc}")
        payload = {
            "ok": out.ok,
            "path": str(out.path) if out.path else "",
            "vin": out.vin,
            "os_id": out.os_id,
            "checksum": out.checksum,
            "error": out.error,
            "variant": out.variant,
            "skipped_blocks": out.skipped_blocks,
            "reconnected": reconnected,
            "region": "flash",
        }
        self.e92_done.emit(payload)
        if out.vin or out.os_id:
            info = dict(self._last_identity)
            if out.vin:
                info["vin"] = out.vin
            if out.os_id:
                cals = list(info.get("cal_ids") or [])
                if out.os_id not in cals:
                    cals = [out.os_id] + cals
                info["cal_ids"] = cals
                info["cal_id"] = " · ".join(cals)
            info["present"] = True
            self._last_identity = info
            self.identity_ready.emit(info)
        if out.ok:
            extra = " · reconnected" if reconnected else ""
            self._emit_activity(
                f"Read saved · {out.os_id or 'E92'}{extra}",
                pct=100,
                active=False,
            )
        else:
            self._emit_activity(out.error or "Read failed", pct=0, active=False)

    def _do_e92_shadow(self) -> None:
        spec = dict(self._last_spec or self._spec)
        if not supports_e92_read(spec):
            self.e92_done.emit(
                {
                    "ok": False,
                    "region": "shadow",
                    "error": (
                        "Shadow password needs a raw CAN adapter. "
                        "ELM327/OBDLink and Demo cannot upload the read kernel."
                    ),
                    "reconnected": bool(self._transport is not None),
                }
            )
            return
        if not kernel_present():
            self.e92_done.emit(
                {"ok": False, "region": "shadow", "error": "Read kernel is not installed."}
            )
            return
        br = int(self._last_bitrate or self._bitrate or 500_000)
        self._close_transport()
        self.connected_changed.emit(False)
        self._emit_activity("Shadow password — taking exclusive bus", active=True)
        self.log_line.emit("released OBD session for shadow read")
        ensure_user_dirs()
        log_path = LOGS_DIR / time.strftime("e92_shadow_%Y%m%d-%H%M%S.txt")
        try:
            log_path.write_text("released OBD session for shadow read\n", encoding="utf-8")
        except Exception:
            log_path = None

        def log(msg: str) -> None:
            self.log_line.emit(msg)
            self._emit_activity(msg, active=True)
            if log_path is None:
                return
            try:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(msg + "\n")
            except Exception:
                pass

        def prog(info: dict) -> None:
            self.e92_progress.emit(info)
            addr = int(info.get("addr") or 0)
            pct = info.get("pct")
            self._emit_activity(
                f"Shadow read  0x{addr:X}",
                pct=int(pct) if pct is not None else None,
                active=True,
            )

        out = read_shadow(
            spec=spec,
            bitrate=br,
            log=log,
            stop_check=self._cancel_read.is_set,
            progress=prog,
        )
        reconnected = False
        if self._reconnect_after_read and spec:
            try:
                self._do_connect(spec, br)
                reconnected = True
                self.log_line.emit("reconnected after shadow read")
            except Exception as exc:
                self.connected_changed.emit(False)
                self.log_line.emit(f"reconnect failed: {exc}")
        self.e92_done.emit(
            {
                "ok": out.ok,
                "path": str(out.path) if out.path else "",
                "vin": out.vin,
                "os_id": out.os_id,
                "checksum": out.checksum,
                "error": out.error,
                "variant": out.variant,
                "reconnected": reconnected,
                "region": "shadow",
                "nvpwd_hex": out.nvpwd_hex,
                "nvpwd_class": out.nvpwd_class,
            }
        )
        if out.ok:
            pwd = out.nvpwd_hex or "(unparsed)"
            self._emit_activity(
                f"NVPWD {pwd}  class={out.nvpwd_class or '?'}",
                pct=100,
                active=False,
            )
        else:
            self._emit_activity(out.error or "Shadow read failed", pct=0, active=False)

    def _do_write(
        self,
        path,
        dest_addrs=None,
        allow_image_mismatch: bool = False,
        clone: bool = False,
        restamp: bool = False,
    ) -> None:
        if path is None:
            request_write()
            return
        if dest_addrs is None:
            raise WriteBlocked("One dest per kernel.")
        if isinstance(dest_addrs, int):
            dests = (dest_addrs,)
        else:
            dests = tuple(int(a) for a in dest_addrs)
        if not dests:
            raise WriteBlocked("One dest per kernel.")
        spec = dict(self._last_spec or self._spec)
        if not supports_e92_read(spec):
            self.write_done.emit(
                {
                    "ok": False,
                    "error": (
                        "E92 write needs a raw CAN adapter. "
                        "ELM327/OBDLink and Demo cannot upload the write kernel."
                    ),
                }
            )
            return
        br = int(self._last_bitrate or self._bitrate or 500_000)
        if restamp:
            from scantool_public.features.cal_cs import restamp_calibration_image

            try:
                raw = Path(path).read_bytes()
                raw, notes = restamp_calibration_image(raw)
                for note in notes:
                    self.log_line.emit(note)
                ensure_user_dirs()
                staged = LOGS_DIR / "_write_restamp.bin"
                staged.write_bytes(raw)
                path = staged
            except Exception as exc:
                self.log_line.emit(f"cal CS restamp skipped: {exc}")
        self._close_transport()
        self.connected_changed.emit(False)
        self.log_line.emit("released OBD session for write")

        def log(msg: str) -> None:
            self.log_line.emit(msg)
            self._emit_activity(msg, active=True)

        n = len(dests)
        done = 0
        payload: dict = {"ok": False, "error": "", "dests_done": 0, "path": str(path)}
        bus = None
        resident = False
        committed: list = []
        in_dest = False
        try:
            bus = open_raw_bus(spec, br)
            for i, addr in enumerate(dests):
                if self._cancel_read.is_set():
                    raise WriteBlocked("Write cancelled.")
                part = i + 1
                last = i + 1 == n

                def prog(info: dict, _i=i, _addr=addr) -> None:
                    inner = int(info.get("pct") or 0)
                    overall = int((_i * 100 + inner) / n) if n else inner
                    text = str(info.get("text") or info.get("dest") or "")
                    line = f"Write {part}/{n}  {text}".strip()
                    self.write_progress.emit({**info, "pct": overall, "text": line, "part": part, "parts": n})
                    self._emit_activity(line, pct=overall, active=True)

                self._emit_activity(f"Write {part}/{n}", active=True)
                in_dest = True
                from scantool_public.features.e92_write import live_module_is_late

                late = live_module_is_late(self._last_identity)
                out = request_write(
                    path=path,
                    dest=addr,
                    spec=spec,
                    bitrate=br,
                    variant="early",
                    log=log,
                    stop_check=self._cancel_read.is_set,
                    progress=prog,
                    bus=bus,
                    reuse_kernel=resident,
                    reset=last,
                    allow_image_mismatch=allow_image_mismatch or clone,
                    rollback=committed,
                    clone=clone,
                    restamp=False,
                    late=late,
                )
                in_dest = False
                done += int(out.dests_done or 0)
                resident = not last
                if out.preread:
                    committed.append((out.dest or addr, out.preread))
                payload["path"] = out.path or payload["path"]
            payload["ok"] = True
            payload["dests_done"] = done
        except WriteBlocked as exc:
            extra = ""
            if committed and bus is not None and not in_dest:
                from scantool_public.features.e92_write import (
                    WRITE_KERNEL,
                    _rollback_note,
                    add_vendor_to_path,
                    rollback_committed_dests,
                    reset_to_stock_best_effort,
                )

                log("write stopped — restoring dests already programmed")
                extra = ""
                try:
                    add_vendor_to_path()
                    from ecu_bin_extractor import E92BinExtractor, E92Variant  # type: ignore

                    ext = E92BinExtractor(
                        bus,
                        log=log,
                        detail_log=log,
                        variant=E92Variant.EARLY,
                        kernel_path=WRITE_KERNEL,
                    )
                    n_ok, n_skip = rollback_committed_dests(bus, committed, ext, log)
                    extra = _rollback_note(n_ok, n_skip, True)
                except Exception as rec:
                    log(f"rollback aborted: {rec}")
                    extra = _rollback_note(0, len(committed), True)
                reset_to_stock_best_effort(bus, log)
            payload = {
                "ok": False,
                "error": str(exc) + extra,
                "dests_done": done,
                "path": str(path),
            }
        finally:
            if bus is not None:
                try:
                    bus.shutdown()
                except Exception:
                    pass
        reconnected = False
        if self._reconnect_after_read and spec:
            try:
                self._do_connect(spec, br)
                reconnected = True
                self.log_line.emit("reconnected after E92 write")
            except Exception as exc:
                self.connected_changed.emit(False)
                self.log_line.emit(f"reconnect failed: {exc}")
        payload["reconnected"] = reconnected
        if reconnected:
            payload["after"] = dict(self._last_identity)
        self.write_done.emit(payload)
        if payload.get("ok"):
            extra = " · reconnected" if reconnected else ""
            after = payload.get("after") or {}
            vin = after.get("vin") or ""
            cals = after.get("cal_ids") or []
            cal = cals[0] if cals else ""
            ident_s = f"  VIN {vin}  CAL {cal}" if vin or cal else ""
            self._emit_activity(
                f"Write complete ({done} dests){extra}{ident_s}",
                pct=100,
                active=False,
            )
        else:
            self._emit_activity(payload.get("error") or "Write failed", pct=0, active=False)


def adapters() -> list[dict]:
    return list_adapters()
