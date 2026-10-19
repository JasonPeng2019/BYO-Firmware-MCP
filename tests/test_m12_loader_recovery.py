"""Fresh independent public continuation tests for the accepted R11 repair.

Reuse the reviewed native I/O boundary, but drive a retained remote inventory,
replacement sessions and two genuinely configured boards through public calls.
No validation, assignment, grant, profile or readiness outcome is substituted.
"""

import copy
import json
import sys
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.probe_inventory import ProbeInfo
from pyocd_debug_mcp.services.connections import parse_probe_connection_id
from tests import test_m12_independent_recovery as hardware
from tests.m05_public_support import PublicSetup
from tests.m05_public_support import NULL_PLAN
from tests.tiered_acceptance_support import tiered_test_server


REMOTE = "remote:127.0.0.1:9"


class LoaderRecoveryTests(unittest.TestCase):
    bench = hardware.IndependentRecoveryTests.bench
    override = hardware.IndependentRecoveryTests.override
    read_plan = hardware.IndependentRecoveryTests.read_plan
    validate = hardware.IndependentRecoveryTests.validate

    def recover(self, f, *, target=hardware.TARGET):
        # The remote remains registered/visible. Its native session actually
        # attempts open and fails at the explicit transport boundary.
        inventory = f.flow.json("setup_overview", {})
        remote_token = next(
            c["connection_id"]
            for c in inventory["connections"]
            if parse_probe_connection_id(c["connection_id"]) == ("remote", REMOTE)
        )
        assigned = f.flow.json(
            "setup_overview",
            {
                "board_names": ["Independent M12 recovery"],
                "connection_assignments": {"Independent M12 recovery": remote_token},
            },
        )
        self.assertEqual(assigned["routes"][0]["board_id"], f.board)
        f.selected_uid, f.selected_family = REMOTE, "remote"
        f.selected_target = hardware.TARGET
        f.fail_open = True
        before = len(f.sessions)
        with self.assertRaisesRegex(
            Exception, "controlled exceptional transport failure"
        ) as caught:
            f.flow.call("connect", {"board_id": f.board})
        self.assertEqual(len(f.sessions), before + 1)
        self.assertTrue(f.sessions[-1].closed)
        self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))
        f.failure = str(caught.exception)
        f.fail_open = False
        f.selected_uid, f.selected_family = hardware.UID, "jlink"
        f.selected_target = target
        if not any(row.uid == hardware.UID for row in f.rows):
            f.rows.append(ProbeInfo(hardware.UID, "Independent local recovery", "", family="jlink"))
        inventory = f.flow.json("setup_overview", {})
        self.assertEqual(
            {row["probe_family"] for row in inventory["connections"]}, {"remote", "jlink"}
        )
        plan = self.override(f, target=target)
        self.assertEqual(
            f.flow.execute(plan["stable_client_fallback"])["status"], "batch_completed"
        )
        self.assertEqual(f.profile.read_bytes(), f.original)
        return plan

    def assert_read_blocked(self, f, *, board=None):
        before = list(f.hardware)
        if board is not None:
            f.flow.call("read_memory_address-plan", NULL_PLAN)
        plan = (
            self.read_plan(f)
            if board is None
            else f.flow.plan(
                "read_memory_address",
                board,
                {"address": hardware.RAM, "width": 32, "length": None},
            )
        )
        denied = f.flow.execute(plan["stable_client_fallback"])
        self.assertEqual(denied["status"], "batch_failed", denied)
        self.assertEqual(f.hardware, before, "refused protected access reached native hardware")

    def exact_loader_validation(self, f):
        loaded = f.flow.json(
            "load_setup_tool", {"board_id": f.board, "tool_name": "board_validate"}
        )
        self.assertEqual(loaded["status"], "setup_tool_loaded", loaded)
        returned = copy.deepcopy(loaded["next_call"])
        # Follow exactly what the server supplied, including its opaque token.
        result = f.flow.json(returned["tool"], returned["arguments"])
        self.assertEqual(
            result["status"],
            "validation_passed",
            "loader-returned post-override validation must succeed: " + json.dumps(result),
        )
        return returned

    def assert_read_word(self, f):
        result = f.flow.execute(self.read_plan(f)["stable_client_fallback"])
        self.assertEqual(hardware.batch_word(result), hardware.WORD)

    def test_retained_remote_outage_exact_loader_call_enables_only_validated_read(self):
        with self.bench(initial_provider="remote", initial_uid=REMOTE) as f:
            self.recover(f)
            self.assert_read_blocked(f)
            returned = self.exact_loader_validation(f)
            self.assertTrue(
                f.flow.json("get_setup_status", {"board_id": f.board})["ready_for_code"]
            )
            self.assert_read_word(f)
            self.assertNotEqual(returned["arguments"]["probe_id"], "probeid:remote:" + REMOTE)
            self.assertTrue(
                f.flow.json("get_setup_status", {"board_id": f.board})["ready_for_code"]
            )
            self.assertEqual(f.profile.read_bytes(), f.original)

    def test_same_probe_token_replacement_needs_new_validation_and_new_read_plan(self):
        with self.bench(initial_provider="remote", initial_uid=REMOTE) as f:
            self.recover(f)
            old_call = self.exact_loader_validation(f)
            old_session = f.server.connection_manager.connection_for(f.board)
            old_read = self.read_plan(f)["stable_client_fallback"]
            f.flow.call("disconnect", {"board_id": f.board})
            self.recover(f)
            current = f.server.connection_manager.connection_for(f.board)
            self.assertIsNot(current, old_session)
            self.assertTrue(old_session.handle.session.closed)
            self.assertIsNone(f.server.gate_manager.live_identity(f.board))
            before = list(f.hardware)
            self.assertEqual(f.flow.execute(old_read)["status"], "batch_failed")
            self.assertEqual(f.hardware, before)
            self.assert_read_blocked(f)
            new_call = self.exact_loader_validation(f)
            self.assertEqual(new_call, old_call, "same probe token is deliberately reused")
            # A newly reviewed same-parameter plan is current authority. Its
            # fallback has no old plan ID, so validate then use that new grant.
            self.assert_read_word(f)
            self.assertEqual(f.profile.read_bytes(), f.original)

    def test_other_full_board_cannot_borrow_recovery_selector_or_readiness(self):
        with self.bench(initial_provider="remote", initial_uid=REMOTE) as f:
            remote_b = "remote:127.0.0.1:10"
            f.rows.append(ProbeInfo(remote_b, "Second configured remote", "", family="remote"))
            inventory = f.flow.json("setup_overview", {})
            token_b = next(
                c["connection_id"]
                for c in inventory["connections"]
                if parse_probe_connection_id(c["connection_id"]) == ("remote", remote_b)
            )
            route = f.flow.json(
                "setup_overview",
                {
                    "board_names": ["Second independent FULL board"],
                    "connection_assignments": {"Second independent FULL board": token_b},
                },
            )["routes"][0]
            f.selected_uid, f.selected_family = remote_b, "remote"
            _, setup = f.flow.accept(
                route, f.root / "synthetic.pdf", "nRF52840-QIAA", tier="setup-full"
            )
            pending = f.flow.start(setup)
            self.assertEqual(pending["status"], "setup_research_required", pending)
            other = route["board_id"]
            f.flow.answer(other, pending, f.flow.target_answer(pending, hardware.TARGET))
            self.assertEqual(f.flow.fix(setup)["status"], "setup_completed")
            other_profile = f.server._profile_repository.load(other).source_path
            other_bytes = other_profile.read_bytes()
            f.flow.call("disconnect", {"board_id": other})
            f.flow.json(
                "setup_overview",
                {
                    "board_names": ["Second independent FULL board"],
                    "connection_assignments": {"Second independent FULL board": token_b},
                },
            )
            f.flow.call("connect", {"board_id": other})
            self.assertIsNone(f.server.gate_manager.live_identity(other))
            self.recover(f)
            returned = self.exact_loader_validation(f)
            before = list(f.hardware)
            for board, token in ((other, returned["arguments"]["probe_id"]), (f.board, token_b)):
                f.flow.json("load_setup_tool", {"board_id": board, "tool_name": "board_validate"})
                with self.assertRaisesRegex(Exception, "exact current setup_overview assignment"):
                    f.flow.call("board_validate", {"board_id": board, "probe_id": token})
            self.assertEqual(f.hardware, before)
            self.assertIsNone(f.server.gate_manager.live_identity(other))
            self.assert_read_blocked(f, board=other)
            self.assert_read_word(f)
            self.assertEqual(other_profile.read_bytes(), other_bytes)

    def test_stale_remote_wrong_probe_and_wrong_target_fail_without_read_authority(self):
        with self.bench(initial_provider="remote", initial_uid=REMOTE) as f:
            f.flow.overview("Independent M12 recovery")
            old = f.flow.json(
                "load_setup_tool", {"board_id": f.board, "tool_name": "board_validate"}
            )["next_call"]
            self.recover(f, target="cortex_m")
            loaded = f.flow.json(
                "load_setup_tool", {"board_id": f.board, "tool_name": "board_validate"}
            )["next_call"]
            before = list(f.hardware)
            for token in (old["arguments"]["probe_id"], "probeid:jlink:UNSELECTED"):
                with self.assertRaisesRegex(Exception, "exact current setup_overview assignment"):
                    f.flow.call("board_validate", {"board_id": f.board, "probe_id": token})
            result = f.flow.json(loaded["tool"], loaded["arguments"])
            self.assertEqual(result["status"], "validation_blocked", result)
            self.assertIn("backend target differs", json.dumps(result))
            self.assertEqual(f.hardware, before)
            self.assert_read_blocked(f)
            f.flow.call("disconnect", {"board_id": f.board})
            self.recover(f)
            self.exact_loader_validation(f)
            self.assert_read_word(f)

    def test_fresh_run_does_not_replay_saved_exceptional_readiness_or_calls(self):
        with self.bench(initial_provider="remote", initial_uid=REMOTE) as f:
            self.recover(f)
            returned = self.exact_loader_validation(f)
            read = self.read_plan(f)["stable_client_fallback"]
            before = list(f.hardware)
            try:
                with tiered_test_server(f.root) as fresh:
                    with (
                        patch.object(
                            fresh,
                            "list_connected_probes_detailed",
                            side_effect=AssertionError("real discovery"),
                        ),
                        patch.object(fresh, "_run_cmd", side_effect=AssertionError("real CLI")),
                    ):
                        client = PublicSetup(fresh)
                        client.call("initialization_handshake", {})
                        status = client.json("get_setup_status", {"board_id": f.board})
                        self.assertTrue(status["configuration_ready"], status)
                        self.assertFalse(status["ready_for_code"], status)
                        loader = client.json(
                            "load_setup_tool", {"board_id": f.board, "tool_name": "board_validate"}
                        )
                        self.assertEqual(loader["status"], "setup_assignment_required", loader)
                        stale = client.json(returned["tool"], returned["arguments"])
                        self.assertEqual(stale["status"], "setup_tool_not_loaded", stale)
                        self.assertEqual(client.execute(read)["status"], "batch_failed")
                        self.assertEqual(f.hardware, before)
                        self.assertNotEqual(fresh.server_run.run_id, f.server.server_run.run_id)
                        f.wire.append(
                            {
                                "fresh_run": fresh.server_run.run_id,
                                "status": status,
                                "loader": loader,
                                "stale": stale,
                            }
                        )
                    fresh._monitor.closeout("M12 independent fresh run cleanup")
            finally:
                sys.modules["pyocd_debug_mcp.server"] = f.server
                sys.modules["pyocd_debug_mcp"].server = f.server

    def test_unaffected_normal_full_remote_loader_validate_read_control(self):
        with self.bench(initial_provider="remote", initial_uid=REMOTE) as f:
            f.flow.overview("Independent M12 recovery")
            f.flow.call("connect", {"board_id": f.board})
            self.assert_read_blocked(f)
            loaded = f.flow.json(
                "load_setup_tool", {"board_id": f.board, "tool_name": "board_validate"}
            )
            call = loaded["next_call"]
            result = f.flow.json(call["tool"], call["arguments"])
            self.assertEqual(result["status"], "validation_passed", result)
            self.assert_read_word(f)
            self.assertEqual(f.profile.read_bytes(), f.original)


if __name__ == "__main__":
    unittest.main()
