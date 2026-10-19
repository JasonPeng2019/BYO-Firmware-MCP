"""M17 host proof: public M16 setup and real M02 flash, synthetic probe I/O only.

The M16 source/register research is explicitly synthetic. No real grant, probe,
vendor identity, instruction execution or physical restoration is represented.
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

from pyocd.core.memory_map import MemoryMap, RamRegion
from pyocd_debug_mcp.adapters import swd_pyocd
from tests import m16_independent_support as partition
from tests.m02_independent_support import (
    BASE,
    MemoryBoundary,
    pattern,
    separate_loads,
    write_artifact,
)
from tests.m05_public_support import NULL_PLAN


BOOT_SIZE = 0x4000
SECTOR = 0x800
CAPACITY = 0x100000


class DeploymentMemory(MemoryBoundary):
    def __init__(self):
        super().__init__()
        self.memory = bytearray(pattern(CAPACITY, 213))
        # A restorable synthetic prior image, with valid Cortex-M vector words.
        self.memory[:8] = struct.pack("<II", 0x20001000, BASE + 0x101)
        self.initial = bytes(self.memory)
        self.memory_map = MemoryMap(
            self.region(BASE, CAPACITY), RamRegion(start=0x20000000, length=0x18000)
        )
        self.final_state = "RUNNING"

    def reset(self):
        super().reset()
        self.state = self.final_state

    def programmer(self, session, **kwargs):
        programmer = super().programmer(session, **kwargs)

        def program(name, **options):
            # Model complete affected-sector erasure as well as addressed writes.
            # The untouched oracle excludes these sectors, not just image bytes.
            if self.program_mode in {"write", "corrupt"}:
                sectors = {
                    address // SECTOR * SECTOR
                    for start, data in separate_loads(Path(name), options.get("base_address"))
                    for address in range(start, start + len(data))
                }
                for start in sorted(sectors):
                    self.calls.append(("sector-erase", start, SECTOR))
                    self.memory[start - BASE : start - BASE + SECTOR] = b"\xff" * SECTOR
            return programmer.program(name, **options)

        return SimpleNamespace(program=program)


@contextmanager
def deployment_fixture(test):
    target = DeploymentMemory()
    probe_class = partition.PartitionProbe
    adapter = swd_pyocd.PyOCDSWDInterface()

    def probe(root, family, factory):
        backend = probe_class(root, family, factory)
        original_open, original_read = backend.open, backend.read_memory

        def open_session(**kwargs):
            return replace(original_open(**kwargs), session=SimpleNamespace(target=target))

        def read(handle, address, width_bits, **kwargs):
            if BASE <= address < BASE + CAPACITY:
                backend.calls.append(("read", handle.probe_uid, address, width_bits))
                return target.read_memory(address, width_bits, **kwargs)
            return original_read(handle, address, width_bits, **kwargs)

        backend.open = open_session
        backend.read_memory = read
        backend.flash = adapter.flash
        backend.check_flash_support = adapter.check_flash_support
        return backend

    with (
        patch.object(partition, "PartitionProbe", side_effect=probe),
        patch.object(swd_pyocd, "FileProgrammer", side_effect=target.programmer),
        partition.full_fixture(test, "pack") as f,
    ):
        f.target = target
        f.factory.request.update(start=BASE, end=BASE + BOOT_SIZE)
        try:
            yield f
        finally:
            destination = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / test.id() / "pack"
            (destination / "memory-before.bin").write_bytes(target.initial)
            (destination / "memory-after.bin").write_bytes(target.memory)
            (destination / "flash-io.json").write_text(
                json.dumps(
                    dict(calls=target.calls, reads=target.reads, state=target.state), indent=2
                ),
                encoding="utf-8",
            )


def boot_artifact(f, *, name="boot.elf", runs=None):
    if runs is None:
        runs = (
            (BASE, struct.pack("<II", 0x20001000, BASE + 0x101) + pattern(593, 31)),
            (BASE + 0x2000, pattern(79, 117)),
        )
    return write_artifact(f.root / name, runs), runs


def boot_plan(f, artifact, *, permission="one-time"):
    f.flow.call("flash_bootloader-plan", NULL_PLAN)
    return f.flow.plan(
        "flash_bootloader", f.board, dict(artifact=str(artifact)), permission=permission
    )


def execute_boot(f, artifact):
    return f.call("flash_bootloader", dict(board_id=f.board, artifact=str(artifact)))


def admit_and_validate(test, f):
    test.assertEqual(f.admit()["status"], "safety_refresh_completed")
    test.assertIsNone(f.server.gate_manager.snapshot(f.board))
    test.assertEqual(f.validate()["status"], "validation_passed")
    test.assertEqual(f.caps()["identity"]["capability"], "exact")
