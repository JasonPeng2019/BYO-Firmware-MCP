"""S02A-01 supported-child control after real validation gate publication.

Reuses the existing valid full pack/datasheet setup fixture, not its regression
assertions. Only hardware/inventory is simulated. All public tool dispatch,
validation, gate publication, reports, cancellation and later read are real.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import anyio

from pyocd_debug_mcp.kernel.operations import current_operation, operation_manager
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from tests.elf_symbol_fixture import ElfSymbol, elf32_symbols
from tests import test_lite_application_flash as full_fixture
from tests.tiered_acceptance_support import public_text


class FinalSupportedBatchCancellation(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("VI01A_FULL_PROJECT"):
            return  # Existing fresh-process decorator establishes root before imports.
        self.fixture = full_fixture.FullGenericAllocationTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def scenario(self, interruption):
        server = self.fixture.server
        board = self.fixture.board_id
        backend = self.fixture.backend
        token = server.assignment_store.connection_for(board)
        public_text(server, "load_setup_tool", {"board_id": board, "tool_name": "board_validate"})
        artifact = self.fixture.root / "batch-value.elf"
        artifact.write_bytes(elf32_symbols([ElfSymbol("after_validation", 0x20000024)]))
        committed = threading.Event()
        observations = []
        published_validation_runs = []
        entered_children = []
        real_commit = server.run_if_not_cancelled
        real_dispatch = server.mcp._call_tool_inner

        async def observed_dispatch(name, arguments, board_id):
            entered_children.append(name)
            return await real_dispatch(name, arguments, board_id)

        def commit(action):
            result = real_commit(action)
            managed = current_operation()
            self.assertIsNotNone(managed)
            observations.append((managed.tool_name, managed.completion_committed))
            published = server.gate_manager.snapshot(board)
            self.assertIsNotNone(published)
            published_validation_runs.append(published.validation_run)
            committed.set()
            if interruption:
                self.assertTrue(managed.cancellation_requested.wait(15), "parent never interrupted")
            return result

        actual_backend = server._board_validator._backend
        inventory = ValidationInventory(
            probes=(ValidationProbe("full-probe", "ST-Link", "stlink", "full-serial"),)
        )
        outcomes = {}

        def keep_evidence():
            destination = os.environ.get("BYO_FINAL_CASE_EVIDENCE")
            if not destination:
                return
            root = Path(destination) / self._testMethodName
            root.mkdir(parents=True, exist_ok=True)
            gate = server.gate_manager.snapshot(board)
            if published_validation_runs:
                report = server._report_writer.paths("validation", published_validation_runs[-1])
                for label, path in (
                    ("validation-report.json", report.report),
                    ("validation-events.jsonl", report.events),
                ):
                    if path.is_file():
                        (root / label).write_bytes(path.read_bytes())
            (root / "observations.json").write_text(
                json.dumps(
                    {
                        "interruption": interruption,
                        "publication_observations": observations,
                        "batch_outcomes": outcomes,
                        "published_validation_runs": published_validation_runs,
                        "entered_registered_tools": entered_children,
                        "backend_calls": backend.calls,
                        "gate": gate,
                        "operations": operation_manager.snapshots(),
                    },
                    indent=2,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )

        self.addCleanup(keep_evidence)
        actions = [
            {"tool_name": "board_validate", "arguments": {"board_id": board, "probe_id": token}},
            {
                "tool_name": "read_memory_symbol",
                "arguments": {
                    "board_id": board,
                    "symbol": "after_validation",
                    "elf_artifact": str(artifact),
                },
            },
        ]
        real_timeout = server.mcp._timeout_resolver

        async def exercise():
            async def run():
                with anyio.CancelScope() as scope:
                    outcomes["scope"] = scope
                    try:
                        outcomes["reply"] = await server.mcp.call_tool(
                            "action_batch", {"board_id": board, "actions": actions}
                        )
                    except BaseException as exc:
                        outcomes["error"] = type(exc).__name__
                    finally:
                        outcomes["done"] = True

            with anyio.fail_after(25):
                async with anyio.create_task_group() as group:
                    group.start_soon(run)
                    while not committed.is_set() and not outcomes.get("done"):
                        await anyio.sleep(0.01)
                    self.assertTrue(committed.is_set(), outcomes)
                    if interruption == "cancel":
                        outcomes["scope"].cancel()

        backend.calls.clear()
        with (
            patch.object(server, "run_if_not_cancelled", side_effect=commit),
            patch.object(server.mcp, "_call_tool_inner", side_effect=observed_dispatch),
            patch.object(
                server._board_validator,
                "_backend",
                replace(actual_backend, inventory=lambda: inventory),
            ),
            patch.object(
                server.mcp,
                "_timeout_resolver",
                side_effect=lambda name, args: (
                    0.6
                    if interruption == "deadline" and name == "action_batch"
                    else real_timeout(name, args)
                ),
            ),
        ):
            anyio.run(exercise)
        self.assertIn(("board_validate", True), observations)
        # Assert actual publication/report output even if the batch itself
        # returns an interruption; truthful committed completion is preserved.
        gate = server.gate_manager.snapshot(board)
        self.assertTrue(published_validation_runs)
        report_path = server._report_writer.paths(
            "validation", published_validation_runs[-1]
        ).report
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["terminal_status"], "validation_passed")
        later_reads = [c for c in backend.calls if c[0] == "read_memory" and c[2] == 0x20000024]
        if interruption:
            # Genuine cancellation can legitimately invalidate a transport that
            # validation used. A committed report is not permanent readiness.
            if server.connection_manager.maybe_connection(board) is None:
                self.assertIsNone(gate)
            self.assertNotIn("read_memory_symbol", entered_children, outcomes)
            self.assertEqual(later_reads, [], (outcomes, backend.calls))
        else:
            self.assertIsNotNone(gate)
            self.assertEqual(gate.validation_run, published_validation_runs[-1])
            self.assertEqual(len(later_reads), 1)
            payload = json.loads(outcomes["reply"][0].text.splitlines()[0])
            self.assertEqual(payload["status"], "batch_completed")
            self.assertEqual(len(payload["completed"]), 2)
        self.assertEqual(operation_manager.snapshots(), ())

    @full_fixture.fresh_full_project
    def test_supported_normal_validation_then_read(self):
        self.scenario(None)

    @full_fixture.fresh_full_project
    def test_supported_cancel_after_committed_validation_stops_later_read(self):
        self.scenario("cancel")

    @full_fixture.fresh_full_project
    def test_supported_deadline_after_committed_validation_stops_later_read(self):
        self.scenario("deadline")


if __name__ == "__main__":
    unittest.main()
