"""Host-only flash fixtures: addressed artifact writers and a programmable fake target.

The fake target uses pyOCD's real memory-map classes, so the live flash-support walk
sees the same region objects a session would. Its flash keeps written bytes and
answers readback; nothing here touches a probe.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import Mock

from pyocd.core.memory_map import FlashRegion, MemoryMap, RamRegion

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig

FLASH_START = 0x0800_0000
FLASH_SIZE = 0x2_0000
SRAM_START = 0x2000_0000
SRAM_SIZE = 0x1_0000


class ElfSegment(NamedTuple):
    paddr: int
    data: bytes
    memsz: int | None = None
    vaddr: int | None = None


def write_intel_hex(path: Path, runs: Iterable[tuple[int, bytes]]) -> Path:
    """Write data runs as Intel HEX with extended linear address records."""

    records: list[str] = []

    def record(kind: int, offset: int, payload: bytes) -> None:
        body = bytes((len(payload), offset >> 8, offset & 0xFF, kind)) + payload
        records.append(":" + (body + bytes(((-sum(body)) & 0xFF,))).hex().upper())

    upper: int | None = None
    for start, data in runs:
        position = 0
        while position < len(data):
            address = start + position
            size = min(16, len(data) - position, 0x1_0000 - (address & 0xFFFF))
            if address >> 16 != upper:
                upper = address >> 16
                record(4, 0, upper.to_bytes(2, "big"))
            record(0, address & 0xFFFF, data[position : position + size])
            position += size
    record(1, 0, b"")
    path.write_text("\n".join(records) + "\n", encoding="ascii")
    return path


def write_elf32(path: Path, segments: Sequence[ElfSegment], *, entry: int = 0) -> Path:
    """Write a little-endian ARM ELF32 with one PT_LOAD program header per segment."""

    header_size = 52
    program_header_size = 32
    data_offset = header_size + program_header_size * len(segments)
    program_headers = b""
    payload = b""
    for segment in segments:
        memory_size = len(segment.data) if segment.memsz is None else segment.memsz
        vaddr = segment.paddr if segment.vaddr is None else segment.vaddr
        program_headers += struct.pack(
            "<8I",
            1,
            data_offset + len(payload),
            vaddr,
            segment.paddr,
            len(segment.data),
            memory_size,
            5,
            4,
        )
        payload += segment.data
    ident = b"\x7fELF\x01\x01\x01" + bytes(9)
    header = struct.pack(
        "<16sHHIIIIIHHHHHH",
        ident,
        2,
        40,
        1,
        entry,
        header_size,
        0,
        0x0500_0000,
        header_size,
        program_header_size,
        len(segments),
        40,
        0,
        0,
    )
    path.write_bytes(header + program_headers + payload)
    return path


def flash_memory_map(*, flash: Any = None) -> MemoryMap:
    """A device map: one flash region with a usable algorithm, then SRAM."""

    region = FlashRegion(start=FLASH_START, length=FLASH_SIZE, blocksize=0x400, name="flash")
    region.flash = SimpleNamespace(is_valid=True) if flash is None else flash
    return MemoryMap(region, RamRegion(start=SRAM_START, length=SRAM_SIZE, name="sram"))


class FakeFlashTarget:
    """A target whose flash keeps programmed bytes and answers readback."""

    def __init__(
        self,
        memory_map: MemoryMap | None = None,
        *,
        part_number: str = "fake_part",
        state: str = "RUNNING",
    ) -> None:
        self.memory_map = flash_memory_map() if memory_map is None else memory_map
        self.part_number = part_number
        self.selected_core = SimpleNamespace(node_name="core0")
        self.memory: dict[int, int] = {}
        self.state = state
        self.calls: list[str] = []
        self.reads: list[tuple[int, int]] = []
        self.read_error: BaseException | None = None
        self.final_reset_error: BaseException | None = None
        self.state_error: BaseException | None = None

    def write(self, address: int, data: bytes) -> None:
        for index, value in enumerate(data):
            self.memory[address + index] = value

    def reset_and_halt(self) -> None:
        final = "program" in self.calls
        self.calls.append("reset_and_halt")
        if final and self.final_reset_error is not None:
            raise self.final_reset_error

    def reset(self) -> None:
        self.calls.append("reset")
        if self.final_reset_error is not None:
            raise self.final_reset_error

    def get_state(self) -> object:
        if self.state_error is not None:
            raise self.state_error
        return SimpleNamespace(name=self.state)

    def read_memory_block8(self, address: int, size: int) -> list[int]:
        self.calls.append("read")
        self.reads.append((address, size))
        if self.read_error is not None:
            raise self.read_error
        return [self.memory.get(item, 0xFF) for item in range(address, address + size)]


def file_programmer(
    target: FakeFlashTarget,
    writes: Iterable[tuple[int, bytes]] = (),
    *,
    error: BaseException | None = None,
) -> Mock:
    """Stand in for pyOCD's FileProgrammer class; ``writes`` is what reaches flash."""

    pending = tuple(writes)
    programmer = Mock()

    def program(*_args: object, **_kwargs: object) -> None:
        target.calls.append("program")
        if error is not None:
            raise error
        for address, data in pending:
            target.write(address, data)

    programmer.program.side_effect = program
    return Mock(return_value=programmer)


def programmed_files(programmer: Mock) -> list[tuple[Path, bytes]]:
    """Record each path a ``file_programmer`` is asked to program and its bytes then."""

    seen: list[tuple[Path, bytes]] = []
    program = programmer.return_value.program
    write = program.side_effect

    def record(path: str, **kwargs: object) -> None:
        seen.append((Path(path), Path(path).read_bytes()))
        write(path, **kwargs)

    program.side_effect = record
    return seen


def flash_handle(target: object) -> TargetSessionHandle:
    board = BoardConfig("board", "Board", "mcu", "probe", "target", "probe", (), (), 0)
    return TargetSessionHandle(SimpleNamespace(target=target), board, "probe", "fake", None)
