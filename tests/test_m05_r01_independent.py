"""Independent R01 oracles: public LITE validation, binding and finite answers.

Only inventory/target transport and isolated stores are simulated. Silicon facts
in this file are synthetic; physical evidence comes from m05_physical_lite.
"""

from __future__ import annotations

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
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationSerial
from tests.m05_public_support import NULL_PLAN, PublicSetup, child
from tests import test_m05_public_lifecycle as lifecycle
from tests.test_m05_lite_validation import record_imports
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.tiered_acceptance_support import tiered_test_server


def validate_advertised(flow):
    route = flow.overview("Independent M05")["routes"][0]
    assert route["route"] == "lite", route
    load = route["load_call"]
    loaded = flow.json(load["tool"], load["arguments"])
    assert loaded["next_call"] == route["next_call"], (loaded, route)
    call = loaded["next_call"]
    # Convert actual public refusal to an assertion, so a pre-C01 mutant's
    # meaningful failure cannot be mistaken for a fixture/import error.
    try:
        result = flow.json(call["tool"], call["arguments"])
    except Exception as exc:
        raise AssertionError(f"C01 advertised LITE validation must be usable: {exc}") from exc
    assert result["status"] == "validation_passed", result
    return result


def check_validation(server, backend, flow, board, validation, before):
    observed = validation["observed"]
    connection = server.connection_manager.connection_for(board)
    metadata = session_metadata(connection.handle)
    assert validation["code"] == "validation/lite-binding-checked", validation
    assert observed["tier"] == "setup-lite"
    assert observed["map_kind"] == "partial"
    assert observed["identity_assertion"] == "trusted-not-proven"
    assert observed["identity_capability"] is None
    assert observed["server_run_id"] == server.server_run.run_id
    assert observed["session_id"] == connection.runtime_session.session_id
    assert observed["connection_id"] == connection.connection_id
    assert observed["probe_id"] == server.assignment_store.connection_for(board)
    assert metadata.probe_uid == "LITE-ACCEPTANCE-PROBE"
    assert observed["target"] == metadata.target_override == "nrf52840"
    assert observed["target_state"] == "halted", observed
    assert observed["policy_digest"] == server._capability_policies.resolve(board).policy_digest
    calls = backend.calls[before:]
    assert calls and calls[-1][0] == "get_state", calls
    assert not any(c[0] in {"write_memory", "read_memory", "flash", "recover"} for c in calls)
    assert server.gate_manager.live_identity(board) is None
    for authority in (
        server.plan_engine.active_plan("board_setup", board),
        server.setup_tool_loader.allowance_for(board),
        server.permission_store.active_grant("board_setup", board),
    ):
        assert authority is None, authority
    capabilities = flow.json("get_capabilities", {"board_id": board})
    assert capabilities["tier"] == "setup-lite"
    assert capabilities["identity"]["assertion"] == "trusted-not-proven"
    families = {row["family"]: row for row in capabilities["capabilities"]}
    for family in (
        "identity-validation",
        "flash-application",
        "flash-bootloader",
        "target-recovery",
    ):
        assert not families[family]["safe"], families[family]
    assert families["target-recovery"]["recovery_mode"] == "unavailable"


def partial_operations(server, backend, flow, board):
    for action in ("read_memory_address", "write_memory"):
        flow.call(action + "-plan", NULL_PLAN)

    def execute(action, parameters):
        plan = flow.plan(action, board, parameters)
        assert plan["status"] == "plan_accepted", plan
        return flow.execute(plan["stable_client_fallback"])

    read = {"address": 0x20000000, "width": 32, "length": None}
    write = {
        "symbol_or_address": read["address"],
        "value": 0xEDCBA987,
        "width": 32,
        "allow_address_fallback": True,
        "reason": "Independent synthetic scratch restoration",
        "elf_artifact": None,
    }
    original = child(execute("read_memory_address", read))["result"]
    try:
        assert execute("write_memory", write)["status"] == "batch_completed"
        assert int(child(execute("read_memory_address", read))["result"], 0) == 0xEDCBA987
    finally:
        assert (
            execute("write_memory", {**write, "value": int(original, 0)})["status"]
            == "batch_completed"
        )
        assert child(execute("read_memory_address", read))["result"] == original
    for action, parameters in (
        ("read_memory_address", {**read, "address": 0x20001000}),
        ("read_memory_address", {**read, "address": 0x20000FFE}),
        ("write_memory", {**write, "symbol_or_address": 0x20001000}),
        ("write_memory", {**write, "symbol_or_address": 0x20000FFE}),
        ("write_memory", {**write, "symbol_or_address": 0}),
    ):
        before = list(backend.calls)
        batch = execute(action, parameters)
        assert batch["status"] == "batch_failed", batch
        assert child(batch)["status"] == "refused", batch
        assert backend.calls == before, backend.calls[len(before) :]
    with patch.object(
        server,
        "capture_uart_output",
        return_value=SimpleNamespace(matched=False, text="", duration_seconds=0.01),
    ):
        uart = flow.json(
            "read_serial_raw",
            {"board_id": board, "port": "synthetic", "baudrate": 115200, "read_seconds": 0.01},
        )
    assert uart["status"] == "ok", uart
    assert "bypass" in str(uart["warning"]).lower(), uart
    assert not any(c[0] in {"flash", "recover"} for c in backend.calls)


def restarted_client():
    with tiered_test_server(Path(os.environ["BYO_MCP_ARTIFACT_ROOT"])) as server:
        backend = lifecycle.MemoryProbe()
        server.configure_tiered_test_seams(backend=backend)
        with patch.object(server, "_hardware_inventory", _setup_inventory()):
            flow = PublicSetup(server)
            board = flow.overview("Independent M05")["routes"][0]["board_id"]
            try:
                before = len(backend.calls)
                validation = validate_advertised(flow)
                check_validation(server, backend, flow, board, validation, before)
                partial_operations(server, backend, flow, board)
                print(
                    json.dumps(
                        {"pid": os.getpid(), "validation": validation, "calls": backend.calls},
                        default=str,
                    )
                )
            finally:
                flow.call("disconnect", {"board_id": board})
                record_imports("r01-restart")


class IndependentR01Tests(unittest.TestCase):
    project = lifecycle.PublicLifecycleTests.project
    research = lifecycle.PublicLifecycleTests.research
    reach_ram = lifecycle.PublicLifecycleTests.reach_ram
    complete = lifecycle.PublicLifecycleTests.complete

    def tearDown(self):
        record_imports(self.id())

    def test_c01_fresh_process_live_binding_partial_policy_and_operations(self):
        with self.project() as (root, server, backend, flow, route, pdf):
            board, _, plan, pending = self.research(flow, route, pdf, attached=True)
            old_session = server._active_session_id(board)
            pending = self.reach_ram(flow, board, plan, pending)
            self.complete(flow, board, plan, pending)
            before = len(backend.calls)
            validation = validate_advertised(flow)
            check_validation(server, backend, flow, board, validation, before)
            self.assertEqual(server._active_session_id(board), old_session)
            partial_operations(server, backend, flow, board)
            flow.call("disconnect", {"board_id": board})
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from tests.test_m05_r01_independent import restarted_client; restarted_client()",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env={
                    **os.environ,
                    "BYO_MCP_ARTIFACT_ROOT": str(root),
                    "BYO_MCP_MONITOR_ROOT": str(root / "r01-restart-monitor"),
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertNotEqual(result["pid"], os.getpid())
            observed = result["validation"]["observed"]
            self.assertNotEqual(observed["server_run_id"], validation["observed"]["server_run_id"])
            self.assertNotEqual(observed["session_id"], old_session)
            self.assertEqual(observed["policy_digest"], validation["observed"]["policy_digest"])
            self.assertEqual([c[0] for c in result["calls"][:2]], ["open", "get_state"])

    def test_bound_read_plan_cannot_survive_real_disconnect_reconnect(self):
        with self.project() as (_, server, backend, flow, route, pdf):
            board, _, plan, pending = self.research(flow, route, pdf)
            self.complete(flow, board, plan, self.reach_ram(flow, board, plan, pending))
            original = validate_advertised(flow)
            flow.call("read_memory_address-plan", NULL_PLAN)
            old = flow.plan(
                "read_memory_address", board, {"address": 0x20000000, "width": 32, "length": None}
            )
            flow.call("disconnect", {"board_id": board})
            current = validate_advertised(flow)
            self.assertNotEqual(
                current["observed"]["session_id"], original["observed"]["session_id"]
            )
            before = list(backend.calls)
            self.assertEqual(flow.execute(old["stable_client_fallback"])["status"], "batch_failed")
            self.assertEqual(backend.calls, before)
            partial_operations(server, backend, flow, board)

    def test_ordinary_probe_target_drift_refuses_before_live_read_or_effect(self):
        for field, wrong in (("probe_uid", "OTHER-PROBE"), ("target_override", "cortex_m")):
            with self.subTest(field=field), self.project() as (_, server, _, flow, route, pdf):

                class DriftProbe(lifecycle.MemoryProbe):
                    drift = False

                    def open(self, **kwargs):
                        handle = super().open(**kwargs)
                        return (
                            replace(
                                handle, metadata=replace(session_metadata(handle), **{field: wrong})
                            )
                            if self.drift
                            else handle
                        )

                backend = DriftProbe()
                server.configure_tiered_test_seams(backend=backend)
                board, _, plan, pending = self.research(flow, route, pdf)
                self.complete(flow, board, plan, self.reach_ram(flow, board, plan, pending))
                digest = server._capability_policies.resolve(board).policy_digest
                flow.call("disconnect", {"board_id": board})
                backend.drift = True
                before = len(backend.calls)
                with self.assertRaisesRegex(AssertionError, "binding|probe|assignment"):
                    validate_advertised(flow)
                calls = backend.calls[before:]
                self.assertEqual([c[0] for c in calls], ["open", "close"], calls)
                self.assertIsNone(server.connection_manager.maybe_connection(board))
                self.assertIsNone(server.gate_manager.live_identity(board))
                self.assertEqual(server._capability_policies.resolve(board).policy_digest, digest)

    def test_full_control_still_proves_live_identity_and_rejects_mismatch(self):
        with self.project() as (_, server, _, flow, route, pdf):

            class IdentityProbe(lifecycle.MemoryProbe):
                mismatch = False

                def read_memory(self, handle, address, width_bits, **kwargs):
                    value = super().read_memory(handle, address, width_bits, **kwargs)
                    return 0 if self.mismatch else value

            backend = IdentityProbe()
            server.configure_tiered_test_seams(backend=backend)
            board, _, plan, pending = self.research(flow, route, pdf, tier="setup-full")
            flow.answer(board, pending, flow.target_answer(pending, "nrf52840"))
            self.assertEqual(flow.fix(plan)["status"], "setup_completed")
            loaded = flow.json(
                "load_setup_tool", {"board_id": board, "tool_name": "board_validate"}
            )
            call = loaded["next_call"]
            before = len(backend.calls)
            result = flow.json(call["tool"], call["arguments"])
            self.assertEqual(result["status"], "validation_passed", result)
            self.assertTrue(any(c[0] == "read_memory" for c in backend.calls[before:]))
            self.assertFalse(any(c[0] == "get_state" for c in backend.calls[before:]))
            self.assertIsNotNone(server.gate_manager.live_identity(board))
            backend.mismatch = True
            result = flow.json(call["tool"], call["arguments"])
            self.assertEqual(result["status"], "validation_failed", result)
            self.assertIn("mismatch", result["code"])
            self.assertIsNone(server.gate_manager.live_identity(board))

    def test_ten_accepted_answers_exhaust_without_plan_or_permission_refill(self):
        with self.project() as (_, server, backend, flow, route, pdf):
            current = [0]
            base = _setup_inventory().validation_inventory()

            def inventory():
                generation = current[0]
                return ValidationInventory(
                    probes=base.probes,
                    serial_ports=tuple(
                        ValidationSerial(
                            "".join(
                                letter.lower() if (generation * 2 + i) & (1 << bit) else letter
                                for bit, letter in enumerate("UARTFIXTURE")
                            ),
                            f"COM{i + 20}",
                            "Synthetic multi-interface UART",
                            "UARTFIXTURE",
                        )
                        for i in range(2)
                    ),
                )

            boundary = SimpleNamespace(
                validation_inventory=inventory,
                snapshot=lambda: snapshot_from_validation_inventory(inventory()),
            )
            board = route["board_id"]
            flow.json("load_setup_tool", {"board_id": board, "tool_name": "board_setup-plan"})
            flow.call("board_setup-plan", NULL_PLAN)
            parameters = {
                **route["plan_action_parameters_template"],
                "mcu_part_number": "nRF52840-QIAA",
                "target_tier": "setup-lite",
                "requires_uart": True,
                "serial_baudrate": 115200,
                "serial_id": "UARTFIXTURE",
                "datasheet_path": str(pdf),
            }
            with patch.object(server, "_hardware_inventory", boundary):
                plan = flow.plan("board_setup", board, parameters, permission="one-time")
                self.assertEqual(plan["setup_continuation"]["max_answered_resumptions"], 10)
                pending = flow.start(plan)
                grant = server.permission_store.active_grant("board_setup", board)
                for number in range(10):
                    self.assertEqual(pending["status"], "setup_needs_user_input", pending)
                    answer = {"choice_id": pending["choices"][0]["choice_id"]}
                    self.assertEqual(
                        flow.answer(board, pending, answer)["accepted"], "friendly_choice"
                    )
                    with self.assertRaisesRegex(Exception, "already accepted"):
                        flow.answer(board, pending, answer)
                    active = server.plan_engine.active_plan("board_setup", board)
                    self.assertEqual(
                        (active.plan_id, active.remaining_calls, dict(active.paired_remaining)),
                        (plan["plan_id"], 0, {"board_fix_setup": 1 if number == 0 else 0}),
                    )
                    self.assertIs(server.permission_store.active_grant("board_setup", board), grant)
                    # Hardware-only enumeration churn: stable identity compares
                    # case-insensitively, while a selected ephemeral token must
                    # match a current row exactly. Two UART endpoints remain;
                    # the next real preflight asks again. No workflow mock.
                    current[0] += 1
                    pending = flow.fix(plan)
                    if number < 9:
                        self.assertIsNotNone(server.setup_tool_loader.allowance_for(board))
                self.assertEqual(pending["status"], "setup_blocked", pending)
                self.assertIn("exhausted", pending["agent_prompt"])
                self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
                self.assertIsNone(server.setup_tool_loader.allowance_for(board))
                self.assertIsNone(server.permission_store.active_grant("board_setup", board))
                with self.assertRaisesRegex(Exception, "closed"):
                    flow.answer(board, pending, answer)
                self.assertEqual(
                    flow.execute(plan["paired_action_fallbacks"][0]["call"])["status"],
                    "batch_failed",
                )
                self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
