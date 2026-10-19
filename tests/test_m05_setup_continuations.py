"""M05 implementation checks: public, bounded setup with synthetic hardware only."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from pyocd_debug_mcp.adapters.swd_interface import session_metadata
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.guardrails.plan_defs import (
    SETUP_CONTINUATION_LIMIT,
    SETUP_CONTINUATION_SECONDS,
)
from pyocd_debug_mcp.kernel.operations import OperationCancelledError

from tests.test_tiered_acceptance_lite import (
    _accepted_setup_plan,
    _batch_payload,
    _child_setup_response,
    _minimal_local_pdf,
    _setup_inventory,
)
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    fixture,
    isolated_project,
    public_text,
    tiered_test_server,
)


class _Backend(DeterministicTargetBackend):
    def open(self, **kwargs):
        handle = super().open(**kwargs)
        return replace(handle, metadata=replace(session_metadata(handle), probe_family="jlink"))


class SetupContinuationTests(unittest.TestCase):
    @contextmanager
    def project(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = _Backend(read_value=0x410FC241)
            server.configure_tiered_test_seams(backend=backend)
            with (
                patch.object(server, "_hardware_inventory", _setup_inventory()),
                patch.object(
                    server, "_built_in_target_names", return_value={"nrf52840", "cortex_m"}
                ),
            ):
                yield root, server, backend
            for board in server.connection_manager.assigned_board_ids():
                server.disconnect(board)

    def plan(self, root, server, *, attached=False, target="nrf52840"):
        if attached:
            overview = json.loads(
                public_text(server, "setup_overview", {"board_names": ["M05 board"]})
            )
            board = overview["routes"][0]["board_id"]
            public_text(
                server,
                "connect",
                {"board_id": board, "probe_uid": "LITE-ACCEPTANCE-PROBE", "target": target},
            )
        return _accepted_setup_plan(
            self,
            server,
            display_name="M05 board",
            target_tier="setup-lite",
            datasheet=_minimal_local_pdf(root, "local.pdf"),
            mcu_part_number="nRF52840-QIAA",
        )

    def batch(self, server, call):
        return _batch_payload(public_text(server, call["tool_name"], call["arguments"]))

    def pending(self, server, plan):
        batch = self.batch(server, plan["stable_client_fallback"])
        self.assertEqual(batch["status"], "batch_pending", batch)
        response = _child_setup_response(batch)
        self.assertEqual(response["status"], "setup_research_required", response)
        return response

    def reply(self, server, board, pending, response):
        return json.loads(
            public_text(
                server,
                "continue_setup",
                {
                    "board_id": board,
                    "continuation_id": pending["continuation_id"],
                    "response": response,
                },
            )
        )

    def target_reply(self, pending):
        alternative = pending["accepted_response"]["alternatives"][0]
        return {
            **alternative["response"],
            "pyocd_target": alternative["eligible_targets"][0],
            "evidence": [
                {
                    "source": "Synthetic official-support fixture",
                    "claim": "Installed target supports the exact supplied part.",
                }
            ],
            "reasoning_summary": "Host fixture establishes compatibility; CPUID is not exact package proof.",
        }

    def lite_reply(self):
        return {
            key: value
            for key, value in fixture("lite_confirmation.json").items()
            if key != "schema_version"
        }

    def test_two_research_steps_correctable_answers_completion_and_restart(self):
        with self.project() as (root, server, backend):
            board, action, plan = self.plan(root, server)
            pending = self.pending(server, plan)
            target = self.target_reply(pending)
            incomplete = copy.deepcopy(target)
            incomplete.pop("reasoning_summary")
            with self.assertRaisesRegex(Exception, "missing=.*reasoning_summary"):
                self.reply(server, board, pending, incomplete)
            self.assertEqual(backend.calls, [])
            self.assertEqual(
                self.reply(server, board, pending, target)["status"], "setup_continuation_accepted"
            )
            calls = list(backend.calls)
            with self.assertRaisesRegex(Exception, "already accepted"):
                self.reply(server, board, pending, target)
            self.assertEqual(backend.calls, calls)
            fix = plan["paired_action_fallbacks"][0]["call"]
            drifted = copy.deepcopy(fix)
            drifted["arguments"]["actions"][0]["arguments"]["mcu_part_number"] = "another-part"
            self.assertEqual(self.batch(server, drifted)["status"], "batch_failed")
            self.assertEqual(backend.calls, calls)
            second = _child_setup_response(self.batch(server, fix))
            self.assertEqual(second["status"], "setup_research_required", second)
            self.assertEqual(second["first_unverified_phase"], "safety_research")
            active = server.plan_engine.active_plan("board_setup", board)
            self.assertEqual(
                (active.max_calls, active.max_calls_buffer, active.remaining_calls), (1, 0, 0)
            )
            self.assertEqual(dict(active.paired_remaining), {"board_fix_setup": 0})
            calls = list(backend.calls)
            self.assertEqual(self.batch(server, fix)["status"], "batch_failed")
            self.assertEqual(backend.calls, calls)
            bad_map = self.lite_reply()
            bad_map["regions"][0].pop("source_note")
            with self.assertRaisesRegex(Exception, "source_note"):
                self.reply(server, board, second, bad_map)
            self.assertEqual(
                self.reply(server, board, second, self.lite_reply())["accepted"],
                "lite_confirmation",
            )
            completed = _child_setup_response(self.batch(server, fix))
            self.assertEqual(completed["status"], "setup_completed", completed)
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertEqual(CapabilityPolicyRepository(root).resolve(board).tier, Tier.SETUP_LITE)
            self.assertEqual(action["mcu_part_number"], "nRF52840-QIAA")
            with self.assertRaisesRegex(Exception, "closed"):
                self.reply(server, board, second, self.lite_reply())
            restarted = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from tests.tiered_acceptance_support import public_text; "
                    "from pyocd_debug_mcp import server; "
                    f"print(public_text(server, 'get_capabilities', {{'board_id': {board!r}}}))",
                ],
                env={
                    **os.environ,
                    "BYO_MCP_ARTIFACT_ROOT": str(root),
                    "BYO_MCP_MONITOR_ROOT": str(root / "restarted-monitor"),
                },
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            capabilities = json.loads(restarted.stdout)
            self.assertEqual(capabilities["tier"], "setup-lite")
            self.assertEqual(capabilities["identity"]["assertion"], "trusted-not-proven")

    def test_attached_plan_keeps_actual_session_and_reuses_same_target(self):
        with self.project() as (root, server, backend):
            board, _, plan = self.plan(root, server, attached=True)
            session_id = server._active_session_id(board)
            self.assertIsNotNone(session_id)
            self.assertEqual(
                server.plan_engine.active_plan("board_setup", board).session_id, session_id
            )
            pending = self.pending(server, plan)
            self.reply(server, board, pending, self.target_reply(pending))
            fix = plan["paired_action_fallbacks"][0]["call"]
            second = _child_setup_response(self.batch(server, fix))
            self.assertEqual(second["status"], "setup_research_required", second)
            self.reply(server, board, second, self.lite_reply())
            completed = _child_setup_response(self.batch(server, fix))
            self.assertEqual(completed["status"], "setup_completed", completed)
            self.assertEqual(server._active_session_id(board), session_id)
            self.assertEqual(sum(call[0] == "open" for call in backend.calls), 1)
            self.assertFalse(any(call[0] == "close" for call in backend.calls))

    def test_replacement_closes_old_continuation_before_new_action(self):
        with self.project() as (root, server, backend):
            board, _, plan = self.plan(root, server)
            old = self.pending(server, plan)
            _, _, replacement = self.plan(root, server)
            with self.assertRaisesRegex(Exception, "closed"):
                self.reply(server, board, old, self.target_reply(old))
            self.assertEqual(
                server.plan_engine.active_plan("board_setup", board).plan_id, replacement["plan_id"]
            )
            self.assertEqual(backend.calls, [])
            self.pending(server, replacement)

    def test_incompatible_attached_target_requires_disconnect_before_plan_acceptance(self):
        with self.project() as (root, server, backend):
            with self.assertRaisesRegex(Exception, "Disconnect before submitting"):
                self.plan(root, server, attached=True, target="cortex_m")
            board = server.connection_manager.assigned_board_ids()[0]
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertEqual(sum(call[0] == "open" for call in backend.calls), 1)

    def test_full_disconnected_installed_target_completes_with_owned_session_binding(self):
        with self.project() as (root, server, backend):
            board, _, plan = _accepted_setup_plan(
                self,
                server,
                display_name="M05 full board",
                target_tier="setup-full",
                datasheet=_minimal_local_pdf(root, "full-local.pdf"),
                mcu_part_number="nRF52840-QIAA",
            )
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board).session_id)
            pending = self.pending(server, plan)
            self.reply(server, board, pending, self.target_reply(pending))
            completed = _child_setup_response(
                self.batch(server, plan["paired_action_fallbacks"][0]["call"])
            )
            self.assertEqual(completed["status"], "setup_completed", completed)
            self.assertIsNotNone(server._active_session_id(board))
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertEqual(CapabilityPolicyRepository(root).resolve(board).tier, Tier.SETUP_FULL)
            self.assertFalse(
                any(call[0] in {"write_memory", "flash", "recover"} for call in backend.calls)
            )

    def test_pending_research_in_batch_never_dispatches_dependent_raw_write(self):
        with self.project() as (root, server, backend):
            board, _, plan = self.plan(root, server)
            call = copy.deepcopy(plan["stable_client_fallback"])
            call["arguments"]["actions"].append(
                {
                    "tool_name": "write_memory_raw",
                    "arguments": {"board_id": board, "address": 0x20000000, "value": 123},
                }
            )
            batch = self.batch(server, call)
            self.assertEqual(batch["status"], "batch_pending", batch)
            self.assertEqual(batch["completed"], [])
            self.assertEqual(batch["failure"]["tool_name"], "board_setup")
            self.assertEqual(backend.calls, [])

    def test_expiry_closes_workflow_loader_and_plan_before_research_io(self):
        with self.project() as (root, server, backend):
            board, _, plan = self.plan(root, server)
            pending = self.pending(server, plan)
            # Replace the module's clock object, not the global time module used
            # by asyncio/AnyIO to deliver the public MCP response.
            with patch("pyocd_debug_mcp.setup_flow.setup.time") as clock:
                clock.monotonic.return_value = time.monotonic() + SETUP_CONTINUATION_SECONDS + 1
                with self.assertRaisesRegex(Exception, "expired"):
                    self.reply(server, board, pending, self.target_reply(pending))
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertEqual(backend.calls, [])
            self.assertIsNone(server.permission_store.active_grant("board_setup", board))

    def test_revocation_closes_pending_authority_and_research_facts(self):
        with self.project() as (root, server, backend):
            board, _, plan = self.plan(root, server)
            pending = self.pending(server, plan)
            self.reply(server, board, pending, self.target_reply(pending))
            self.assertIn(board, server._setup_target_overrides)
            server.permission_store.revoke(
                "board_setup", board, reason="host fixture user revoked setup"
            )
            calls = list(backend.calls)
            with self.assertRaisesRegex(Exception, "closed"):
                self.reply(server, board, pending, self.target_reply(pending))
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertNotIn(board, server._setup_target_overrides)
            self.assertEqual(backend.calls, calls)

    def test_plan_expiry_is_measured_from_acceptance_before_workflow_expiry(self):
        with self.project() as (root, server, backend):
            board, _, plan = self.plan(root, server)
            pending = self.pending(server, plan)
            with patch("pyocd_debug_mcp.guardrails.plan_engine.time") as clock:
                clock.monotonic.return_value = time.monotonic() + SETUP_CONTINUATION_SECONDS + 1
                with self.assertRaisesRegex(Exception, "expired"):
                    self.reply(server, board, pending, self.target_reply(pending))
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertEqual(backend.calls, [])
            self.assertIsNone(server.permission_store.active_grant("board_setup", board))

    def test_unrelated_live_session_change_refuses_before_answer_io(self):
        with self.project() as (root, server, backend):
            board, _, plan = self.plan(root, server, attached=True)
            pending = self.pending(server, plan)
            calls = list(backend.calls)
            with patch.object(server, "_active_session_id", return_value="unrelated-session"):
                with self.assertRaisesRegex(Exception, "session changed"):
                    self.reply(server, board, pending, self.target_reply(pending))
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertEqual(backend.calls, calls)
            self.assertIsNone(server.permission_store.active_grant("board_setup", board))

    def test_cancelled_fix_closes_reserved_one_time_permission(self):
        with self.project() as (root, server, backend):
            board, _, plan = self.plan(root, server)
            pending = self.pending(server, plan)
            self.reply(server, board, pending, self.target_reply(pending))
            calls = list(backend.calls)
            with patch.object(
                server._setup_workflow,
                "cancellation_checkpoint",
                side_effect=OperationCancelledError("cancelled fix"),
            ):
                self.assertEqual(
                    self.batch(server, plan["paired_action_fallbacks"][0]["call"])["status"],
                    "batch_failed",
                )
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertEqual(backend.calls, calls)

    def test_disconnect_closes_attached_continuation_and_reconnect_cannot_replay(self):
        with self.project() as (root, server, backend):
            board, _, plan = self.plan(root, server, attached=True)
            pending = self.pending(server, plan)
            public_text(server, "disconnect", {"board_id": board})
            public_text(
                server,
                "connect",
                {"board_id": board, "probe_uid": "LITE-ACCEPTANCE-PROBE", "target": "nrf52840"},
            )
            calls = list(backend.calls)
            with self.assertRaisesRegex(Exception, "closed"):
                self.reply(server, board, pending, self.target_reply(pending))
            self.assertEqual(backend.calls, calls)

    def test_cancellation_at_first_checkpoint_closes_exact_allowance(self):
        with self.project() as (root, server, backend):
            board, _, plan = self.plan(root, server)
            with patch.object(
                server._setup_workflow,
                "cancellation_checkpoint",
                side_effect=OperationCancelledError("cancelled"),
            ):
                self.assertEqual(
                    self.batch(server, plan["stable_client_fallback"])["status"], "batch_failed"
                )
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertEqual(backend.calls, [])

    def test_answered_resume_budget_ends_without_refilling_plan_calls(self):
        with self.project() as (root, server, backend):
            board, _, plan = self.plan(root, server)
            pending = self.pending(server, plan)
            fix = plan["paired_action_fallbacks"][0]["call"]
            # A fallible resolver repeatedly loses its staged result. Each fresh,
            # validated answer can resume, but no new plan/grant is manufactured.
            with patch.object(server, "_setup_inventory", wraps=server._setup_inventory):
                for index in range(SETUP_CONTINUATION_LIMIT):
                    server._setup_target_overrides.pop(board, None)
                    server._setup_builtin_candidates.pop(board, None)
                    self.reply(server, board, pending, self.target_reply(pending))
                    server._setup_target_overrides.pop(board, None)
                    server._setup_builtin_candidates.pop(board, None)
                    result = _child_setup_response(self.batch(server, fix))
                    if index < SETUP_CONTINUATION_LIMIT - 1:
                        self.assertEqual(result["status"], "setup_research_required", result)
                        pending = result
                    else:
                        self.assertEqual(result["status"], "setup_blocked", result)
            self.assertIsNone(server.plan_engine.active_plan("board_setup", board))
            self.assertIsNone(server.setup_tool_loader.allowance_for(board))
            self.assertEqual(CapabilityPolicyRepository(root).resolve(board).tier, Tier.NO_SETUP)


if __name__ == "__main__":
    unittest.main()
