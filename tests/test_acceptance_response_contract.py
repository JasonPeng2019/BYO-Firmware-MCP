"""Independent scalar-client checks against the real monitored public dispatcher."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import unittest

from mcp.types import CallToolResult, ImageContent, TextContent

from tests.m05_public_support import PublicSetup
from tests import tiered_acceptance_support as support


def text(value: str) -> TextContent:
    return TextContent(type="text", text=value)


class ScalarResponseContract(unittest.TestCase):
    def test_locked_sdk_shapes_preserve_primary_and_expected_refusal(self):
        from pyocd_debug_mcp.monitor.monitor import CHECKIN_PROMPT

        primary = {"schema_version": 1, "status": "ok", "value": 19}
        refusal = {"schema_version": 1, "status": "refused", "code": "tier/board-required"}
        for payload, failed in ((primary, False), (refusal, True)):
            first = text(json.dumps(payload))
            for content in ([first], [first, text(CHECKIN_PROMPT)]):
                for response in (
                    content,
                    (content, payload),
                    CallToolResult(content=content, structuredContent=payload, isError=failed),
                ):
                    with self.subTest(payload=payload, response=type(response).__name__):
                        before = repr(response)
                        self.assertEqual(support.parse_response(response), payload)
                        self.assertEqual(
                            repr(response), before, "client must not mutate MCP evidence"
                        )
        self.assertEqual(support._content_text("baseline text"), "baseline text")

    def test_unexpected_content_and_protocol_errors_are_not_scalar_success(self):
        from pyocd_debug_mcp.monitor.monitor import CHECKIN_PROMPT

        first = text('{"schema_version":1,"status":"ok"}')
        advisory = text(CHECKIN_PROMPT)
        for response in (
            [],
            [first, text("unrecognized trailing diagnostic")],
            [first, text(CHECKIN_PROMPT + " unexpected suffix")],
            [first, advisory, advisory],
            [advisory, first],
            [ImageContent(type="image", data="AA==", mimeType="image/png"), advisory],
            ([],),
            ([first], "not structured output"),
            CallToolResult(content=[first], isError=True),
            CallToolResult(content=[text("transport broke")], isError=True),
            SimpleNamespace(text="not an SDK content block", type="image"),
        ):
            with self.subTest(response=repr(response)):
                with self.assertRaises(AssertionError):
                    support._content_text(response)

    def test_malformed_primary_is_not_repaired_by_advisory_or_structured_output(self):
        from pyocd_debug_mcp.monitor.monitor import CHECKIN_PROMPT

        for primary in ("{bad", "[]", '{"schema_version":2}', "{}", "null"):
            content = [text(primary), text(CHECKIN_PROMPT)]
            for response in (content, (content, {"schema_version": 1, "status": "ok"})):
                with self.subTest(primary=primary, shape=type(response).__name__):
                    with self.assertRaises(AssertionError):
                        support.parse_response(response)

    def test_public_helpers_do_not_swallow_true_tool_exceptions(self):
        async def broken(*_args):
            raise RuntimeError("actual dispatch failure")

        server = SimpleNamespace(mcp=SimpleNamespace(call_tool=broken))
        with self.assertRaises(AssertionError):
            support.public_call(server, "read_memory_raw", {})
        with self.assertRaisesRegex(RuntimeError, "actual dispatch failure"):
            support.public_text(server, "get_state", {})


class PublicCadenceContract(unittest.TestCase):
    def test_default_cadence_keeps_public_setup_raw_and_refusal_callers_usable(self):
        from pyocd_debug_mcp.monitor.counters import CHECKIN_CADENCE
        from pyocd_debug_mcp.monitor.monitor import CHECKIN_PROMPT, IssueMonitor

        with support.isolated_project() as root, support.tiered_test_server(root) as server:
            self.assertIsInstance(server._monitor, IssueMonitor)
            self.assertEqual(server._monitor._checkin_every, CHECKIN_CADENCE)
            # Use the real default cadence, without changing counters, monitor,
            # or dispatcher. Each checkpoint target is an actual public caller.
            completed = 0
            captured = []
            original = server.mcp.call_tool

            async def capture(name, arguments):
                result = await original(name, arguments)
                captured.append(result)
                return result

            server.mcp.call_tool = capture

            def advance():
                nonlocal completed
                while completed % CHECKIN_CADENCE != CHECKIN_CADENCE - 1:
                    response = asyncio.run(original("server_health_check", {}))
                    content = response[0] if isinstance(response, tuple) else response
                    self.assertEqual(len(content), 1)
                    completed += 1

            backend = support.DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend)
            handle = backend.open(board=None, unique_id="cadence-probe", target="nrf52840")
            server._promote_open_session("cadence-board", handle, gate_reason="host fixture")

            # The setup form path itself does no inventory I/O.
            def structured_fixture() -> dict[str, object]:
                return {"schema_version": 1, "status": "ok"}

            server.mcp.add_tool(structured_fixture, name="scalar_structured_fixture")
            artifact = root / "unaddressed.bin"
            artifact.write_bytes(b"\x12\x34\x56\x78")
            try:
                for call in (
                    lambda: PublicSetup(server).call("board_setup-plan", PublicSetupPlanGuide),
                    lambda: support.public_call(
                        server,
                        "read_memory_raw",
                        {
                            "board_id": "cadence-board",
                            "address": 0x20000000,
                        },
                    ),
                    lambda: support.public_call(
                        server,
                        "flash_raw",
                        {
                            "board_id": "cadence-board",
                            "artifact": str(artifact),
                        },
                    ),
                    lambda: support.public_call(server, "scalar_structured_fixture", {}),
                ):
                    advance()
                    result = call()
                    completed += 1
                    response = captured[-1]
                    content = response[0] if isinstance(response, tuple) else response
                    self.assertEqual(len(content), 2)
                    self.assertEqual(content[-1].text, CHECKIN_PROMPT)
                    # This is the historical helper's failing assertion, applied
                    # to an actual response rather than a constructed list.
                    self.assertNotEqual(len(content), 1)
                    self.assertIsInstance(result, (str, dict))
                    if isinstance(result, dict) and result.get("status") == "refused":
                        self.assertEqual(result["code"], "flash/unsupported-artifact")
                    self.assertNotIn(CHECKIN_PROMPT, content[0].text)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(len([c for c in backend.calls if c[0] == "read_memory"]), 1)
                with self.assertRaises(Exception):
                    asyncio.run(original("get_state", {"board_id": "absent-board"}))
            finally:
                support.public_text(server, "disconnect", {"board_id": "cadence-board"})
                server._monitor.closeout("public cadence test finished")


PublicSetupPlanGuide = dict.fromkeys(
    (
        "board_id",
        "hypothesis",
        "strategy",
        "hypothesis_made",
        "strategy_evaluated",
        "expected_fail_return",
        "expected_success_return",
        "max_calls",
        "max_calls_buffer",
        "action_parameters",
        "user_permission",
    )
)


if __name__ == "__main__":
    unittest.main()
