"""Check-in annotations preserve the locked SDK's real dispatch result shapes."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    TextContent,
    TextResourceContents,
)

from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.monitor.monitor import CHECKIN_PROMPT
from pyocd_debug_mcp.monitor.transport import TestTransport
from tests.monitor_support import MonitorTestCase
from tests.test_monitor_wiring import _fresh_server


class OneShotMonitor:
    def __init__(self) -> None:
        self.due = True
        self.consumptions = 0
        self.results = []
        self.failures = []

    def begin(self, *args):
        return self

    def completed(self, result):
        self.results.append(result)

    def failed(self, exc):
        self.failures.append(exc)

    def consume_checkin_prompt(self):
        self.consumptions += 1
        if self.due:
            self.due = False
            return CHECKIN_PROMPT
        return None


def wire_call(mcp, name):
    """Use the actual lowlevel input/output validation and serialization path."""
    request = CallToolRequest(params=CallToolRequestParams(name=name, arguments={}))
    result = asyncio.run(mcp._mcp_server.request_handlers[CallToolRequest](request)).root
    # Round-trip the same JSON model that is written to the MCP wire.
    return CallToolResult.model_validate_json(result.model_dump_json(by_alias=True))


class HealthJSONAtCadence(MonitorTestCase):
    def test_third_health_json_survives_the_real_checkin_boundary(self):
        with patch.dict("os.environ", BYO_MCP_SNAPSHOT_CADENCE="2", BYO_MCP_CHECKIN_CADENCE="4"):
            server = _fresh_server(self.store_dir)
        monitor = server._monitor
        self._monitors.append(monitor)
        monitor._default_transport = lambda workspace=None: TestTransport()
        mcp = server.mcp
        asyncio.run(
            mcp.call_tool("initialization_handshake", {"workspace_path": str(self.store_dir)})
        )
        for total in (1, 2, 3, 4):
            result = wire_call(mcp, "server_health_check")
            health = json.loads(result.content[0].text)
            self.assertFalse(result.isError)
            self.assertEqual(health["counters"]["total"], total)
            self.assertEqual(health["counters"]["per_tool"]["initialization_handshake"], 1)
            self.assertEqual(len(result.content), 2 if total == 3 else 1)
            if total == 3:
                self.assertEqual(result.content[1].text, CHECKIN_PROMPT)
        self.assertEqual(monitor._counters.snapshot().total, 5)
        self.assertIsNone(monitor.consume_checkin_prompt())


class SDKResultPreservation(unittest.TestCase):
    def setUp(self):
        self.mcp = RegistryFastMCP("checkin-results")
        self.monitor = OneShotMonitor()
        self.mcp.configure_monitor(self.monitor)

    def assert_content_preserved(self, result, original):
        self.assertEqual(
            [item.model_dump(mode="json", by_alias=True) for item in result.content[:-1]],
            original,
        )
        self.assertEqual(result.content[-1].text, CHECKIN_PROMPT)

    def test_multiblock_nontext_only_and_empty_lists_keep_content_and_order(self):
        image = ImageContent(type="image", data="aGVsbG8=", mimeType="image/png")
        resource = EmbeddedResource(
            type="resource",
            resource=TextResourceContents(uri="file:///fixture.c", text="int x;\n"),
        )
        text = TextContent(type="text", text=' {"value":"café"}\r\n', _meta={"original": True})
        tail = TextContent(type="text", text="trailing text\n")
        for index, blocks in enumerate(([image, text, resource, tail], [image, resource], [])):
            with self.subTest(blocks=blocks):
                self.monitor.due = True
                saved = [item.model_dump(mode="json", by_alias=True) for item in blocks]

                def content():
                    return blocks

                name = f"content_{index}"
                self.mcp.add_tool(content, name=name, structured_output=False)
                result = wire_call(self.mcp, name)
                self.assert_content_preserved(result, saved)
                self.assertEqual(
                    [item.model_dump(mode="json", by_alias=True) for item in blocks], saved
                )
                observed = self.monitor.results[-1]
                self.assertEqual(len(observed), len(blocks))
                for actual, original in zip(observed, blocks):
                    self.assertIs(actual, original)

    def test_structured_dict_uses_sdk_tuple_without_changing_either_payload(self):
        def structured() -> dict[str, int]:
            return {"count": 7}

        self.mcp.add_tool(structured)
        self.mcp.configure_monitor(None)
        baseline = wire_call(self.mcp, "structured")
        self.mcp.configure_monitor(self.monitor)
        result = wire_call(self.mcp, "structured")
        observed = self.monitor.results[-1]
        self.assertIsInstance(observed, tuple)
        self.assertEqual(len(observed), 2)
        self.assert_content_preserved(
            result, [item.model_dump(mode="json", by_alias=True) for item in baseline.content]
        )
        self.assertEqual(result.structuredContent, baseline.structuredContent)
        self.assertEqual(json.loads(result.content[0].text), {"count": 7})
        self.assertEqual(len(observed[0]), len(baseline.content))

    def test_calltoolresult_keeps_structured_error_status_metadata_and_original_object(self):
        original = CallToolResult(
            content=[TextContent(type="text", text='{"status":"refused"}\n')],
            structuredContent={"status": "refused", "code": "fixture/refused"},
            isError=True,
            _meta={"progressToken": "original", "nested": {"value": 42}},
        )
        saved = original.model_dump(mode="json", by_alias=True)

        def explicit() -> CallToolResult:
            return original

        self.mcp.add_tool(explicit, structured_output=False)
        result = wire_call(self.mcp, "explicit")
        self.assert_content_preserved(result, saved["content"])
        expected = dict(
            saved,
            content=[
                *saved["content"],
                TextContent(type="text", text=CHECKIN_PROMPT).model_dump(
                    mode="json", by_alias=True
                ),
            ],
        )
        self.assertEqual(result.model_dump(mode="json", by_alias=True), expected)
        self.assertEqual(original.model_dump(mode="json", by_alias=True), saved)
        self.assertIs(self.monitor.results[-1], original)
        next_result = wire_call(self.mcp, "explicit")
        self.assertEqual(next_result.model_dump(mode="json", by_alias=True), saved)
        self.assertEqual(self.monitor.consumptions, 2)

    def test_nested_dispatch_consumes_only_at_outer_result_without_repeating_handlers(self):
        calls = []

        async def child() -> str:
            calls.append("child")
            return '{"child":true}'

        async def outer():
            calls.append("outer")
            return await self.mcp.call_tool("child", {})

        self.mcp.add_tool(child, structured_output=False)
        self.mcp.add_tool(outer, structured_output=False)
        first = wire_call(self.mcp, "outer")
        self.assertEqual(len(first.content), 2)
        self.assertEqual(json.loads(first.content[0].text), {"child": True})
        self.assertEqual(first.content[1].text, CHECKIN_PROMPT)
        self.assertEqual(self.monitor.consumptions, 1)
        self.assertEqual([len(result) for result in self.monitor.results], [1, 1])
        second = wire_call(self.mcp, "outer")
        self.assertEqual(len(second.content), 1)
        self.assertEqual(calls, ["outer", "child", "outer", "child"])
        self.assertEqual(self.monitor.consumptions, 2)

    def test_annotation_failures_leave_result_unchanged_and_never_retry_dispatch(self):
        calls = []

        def genuine() -> str:
            calls.append("genuine")
            return '{"genuine":true}\n'

        self.mcp.add_tool(genuine, structured_output=False)
        for seam in ("consume", "construct"):
            with self.subTest(seam=seam):
                self.monitor.due = True
                target = (
                    self.monitor
                    if seam == "consume"
                    else "pyocd_debug_mcp.kernel.registry.TextContent"
                )
                context = (
                    patch.object(
                        target, "consume_checkin_prompt", side_effect=RuntimeError("monitor failed")
                    )
                    if seam == "consume"
                    else patch(target, side_effect=RuntimeError("annotation failed"))
                )
                with context:
                    result = wire_call(self.mcp, "genuine")
                self.assertFalse(result.isError)
                self.assertEqual([block.text for block in result.content], ['{"genuine":true}\n'])
        self.assertEqual(calls, ["genuine", "genuine"])
        # Construction failed after consumption: no duplicate advisory on a later result.
        self.assertEqual(len(wire_call(self.mcp, "genuine").content), 1)

    def test_failed_dispatch_retains_original_exception_and_pending_advisory(self):
        calls = []

        def failing():
            calls.append("failing")
            raise RuntimeError("genuine failure")

        self.mcp.add_tool(failing, structured_output=False)
        with self.assertRaisesRegex(Exception, "genuine failure") as caught:
            asyncio.run(self.mcp.call_tool("failing", {}))
        self.assertIs(self.monitor.failures[0], caught.exception)
        self.assertEqual(calls, ["failing"])
        self.assertEqual(self.monitor.consumptions, 0)
        self.assertTrue(self.monitor.due)


if __name__ == "__main__":
    unittest.main()
