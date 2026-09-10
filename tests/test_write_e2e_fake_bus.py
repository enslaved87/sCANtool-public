"""Offline Write entire path. Fake NOR + fake W1. Never opens a CAN adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scantool_public.features.e92_write import (
    ALLOW_REUSE_KERNEL,
    CHUNK,
    FLASH_SIZE,
    MAS_55AA,
    MAS_55AA_OFF,
    SKIP_TAIL_CLASS,
    SKIP_TAIL_SIZE,
    VIN_ADDR,
    WriteBlocked,
    dest_by_addr,
    execute_write,
    prepare_image,
    rollback_committed_dests,
)
from scantool_public.paths import add_vendor_to_path, WRITE_KERNEL

VIN = "TESTSCANT00L00001"
OSID = "12664769"
ECU_ID = 0x7E8


def _image() -> bytearray:
    raw = bytearray(b"\x11" * FLASH_SIZE)
    raw[VIN_ADDR : VIN_ADDR + 17] = VIN.encode("ascii")
    raw[0xC0110 : 0xC0118] = OSID.encode("ascii")
    raw[MAS_55AA_OFF : MAS_55AA_OFF + 2] = MAS_55AA
    # Distinct AND-safe patterns (no 0x00) so dest 1/2 dump-match is visible.
    raw[0x40000:0x60000] = bytes(0x33 for _ in range(0x20000))
    raw[0x60000:0x80000] = bytes(0x55 for _ in range(0x20000))
    return raw


class FakeBus:
    """ISO-TP-ish W1: $6B erase, $6C 4 KiB AND-program, $11 reset."""

    def __init__(self, world: "World"):
        self.world = world
        self.rx: list[bytes] = []
        self.erases: list[int] = []
        self.programs: list[int] = []
        self.resets = 0
        self.fail_erase: set[int] = set()
        self._fill: tuple[int, bytearray] | None = None

    def send(self, msg) -> None:
        data = bytes(msg.data)[:8].ljust(8, b"\x00")
        if self._fill is not None:
            addr, buf = self._fill
            buf += data
            if len(buf) >= CHUNK:
                chunk = bytes(buf[:CHUNK])
                self._program(addr, chunk)
                self._fill = None
                self.rx.append(addr.to_bytes(4, "big") + b"SCPB")
            else:
                self._fill = (addr, buf)
            return
        n = data[0] & 0x0F
        payload = data[1 : 1 + n]
        if not payload:
            return
        if payload[0] == 0x6B and len(payload) >= 5:
            addr = int.from_bytes(payload[1:5], "big")
            if addr in self.fail_erase:
                return
            self._erase(addr)
            self.rx.append(addr.to_bytes(4, "big") + b"SCPB")
            return
        if payload[0] == 0x6C and len(payload) >= 5:
            addr = int.from_bytes(payload[1:5], "big")
            self._fill = (addr, bytearray())
            return
        if payload[0] == 0x11:
            self.resets += 1
            self.world.alive = False

    def recv(self, timeout=None):
        del timeout
        if not self.rx:
            return None
        return SimpleNamespace(
            data=self.rx.pop(0),
            arbitration_id=ECU_ID,
            is_error_frame=False,
        )

    def shutdown(self) -> None:
        return

    def _erase(self, addr: int) -> None:
        dest = dest_by_addr(addr)
        self.world.nor[dest.addr : dest.addr + dest.size] = b"\xff" * dest.size
        self.erases.append(addr)

    def _program(self, addr: int, chunk: bytes) -> None:
        for i, b in enumerate(chunk):
            self.world.nor[addr + i] &= b
        self.programs.append(addr)


class FakeExtractor:
    def __init__(self, bus, log=None, detail_log=None, variant=None, stop_check=None, kernel_path=None):
        del log, detail_log, stop_check, kernel_path
        self.bus = bus
        self.world = bus.world
        self.variant = variant
        self.uds = SimpleNamespace(
            stack=SimpleNamespace(reset=lambda: None),
            drain_rx=lambda _t: None,
            uds_read_memory_block=self._read,
        )

    def is_kernel_alive(self, _timeout=0.5) -> bool:
        return bool(self.world.alive)

    def probe_identity(self):
        return self.world.vin, self.world.osid

    def upload_kernel(self, require_early=True) -> bool:
        del require_early
        self.world.uploads += 1
        self.world.alive = True
        return True

    def recover_to_stock(self) -> None:
        self.world.alive = False

    def _read(self, addr, n, timeout_s=15.0):
        del timeout_s
        if addr < 0 or addr + n > len(self.world.nor):
            return False, b""
        return True, bytes(self.world.nor[addr : addr + n])


class World:
    def __init__(self, image: bytes):
        self.image = bytes(image)
        self.nor = bytearray(b"\xaa" * FLASH_SIZE)
        self.nor[VIN_ADDR : VIN_ADDR + 17] = VIN.encode("ascii")
        self.nor[MAS_55AA_OFF : MAS_55AA_OFF + 2] = MAS_55AA
        self.uploads = 0
        self.alive = False
        self.vin = VIN
        self.osid = OSID


@pytest.fixture
def harness(monkeypatch):
    add_vendor_to_path()
    import ecu_bin_extractor

    monkeypatch.setattr(ecu_bin_extractor, "E92BinExtractor", FakeExtractor)
    monkeypatch.setattr("scantool_public.features.e92_write.time.sleep", lambda *_a, **_k: None)
    image = bytes(prepare_image(_image()))
    world = World(image)
    bus = FakeBus(world)
    return world, bus, image


def _job(image, bus, dests):
    committed = []
    n = len(dests)
    last_out = None
    for i, addr in enumerate(dests):
        last_out = execute_write(
            image=image,
            dest=addr,
            bus=bus,
            variant="early",
            reuse_kernel=(i > 0),
            reset=(i + 1 == n),
            rollback=committed,
        )
        if last_out.preread:
            committed.append((addr, last_out.preread))
    return last_out, committed


def test_write_kernel_blob_present():
    assert WRITE_KERNEL.is_file()
    assert ALLOW_REUSE_KERNEL is True


def test_two_dests_one_kernel_dump_match(harness):
    world, bus, image = harness
    out, committed = _job(image, bus, (0x40000, 0x60000))
    assert out.ok
    assert world.uploads == 1
    assert bus.erases == [0x40000, 0x60000]
    assert world.nor[0x40000:0x60000] == image[0x40000:0x60000]
    assert world.nor[0x60000:0x80000] == image[0x60000:0x80000]
    assert bus.resets == 1
    assert world.alive is False
    assert [a for a, _p in committed] == [0x40000, 0x60000]


def test_reuse_false_uploads_twice(harness):
    world, bus, image = harness
    execute_write(image=image, dest=0x40000, bus=bus, variant="early", reuse_kernel=False, reset=True)
    assert world.uploads == 1
    world.alive = False
    execute_write(image=image, dest=0x60000, bus=bus, variant="early", reuse_kernel=False, reset=True)
    assert world.uploads == 2


def test_skip_tail_fullread_refused(harness):
    _world, bus, image = harness
    raw = bytearray(image)
    for addr in SKIP_TAIL_CLASS:
        raw[addr : addr + SKIP_TAIL_SIZE] = b"\xff" * SKIP_TAIL_SIZE
    with pytest.raises(WriteBlocked) as ctx:
        execute_write(image=bytes(raw), dest=0x40000, bus=bus, variant="early")
    assert "skip-tail" in str(ctx.value).lower()
    assert _world.uploads == 0


def test_vin_mismatch_refused(harness):
    world, bus, image = harness
    world.vin = "TESTSCANT00L99999"
    with pytest.raises(WriteBlocked) as ctx:
        execute_write(image=image, dest=0x40000, bus=bus, variant="early")
    assert "match" in str(ctx.value).lower()
    assert world.uploads == 0


def test_rollback_restores_dest1_when_dest2_erase_fails(harness):
    world, bus, image = harness
    add_vendor_to_path()
    import ecu_bin_extractor

    out1 = execute_write(
        image=image,
        dest=0x40000,
        bus=bus,
        variant="early",
        reuse_kernel=False,
        reset=False,
    )
    assert out1.ok
    assert world.nor[0x40000:0x60000] == image[0x40000:0x60000]
    preread = out1.preread
    assert preread[0] == 0xAA
    bus.fail_erase.add(0x60000)
    with pytest.raises(WriteBlocked):
        execute_write(
            image=image,
            dest=0x60000,
            bus=bus,
            variant="early",
            reuse_kernel=True,
            reset=True,
            rollback=[(0x40000, preread)],
        )
    ext = ecu_bin_extractor.E92BinExtractor(bus, variant=None)
    # Dest 2 never programmed. Rollback of dest 1 is inside execute_write on fault.
    # After fault, dest 1 should be preread (0xAA) if rollback ran while kernel alive.
    # execute_write restore_after_fault + rollback_committed_dests.
    # Dest 2 erase failed before $6B ACK so dest 2 stays 0xAA. Dest 1 rollback
    # erases then programs preread.
    assert world.nor[0x40000:0x40010] == preread[:16]
    del ext
    # Direct restore still works if the in-call rollback skipped a hung kernel.
    if world.nor[0x40000:0x40010] != preread[:16]:
        world.alive = True
        n_ok, n_skip = rollback_committed_dests(
            bus, [(dest_by_addr(0x40000), preread)], FakeExtractor(bus), lambda _m: None
        )
        assert n_ok == 1
        assert n_skip == 0
        assert world.nor[0x40000:0x40010] == preread[:16]
