"""M06 host regressions: public FULL setup, real ELF lookup, fake probe I/O only."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from tests.m05_public_support import NULL_PLAN, PublicSetup
from tests.test_m05_lite_validation import advertised_validation
from tests.test_m05_public_lifecycle import MemoryProbe
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.tiered_acceptance_support import isolated_project, public_call, tiered_test_server


def symbol_elf(path: Path) -> Path:
    """A real ELF32 symbol table; no compiler or replacement symbol resolver."""
    names = b"\0value\0small\0outside\0unaligned\0function\0unknown_size\0"
    sections = b"\0.shstrtab\0.strtab\0.symtab\0.data\0"
    symbols = bytes(16)
    for name, address, size, kind in (
        (b"value", 0x20000000, 4, 1),
        (b"small", 0x20000004, 1, 1),
        (b"outside", 0x60000000, 4, 1),
        (b"unaligned", 0x20000001, 4, 1),
        (b"function", 0x20000008, 4, 2),
        (b"unknown_size", 0x2000000C, 0, 1),
    ):
        symbols += struct.pack("<IIIBBH", names.index(name), address, size, 0x10 | kind, 0, 4)
    section_offset = 52 + len(sections) + len(names) + len(symbols)
    header = b"\x7fELF\x01\x01\x01" + bytes(9)
    header += struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 0, section_offset, 0, 52, 0, 0, 40, 5, 1)
    table = bytes(40)
    for name, kind, flags, address, offset, size, link, info, alignment, entry_size in (
        (b".shstrtab", 3, 0, 0, 52, len(sections), 0, 0, 1, 0),
        (b".strtab", 3, 0, 0, 52 + len(sections), len(names), 0, 0, 1, 0),
        (b".symtab", 2, 0, 0, 52 + len(sections) + len(names), len(symbols), 2, 1, 4, 16),
        (b".data", 8, 3, 0x20000000, 0, 16, 0, 0, 4, 0),
    ):
        table += struct.pack(
            "<10I",
            sections.index(name),
            kind,
            flags,
            address,
            offset,
            size,
            link,
            info,
            alignment,
            entry_size,
        )
    path.write_bytes(header + sections + names + symbols + table)
    return path


class PublicSymbolReadTests(unittest.TestCase):
    @contextmanager
    def project(self, *, tier="setup-full", validate=True):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = MemoryProbe()
            server.configure_tiered_test_seams(backend=backend)
            with patch.object(server, "_hardware_inventory", _setup_inventory()):
                flow = PublicSetup(server)
                route = flow.overview("M06 synthetic host board")["routes"][0]
                board = route["board_id"]
                pdf = root / "synthetic.pdf"
                pdf.write_bytes(b"%PDF-1.4\n% Synthetic host evidence only\n")
                _, plan = flow.accept(route, pdf, "nRF52840-QIAA", tier=tier)
                pending = flow.start(plan)
                self.assertEqual(pending["status"], "setup_research_required", pending)
                flow.answer(board, pending, flow.target_answer(pending, "nrf52840"))
                pending = flow.fix(plan)
                if tier == "setup-lite":
                    flow.answer(board, pending, flow.ram_answer(pending))
                    pending = flow.fix(plan)
                self.assertEqual(pending["status"], "setup_completed", pending)
                if validate:
                    self.assertEqual(
                        advertised_validation(flow, board)["status"], "validation_passed"
                    )
                artifact = symbol_elf(root / "variables.elf")
                try:
                    yield root, server, backend, flow, board, artifact
                finally:
                    if evidence := os.environ.get("BYO_MANAGED_TEST_EVIDENCE"):
                        snapshot = Path(evidence) / root.name
                        snapshot.mkdir(parents=True, exist_ok=True)
                        for path in root.iterdir():
                            if path.is_file() and path.suffix in {".elf", ".axf", ".pdf"}:
                                shutil.copyfile(path, snapshot / path.name)
                        if (root / ".firm").is_dir():
                            shutil.copytree(root / ".firm", snapshot / ".firm")
                        (snapshot / "backend-calls.json").write_text(
                            json.dumps(backend.calls, indent=2, default=str), encoding="utf-8"
                        )
                    for connected in server.connection_manager.assigned_board_ids():
                        flow.call("disconnect", {"board_id": connected})

    @staticmethod
    def arguments(board, artifact, **extra):
        return {"board_id": board, "symbol": "value", "elf_artifact": str(artifact), **extra}

    def test_public_registration_direct_and_batch_agree_with_planned_address(self):
        with self.project() as (_, server, backend, flow, board, artifact):
            tools = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
            self.assertIn("read_memory_symbol", tools)
            self.assertNotIn("read_memory_symbol-plan", tools)
            args = self.arguments(board, artifact)
            before = len(backend.calls)
            direct = flow.call("read_memory_symbol", args)
            self.assertIn("value=0x12345678", direct)
            batch = flow.json(
                "action_batch",
                {
                    "board_id": board,
                    "actions": [{"tool_name": "read_memory_symbol", "arguments": args}],
                },
            )
            self.assertEqual(batch["status"], "batch_completed", batch)
            self.assertIn("value=0x12345678", str(batch["completed"][0]["result"]))
            flow.call("read_memory_address-plan", NULL_PLAN)
            plan = flow.plan(
                "read_memory_address", board, {"address": 0x20000000, "width": 32, "length": None}
            )
            self.assertEqual(plan["status"], "plan_accepted", plan)
            address = flow.call(
                "read_memory_address",
                {"board_id": board, "address": 0x20000000, "width": 32, "length": None},
            )
            self.assertIn("0x12345678", address)
            self.assertEqual(
                [c[2:] for c in backend.calls[before:] if c[0] == "read_memory"],
                [(0x20000000, 32)] * 3,
            )

    def test_invalid_artifacts_symbols_objects_widths_and_ranges_do_no_memory_io(self):
        with self.project() as (root, _, backend, flow, board, artifact):
            invalid = root / "invalid.elf"
            invalid.write_bytes(b"not an ELF")
            truncated = root / "truncated.elf"
            truncated.write_bytes(b"\x7fELF")
            for extra, code in (
                ({"elf_artifact": None}, "memory/symbol-artifact-unavailable"),
                ({"elf_artifact": str(root / "missing.elf")}, "memory/symbol-artifact-unavailable"),
                ({"elf_artifact": str(invalid)}, "memory/symbol-artifact-unavailable"),
                ({"elf_artifact": str(truncated)}, "memory/symbol-artifact-parse-failed"),
                ({"symbol": "absent"}, "memory/symbol-not-found"),
                ({"symbol": ""}, "memory/empty-symbol"),
                ({"width": 64}, "memory/invalid-width"),
                ({"symbol": "small"}, "memory/symbol-width-exceeds-object"),
                ({"symbol": "unknown_size"}, "memory/symbol-size-unknown"),
                ({"symbol": "function"}, "memory/symbol-is-function"),
                ({"symbol": "unaligned"}, "memory/symbol-address-unaligned"),
            ):
                with self.subTest(extra=extra):
                    before = list(backend.calls)
                    result = flow.call(
                        "read_memory_symbol", self.arguments(board, artifact, **extra)
                    )
                    self.assertIn(code, result)
                    self.assertEqual(
                        backend.calls[len(before) :], [("release_reset", "LITE-ACCEPTANCE-PROBE")]
                    )
            before = list(backend.calls)
            with self.assertRaisesRegex(
                Exception, "not covered by one authoritative mapped region"
            ):
                flow.call("read_memory_symbol", self.arguments(board, artifact, symbol="outside"))
            self.assertEqual(
                backend.calls[len(before) :], [("release_reset", "LITE-ACCEPTANCE-PROBE")]
            )

    def test_disconnect_reconnect_requires_public_revalidation(self):
        with self.project() as (_, server, backend, flow, board, artifact):
            args = self.arguments(board, artifact)
            self.assertIn("value=", flow.call("read_memory_symbol", args))
            session = server._active_session_id(board)
            flow.call("disconnect", {"board_id": board})
            before = list(backend.calls)
            with self.assertRaises(Exception):
                flow.call("read_memory_symbol", args)
            self.assertEqual(backend.calls, before)
            flow.call("connect", {"board_id": board})
            self.assertNotEqual(session, server._active_session_id(board))
            before = list(backend.calls)
            with self.assertRaisesRegex(Exception, "not validated"):
                flow.call("read_memory_symbol", args)
            self.assertEqual(backend.calls, before)
            flow.overview("M06 synthetic host board")
            self.assertEqual(advertised_validation(flow, board)["status"], "validation_passed")
            self.assertIn("value=", flow.call("read_memory_symbol", args))

    def test_restart_needs_fresh_public_validation_and_explicit_project_elf(self):
        with self.project() as (root, _, _, flow, board, artifact):
            self.assertIn(
                "value=", flow.call("read_memory_symbol", self.arguments(board, artifact))
            )
            flow.call("disconnect", {"board_id": board})
            code = """import sys
from pathlib import Path
from unittest.mock import patch
from tests.test_m06_public_symbol_reads import MemoryProbe, PublicSetup, _setup_inventory
from tests.test_m05_lite_validation import advertised_validation
from tests.tiered_acceptance_support import tiered_test_server
root, board, artifact = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
with tiered_test_server(root) as server:
    backend = MemoryProbe()
    server.configure_tiered_test_seams(backend=backend)
    with patch.object(server, "_hardware_inventory", _setup_inventory()):
        flow = PublicSetup(server)
        flow.overview("M06 synthetic host board")
        flow.call("connect", {"board_id": board})
        try:
            args = {"board_id": board, "symbol": "value", "elf_artifact": str(artifact)}
            before = list(backend.calls)
            try:
                flow.call("read_memory_symbol", args)
            except Exception as exc:
                assert "not validated" in str(exc), str(exc)
            else:
                raise AssertionError("restart must not retain validation")
            assert backend.calls == before
            result = advertised_validation(flow, board)
            assert result["status"] == "validation_passed", result
            missing = flow.call("read_memory_symbol", {"board_id": board, "symbol": "value"})
            assert "memory/symbol-artifact-unavailable" in missing, missing
            axf = root / "variables.axf"
            axf.write_bytes(artifact.read_bytes())
            result = flow.call("read_memory_symbol", {**args, "elf_artifact": str(axf)})
            assert "value=0x12345678" in result, result
            print("Fresh process: validation required, implicit ELF refused, explicit AXF read passed")
        finally:
            flow.call("disconnect", {"board_id": board})
"""
            command = [sys.executable, "-B", "-c", code, str(root), board, str(artifact)]
            started = datetime.now(timezone.utc).isoformat()
            result = subprocess.run(
                command,
                env={
                    **os.environ,
                    "BYO_MCP_ARTIFACT_ROOT": str(root),
                    "BYO_MCP_MONITOR_ROOT": str(root / "restart-monitor"),
                },
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if evidence := os.environ.get("BYO_MANAGED_TEST_EVIDENCE"):
                (Path(evidence) / "m06-restart-child.json").write_text(
                    json.dumps(
                        {
                            "argv": command,
                            "started_at": started,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                            "exit_code": result.returncode,
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_corrupt_active_setup_refuses_direct_and_batch_before_memory_io(self):
        with self.project() as (_, server, backend, flow, board, artifact):
            pointer = server._capability_policies.pointer_path(board)
            original = pointer.read_bytes()
            before = list(backend.calls)
            try:
                pointer.write_bytes(b"not-json")
                args = self.arguments(board, artifact)
                with self.assertRaisesRegex(Exception, "corrupt committed safety policy"):
                    flow.call("read_memory_symbol", args)
                batch = flow.json(
                    "action_batch",
                    {
                        "board_id": board,
                        "actions": [{"tool_name": "read_memory_symbol", "arguments": args}],
                    },
                )
                self.assertEqual(batch["status"], "batch_failed", batch)
                self.assertEqual(backend.calls, before)
            finally:
                pointer.write_bytes(original)

    def test_monitor_stop_refuses_direct_and_batch_without_reading(self):
        with self.project() as (_, server, backend, flow, board, artifact):
            # Real stop state, using the monitor's existing timestamp storage.
            server._monitor._block.refresh(
                "host-test", "synthetic", datetime.now(timezone.utc) - timedelta(days=100)
            )
            args = self.arguments(board, artifact)
            before = list(backend.calls)
            with self.assertRaisesRegex(Exception, "logging|Logging|logg|unauditable|days"):
                flow.call("read_memory_symbol", args)
            batch = flow.json(
                "action_batch",
                {
                    "board_id": board,
                    "actions": [{"tool_name": "read_memory_symbol", "arguments": args}],
                },
            )
            self.assertEqual(batch["status"], "batch_failed", batch)
            self.assertEqual(backend.calls, before)
            server._monitor._block.refresh("host-test", "synthetic", datetime.now(timezone.utc))

    def test_raw_and_lite_cannot_gain_symbol_read_authority(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = MemoryProbe()
            server.configure_tiered_test_seams(backend=backend)
            flow = PublicSetup(server)
            with self.assertRaisesRegex(Exception, "no safety map"):
                flow.call("read_memory_symbol", self.arguments("raw", root / "absent.elf"))
            self.assertEqual(backend.calls, [])
        with self.project(tier="setup-lite") as (_, _, backend, flow, board, artifact):
            before = list(backend.calls)
            with self.assertRaisesRegex(Exception, "FULL|setup-full"):
                flow.call("read_memory_symbol", self.arguments(board, artifact))
            self.assertEqual(backend.calls, before)
        with self.project() as (_, server, backend, flow, board, _):
            before = list(backend.calls)
            result = public_call(
                server, "read_memory_raw", {"board_id": board, "address": 0x20000000}
            )
            self.assertEqual(result["status"], "refused", result)
            self.assertEqual(backend.calls, before)

    def test_public_symbol_read_waits_for_board_operation_lock(self):
        with self.project() as (_, server, backend, flow, board, artifact):
            started = threading.Event()
            finished = threading.Event()
            results = []

            def read():
                started.set()
                try:
                    results.append(flow.call("read_memory_symbol", self.arguments(board, artifact)))
                except Exception as exc:
                    results.append(exc)
                finally:
                    finished.set()

            with server.connection_manager.lock_for(board):
                before = list(backend.calls)
                thread = threading.Thread(target=read)
                thread.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(0.1))
                self.assertEqual(backend.calls, before)
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(results), 1)
            self.assertIn("value=0x12345678", str(results[0]))


if __name__ == "__main__":
    unittest.main()
