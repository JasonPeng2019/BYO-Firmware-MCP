"""M17-T synthetic bus; real registered setup, admission, plans and flash adapter.

The M16 research fixture is synthetic host data. This helper supplies no physical
identity, approval or execution evidence. Expected image bytes are parsed with
pyelftools/IntelHex independently of the product image implementation.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import struct
from types import SimpleNamespace
from unittest.mock import patch

from pyocd.core.memory_map import FlashRegion, MemoryMap, RamRegion
from pyocd_debug_mcp.adapters import swd_pyocd
from tests import m16_independent_support as partition
from tests.m02_independent_support import pattern, separate_loads, write_artifact
from tests.m05_public_support import NULL_PLAN

BASE, CAPACITY, SECTOR, BOOT_END = 0x08000000, 0x100000, 0x800, 0x6000


class AddressedBus:
    def __init__(self):
        self.memory = bytearray(pattern(CAPACITY, 83))
        self.memory[:8] = struct.pack("<II", 0x20001000, BASE + 0x181)
        self.original = bytes(self.memory)
        self.calls, self.reads = [], []
        self.state = "HALTED"
        self.mode = "write"
        self.reset_fault = False
        self.final_state = "RUNNING"
        self.part_number = "STM32L476RGTx"
        self.selected_core = SimpleNamespace(node_name="core0")
        region = FlashRegion(start=BASE, length=CAPACITY, blocksize=SECTOR)
        region.flash = SimpleNamespace(is_valid=True, has_required_sequences=True)
        self.memory_map = MemoryMap(region, RamRegion(start=0x20000000, length=0x18000))

    def reset_and_halt(self):
        self.calls.append(("reset-halt",))
        self.state = "HALTED"

    def reset(self):
        self.calls.append(("final-reset",))
        if self.reset_fault:
            raise OSError("M17-T final reset transport loss")
        self.state = self.final_state

    def get_state(self):
        return SimpleNamespace(name=self.state)

    def read_memory(self, address, width_bits, **kwargs):
        offset = address - BASE
        assert 0 <= offset < offset + width_bits // 8 <= CAPACITY
        return int.from_bytes(self.memory[offset : offset + width_bits // 8], "little")

    def read_memory_block8(self, address, count):
        offset = address - BASE
        assert 0 <= offset < offset + count <= CAPACITY
        self.calls.append(("readback", address, count))
        self.reads.append((address, count))
        return list(self.memory[offset : offset + count])

    def programmer(self, session, **options):
        assert session.target is self
        assert options == {"chip_erase": "sector"}, options
        self.calls.append(("programmer", options))

        def program(path, **kwargs):
            self.calls.append(("attempt",))
            if self.mode == "noop":
                return
            runs = separate_loads(Path(path), kwargs.get("base_address"))
            sectors = sorted(
                {
                    a // SECTOR * SECTOR
                    for start, data in runs
                    for a in range(start, start + len(data))
                }
            )
            for address in sectors:
                offset = address - BASE
                assert 0 <= offset < offset + SECTOR <= CAPACITY
                self.calls.append(("erase", address, SECTOR))
                self.memory[offset : offset + SECTOR] = b"\xff" * SECTOR
            for address, data in runs:
                offset = address - BASE
                self.memory[offset : offset + len(data)] = data
                if self.mode == "post-write-fault":
                    raise OSError("M17-T link loss after first addressed write")

        return SimpleNamespace(program=program)


@contextmanager
def workflow(test):
    bus = AddressedBus()
    original = partition.PartitionProbe
    adapter = swd_pyocd.PyOCDSWDInterface()

    def make_probe(root, family, factory):
        backend = original(root, family, factory)
        open_session, read = backend.open, backend.read_memory

        def open_bus(**kwargs):
            return replace(open_session(**kwargs), session=SimpleNamespace(target=bus))

        def read_bus(handle, address, width_bits, **kwargs):
            if BASE <= address < BASE + CAPACITY:
                return bus.read_memory(address, width_bits, **kwargs)
            return read(handle, address, width_bits, **kwargs)

        backend.open, backend.read_memory = open_bus, read_bus
        backend.flash, backend.check_flash_support = adapter.flash, adapter.check_flash_support
        return backend

    with (
        patch.object(partition, "PartitionProbe", side_effect=make_probe),
        patch.object(swd_pyocd, "FileProgrammer", side_effect=bus.programmer),
        partition.full_fixture(test, "pack") as f,
    ):
        f.bus = bus
        f.factory.request.update(start=BASE, end=BASE + BOOT_END)
        try:
            yield f
        finally:
            destination = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / test.id() / "pack"
            (destination / "independent-before.bin").write_bytes(bus.original)
            (destination / "independent-after.bin").write_bytes(bus.memory)
            (destination / "independent-bus.json").write_text(
                json.dumps(dict(calls=bus.calls, reads=bus.reads, state=bus.state), indent=2),
                encoding="utf-8",
            )


def image(f, name="independent.hex", runs=None):
    if runs is None:
        runs = (
            (BASE, struct.pack("<II", 0x20001000, BASE + 0x181) + pattern(5003, 17)),
            (BASE + 0x4000, pattern(133, 192)),
        )
    path = write_artifact(f.root / name, runs)
    if path.suffix == ".hex":
        # Public safety extraction also checks the same-build ELF execution
        # evidence; supply actual equal addressed bytes rather than bypass it.
        write_artifact(path.with_suffix(".elf"), runs)
    assert separate_loads(path) == runs
    return path, runs


def plan(f, path, permission="one-time"):
    f.flow.call("flash_bootloader-plan", NULL_PLAN)
    return f.flow.plan("flash_bootloader", f.board, dict(artifact=str(path)), permission=permission)


def execute(f, path):
    return f.call("flash_bootloader", dict(board_id=f.board, artifact=str(path)))


def admit(test, f):
    test.assertEqual(f.admit()["status"], "safety_refresh_completed")
    test.assertIsNone(f.server.gate_manager.snapshot(f.board))
    test.assertEqual(f.validate()["status"], "validation_passed")
    test.assertEqual(f.caps()["identity"]["capability"], "exact")


def affected(runs):
    return {a // SECTOR * SECTOR for s, data in runs for a in range(s, s + len(data))}


def assert_image(test, f, runs):
    for start, data in runs:
        offset = start - BASE
        test.assertEqual(bytes(f.bus.memory[offset : offset + len(data)]), data, hex(start))


def assert_outside(test, f, before, runs):
    changed = affected(runs)
    for offset in range(0, CAPACITY, SECTOR):
        if BASE + offset not in changed:
            test.assertEqual(
                bytes(f.bus.memory[offset : offset + SECTOR]),
                before[offset : offset + SECTOR],
                hex(BASE + offset),
            )
