"""M05-C01 public workflow checks; only inventory/target I/O and stores are isolated."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyocd_debug_mcp.adapters.swd_interface import session_metadata
from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from pyocd_debug_mcp.target_errors import TargetConnectionError
from tests.m05_public_support import NULL_PLAN, PublicSetup, child
from tests import test_m05_public_lifecycle as lifecycle
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.tiered_acceptance_support import tiered_test_server


class BindingProbe(lifecycle.MemoryProbe):
    """Provider boundary that can report a changed binding or failed state read."""

    target_override = None
    probe_override = None
    state_error = None
    wrong_identity = False

    def open(self, **kwargs):
        handle = super().open(**kwargs)
        metadata = session_metadata(handle)
        return replace(
            handle,
            metadata=replace(
                metadata,
                target_override=self.target_override or metadata.target_override,
                probe_uid=self.probe_override or metadata.probe_uid,
            ),
        )

    def get_state(self, handle):
        result = super().get_state(handle)
        if self.state_error:
            raise self.state_error
        return result

    def read_memory(self, handle, address, width_bits, **kwargs):
        result = super().read_memory(handle, address, width_bits, **kwargs)
        return 0 if self.wrong_identity else result


def advertised_validation(flow, board):
    loaded = flow.json("load_setup_tool", {"board_id": board, "tool_name": "board_validate"})
    call = loaded["next_call"]
    return flow.json(call["tool"], call["arguments"])


def exercise_partial_ram(flow, board):
    """Execute real public plans, write/read/restore synthetic RAM, refuse outside it."""

    for action in ("read_memory_address", "write_memory"):
        flow.call(action + "-plan", NULL_PLAN)

    def execute(action, parameters):
        plan = flow.plan(action, board, parameters)
        assert plan["status"] == "plan_accepted", plan
        return flow.execute(plan["stable_client_fallback"])

    read = {"address": 0x20000000, "width": 32, "length": None}
    write = {
        "symbol_or_address": 0x20000000,
        "value": 0x87654321,
        "width": 32,
        "allow_address_fallback": True,
        "reason": "Host regression scratch word, restored below",
        "elf_artifact": None,
    }
    prior = child(execute("read_memory_address", read))["result"]
    assert execute("write_memory", write)["status"] == "batch_completed"
    assert child(execute("read_memory_address", read))["result"] == "0x87654321"
    assert (
        execute("write_memory", {**write, "value": int(prior, 16)})["status"] == "batch_completed"
    )
    assert child(execute("read_memory_address", read))["result"] == prior
    for action, parameters in (
        ("read_memory_address", {**read, "address": 0x20001000}),
        ("read_memory_address", {**read, "address": 0x20000FFE}),
        ("write_memory", {**write, "symbol_or_address": 0x20001000}),
        ("write_memory", {**write, "symbol_or_address": 0}),
    ):
        refused = execute(action, parameters)
        assert refused["status"] == "batch_failed", refused
        assert child(refused)["status"] == "refused", refused


def record_imports(label):
    evidence = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"])
    evidence.mkdir(parents=True, exist_ok=True)
    imports = []
    for name, module in sorted(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if name.startswith(("pyocd_debug_mcp", "tests.")) and path and Path(path).is_file():
            imports.append(
                {
                    "module": name,
                    "path": str(Path(path).resolve()),
                    "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                }
            )
    (evidence / (label + ".imports.json")).write_bytes(
        (json.dumps(imports, indent=2) + "\n").encode()
    )


def restart_workflow():
    """Child process follows returned routes; no policy/permission fixtures are installed."""

    with tiered_test_server(Path(os.environ["BYO_MCP_ARTIFACT_ROOT"])) as server:
        backend = BindingProbe()
        server.configure_tiered_test_seams(backend=backend)
        with patch.object(server, "_hardware_inventory", _setup_inventory()):
            flow = PublicSetup(server)
            route = flow.overview("Independent M05")["routes"][0]
            board = route["board_id"]
            try:
                assert route["route"] == "lite", route
                assert route["next_tool"] == "board_validate", route
                load = route["load_call"]
                loaded = flow.json(load["tool"], load["arguments"])
                assert loaded["next_call"] == route["next_call"], (loaded, route)
                call = loaded["next_call"]
                validation = flow.json(call["tool"], call["arguments"])
                exercise_partial_ram(flow, board)
                print(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "validation": validation,
                            "capabilities": flow.json("get_capabilities", {"board_id": board}),
                            "gate": server.gate_manager.live_identity(board),
                            "setup_plan": server.plan_engine.active_plan("board_setup", board),
                            "setup_allowance": server.setup_tool_loader.allowance_for(board),
                            "setup_grant": server.permission_store.active_grant(
                                "board_setup", board
                            ),
                            "calls": backend.calls,
                        },
                        default=str,
                    )
                )
            finally:
                flow.call("disconnect", {"board_id": board})
                record_imports("restart-child")


class LiteValidationTests(unittest.TestCase):
    project = lifecycle.PublicLifecycleTests.project
    research = lifecycle.PublicLifecycleTests.research
    reach_ram = lifecycle.PublicLifecycleTests.reach_ram

    def complete_lite(self, flow, route, pdf, *, attached=False):
        board, _, plan, pending = self.research(flow, route, pdf, attached=attached)
        pending = self.reach_ram(flow, board, plan, pending)
        flow.answer(board, pending, flow.ram_answer(pending))
        completed = flow.fix(plan)
        self.assertEqual(completed["status"], "setup_completed", completed)
        self.assertIn("board_validate", completed["agent_prompt"])
        self.assertNotIn("automatic validation completed", completed["agent_prompt"])
        return board

    def assert_lite(self, server, flow, board, validation):
        self.assertEqual(validation["status"], "validation_passed", validation)
        observed = validation["observed"]
        self.assertEqual(observed["tier"], "setup-lite")
        self.assertEqual(observed["identity_assertion"], "trusted-not-proven")
        self.assertIsNone(observed["identity_capability"])
        self.assertEqual(observed["server_run_id"], server.server_run.run_id)
        self.assertEqual(observed["session_id"], server._active_session_id(board))
        self.assertEqual(
            observed["policy_digest"], server._capability_policies.resolve(board).policy_digest
        )
        self.assertIsNone(server.gate_manager.live_identity(board))
        capabilities = flow.json("get_capabilities", {"board_id": board})
        self.assertEqual(capabilities["tier"], "setup-lite")
        self.assertEqual(capabilities["identity"]["assertion"], "trusted-not-proven")
        families = {row["family"]: row for row in capabilities["capabilities"]}
        for family in (
            "identity-validation",
            "flash-application",
            "flash-bootloader",
            "target-recovery",
        ):
            self.assertFalse(families[family]["safe"], families[family])
        self.assertEqual(families["target-recovery"]["recovery_mode"], "unavailable")

    def tearDown(self):
        record_imports(self.id())

    def test_attached_and_detached_completion_follows_loader_and_checks_live_state_each_time(self):
        for attached in (False, True):
            with (
                self.subTest(attached=attached),
                self.project() as (_, server, backend, flow, route, pdf),
            ):
                board = self.complete_lite(flow, route, pdf, attached=attached)
                before = len(backend.calls)
                validation = advertised_validation(flow, board)
                self.assert_lite(server, flow, board, validation)
                self.assertEqual(
                    [call[0] for call in backend.calls[before:]],
                    ["get_state"] if attached else ["open", "get_state"],
                )
                again = advertised_validation(flow, board)
                self.assertNotEqual(again["continuation_id"], validation["continuation_id"])
                self.assertEqual(backend.calls[-1][0], "get_state")
                exercise_partial_ram(flow, board)
                self.assertFalse(any(call[0] in {"flash", "recover"} for call in backend.calls))

    def test_fresh_process_follows_overview_and_uses_partial_policy_without_setup_authority(self):
        with self.project() as (root, server, _, flow, route, pdf):
            board = self.complete_lite(flow, route, pdf)
            original = advertised_validation(flow, board)
            flow.call("disconnect", {"board_id": board})
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from tests.test_m05_lite_validation import restart_workflow; restart_workflow()",
                ],
                env={
                    **os.environ,
                    "BYO_MCP_ARTIFACT_ROOT": str(root),
                    "BYO_MCP_MONITOR_ROOT": str(root / "restart-monitor"),
                },
                capture_output=True,
                text=True,
                timeout=45,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            evidence = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"])
            (evidence / "restart-workflow.json").write_bytes(
                (json.dumps(result, indent=2) + "\n").encode()
            )
            self.assertNotEqual(result["pid"], os.getpid())
            validation = result["validation"]
            self.assertEqual(validation["status"], "validation_passed", validation)
            for key in ("server_run_id", "session_id"):
                self.assertNotEqual(validation["observed"][key], original["observed"][key])
            self.assertEqual(
                validation["observed"]["policy_digest"], original["observed"]["policy_digest"]
            )
            self.assertEqual(result["capabilities"]["identity"]["assertion"], "trusted-not-proven")
            for key in ("gate", "setup_plan", "setup_allowance", "setup_grant"):
                self.assertIsNone(result[key])
            self.assertEqual([c[0] for c in result["calls"][:2]], ["open", "get_state"])
            self.assertFalse(
                any(c[0] in {"flash", "recover", "read_serial"} for c in result["calls"])
            )

    def test_changed_live_target_or_probe_refuses_before_state_query(self):
        for field, value in (("target_override", "stm32l476rg"), ("probe_override", "OTHER-PROBE")):
            with self.subTest(field=field), self.project() as (_, server, _, flow, route, pdf):
                backend = BindingProbe()
                server.configure_tiered_test_seams(backend=backend)
                board = self.complete_lite(flow, route, pdf)
                digest = server._capability_policies.resolve(board).policy_digest
                flow.call("disconnect", {"board_id": board})
                flow.overview("Independent M05")
                setattr(backend, field, value)
                before = len(backend.calls)
                with self.assertRaisesRegex(Exception, "binding|probe|assignment"):
                    advertised_validation(flow, board)
                self.assertFalse(any(c[0] == "get_state" for c in backend.calls[before:]))
                self.assertIsNone(server.connection_manager.maybe_connection(board))
                self.assertEqual(backend.calls[-1][0], "close")
                self.assertIsNone(server.gate_manager.live_identity(board))
                self.assertEqual(server._capability_policies.resolve(board).policy_digest, digest)

    def test_reassigned_probe_rejects_old_advertised_token_before_provider_work(self):
        with self.project() as (_, server, backend, flow, route, pdf):
            board = self.complete_lite(flow, route, pdf)
            loaded = flow.json(
                "load_setup_tool", {"board_id": board, "tool_name": "board_validate"}
            )
            flow.call("disconnect", {"board_id": board})
            inventory = ValidationInventory(
                probes=(ValidationProbe("OTHER", "Other", "jlink", "OTHER"),)
            )
            boundary = SimpleNamespace(
                snapshot=lambda: snapshot_from_validation_inventory(inventory),
                validation_inventory=lambda: inventory,
            )
            with patch.object(server, "_hardware_inventory", boundary):
                overview = flow.overview("Independent M05")
                self.assertTrue(overview["routes"], overview)
                self.assertNotEqual(
                    server.assignment_store.connection_for(board),
                    loaded["next_call"]["arguments"]["probe_id"],
                    overview,
                )
                before = list(backend.calls)
                call = loaded["next_call"]
                with self.assertRaisesRegex(Exception, "does not match.*assignment"):
                    flow.json(call["tool"], call["arguments"])
                self.assertEqual(backend.calls, before)
                self.assert_lite(server, flow, board, advertised_validation(flow, board))

    def test_disappeared_inventory_and_failed_provider_never_produce_passing_check(self):
        with self.project() as (_, server, _, flow, route, pdf):
            backend = BindingProbe()
            server.configure_tiered_test_seams(backend=backend)
            board = self.complete_lite(flow, route, pdf)
            advertised_validation(flow, board)
            empty = SimpleNamespace(validation_inventory=lambda: ValidationInventory())
            before = list(backend.calls)
            with patch.object(server, "_hardware_inventory", empty):
                with self.assertRaisesRegex(Exception, "no longer present|selection"):
                    advertised_validation(flow, board)
            self.assertEqual(backend.calls[len(before) :], [("close", "LITE-ACCEPTANCE-PROBE")])
            self.assertIsNone(server.connection_manager.maybe_connection(board))
            flow.overview("Independent M05")
            backend.state_error = TargetConnectionError("synthetic transport lost")
            with self.assertRaisesRegex(Exception, "synthetic transport lost"):
                advertised_validation(flow, board)
            self.assertIsNone(server.gate_manager.live_identity(board))
            self.assertIsNone(server.connection_manager.maybe_connection(board))
            backend.state_error = None
            flow.overview("Independent M05")
            self.assert_lite(server, flow, board, advertised_validation(flow, board))

    def test_reconnect_requires_new_operation_plan_and_never_recreates_setup_authority(self):
        with self.project() as (_, server, backend, flow, route, pdf):
            board = self.complete_lite(flow, route, pdf)
            original = advertised_validation(flow, board)
            flow.call("read_memory_address-plan", NULL_PLAN)
            old_plan = flow.plan(
                "read_memory_address", board, {"address": 0x20000000, "width": 32, "length": None}
            )
            flow.call("disconnect", {"board_id": board})
            flow.overview("Independent M05")
            current = advertised_validation(flow, board)
            self.assertNotEqual(
                current["observed"]["session_id"], original["observed"]["session_id"]
            )
            before = list(backend.calls)
            self.assertEqual(
                flow.execute(old_plan["stable_client_fallback"])["status"], "batch_failed"
            )
            self.assertEqual(backend.calls, before)
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertIsNone(server.permission_store.active_grant("board_setup", board))
            exercise_partial_ram(flow, board)

    def test_full_keeps_real_identity_reads_and_mismatch_refusal(self):
        with self.project() as (_, server, _, flow, route, pdf):
            backend = BindingProbe()
            server.configure_tiered_test_seams(backend=backend)
            board, _, plan, pending = self.research(flow, route, pdf, tier="setup-full")
            flow.answer(board, pending, flow.target_answer(pending, "nrf52840"))
            self.assertEqual(flow.fix(plan)["status"], "setup_completed")
            before = len(backend.calls)
            validation = advertised_validation(flow, board)
            self.assertEqual(validation["status"], "validation_passed", validation)
            self.assertTrue(any(c[0] == "read_memory" for c in backend.calls[before:]))
            self.assertFalse(any(c[0] == "get_state" for c in backend.calls[before:]))
            self.assertIn(
                server.gate_manager.snapshot(board).identity_capability, {"exact", "compatible"}
            )
            backend.wrong_identity = True
            refused = advertised_validation(flow, board)
            self.assertEqual(refused["status"], "validation_failed", refused)
            self.assertIn("mismatch", refused["code"])
            self.assertIsNone(server.gate_manager.live_identity(board))

    def test_no_setup_cannot_use_validation_to_gain_safe_authority(self):
        with self.project() as (_, _, backend, flow, route, _):
            with self.assertRaisesRegex(Exception, "setup-lite or setup-full"):
                flow.json("board_validate", {"board_id": route["board_id"]})
            self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
