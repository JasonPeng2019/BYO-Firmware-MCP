"""Focused regressions for the round-1 trusted-caller policy changes."""

from __future__ import annotations

import argparse
import asyncio
import math
import tempfile
import unittest
from pathlib import Path

from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.guardrails.plan_defs import definition_for_action
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanRefusal
from pyocd_debug_mcp.kernel.finalizers import FinalizerValidationError, parse_finalizer
from pyocd_debug_mcp.kernel.operations import (
    ARGUMENT_TIMEOUT_GRACE_SECONDS,
    operation_timeout_seconds,
)
from pyocd_debug_mcp.native_build import _declared_artifacts, _timeout_seconds
from pyocd_debug_mcp.safety.regions import (
    ActionCategory,
    AddressRange,
    Allowed,
    Provenance,
    Refusal,
    RegionKind,
    SafetyMap,
    SafetyRegion,
    SourceAuthority,
)
from pyocd_debug_mcp.services.uart_exchange_schema import validate_serial_exchange_parameters
from pyocd_debug_mcp.services.symbols import ResolvedSymbol
from pyocd_debug_mcp.setup_flow.packs import (
    PackCandidate,
    PackCandidateError,
    PackCandidatePipeline,
)
from pyocd_debug_mcp.setup_flow.preflight import SetupUserInput
from pyocd_debug_mcp.setup_flow.research import (
    ResearchTracker,
    ValidationOutcome,
    make_research_request,
)
from pyocd_debug_mcp.setup_flow.setup import SetupWorkflow, SetupWorkflowError
from pyocd_debug_mcp.tools.batch import BatchChild, BatchValidationError, build_batch_handlers
from pyocd_debug_mcp.tools.memory import MemoryToolServices, build_memory_handlers
from pyocd_debug_mcp.tools.misc import MiscToolServices, build_misc_handlers


class _Registry:
    def is_registered(self, name: str) -> bool:
        return True

    def unlock(self, name: str, board_id: str) -> None:
        del name, board_id

    def relock(self, name: str, board_id: str) -> None:
        del name, board_id

    def is_unlocked(self, name: str, board_id: str | None) -> bool:
        del name, board_id
        return False


def _unexpected_symbol(_path: Path, _name: str) -> ResolvedSymbol:
    raise AssertionError("symbol resolution is not expected in this test")


class RoundOneTrustedCallerTests(unittest.TestCase):
    def test_five_research_rejections_continue_and_duplicates_still_reject(self) -> None:
        request = make_research_request(
            fact_id="target",
            continuation_token="token",
            board_id="board",
            mcu_part_number="STM32U5",
            unresolved_fact="target",
            requested_fields=("candidate",),
            authoritative_facts={},
        )
        tracker = ResearchTracker()
        for index in range(5):
            result = tracker.validate_reply(
                request,
                {"candidate": str(index)},
                lambda _: ValidationOutcome(False, "rejected"),
            )
            self.assertEqual(result.status, "setup_research_required")
        duplicate = tracker.validate_reply(
            request,
            {"candidate": "4"},
            lambda _: ValidationOutcome(False, "unreachable"),
        )
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(len(tracker.failures(request)), 5)

    def test_five_checksum_mismatches_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FirmStore(root)
            pipeline = PackCandidatePipeline(
                store,
                enumerate_targets=lambda *_: (),
                live_connect=lambda *_: None,
            )
            for index in range(5):
                source = root / f"candidate-{index}.pack"
                source.write_bytes(f"payload-{index}".encode())
                candidate = PackCandidate(
                    "Vendor.Device",
                    f"{index}.0",
                    f"candidate-{index}.pack",
                    "https://vendor.invalid/pack",
                    source,
                    "0" * 64,
                )
                with self.assertRaises(PackCandidateError) as raised:
                    pipeline.validate(candidate, required_target="target")
                self.assertEqual(raised.exception.code, "package/checksum-mismatch")
            self.assertEqual(len(pipeline.failures), 5)

    def test_five_replacement_setup_plans_work_and_duplicate_id_fails(self) -> None:
        user_input = SetupUserInput(
            "board", "connection", "Board", "STM32U5", None, requires_uart=False
        )
        workflow = SetupWorkflow(None, lambda _: None)  # type: ignore[arg-type]
        for index in range(5):
            workflow.begin_plan(f"plan-{index}", user_input, mode="setup")
        with self.assertRaises(SetupWorkflowError):
            workflow.begin_plan("plan-4", user_input, mode="setup")

    def test_flexible_budget_has_no_upper_cap_but_fixed_budget_remains_exact(self) -> None:
        flexible = definition_for_action("read_memory_address")
        self.assertEqual(PlanEngine._validate_budget(flexible, 21, 11, session_id=None), (21, 11))
        fixed = definition_for_action("set_breakpoint")
        with self.assertRaises(PlanRefusal):
            PlanEngine._validate_budget(fixed, 2, 0, session_id=None)

    def test_large_batch_runs_all_children_and_retains_child_rejections(self) -> None:
        seen: list[str] = []

        async def dispatch(name: str, arguments: dict[str, object]) -> str:
            del arguments
            seen.append(name)
            return name

        handler = build_batch_handlers(
            dispatch, tool_exists=lambda name: name == "read_memory_address"
        )["action_batch"]
        children = [
            BatchChild(tool_name="read_memory_address", arguments={"board_id": "board"})
            for _ in range(65)
        ]
        asyncio.run(handler("board", children))
        self.assertEqual(len(seen), 65)
        with self.assertRaises(BatchValidationError):
            asyncio.run(
                handler("board", [BatchChild(tool_name="unknown", arguments={"board_id": "board"})])
            )
        with self.assertRaises(BatchValidationError):
            asyncio.run(
                handler(
                    "board", [BatchChild(tool_name="action_batch", arguments={"board_id": "board"})]
                )
            )
        with self.assertRaises(BatchValidationError):
            asyncio.run(
                handler(
                    "board",
                    [BatchChild(tool_name="read_memory_address", arguments={"board_id": "other"})],
                )
            )

    def test_unbounded_serial_schema_and_deadline_rejects_nonfinite_values(self) -> None:
        parameters = {
            "steps": [
                {"text": "x" * 5000, "expected_text": "ok", "line_ending": "lf"} for _ in range(9)
            ],
            "read_seconds": 31.0,
            "baudrate": 115200,
            "port": None,
            "ready_text": "ready",
            "ready_seconds": 31.0,
            "ready_probe_text": "p" * 257,
            "ready_probe_line_ending": "lf",
            "ready_probe_delay_seconds": 31.0,
            "clear_input": False,
        }
        self.assertIsNone(validate_serial_exchange_parameters(parameters))
        parameters["read_seconds"] = math.inf
        self.assertIsNotNone(validate_serial_exchange_parameters(parameters))
        parameters["read_seconds"] = 1.0
        parameters["ready_seconds"] = math.nan
        self.assertIsNotNone(validate_serial_exchange_parameters(parameters))
        expected = 9 * 31.0 + 31.0 + ARGUMENT_TIMEOUT_GRACE_SECONDS
        self.assertGreaterEqual(
            operation_timeout_seconds(
                "serial_exchange", {**parameters, "ready_seconds": 31.0, "read_seconds": 31.0}
            ),
            expected,
        )
        with self.assertRaises(FinalizerValidationError):
            parse_finalizer(
                "write_serial", {"action": "uart_write", "text": "x", "timeout_seconds": math.inf}
            )
        self.assertGreaterEqual(
            operation_timeout_seconds(
                "write_serial",
                {
                    "timeout_seconds": 31.0,
                    "on_exit": {"action": "uart_write", "timeout_seconds": 6.0},
                },
            ),
            31.0 + 6.0 + 2 * ARGUMENT_TIMEOUT_GRACE_SECONDS,
        )

    def test_prohibited_ranges_are_readable_but_never_mutable(self) -> None:
        region = SafetyRegion(
            "critical",
            RegionKind.PROHIBITED,
            AddressRange(0x1000, 0x1100),
            (Provenance(SourceAuthority.OFFICIAL_DOCUMENT, "source", "critical"),),
        )
        safety_map = SafetyMap((region,))
        requested = AddressRange(0x1000, 0x1004)
        self.assertIsInstance(safety_map.check(ActionCategory.MEMORY_READ, (requested,)), Allowed)
        for action in (
            ActionCategory.MEMORY_WRITE,
            ActionCategory.REGISTER_WRITE,
            ActionCategory.FLASH_APPLICATION,
            ActionCategory.BREAKPOINT,
        ):
            self.assertIsInstance(safety_map.check(action, (requested,)), Refusal)
        self.assertIsInstance(
            safety_map.check(ActionCategory.MEMORY_READ, (AddressRange(0x2000, 0x2004),)), Refusal
        )

    def test_large_mapped_memory_read_reaches_backend_and_invalid_inputs_refuse(self) -> None:
        checked: list[tuple[int, int]] = []
        block_reads: list[int] = []
        services = MemoryToolServices(
            runtime_for=lambda _: None,
            active_session_id=lambda _: None,
            duration_ms=lambda _: 0,
            record_event=lambda *args, **kwargs: None,
            format_refusal=lambda refusal, **kwargs: refusal.message,
            handle_for=lambda _: object(),
            symbol_artifact_for=lambda _: Path("unused"),
            find_symbols=lambda *_: (),
            resolve_symbol=_unexpected_symbol,
            read_target_memory=lambda *_: 0,
            read_target_block=lambda _handle, _address, length: block_reads.append(length) or [0],
            write_target_memory=lambda *_: None,
            check_memory_read=lambda _board, address, length: checked.append((address, length)),
        )
        read = build_memory_handlers(services)["read_memory_address"]
        self.assertIn("00", read("board", 0, 8, 65_537))
        self.assertEqual(checked, [(0, 65_537)])
        self.assertEqual(block_reads, [65_537])
        self.assertIn("positive integer", read("board", 0, 8, 0))
        self.assertIn("positive integer", read("board", 0, 8, -1))
        self.assertIn("width must be one", read("board", 0, 64, 1))

    def test_wait_and_native_build_accept_values_above_former_limits(self) -> None:
        waits: list[float] = []
        wait = build_misc_handlers(
            MiscToolServices(
                runtime_for=lambda _: None,
                duration_ms=lambda _: 0,
                record_event=lambda *args, **kwargs: None,
                sleep=waits.append,
            )
        )["wait"]
        self.assertIn("60001", wait("board", 60_001))
        self.assertEqual(waits, [60.001])
        self.assertEqual(_timeout_seconds(86_401), 86_401.0)
        self.assertRaises(RuntimeError, _timeout_seconds, math.inf)
        role = "ArtifactRole" + ("x" * 80)
        declared = _declared_artifacts(
            argparse.Namespace(
                artifact_elf=None,
                artifact_hex=None,
                artifact_map=None,
                artifact=[f"{role}=image.bin"],
            )
        )
        self.assertIsNotNone(declared)
        assert declared is not None
        self.assertEqual(declared[role.casefold()], "image.bin")
