"""Fresh independent M05 tests: public dispatch and real policy/plan/admission code.

Only the probe inventory, target I/O boundary, and clocks/barriers are controlled.
No policy/grant/profile is seeded and no guard or phase handler is replaced.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyocd_debug_mcp.adapters.swd_interface import session_metadata
from pyocd_debug_mcp.kernel.operations import current_operation, operation_manager
from tests.m05_public_support import NULL_PLAN, PublicSetup, child
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    tiered_test_server,
)


class MemoryProbe(DeterministicTargetBackend):
    """Hardware boundary with distinguishable CPUID and scratch memory."""

    def __init__(self):
        super().__init__()
        self.memory = {}

    def open(self, **kwargs):
        handle = super().open(**kwargs)
        return replace(handle, metadata=replace(session_metadata(handle), probe_family="jlink"))

    def read_memory(self, handle, address, width_bits, **kwargs):
        super().read_memory(handle, address, width_bits, **kwargs)
        return 0x410FC241 if address == 0xE000ED00 else self.memory.get(address, 0x12345678)

    def write_memory(self, handle, address, value, width_bits):
        super().write_memory(handle, address, value, width_bits)
        self.memory[address] = value


class PublicLifecycleTests(unittest.TestCase):
    @contextmanager
    def project(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = MemoryProbe()
            server.configure_tiered_test_seams(backend=backend)
            with patch.object(server, "_hardware_inventory", _setup_inventory()):
                pdf = root / "fixture.pdf"
                pdf.write_bytes(
                    b"%PDF-1.4\n% Independent synthetic documentation, no physical claim\n"
                )
                flow = PublicSetup(server)
                route = flow.overview("Independent M05")["routes"][0]
                try:
                    yield root, server, backend, flow, route, pdf
                finally:
                    for board in server.connection_manager.assigned_board_ids():
                        flow.call("disconnect", {"board_id": board})
                    evidence = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"])
                    evidence.mkdir(parents=True, exist_ok=True)
                    imports = [
                        {
                            "module": name,
                            "path": str(Path(module.__file__).resolve()),
                            "sha256": hashlib.sha256(
                                Path(module.__file__).read_bytes()
                            ).hexdigest(),
                        }
                        for name, module in sorted(sys.modules.items())
                        if name.startswith(("pyocd_debug_mcp", "tests.m05_", "tests.test_m05_"))
                        and getattr(module, "__file__", None)
                        and Path(module.__file__).is_file()
                    ]
                    (evidence / (self.id() + ".imports.json")).write_text(
                        json.dumps(imports, indent=2) + "\n"
                    )

    def research(self, flow, route, pdf, *, attached=False, tier="setup-lite"):
        board = route["board_id"]
        if attached:
            flow.call(
                "connect",
                {"board_id": board, "probe_uid": "LITE-ACCEPTANCE-PROBE", "target": "nrf52840"},
            )
        action, plan = flow.accept(route, pdf, "nRF52840-QIAA", tier=tier)
        pending = flow.start(plan)
        self.assertEqual(pending["status"], "setup_research_required", pending)
        self.assertEqual(plan["setup_continuation"]["max_answered_resumptions"], 10)
        self.assertEqual(plan["setup_continuation"]["lifetime_seconds"], 1800)
        return board, action, plan, pending

    def reach_ram(self, flow, board, plan, pending):
        reply = flow.answer(board, pending, flow.target_answer(pending, "nrf52840"))
        self.assertEqual(reply["status"], "setup_continuation_accepted", reply)
        pending = flow.fix(plan)
        self.assertEqual(pending["first_unverified_phase"], "safety_research", pending)
        return pending

    def complete(self, flow, board, plan, pending):
        answer = flow.ram_answer(pending)
        self.assertEqual(flow.answer(board, pending, answer)["accepted"], "lite_confirmation")
        result = flow.fix(plan)
        self.assertEqual(result["status"], "setup_completed", result)
        return answer

    def test_disconnected_real_forms_correctable_answers_and_no_refill(self):
        with self.project() as (root, server, backend, flow, route, pdf):
            board, action, plan, pending = self.research(flow, route, pdf)
            target = flow.target_answer(pending, "nrf52840")
            bad = copy.deepcopy(target)
            del bad["evidence"]
            with self.assertRaisesRegex(Exception, "missing=.*evidence"):
                flow.answer(board, pending, bad)
            self.assertEqual(backend.calls, [])
            flow.answer(board, pending, target)
            with self.assertRaisesRegex(Exception, "already accepted"):
                flow.answer(board, pending, target)
            pending = flow.fix(plan)
            before = list(backend.calls)
            repeated = flow.execute(plan["paired_action_fallbacks"][0]["call"])
            self.assertEqual(repeated["status"], "batch_failed", repeated)
            self.assertEqual(backend.calls, before)
            active = server.plan_engine.active_plan("board_setup", board)
            self.assertEqual(
                (active.max_calls, active.max_calls_buffer, active.remaining_calls), (1, 0, 0)
            )
            self.assertEqual(dict(active.paired_remaining), {"board_fix_setup": 0})
            answer = flow.ram_answer(pending)
            del answer["regions"][0]["source_note"]
            with self.assertRaisesRegex(Exception, r"regions\[0\].source_note"):
                flow.answer(board, pending, answer)
            confirmed = self.complete(flow, board, plan, pending)
            self.assertEqual(action["target_tier"], "setup-lite")
            self.assertEqual(action["mcu_part_number"], "nRF52840-QIAA")
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertIsNone(server.permission_store.active_grant("board_setup", board))
            with self.assertRaisesRegex(Exception, "closed"):
                flow.answer(board, pending, confirmed)
            self.assertFalse(
                any(c[0] in {"flash", "recover", "write_memory"} for c in backend.calls)
            )
            self.assertEqual(
                flow.json("get_capabilities", {"board_id": board})["tier"], "setup-lite"
            )

    def test_attached_session_unchanged_and_immutable_fallback_fields(self):
        with self.project() as (_, server, backend, flow, route, pdf):
            board, action, plan, pending = self.research(flow, route, pdf, attached=True)
            session = server._active_session_id(board)
            self.assertEqual(
                server.plan_engine.active_plan("board_setup", board).session_id, session
            )
            target = flow.target_answer(pending, "nrf52840")
            with self.assertRaisesRegex(Exception, "Omit optional attachment fields"):
                flow.answer(
                    board,
                    pending,
                    {
                        **target,
                        "debug_protocol": "swd",
                        "debug_connect_mode": "halt",
                        "debug_clock_hz": 1000000,
                    },
                )
            flow.answer(board, pending, target)
            for key, value in (
                ("mcu_part_number", "nRF52832-QFAA"),
                ("target_tier", "setup-full"),
                ("connection_id", "changed-probe"),
            ):
                changed = copy.deepcopy(plan["paired_action_fallbacks"][0]["call"])
                changed["arguments"]["actions"][0]["arguments"][key] = value
                before = list(backend.calls)
                self.assertEqual(flow.execute(changed)["status"], "batch_failed")
                self.assertEqual(backend.calls, before)
            pending = flow.fix(plan)
            self.complete(flow, board, plan, pending)
            self.assertEqual(server._active_session_id(board), session)
            self.assertEqual([c[0] for c in backend.calls].count("open"), 1)
            self.assertNotIn("close", [c[0] for c in backend.calls])
            self.assertEqual(
                action["connection_id"], route["plan_action_parameters_template"]["connection_id"]
            )

    def test_replace_replay_and_stale_callback_preserve_new_authority(self):
        with self.project() as (_, server, backend, flow, route, pdf):
            board, _, old_plan, old = self.research(flow, route, pdf)
            _, replacement = flow.accept(route, pdf, "nRF52840-QIAA")
            current = flow.start(replacement)
            grant = server.permission_store.active_grant("board_setup", board)
            # Deliver an old completion callback after the replacement exists.
            # This calls the real callback; no callback/guard implementation is mocked.
            server._setup_workflow.on_allowance_closed(
                board, old_plan["plan_id"], "late old completion"
            )
            self.assertEqual(
                server.plan_engine.active_plan("board_setup", board).plan_id, replacement["plan_id"]
            )
            self.assertEqual(server.setup_tool_loader.allowance_for(board), replacement["plan_id"])
            self.assertIs(server.permission_store.active_grant("board_setup", board), grant)
            with self.assertRaisesRegex(Exception, "closed"):
                flow.answer(board, old, flow.target_answer(old, "nrf52840"))
            current = self.reach_ram(flow, board, replacement, current)
            self.complete(flow, board, replacement, current)
            self.assertFalse(
                any(c[0] in {"flash", "recover", "write_memory"} for c in backend.calls)
            )

    def test_disconnect_reconnect_rejects_old_session_without_new_effect(self):
        with self.project() as (_, server, backend, flow, route, pdf):
            board, _, plan, pending = self.research(flow, route, pdf, attached=True)
            session = server._active_session_id(board)
            flow.call("disconnect", {"board_id": board})
            flow.call(
                "connect",
                {"board_id": board, "probe_uid": "LITE-ACCEPTANCE-PROBE", "target": "nrf52840"},
            )
            self.assertNotEqual(server._active_session_id(board), session)
            before = list(backend.calls)
            with self.assertRaisesRegex(Exception, "closed"):
                flow.answer(board, pending, flow.target_answer(pending, "nrf52840"))
            self.assertEqual(
                flow.execute(plan["paired_action_fallbacks"][0]["call"])["status"], "batch_failed"
            )
            self.assertEqual(backend.calls, before)

    def test_expiry_from_plan_acceptance_before_first_answer(self):
        with self.project() as (_, server, backend, flow, route, pdf):
            board, _, plan, pending = self.research(flow, route, pdf)
            with patch("pyocd_debug_mcp.guardrails.plan_engine.time") as clock:
                clock.monotonic.return_value = time.monotonic() + 1801
                with self.assertRaisesRegex(Exception, "expired"):
                    flow.answer(board, pending, flow.target_answer(pending, "nrf52840"))
            self.assertEqual(backend.calls, [])
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertIsNone(server.permission_store.active_grant("board_setup", board))

    def test_revoked_pending_plan_cannot_recreate_authority(self):
        with self.project() as (_, server, backend, flow, route, pdf):
            board, _, plan, pending = self.research(flow, route, pdf)
            # Exercise the real user revocation API, never edit the grant.
            server.permission_store.revoke("board_setup", board, reason="operator withdrawal")
            with self.assertRaisesRegex(Exception, "closed"):
                flow.answer(board, pending, flow.target_answer(pending, "nrf52840"))
            self.assertEqual(flow.execute(plan["stable_client_fallback"])["status"], "batch_failed")
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertEqual(backend.calls, [])

    def test_actual_request_cancellation_at_hardware_inventory_barrier(self):
        for at_fix in (False, True):
            with (
                self.subTest(at_fix=at_fix),
                self.project() as (_, server, backend, flow, route, pdf),
            ):
                if at_fix:
                    board, _, plan, pending = self.research(flow, route, pdf)
                    flow.answer(board, pending, flow.target_answer(pending, "nrf52840"))
                    selected_call = plan["paired_action_fallbacks"][0]["call"]
                else:
                    board = route["board_id"]
                    _, plan = flow.accept(route, pdf, "nRF52840-QIAA")
                    selected_call = plan["stable_client_fallback"]
                reached, release = threading.Event(), threading.Event()
                request_ids = []
                original_snapshot = server._hardware_inventory.validation_inventory

                def paused_snapshot():
                    request_ids.append(current_operation().request_id)
                    reached.set()
                    if not release.wait(10):
                        raise AssertionError("test barrier was not released")
                    return original_snapshot()

                before = list(backend.calls)
                with patch.object(
                    server._hardware_inventory, "validation_inventory", paused_snapshot
                ):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pending_call = pool.submit(flow.execute, selected_call)
                        try:
                            self.assertTrue(
                                reached.wait(10), "public request never reached inventory"
                            )
                            self.assertGreaterEqual(
                                operation_manager.cancel_request(request_ids[0]), 1
                            )
                        finally:
                            release.set()
                        result = pending_call.result(timeout=15)
                self.assertEqual(result["status"], "batch_failed", result)
                self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
                self.assertIsNone(server.setup_tool_loader.allowance_for(board))
                self.assertIsNone(server.permission_store.active_grant("board_setup", board))
                self.assertEqual(backend.calls, before)

    def test_new_lite_prompt_is_answerable_after_research_fix(self):
        """Sensitivity oracle for the original close-on-first-paired-fix defect."""
        with self.project() as (_, server, _, flow, route, pdf):
            board, _, plan, pending = self.research(flow, route, pdf)
            pending = self.reach_ram(flow, board, plan, pending)
            try:
                reply = flow.answer(board, pending, flow.ram_answer(pending))
            except Exception as exc:
                self.fail(f"The new advertised LITE prompt must remain answerable: {exc}")
            self.assertEqual(reply["accepted"], "lite_confirmation")
            self.assertEqual(flow.fix(plan)["status"], "setup_completed")

    def test_completed_partial_lite_keeps_guarded_ranges_and_raw_uart_warning(self):
        with self.project() as (_, server, backend, flow, route, pdf):
            board, _, plan, pending = self.research(flow, route, pdf, attached=True)
            pending = self.reach_ram(flow, board, plan, pending)
            self.complete(flow, board, plan, pending)
            for action in ("read_memory_address", "write_memory"):
                flow.call(action + "-plan", NULL_PLAN)

            def execute(action, args):
                accepted = flow.plan(action, board, args)
                self.assertEqual(accepted["status"], "plan_accepted", accepted)
                return flow.execute(accepted["stable_client_fallback"])

            read = {"address": 0x20000000, "width": 32, "length": None}
            write = {
                "symbol_or_address": 0x20000000,
                "value": 0x87654321,
                "width": 32,
                "allow_address_fallback": True,
                "reason": "Independent synthetic RAM word",
                "elf_artifact": None,
            }
            self.assertEqual(child(execute("read_memory_address", read))["result"], "0x12345678")
            self.assertEqual(execute("write_memory", write)["status"], "batch_completed")
            self.assertEqual(child(execute("read_memory_address", read))["result"], "0x87654321")
            self.assertEqual(
                execute("write_memory", {**write, "value": 0x12345678})["status"], "batch_completed"
            )
            for action, args in (
                ("read_memory_address", {**read, "address": 0x20001000}),
                ("read_memory_address", {**read, "address": 0x20000FFE}),
                ("write_memory", {**write, "symbol_or_address": 0x20001000}),
                ("write_memory", {**write, "symbol_or_address": 0}),
            ):
                with self.subTest(action=action, parameters=args):
                    before = list(backend.calls)
                    result = execute(action, args)
                    self.assertEqual(result["status"], "batch_failed", result)
                    self.assertEqual(child(result)["status"], "refused", result)
                    self.assertEqual(backend.calls, before)
            capabilities = flow.json("get_capabilities", {"board_id": board})
            families = {row["family"]: row for row in capabilities["capabilities"]}
            self.assertEqual(capabilities["tier"], "setup-lite")
            self.assertEqual(capabilities["identity"]["assertion"], "trusted-not-proven")
            for family in ("flash-application", "flash-bootloader", "target-recovery"):
                self.assertFalse(families[family]["safe"], families[family])
            self.assertEqual(families["target-recovery"]["recovery_mode"], "unavailable")
            with patch.object(
                server,
                "capture_uart_output",
                return_value=SimpleNamespace(matched=False, text="", duration_seconds=0.01),
            ):
                raw = flow.json(
                    "read_serial_raw",
                    {
                        "board_id": board,
                        "port": "synthetic-uart",
                        "baudrate": 115200,
                        "read_seconds": 0.01,
                    },
                )
            self.assertEqual(raw["status"], "ok", raw)
            self.assertIn("warning", raw)
            self.assertIn("bypass", str(raw["warning"]).lower())
            self.assertFalse(any(c[0] in {"flash", "recover"} for c in backend.calls))

    def test_advertised_post_lite_validation_works_before_and_after_restart(self):
        """C01: follow actual advertised next_call; never promote LITE to obtain validation."""
        with self.project() as (root, server, _, flow, route, pdf):
            board, _, plan, pending = self.research(flow, route, pdf)
            pending = self.reach_ram(flow, board, plan, pending)
            self.complete(flow, board, plan, pending)
            for restarted in (False, True):
                with self.subTest(restarted=restarted):
                    if not restarted:
                        loaded = flow.json(
                            "load_setup_tool", {"board_id": board, "tool_name": "board_validate"}
                        )
                        advertised = loaded["next_call"]
                        try:
                            result = flow.json(advertised["tool"], advertised["arguments"])
                            outcome = {
                                "usable": result.get("status")
                                not in {
                                    "refused",
                                    "validation_failed",
                                    "validation_blocked",
                                    "validation_incomplete",
                                },
                                "result": result,
                            }
                        except Exception as exc:
                            outcome = {"usable": False, "error": str(exc), "next_call": advertised}
                    else:
                        code = """import json, os
from pathlib import Path
from unittest.mock import patch
from tests.tiered_acceptance_support import tiered_test_server
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.test_m05_public_lifecycle import MemoryProbe
from tests.m05_public_support import PublicSetup
with tiered_test_server(Path(os.environ['BYO_MCP_ARTIFACT_ROOT'])) as server:
 server.configure_tiered_test_seams(backend=MemoryProbe())
 with patch.object(server, '_hardware_inventory', _setup_inventory()):
  flow = PublicSetup(server)
  board = os.environ['M05_RESTART_BOARD']
  route = flow.overview('Independent M05')['routes'][0]
  call = route['next_call']
  flow.call(call['tool'], call['arguments'])
  loaded = flow.json('load_setup_tool', {'board_id': board, 'tool_name': 'board_validate'})
  call = loaded['next_call']
  try:
   result = flow.json(call['tool'], call['arguments'])
   outcome = {'usable': result.get('status') not in {'refused', 'validation_failed', 'validation_blocked', 'validation_incomplete'}, 'result': result}
  except Exception as exc:
   outcome = {'usable': False, 'error': str(exc), 'next_call': call}
  outcome['capabilities'] = flow.json('get_capabilities', {'board_id': board})
  outcome['pid'] = os.getpid()
  print(json.dumps(outcome))
  flow.call('disconnect', {'board_id': board})
"""
                        proc = subprocess.run(
                            [sys.executable, "-c", code],
                            capture_output=True,
                            text=True,
                            timeout=45,
                            env={
                                **os.environ,
                                "BYO_MCP_ARTIFACT_ROOT": str(root),
                                "BYO_MCP_MONITOR_ROOT": str(root / "restart-validation-monitor"),
                                "M05_RESTART_BOARD": board,
                            },
                        )
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        outcome = json.loads(proc.stdout)
                        self.assertNotEqual(outcome["pid"], os.getpid())
                        self.assertEqual(outcome["capabilities"]["tier"], "setup-lite")
                        self.assertEqual(
                            outcome["capabilities"]["identity"]["assertion"], "trusted-not-proven"
                        )
                    evidence = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"])
                    evidence.mkdir(parents=True, exist_ok=True)
                    (evidence / f"C01-restarted-{restarted}.json").write_text(
                        json.dumps(outcome, indent=2) + "\n"
                    )
                    self.assertTrue(
                        outcome["usable"],
                        "The advertised post-LITE route must work without changing tier: "
                        + str(outcome.get("error", outcome.get("result"))),
                    )

    def test_full_installed_control_and_m03_pending_stop(self):
        with self.project() as (_, server, backend, flow, route, pdf):
            board, _, plan, pending = self.research(flow, route, pdf, tier="setup-full")
            flow.answer(board, pending, flow.target_answer(pending, "nrf52840"))
            completed = flow.fix(plan)
            self.assertEqual(completed["status"], "setup_completed", completed)
            self.assertEqual(
                flow.json("get_capabilities", {"board_id": board})["tier"], "setup-full"
            )
            self.assertFalse(
                any(c[0] in {"flash", "recover", "write_memory"} for c in backend.calls)
            )
        with self.project() as (_, server, backend, flow, route, pdf):
            _, plan = flow.accept(route, pdf, "nRF52840-QIAA")
            batch = copy.deepcopy(plan["stable_client_fallback"])
            batch["arguments"]["actions"].append(
                {
                    "tool_name": "write_memory_raw",
                    "arguments": {"board_id": route["board_id"], "address": 0x20000000, "value": 7},
                }
            )
            result = flow.execute(batch)
            self.assertEqual(result["status"], "batch_pending", result)
            self.assertEqual(result["completed"], [])
            self.assertEqual(child(result)["status"], "setup_research_required")
            self.assertEqual(backend.calls, [])

    def test_fresh_process_keeps_lite_and_drops_setup_authority(self):
        with self.project() as (root, server, _, flow, route, pdf):
            board, _, plan, pending = self.research(flow, route, pdf)
            pending = self.reach_ram(flow, board, plan, pending)
            self.complete(flow, board, plan, pending)
            env = {
                **os.environ,
                "BYO_MCP_ARTIFACT_ROOT": str(root),
                "BYO_MCP_MONITOR_ROOT": str(root / "restart-monitor"),
            }
            code = """import json, os
from pyocd_debug_mcp import server
from tests.m05_public_support import PublicSetup
flow = PublicSetup(server)
board = os.environ['M05_RESTART_BOARD']
print(json.dumps({'pid': os.getpid(), 'capabilities': flow.json('get_capabilities', {'board_id': board}),
 'plan': server.plan_engine.active_plan('board_setup', board),
 'allowance': server.setup_tool_loader.allowance_for(board),
 'grant': server.permission_store.active_grant('board_setup', board)}))
"""
            proc = subprocess.run(
                [sys.executable, "-c", code],
                env={**env, "M05_RESTART_BOARD": board},
                capture_output=True,
                text=True,
                timeout=45,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertNotEqual(result["pid"], os.getpid())
            self.assertEqual(result["capabilities"]["tier"], "setup-lite")
            self.assertEqual(result["capabilities"]["identity"]["assertion"], "trusted-not-proven")
            self.assertIsNone(result["plan"])
            self.assertIsNone(result["allowance"])
            self.assertIsNone(result["grant"])


if __name__ == "__main__":
    unittest.main()
