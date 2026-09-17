"""Deterministic AT10 fixture-contract tests; never imports a hardware backend."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


RUNNER_PATH = Path(__file__).with_name("manual") / "manual_tiered_hardware_check.py"


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tiered_hardware_runner_unit", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _board(root: Path) -> dict[str, object]:
    artifact = root / "fixture.bin"
    evidence = root / "restoration.json"
    return {
        "board_id": "physical_board",
        "model": "fixture-model",
        "probe_uid": "probe-a",
        "connection_id": "probeid:test:fixture-connection-a",
        "target": "fixture-target",
        "serial_binding": "serial-a",
        "expected_tier": "setup-lite",
        "safe_ram": {"start": 0x20000000, "length": 4, "write_value": 1},
        "flash_fixture": {
            "artifact": str(artifact),
            "restore_artifact": str(artifact),
            "address": 0,
            "erase_footprint": [{"start": 0, "end": 4096}],
            "post_flash_readback": {"result": "AA"},
            "post_restore_readback": {"result": "FF"},
        },
        "restoration": {"procedure": "restore fixture image", "evidence_path": str(evidence)},
        "full_evidence": {"status": "available", "tier": "setup-full", "reference": "fixture"},
        "disruption_approval": {
            "ram_write": {"approved": True, "scope": "ram_write", "reference": "fixture-approval"},
            "flash": {"approved": True, "scope": "flash", "reference": "fixture-approval"},
            "mass_erase": {
                "approved": False,
                "scope": "non-executing-disclosure-reservation",
                "reference": "fixture-approval",
            },
        },
    }


def _hw04_execution_fixture(
    root: Path, raw_steps: list[dict[str, object]], full_steps: list[dict[str, object]]
) -> dict[str, object]:
    """Build a minimum validated-at-runtime HW04 fixture with local placeholders only."""

    raw_mask = bytes((0xA5, 0x5A, 0x3C, 0xC3) * 4)

    def intel_hex(segments: list[tuple[int, bytes]]) -> bytes:
        lines: list[str] = []
        upper: int | None = None
        for start, contents in segments:
            for offset in range(0, len(contents), 16):
                address = start + offset
                new_upper = address >> 16
                if new_upper != upper:
                    body = bytes((2, 0, 0, 4)) + new_upper.to_bytes(2, "big")
                    lines.append(":" + (body + bytes(((-sum(body)) & 0xFF,))).hex().upper())
                    upper = new_upper
                payload = contents[offset : offset + 16]
                body = bytes((len(payload), (address >> 8) & 0xFF, address & 0xFF, 0)) + payload
                lines.append(":" + (body + bytes(((-sum(body)) & 0xFF,))).hex().upper())
        lines.append(":00000001FF")
        return ("\n".join(lines) + "\n").encode("ascii")

    def write(path: Path, payload: bytes) -> tuple[str, str]:
        path.write_bytes(payload)
        return str(path), hashlib.sha256(payload).hexdigest()

    def board(board_id: str) -> dict[str, object]:
        raw_address = 0x080FF800
        raw_restore = b"\xff" * 2048
        raw_test = bytes(
            value ^ raw_mask[index] if index < len(raw_mask) else value
            for index, value in enumerate(raw_restore)
        )
        raw_test_path, raw_test_sha256 = write(
            root / f"{board_id}-raw-test.hex", intel_hex([(raw_address, raw_test)])
        )
        raw_restore_path, raw_restore_sha256 = write(
            root / f"{board_id}-raw-restore.hex", intel_hex([(raw_address, raw_restore)])
        )
        first_address, handler_address = 0x08000000, 0x08000800
        first = bytearray(b"\xff" * 2048)
        first[:8] = bytes.fromhex("00 80 01 20 15 0B 00 08")
        handler = bytearray(b"\xff" * 2048)
        safe_offset = 0x08000D94 - handler_address
        safe_test_handler = bytearray(handler)
        safe_test_handler[safe_offset : safe_offset + 4] = b"\xa5" * 4
        safe_test_path, safe_test_sha256 = write(
            root / f"{board_id}-safe-test.hex",
            intel_hex([(first_address, bytes(first)), (handler_address, bytes(safe_test_handler))]),
        )
        safe_restore_path, safe_restore_sha256 = write(
            root / f"{board_id}-safe-restore.hex",
            intel_hex([(first_address, bytes(first)), (handler_address, bytes(handler))]),
        )
        _safe_test_elf, safe_test_elf_sha256 = write(
            root / f"{board_id}-safe-test.elf", b"\x7fELFfixture-safe-test"
        )
        _safe_restore_elf, safe_restore_elf_sha256 = write(
            root / f"{board_id}-safe-restore.elf", b"\x7fELFfixture-safe-restore"
        )
        return {
            "board_id": board_id,
            "probe_uid": f"{board_id}-probe",
            "connection_id": f"probeid:test:{board_id}-connection",
            "target": "stm32l476rgtx",
            "disruption_approval": {
                "ram_write": {"approved": True},
                "flash": {"approved": True},
            },
            "flash_fixture": {
                "raw": {
                    "artifact": raw_test_path,
                    "restore_artifact": raw_restore_path,
                    "address": raw_address,
                    "erase_footprint": [{"start": 0x080FF800, "end": 0x08100000}],
                    "post_flash_readback": {"result": "AA AA AA AA"},
                    "post_restore_readback": {"result": "FF FF FF FF"},
                    "integrity": {
                        "artifact_sha256": raw_test_sha256,
                        "restore_artifact_sha256": raw_restore_sha256,
                        "approved_ranges": [{"start": raw_address, "end": 0x08100000}],
                        "delta": {
                            "kind": "xor",
                            "start": raw_address,
                            "mask": raw_mask.hex().upper(),
                        },
                    },
                },
                "safe": {
                    "artifact": safe_test_path,
                    "restore_artifact": safe_restore_path,
                    "address": first_address,
                    "erase_footprint": [
                        {"start": first_address, "end": handler_address},
                        {"start": handler_address, "end": 0x08001000},
                    ],
                    "post_flash_readback": {"result": "A5 A5 A5 A5"},
                    "post_restore_readback": {"result": "FF FF FF FF"},
                    "integrity": {
                        "artifact_sha256": safe_test_sha256,
                        "restore_artifact_sha256": safe_restore_sha256,
                        "artifact_companion_sha256": safe_test_elf_sha256,
                        "restore_companion_sha256": safe_restore_elf_sha256,
                        "approved_ranges": [
                            {"start": first_address, "end": handler_address},
                            {"start": handler_address, "end": 0x08001000},
                        ],
                        "delta": {
                            "kind": "replace",
                            "start": 0x08000D94,
                            "before": "FFFFFFFF",
                            "after": "A5A5A5A5",
                        },
                    },
                },
            },
        }

    stm = board("stm-board")
    nordic = board("nrf-board")
    return {
        "boards": {"nrf52840": nordic, "stm32l476rtg": stm},
        "case_project_roots": {"HW04": {"raw": str(root), "full": str(root)}},
        "cases": {
            "HW04": {
                "boards": {
                    "nrf52840": {"status": "blocked", "reason": "not in this fixture"},
                    "stm32l476rtg": {
                        "status": "ready",
                        "raw_steps": raw_steps,
                        "full_steps": full_steps,
                    },
                }
            }
        },
    }


def _hw04_complete_raw_steps(
    *,
    test_readback: str = "AA AA AA AA",
    restored_readback: str = "FF FF FF FF",
) -> list[dict[str, object]]:
    """Build the logical raw lifecycle used by fresh-session executor tests."""

    board_id = "stm-board"
    return [
        {
            "id": "raw_connect",
            "tool": "connect",
            "arguments": {
                "board_id": board_id,
                "probe_uid": "stm-board-probe",
                "target": "stm32l476rgtx",
            },
            "expect": {"text_contains": ["stm-board-probe"]},
        },
        {
            "id": "initial_ram_read",
            "tool": "read_memory_raw",
            "arguments": {"board_id": board_id, "address": 0x20000000},
            "expect": {"payload": {"status": "ok"}},
        },
        {
            "id": "raw_ram_write",
            "tool": "write_memory_raw",
            "arguments": {"board_id": board_id, "address": 0x20000000, "value": 0xA5A5A5A5},
            "expect": {"payload": {"status": "ok"}},
        },
        {
            "id": "raw_ram_readback",
            "tool": "read_memory_raw",
            "arguments": {"board_id": board_id, "address": 0x20000000},
            "expect": {"payload": {"status": "ok"}},
        },
        {
            "id": "raw_ram_restore",
            "tool": "write_memory_raw",
            "arguments": {"board_id": board_id, "address": 0x20000000, "value": 0x11111111},
            "expect": {"payload": {"status": "ok"}},
        },
        {
            "id": "ram_restored_readback",
            "tool": "read_memory_raw",
            "arguments": {"board_id": board_id, "address": 0x20000000},
            "expect": {"payload": {"status": "ok"}},
        },
        {
            "id": "initial_flash_readback",
            "tool": "read_memory_raw",
            "arguments": {"board_id": board_id, "address": 0x080FF800},
            "expect": {"payload": {"status": "ok"}},
        },
        {
            "id": "raw_flash",
            "tool": "flash_raw",
            "arguments": {"board_id": board_id, "artifact": "raw-test"},
            "expect": {"payload": {"status": "ok"}},
        },
        {
            "id": "raw_flash_readback",
            "tool": "read_memory_raw",
            "arguments": {"board_id": board_id, "address": 0x080FF800},
            "expect": {"payload": {"status": "ok", "result": test_readback}},
        },
        {
            "id": "raw_restore",
            "tool": "flash_raw",
            "arguments": {"board_id": board_id, "artifact": "raw-restore"},
            "expect": {"payload": {"status": "ok"}},
        },
        {
            "id": "raw_restored_readback",
            "tool": "read_memory_raw",
            "arguments": {"board_id": board_id, "address": 0x080FF800},
            "expect": {"payload": {"status": "ok", "result": restored_readback}},
        },
        {
            "id": "final_disconnect",
            "tool": "disconnect",
            "arguments": {"board_id": board_id},
            "expect": {"text_contains": ["disconnected"]},
        },
    ]


def _hw04_complete_safe_steps(
    *,
    test_readback: str = "A5 A5 A5 A5",
    restored_readback: str = "FF FF FF FF",
) -> list[dict[str, object]]:
    """Build the complete safe lifecycle with plan-bound test and restore actions."""

    board_id = "stm-board"
    test = {"board_id": board_id, "actions": [{"kind": "safe-test"}]}
    containment = {"board_id": board_id, "actions": [{"kind": "containment-refusal"}]}
    readback = {"board_id": board_id, "actions": [{"kind": "safe-readback"}]}
    restore = {"board_id": board_id, "actions": [{"kind": "safe-restore"}]}
    restored = {"board_id": board_id, "actions": [{"kind": "safe-restored-readback"}]}

    def plan(identifier: str, tool: str, capture: str) -> list[dict[str, object]]:
        return [
            {
                "id": f"{identifier}_plan_guide",
                "tool": tool,
                "arguments": {"board_id": board_id},
                "expect": {"text_contains": ["guide"]},
            },
            {
                "id": f"{identifier}_plan_accept",
                "tool": tool,
                "arguments": {"board_id": board_id, "accept": True},
                "expect": {"payload": {"status": "plan_accepted"}},
                "capture": capture,
            },
        ]

    def action(
        identifier: str,
        fallback: dict[str, object],
        capture: str,
        *,
        result: str | None = None,
    ) -> dict[str, object]:
        expect: dict[str, object] = {"payload": {"status": "batch_completed"}}
        if result is not None:
            expect["child_payload"] = {
                "status": "ok",
                "operation": "read_memory_address",
                "result": result,
            }
        elif identifier != "safe_containment_refusal":
            expect["child_payload"] = {"status": "ok", "operation": "flash_application"}
        return {
            "id": identifier,
            "tool": "action_batch",
            "arguments": fallback,
            "fallback_from": capture,
            "expect": expect,
        }

    return [
        {
            "id": "full_assign",
            "tool": "setup_overview",
            "arguments": {
                "board_names": [board_id],
                "connection_assignments": {
                    board_id: "probeid:test:stm-board-connection",
                },
            },
            "expect": {"payload": {"status": "setup_routes_ready"}},
        },
        {
            "id": "full_connect",
            "tool": "connect",
            "arguments": {"board_id": board_id},
            "expect": {"text_contains": ["stm-board-probe"]},
        },
        {
            "id": "full_load_validation_tool",
            "tool": "load_setup_tool",
            "arguments": {"board_id": board_id, "tool_name": "board_validate"},
            "expect": {"payload": {"status": "setup_tool_loaded"}},
            "capture": "full_validation_loader",
        },
        {
            "id": "full_board_validate",
            "tool": "board_validate",
            "arguments": {
                "board_id": board_id,
                "probe_id": "$capture.full_validation_loader.next_call.arguments.probe_id",
            },
            "expect": {"payload": {"status": "validation_passed"}},
        },
        *plan("safe_flash", "flash_application-plan", "safe_flash_plan"),
        action("safe_flash", test, "safe_flash_plan"),
        *plan(
            "safe_containment_refusal",
            "read_memory_address-plan",
            "safe_containment_refusal_plan",
        ),
        action("safe_containment_refusal", containment, "safe_containment_refusal_plan"),
        *plan("safe_flash_readback", "read_memory_address-plan", "safe_flash_readback_plan"),
        action("safe_flash_readback", readback, "safe_flash_readback_plan", result=test_readback),
        *plan("safe_restore", "flash_application-plan", "safe_restore_plan"),
        action("safe_restore", restore, "safe_restore_plan"),
        *plan(
            "safe_restored_readback",
            "read_memory_address-plan",
            "safe_restored_readback_plan",
        ),
        action(
            "safe_restored_readback",
            restored,
            "safe_restored_readback_plan",
            result=restored_readback,
        ),
        {
            "id": "final_disconnect",
            "tool": "disconnect",
            "arguments": {"board_id": board_id},
            "expect": {"text_contains": ["disconnected"]},
        },
    ]


class TieredHardwareRunnerAcceptanceTests(unittest.TestCase):
    def test_batch_child_result_normalizes_public_plain_text_successes(self) -> None:
        runner = _runner()
        cases = (
            (
                "flash_application",
                "Flashed fixture.hex as flash_application within its mapped partition; "
                "final reset state is unconfirmed; reconnect and check target state before use.\n"
                "Safe exit: leave the board in the intended run state, then disconnect when "
                "hardware work is complete.",
                {"status": "ok", "operation": "flash_application"},
            ),
            (
                "read_memory_address",
                "A5 A5 A5 A5\n"
                "Safe exit: leave the board in the intended run state, then disconnect when "
                "hardware work is complete.",
                {
                    "status": "ok",
                    "operation": "read_memory_address",
                    "result": "A5 A5 A5 A5",
                },
            ),
            (
                "read_memory_address",
                "0xA5A5A5A5\n"
                "Safe exit: leave the board in the intended run state, then disconnect when "
                "hardware work is complete.",
                {
                    "status": "ok",
                    "operation": "read_memory_address",
                    "result": "0xA5A5A5A5",
                },
            ),
        )
        for tool_name, result, expected in cases:
            with self.subTest(tool_name=tool_name):
                payload, text = runner._batch_child_result(
                    {
                        "payload": {
                            "status": "batch_completed",
                            "completed": [{"tool_name": tool_name, "result": result}],
                        }
                    },
                    tool_name,
                )
                self.assertEqual(payload, expected)
                self.assertEqual(text, result)

    def test_batch_child_result_rejects_arbitrary_and_refusal_prefixed_text(self) -> None:
        runner = _runner()
        safe_exit = (
            "Safe exit: leave the board in the intended run state, then disconnect when hardware "
            "work is complete."
        )
        cases = (
            ("read_memory_address", f"operation completed\n{safe_exit}"),
            ("read_memory_address", f"Refused [memory/denied]: 00 00 00 00\n{safe_exit}"),
            (
                "flash_application",
                "Refused: expected text 'Flashed fixture.hex as flash_application within its "
                f"mapped partition; target left halted.'\n{safe_exit}",
            ),
            (
                "flash_application",
                "Flashed fixture.hex as flash_application within its mapped partition; "
                "target left halted.",
            ),
        )
        for tool_name, result in cases:
            with self.subTest(tool_name=tool_name, result=result):
                payload, text = runner._batch_child_result(
                    {
                        "payload": {
                            "status": "batch_completed",
                            "completed": [{"tool_name": tool_name, "result": result}],
                        }
                    },
                    tool_name,
                )
                self.assertIsNone(payload)
                self.assertEqual(text, result)

    def test_live_session_parameters_isolate_monitor_root_and_target_server_module(
        self,
    ) -> None:
        """AT10: each live runner session contains monitor state inside its case root."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            inherited_root = root.parent / "external-monitor-state"
            with patch.dict(os.environ, {"BYO_MCP_MONITOR_ROOT": str(inherited_root)}):
                parameters = runner._stdio_server_parameters(root)
            self.assertEqual(parameters.command, sys.executable)
            self.assertEqual(parameters.args, ["-m", "pyocd_debug_mcp.server"])
            self.assertEqual(parameters.env["BYO_MCP_ARTIFACT_ROOT"], str(root))
            monitor_root = Path(parameters.env["BYO_MCP_MONITOR_ROOT"])
            expected_monitor_root = root / ".agent-workspace" / "runtime" / "at10-monitor"
            self.assertTrue(monitor_root.is_absolute())
            self.assertEqual(monitor_root, expected_monitor_root)
            self.assertNotEqual(monitor_root, inherited_root)
            self.assertTrue(monitor_root.is_relative_to(root))

    def test_old_schema_and_destructive_execution_tool_are_rejected_pre_transport(self) -> None:
        runner = _runner()
        with self.assertRaisesRegex(runner.FixtureError, "schema_version must be 2"):
            runner.validate_fixture_document({"schema_version": 1})
        with self.assertRaisesRegex(runner.FixtureError, "forbidden"):
            runner._step(
                {
                    "id": "erase",
                    "tool": "target_unlock",
                    "arguments": {"board_id": "physical_board"},
                    "expect": {"text_contains": ["never"]},
                },
                "fixture.steps[0]",
                allow_pause=False,
            )

    def test_contract_rejects_decorative_target_cross_board_and_fake_non_map_steps(self) -> None:
        runner = _runner()
        board = {
            "board_id": "physical_board",
            "probe_uid": "probe-a",
            "target": "fixture-target",
            "serial_binding": "serial-a",
            "expected_tier": "setup-lite",
        }
        other = {
            "board_id": "other_board",
            "probe_uid": "probe-b",
            "target": "other-target",
            "serial_binding": "serial-b",
            "expected_tier": "setup-full",
        }
        with self.assertRaisesRegex(runner.FixtureError, "must not supply"):
            runner._validate_step_contract(
                "HW01",
                {
                    "id": "connect",
                    "tool": "connect",
                    "arguments": {
                        "board_id": "physical_board",
                        "probe_uid": "probe-a",
                        "target": "fixture-target",
                    },
                    "expect": {"text_contains": ["physical_board", "probe-a", "fixture-target"]},
                },
                board,
                [other],
                "connect",
            )
        with self.assertRaisesRegex(runner.FixtureError, "different fixture board"):
            runner._validate_step_contract(
                "HW02",
                {
                    "id": "safe_read",
                    "tool": "read_memory_address",
                    "arguments": {"board_id": "physical_board", "address": "other-target"},
                    "expect": {"payload": {"status": "ok", "operation": "read_memory_address"}},
                },
                board,
                [other],
                "safe_read",
            )
        with self.assertRaisesRegex(runner.FixtureError, "read_cpu_register"):
            runner._validate_step_contract(
                "HW02",
                {
                    "id": "non_map_operation",
                    "tool": "get_capabilities",
                    "arguments": {"board_id": "physical_board", "name": "pc"},
                    "expect": {"text_contains": ["0x"]},
                },
                board,
                [other],
                "non_map_operation",
            )
        with self.assertRaisesRegex(runner.FixtureError, "setup_overview"):
            runner._validate_step_contract(
                "HW01",
                {
                    "id": "assign",
                    "tool": "board_validate",
                    "arguments": {"board_id": "physical_board", "probe_id": "probe-a"},
                    "expect": {"text_contains": ["physical_board", "probe-a"]},
                },
                board,
                [other],
                "assign",
            )

    def test_hw02_post_setup_connect_binds_the_fixture_probe(self) -> None:
        """AT10/HW02: safe work reconnects exactly the configured board after setup."""

        runner = _runner()
        board = {
            "board_id": "physical_board",
            "probe_uid": "probe-a",
            "target": "fixture-target",
            "serial_binding": "serial-a",
            "expected_tier": "setup-lite",
        }
        connect = {
            "id": "connect",
            "tool": "connect",
            "arguments": {"board_id": "physical_board"},
            "expect": {"text_contains": ["physical_board", "probe-a"]},
        }
        self.assertIn("connect", runner.REQUIRED_STEP_IDS["HW02"])
        runner._validate_step_contract("HW02", connect, board, [], "connect")
        with self.assertRaisesRegex(runner.FixtureError, "only board_id"):
            runner._validate_step_contract(
                "HW02",
                {
                    **connect,
                    "arguments": {
                        **connect["arguments"],
                        "target": "fixture-target",
                    },
                },
                board,
                [],
                "connect",
            )

    def test_hw03_full_safe_work_reconnects_configured_profile_before_read(self) -> None:
        """AT10/HW03: a durable full profile is not a live target session."""

        runner = _runner()
        board = {
            "board_id": "physical_full_board",
            "probe_uid": "probe-full-a1b2",
            "target": "fixture-full-target",
            "serial_binding": "serial-full-a",
            "expected_tier": "setup-full",
        }
        connect = {
            "id": "connect",
            "tool": "connect",
            "arguments": {"board_id": "physical_full_board"},
            "expect": {"text_contains": ["physical_full_board", "probe-full-a1b2"]},
        }
        assignment_token = "connection:server-selected-a1b2"
        assign = {
            "id": "assign",
            "tool": "setup_overview",
            "arguments": {
                "board_names": ["physical_full_board"],
                "connection_assignments": {"physical_full_board": assignment_token},
            },
            "expect": {"text_contains": ["physical_full_board", "probe-full-a1b2"]},
        }
        loader = {
            "id": "load_setup_tool",
            "tool": "load_setup_tool",
            "arguments": {"board_id": "physical_full_board", "tool_name": "board_validate"},
            "expect": {
                "payload": {
                    "status": "setup_tool_loaded",
                    "board_id": "physical_full_board",
                    "tool_name": "board_validate",
                }
            },
            "capture": "validation_loader",
        }
        validate = {
            "id": "board_validate",
            "tool": "board_validate",
            "arguments": {
                "board_id": "physical_full_board",
                "probe_id": "$capture.validation_loader.next_call.arguments.probe_id",
            },
            "expect": {"payload": {"status": "validation_passed", "code": "validation/passed"}},
        }
        self.assertIn("connect", runner.REQUIRED_STEP_IDS["HW03"])
        self.assertIn("assign", runner.REQUIRED_STEP_IDS["HW03"])
        self.assertIn("load_setup_tool", runner.REQUIRED_STEP_IDS["HW03"])
        self.assertIn("board_validate", runner.REQUIRED_STEP_IDS["HW03"])
        runner._validate_step_contract("HW03", assign, board, [], "assign")
        runner._validate_step_contract("HW03", connect, board, [], "connect")
        runner._validate_step_contract("HW03", loader, board, [], "load_setup_tool")
        runner._validate_step_contract("HW03", validate, board, [], "board_validate")
        with self.assertRaisesRegex(runner.FixtureError, "only board_id"):
            runner._validate_step_contract(
                "HW03",
                {**connect, "arguments": {**connect["arguments"], "target": "fixture-full-target"}},
                board,
                [],
                "connect",
            )
        with self.assertRaisesRegex(runner.FixtureError, "only board_id and tool_name"):
            runner._validate_step_contract(
                "HW03",
                {**loader, "arguments": {**loader["arguments"], "probe_uid": "probe-full-a1b2"}},
                board,
                [],
                "load_setup_tool",
            )
        with self.assertRaisesRegex(runner.FixtureError, "only board_id"):
            runner._validate_step_contract(
                "HW03",
                {
                    **validate,
                    "arguments": {
                        **validate["arguments"],
                        "probe_uid": "probe-full-a1b2",
                    },
                },
                board,
                [],
                "board_validate",
            )

        steps = [
            {
                "id": "capabilities",
                "tool": "get_capabilities",
                "arguments": {"board_id": "physical_full_board"},
                "expect": {"payload": {"status": "capability_status", "tier": "setup-full"}},
            },
            assign,
            connect,
            loader,
            validate,
            {
                "id": "safe_operation",
                "tool": "action_batch",
                "arguments": {
                    "board_id": "physical_full_board",
                    "actions": [
                        {
                            "tool_name": "read_memory_address",
                            "arguments": {
                                "board_id": "physical_full_board",
                                "address": 0x1000,
                                "width": 32,
                                "length": 4,
                            },
                        }
                    ],
                },
                "fallback_from": "safe_operation_plan",
                "expect": {
                    "payload": {
                        "status": "batch_completed",
                        "board_id": "physical_full_board",
                        "completed": [{"tool_name": "read_memory_address"}],
                    },
                    "child_hex_bytes": 4,
                },
            },
        ]
        runner._validate_step_contract("HW03", steps[-1], board, [], "safe_operation")
        with self.assertRaisesRegex(runner.FixtureError, "exactly four"):
            runner._validate_step_contract(
                "HW03",
                {
                    **steps[-1],
                    "expect": {**steps[-1]["expect"], "child_hex_bytes": 3},
                },
                board,
                [],
                "safe_operation",
            )
        raw_refusal = {
            "id": "raw_refusal",
            "tool": "read_memory_raw",
            "arguments": {"board_id": "physical_full_board", "address": 0x1000},
            "expect": {
                "payload": {
                    "status": "refused",
                    "code": "tier/wrong-route",
                    "operation": "read_memory_raw",
                }
            },
        }
        with self.assertRaisesRegex(runner.FixtureError, "mcp_error"):
            runner._validate_step_contract("HW03", raw_refusal, board, [], "raw_refusal")

        class FullProfileSession:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                self.calls.append(tool)
                if tool == "get_capabilities":
                    return [
                        SimpleNamespace(
                            text=json.dumps({"status": "capability_status", "tier": "setup-full"})
                        )
                    ]
                if tool == "setup_overview":
                    if arguments != {
                        "board_names": ["physical_full_board"],
                        "connection_assignments": {"physical_full_board": assignment_token},
                    }:
                        raise AssertionError(f"unexpected assignment arguments: {arguments!r}")
                    return [
                        SimpleNamespace(text="physical_full_board probe=PROBE-FULL-A1B2 assigned")
                    ]
                if tool == "connect":
                    if arguments != {"board_id": "physical_full_board"}:
                        raise AssertionError(
                            f"unexpected configured-profile arguments: {arguments!r}"
                        )
                    return [SimpleNamespace(text="physical_full_board probe=PROBE-FULL-A1B2")]
                if tool == "load_setup_tool":
                    if arguments != {
                        "board_id": "physical_full_board",
                        "tool_name": "board_validate",
                    }:
                        raise AssertionError(f"unexpected loader arguments: {arguments!r}")
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "setup_tool_loaded",
                                    "board_id": "physical_full_board",
                                    "tool_name": "board_validate",
                                    "next_call": {
                                        "tool": "board_validate",
                                        "arguments": {
                                            "board_id": "physical_full_board",
                                            "probe_id": assignment_token,
                                        },
                                    },
                                }
                            )
                        )
                    ]
                if tool == "board_validate":
                    if arguments != {
                        "board_id": "physical_full_board",
                        "probe_id": assignment_token,
                    }:
                        raise AssertionError(f"unexpected validation arguments: {arguments!r}")
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {"status": "validation_passed", "code": "validation/passed"}
                            )
                        )
                    ]
                if tool == "action_batch":
                    if arguments != steps[-1]["arguments"]:
                        raise AssertionError(f"unexpected safe-action arguments: {arguments!r}")
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "batch_completed",
                                    "board_id": "physical_full_board",
                                    "completed": [
                                        {
                                            "tool_name": "read_memory_address",
                                            "result": "00 00 00 00\nSafe exit: leave the board in the intended run state.",
                                        }
                                    ],
                                }
                            )
                        )
                    ]
                raise AssertionError(f"unexpected public call: {tool}")

        async def check() -> None:
            session = FullProfileSession()
            rows, _captures = await runner.run_steps(
                session,
                Path.cwd(),
                board,
                steps,
                pause=lambda _: asyncio.sleep(0),
                captures={
                    "safe_operation_plan": {
                        "stable_client_fallback": {
                            "tool_name": "action_batch",
                            "arguments": steps[-1]["arguments"],
                        }
                    }
                },
            )
            self.assertEqual(
                session.calls,
                [
                    "get_capabilities",
                    "setup_overview",
                    "connect",
                    "load_setup_tool",
                    "board_validate",
                    "action_batch",
                ],
            )
            self.assertTrue(all(row["passed"] for row in rows))

        asyncio.run(check())

    def test_hw03_safe_operation_rejects_malformed_or_wrong_length_plaintext_child(self) -> None:
        """AT10/HW03: a safe read proves one 32-bit result without inventing its value."""

        runner = _runner()
        board = {"board_id": "physical_full_board"}
        step = {
            "id": "safe_operation",
            "tool": "action_batch",
            "arguments": {"board_id": "physical_full_board", "actions": []},
            "expect": {
                "payload": {
                    "status": "batch_completed",
                    "board_id": "physical_full_board",
                    "completed": [{"tool_name": "read_memory_address"}],
                },
                "child_hex_bytes": 4,
            },
        }

        class PlaintextChildSession:
            def __init__(self, result: str) -> None:
                self._result = result

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                if tool != "action_batch" or arguments != step["arguments"]:
                    raise AssertionError(f"unexpected public call: {tool} {arguments!r}")
                return [
                    SimpleNamespace(
                        text=json.dumps(
                            {
                                "status": "batch_completed",
                                "board_id": "physical_full_board",
                                "completed": [
                                    {"tool_name": "read_memory_address", "result": self._result}
                                ],
                            }
                        )
                    )
                ]

        async def check() -> None:
            for malformed in ("00 00 00\nSafe exit", "00 00 GG 00\nSafe exit"):
                with (
                    self.subTest(result=malformed),
                    self.assertRaisesRegex(runner.CheckFailed, "4 hex bytes"),
                ):
                    await runner.run_steps(
                        PlaintextChildSession(malformed),
                        Path.cwd(),
                        board,
                        [step],
                        pause=lambda _: asyncio.sleep(0),
                    )

        asyncio.run(check())

    def test_hw06_planned_route_read_requires_four_plaintext_hex_bytes(self) -> None:
        """AT10/HW06: a protected full-tier routed read proves one 32-bit result by shape."""

        runner = _runner()
        board = {"board_id": "full_route_board", "expected_tier": "setup-full"}
        parameters = {"address": 0x1000, "width": 32, "length": 4}
        fallback = {
            "board_id": "full_route_board",
            "actions": [
                {
                    "tool_name": "read_memory_address",
                    "arguments": {"board_id": "full_route_board", **parameters},
                }
            ],
        }
        planned_route = {
            "id": "route_read",
            "tool": "action_batch",
            "arguments": fallback,
            "fallback_from": "route_read_plan",
            "expect": {
                "payload": {
                    "status": "batch_completed",
                    "board_id": "full_route_board",
                    "completed": [{"tool_name": "read_memory_address"}],
                },
                "child_hex_bytes": 4,
            },
        }
        runner._validate_step_contract("HW06", planned_route, board, [], "route_read")
        with self.assertRaisesRegex(runner.FixtureError, "exactly four"):
            runner._validate_step_contract(
                "HW06",
                {**planned_route, "expect": {**planned_route["expect"], "child_hex_bytes": 3}},
                board,
                [],
                "route_read",
            )
        no_setup = {
            "id": "route_read",
            "tool": "read_memory_raw",
            "arguments": {"board_id": "raw_route_board", "address": 0x1000},
            "expect": {"payload": {"status": "ok", "tier": "no-setup", "raw": True}},
        }
        runner._validate_step_contract(
            "HW06",
            no_setup,
            {"board_id": "raw_route_board", "expected_tier": "no-setup"},
            [],
            "route_read",
        )

        class RouteReadSession:
            def __init__(self, result: str) -> None:
                self._result = result

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                if tool != "action_batch" or arguments != fallback:
                    raise AssertionError(f"unexpected public call: {tool} {arguments!r}")
                return [
                    SimpleNamespace(
                        text=json.dumps(
                            {
                                "status": "batch_completed",
                                "board_id": "full_route_board",
                                "completed": [
                                    {"tool_name": "read_memory_address", "result": self._result}
                                ],
                            }
                        )
                    )
                ]

        async def check() -> None:
            rows, _captures = await runner.run_steps(
                RouteReadSession(
                    "A0 b1 02 FF\nSafe exit: leave the board in the intended run state."
                ),
                Path.cwd(),
                board,
                [planned_route],
                pause=lambda _: asyncio.sleep(0),
                captures={
                    "route_read_plan": {
                        "stable_client_fallback": {
                            "tool_name": "action_batch",
                            "arguments": fallback,
                        }
                    }
                },
            )
            self.assertTrue(rows[0]["passed"])
            for malformed in ("A0 B1 02\nSafe exit", "A0 B1 ZZ FF\nSafe exit"):
                with (
                    self.subTest(result=malformed),
                    self.assertRaisesRegex(runner.CheckFailed, "4 hex bytes"),
                ):
                    await runner.run_steps(
                        RouteReadSession(malformed),
                        Path.cwd(),
                        board,
                        [planned_route],
                        pause=lambda _: asyncio.sleep(0),
                        captures={
                            "route_read_plan": {
                                "stable_client_fallback": {
                                    "tool_name": "action_batch",
                                    "arguments": fallback,
                                }
                            }
                        },
                    )

        asyncio.run(check())

    def test_hw06_tier_specific_connect_and_protected_validation_contract(self) -> None:
        """AT10/HW06: raw routing binds hardware directly; protected routing revalidates it."""

        runner = _runner()
        raw_board = {
            "board_id": "raw_route_board",
            "probe_uid": "raw-probe-a1b2",
            "target": "raw-target",
            "serial_binding": "raw-serial",
            "expected_tier": "no-setup",
        }
        protected_board = {
            "board_id": "protected_route_board",
            "probe_uid": "protected-probe-c3d4",
            "target": "protected-target",
            "serial_binding": "protected-serial",
            "expected_tier": "setup-full",
        }
        assignment_token = "connection:protected-server-selected"
        self.assertTrue(
            {"assign", "load_setup_tool", "board_validate"}.issubset(
                runner._required_step_ids("HW06", "setup-full")
            )
        )
        self.assertFalse(
            {"assign", "load_setup_tool", "board_validate"}.intersection(
                runner._required_step_ids("HW06", "no-setup")
            )
        )
        raw_connect = {
            "id": "connect",
            "tool": "connect",
            "arguments": {
                "board_id": "raw_route_board",
                "probe_uid": "raw-probe-a1b2",
                "target": "raw-target",
            },
            "expect": {"text_contains": ["raw-probe-a1b2"]},
        }
        protected_connect = {
            "id": "connect",
            "tool": "connect",
            "arguments": {"board_id": "protected_route_board"},
            "expect": {"text_contains": ["protected_route_board"]},
        }
        assign = {
            "id": "assign",
            "tool": "setup_overview",
            "arguments": {
                "board_names": ["protected_route_board"],
                "connection_assignments": {"protected_route_board": assignment_token},
            },
            "expect": {"text_contains": ["protected_route_board", "protected-probe-c3d4"]},
        }
        loader = {
            "id": "load_setup_tool",
            "tool": "load_setup_tool",
            "arguments": {"board_id": "protected_route_board", "tool_name": "board_validate"},
            "expect": {
                "payload": {
                    "status": "setup_tool_loaded",
                    "board_id": "protected_route_board",
                    "tool_name": "board_validate",
                }
            },
            "capture": "validation_loader",
        }
        validate = {
            "id": "board_validate",
            "tool": "board_validate",
            "arguments": {
                "board_id": "protected_route_board",
                "probe_id": "$capture.validation_loader.next_call.arguments.probe_id",
            },
            "expect": {"payload": {"status": "validation_passed", "code": "validation/passed"}},
        }
        runner._validate_step_contract("HW06", raw_connect, raw_board, [protected_board], "connect")
        runner._validate_step_contract(
            "HW06", protected_connect, protected_board, [raw_board], "connect"
        )
        runner._validate_step_contract("HW06", assign, protected_board, [raw_board], "assign")
        runner._validate_step_contract(
            "HW06", loader, protected_board, [raw_board], "load_setup_tool"
        )
        runner._validate_step_contract(
            "HW06", validate, protected_board, [raw_board], "board_validate"
        )
        with self.assertRaisesRegex(runner.FixtureError, "probe_uid and target"):
            runner._validate_step_contract(
                "HW06",
                {**raw_connect, "arguments": {"board_id": "raw_route_board"}},
                raw_board,
                [protected_board],
                "connect",
            )
        with self.assertRaisesRegex(runner.FixtureError, "only board_id"):
            runner._validate_step_contract(
                "HW06",
                {
                    **protected_connect,
                    "arguments": {**protected_connect["arguments"], "target": "protected-target"},
                },
                protected_board,
                [raw_board],
                "connect",
            )
        with self.assertRaisesRegex(runner.FixtureError, "different fixture board"):
            runner._validate_step_contract(
                "HW06",
                {
                    **raw_connect,
                    "arguments": {
                        "board_id": "raw_route_board",
                        "probe_uid": "protected-probe-c3d4",
                        "target": "protected-target",
                    },
                },
                raw_board,
                [protected_board],
                "connect",
            )

        parameters = {"address": 0x1000, "width": 32, "length": 4}
        route_fallback = {
            "board_id": "protected_route_board",
            "actions": [
                {
                    "tool_name": "read_memory_address",
                    "arguments": {"board_id": "protected_route_board", **parameters},
                }
            ],
        }
        protected_steps = [
            {"id": "capabilities"},
            assign,
            protected_connect,
            loader,
            validate,
            {"id": "route_read_plan_guide"},
            {"id": "route_read_plan_accept", "arguments": {"action_parameters": parameters}},
            {"id": "route_read", "arguments": route_fallback},
        ]
        runner._validate_plan_lifecycles("HW06", protected_steps, protected_board)
        with self.assertRaisesRegex(runner.FixtureError, "setup_overview assignment"):
            runner._validate_plan_lifecycles(
                "HW06",
                [step for step in protected_steps if step["id"] != "assign"],
                protected_board,
            )
        with self.assertRaisesRegex(runner.FixtureError, "capability, assign, connect"):
            runner._validate_plan_lifecycles(
                "HW06",
                [protected_steps[0], protected_steps[2], protected_steps[1], *protected_steps[3:]],
                protected_board,
            )

    def test_hw06_shared_session_runs_protected_validation_once_before_route_reads(self) -> None:
        """AT10/HW06: both routes stay live together while only the protected board validates."""

        runner = _runner()
        raw_board = {
            "board_id": "raw_route_board",
            "probe_uid": "raw-probe-a1b2",
            "target": "raw-target",
            "expected_tier": "no-setup",
        }
        protected_board = {
            "board_id": "protected_route_board",
            "probe_uid": "protected-probe-c3d4",
            "target": "protected-target",
            "expected_tier": "setup-full",
        }
        assignment_token = "connection:protected-server-selected"
        parameters = {"address": 0x1000, "width": 32, "length": 4}
        fallback = {
            "board_id": "protected_route_board",
            "actions": [
                {
                    "tool_name": "read_memory_address",
                    "arguments": {"board_id": "protected_route_board", **parameters},
                }
            ],
        }
        plan_accept = {"board_id": "protected_route_board", "action_parameters": parameters}
        raw_steps = [
            {
                "id": "capabilities",
                "tool": "get_capabilities",
                "arguments": {"board_id": "raw_route_board"},
                "expect": {"payload": {"status": "capability_status", "tier": "no-setup"}},
            },
            {
                "id": "connect",
                "tool": "connect",
                "arguments": {
                    "board_id": "raw_route_board",
                    "probe_uid": "raw-probe-a1b2",
                    "target": "raw-target",
                },
                "expect": {"text_contains": ["raw_route_board"]},
            },
            {
                "id": "route_read",
                "tool": "read_memory_raw",
                "arguments": {"board_id": "raw_route_board", "address": 0x1000},
                "expect": {"payload": {"status": "ok", "tier": "no-setup", "raw": True}},
            },
            {
                "id": "disconnect",
                "tool": "disconnect",
                "arguments": {"board_id": "raw_route_board"},
                "expect": {"text_contains": ["raw_route_board"]},
            },
        ]
        protected_steps = [
            {
                "id": "capabilities",
                "tool": "get_capabilities",
                "arguments": {"board_id": "protected_route_board"},
                "expect": {"payload": {"status": "capability_status", "tier": "setup-full"}},
            },
            {
                "id": "assign",
                "tool": "setup_overview",
                "arguments": {
                    "board_names": ["protected_route_board"],
                    "connection_assignments": {"protected_route_board": assignment_token},
                },
                "expect": {"text_contains": ["protected_route_board", "protected-probe-c3d4"]},
            },
            {
                "id": "connect",
                "tool": "connect",
                "arguments": {"board_id": "protected_route_board"},
                "expect": {"text_contains": ["protected_route_board"]},
            },
            {
                "id": "load_setup_tool",
                "tool": "load_setup_tool",
                "arguments": {
                    "board_id": "protected_route_board",
                    "tool_name": "board_validate",
                },
                "expect": {
                    "payload": {
                        "status": "setup_tool_loaded",
                        "board_id": "protected_route_board",
                        "tool_name": "board_validate",
                    }
                },
                "capture": "validation_loader",
            },
            {
                "id": "board_validate",
                "tool": "board_validate",
                "arguments": {
                    "board_id": "protected_route_board",
                    "probe_id": "$capture.validation_loader.next_call.arguments.probe_id",
                },
                "expect": {"payload": {"status": "validation_passed", "code": "validation/passed"}},
            },
            {
                "id": "route_read_plan_guide",
                "tool": "read_memory_address-plan",
                "arguments": runner._PLAN_GUIDE_ARGUMENTS,
                "expect": {"text_contains": ["Plan initialization for read_memory_address-plan"]},
            },
            {
                "id": "route_read_plan_accept",
                "tool": "read_memory_address-plan",
                "arguments": plan_accept,
                "expect": {
                    "payload": {
                        "status": "plan_accepted",
                        "underlying_action": "read_memory_address",
                    }
                },
                "capture": "route_read_plan",
            },
            {
                "id": "route_read",
                "tool": "action_batch",
                "arguments": fallback,
                "fallback_from": "route_read_plan",
                "expect": {
                    "payload": {
                        "status": "batch_completed",
                        "board_id": "protected_route_board",
                        "tier": "setup-full",
                        "completed": [{"tool_name": "read_memory_address"}],
                    },
                    "child_hex_bytes": 4,
                },
            },
            {
                "id": "disconnect",
                "tool": "disconnect",
                "arguments": {"board_id": "protected_route_board"},
                "expect": {"text_contains": ["protected_route_board"]},
            },
        ]

        class SharedSession:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.connected: set[str] = set()

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                self.calls.append(tool)
                if tool == "get_capabilities":
                    board_id = arguments["board_id"]
                    tier = "no-setup" if board_id == "raw_route_board" else "setup-full"
                    return [
                        SimpleNamespace(
                            text=json.dumps({"status": "capability_status", "tier": tier})
                        )
                    ]
                if tool == "setup_overview":
                    if arguments != protected_steps[1]["arguments"]:
                        raise AssertionError(f"unexpected assignment: {arguments!r}")
                    return [
                        SimpleNamespace(
                            text="protected_route_board probe=protected-probe-c3d4 assigned"
                        )
                    ]
                if tool == "connect":
                    expected = (
                        raw_steps[1]["arguments"]
                        if len(self.connected) == 0
                        else protected_steps[2]["arguments"]
                    )
                    if arguments != expected:
                        raise AssertionError(f"unexpected connect: {arguments!r}")
                    board_id = arguments["board_id"]
                    self.connected.add(board_id)
                    return [SimpleNamespace(text=f"connected {board_id}")]
                if tool == "load_setup_tool":
                    if arguments != protected_steps[3]["arguments"]:
                        raise AssertionError(f"unexpected loader: {arguments!r}")
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "setup_tool_loaded",
                                    "board_id": "protected_route_board",
                                    "tool_name": "board_validate",
                                    "next_call": {
                                        "tool": "board_validate",
                                        "arguments": {
                                            "board_id": "protected_route_board",
                                            "probe_id": assignment_token,
                                        },
                                    },
                                }
                            )
                        )
                    ]
                if tool == "board_validate":
                    if arguments != {
                        "board_id": "protected_route_board",
                        "probe_id": assignment_token,
                    }:
                        raise AssertionError(f"unexpected validation: {arguments!r}")
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {"status": "validation_passed", "code": "validation/passed"}
                            )
                        )
                    ]
                if tool == "read_memory_address-plan":
                    if arguments == runner._PLAN_GUIDE_ARGUMENTS:
                        return [
                            SimpleNamespace(text="Plan initialization for read_memory_address-plan")
                        ]
                    if arguments == plan_accept:
                        return [
                            SimpleNamespace(
                                text=json.dumps(
                                    {
                                        "status": "plan_accepted",
                                        "underlying_action": "read_memory_address",
                                        "stable_client_fallback": {
                                            "tool_name": "action_batch",
                                            "arguments": fallback,
                                        },
                                    }
                                )
                            )
                        ]
                if tool == "read_memory_raw":
                    if self.connected != {"raw_route_board", "protected_route_board"}:
                        raise AssertionError("route reads were not concurrent")
                    return [
                        SimpleNamespace(
                            text=json.dumps({"status": "ok", "tier": "no-setup", "raw": True})
                        )
                    ]
                if tool == "action_batch":
                    if self.connected != {"raw_route_board", "protected_route_board"}:
                        raise AssertionError("route reads were not concurrent")
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "batch_completed",
                                    "board_id": "protected_route_board",
                                    "tier": "setup-full",
                                    "completed": [
                                        {
                                            "tool_name": "read_memory_address",
                                            "result": "00 00 00 00\nSafe exit: leave the board in the intended run state.",
                                        }
                                    ],
                                }
                            )
                        )
                    ]
                if tool == "disconnect":
                    board_id = arguments["board_id"]
                    self.connected.discard(board_id)
                    return [SimpleNamespace(text=f"disconnected {board_id}")]
                raise AssertionError(f"unexpected public tool: {tool}")

        class SessionContext:
            def __init__(self, session: SharedSession) -> None:
                self._session = session

            async def __aenter__(self) -> SharedSession:
                return self._session

            async def __aexit__(self, *unused: object) -> None:
                del unused

        async def check() -> None:
            session = SharedSession()
            transcript = await runner.execute_with_session_factory(
                {
                    "boards": {"nrf52840": raw_board, "stm32l476rtg": protected_board},
                    "case_project_roots": {"HW06": str(Path.cwd())},
                    "cases": {
                        "HW06": {
                            "boards": {
                                "nrf52840": {"status": "ready", "steps": raw_steps},
                                "stm32l476rtg": {"status": "ready", "steps": protected_steps},
                            }
                        }
                    },
                },
                ("HW06",),
                lambda _root: SessionContext(session),
                pause=lambda _: asyncio.sleep(0),
                allow_write_flash=False,
            )
            self.assertEqual(transcript["case_results"]["HW06"]["status"], "pass")
            self.assertEqual(
                session.calls,
                [
                    "get_capabilities",
                    "get_capabilities",
                    "setup_overview",
                    "connect",
                    "connect",
                    "load_setup_tool",
                    "board_validate",
                    "read_memory_address-plan",
                    "read_memory_address-plan",
                    "read_memory_raw",
                    "action_batch",
                    "disconnect",
                    "disconnect",
                    "disconnect",
                    "disconnect",
                ],
            )
            raw_phase = transcript["case_results"]["HW06"]["boards"]["nrf52840"]["phases"][0]
            protected_phase = transcript["case_results"]["HW06"]["boards"]["stm32l476rtg"][
                "phases"
            ][0]
            self.assertEqual(
                [row["id"] for row in raw_phase["steps"]],
                ["capabilities", "connect", "route_read", "disconnect"],
            )
            self.assertEqual(
                [row["id"] for row in protected_phase["steps"]],
                [
                    "capabilities",
                    "assign",
                    "connect",
                    "load_setup_tool",
                    "board_validate",
                    "route_read_plan_guide",
                    "route_read_plan_accept",
                    "route_read",
                    "disconnect",
                ],
            )

            raw_steps[1]["expect"] = {"text_contains": ["deliberately-missing"]}
            failed_session = SharedSession()
            failed_transcript = await runner.execute_with_session_factory(
                {
                    "boards": {"nrf52840": raw_board, "stm32l476rtg": protected_board},
                    "case_project_roots": {"HW06": str(Path.cwd())},
                    "cases": {
                        "HW06": {
                            "boards": {
                                "nrf52840": {"status": "ready", "steps": raw_steps},
                                "stm32l476rtg": {"status": "ready", "steps": protected_steps},
                            }
                        }
                    },
                },
                ("HW06",),
                lambda _root: SessionContext(failed_session),
                pause=lambda _: asyncio.sleep(0),
                allow_write_flash=False,
            )
            self.assertEqual(failed_transcript["case_results"]["HW06"]["status"], "failed")
            self.assertEqual(
                failed_transcript["case_results"]["HW06"]["boards"]["nrf52840"]["status"],
                "failed",
            )

        asyncio.run(check())

    def test_hw03_schema_rejects_missing_or_misordered_assignment(self) -> None:
        """AT10/HW03: the run-scoped setup assignment must precede the live validation route."""

        runner = _runner()
        board = {"board_id": "physical_full_board", "expected_tier": "setup-full"}
        parameters = {"address": 0x1000, "width": 32, "length": 4}
        safe_fallback = {
            "board_id": "physical_full_board",
            "actions": [
                {
                    "tool_name": "read_memory_address",
                    "arguments": {"board_id": "physical_full_board", **parameters},
                }
            ],
        }
        steps = [
            {"id": "capabilities"},
            {"id": "assign"},
            {"id": "connect"},
            {"id": "load_setup_tool"},
            {"id": "board_validate"},
            {"id": "safe_operation_plan_guide"},
            {"id": "safe_operation_plan_accept", "arguments": {"action_parameters": parameters}},
            {"id": "safe_operation", "arguments": safe_fallback},
        ]

        runner._validate_plan_lifecycles("HW03", steps, board)
        missing_assignment = [step for step in steps if step["id"] != "assign"]
        with self.assertRaisesRegex(runner.FixtureError, "setup_overview assignment"):
            runner._validate_plan_lifecycles("HW03", missing_assignment, board)
        misordered_assignment = [steps[0], steps[2], steps[1], *steps[3:]]
        with self.assertRaisesRegex(
            runner.FixtureError, "confirm full capability, assign, connect"
        ):
            runner._validate_plan_lifecycles("HW03", misordered_assignment, board)

    def test_text_response_expectations_casefold_probe_identity_but_payloads_stay_exact(
        self,
    ) -> None:
        """AT10: text display casing cannot mask a different structured value."""

        runner = _runner()
        board = {"board_id": "physical_board"}
        step = {
            "id": "connect",
            "tool": "connect",
            "arguments": {"board_id": "physical_board"},
            "expect": {"text_contains": ["physical_board", "probe-uid-a1b2"]},
        }

        class UppercaseProbeSession:
            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                if tool != "connect" or arguments != {"board_id": "physical_board"}:
                    raise AssertionError(f"unexpected public call: {tool} {arguments!r}")
                return [SimpleNamespace(text="connected physical_board probe=PROBE-UID-A1B2")]

        async def check() -> None:
            rows, _captures = await runner.run_steps(
                UppercaseProbeSession(),
                Path.cwd(),
                board,
                [step],
                pause=lambda _: asyncio.sleep(0),
            )
            self.assertTrue(rows[0]["passed"])
            with self.assertRaises(runner.CheckFailed):
                runner._contains(
                    {"probe_uid": "PROBE-UID-A1B2"}, {"probe_uid": "probe-uid-a1b2"}, "payload"
                )

        asyncio.run(check())

    def test_hw04_connect_success_oracle_requires_casefolded_fixture_uid(self) -> None:
        """HW04 connects bind textual success to the fixture probe, not a display label."""

        runner = _runner()
        board = {
            "board_id": "stm-board",
            "probe_uid": "probe-uid-a1b2",
            "connection_id": "probeid:stlink:stm-board-connection",
            "target": "stm32l476rgtx",
            "expected_tier": "setup-full",
            "safe_ram": {"start": 0x20000000, "write_value": 0xA5A5A5A5},
            "flash_fixture": {"raw": {}, "safe": {}},
        }
        raw_connect = {
            "id": "raw_connect",
            "tool": "connect",
            "arguments": {
                "board_id": "stm-board",
                "probe_uid": "probe-uid-a1b2",
                "target": "stm32l476rgtx",
            },
            "expect": {"text_contains": ["probe-uid-a1b2"]},
        }
        full_connect = {
            "id": "full_connect",
            "tool": "connect",
            "arguments": {"board_id": "stm-board"},
            "expect": {"text_contains": ["probe-uid-a1b2"]},
        }
        runner._validate_step_contract("HW04", raw_connect, board, [], "raw_connect")
        runner._validate_step_contract("HW04", full_connect, board, [], "full_connect")

        class Session:
            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                if tool == "connect" and arguments == raw_connect["arguments"]:
                    return [
                        SimpleNamespace(
                            text="Connected to board 'Friendly product label' via probe PROBE-UID-A1B2."
                        )
                    ]
                if tool == "connect" and arguments == full_connect["arguments"]:
                    return [
                        SimpleNamespace(
                            text="Connected to board 'Friendly product label' via probe PROBE-UID-A1B2."
                        )
                    ]
                raise AssertionError(f"unexpected public call: {tool} {arguments!r}")

        async def check() -> None:
            raw_rows, _ = await runner.run_steps(
                Session(), Path("."), board, [raw_connect], pause=lambda _: asyncio.sleep(0)
            )
            full_rows, _ = await runner.run_steps(
                Session(), Path("."), board, [full_connect], pause=lambda _: asyncio.sleep(0)
            )
            self.assertTrue(raw_rows[0]["passed"])
            self.assertTrue(full_rows[0]["passed"])

        asyncio.run(check())

    def test_hw04_wrong_connect_uid_blocks_raw_and_full_mutations(self) -> None:
        """HW04 cannot dispatch a mutation when either connect names another probe."""

        runner = _runner()
        board = {
            "board_id": "stm-board",
            "probe_uid": "probe-uid-a1b2",
            "connection_id": "probeid:stlink:stm-board-connection",
            "target": "stm32l476rgtx",
        }
        raw_calls: list[str] = []
        full_calls: list[str] = []
        raw_steps = [
            {
                "id": "raw_connect",
                "tool": "connect",
                "arguments": {
                    "board_id": "stm-board",
                    "probe_uid": "probe-uid-a1b2",
                    "target": "stm32l476rgtx",
                },
                "expect": {"text_contains": ["probe-uid-a1b2"]},
            },
            {
                "id": "raw_flash",
                "tool": "flash_raw",
                "arguments": {"board_id": "stm-board", "artifact": "must-not-run"},
                "expect": {"payload": {"status": "ok"}},
            },
        ]
        full_steps = [
            {
                "id": "full_assign",
                "tool": "setup_overview",
                "arguments": {
                    "board_names": ["stm-board"],
                    "connection_assignments": {"stm-board": "probeid:stlink:stm-board-connection"},
                },
                "expect": {"payload": {"status": "setup_routes_ready"}},
            },
            {
                "id": "full_connect",
                "tool": "connect",
                "arguments": {"board_id": "stm-board"},
                "expect": {"text_contains": ["probe-uid-a1b2"]},
            },
            {
                "id": "safe_flash",
                "tool": "action_batch",
                "arguments": {"board_id": "stm-board", "actions": []},
                "expect": {"payload": {"status": "batch_completed"}},
            },
        ]

        class RawSession:
            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                del arguments
                raw_calls.append(tool)
                if tool == "connect":
                    return [
                        SimpleNamespace(
                            text="Connected to board 'Friendly product label' via probe OTHER-UID."
                        )
                    ]
                raise AssertionError(f"unexpected raw mutation: {tool}")

        class FullSession:
            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                del arguments
                full_calls.append(tool)
                if tool == "setup_overview":
                    return [SimpleNamespace(text=json.dumps({"status": "setup_routes_ready"}))]
                if tool == "connect":
                    return [
                        SimpleNamespace(
                            text="Connected to board 'Friendly product label' via probe OTHER-UID."
                        )
                    ]
                raise AssertionError(f"unexpected safe mutation: {tool}")

        with self.assertRaisesRegex(
            runner.CheckFailed, "raw_connect.text missing 'probe-uid-a1b2'"
        ):
            asyncio.run(
                runner.run_steps(
                    RawSession(), Path("."), board, raw_steps, pause=lambda _: asyncio.sleep(0)
                )
            )
        with self.assertRaisesRegex(
            runner.CheckFailed, "full_connect.text missing 'probe-uid-a1b2'"
        ):
            asyncio.run(
                runner.run_steps(
                    FullSession(), Path("."), board, full_steps, pause=lambda _: asyncio.sleep(0)
                )
            )
        self.assertEqual(raw_calls, ["connect"])
        self.assertEqual(full_calls, ["setup_overview", "connect"])

    def test_board_fixture_rejects_malformed_footprint_and_non_approval_text(self) -> None:
        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = _board(root)
            malformed["flash_fixture"]["erase_footprint"] = [{"start": 8, "end": 8}]  # type: ignore[index]
            with self.assertRaisesRegex(runner.FixtureError, "invalid"):
                runner._common_board("nrf52840", malformed)
            non_approval = _board(root)
            non_approval["disruption_approval"]["flash"] = "NO"  # type: ignore[index]
            with self.assertRaisesRegex(runner.FixtureError, "must be an object"):
                runner._common_board("nrf52840", non_approval)

    def test_full_evidence_and_mass_erase_record_cannot_claim_execution_authority(self) -> None:
        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            decorative_full = _board(root)
            decorative_full["full_evidence"]["status"] = "looks-good"  # type: ignore[index]
            with self.assertRaisesRegex(runner.FixtureError, "available/setup-full"):
                runner._common_board("nrf52840", decorative_full)
            execution_claim = _board(root)
            execution_claim["disruption_approval"]["mass_erase"]["approved"] = True  # type: ignore[index]
            with self.assertRaisesRegex(runner.FixtureError, "never authorizes"):
                runner._common_board("nrf52840", execution_claim)
            ambiguous_scope = _board(root)
            ambiguous_scope["disruption_approval"]["mass_erase"]["scope"] = "mass_erase"  # type: ignore[index]
            with self.assertRaisesRegex(runner.FixtureError, "non-executing"):
                runner._common_board("nrf52840", ambiguous_scope)

    def test_full_evidence_and_mutation_approvals_can_explicitly_block_later_cases(self) -> None:
        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unavailable = _board(root)
            unavailable["full_evidence"] = {"status": "blocked", "reason": "review pending"}
            unavailable["disruption_approval"]["ram_write"] = {  # type: ignore[index]
                "approved": False,
                "scope": "declined-or-pending",
                "reference": "operator has not approved writes",
            }
            unavailable["disruption_approval"]["flash"] = {  # type: ignore[index]
                "approved": False,
                "scope": "declined-or-pending",
                "reference": "operator has not approved flash",
            }
            validated = runner._common_board("nrf52840", unavailable)
            self.assertEqual(validated["full_evidence"]["status"], "blocked")
            self.assertFalse(validated["disruption_approval"]["flash"]["approved"])

    def test_runner_rejects_shared_case_project_or_runtime_before_transport(self) -> None:
        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "case"
            (root / ".agent-workspace" / "runtime").mkdir(parents=True)
            self.assertEqual(runner._project_root(str(root), "case"), root.resolve())
            with self.assertRaisesRegex(runner.FixtureError, "distinct physical projects"):
                runner._require_distinct_projects([root, root])

    def test_hw04_cannot_replace_actual_raw_flash_with_a_full_tier_refusal(self) -> None:
        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            board = _board(Path(temporary))
            with self.assertRaisesRegex(runner.FixtureError, "must be 'ok'"):
                runner._validate_step_contract(
                    "HW04",
                    {
                        "id": "raw_flash",
                        "tool": "flash_raw",
                        "arguments": {
                            "board_id": "physical_board",
                            "artifact": board["flash_fixture"]["artifact"],  # type: ignore[index]
                        },
                        "expect": {
                            "payload": {
                                "status": "refused",
                                "operation": "flash_raw",
                                "tier": "no-setup",
                            }
                        },
                    },
                    board,
                    [],
                    "raw_flash",
                )

    def test_hw04_rejects_ram_readback_that_does_not_prove_write_or_restoration(self) -> None:
        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            board = _board(Path(temporary))
            base = {
                "tool": "read_memory_raw",
                "arguments": {"board_id": "physical_board", "address": 0x20000000},
                "expect": {
                    "payload": {
                        "status": "ok",
                        "operation": "read_memory_raw",
                        "tier": "no-setup",
                    }
                },
            }
            with self.assertRaisesRegex(runner.FixtureError, "result"):
                runner._validate_step_contract(
                    "HW04",
                    {**base, "id": "raw_ram_readback"},
                    board,
                    [],
                    "raw_ram_readback",
                )
            with self.assertRaisesRegex(runner.FixtureError, "result"):
                runner._validate_step_contract(
                    "HW04",
                    {**base, "id": "ram_restored_readback"},
                    board,
                    [],
                    "ram_restored_readback",
                )

    def test_hw04_flash_readback_uses_the_fixture_payload_fragment_without_double_nesting(
        self,
    ) -> None:
        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            board = _board(Path(temporary))
            runner._validate_step_contract(
                "HW04",
                {
                    "id": "raw_flash_readback",
                    "tool": "read_memory_raw",
                    "arguments": {"board_id": "physical_board", "address": 0},
                    "expect": {
                        "payload": {
                            "status": "ok",
                            "operation": "read_memory_raw",
                            "tier": "no-setup",
                            "result": "AA",
                        }
                    },
                    "capture": "raw_flash_observed",
                },
                board,
                [],
                "raw_flash_readback",
            )

    def test_hw04_distinct_raw_and_safe_flash_lanes_cannot_cross_bind(self) -> None:
        """Schema-v2 may distinguish lanes, while the legacy form still aliases both."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = runner._common_board("stm32l476rtg", _board(root))
            self.assertEqual(
                legacy["flash_fixture"]["raw"]["artifact"],  # type: ignore[index]
                legacy["flash_fixture"]["safe"]["artifact"],  # type: ignore[index]
            )

            board = _board(root)
            raw = dict(board["flash_fixture"])  # type: ignore[arg-type,index]
            raw["integrity"] = {
                "artifact_sha256": "0" * 64,
                "restore_artifact_sha256": "1" * 64,
                "approved_ranges": [{"start": 0, "end": 4096}],
                "delta": {"kind": "xor", "start": 0, "mask": "A55A3CC3" * 4},
            }
            safe_artifact = root / "safe.hex"
            safe_restore = root / "safe-restore.hex"
            board["flash_fixture"] = {
                "raw": raw,
                "safe": {
                    "artifact": str(safe_artifact),
                    "restore_artifact": str(safe_restore),
                    "address": 0x08000000,
                    "erase_footprint": [
                        {"start": 0x08000000, "end": 0x08000800},
                        {"start": 0x08000800, "end": 0x08001000},
                    ],
                    "post_flash_readback": {"result": "A5 A5 A5 A5"},
                    "post_restore_readback": {"result": "FF FF FF FF"},
                    "integrity": {
                        "artifact_sha256": "2" * 64,
                        "restore_artifact_sha256": "3" * 64,
                        "artifact_companion_sha256": "4" * 64,
                        "restore_companion_sha256": "5" * 64,
                        "approved_ranges": [
                            {"start": 0x08000000, "end": 0x08000800},
                            {"start": 0x08000800, "end": 0x08001000},
                        ],
                        "delta": {
                            "kind": "replace",
                            "start": 0x08000D94,
                            "before": "FFFFFFFF",
                            "after": "A5A5A5A5",
                        },
                    },
                },
            }
            validated = runner._common_board("stm32l476rtg", board)
            raw_fixture = validated["flash_fixture"]["raw"]
            safe_fixture = validated["flash_fixture"]["safe"]
            self.assertNotEqual(raw_fixture["artifact"], safe_fixture["artifact"])
            self.assertNotEqual(raw_fixture["address"], safe_fixture["address"])

            with self.assertRaisesRegex(runner.FixtureError, "raw flash artifact"):
                runner._validate_step_contract(
                    "HW04",
                    {
                        "id": "raw_flash",
                        "tool": "flash_raw",
                        "arguments": {
                            "board_id": "physical_board",
                            "artifact": safe_fixture["artifact"],
                        },
                        "expect": {
                            "payload": {
                                "status": "ok",
                                "operation": "flash_raw",
                                "tier": "no-setup",
                            }
                        },
                    },
                    validated,
                    [],
                    "raw_flash",
                )
            with self.assertRaisesRegex(runner.FixtureError, "safe flash artifact"):
                runner._validate_plan_protocol_step(
                    "safe_flash",
                    "accept",
                    runner._PLAN_PROTOCOLS["HW04"]["safe_flash"],
                    {
                        "id": "safe_flash_plan_accept",
                        "tool": "flash_application-plan",
                        "arguments": {
                            "board_id": "physical_board",
                            "hypothesis": "Captured pages form a bounded valid image.",
                            "strategy": "Program only the safe sparse fixture.",
                            "hypothesis_made": True,
                            "strategy_evaluated": True,
                            "expected_fail_return": "The server refuses an out-of-map image.",
                            "expected_success_return": "The server accepts the safe image.",
                            "max_calls": 1,
                            "max_calls_buffer": 0,
                            "action_parameters": {"artifact": raw_fixture["artifact"]},
                        },
                        "expect": {
                            "payload": {
                                "status": "plan_accepted",
                                "underlying_action": "flash_application",
                            }
                        },
                        "capture": "safe_flash_plan",
                    },
                    validated,
                    "safe_flash_plan_accept",
                )
            with self.assertRaisesRegex(runner.FixtureError, "result"):
                runner._validate_plan_protocol_step(
                    "safe_flash_readback",
                    "action",
                    runner._PLAN_PROTOCOLS["HW04"]["safe_flash_readback"],
                    {
                        "id": "safe_flash_readback",
                        "tool": "action_batch",
                        "arguments": {"board_id": "physical_board", "actions": []},
                        "fallback_from": "safe_flash_readback_plan",
                        "expect": {
                            "payload": {
                                "status": "batch_completed",
                                "board_id": "physical_board",
                                "completed": [{"tool_name": "read_memory_address"}],
                            },
                            "child_payload": {
                                "status": "ok",
                                "operation": "read_memory_address",
                                "result": raw_fixture["post_flash_readback"]["result"],
                            },
                        },
                    },
                    validated,
                    "safe_flash_readback",
                )

    def test_hw04_safe_flash_cannot_pass_on_batch_acceptance_without_child_success_or_readback(
        self,
    ) -> None:
        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            board = _board(Path(temporary))
            base = {
                "tool": "action_batch",
                "arguments": {"board_id": "physical_board", "actions": []},
                "fallback_from": "safe_flash_plan",
                "expect": {
                    "payload": {
                        "status": "batch_completed",
                        "board_id": "physical_board",
                        "completed": [{"tool_name": "flash_application"}],
                    },
                    "child_payload": {"status": "ok", "operation": "flash_application"},
                },
            }
            with self.assertRaisesRegex(runner.FixtureError, "public flash success"):
                runner._validate_plan_protocol_step(
                    "safe_flash",
                    "action",
                    runner._PLAN_PROTOCOLS["HW04"]["safe_flash"],
                    {**base, "id": "safe_flash"},
                    board,
                    "safe_flash",
                )
            with self.assertRaisesRegex(runner.FixtureError, "result"):
                runner._validate_plan_protocol_step(
                    "safe_flash_readback",
                    "action",
                    runner._PLAN_PROTOCOLS["HW04"]["safe_flash_readback"],
                    {
                        **base,
                        "id": "safe_flash_readback",
                        "fallback_from": "safe_flash_readback_plan",
                        "expect": {
                            "payload": {
                                "status": "batch_completed",
                                "board_id": "physical_board",
                                "completed": [{"tool_name": "read_memory_address"}],
                            },
                            "child_payload": {
                                "status": "ok",
                                "operation": "read_memory_address",
                            },
                        },
                    },
                    board,
                    "safe_flash_readback",
                )

    def test_hw04_safe_flash_accepts_the_public_plain_text_success_contract(self) -> None:
        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            board = _board(Path(temporary))
            runner._validate_plan_protocol_step(
                "safe_flash",
                "action",
                runner._PLAN_PROTOCOLS["HW04"]["safe_flash"],
                {
                    "id": "safe_flash",
                    "tool": "action_batch",
                    "arguments": {"board_id": "physical_board", "actions": []},
                    "fallback_from": "safe_flash_plan",
                    "expect": {
                        "payload": {
                            "status": "batch_completed",
                            "board_id": "physical_board",
                            "completed": [{"tool_name": "flash_application"}],
                        },
                        "child_payload": {
                            "status": "ok",
                            "operation": "flash_application",
                        },
                        "child_text_contains": ["as flash_application within its mapped partition"],
                    },
                },
                board,
                "safe_flash",
            )

    def test_hw04_full_safe_containment_accepts_fail_closed_batch_result(self) -> None:
        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            board = _board(Path(temporary))
            runner._validate_plan_protocol_step(
                "safe_containment_refusal",
                "action",
                runner._PLAN_PROTOCOLS["HW04"]["safe_containment_refusal"],
                {
                    "id": "safe_containment_refusal",
                    "tool": "action_batch",
                    "arguments": {"board_id": "physical_board", "actions": []},
                    "fallback_from": "safe_containment_refusal_plan",
                    "expect": {
                        "payload": {
                            "status": "batch_failed",
                            "board_id": "physical_board",
                            "completed": [],
                            "failure": {
                                "error_type": "ToolError",
                                "tool_name": "write_memory",
                                "message": (
                                    "memory_write is unavailable in a physical_flash region. "
                                    "Required remedy: board_safety_refresh."
                                ),
                            },
                        }
                    },
                },
                board,
                "safe_containment_refusal",
            )

    def test_hw04_nordic_containment_accepts_exact_application_flash_refusal(self) -> None:
        """The generic Nordic allocation is application flash, but remains non-writable here."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            board = _board(Path(temporary))
            board["target"] = "nrf52840"
            runner._validate_plan_protocol_step(
                "safe_containment_refusal",
                "action",
                runner._PLAN_PROTOCOLS["HW04"]["safe_containment_refusal"],
                {
                    "id": "safe_containment_refusal",
                    "tool": "action_batch",
                    "arguments": {"board_id": "physical_board", "actions": []},
                    "fallback_from": "safe_containment_refusal_plan",
                    "expect": {
                        "payload": {
                            "status": "batch_failed",
                            "board_id": "physical_board",
                            "completed": [],
                            "failure": {
                                "error_type": "ToolError",
                                "tool_name": "write_memory",
                                "message": (
                                    "memory_write is unavailable in a application_flash region. "
                                    "Required remedy: board_safety_refresh.\n"
                                    "Safe exit: leave the board in the intended run state, then "
                                    "disconnect when hardware work is complete."
                                ),
                            },
                        }
                    },
                },
                board,
                "safe_containment_refusal",
            )

    def test_hw04_full_safe_containment_rejects_unrelated_batch_failure(self) -> None:
        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            board = _board(Path(temporary))
            with self.assertRaisesRegex(runner.FixtureError, "flash containment"):
                runner._validate_plan_protocol_step(
                    "safe_containment_refusal",
                    "action",
                    runner._PLAN_PROTOCOLS["HW04"]["safe_containment_refusal"],
                    {
                        "id": "safe_containment_refusal",
                        "tool": "action_batch",
                        "arguments": {"board_id": "physical_board", "actions": []},
                        "fallback_from": "safe_containment_refusal_plan",
                        "expect": {
                            "payload": {
                                "status": "batch_failed",
                                "board_id": "physical_board",
                                "completed": [],
                                "failure": {
                                    "error_type": "ToolError",
                                    "tool_name": "write_memory",
                                    "message": "transport unavailable",
                                },
                            }
                        },
                    },
                    board,
                    "safe_containment_refusal",
                )

    def test_hw05_restart_refusal_must_replay_the_old_mass_erase_approval_bytes(self) -> None:
        runner = _runner()
        approval = {
            "board_id": "physical_board",
            "hypothesis": "Target is locked.",
            "strategy": "Disclose only; do not unlock.",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "A stale approval is refused.",
            "expected_success_return": "The disclosure is reserved only.",
            "max_calls": 1,
            "max_calls_buffer": 0,
            "action_parameters": {"recovery_mechanism": "vendor-recovery"},
            "user_permission": "one-time",
        }
        pre = [{"id": "mass_erase_reservation", "arguments": approval}]
        with self.assertRaisesRegex(runner.FixtureError, "exact pre-restart approval bytes"):
            runner._validate_restart_unlock_refusal(
                pre,
                [
                    {
                        "id": "restart_stale_mass_erase_refusal",
                        "arguments": {**approval, "user_permission": "different"},
                    }
                ],
            )

    def test_fake_guarded_session_rejects_safe_fallback_without_its_accepted_plan(self) -> None:
        """AT10: runtime execution, not just schema, retains the plan/fallback protocol."""

        runner = _runner()
        board = {
            "board_id": "physical_board",
            "probe_uid": "probe-a",
            "target": "fixture-target",
            "serial_binding": "serial-a",
            "expected_tier": "setup-lite",
        }
        plan_arguments = {
            "board_id": "physical_board",
            "hypothesis": "The selected address is inside the confirmed map.",
            "strategy": "Read one bounded 32-bit value through the accepted fallback.",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "The server refuses an out-of-map address.",
            "expected_success_return": "One mapped word is returned.",
            "max_calls": 1,
            "max_calls_buffer": 0,
            "action_parameters": {"address": 0x20000000, "width": 32, "length": 4},
        }
        fallback = {
            "board_id": "physical_board",
            "actions": [
                {
                    "tool_name": "read_memory_address",
                    "arguments": {
                        "board_id": "physical_board",
                        "address": 0x20000000,
                        "width": 32,
                        "length": 4,
                    },
                }
            ],
        }

        class PlanRequiredSession:
            def __init__(self) -> None:
                self.accepted = False
                self.calls: list[str] = []

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                self.calls.append(tool)
                if tool == "read_memory_address-plan" and arguments == runner._PLAN_GUIDE_ARGUMENTS:
                    return [
                        SimpleNamespace(text="Plan initialization for read_memory_address-plan")
                    ]
                if tool == "read_memory_address-plan" and arguments == plan_arguments:
                    self.accepted = True
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "plan_accepted",
                                    "underlying_action": "read_memory_address",
                                    "stable_client_fallback": {
                                        "tool_name": "action_batch",
                                        "arguments": fallback,
                                    },
                                }
                            )
                        )
                    ]
                if tool == "action_batch" and self.accepted and arguments == fallback:
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "batch_completed",
                                    "board_id": "physical_board",
                                    "completed": [
                                        {
                                            "tool_name": "read_memory_address",
                                            "result": json.dumps(
                                                {
                                                    "status": "ok",
                                                    "operation": "read_memory_address",
                                                    "result": "DE AD BE EF",
                                                }
                                            ),
                                        }
                                    ],
                                }
                            )
                        )
                    ]
                return SimpleNamespace(
                    isError=True, content=[SimpleNamespace(text="missing accepted plan")]
                )

        guide = {
            "id": "safe_read_plan_guide",
            "tool": "read_memory_address-plan",
            "arguments": runner._PLAN_GUIDE_ARGUMENTS,
            "expect": {"text_contains": ["Plan initialization for read_memory_address-plan"]},
        }
        accept = {
            "id": "safe_read_plan_accept",
            "tool": "read_memory_address-plan",
            "arguments": plan_arguments,
            "expect": {
                "payload": {"status": "plan_accepted", "underlying_action": "read_memory_address"}
            },
            "capture": "safe_read_plan",
        }
        action = {
            "id": "safe_read",
            "tool": "action_batch",
            "arguments": fallback,
            "fallback_from": "safe_read_plan",
            "expect": {
                "payload": {
                    "status": "batch_completed",
                    "board_id": "physical_board",
                    "completed": [{"tool_name": "read_memory_address"}],
                },
                "child_payload": {"status": "ok", "operation": "read_memory_address"},
            },
        }

        async def check() -> None:
            session = PlanRequiredSession()
            await runner.run_steps(
                session,
                Path.cwd(),
                board,
                [guide, accept, action],
                pause=lambda _: asyncio.sleep(0),
            )
            self.assertEqual(
                session.calls,
                ["read_memory_address-plan", "read_memory_address-plan", "action_batch"],
            )
            with self.assertRaises(runner.CheckFailed):
                await runner.run_steps(
                    PlanRequiredSession(),
                    Path.cwd(),
                    board,
                    [
                        {
                            **action,
                            "fallback_from": "unrecorded_plan",
                        }
                    ],
                    pause=lambda _: asyncio.sleep(0),
                )

        asyncio.run(check())

    def test_lite_confirmation_uses_the_parsed_captured_batch_child_continuation(self) -> None:
        """AT10: confirmation cannot invent the setup continuation identifier."""

        runner = _runner()
        board = {
            "board_id": "physical_board",
            "probe_uid": "probe-a",
            "target": "fixture-target",
            "serial_binding": "serial-a",
            "expected_tier": "setup-lite",
        }
        continuation = "server-issued-continuation-8e3d"
        lite_setup = {
            "id": "lite_setup",
            "tool": "action_batch",
            "arguments": {
                "board_id": "physical_board",
                "actions": [{"tool_name": "board_setup", "arguments": {}}],
            },
            "expect": {
                "payload": {
                    "status": "batch_completed",
                    "completed": [{"tool_name": "board_setup"}],
                },
                "child_payload": {
                    "status": "setup_confirmation_required",
                    "continuation_id": continuation,
                },
            },
            "capture": "lite_setup",
        }
        lite_confirmation = {
            "id": "lite_confirmation",
            "tool": "continue_setup",
            "arguments": {
                "board_id": "physical_board",
                "continuation_id": "$capture.lite_setup.completed.0.result.continuation_id",
                "response": {"confirmed": True},
            },
            "expect": {"payload": {"accepted": "lite_confirmation"}},
        }

        class ContinuationSession:
            def __init__(self) -> None:
                self.confirmation_arguments: dict[str, object] | None = None

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                if tool == "action_batch":
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "batch_completed",
                                    "board_id": "physical_board",
                                    "completed": [
                                        {
                                            "tool_name": "board_setup",
                                            "result": json.dumps(
                                                {
                                                    "status": "setup_confirmation_required",
                                                    "continuation_id": continuation,
                                                }
                                            ),
                                        }
                                    ],
                                }
                            )
                        )
                    ]
                if tool == "continue_setup":
                    self.confirmation_arguments = arguments
                    return [SimpleNamespace(text=json.dumps({"accepted": "lite_confirmation"}))]
                raise AssertionError(f"unexpected live-MCP tool call: {tool}")

        with self.assertRaisesRegex(runner.FixtureError, "parsed lite-setup child capture"):
            runner._validate_step_contract(
                "HW02",
                {
                    **lite_confirmation,
                    "arguments": {
                        **lite_confirmation["arguments"],
                        "continuation_id": "fixture-invented-continuation",
                    },
                },
                board,
                [],
                "lite_confirmation",
            )
        runner._validate_step_contract("HW02", lite_confirmation, board, [], "lite_confirmation")

        async def check() -> None:
            session = ContinuationSession()
            _rows, captures = await runner.run_steps(
                session,
                Path.cwd(),
                board,
                [lite_setup, lite_confirmation],
                pause=lambda _: asyncio.sleep(0),
            )
            self.assertEqual(
                session.confirmation_arguments,
                lite_confirmation["arguments"] | {"continuation_id": continuation},
            )
            self.assertEqual(
                captures["lite_setup"]["completed"][0]["result"],
                json.dumps(
                    {
                        "status": "setup_confirmation_required",
                        "continuation_id": continuation,
                    }
                ),
            )

        asyncio.run(check())

    def test_failed_batch_retains_completed_and_child_mismatch_transcript_rows(self) -> None:
        """AT10: a failed safe child leaves actionable private transcript evidence."""

        runner = _runner()
        board = {"board_id": "private-board"}
        steps = [
            {
                "id": "connect",
                "tool": "connect",
                "arguments": {"board_id": "private-board"},
                "expect": {"text_contains": ["connected"]},
            },
            {
                "id": "capabilities",
                "tool": "get_capabilities",
                "arguments": {"board_id": "private-board"},
                "expect": {"payload": {"status": "capability_status"}},
            },
            {
                "id": "safe_read",
                "tool": "action_batch",
                "arguments": {"board_id": "private-board", "actions": []},
                "expect": {
                    "payload": {
                        "status": "batch_completed",
                        "completed": [{"tool_name": "read_memory_address"}],
                    },
                    "child_payload": {"status": "ok", "operation": "read_memory_address"},
                },
            },
        ]

        class FailingBatchSession:
            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                del arguments
                if tool == "connect":
                    return [SimpleNamespace(text="connected")]
                if tool == "get_capabilities":
                    return [SimpleNamespace(text=json.dumps({"status": "capability_status"}))]
                if tool == "action_batch":
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "batch_completed",
                                    "completed": [
                                        {
                                            "tool_name": "read_memory_address",
                                            "result": json.dumps(
                                                {
                                                    "status": "batch_failed",
                                                    "operation": "read_memory_address",
                                                    "code": "safe/child-failed",
                                                }
                                            ),
                                        }
                                    ],
                                }
                            )
                        )
                    ]
                if tool == "disconnect":
                    return [SimpleNamespace(text="disconnected")]
                raise AssertionError(f"unexpected MCP tool call: {tool}")

        class SessionContext:
            async def __aenter__(self) -> FailingBatchSession:
                return FailingBatchSession()

            async def __aexit__(self, *unused: object) -> None:
                del unused

        async def check() -> None:
            transcript = await runner.execute_with_session_factory(
                {
                    "boards": {
                        "nrf52840": board,
                        "stm32l476rtg": {"board_id": "blocked-board"},
                    },
                    "case_project_roots": {"HW02": str(Path.cwd())},
                    "cases": {
                        "HW02": {
                            "boards": {
                                "nrf52840": {"status": "ready", "steps": steps},
                                "stm32l476rtg": {"status": "blocked", "reason": "fixture"},
                            }
                        }
                    },
                },
                ("HW02",),
                lambda _root: SessionContext(),
                pause=lambda _: asyncio.sleep(0),
                allow_write_flash=False,
            )
            phase = transcript["case_results"]["HW02"]["boards"]["nrf52840"]["phases"][0]
            self.assertEqual(
                [row["id"] for row in phase["steps"]],
                ["connect", "capabilities", "safe_read"],
            )
            self.assertTrue(phase["steps"][0]["passed"])
            self.assertTrue(phase["steps"][1]["passed"])
            self.assertFalse(phase["steps"][2]["passed"])
            self.assertEqual(
                phase["steps"][2]["actual"]["payload"]["completed"][0]["result"],
                json.dumps(
                    {
                        "status": "batch_failed",
                        "operation": "read_memory_address",
                        "code": "safe/child-failed",
                    }
                ),
            )
            self.assertIn("child_payload.status", phase["steps"][2]["failure"])

        asyncio.run(check())

    def test_hw04_raw_flash_verification_uses_three_distinct_sessions(self) -> None:
        """HW04 ignores stale mutation-session reads and verifies each image freshly."""

        runner = _runner()
        raw_steps = [
            {
                "id": "raw_connect",
                "tool": "connect",
                "arguments": {
                    "board_id": "stm-board",
                    "probe_uid": "stm-board-probe",
                    "target": "stm32l476rgtx",
                },
                "expect": {"text_contains": ["stm-board-probe"]},
            },
            {
                "id": "initial_ram_read",
                "tool": "read_memory_raw",
                "arguments": {"board_id": "stm-board", "address": 0x20000000},
                "expect": {"payload": {"status": "ok", "result": "0x11111111"}},
                "capture": "initial_ram",
            },
            {
                "id": "raw_ram_write",
                "tool": "write_memory_raw",
                "arguments": {"board_id": "stm-board", "address": 0x20000000, "value": 0xA5A5A5A5},
                "expect": {"payload": {"status": "ok"}},
            },
            {
                "id": "raw_ram_readback",
                "tool": "read_memory_raw",
                "arguments": {"board_id": "stm-board", "address": 0x20000000},
                "expect": {"payload": {"status": "ok", "result": "0xA5A5A5A5"}},
            },
            {
                "id": "raw_ram_restore",
                "tool": "write_memory_raw",
                "arguments": {
                    "board_id": "stm-board",
                    "address": 0x20000000,
                    "value": "$capture.initial_ram.result",
                },
                "expect": {"payload": {"status": "ok"}},
            },
            {
                "id": "ram_restored_readback",
                "tool": "read_memory_raw",
                "arguments": {"board_id": "stm-board", "address": 0x20000000},
                "expect": {"payload": {"status": "ok", "result": "$capture.initial_ram.result"}},
            },
            {
                "id": "initial_flash_readback",
                "tool": "read_memory_raw",
                "arguments": {"board_id": "stm-board", "address": 0x080FF800},
                "expect": {"payload": {"status": "ok", "result": "FF FF FF FF"}},
            },
            {
                "id": "raw_flash",
                "tool": "flash_raw",
                "arguments": {"board_id": "stm-board", "artifact": "raw-test"},
                "expect": {"payload": {"status": "ok"}},
            },
            {
                "id": "raw_flash_readback",
                "tool": "read_memory_raw",
                "arguments": {"board_id": "stm-board", "address": 0x080FF800},
                "expect": {"payload": {"status": "ok", "result": "AA AA AA AA"}},
            },
            {
                "id": "raw_restore",
                "tool": "flash_raw",
                "arguments": {"board_id": "stm-board", "artifact": "raw-restore"},
                "expect": {"payload": {"status": "ok"}},
            },
            {
                "id": "raw_restored_readback",
                "tool": "read_memory_raw",
                "arguments": {"board_id": "stm-board", "address": 0x080FF800},
                "expect": {"payload": {"status": "ok", "result": "FF FF FF FF"}},
            },
            {
                "id": "final_disconnect",
                "tool": "disconnect",
                "arguments": {"board_id": "stm-board"},
                "expect": {"text_contains": ["disconnected"]},
            },
        ]
        calls: dict[str, list[str]] = {"mutation": [], "test": [], "restore": []}

        def response(payload: dict[str, object]) -> list[SimpleNamespace]:
            return [SimpleNamespace(text=json.dumps(payload))]

        class Session:
            def __init__(self, name: str) -> None:
                self.name = name

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                calls[self.name].append(tool)
                if tool == "connect":
                    return [
                        SimpleNamespace(
                            text="Connected to board 'Friendly product label' via probe STM-BOARD-PROBE."
                        )
                    ]
                if tool == "disconnect":
                    return [SimpleNamespace(text="disconnected")]
                if self.name == "mutation":
                    if tool == "read_memory_raw" and arguments["address"] == 0x20000000:
                        reads = ["0x11111111", "0xA5A5A5A5", "0x11111111"]
                        return response(
                            {"status": "ok", "result": reads[calls[self.name].count(tool) - 1]}
                        )
                    if tool == "read_memory_raw":
                        return response({"status": "ok", "result": "FF FF FF FF"})
                    if tool == "write_memory_raw":
                        return response({"status": "ok"})
                    if tool == "flash_raw" and arguments["artifact"] == "raw-test":
                        return response({"status": "ok"})
                    raise AssertionError(f"mutation session consulted after test flash: {tool}")
                if self.name == "test":
                    if tool == "read_memory_raw":
                        return response({"status": "ok", "result": "AA AA AA AA"})
                    if tool == "flash_raw" and arguments["artifact"] == "raw-restore":
                        return response({"status": "ok"})
                if self.name == "restore" and tool == "read_memory_raw":
                    return response({"status": "ok", "result": "FF FF FF FF"})
                raise AssertionError(f"unexpected {self.name} session call: {tool} {arguments!r}")

        class SessionContext:
            def __init__(self, name: str) -> None:
                self.session = Session(name)
                self.exited = False

            async def __aenter__(self) -> Session:
                return self.session

            async def __aexit__(self, *unused: object) -> None:
                del unused
                self.exited = True

        with tempfile.TemporaryDirectory() as temporary:
            contexts = [
                SessionContext("mutation"),
                SessionContext("test"),
                SessionContext("restore"),
            ]
            fixture = _hw04_execution_fixture(Path(temporary), raw_steps, [])
            transcript = asyncio.run(
                runner.execute_with_session_factory(
                    fixture,
                    ("HW04",),
                    lambda _root: contexts.pop(0),
                    pause=lambda _: asyncio.sleep(0),
                    allow_write_flash=True,
                )
            )

        phase = transcript["case_results"]["HW04"]["boards"]["stm32l476rtg"]["phases"][0]
        self.assertEqual(transcript["case_results"]["HW04"]["status"], "blocked")
        self.assertEqual(transcript["overall_status"], "blocked")
        self.assertEqual([row["id"] for row in phase["steps"]], [step["id"] for step in raw_steps])
        self.assertEqual(
            calls["mutation"],
            [
                "connect",
                "read_memory_raw",
                "write_memory_raw",
                "read_memory_raw",
                "write_memory_raw",
                "read_memory_raw",
                "read_memory_raw",
                "flash_raw",
                "disconnect",
            ],
        )
        self.assertEqual(calls["test"], ["connect", "read_memory_raw", "flash_raw", "disconnect"])
        self.assertEqual(calls["restore"], ["connect", "read_memory_raw", "disconnect"])
        self.assertEqual(contexts, [])

    def test_hw04_raw_flash_mismatch_emergency_restores_and_reads_back(self) -> None:
        """A rejected raw test flash is restored and verified in two fresh sessions."""

        runner = _runner()
        case = self
        calls: dict[str, list[str]] = {"mutation": [], "restore": [], "verify": []}

        class Session:
            def __init__(self, name: str) -> None:
                self.name = name

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                calls[self.name].append(tool)
                if tool == "connect":
                    return [SimpleNamespace(text="Connected via stm-board-probe.")]
                if tool == "disconnect":
                    return [SimpleNamespace(text="disconnected")]
                if self.name == "mutation" and tool == "flash_raw":
                    case.assertEqual(arguments["artifact"], "raw-test")
                    return [SimpleNamespace(text=json.dumps({"status": "refused"}))]
                if self.name == "restore" and tool == "flash_raw":
                    case.assertEqual(arguments["artifact"], "raw-restore")
                    return [SimpleNamespace(text=json.dumps({"status": "ok"}))]
                if tool == "read_memory_raw":
                    return [
                        SimpleNamespace(text=json.dumps({"status": "ok", "result": "FF FF FF FF"}))
                    ]
                if tool == "write_memory_raw":
                    return [SimpleNamespace(text=json.dumps({"status": "ok"}))]
                raise AssertionError(f"unexpected {self.name} call: {tool} {arguments!r}")

        class SessionContext:
            def __init__(self, name: str) -> None:
                self.name = name

            async def __aenter__(self) -> Session:
                return Session(self.name)

            async def __aexit__(self, *unused: object) -> None:
                del unused

        with tempfile.TemporaryDirectory() as temporary:
            contexts = [
                SessionContext("mutation"),
                SessionContext("restore"),
                SessionContext("verify"),
            ]
            transcript = asyncio.run(
                runner.execute_with_session_factory(
                    _hw04_execution_fixture(Path(temporary), _hw04_complete_raw_steps(), []),
                    ("HW04",),
                    lambda _root: contexts.pop(0),
                    pause=lambda _: asyncio.sleep(0),
                    allow_write_flash=True,
                )
            )

        phase = transcript["case_results"]["HW04"]["boards"]["stm32l476rtg"]["phases"][0]
        self.assertIn("raw_flash.payload.status", phase["failure"])
        emergency = phase["emergency_restoration"]
        self.assertEqual(emergency["lane"], "raw")
        self.assertEqual(emergency["restore"]["status"], "pass")
        self.assertEqual(emergency["readback"]["status"], "pass")
        self.assertEqual(
            [session["name"] for session in emergency["physical_sessions"]],
            ["emergency_restore", "emergency_restore_verification"],
        )
        self.assertEqual(calls["restore"], ["connect", "flash_raw", "disconnect"])
        self.assertEqual(calls["verify"], ["connect", "read_memory_raw", "disconnect"])
        self.assertEqual(transcript["case_results"]["HW04"]["status"], "failed")

    def test_hw04_safe_flash_verification_uses_three_fresh_full_sessions(self) -> None:
        """Safe test/read/restore work is never verified in the dispatch session."""

        runner = _runner()
        steps = _hw04_complete_safe_steps()
        calls: dict[str, list[str]] = {"mutation": [], "test": [], "restore": []}
        fallbacks = {
            "mutation": [
                {"board_id": "stm-board", "actions": [{"kind": "safe-test"}]},
                {"board_id": "stm-board", "actions": [{"kind": "containment-refusal"}]},
            ],
            "test": [
                {"board_id": "stm-board", "actions": [{"kind": "safe-readback"}]},
                {"board_id": "stm-board", "actions": [{"kind": "safe-restore"}]},
            ],
            "restore": [
                {
                    "board_id": "stm-board",
                    "actions": [{"kind": "safe-restored-readback"}],
                }
            ],
        }

        def batch(child: dict[str, object]) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    text=json.dumps(
                        {
                            "status": "batch_completed",
                            "completed": [
                                {"tool_name": child["operation"], "result": json.dumps(child)}
                            ],
                        }
                    )
                )
            ]

        class Session:
            def __init__(self, name: str) -> None:
                self.name = name

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                calls[self.name].append(tool)
                if tool == "setup_overview":
                    return [SimpleNamespace(text=json.dumps({"status": "setup_routes_ready"}))]
                if tool == "connect":
                    return [SimpleNamespace(text="Connected via stm-board-probe.")]
                if tool == "load_setup_tool":
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "setup_tool_loaded",
                                    "next_call": {"arguments": {"probe_id": "current-token"}},
                                }
                            )
                        )
                    ]
                if tool == "board_validate":
                    return [SimpleNamespace(text=json.dumps({"status": "validation_passed"}))]
                if tool.endswith("-plan"):
                    if arguments.get("accept") is not True:
                        return [SimpleNamespace(text="guide")]
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "plan_accepted",
                                    "stable_client_fallback": {
                                        "tool_name": "action_batch",
                                        "arguments": fallbacks[self.name].pop(0),
                                    },
                                }
                            )
                        )
                    ]
                if tool == "action_batch":
                    kind = arguments["actions"][0]["kind"]
                    child = {
                        "status": "ok",
                        "operation": (
                            "read_memory_address" if "readback" in kind else "flash_application"
                        ),
                    }
                    if kind == "safe-readback":
                        child["result"] = "A5 A5 A5 A5"
                    elif kind == "safe-restored-readback":
                        child["result"] = "FF FF FF FF"
                    elif kind == "containment-refusal":
                        child.update({"status": "refused", "operation": "read_memory_address"})
                    return batch(child)
                if tool == "disconnect":
                    return [SimpleNamespace(text="disconnected")]
                raise AssertionError(f"unexpected {self.name} call: {tool} {arguments!r}")

        class SessionContext:
            def __init__(self, name: str) -> None:
                self.name = name

            async def __aenter__(self) -> Session:
                return Session(self.name)

            async def __aexit__(self, *unused: object) -> None:
                del unused

        with tempfile.TemporaryDirectory() as temporary:
            contexts = [
                SessionContext("mutation"),
                SessionContext("test"),
                SessionContext("restore"),
            ]
            transcript = asyncio.run(
                runner.execute_with_session_factory(
                    _hw04_execution_fixture(Path(temporary), [], steps),
                    ("HW04",),
                    lambda _root: contexts.pop(0),
                    pause=lambda _: asyncio.sleep(0),
                    allow_write_flash=True,
                )
            )

        phase = transcript["case_results"]["HW04"]["boards"]["stm32l476rtg"]["phases"][1]
        self.assertEqual(phase["status"], "pass")
        self.assertEqual([row["id"] for row in phase["steps"]], [step["id"] for step in steps])
        self.assertEqual(
            [record["name"] for record in phase["physical_sessions"]],
            ["mutation", "test_verification_and_restore", "restoration_verification"],
        )
        self.assertEqual(
            calls["mutation"],
            [
                "setup_overview",
                "connect",
                "load_setup_tool",
                "board_validate",
                "flash_application-plan",
                "flash_application-plan",
                "action_batch",
                "read_memory_address-plan",
                "read_memory_address-plan",
                "action_batch",
                "disconnect",
            ],
        )
        self.assertEqual(
            calls["test"],
            [
                "setup_overview",
                "connect",
                "load_setup_tool",
                "board_validate",
                "read_memory_address-plan",
                "read_memory_address-plan",
                "action_batch",
                "flash_application-plan",
                "flash_application-plan",
                "action_batch",
                "disconnect",
            ],
        )
        self.assertEqual(
            calls["restore"],
            [
                "setup_overview",
                "connect",
                "load_setup_tool",
                "board_validate",
                "read_memory_address-plan",
                "read_memory_address-plan",
                "action_batch",
                "disconnect",
            ],
        )

    def test_hw04_fresh_raw_test_read_mismatch_uses_two_session_emergency_recovery(self) -> None:
        """A fresh post-test mismatch never consults the mutation session again."""

        runner = _runner()
        case = self
        calls: dict[str, list[str]] = {
            "mutation": [],
            "test": [],
            "emergency_restore": [],
            "emergency_verify": [],
        }

        class Session:
            def __init__(self, name: str) -> None:
                self.name = name

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                calls[self.name].append(tool)
                if tool == "connect":
                    return [SimpleNamespace(text="Connected via stm-board-probe.")]
                if tool == "disconnect":
                    return [SimpleNamespace(text="disconnected")]
                if self.name == "mutation":
                    if tool == "flash_raw":
                        case.assertEqual(arguments["artifact"], "raw-test")
                    if tool in {"read_memory_raw", "write_memory_raw", "flash_raw"}:
                        return [
                            SimpleNamespace(
                                text=json.dumps({"status": "ok", "result": "FF FF FF FF"})
                            )
                        ]
                if self.name == "test" and tool == "read_memory_raw":
                    return [
                        SimpleNamespace(text=json.dumps({"status": "ok", "result": "00 00 00 00"}))
                    ]
                if self.name == "emergency_restore" and tool == "flash_raw":
                    case.assertEqual(arguments["artifact"], "raw-restore")
                    return [SimpleNamespace(text=json.dumps({"status": "ok"}))]
                if self.name == "emergency_verify" and tool == "read_memory_raw":
                    return [
                        SimpleNamespace(text=json.dumps({"status": "ok", "result": "FF FF FF FF"}))
                    ]
                raise AssertionError(f"unexpected {self.name} call: {tool} {arguments!r}")

        class SessionContext:
            def __init__(self, name: str) -> None:
                self.name = name

            async def __aenter__(self) -> Session:
                return Session(self.name)

            async def __aexit__(self, *unused: object) -> None:
                del unused

        with tempfile.TemporaryDirectory() as temporary:
            contexts = [
                SessionContext("mutation"),
                SessionContext("test"),
                SessionContext("emergency_restore"),
                SessionContext("emergency_verify"),
            ]
            phase = asyncio.run(
                runner._execute_hw04_lane(
                    lambda _root: contexts.pop(0),
                    Path(temporary),
                    {
                        "board_id": "stm-board",
                        "probe_uid": "stm-board-probe",
                        "target": "stm32l476rgtx",
                    },
                    _hw04_complete_raw_steps(),
                    lane="raw",
                    phase_name="raw",
                    pause=lambda _: asyncio.sleep(0),
                )
            )

        self.assertIn("raw_flash_readback.payload.result", phase["failure"])
        self.assertEqual(calls["mutation"].count("read_memory_raw"), 4)
        self.assertEqual(calls["test"], ["connect", "read_memory_raw", "disconnect"])
        emergency = phase["emergency_restoration"]
        self.assertEqual(emergency["restore"]["status"], "pass")
        self.assertEqual(emergency["readback"]["status"], "pass")
        self.assertEqual(calls["emergency_restore"], ["connect", "flash_raw", "disconnect"])
        self.assertEqual(calls["emergency_verify"], ["connect", "read_memory_raw", "disconnect"])

    def test_hw04_fresh_raw_restore_read_mismatch_repeats_exact_recovery(self) -> None:
        """A bad fresh restore verifier leaves the restoration guard outstanding."""

        runner = _runner()
        case = self
        calls: dict[str, list[str]] = {
            "mutation": [],
            "test": [],
            "restore_verifier": [],
            "emergency_restore": [],
            "emergency_verify": [],
        }

        class Session:
            def __init__(self, name: str) -> None:
                self.name = name

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                calls[self.name].append(tool)
                if tool == "connect":
                    return [SimpleNamespace(text="Connected via stm-board-probe.")]
                if tool == "disconnect":
                    return [SimpleNamespace(text="disconnected")]
                if self.name == "mutation" and tool in {
                    "read_memory_raw",
                    "write_memory_raw",
                    "flash_raw",
                }:
                    return [
                        SimpleNamespace(text=json.dumps({"status": "ok", "result": "FF FF FF FF"}))
                    ]
                if self.name == "test" and tool == "read_memory_raw":
                    return [
                        SimpleNamespace(text=json.dumps({"status": "ok", "result": "AA AA AA AA"}))
                    ]
                if self.name == "test" and tool == "flash_raw":
                    return [SimpleNamespace(text=json.dumps({"status": "ok"}))]
                if self.name == "restore_verifier" and tool == "read_memory_raw":
                    return [
                        SimpleNamespace(text=json.dumps({"status": "ok", "result": "00 00 00 00"}))
                    ]
                if self.name == "emergency_restore" and tool == "flash_raw":
                    case.assertEqual(arguments["artifact"], "raw-restore")
                    return [SimpleNamespace(text=json.dumps({"status": "ok"}))]
                if self.name == "emergency_verify" and tool == "read_memory_raw":
                    return [
                        SimpleNamespace(text=json.dumps({"status": "ok", "result": "FF FF FF FF"}))
                    ]
                raise AssertionError(f"unexpected {self.name} call: {tool} {arguments!r}")

        class SessionContext:
            def __init__(self, name: str) -> None:
                self.name = name

            async def __aenter__(self) -> Session:
                return Session(self.name)

            async def __aexit__(self, *unused: object) -> None:
                del unused

        with tempfile.TemporaryDirectory() as temporary:
            contexts = [
                SessionContext("mutation"),
                SessionContext("test"),
                SessionContext("restore_verifier"),
                SessionContext("emergency_restore"),
                SessionContext("emergency_verify"),
            ]
            phase = asyncio.run(
                runner._execute_hw04_lane(
                    lambda _root: contexts.pop(0),
                    Path(temporary),
                    {
                        "board_id": "stm-board",
                        "probe_uid": "stm-board-probe",
                        "target": "stm32l476rgtx",
                    },
                    _hw04_complete_raw_steps(),
                    lane="raw",
                    phase_name="raw",
                    pause=lambda _: asyncio.sleep(0),
                )
            )

        self.assertIn("raw_restored_readback.payload.result", phase["failure"])
        self.assertEqual(calls["restore_verifier"], ["connect", "read_memory_raw", "disconnect"])
        self.assertEqual(phase["emergency_restoration"]["restore"]["status"], "pass")
        self.assertEqual(phase["emergency_restoration"]["readback"]["status"], "pass")

    def test_hw04_fresh_verifier_startup_and_exit_failures_trigger_recovery(self) -> None:
        """Fresh verifier startup/close failures retain their cause and restore conservatively."""

        runner = _runner()

        for failure_kind in ("startup", "exit"):
            with (
                self.subTest(failure_kind=failure_kind),
                tempfile.TemporaryDirectory() as temporary,
            ):
                calls: dict[str, list[str]] = {
                    "mutation": [],
                    "test": [],
                    "emergency_restore": [],
                    "emergency_verify": [],
                }

                class Session:
                    def __init__(self, name: str) -> None:
                        self.name = name

                    async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                        calls[self.name].append(tool)
                        if tool == "connect":
                            return [SimpleNamespace(text="Connected via stm-board-probe.")]
                        if tool == "disconnect":
                            return [SimpleNamespace(text="disconnected")]
                        if self.name == "mutation" and tool in {
                            "read_memory_raw",
                            "write_memory_raw",
                            "flash_raw",
                        }:
                            return [
                                SimpleNamespace(
                                    text=json.dumps({"status": "ok", "result": "FF FF FF FF"})
                                )
                            ]
                        if self.name == "test" and tool == "read_memory_raw":
                            return [
                                SimpleNamespace(
                                    text=json.dumps({"status": "ok", "result": "AA AA AA AA"})
                                )
                            ]
                        if self.name == "test" and tool == "flash_raw":
                            return [SimpleNamespace(text=json.dumps({"status": "ok"}))]
                        if self.name == "emergency_restore" and tool == "flash_raw":
                            return [SimpleNamespace(text=json.dumps({"status": "ok"}))]
                        if self.name == "emergency_verify" and tool == "read_memory_raw":
                            return [
                                SimpleNamespace(
                                    text=json.dumps({"status": "ok", "result": "FF FF FF FF"})
                                )
                            ]
                        raise AssertionError(f"unexpected {self.name} call: {tool} {arguments!r}")

                class SessionContext:
                    def __init__(self, name: str, *, fail_exit: bool = False) -> None:
                        self.name = name
                        self.fail_exit = fail_exit

                    async def __aenter__(self) -> Session:
                        return Session(self.name)

                    async def __aexit__(self, *unused: object) -> None:
                        del unused
                        if self.fail_exit:
                            raise RuntimeError("fresh verifier context close")

                contexts: list[SessionContext | RuntimeError] = [SessionContext("mutation")]
                if failure_kind == "startup":
                    contexts.append(RuntimeError("fresh verifier startup"))
                else:
                    contexts.append(SessionContext("test", fail_exit=True))
                contexts.extend(
                    [SessionContext("emergency_restore"), SessionContext("emergency_verify")]
                )

                def factory(_root: Path) -> SessionContext:
                    item = contexts.pop(0)
                    if isinstance(item, RuntimeError):
                        raise item
                    return item

                phase = asyncio.run(
                    runner._execute_hw04_lane(
                        factory,
                        Path(temporary),
                        {
                            "board_id": "stm-board",
                            "probe_uid": "stm-board-probe",
                            "target": "stm32l476rgtx",
                        },
                        _hw04_complete_raw_steps(),
                        lane="raw",
                        phase_name="raw",
                        pause=lambda _: asyncio.sleep(0),
                    )
                )

                expected_error = (
                    "fresh verifier startup" if failure_kind == "startup" else "context close"
                )
                self.assertIn(expected_error, phase["failure"])
                self.assertEqual(phase["emergency_restoration"]["restore"]["status"], "pass")
                self.assertEqual(phase["emergency_restoration"]["readback"]["status"], "pass")
                self.assertEqual(calls["emergency_restore"], ["connect", "flash_raw", "disconnect"])
                self.assertEqual(
                    calls["emergency_verify"], ["connect", "read_memory_raw", "disconnect"]
                )
                if failure_kind == "exit":
                    self.assertIn("session_cleanup_errors", phase)
                self.assertEqual(contexts, [])

    def test_hw04_integrity_is_rejected_before_a_session_starts(self) -> None:
        """AT10: a mutable/missing digest cannot reach even a deterministic transport."""

        runner = _runner()
        entered = False
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _hw04_execution_fixture(Path(temporary), [], [])
            board = fixture["boards"]["stm32l476rtg"]
            board["flash_fixture"]["raw"]["integrity"]["artifact_sha256"] = "0" * 64

            class NoTransport:
                async def __aenter__(self) -> object:
                    nonlocal entered
                    entered = True
                    raise AssertionError("preflight opened a session")

                async def __aexit__(self, *unused: object) -> None:
                    del unused

            with self.assertRaisesRegex(runner.FixtureError, "artifact_sha256"):
                asyncio.run(
                    runner.execute_with_session_factory(
                        fixture,
                        ("HW04",),
                        lambda _root: NoTransport(),
                        pause=lambda _: asyncio.sleep(0),
                        allow_write_flash=True,
                    )
                )
        self.assertFalse(entered)

    def test_hw04_revalidates_each_test_and_restore_artifact_immediately_before_dispatch(
        self,
    ) -> None:
        """AT10: a post-preflight artifact swap cannot reach a live tool call."""

        runner = _runner()
        cases = (
            ("raw_flash", "raw", "artifact", "flash_raw"),
            ("raw_restore", "raw", "restore_artifact", "flash_raw"),
            ("safe_flash", "safe", "artifact", "action_batch"),
            ("safe_restore", "safe", "restore_artifact", "action_batch"),
        )
        for identifier, lane, field, tool in cases:
            with self.subTest(identifier=identifier), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture = _hw04_execution_fixture(root, [], [])
                board = fixture["boards"]["stm32l476rtg"]
                path = Path(board["flash_fixture"][lane][field])
                path.write_bytes(path.read_bytes() + b"post-preflight-swap")
                called = False

                class Session:
                    async def call_tool(
                        self, unused_tool: str, unused_arguments: dict[str, object]
                    ) -> object:
                        nonlocal called
                        called = True
                        del unused_tool, unused_arguments
                        raise AssertionError("artifact drift reached the transport")

                step = {
                    "id": identifier,
                    "tool": tool,
                    "arguments": {
                        "board_id": board["board_id"],
                        "artifact": str(path),
                    },
                    "expect": {"payload": {"status": "ok"}},
                }
                with self.assertRaisesRegex(runner.FixtureError, "sha256"):
                    asyncio.run(
                        runner.run_steps(
                            Session(),
                            root,
                            board,
                            [step],
                            pause=lambda _: asyncio.sleep(0),
                        )
                    )
                self.assertFalse(called)

    def test_hw04_full_page_hash_expectation_rejects_one_byte_drift(self) -> None:
        """AT10: full-page evidence compares bytes, not merely response shape or length."""

        runner = _runner()
        expected_bytes = bytes((0x00, 0x11, 0x22, 0x33))
        step = {
            "id": "page_hash_check",
            "tool": "read_memory_raw",
            "arguments": {"board_id": "physical_board", "address": 0, "width": 8, "length": 4},
            "expect": {
                "payload": {"status": "ok"},
                "hex_sha256": {
                    "byte_count": len(expected_bytes),
                    "sha256": hashlib.sha256(expected_bytes).hexdigest(),
                },
            },
        }

        class Session:
            async def call_tool(self, unused_tool: str, unused_arguments: object) -> object:
                del unused_tool, unused_arguments
                return [SimpleNamespace(text=json.dumps({"status": "ok", "result": "00 11 22 34"}))]

        with self.assertRaisesRegex(runner.CheckFailed, "SHA-256"):
            asyncio.run(
                runner.run_steps(
                    Session(),
                    Path.cwd(),
                    {"board_id": "physical_board"},
                    [step],
                    pause=lambda _: asyncio.sleep(0),
                )
            )

    def test_hw04_safe_child_page_hash_checks_exact_public_bytes(self) -> None:
        """AT10: planned safe reads hash the child result before accepting restoration."""

        runner = _runner()
        expected_bytes = bytes((0x00, 0x11, 0x22, 0x33))
        step = {
            "id": "safe_restored_page_0",
            "tool": "action_batch",
            "arguments": {"board_id": "physical_board", "actions": []},
            "expect": {
                "payload": {
                    "status": "batch_completed",
                    "board_id": "physical_board",
                    "completed": [{"tool_name": "read_memory_address"}],
                },
                "child_hex_sha256": {
                    "byte_count": len(expected_bytes),
                    "sha256": hashlib.sha256(expected_bytes).hexdigest(),
                },
            },
        }

        class Session:
            def __init__(self, data: bytes) -> None:
                self.data = data

            async def call_tool(self, unused_tool: str, unused_arguments: object) -> object:
                del unused_tool, unused_arguments
                result = " ".join(f"{value:02X}" for value in self.data)
                result += "\nSafe exit: leave the board in the intended run state, then disconnect when hardware work is complete."
                return [
                    SimpleNamespace(
                        text=json.dumps(
                            {
                                "status": "batch_completed",
                                "board_id": "physical_board",
                                "completed": [
                                    {"tool_name": "read_memory_address", "result": result}
                                ],
                            }
                        )
                    )
                ]

        async def check() -> None:
            rows, _captures = await runner.run_steps(
                Session(expected_bytes),
                Path.cwd(),
                {"board_id": "physical_board"},
                [step],
                pause=lambda _: asyncio.sleep(0),
            )
            self.assertTrue(rows[0]["passed"])
            with self.assertRaisesRegex(runner.CheckFailed, "SHA-256"):
                await runner.run_steps(
                    Session(expected_bytes[:-1] + b"\x34"),
                    Path.cwd(),
                    {"board_id": "physical_board"},
                    [step],
                    pause=lambda _: asyncio.sleep(0),
                )

        asyncio.run(check())

    def test_hw04_sparse_safe_lifecycle_requires_all_preflight_and_restored_pages(self) -> None:
        """AT10: Nordic sparse safety makes six full-page proofs non-optional."""

        runner = _runner()
        complete = runner._hw04_full_lifecycle(True)
        runner._validate_hw04_lifecycle(
            "full_steps",
            [{"id": identifier} for identifier in complete],
            require_page_proof=True,
        )
        for missing in ("safe_preflight_page_1", "safe_restored_page_2"):
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(runner.FixtureError, "HW04 full_steps lifecycle"),
            ):
                runner._validate_hw04_lifecycle(
                    "full_steps",
                    [{"id": identifier} for identifier in complete if identifier != missing],
                    require_page_proof=True,
                )

    def test_hw04_retained_ram_evidence_uses_flash_only_raw_lifecycle(self) -> None:
        """AT10 resume does not repeat a separately accepted RAM mutation."""

        runner = _runner()
        flash_only = (
            "raw_connect",
            "initial_flash_readback",
            "raw_flash",
            "raw_flash_readback",
            "raw_restore",
            "raw_restored_readback",
            "final_disconnect",
        )
        runner._validate_hw04_lifecycle(
            "raw_steps",
            [{"id": identifier} for identifier in flash_only],
            retained_ram=True,
        )
        with self.assertRaisesRegex(runner.FixtureError, "HW04 raw_steps lifecycle"):
            runner._validate_hw04_lifecycle(
                "raw_steps",
                [{"id": identifier} for identifier in runner._HW04_RAW_LIFECYCLE],
                retained_ram=True,
            )

    def test_hw04_retained_raw_evidence_uses_safe_only_resume_lifecycle(self) -> None:
        """AT10 retry cannot repeat the now-accepted Nordic raw flash phase."""

        runner = _runner()
        runner._validate_hw04_lifecycle("raw_steps", [], retained_raw=True)
        with self.assertRaisesRegex(runner.FixtureError, "HW04 raw_steps lifecycle"):
            runner._validate_hw04_lifecycle(
                "raw_steps",
                [{"id": "raw_connect"}],
                retained_raw=True,
            )

    def test_hw04_flash_only_executor_never_selects_a_ram_call(self) -> None:
        """AT10 resume partitions only the seven remaining raw flash steps."""

        runner = _runner()
        steps = [{"id": identifier} for identifier in runner._HW04_RAW_FLASH_ONLY_LIFECYCLE]
        selected: list[str] = []

        async def physical_session(
            unused_factory: object,
            unused_root: object,
            unused_board: object,
            *,
            name: str,
            bootstrap: object,
            logical_steps: list[dict[str, str]],
            pause: object,
            restore_state: object = None,
        ) -> tuple[dict[str, object], list[dict[str, object]], dict[str, object], None]:
            del unused_factory, unused_root, unused_board, pause, restore_state
            identifiers = [step["id"] for step in [*bootstrap, *logical_steps]]
            selected.extend(identifiers)
            rows = [{"id": identifier, "passed": True} for identifier in identifiers]
            return {"name": name, "cleanup": {"attempted": True}}, rows, {}, None

        with patch.object(runner, "_run_hw04_physical_session", side_effect=physical_session):
            phase = asyncio.run(
                runner._execute_hw04_lane(
                    lambda _root: contextlib.nullcontext(),
                    Path.cwd(),
                    {"board_id": "physical_board"},
                    steps,
                    lane="raw",
                    phase_name="raw",
                    pause=lambda _: asyncio.sleep(0),
                )
            )

        self.assertEqual(phase["status"], "pass")
        self.assertFalse(
            {
                "initial_ram_read",
                "raw_ram_write",
                "raw_ram_readback",
                "raw_ram_restore",
                "ram_restored_readback",
            }.intersection(selected)
        )

    def test_hw04_retained_ram_evidence_requires_exact_restore_and_disconnect(self) -> None:
        """AT10 resume cannot replace retained RAM proof with a path-and-hash assertion."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            board = _board(root)
            row = {
                "status": "pass",
                "board_id": board["board_id"],
                "address": board["safe_ram"]["start"],
                "length": 4,
                "test_value": "0x00000001",
                "test_readback": "0x00000001",
                "initial_value": "0x12345678",
                "restored_readback": "0x12345678",
                "cleanup": {"attempted": True, "response": "Disconnected board."},
            }
            path = root / "ram-proof.jsonl"

            def declaration() -> dict[str, str]:
                return {
                    "path": str(path.resolve()),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }

            path.write_text(
                json.dumps({"status": "ram_proof_complete", "boards": [row]}) + "\n",
                encoding="utf-8",
            )
            accepted = runner._validate_retained_hw04_ram_evidence(declaration(), board, "retained")
            self.assertEqual(accepted["path"], str(path.resolve()))

            row["restored_readback"] = "0x87654321"
            path.write_text(
                json.dumps({"status": "ram_proof_complete", "boards": [row]}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(runner.FixtureError, "exact RAM write, restore"):
                runner._validate_retained_hw04_ram_evidence(declaration(), board, "retained")

    def test_hw04_retained_raw_evidence_requires_complete_restored_page(self) -> None:
        """A prior raw PASS is reusable only with exact full-page restoration evidence."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _hw04_execution_fixture(root, [], [])
            board = fixture["boards"]["stm32l476rtg"]
            restored = " ".join(["FF"] * 2048)
            rows = [
                {"id": identifier, "passed": True, "actual": {}}
                for identifier in runner._HW04_RAW_FLASH_ONLY_LIFECYCLE
            ]
            next(row for row in rows if row["id"] == "raw_restored_readback")["actual"] = {
                "payload": {"result": restored}
            }
            next(row for row in rows if row["id"] == "final_disconnect")["actual"] = {
                "text": "Disconnected board."
            }
            disconnected = {"cleanup": {"response": {"text": "Disconnected board."}}}
            transcript = {
                "case_results": {
                    "HW04": {
                        "boards": {
                            board["board_id"]: {
                                "phases": [
                                    {
                                        "name": "raw",
                                        "status": "pass",
                                        "steps": rows,
                                        "physical_sessions": [
                                            disconnected,
                                            disconnected,
                                            {"cleanup": {"attempted": False}},
                                        ],
                                    }
                                ]
                            }
                        }
                    }
                }
            }
            path = root / "raw-transcript.json"

            def declaration() -> dict[str, str]:
                return {
                    "path": str(path.resolve()),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }

            path.write_text(json.dumps(transcript), encoding="utf-8")
            accepted = runner._validate_retained_hw04_raw_evidence(declaration(), board, "retained")
            self.assertEqual(accepted["path"], str(path.resolve()))

            next(row for row in rows if row["id"] == "raw_restored_readback")["actual"] = {
                "payload": {"result": "00 " + " ".join(["FF"] * 2047)}
            }
            path.write_text(json.dumps(transcript), encoding="utf-8")
            with self.assertRaisesRegex(runner.FixtureError, "restoration digest"):
                runner._validate_retained_hw04_raw_evidence(declaration(), board, "retained")

    def test_hw04_sparse_safe_emergency_rechecks_all_restored_pages(self) -> None:
        """AT10: emergency recovery uses the same three whole-page verification legs."""

        runner = _runner()
        identifiers = runner._hw04_full_lifecycle(True)
        steps = [{"id": identifier} for identifier in identifiers]
        restore, readback = runner._hw04_emergency_steps("safe", steps)

        self.assertEqual(
            [step["id"] for step in restore],
            ["safe_restore_plan_guide", "safe_restore_plan_accept", "safe_restore"],
        )
        for index in range(3):
            self.assertIn(f"safe_restored_page_{index}", [step["id"] for step in readback])

    def test_hw04_integrity_rejects_wrong_range_and_raw_delta_after_rehash(self) -> None:
        """AT10: a new hash cannot bless bytes outside the exact lane rule."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _hw04_execution_fixture(Path(temporary), [], [])
            board = fixture["boards"]["stm32l476rtg"]
            raw = board["flash_fixture"]["raw"]
            raw["integrity"]["approved_ranges"] = [{"start": 0x080FF800, "end": 0x080FFFFF}]
            with self.assertRaisesRegex(runner.FixtureError, "approved_ranges"):
                runner._preflight_hw04_lane("stm32l476rtg", board, "raw")

            raw["integrity"]["approved_ranges"] = [{"start": 0x080FF800, "end": 0x08100000}]
            raw["integrity"]["delta"]["mask"] = "00" * 16
            with self.assertRaisesRegex(runner.FixtureError, "raw delta"):
                runner._preflight_hw04_lane("stm32l476rtg", board, "raw")

            safe = board["flash_fixture"]["safe"]
            safe["integrity"]["delta"]["after"] = "00000000"
            with self.assertRaisesRegex(runner.FixtureError, "safe delta"):
                runner._preflight_hw04_lane("stm32l476rtg", board, "safe")

    def test_hw04_nordic_safe_preflight_accepts_only_the_exact_sparse_three_page_delta(
        self,
    ) -> None:
        """AT10: Nordic safe flash preserves vectors and changes only final scratch."""

        runner = _runner()

        def intel_hex(segments: list[tuple[int, bytes]]) -> bytes:
            lines: list[str] = []
            upper: int | None = None
            for start, contents in segments:
                for offset in range(0, len(contents), 16):
                    address = start + offset
                    new_upper = address >> 16
                    if new_upper != upper:
                        body = bytes((2, 0, 0, 4)) + new_upper.to_bytes(2, "big")
                        lines.append(":" + (body + bytes(((-sum(body)) & 0xFF,))).hex().upper())
                        upper = new_upper
                    payload = contents[offset : offset + 16]
                    body = bytes((len(payload), (address >> 8) & 0xFF, address & 0xFF, 0)) + payload
                    lines.append(":" + (body + bytes(((-sum(body)) & 0xFF,))).hex().upper())
            lines.append(":00000001FF")
            return ("\n".join(lines) + "\n").encode("ascii")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ranges = ((0x00000000, 0x00001000), (0x00012000, 0x00013000), (0x000FF000, 0x00100000))
            restore_segments = [(start, b"\xff" * (end - start)) for start, end in ranges]
            test_segments = [*restore_segments[:-1], (ranges[-1][0], b"\xa5" * 4 + b"\xff" * 4092)]
            test_path = root / "nrf52840-safe-test.hex"
            restore_path = root / "nrf52840-safe-restore.hex"
            test_path.write_bytes(intel_hex(test_segments))
            restore_path.write_bytes(intel_hex(restore_segments))
            test_elf = test_path.with_suffix(".elf")
            restore_elf = restore_path.with_suffix(".elf")
            test_elf.write_bytes(b"\x7fELFnordic-safe-test")
            restore_elf.write_bytes(b"\x7fELFnordic-safe-restore")
            spans = [{"start": start, "end": end} for start, end in ranges]
            safe = {
                "artifact": str(test_path),
                "restore_artifact": str(restore_path),
                "address": 0x000FF000,
                "erase_footprint": spans,
                "post_flash_readback": {"result": "A5 A5 A5 A5"},
                "post_restore_readback": {"result": "FF FF FF FF"},
                "integrity": {
                    "artifact_sha256": hashlib.sha256(test_path.read_bytes()).hexdigest(),
                    "restore_artifact_sha256": hashlib.sha256(
                        restore_path.read_bytes()
                    ).hexdigest(),
                    "artifact_companion_sha256": hashlib.sha256(test_elf.read_bytes()).hexdigest(),
                    "restore_companion_sha256": hashlib.sha256(
                        restore_elf.read_bytes()
                    ).hexdigest(),
                    "approved_ranges": spans,
                    "delta": {
                        "kind": "replace",
                        "start": 0x000FF000,
                        "before": "FFFFFFFF",
                        "after": "A5A5A5A5",
                    },
                },
            }
            board = {"flash_fixture": {"raw": {}, "safe": safe}}

            receipt = runner._preflight_hw04_lane("nrf52840", board, "safe")

            self.assertEqual(receipt["approved_ranges"], spans)
            self.assertEqual(receipt["delta"]["start"], 0x000FF000)

            safe["integrity"]["approved_ranges"] = spans[:-1]
            with self.assertRaisesRegex(runner.FixtureError, "approved_ranges"):
                runner._preflight_hw04_lane("nrf52840", board, "safe")

    def test_hw04_integrity_rejects_address_or_footprint_outside_approved_ranges(self) -> None:
        """AT10: read-back and erasure declarations cannot drift from immutable bytes."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _hw04_execution_fixture(Path(temporary), [], [])
            board = fixture["boards"]["stm32l476rtg"]
            raw = board["flash_fixture"]["raw"]
            raw["address"] = 0x080FF810
            with self.assertRaisesRegex(runner.FixtureError, "address must match"):
                runner._preflight_hw04_lane("stm32l476rtg", board, "raw")

            raw["address"] = 0x080FF800
            raw["erase_footprint"] = [{"start": 0x080FF800, "end": 0x080FFFFF}]
            with self.assertRaisesRegex(runner.FixtureError, "erase_footprint must match"):
                runner._preflight_hw04_lane("stm32l476rtg", board, "raw")

    def test_hw04_lifecycle_order_rejects_restore_before_flash(self) -> None:
        """AT10: set equality cannot permit a restoration before its test write."""

        runner = _runner()
        raw_ids = [
            "initial_ram_read",
            "raw_ram_write",
            "raw_ram_readback",
            "raw_ram_restore",
            "ram_restored_readback",
            "initial_flash_readback",
            "raw_restore",
            "raw_flash",
            "raw_flash_readback",
            "raw_restored_readback",
            "final_disconnect",
        ]
        with self.assertRaisesRegex(runner.FixtureError, "HW04 raw_steps lifecycle"):
            runner._validate_hw04_lifecycle(
                "raw_steps", [{"id": identifier} for identifier in raw_ids]
            )

    def test_hw04_lifecycles_require_phase_bootstraps_before_mutations(self) -> None:
        """Fresh HW04 sessions must connect, and the safe route must validate, first."""

        runner = _runner()
        raw_ids = [
            "raw_connect",
            "initial_ram_read",
            "raw_ram_write",
            "raw_ram_readback",
            "raw_ram_restore",
            "ram_restored_readback",
            "initial_flash_readback",
            "raw_flash",
            "raw_flash_readback",
            "raw_restore",
            "raw_restored_readback",
            "final_disconnect",
        ]
        full_ids = [
            "full_assign",
            "full_connect",
            "full_load_validation_tool",
            "full_board_validate",
            "safe_flash_plan_guide",
            "safe_flash_plan_accept",
            "safe_flash",
            "safe_containment_refusal_plan_guide",
            "safe_containment_refusal_plan_accept",
            "safe_containment_refusal",
            "safe_flash_readback_plan_guide",
            "safe_flash_readback_plan_accept",
            "safe_flash_readback",
            "safe_restore_plan_guide",
            "safe_restore_plan_accept",
            "safe_restore",
            "safe_restored_readback_plan_guide",
            "safe_restored_readback_plan_accept",
            "safe_restored_readback",
            "final_disconnect",
        ]

        runner._validate_hw04_lifecycle("raw_steps", [{"id": identifier} for identifier in raw_ids])
        runner._validate_hw04_lifecycle(
            "full_steps", [{"id": identifier} for identifier in full_ids]
        )
        with self.assertRaisesRegex(runner.FixtureError, "HW04 raw_steps lifecycle"):
            runner._validate_hw04_lifecycle(
                "raw_steps", [{"id": identifier} for identifier in raw_ids[1:]]
            )
        with self.assertRaisesRegex(runner.FixtureError, "HW04 full_steps lifecycle"):
            runner._validate_hw04_lifecycle(
                "full_steps", [{"id": identifier} for identifier in full_ids[1:]]
            )
        with self.assertRaisesRegex(runner.FixtureError, "HW04 full_steps lifecycle"):
            runner._validate_hw04_lifecycle(
                "full_steps",
                [{"id": identifier} for identifier in [full_ids[1], full_ids[0], *full_ids[2:]]],
            )

    def test_connection_id_requires_provider_and_opaque_components(self) -> None:
        """Assignment tokens must be canonical server IDs, not merely prefix-shaped text."""

        runner = _runner()
        for invalid in ("probeid::opaque", "probeid:provider:", 7):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(runner.FixtureError, "connection_id"):
                    runner._provider_qualified_connection_id(
                        invalid, "boards.stm32l476rtg.connection_id"
                    )

    def test_legacy_non_hw04_board_allows_no_connection_id(self) -> None:
        """The new full-profile binding remains optional for legacy non-HW04 fixtures."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            board = _board(Path(temporary))
            board.pop("connection_id")
            normalized = runner._common_board("legacy-board", board)

        self.assertNotIn("connection_id", normalized)

    def test_hw04_case_fixture_accepts_bootstrapped_id_sets_before_contract_binding(self) -> None:
        """The schema-level HW04 ID sets include every required phase bootstrap."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            board = _board(Path(temporary))
            board.update(
                {
                    "board_id": "stm-board",
                    "probe_uid": "stm-board-probe",
                    "target": "stm32l476rgtx",
                    "expected_tier": "setup-full",
                }
            )
            raw_steps = [
                {
                    "id": identifier,
                    "tool": "placeholder",
                    "arguments": {},
                    "expect": {"text_contains": ["placeholder"]},
                }
                for identifier in runner._HW04_RAW_LIFECYCLE
            ]
            full_steps = [
                {
                    "id": identifier,
                    "tool": "placeholder",
                    "arguments": {},
                    "expect": {"text_contains": ["placeholder"]},
                }
                for identifier in runner._HW04_FULL_LIFECYCLE
            ]
            with (
                patch.object(runner, "_validate_step_contract") as validate_contract,
                patch.object(runner, "_validate_plan_lifecycles"),
            ):
                validated = runner._case_board(
                    "HW04",
                    "stm32l476rtg",
                    {"status": "ready", "raw_steps": raw_steps, "full_steps": full_steps},
                    {"stm32l476rtg": board},
                )
        self.assertEqual(
            [step["id"] for step in validated["raw_steps"]], list(runner._HW04_RAW_LIFECYCLE)
        )
        self.assertEqual(
            [step["id"] for step in validated["full_steps"]],
            list(runner._HW04_FULL_LIFECYCLE),
        )
        self.assertEqual(
            validate_contract.call_count,
            len(runner._HW04_RAW_LIFECYCLE) + len(runner._HW04_FULL_LIFECYCLE),
        )

    def test_hw04_connect_refusal_prevents_later_raw_flash_and_disconnects(self) -> None:
        """A fresh raw phase never dispatches a mutation after its bootstrap fails."""

        runner = _runner()
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _hw04_execution_fixture(
                Path(temporary),
                _hw04_complete_raw_steps(),
                [],
            )

            class Session:
                async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                    del arguments
                    calls.append(tool)
                    if tool == "connect":
                        return [SimpleNamespace(text=json.dumps({"status": "refused"}))]
                    if tool == "disconnect":
                        return [SimpleNamespace(text="disconnected")]
                    raise AssertionError(f"unexpected mutation after failed connect: {tool}")

            class SessionContext:
                async def __aenter__(self) -> Session:
                    return Session()

                async def __aexit__(self, *unused: object) -> None:
                    del unused

            transcript = asyncio.run(
                runner.execute_with_session_factory(
                    fixture,
                    ("HW04",),
                    lambda _root: SessionContext(),
                    pause=lambda _: asyncio.sleep(0),
                    allow_write_flash=True,
                )
            )

        phase = transcript["case_results"]["HW04"]["boards"]["stm32l476rtg"]["phases"][0]
        self.assertIn("raw_connect.text missing", phase["failure"])
        self.assertEqual(calls, ["connect", "disconnect"])

    def test_hw04_validation_refusal_prevents_safe_flash_dispatch(self) -> None:
        """The safe phase stops before planning or flashing when validation is not passed."""

        runner = _runner()
        calls: list[str] = []
        board = {
            "board_id": "stm-board",
            "probe_uid": "stm-board-probe",
            "connection_id": "probeid:stlink:stm-board-connection",
            "target": "stm32l476rgtx",
        }
        steps = [
            {
                "id": "full_assign",
                "tool": "setup_overview",
                "arguments": {
                    "board_names": ["stm-board"],
                    "connection_assignments": {"stm-board": "probeid:stlink:stm-board-connection"},
                },
                "expect": {
                    "payload": {"status": "setup_routes_ready"},
                    "text_contains": ["stm-board"],
                },
            },
            {
                "id": "full_connect",
                "tool": "connect",
                "arguments": {"board_id": "stm-board"},
                "expect": {"text_contains": ["Connected to board"]},
            },
            {
                "id": "full_load_validation_tool",
                "tool": "load_setup_tool",
                "arguments": {"board_id": "stm-board", "tool_name": "board_validate"},
                "expect": {"payload": {"status": "setup_tool_loaded"}},
                "capture": "full_validation_loader",
            },
            {
                "id": "full_board_validate",
                "tool": "board_validate",
                "arguments": {
                    "board_id": "stm-board",
                    "probe_id": "$capture.full_validation_loader.next_call.arguments.probe_id",
                },
                "expect": {"payload": {"status": "validation_passed"}},
            },
            {
                "id": "safe_flash",
                "tool": "action_batch",
                "arguments": {"board_id": "stm-board", "actions": []},
                "expect": {"payload": {"status": "batch_completed"}},
            },
        ]

        class Session:
            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                del arguments
                calls.append(tool)
                if tool == "setup_overview":
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {"status": "setup_routes_ready", "board_id": "stm-board"}
                            )
                        )
                    ]
                if tool == "connect":
                    return [SimpleNamespace(text="Connected to board 'stm-board'.")]
                if tool == "load_setup_tool":
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "setup_tool_loaded",
                                    "next_call": {"arguments": {"probe_id": "validated-probe"}},
                                }
                            )
                        )
                    ]
                if tool == "board_validate":
                    return [SimpleNamespace(text=json.dumps({"status": "refused"}))]
                raise AssertionError(f"safe flash ran before validation passed: {tool}")

        with self.assertRaisesRegex(runner.CheckFailed, "full_board_validate.payload.status"):
            asyncio.run(
                runner.run_steps(
                    Session(), Path("."), board, steps, pause=lambda _: asyncio.sleep(0)
                )
            )
        self.assertEqual(calls, ["setup_overview", "connect", "load_setup_tool", "board_validate"])

    def test_hw04_assigned_loader_without_token_blocks_before_safe_flash(self) -> None:
        """A successful full assignment cannot bypass a loader missing its current token."""

        runner = _runner()
        calls: list[str] = []
        board = {
            "board_id": "stm-board",
            "connection_id": "probeid:stlink:stm-board-connection",
        }
        steps = [
            {
                "id": "full_assign",
                "tool": "setup_overview",
                "arguments": {
                    "board_names": ["stm-board"],
                    "connection_assignments": {"stm-board": "probeid:stlink:stm-board-connection"},
                },
                "expect": {
                    "payload": {"status": "setup_routes_ready"},
                    "text_contains": ["stm-board"],
                },
            },
            {
                "id": "full_connect",
                "tool": "connect",
                "arguments": {"board_id": "stm-board"},
                "expect": {"text_contains": ["Connected to board"]},
            },
            {
                "id": "full_load_validation_tool",
                "tool": "load_setup_tool",
                "arguments": {"board_id": "stm-board", "tool_name": "board_validate"},
                "expect": {"payload": {"status": "setup_tool_loaded"}},
                "capture": "full_validation_loader",
            },
            {
                "id": "full_board_validate",
                "tool": "board_validate",
                "arguments": {
                    "board_id": "stm-board",
                    "probe_id": "$capture.full_validation_loader.next_call.arguments.probe_id",
                },
                "expect": {"payload": {"status": "validation_passed"}},
            },
            {
                "id": "safe_flash",
                "tool": "action_batch",
                "arguments": {"board_id": "stm-board", "actions": []},
                "expect": {"payload": {"status": "batch_completed"}},
            },
        ]

        class Session:
            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                calls.append(tool)
                if tool == "setup_overview":
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {"status": "setup_routes_ready", "board_id": "stm-board"}
                            )
                        )
                    ]
                if tool == "connect":
                    return [SimpleNamespace(text="Connected to board 'stm-board'.")]
                if tool == "load_setup_tool":
                    return [SimpleNamespace(text=json.dumps({"status": "setup_tool_loaded"}))]
                raise AssertionError(
                    f"unsafe call after missing loader token: {tool} {arguments!r}"
                )

        with self.assertRaisesRegex(runner.CheckFailed, "unresolved fixture reference"):
            asyncio.run(
                runner.run_steps(
                    Session(), Path("."), board, steps, pause=lambda _: asyncio.sleep(0)
                )
            )
        self.assertEqual(calls, ["setup_overview", "connect", "load_setup_tool"])

    def test_hw04_assignment_response_mismatch_blocks_before_full_connect(self) -> None:
        """A server assignment refusal cannot fall through to configured-profile connect."""

        runner = _runner()
        calls: list[str] = []
        assign = {
            "id": "full_assign",
            "tool": "setup_overview",
            "arguments": {
                "board_names": ["stm-board"],
                "connection_assignments": {"stm-board": "probeid:stlink:stm-board-connection"},
            },
            "expect": {
                "payload": {"status": "setup_routes_ready"},
                "text_contains": ["stm-board"],
            },
        }
        connect = {
            "id": "full_connect",
            "tool": "connect",
            "arguments": {"board_id": "stm-board"},
            "expect": {"text_contains": ["Connected to board"]},
        }

        class Session:
            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                calls.append(tool)
                if tool == "setup_overview":
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {"status": "setup_assignment_required", "board_id": "stm-board"}
                            )
                        )
                    ]
                raise AssertionError(f"unsafe post-assignment-mismatch call: {tool} {arguments!r}")

        with self.assertRaisesRegex(runner.CheckFailed, "full_assign.payload.status"):
            asyncio.run(
                runner.run_steps(
                    Session(),
                    Path("."),
                    {"board_id": "stm-board"},
                    [assign, connect],
                    pause=lambda _: asyncio.sleep(0),
                )
            )
        self.assertEqual(calls, ["setup_overview"])

    def test_hw04_bootstrap_rejects_an_alternate_config_or_wrong_validation_loader(self) -> None:
        """Raw is exact-bound; full bootstrap must use its committed profile route."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            board = _board(Path(temporary))
            board.update(
                {
                    "board_id": "stm-board",
                    "probe_uid": "stm-board-probe",
                    "connection_id": "probeid:stlink:stm-board-connection",
                    "target": "stm32l476rgtx",
                    "serial_binding": "COM-fake",
                    "expected_tier": "setup-full",
                }
            )
            with self.assertRaisesRegex(runner.FixtureError, "exact STM binding"):
                runner._validate_step_contract(
                    "HW04",
                    {
                        "id": "raw_connect",
                        "tool": "connect",
                        "arguments": {
                            "board_id": "stm-board",
                            "probe_uid": "stm-board-probe",
                            "target": "stm32l476rgtx",
                            "board_config": "other-profile.yaml",
                        },
                        "expect": {"text_contains": ["stm-board-probe"]},
                    },
                    board,
                    [],
                    "raw_connect",
                )
            runner._validate_step_contract(
                "HW04",
                {
                    "id": "full_assign",
                    "tool": "setup_overview",
                    "arguments": {
                        "board_names": ["stm-board"],
                        "connection_assignments": {
                            "stm-board": "probeid:stlink:stm-board-connection"
                        },
                    },
                    "expect": {
                        "payload": {"status": "setup_routes_ready"},
                        "text_contains": ["stm-board"],
                    },
                },
                board,
                [],
                "full_assign",
            )
            with self.assertRaisesRegex(runner.FixtureError, "exact fixture connection ID"):
                runner._validate_step_contract(
                    "HW04",
                    {
                        "id": "full_assign",
                        "tool": "setup_overview",
                        "arguments": {
                            "board_names": ["stm-board"],
                            "connection_assignments": {"stm-board": "stm-board-probe"},
                        },
                        "expect": {
                            "payload": {"status": "setup_routes_ready"},
                            "text_contains": ["stm-board"],
                        },
                    },
                    board,
                    [],
                    "full_assign",
                )
            runner._validate_step_contract(
                "HW04",
                {
                    "id": "full_connect",
                    "tool": "connect",
                    "arguments": {"board_id": "stm-board"},
                    "expect": {"text_contains": ["stm-board-probe"]},
                },
                board,
                [],
                "full_connect",
            )
            for forbidden_override in (
                {"probe_uid": "stm-board-probe"},
                {"target": "stm32l476rgtx"},
                {"probe_uid": "stm-board-probe", "target": "stm32l476rgtx"},
            ):
                with self.subTest(forbidden_override=forbidden_override):
                    with self.assertRaisesRegex(
                        runner.FixtureError, "configured full-profile route"
                    ):
                        runner._validate_step_contract(
                            "HW04",
                            {
                                "id": "full_connect",
                                "tool": "connect",
                                "arguments": {"board_id": "stm-board", **forbidden_override},
                                "expect": {"text_contains": ["stm-board"]},
                            },
                            board,
                            [],
                            "full_connect",
                        )
            with self.assertRaisesRegex(runner.FixtureError, "must load 'board_validate'"):
                runner._validate_step_contract(
                    "HW04",
                    {
                        "id": "full_load_validation_tool",
                        "tool": "load_setup_tool",
                        "arguments": {
                            "board_id": "stm-board",
                            "tool_name": "board_setup-plan",
                        },
                        "expect": {"payload": {"status": "setup_tool_loaded"}},
                        "capture": "full_validation_loader",
                    },
                    board,
                    [],
                    "full_load_validation_tool",
                )

    def test_hw04_full_connect_wrong_route_refusal_stops_before_safe_flash(self) -> None:
        """A product tier/wrong-route error cannot fall through to a safe mutation."""

        runner = _runner()
        calls: list[str] = []
        steps = [
            {
                "id": "full_assign",
                "tool": "setup_overview",
                "arguments": {
                    "board_names": ["stm-board"],
                    "connection_assignments": {"stm-board": "probeid:stlink:stm-board-connection"},
                },
                "expect": {
                    "payload": {"status": "setup_routes_ready"},
                    "text_contains": ["stm-board"],
                },
            },
            {
                "id": "full_connect",
                "tool": "connect",
                "arguments": {"board_id": "stm-board"},
                "expect": {"text_contains": ["Connected to board"]},
            },
            {
                "id": "safe_flash",
                "tool": "action_batch",
                "arguments": {"board_id": "stm-board", "actions": []},
                "expect": {"payload": {"status": "batch_completed"}},
            },
        ]

        class Session:
            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                calls.append(tool)
                if tool == "setup_overview":
                    if arguments != {
                        "board_names": ["stm-board"],
                        "connection_assignments": {
                            "stm-board": "probeid:stlink:stm-board-connection"
                        },
                    }:
                        raise AssertionError(f"unexpected full assignment: {arguments!r}")
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {"status": "setup_routes_ready", "board_id": "stm-board"}
                            )
                        )
                    ]
                if tool == "connect":
                    if arguments != {"board_id": "stm-board"}:
                        raise AssertionError(f"unexpected full-route binding: {arguments!r}")
                    return SimpleNamespace(
                        isError=True,
                        content=[
                            SimpleNamespace(
                                text=json.dumps(
                                    {
                                        "status": "refused",
                                        "code": "tier/wrong-route",
                                        "operation": "connect",
                                    }
                                )
                            )
                        ],
                    )
                raise AssertionError(f"unsafe post-refusal call: {tool} {arguments!r}")

        with self.assertRaisesRegex(
            runner.CheckFailed, "full_connect.mcp_error expected False, got True"
        ):
            asyncio.run(
                runner.run_steps(
                    Session(),
                    Path("."),
                    {
                        "board_id": "stm-board",
                        "connection_id": "probeid:stlink:stm-board-connection",
                    },
                    steps,
                    pause=lambda _: asyncio.sleep(0),
                )
            )
        self.assertEqual(calls, ["setup_overview", "connect"])

    def test_hw04_fresh_safe_recovery_revalidates_before_exact_restore(self) -> None:
        """An uncertain safe flash restores only after the fresh full-route bootstrap."""

        runner = _runner()
        calls: list[str] = []
        board = {
            "board_id": "stm-board",
            "probe_uid": "stm-board-probe",
            "connection_id": "probeid:stlink:stm-board-connection",
            "target": "stm32l476rgtx",
        }
        restore_fallback = {"board_id": "stm-board", "actions": [{"kind": "restore"}]}
        readback_fallback = {"board_id": "stm-board", "actions": [{"kind": "readback"}]}
        steps = [
            {
                "id": "full_assign",
                "tool": "setup_overview",
                "arguments": {
                    "board_names": ["stm-board"],
                    "connection_assignments": {"stm-board": "probeid:stlink:stm-board-connection"},
                },
                "expect": {
                    "payload": {"status": "setup_routes_ready"},
                    "text_contains": ["stm-board"],
                },
            },
            {
                "id": "full_connect",
                "tool": "connect",
                "arguments": {"board_id": "stm-board"},
                "expect": {"text_contains": ["stm-board-probe"]},
            },
            {
                "id": "full_load_validation_tool",
                "tool": "load_setup_tool",
                "arguments": {"board_id": "stm-board", "tool_name": "board_validate"},
                "expect": {"payload": {"status": "setup_tool_loaded"}},
                "capture": "full_validation_loader",
            },
            {
                "id": "full_board_validate",
                "tool": "board_validate",
                "arguments": {
                    "board_id": "stm-board",
                    "probe_id": "$capture.full_validation_loader.next_call.arguments.probe_id",
                },
                "expect": {"payload": {"status": "validation_passed", "code": "validation/passed"}},
            },
            {
                "id": "safe_restore_plan_guide",
                "tool": "flash_application-plan",
                "arguments": {"board_id": "stm-board"},
                "expect": {"text_contains": ["guide"]},
            },
            {
                "id": "safe_restore_plan_accept",
                "tool": "flash_application-plan",
                "arguments": {"board_id": "stm-board", "accept": True},
                "expect": {"payload": {"status": "plan_accepted"}},
                "capture": "safe_restore_plan",
            },
            {
                "id": "safe_restore",
                "tool": "action_batch",
                "arguments": restore_fallback,
                "fallback_from": "safe_restore_plan",
                "expect": {
                    "payload": {"status": "batch_completed"},
                    "child_payload": {"status": "ok", "operation": "flash_application"},
                },
            },
            {
                "id": "safe_restored_readback_plan_guide",
                "tool": "read_memory_address-plan",
                "arguments": {"board_id": "stm-board"},
                "expect": {"text_contains": ["guide"]},
            },
            {
                "id": "safe_restored_readback_plan_accept",
                "tool": "read_memory_address-plan",
                "arguments": {"board_id": "stm-board", "accept": True},
                "expect": {"payload": {"status": "plan_accepted"}},
                "capture": "safe_restored_readback_plan",
            },
            {
                "id": "safe_restored_readback",
                "tool": "action_batch",
                "arguments": readback_fallback,
                "fallback_from": "safe_restored_readback_plan",
                "expect": {
                    "payload": {"status": "batch_completed"},
                    "child_payload": {
                        "status": "ok",
                        "operation": "read_memory_address",
                        "result": "FF FF FF FF",
                    },
                },
            },
        ]

        def batch(child: dict[str, object]) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    text=json.dumps(
                        {
                            "status": "batch_completed",
                            "completed": [
                                {"tool_name": child["operation"], "result": json.dumps(child)}
                            ],
                        }
                    )
                )
            ]

        class Session:
            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                calls.append(tool)
                if tool == "setup_overview":
                    if arguments != {
                        "board_names": ["stm-board"],
                        "connection_assignments": {
                            "stm-board": "probeid:stlink:stm-board-connection"
                        },
                    }:
                        raise AssertionError(f"unexpected fresh assignment: {arguments!r}")
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {"status": "setup_routes_ready", "board_id": "stm-board"}
                            )
                        )
                    ]
                if tool == "connect":
                    if arguments != {"board_id": "stm-board"}:
                        raise AssertionError(f"unexpected fresh binding: {arguments!r}")
                    return [SimpleNamespace(text="Connected via stm-board-probe.")]
                if tool == "load_setup_tool":
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "setup_tool_loaded",
                                    "next_call": {"arguments": {"probe_id": "fresh-probe"}},
                                }
                            )
                        )
                    ]
                if tool == "board_validate":
                    if arguments["probe_id"] != "fresh-probe":
                        raise AssertionError(f"unexpected fresh probe: {arguments!r}")
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {"status": "validation_passed", "code": "validation/passed"}
                            )
                        )
                    ]
                if tool in {"flash_application-plan", "read_memory_address-plan"}:
                    if arguments.get("accept") is not True:
                        return [SimpleNamespace(text="guide")]
                    fallback = (
                        restore_fallback if tool == "flash_application-plan" else readback_fallback
                    )
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "plan_accepted",
                                    "stable_client_fallback": {
                                        "tool_name": "action_batch",
                                        "arguments": fallback,
                                    },
                                }
                            )
                        )
                    ]
                if tool == "action_batch" and arguments == restore_fallback:
                    return batch({"status": "ok", "operation": "flash_application"})
                if tool == "action_batch" and arguments == readback_fallback:
                    return batch(
                        {
                            "status": "ok",
                            "operation": "read_memory_address",
                            "result": "FF FF FF FF",
                        }
                    )
                if tool == "disconnect":
                    return [SimpleNamespace(text="disconnected")]
                raise AssertionError(f"unexpected safe-recovery call: {tool} {arguments!r}")

        class SessionContext:
            async def __aenter__(self) -> Session:
                return Session()

            async def __aexit__(self, *unused: object) -> None:
                del unused

        result = asyncio.run(
            runner._fresh_emergency_hw04_restoration(
                lambda _root: SessionContext(),
                Path("."),
                board,
                steps,
                lane="safe",
                trigger="safe_flash",
                pause=lambda _: asyncio.sleep(0),
            )
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["restore"]["status"], "pass")
        self.assertEqual(result["readback"]["status"], "pass")
        self.assertEqual(
            [record["name"] for record in result["physical_sessions"]],
            ["emergency_restore", "emergency_restore_verification"],
        )
        self.assertEqual(
            calls,
            [
                "setup_overview",
                "connect",
                "load_setup_tool",
                "board_validate",
                "flash_application-plan",
                "flash_application-plan",
                "action_batch",
                "disconnect",
                "setup_overview",
                "connect",
                "load_setup_tool",
                "board_validate",
                "read_memory_address-plan",
                "read_memory_address-plan",
                "action_batch",
                "disconnect",
            ],
        )

    def test_hw04_safe_flash_child_mismatch_uses_fresh_restore_and_readback_plans(self) -> None:
        """A safe test-response mismatch restores and verifies through two fresh full routes."""

        runner = _runner()
        case = self
        steps = _hw04_complete_safe_steps()
        calls: dict[str, list[str]] = {"mutation": [], "restore": [], "verify": []}
        fallbacks = {
            "mutation": [{"board_id": "stm-board", "actions": [{"kind": "safe-test"}]}],
            "restore": [{"board_id": "stm-board", "actions": [{"kind": "safe-restore"}]}],
            "verify": [
                {
                    "board_id": "stm-board",
                    "actions": [{"kind": "safe-restored-readback"}],
                }
            ],
        }

        def batch(child: dict[str, object]) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    text=json.dumps(
                        {
                            "status": "batch_completed",
                            "completed": [
                                {"tool_name": child["operation"], "result": json.dumps(child)}
                            ],
                        }
                    )
                )
            ]

        class Session:
            def __init__(self, name: str) -> None:
                self.name = name

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                calls[self.name].append(tool)
                if tool == "setup_overview":
                    return [SimpleNamespace(text=json.dumps({"status": "setup_routes_ready"}))]
                if tool == "connect":
                    return [SimpleNamespace(text="Connected via stm-board-probe.")]
                if tool == "load_setup_tool":
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "setup_tool_loaded",
                                    "next_call": {"arguments": {"probe_id": "current-token"}},
                                }
                            )
                        )
                    ]
                if tool == "board_validate":
                    return [SimpleNamespace(text=json.dumps({"status": "validation_passed"}))]
                if tool.endswith("-plan"):
                    if arguments.get("accept") is not True:
                        return [SimpleNamespace(text="guide")]
                    return [
                        SimpleNamespace(
                            text=json.dumps(
                                {
                                    "status": "plan_accepted",
                                    "stable_client_fallback": {
                                        "tool_name": "action_batch",
                                        "arguments": fallbacks[self.name].pop(0),
                                    },
                                }
                            )
                        )
                    ]
                if tool == "action_batch":
                    kind = arguments["actions"][0]["kind"]
                    if self.name == "mutation":
                        return batch({"status": "unexpected", "operation": "flash_application"})
                    if self.name == "restore":
                        case.assertEqual(kind, "safe-restore")
                        return batch({"status": "ok", "operation": "flash_application"})
                    case.assertEqual(kind, "safe-restored-readback")
                    return batch(
                        {
                            "status": "ok",
                            "operation": "read_memory_address",
                            "result": "FF FF FF FF",
                        }
                    )
                if tool == "disconnect":
                    return [SimpleNamespace(text="disconnected")]
                raise AssertionError(f"unexpected {self.name} call: {tool} {arguments!r}")

        class SessionContext:
            def __init__(self, name: str) -> None:
                self.name = name

            async def __aenter__(self) -> Session:
                return Session(self.name)

            async def __aexit__(self, *unused: object) -> None:
                del unused

        with tempfile.TemporaryDirectory() as temporary:
            contexts = [
                SessionContext("mutation"),
                SessionContext("restore"),
                SessionContext("verify"),
            ]
            phase = asyncio.run(
                runner._execute_hw04_lane(
                    lambda _root: contexts.pop(0),
                    Path(temporary),
                    {
                        "board_id": "stm-board",
                        "probe_uid": "stm-board-probe",
                        "connection_id": "probeid:test:stm-board-connection",
                        "target": "stm32l476rgtx",
                    },
                    steps,
                    lane="safe",
                    phase_name="full",
                    pause=lambda _: asyncio.sleep(0),
                )
            )

        self.assertIn("safe_flash.child_payload.status", phase["failure"])
        emergency = phase["emergency_restoration"]
        self.assertEqual(emergency["lane"], "safe")
        self.assertEqual(emergency["restore"]["status"], "pass")
        self.assertEqual(emergency["readback"]["status"], "pass")
        self.assertEqual(
            [record["name"] for record in emergency["physical_sessions"]],
            ["emergency_restore", "emergency_restore_verification"],
        )
        self.assertEqual(calls["mutation"].count("action_batch"), 1)
        self.assertEqual(
            calls["restore"][:4], ["setup_overview", "connect", "load_setup_tool", "board_validate"]
        )
        self.assertEqual(
            calls["verify"][:4], ["setup_overview", "connect", "load_setup_tool", "board_validate"]
        )

    def test_hw04_completed_final_disconnect_is_not_repeated_by_cleanup(self) -> None:
        """A successful HW04 phase finishes at its declared restoration-readback disconnect."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _hw04_execution_fixture(
                Path(temporary),
                [],
                [
                    {
                        "id": "full_assign",
                        "tool": "setup_overview",
                        "arguments": {
                            "board_names": ["stm-board"],
                            "connection_assignments": {
                                "stm-board": "probeid:test:stm-board-connection"
                            },
                        },
                        "expect": {
                            "payload": {"status": "setup_routes_ready"},
                            "text_contains": ["stm-board"],
                        },
                    },
                    {
                        "id": "full_connect",
                        "tool": "connect",
                        "arguments": {"board_id": "stm-board"},
                        "expect": {"text_contains": ["stm-board-probe"]},
                    },
                    {
                        "id": "full_load_validation_tool",
                        "tool": "load_setup_tool",
                        "arguments": {"board_id": "stm-board", "tool_name": "board_validate"},
                        "expect": {"payload": {"status": "setup_tool_loaded"}},
                        "capture": "full_validation_loader",
                    },
                    {
                        "id": "full_board_validate",
                        "tool": "board_validate",
                        "arguments": {
                            "board_id": "stm-board",
                            "probe_id": "$capture.full_validation_loader.next_call.arguments.probe_id",
                        },
                        "expect": {
                            "payload": {
                                "status": "validation_passed",
                                "code": "validation/passed",
                            }
                        },
                    },
                    {
                        "id": "final_disconnect",
                        "tool": "disconnect",
                        "arguments": {"board_id": "stm-board"},
                        "expect": {"text_contains": ["disconnected"]},
                    },
                ],
            )
            raw_calls: list[str] = []
            full_calls: list[str] = []

            class Session:
                def __init__(self, calls: list[str]) -> None:
                    self.calls = calls

                async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                    del arguments
                    self.calls.append(tool)
                    if tool == "setup_overview":
                        return [
                            SimpleNamespace(
                                text=json.dumps(
                                    {"status": "setup_routes_ready", "board_id": "stm-board"}
                                )
                            )
                        ]
                    if tool == "connect":
                        return [SimpleNamespace(text="Connected via stm-board-probe.")]
                    if tool == "load_setup_tool":
                        return [
                            SimpleNamespace(
                                text=json.dumps(
                                    {
                                        "status": "setup_tool_loaded",
                                        "next_call": {"arguments": {"probe_id": "full-probe"}},
                                    }
                                )
                            )
                        ]
                    if tool == "board_validate":
                        return [
                            SimpleNamespace(
                                text=json.dumps(
                                    {"status": "validation_passed", "code": "validation/passed"}
                                )
                            )
                        ]
                    if tool == "disconnect":
                        return [SimpleNamespace(text="disconnected")]
                    raise AssertionError(f"unexpected bootstrap test call: {tool}")

            class SessionContext:
                def __init__(self, calls: list[str]) -> None:
                    self.session = Session(calls)

                async def __aenter__(self) -> Session:
                    return self.session

                async def __aexit__(self, *unused: object) -> None:
                    del unused

            contexts = [SessionContext(raw_calls), SessionContext(full_calls)]
            transcript = asyncio.run(
                runner.execute_with_session_factory(
                    fixture,
                    ("HW04",),
                    lambda _root: contexts.pop(0),
                    pause=lambda _: asyncio.sleep(0),
                    allow_write_flash=True,
                )
            )

        phase = next(
            item
            for item in transcript["case_results"]["HW04"]["boards"]["stm32l476rtg"]["phases"]
            if item["name"] == "full"
        )
        self.assertEqual(
            raw_calls,
            ["setup_overview", "connect", "load_setup_tool", "board_validate", "disconnect"],
        )
        self.assertEqual(full_calls, [])
        self.assertFalse(phase["cleanup"]["attempted"])
        self.assertIn("final_disconnect", phase["cleanup"]["reason"])

    def test_hw04_emergency_restore_failure_is_recorded_without_masking_test_failure(self) -> None:
        """AT10: an emergency restore failure is distinct from the original flash mismatch."""

        runner = _runner()
        calls: dict[str, list[str]] = {"mutation": [], "restore": [], "verify": []}

        class Session:
            def __init__(self, name: str) -> None:
                self.name = name

            async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                calls[self.name].append(tool)
                if tool == "connect":
                    return [SimpleNamespace(text="Connected via stm-board-probe.")]
                if tool == "disconnect":
                    return [SimpleNamespace(text="disconnected")]
                if self.name == "mutation" and tool == "flash_raw":
                    return [SimpleNamespace(text=json.dumps({"status": "refused"}))]
                if self.name == "restore" and tool == "flash_raw":
                    return [SimpleNamespace(text=json.dumps({"status": "refused"}))]
                if tool == "read_memory_raw":
                    return [
                        SimpleNamespace(text=json.dumps({"status": "ok", "result": "FF FF FF FF"}))
                    ]
                if tool == "write_memory_raw":
                    return [SimpleNamespace(text=json.dumps({"status": "ok"}))]
                raise AssertionError(f"unexpected {self.name} call: {tool} {arguments!r}")

        class SessionContext:
            def __init__(self, name: str) -> None:
                self.name = name

            async def __aenter__(self) -> Session:
                return Session(self.name)

            async def __aexit__(self, *unused: object) -> None:
                del unused

        with tempfile.TemporaryDirectory() as temporary:
            contexts = [
                SessionContext("mutation"),
                SessionContext("restore"),
                SessionContext("verify"),
            ]
            transcript = asyncio.run(
                runner.execute_with_session_factory(
                    _hw04_execution_fixture(Path(temporary), _hw04_complete_raw_steps(), []),
                    ("HW04",),
                    lambda _root: contexts.pop(0),
                    pause=lambda _: asyncio.sleep(0),
                    allow_write_flash=True,
                )
            )

        phase = transcript["case_results"]["HW04"]["boards"]["stm32l476rtg"]["phases"][0]
        self.assertIn("raw_flash.payload.status", phase["failure"])
        self.assertEqual(phase["emergency_restoration"]["restore"]["status"], "failed")
        self.assertEqual(phase["emergency_restoration"]["readback"]["status"], "pass")
        self.assertIn(
            "raw_restore.payload.status", phase["emergency_restoration"]["restore"]["failure"]
        )
        self.assertEqual(calls["restore"], ["connect", "flash_raw", "disconnect"])
        self.assertEqual(calls["verify"], ["connect", "read_memory_raw", "disconnect"])
        self.assertEqual(transcript["case_results"]["HW04"]["status"], "failed")

    def test_hw04_mutating_flash_exception_is_conservatively_restored(self) -> None:
        """AT10: an ambiguous flash transport failure restores only through a fresh session."""

        runner = _runner()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _hw04_execution_fixture(
                Path(temporary),
                _hw04_complete_raw_steps(),
                [],
            )

            class OriginalSession:
                def __init__(self) -> None:
                    self.calls: list[str] = []

                async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                    self.calls.append(tool)
                    if tool == "connect":
                        return [SimpleNamespace(text="Connected via stm-board-probe.")]
                    if tool in {"read_memory_raw", "write_memory_raw"}:
                        return [
                            SimpleNamespace(
                                text=json.dumps({"status": "ok", "result": "FF FF FF FF"})
                            )
                        ]
                    if tool == "flash_raw":
                        raise RuntimeError("transport ended after dispatch")
                    raise AssertionError(f"failed session was reused for {tool}")

            class RecoverySession:
                def __init__(self, name: str) -> None:
                    self.name = name
                    self.calls: list[str] = []

                async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
                    self.calls.append(tool)
                    if tool == "connect":
                        if arguments != {
                            "board_id": "stm-board",
                            "probe_uid": "stm-board-probe",
                            "target": "stm32l476rgtx",
                        }:
                            raise AssertionError(f"unexpected recovery binding: {arguments!r}")
                        return [SimpleNamespace(text="Connected via stm-board-probe.")]
                    if self.name == "restore" and tool == "flash_raw":
                        return [SimpleNamespace(text=json.dumps({"status": "ok"}))]
                    if self.name == "verify" and tool == "read_memory_raw":
                        return [
                            SimpleNamespace(
                                text=json.dumps({"status": "ok", "result": "FF FF FF FF"})
                            )
                        ]
                    if tool == "disconnect":
                        return [SimpleNamespace(text="disconnected")]
                    raise AssertionError(f"unexpected recovery MCP call: {tool}")

            class SessionContext:
                def __init__(self, session: object, *, fail_exit: bool = False) -> None:
                    self.session = session
                    self.fail_exit = fail_exit
                    self.exited = False

                async def __aenter__(self) -> object:
                    return self.session

                async def __aexit__(self, *unused: object) -> None:
                    del unused
                    self.exited = True
                    if self.fail_exit:
                        raise RuntimeError("failed session context close")

            original, restore, verify = (
                OriginalSession(),
                RecoverySession("restore"),
                RecoverySession("verify"),
            )
            original_context, restore_context, verify_context = (
                SessionContext(original, fail_exit=True),
                SessionContext(restore),
                SessionContext(verify),
            )
            contexts = [original_context, restore_context, verify_context]

            transcript = asyncio.run(
                runner.execute_with_session_factory(
                    fixture,
                    ("HW04",),
                    lambda _root: contexts.pop(0),
                    pause=lambda _: asyncio.sleep(0),
                    allow_write_flash=True,
                )
            )

        phase = transcript["case_results"]["HW04"]["boards"]["stm32l476rtg"]["phases"][0]
        self.assertIn("raw_flash.call_tool raised", phase["failure"])
        self.assertIn("RuntimeError: transport ended after dispatch", phase["failure"])
        self.assertIn(
            "RuntimeError: failed session context close", phase["failed_session_cleanup"]["error"]
        )
        self.assertEqual(phase["emergency_restoration"]["restore"]["status"], "pass", phase)
        self.assertEqual(phase["emergency_restoration"]["readback"]["status"], "pass")
        self.assertEqual(
            original.calls,
            [
                "connect",
                "read_memory_raw",
                "write_memory_raw",
                "read_memory_raw",
                "write_memory_raw",
                "read_memory_raw",
                "read_memory_raw",
                "flash_raw",
            ],
        )
        self.assertTrue(original_context.exited)
        self.assertTrue(restore_context.exited)
        self.assertTrue(verify_context.exited)
        self.assertEqual(contexts, [])
        self.assertEqual(restore.calls, ["connect", "flash_raw", "disconnect"])
        self.assertEqual(verify.calls, ["connect", "read_memory_raw", "disconnect"])
        self.assertEqual(
            [item["name"] for item in phase["emergency_restoration"]["physical_sessions"]],
            ["emergency_restore", "emergency_restore_verification"],
        )

    def test_execute_stdout_is_redacted_while_transcript_keeps_private_evidence(self) -> None:
        """AT10: live execution does not print fixture-bound IDs or token evidence."""

        runner = _runner()
        sentinel_uid = "probe-uid-SENTINEL-9F31"
        sentinel_token = "continuation-token-SENTINEL-BD77"
        private_transcript = {
            "schema_version": 2,
            "overall_status": "failed",
            "case_results": {
                "HW02": {
                    "status": "failed",
                    "boards": {
                        "nrf52840": {
                            "probe_uid": sentinel_uid,
                            "steps": [{"continuation_id": sentinel_token}],
                        }
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            transcript_path = Path(temporary) / "private-transcript.json"
            arguments = SimpleNamespace(
                fixtures=Path(temporary) / "private-fixture.json",
                validate_fixtures=False,
                execute=True,
                cases="HW02",
                allow_write_flash=False,
                transcript=transcript_path,
            )

            async def fake_execute(*unused: object, **unused_kwargs: object) -> dict[str, object]:
                del unused, unused_kwargs
                return private_transcript

            rendered_stdout = io.StringIO()
            with (
                patch.object(runner, "parse_args", return_value=arguments),
                patch.object(runner, "load_fixture", return_value={}),
                patch.object(runner, "selected_cases", return_value=("HW02",)),
                patch.object(runner, "execute", side_effect=fake_execute),
                contextlib.redirect_stdout(rendered_stdout),
            ):
                self.assertEqual(runner.main(), 1)

            stdout = rendered_stdout.getvalue()
            self.assertNotIn(sentinel_uid, stdout)
            self.assertNotIn(sentinel_token, stdout)
            self.assertEqual(
                json.loads(stdout), {"overall_status": "failed", "cases": {"HW02": "failed"}}
            )
            self.assertIn(sentinel_uid, transcript_path.read_text(encoding="utf-8"))
            self.assertIn(sentinel_token, transcript_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
