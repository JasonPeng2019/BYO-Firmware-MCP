"""Independent M02 host boundaries; never a physical or public-setup proof.

The public dispatcher, service, flash adapter, gates, parser, plans, allocation
repository and event store stay real. Only probe/session I/O and FileProgrammer
are controlled. Expected bytes originate in fixture source, never FlashImage.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace
from unittest.mock import patch

from intelhex import IntelHex
from elftools.elf.elffile import ELFFile
from pyocd.core.memory_map import FlashRegion, MemoryMap, RamRegion

from pyocd_debug_mcp.adapters import swd_pyocd as adapter
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.capabilities.policy import Tier
from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.setup_flow import device_support
from pyocd_debug_mcp.setup_flow.validate import (
    ValidationInventory,
    ValidationProbe,
)
from tests.tiered_acceptance_support import isolated_project, public_text, tiered_test_server
from tests.m05_public_support import PublicSetup

BASE = 0x08000000
SIZE = 0x20000
BOARD = "m02_host"
UID = "m02-controlled-probe"


def pattern(size, seed):
    return bytes((i * 23 + (i >> 5) + seed) % 256 for i in range(size))


def fixture_runs():
    # Valid vector words, > two verification chunks, sparse second load whose
    # VMA is RAM. BSS is deliberately excluded from the expected file bytes.
    first = struct.pack("<II", 0x20001000, BASE + 0x101) + pattern(8993, 41)
    return ((BASE, first), (BASE + 0x4000, pattern(79, 117)))


def write_artifact(path, runs):
    """Build artifacts directly from fixture source, independently of product parsers."""
    if path.suffix == ".bin":
        assert len(runs) == 1
        path.write_bytes(runs[0][1])
    elif path.suffix == ".hex":
        image = IntelHex()
        for start, data in runs:
            image.puts(start, data)
        image.write_hex_file(str(path))
    else:
        count = len(runs) + 1
        offset = 52 + 32 * count
        headers, payload = bytearray(), bytearray()
        for i, (address, data) in enumerate(runs):
            headers.extend(
                struct.pack(
                    "<8I",
                    1,
                    offset + len(payload),
                    address if i == 0 else 0x20000000,
                    address,
                    len(data),
                    len(data) + (16 if i else 0),
                    5 if i == 0 else 6,
                    4,
                )
            )
            payload.extend(data)
        headers.extend(
            struct.pack("<8I", 1, offset + len(payload), 0x20000100, 0x20000100, 0, 128, 6, 4)
        )
        path.write_bytes(
            struct.pack(
                "<16sHHIIIIIHHHHHH",
                b"\x7fELF\x01\x01\x01" + bytes(9),
                2,
                40,
                1,
                runs[0][0] + 0x101,
                52,
                0,
                0x05000000,
                52,
                32,
                count,
                40,
                0,
                0,
            )
            + headers
            + payload
        )
    return path


def separate_loads(path, base=None):
    """Programmer/reference parser: pyelftools and IntelHex, no product helper."""
    if path.suffix == ".bin":
        assert type(base) is int
        return ((base, path.read_bytes()),)
    if path.suffix == ".hex":
        image = IntelHex(str(path))
        return tuple((s, bytes(image.tobinarray(start=s, end=e - 1))) for s, e in image.segments())
    with path.open("rb") as stream:
        return tuple(
            (int(s["p_paddr"]), s.data())
            for s in ELFFile(stream).iter_segments()
            if s["p_type"] == "PT_LOAD" and s["p_filesz"]
        )


class MemoryBoundary:
    def __init__(self):
        self.memory = bytearray(pattern(SIZE, 213))
        self.initial = bytes(self.memory)
        self.calls = []
        self.reads = []
        self.reset_line = []
        self.state = "HALTED"
        self.part_number = "STM32L476RGTx"
        self.selected_core = SimpleNamespace(node_name="core0")
        self.read_fault = None
        self.reset_fault = False
        self.program_mode = "write"
        self.before_program = lambda: None
        self.memory_map = MemoryMap(
            self.region(BASE, SIZE), RamRegion(start=0x20000000, length=0x18000)
        )

    @staticmethod
    def region(start, size, *, valid=True, sequences=True):
        region = FlashRegion(start=start, length=size, blocksize=0x800)
        region.flash = SimpleNamespace(is_valid=valid, has_required_sequences=sequences)
        return region

    def reset_and_halt(self):
        self.calls.append(("reset_and_halt",))
        self.state = "HALTED"

    def reset(self):
        self.calls.append(("reset",))
        if self.reset_fault:
            raise OSError("controlled final reset link loss")
        self.state = "RUNNING"

    def get_state(self):
        return SimpleNamespace(name=self.state)

    def read_memory(self, address, width=32, **kwargs):
        if address == 0xE000ED00:
            return 0x410FC240
        start = address - BASE
        return int.from_bytes(self.memory[start : start + width // 8], "little")

    def read_memory_block8(self, address, count):
        self.calls.append(("read", address, count))
        self.reads.append((address, count))
        data = self.memory[address - BASE : address - BASE + count]
        if self.read_fault and address >= BASE + 0x4000:
            if self.read_fault == "short":
                return list(data[:-1])
            raise OSError("controlled independent SWD read failure")
        return list(data)

    def write_memory(self, *args, **kwargs):
        raise AssertionError("unexpected target mutation outside FileProgrammer")

    def programmer(self, session, **kwargs):
        self.calls.append(("programmer", kwargs))
        assert session.target is self
        assert kwargs == {"chip_erase": "sector"}

        def program(name, **options):
            self.calls.append(("program", name, options))
            self.before_program()
            if self.program_mode == "raise":
                raise OSError("controlled programmer exception after allocation commit")
            if self.program_mode == "noop":
                return None
            loads = separate_loads(Path(name), options.get("base_address"))
            for address, data in loads:
                start = address - BASE
                assert 0 <= start < start + len(data) <= SIZE
                self.memory[start : start + len(data)] = data
            if self.program_mode == "corrupt":
                address, data = loads[-1]
                self.memory[address - BASE + len(data) - 1] ^= 0x80
            return None

        return SimpleNamespace(program=program)


class BoundaryAdapter(adapter.PyOCDSWDInterface):
    """Only session construction/close is replaced; real production flash methods."""

    def __init__(self, target):
        self.target = target

    def open(self, **kwargs):
        handle = TargetSessionHandle(
            SimpleNamespace(
                target=self.target,
                probe=SimpleNamespace(
                    unique_id=kwargs.get("unique_id"), assert_reset=self.target.reset_line.append
                ),
            ),
            kwargs.get("board"),
            kwargs.get("unique_id"),
            "m02-controlled-session",
            kwargs.get("target"),
        )
        return replace(
            handle, metadata=replace(handle.metadata, live_part_number=self.target.part_number)
        )

    def close(self, handle):
        pass


def prepare_generic_fixture(server, root):
    """Host-only FULL preparation through current public setup ownership.

    Existing retained vendor pack/PDF inputs and simulated probe/CPUID I/O
    remain fixture inputs. This does not establish physical setup or identity.
    No gate, allocation, parser, permission or plan decision is replaced.
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "fallback-support-evidence/results/hil-artifacts/.firm"
    )
    shutil.copytree(source / "packs", root / ".firm/packs")
    datasheet = root / "STM32L476RG.pdf"
    shutil.copyfile(next((source / "evidence/datasheets").glob("*.pdf")), datasheet)
    inventory = ValidationInventory(
        probes=(ValidationProbe(UID, "Controlled ST-Link", "stlink", UID),)
    )
    boundary = SimpleNamespace(
        snapshot=lambda: snapshot_from_validation_inventory(inventory),
        validation_inventory=lambda: inventory,
    )
    # M05 now requires the real plan to own its disconnected-to-live
    # transition. Direct phase calls have no such ownership. Keep that guard
    # and use the same public setup route that establishes legitimate authority.
    with patch.object(server, "_hardware_inventory", boundary):
        flow = PublicSetup(server)
        route = flow.overview("M02 host")["routes"][0]
        assert route["board_id"] == BOARD, route
        _, accepted = flow.accept(route, datasheet, "STM32L476RGT6", tier="setup-full")
        result = flow.start(accepted)
        assert result["status"] == "setup_completed", result
        loaded = flow.json("load_setup_tool", {"board_id": BOARD, "tool_name": "board_validate"})
        call = loaded["next_call"]
        validation = flow.json(call["tool"], call["arguments"])
        assert validation["status"] == "validation_passed", validation
    assert server._capability_policies.resolve(BOARD).tier is Tier.SETUP_FULL
    assert server.gate_manager.snapshot(BOARD).safety_map is not None


@contextlib.contextmanager
def public_fixture(test, *, guarded=False):
    with isolated_project() as root, tiered_test_server(root) as server:
        target = MemoryBoundary()
        server.configure_tiered_test_seams(backend=BoundaryAdapter(target))
        f = SimpleNamespace(root=root, server=server, target=target, wire=[])

        def call(tool, args):
            row = {"tool": tool, "arguments": args}
            f.wire.append(row)
            try:
                row["reply"] = public_text(server, tool, args)
                return row["reply"]
            except Exception as exc:
                row["exception"] = f"{type(exc).__name__}: {exc}"
                raise

        f.call = call
        try:
            # These import-time path constants otherwise retain the first test
            # process root. Change only their storage location, not resolution.
            with (
                patch.object(device_support, "MANIFEST_PATH", root / ".firm/packs/manifest.yaml"),
                patch.object(device_support, "PACKS_DIR", root / ".firm/packs/files"),
                patch.object(adapter, "FileProgrammer", side_effect=target.programmer),
                patch.object(
                    server,
                    "_validation_inventory",
                    return_value=ValidationInventory(
                        probes=(ValidationProbe(UID, "Controlled ST-Link", "stlink", UID),)
                    ),
                ),
            ):
                if guarded:
                    prepare_generic_fixture(server, root)
                else:
                    test.assertIn(
                        "Connected",
                        call(
                            "connect",
                            {"board_id": BOARD, "probe_uid": UID, "target": "stm32l476rgtx"},
                        ),
                    )
                target.calls.clear()
                yield f
        finally:
            f.events = "\n".join(
                p.read_text(encoding="utf-8")
                for p in sorted(root.glob(".firm/runs/*/logs/events.jsonl"))
            )
            connection = server.connection_manager.maybe_connection(BOARD)
            if connection:
                try:
                    log = connection.runtime_session.log_path
                    if log.exists() and log.read_text(encoding="utf-8") not in f.events:
                        f.events += log.read_text(encoding="utf-8")
                finally:
                    call("disconnect", {"board_id": BOARD})
            if path := os.environ.get("M02_JOURNAL"):
                with Path(path).open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "test": test.id(),
                                "wire": f.wire,
                                "hardware": target.calls,
                                "reads": target.reads,
                                "reset_line": target.reset_line,
                                "events": getattr(f, "events", ""),
                                "final_policy": server._capability_policies.resolve(
                                    BOARD
                                ).map_snapshot,
                                "memory_sha256": hashlib.sha256(target.memory).hexdigest(),
                                "server_import": str(Path(server.__file__).resolve()),
                            },
                            default=str,
                        )
                        + "\n"
                    )


def plan(f, tool, artifact, *, permission=None):
    fields = dict(
        board_id=BOARD,
        hypothesis="This exact artifact is contained by the current flash authority.",
        strategy="Program this immutable artifact once and compare every addressed byte.",
        hypothesis_made=True,
        strategy_evaluated=True,
        expected_fail_return="A refusal or failed verification without a successful deployment record.",
        expected_success_return="Readback proves addressed bytes, with reset state separately reported.",
        max_calls=1,
        max_calls_buffer=0,
        action_parameters={"artifact": str(artifact)},
        user_permission=permission,
    )
    f.call(tool + "-plan", dict.fromkeys(fields))
    if permission is None and tool == "flash_application":
        fields.pop("user_permission")
    return json.loads(f.call(tool + "-plan", fields))
