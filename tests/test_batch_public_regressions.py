"""Independent M03 regressions through registered MCP dispatch, without hardware."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier

# Reuse only setup-input builders; all outcomes below come from real dispatch.
from tests.test_tiered_acceptance_lite import (
    _accepted_setup_plan,
    _minimal_local_pdf,
    _ready_setup_preflight,
    _setup_inventory,
)
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    confirmed_lite_policy,
    fixture,
    isolated_project,
    public_text,
    tiered_test_server,
)


BOARD = "batch_board"
FIRST = 0x20000000
LATER = FIRST + 4


class StatefulBackend(DeterministicTargetBackend):
    """Observe persistent effects, not merely a mocked success reply."""

    def __init__(self) -> None:
        super().__init__()
        self.memory: dict[int, int] = {}
        self.breakpoints: set[int] = set()

    def write_memory(self, handle, address, value, width_bits):
        super().write_memory(handle, address, value, width_bits)
        self.memory[address] = value

    def read_memory(self, handle, address, width_bits, **kwargs):
        super().read_memory(handle, address, width_bits, **kwargs)
        return self.memory.get(address, 0)

    def set_breakpoint(self, handle, address):
        super().set_breakpoint(handle, address)
        if len(self.breakpoints) == 6:
            raise RuntimeError("deterministic comparator exhaustion")
        self.breakpoints.add(address)


def child(name: str, board: str = BOARD, **arguments) -> dict:
    return {"tool_name": name, "arguments": {"board_id": board, **arguments}}


def write_child(board: str = BOARD, *, address: int = FIRST, value: int = 17) -> dict:
    return child("write_memory_raw", board, address=address, value=value)


def body(text: str) -> dict:
    return json.loads(text.split("\n", 1)[0])


def batch(server, actions: list[dict], board: str = BOARD) -> tuple[dict, list[tuple]]:
    # This spy delegates unchanged, including all producer, registry and guard
    # logic. It sees an attempted child even if a route guard rejects it before
    # handler entry or backend I/O. The batch captures mcp.call_tool at import,
    # so spying only on that instance attribute would miss nested dispatches.
    with patch.object(server.mcp, "_call_tool_inner", wraps=server.mcp._call_tool_inner) as spy:
        result = body(public_text(server, "action_batch", {"board_id": board, "actions": actions}))
    return result, [(call.args[0], call.args[1]) for call in spy.call_args_list]


def connect(server, backend) -> None:
    server.configure_tiered_test_seams(
        backend=backend, auto_target_resolver=lambda _board: "nrf52840"
    )
    with patch.object(server, "_hardware_inventory", _setup_inventory()):
        public_text(
            server,
            "connect",
            {
                "board_id": BOARD,
                "probe_uid": "LITE-ACCEPTANCE-PROBE",
                "target": "nrf52840",
            },
        )
    backend.calls.clear()


class IndependentBatchDispatchTests(unittest.TestCase):
    def assert_dispatch_prefix(self, dispatches, actions, stopped_index):
        # Equality asserts zero attempted dispatches for *all* later children.
        self.assertEqual(dispatches[0][0], "action_batch")
        self.assertEqual(
            dispatches[1:],
            [(entry["tool_name"], entry["arguments"]) for entry in actions[: stopped_index + 1]],
        )

    def assert_write_prefix(self, result, backend, *, board=BOARD):
        self.assertEqual(result["board_id"], board)
        self.assertEqual(len(result["completed"]), 1)
        prefix = result["completed"][0]
        self.assertEqual((prefix["index"], prefix["tool_name"]), (0, "write_memory_raw"))
        reported = body(prefix["result"])
        self.assertEqual(reported["status"], "ok")
        self.assertEqual(reported["board_id"], board)
        self.assertIn("wrote 0x11 at 0x20000000", reported["result"])
        self.assertEqual(backend.memory, {FIRST: 17}, "the successful write must remain visible")
        self.assertEqual(
            [(call[2], call[3]) for call in backend.calls if call[0] == "write_memory"],
            [(FIRST, 17)],
        )

    def test_returned_error_and_refusals_keep_effects_and_stop_all_later_dispatch(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = StatefulBackend()
            connect(server, backend)
            invocations = []

            async def m03_result(board_id: str, outcome: str) -> str:
                invocations.append((board_id, outcome))
                return outcome

            server.mcp.add_tool(m03_result, structured_output=False)
            for outcome in (
                '{"status":"error","code":"device/read-failed","detail":{"attempted":true}}',
                '{"status":"refused","code":"policy/range","attempted":false}',
                "Refused [policy/range]: outside the selected map",
            ):
                with self.subTest(outcome=outcome):
                    backend.memory.clear()
                    backend.calls.clear()
                    invocations.clear()
                    actions = [
                        write_child(),
                        child("m03_result", outcome=outcome),
                        write_child(address=LATER, value=99),
                        child("m03_result", outcome="must never run"),
                    ]
                    result, dispatched = batch(server, actions)
                    self.assert_dispatch_prefix(dispatched, actions, 1)
                    self.assertEqual(invocations, [(BOARD, outcome)])
                    self.assertEqual(result["status"], "batch_failed")
                    self.assert_write_prefix(result, backend)
                    self.assertEqual(
                        {
                            key: result["failure"][key]
                            for key in ("index", "tool_name", "result", "error_type")
                        },
                        {
                            "index": 1,
                            "tool_name": "m03_result",
                            "result": outcome,
                            "error_type": "ChildResultError",
                        },
                    )

    def test_registered_child_exception_reports_exact_stopped_child_and_keeps_prefix(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = StatefulBackend()
            connect(server, backend)
            invocations = []

            async def m03_exception(board_id: str) -> str:
                invocations.append(board_id)
                raise LookupError("deterministic child failure after write")

            server.mcp.add_tool(m03_exception, structured_output=False)
            actions = [write_child(), child("m03_exception"), write_child(address=LATER, value=99)]
            result, dispatched = batch(server, actions)
            self.assert_dispatch_prefix(dispatched, actions, 1)
            self.assertEqual(invocations, [BOARD])
            self.assertEqual(result["status"], "batch_failed")
            self.assert_write_prefix(result, backend)
            failure = result["failure"]
            self.assertEqual((failure["index"], failure["tool_name"]), (1, "m03_exception"))
            self.assertEqual(failure["error_type"], "ToolError")
            self.assertIn("deterministic child failure after write", failure["message"])
            self.assertEqual(failure["result"], failure["message"])

    def test_malformed_raw_result_never_dispatches_successors_or_erases_write_prefix(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = StatefulBackend()
            connect(server, backend)
            tool = server.mcp._tool_manager.get_tool("read_memory_raw")
            self.assertIsNotNone(tool)
            for malformed in ("{}", '{"status":', "[]", '{"status":"unknown"}'):
                with self.subTest(malformed=malformed):
                    backend.memory.clear()
                    backend.calls.clear()
                    actions = [
                        write_child(),
                        child("read_memory_raw", address=FIRST),
                        write_child(address=LATER, value=99),
                    ]
                    # Only this faulty producer is substituted. Prefix/successor
                    # producers, result normalization and dispatch remain real.
                    with patch.object(tool, "fn", return_value=malformed) as producer:
                        result, dispatched = batch(server, actions)
                    self.assert_dispatch_prefix(dispatched, actions, 1)
                    self.assertEqual(producer.call_count, 1)
                    self.assertEqual(result["status"], "batch_failed")
                    self.assert_write_prefix(result, backend)
                    failure = result["failure"]
                    self.assertEqual(
                        (failure["index"], failure["tool_name"]), (1, "read_memory_raw")
                    )
                    self.assertEqual(failure["result"], malformed)
                    self.assertEqual(failure["error_type"], "MalformedChildResult")

    def test_real_raw_breakpoint_exhaustion_retains_six_effects_and_never_attempts_eighth(self):
        """F09: real raw producer converts backend failure into a returned error."""
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = StatefulBackend()
            connect(server, backend)
            addresses = [0x1000 + index * 4 for index in range(8)]
            actions = [child("set_breakpoint_raw", address=address) for address in addresses]
            result, dispatched = batch(server, actions)
            self.assert_dispatch_prefix(dispatched, actions, 6)
            self.assertEqual(result["status"], "batch_failed")
            self.assertEqual(backend.breakpoints, set(addresses[:6]))
            self.assertEqual(
                [call[2] for call in backend.calls if call[0] == "set_breakpoint"], addresses[:7]
            )
            self.assertEqual([entry["index"] for entry in result["completed"]], list(range(6)))
            for index, entry in enumerate(result["completed"]):
                self.assertEqual(entry["tool_name"], "set_breakpoint_raw")
                reported = body(entry["result"])
                self.assertEqual(reported["status"], "ok")
                self.assertEqual(reported["result"], f"breakpoint set at 0x{addresses[index]:X}")
            failure = result["failure"]
            self.assertEqual((failure["index"], failure["tool_name"]), (6, "set_breakpoint_raw"))
            self.assertEqual(failure["error_type"], "ChildResultError")
            reported = body(failure["result"])
            self.assertEqual(reported["status"], "error")
            self.assertEqual(reported["code"], "backend/breakpoint-failed")
            self.assertIn("deterministic comparator exhaustion", reported["message"])

    def test_real_lite_dispatch_refusal_is_normalized_and_stops_after_visible_write(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            policies.commit(BOARD, Tier.SETUP_LITE, map_snapshot=confirmed_lite_policy(BOARD))
            backend = StatefulBackend()
            server.configure_tiered_test_seams(backend=backend, policy_repository=policies)
            # Deterministic session boundary; the safe handler, plan, immutable
            # policy, containment guard and refusal normalization are unchanged.
            handle = backend.open(board=None, unique_id="m03-test-probe", target="nrf52840")
            server._promote_open_session(BOARD, handle, gate_reason="independent M03 fixture")
            plan_fields = {
                "board_id": None,
                "hypothesis": None,
                "strategy": None,
                "hypothesis_made": None,
                "strategy_evaluated": None,
                "expected_fail_return": None,
                "expected_success_return": None,
                "max_calls": None,
                "max_calls_buffer": None,
                "action_parameters": None,
                "user_permission": None,
            }
            public_text(server, "read_memory_address-plan", plan_fields)
            accepted = body(
                public_text(
                    server,
                    "read_memory_address-plan",
                    {
                        "board_id": BOARD,
                        "hypothesis": "The requested address may be outside the confirmed map.",
                        "strategy": "Attempt one safe read and retain an honest containment refusal.",
                        "hypothesis_made": True,
                        "strategy_evaluated": True,
                        "expected_fail_return": "A containment refusal before backend read.",
                        "expected_success_return": "A mapped word is read.",
                        "max_calls": 1,
                        "max_calls_buffer": 0,
                        "action_parameters": {"address": 0x40000000, "width": 32, "length": 4},
                    },
                )
            )
            self.assertEqual(accepted["status"], "plan_accepted")
            actions = [
                write_child(),
                *accepted["stable_client_fallback"]["arguments"]["actions"],
                write_child(address=LATER, value=99),
            ]
            backend.calls.clear()
            result, dispatched = batch(server, actions)
            self.assert_dispatch_prefix(dispatched, actions, 1)
            self.assertEqual(result["status"], "batch_failed")
            self.assert_write_prefix(result, backend)
            failure = result["failure"]
            self.assertEqual((failure["index"], failure["tool_name"]), (1, "read_memory_address"))
            self.assertEqual(failure["error_type"], "ToolError")
            refusal = body(failure["result"])
            self.assertEqual(refusal["status"], "refused")
            self.assertEqual(refusal["code"], "safety/lite-containment-refused")
            self.assertEqual(refusal["operation"], "read_memory_address")
            self.assertEqual(refusal["tier"], "setup-lite")
            self.assertEqual(refusal["board_id"], BOARD)
            self.assertIn("setup-lite map", refusal["message"])
            self.assertEqual(refusal["message"], failure["message"].split("\n", 1)[0])
            self.assertFalse(any(call[0].startswith("read_memory") for call in backend.calls))

    def test_all_success_production_batch_preserves_order_and_observable_results(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = StatefulBackend()
            connect(server, backend)
            actions = [
                write_child(),
                child("read_memory_raw", address=FIRST),
                write_child(address=FIRST, value=34),
                child("read_memory_raw", address=FIRST),
            ]
            result, dispatched = batch(server, actions)
            self.assert_dispatch_prefix(dispatched, actions, 3)
            self.assertEqual(result["status"], "batch_completed")
            self.assertIsNone(result["failure"])
            self.assertEqual([entry["index"] for entry in result["completed"]], [0, 1, 2, 3])
            self.assertEqual(
                [entry["tool_name"] for entry in result["completed"]],
                [entry["tool_name"] for entry in actions],
            )
            children = [body(entry["result"]) for entry in result["completed"]]
            self.assertEqual([entry["status"] for entry in children], ["ok"] * 4)
            self.assertEqual(
                [children[index]["result"] for index in (1, 3)], ["0x00000011", "0x00000022"]
            )
            self.assertEqual(backend.memory, {FIRST: 34})
            self.assertEqual(
                [call[0] for call in backend.calls if call[0] in {"read_memory", "write_memory"}],
                ["write_memory", "read_memory", "write_memory", "read_memory"],
            )

    def test_real_setup_and_accepted_research_suspend_work_until_paired_fix_completes(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = StatefulBackend()
            server.configure_tiered_test_seams(backend=backend)
            journal = root / "batch-prefix.txt"

            async def m03_record_prefix(board_id: str, stage: str) -> str:
                with journal.open("a", encoding="utf-8") as handle:
                    handle.write(stage + "\n")
                return json.dumps({"status": "ok", "board_id": board_id, "persisted": stage})

            server.mcp.add_tool(m03_record_prefix, structured_output=False)
            with (
                patch.object(server, "_hardware_inventory", _setup_inventory()),
                patch.object(
                    server._setup_workflow,
                    "inventory_provider",
                    side_effect=lambda _input: _ready_setup_preflight(),
                ),
            ):
                board, _, accepted = _accepted_setup_plan(
                    self,
                    server,
                    display_name="Independent batch setup",
                    target_tier="setup-lite",
                    datasheet=_minimal_local_pdf(root, "batch-local.pdf"),
                )
                setup = accepted["stable_client_fallback"]["arguments"]["actions"]
                dependent = write_child(board, address=LATER, value=99)
                actions = [
                    child("m03_record_prefix", board, stage="before-setup"),
                    *setup,
                    dependent,
                ]
                result, dispatched = batch(server, actions, board)
                self.assert_dispatch_prefix(dispatched, actions, 1)
                self.assertEqual(result["status"], "batch_pending")
                self.assertEqual(result["failure"]["index"], 1)
                self.assertEqual(result["failure"]["tool_name"], "board_setup")
                self.assertEqual(result["failure"]["error_type"], "ChildPending")
                self.assertEqual(
                    result["failure"]["message"],
                    "Child requires continuation (setup_research_required).",
                )
                pending = body(result["failure"]["result"])
                self.assertEqual(pending["status"], "setup_research_required")
                continuation = pending["continuation_id"]
                self.assertTrue(continuation)
                self.assertEqual(
                    result["completed"],
                    [
                        {
                            "index": 0,
                            "tool_name": "m03_record_prefix",
                            "result": json.dumps(
                                {"status": "ok", "board_id": board, "persisted": "before-setup"}
                            ),
                        }
                    ],
                )
                self.assertEqual(journal.read_text(encoding="utf-8"), "before-setup\n")
                waiting = CapabilityPolicyRepository(root).resolve(board)
                self.assertEqual(waiting.tier, Tier.NO_SETUP)
                self.assertTrue(waiting.setup_incomplete)
                self.assertEqual(backend.memory, {})

                confirmation = fixture("lite_confirmation.json")
                actions = [
                    child("m03_record_prefix", board, stage="before-confirmation"),
                    child(
                        "continue_setup",
                        board,
                        continuation_id=continuation,
                        response={
                            key: value
                            for key, value in confirmation.items()
                            if key != "schema_version"
                        },
                    ),
                    dependent,
                ]
                result, dispatched = batch(server, actions, board)
                self.assert_dispatch_prefix(dispatched, actions, 1)
                self.assertEqual(result["status"], "batch_pending")
                self.assertEqual(result["failure"]["index"], 1)
                self.assertEqual(result["failure"]["tool_name"], "continue_setup")
                self.assertEqual(result["failure"]["error_type"], "ChildPending")
                self.assertEqual(
                    result["failure"]["message"],
                    "Child requires continuation (setup_continuation_accepted).",
                )
                staged = body(result["failure"]["result"])
                self.assertEqual(staged["status"], "setup_continuation_accepted")
                self.assertEqual(staged["accepted"], "lite_confirmation")
                self.assertIn("board_fix_setup", staged["redirect"])
                self.assertEqual(
                    body(result["completed"][0]["result"])["persisted"], "before-confirmation"
                )
                self.assertEqual([entry["index"] for entry in result["completed"]], [0])
                waiting = CapabilityPolicyRepository(root).resolve(board)
                self.assertEqual(waiting.tier, Tier.NO_SETUP)
                self.assertTrue(waiting.setup_incomplete)
                self.assertEqual(backend.memory, {})
                self.assertFalse(any(call[0] == "write_memory" for call in backend.calls))
                self.assertEqual(
                    journal.read_text(encoding="utf-8"), "before-setup\nbefore-confirmation\n"
                )

                repair = accepted["paired_action_fallbacks"][0]["call"]["arguments"]["actions"]
                # Lite setup verifies a temporary connection and closes it;
                # ordinary target work still needs the public live connection.
                actions = [*repair, child("connect", board), dependent]
                result, dispatched = batch(server, actions, board)
                self.assert_dispatch_prefix(dispatched, actions, 2)
                self.assertEqual(result["status"], "batch_completed", result)
                self.assertIsNone(result["failure"])
                self.assertEqual([entry["index"] for entry in result["completed"]], [0, 1, 2])
                self.assertEqual(result["completed"][0]["tool_name"], "board_fix_setup")
                self.assertEqual(
                    body(result["completed"][0]["result"])["status"], "setup_completed"
                )
                self.assertEqual(result["completed"][1]["tool_name"], "connect")
                self.assertEqual(body(result["completed"][2]["result"])["status"], "ok")
                self.assertEqual(
                    CapabilityPolicyRepository(root).resolve(board).tier, Tier.SETUP_LITE
                )
                self.assertEqual(backend.memory, {LATER: 99})
                self.assertEqual(
                    len([call for call in backend.calls if call[0] == "write_memory"]), 1
                )
                self.assertEqual(
                    journal.read_text(encoding="utf-8"), "before-setup\nbefore-confirmation\n"
                )
