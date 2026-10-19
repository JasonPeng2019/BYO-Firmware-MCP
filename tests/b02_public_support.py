"""Independent B02 fixtures. Only native inventory and target I/O are controlled."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace
from unittest.mock import patch

from elftools.elf.elffile import ELFFile

from tests.m05_public_support import PublicSetup
from tests.test_m05_lite_validation import advertised_validation
from tests.test_m05_public_lifecycle import MemoryProbe
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.tiered_acceptance_support import isolated_project, tiered_test_server


RAM = 0x20000000
RAM_BYTES = bytes.fromhex("f10b8c6a239ed40755c312a6ef0439b8")


def elf_fixture(path, *, base=0x08000000):
    """Hand-authored ELF32 with independent symbols, sections and load bytes."""
    text = struct.pack("<II", RAM + 0x1000, base + 0x21) + b"\x00\xbf" * 124
    entries = (("word", RAM, 4, 1), ("half", RAM + 4, 2, 1),
               ("byte", RAM + 7, 1, 1), ("unaligned", RAM + 1, 4, 1),
               ("unmapped", 0x60000000, 4, 1), ("edge", RAM + 0x40000, 4, 1),
               ("entry", base + 0x21, 4, 2))
    strings = b"\0" + b"".join(n.encode() + b"\0" for n, *_ in entries)
    names = b"\0.text\0.data\0.strtab\0.symtab\0.shstrtab\0"
    symbols = bytes(16) + b"".join(
        struct.pack("<IIIBBH", strings.index(n.encode() + b"\0"), a, size,
                    0x10 | kind, 0, 1 if kind == 2 else 2)
        for n, a, size, kind in entries)
    text_offset = 84
    strings_offset = text_offset + len(text)
    symbols_offset = strings_offset + len(strings)
    names_offset = symbols_offset + len(symbols)
    sections_offset = names_offset + len(names)
    header = struct.pack("<16sHHIIIIIHHHHHH", b"\x7fELF\x01\x01\x01" + bytes(9),
                         2, 40, 1, base + 0x21, 52, sections_offset, 0x05000000,
                         52, 32, 1, 40, 6, 5)
    program = struct.pack("<8I", 1, text_offset, base, base, len(text), len(text), 5, 4)
    sections = bytes(40)
    for name, kind, flags, address, offset, size, link, info, align, entry_size in (
        (b".text", 1, 6, base, text_offset, len(text), 0, 0, 4, 0),
        (b".data", 8, 3, RAM, 0, 16, 0, 0, 4, 0),
        (b".strtab", 3, 0, 0, strings_offset, len(strings), 0, 0, 1, 0),
        (b".symtab", 2, 0, 0, symbols_offset, len(symbols), 3, 1, 4, 16),
        (b".shstrtab", 3, 0, 0, names_offset, len(names), 0, 0, 1, 0)):
        sections += struct.pack("<10I", names.index(name), kind, flags, address,
                                offset, size, link, info, align, entry_size)
    path.write_bytes(header + program + text + strings + symbols + names + sections)
    return path


def independently_resolve(path, name):
    with path.open("rb") as stream:
        symbols = ELFFile(stream).get_section_by_name(".symtab").get_symbol_by_name(name)
        assert len(symbols) == 1
        return int(symbols[0]["st_value"]), int(symbols[0]["st_size"])


class ByteProbe(MemoryProbe):
    def __init__(self):
        super().__init__()
        self.on_read = lambda: None
        self.identity_value = None

    def read_memory(self, handle, address, width_bits, **kwargs):
        value = super().read_memory(handle, address, width_bits, **kwargs)
        if address == 0xE000ED00 and self.identity_value is not None:
            return self.identity_value
        if RAM <= address < RAM + len(RAM_BYTES):
            self.on_read()
            start = address - RAM
            return int.from_bytes(RAM_BYTES[start:start + width_bits // 8], "little")
        return value


@contextmanager
def symbol_project(test, *, tier="setup-full", validate=True):
    with isolated_project() as root, tiered_test_server(root) as server:
        backend = ByteProbe()
        server.configure_tiered_test_seams(backend=backend)
        f = SimpleNamespace(root=root, server=server, backend=backend, wire=[])

        def call(name, args):
            from tests.tiered_acceptance_support import public_text
            row = {"tool": name, "arguments": args}
            f.wire.append(row)
            try:
                row["reply"] = public_text(server, name, args)
                return row["reply"]
            except Exception as exc:
                row["error"] = str(exc)
                raise

        f.flow = PublicSetup(server, call=call)
        with patch.object(server, "_hardware_inventory", _setup_inventory()):
            route = f.flow.overview("B02 independent symbols")["routes"][0]
            f.board = route["board_id"]
            pdf = root / "synthetic.pdf"
            pdf.write_bytes(b"%PDF-1.4\n% Host-only synthetic evidence\n")
            _, plan = f.flow.accept(route, pdf, "nRF52840-QIAA", tier=tier)
            pending = f.flow.start(plan)
            test.assertEqual(pending["status"], "setup_research_required", pending)
            f.flow.answer(f.board, pending, f.flow.target_answer(pending, "nrf52840"))
            pending = f.flow.fix(plan)
            if tier == "setup-lite":
                f.flow.answer(f.board, pending, f.flow.ram_answer(pending))
                pending = f.flow.fix(plan)
            test.assertEqual(pending["status"], "setup_completed", pending)
            if validate:
                test.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
            f.elf = elf_fixture(root / "independent.elf", base=0)
            try:
                yield f
            finally:
                for board in server.connection_manager.assigned_board_ids():
                    call("disconnect", {"board_id": board})
                evidence = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / test.id()
                evidence.mkdir(parents=True, exist_ok=True)
                (evidence / "public-wire.json").write_text(json.dumps(f.wire, indent=2), encoding="utf-8")
                (evidence / "hardware.json").write_text(json.dumps(backend.calls, default=str), encoding="utf-8")
                shutil.copytree(root / ".firm", evidence / ".firm", dirs_exist_ok=True)
                shutil.copyfile(f.elf, evidence / f.elf.name)


def symbol_args(f, **extra):
    return {"board_id": f.board, "symbol": "word", "elf_artifact": str(f.elf), **extra}


def batch(f, tool, args):
    return f.flow.json("action_batch", {"board_id": f.board,
                       "actions": [{"tool_name": tool, "arguments": args}]})
