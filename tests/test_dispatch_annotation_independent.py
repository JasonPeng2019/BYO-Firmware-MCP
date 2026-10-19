"""Independent public dispatch preservation and actual console cadence contracts."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    TextContent,
    TextResourceContents,
)
from pydantic import AnyUrl

from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.monitor.monitor import CHECKIN_PROMPT
from tests.tiered_acceptance_support import isolated_project, tiered_test_server


class AdvisoryObserver:
    """Deterministic external observer; handlers still traverse normal dispatch."""

    def __init__(self):
        self.due = True
        self.observed = []
        self.consumed = 0
        self.broken = False

    def begin(self, tool: str, arguments: Mapping[str, Any], board: str | None):
        self.observed.append(tool)
        return self

    def completed(self, result):
        pass

    def failed(self, exc):
        pass

    def consume_checkin_prompt(self):
        self.consumed += 1
        if self.broken:
            raise OSError("observer store unavailable")
        if self.due:
            self.due = False
            return CHECKIN_PROMPT
        return None


async def protocol_call(mcp, name, arguments=None):
    request = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments or {}))
    response = await mcp._mcp_server.request_handlers[CallToolRequest](request)
    return json.loads(response.root.model_dump_json(by_alias=True))


class IndependentDispatchPreservation(unittest.TestCase):
    def test_wire_payloads_survive_one_shot_annotation_and_reuse(self):
        mcp = RegistryFastMCP("independent-contract")
        observer = AdvisoryObserver()
        mcp.configure_monitor(observer)
        original = CallToolResult(
            content=[
                TextContent(type="text", text='  {"value":"café 中","n":0}\r\n', _meta={"tag": 7}),
                ImageContent(type="image", data="aGVsbG8=", mimeType="image/png"),
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri=AnyUrl("file:///contract.c"), text="int value;\n"
                    ),
                ),
                TextContent(type="text", text="Keep this trailing prose.\n"),
            ],
            structuredContent={"status": "refused", "nested": {"values": [0, False, None]}},
            isError=True,
            _meta={"progressToken": "independent", "nested": {"tag": 9}},
        )
        saved = json.loads(original.model_dump_json(by_alias=True))
        invocations = []

        def result_handler():
            invocations.append("explicit")
            return original

        mcp.add_tool(result_handler, name="independent_payload", structured_output=False)
        first = asyncio.run(protocol_call(mcp, "independent_payload"))
        self.assertEqual(first["content"][-1]["text"], CHECKIN_PROMPT)
        first["content"].pop()
        self.assertEqual(first, saved)
        self.assertEqual(json.loads(original.model_dump_json(by_alias=True)), saved)
        second = asyncio.run(protocol_call(mcp, "independent_payload"))
        self.assertEqual(second, saved)
        self.assertEqual(invocations, ["explicit", "explicit"])
        self.assertEqual(observer.observed, ["independent_payload", "independent_payload"])

        # A normal structured handler uses the same public contract.
        def structured_handler() -> dict[str, object]:
            return {"values": [1, False, None], "message": "retained"}

        mcp.add_tool(structured_handler, name="independent_structured")
        mcp.configure_monitor(None)
        baseline = asyncio.run(protocol_call(mcp, "independent_structured"))
        mcp.configure_monitor(observer)
        observer.due = True
        annotated = asyncio.run(protocol_call(mcp, "independent_structured"))
        self.assertEqual(annotated["content"].pop()["text"], CHECKIN_PROMPT)
        self.assertEqual(annotated, baseline)

    def test_batch_keeps_exact_child_results_prefix_and_single_outer_advisory(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            mcp = server.mcp
            observer = AdvisoryObserver()
            mcp.configure_monitor(observer)
            effects = []

            async def child(board_id: str, value: str) -> str:
                effects.append(value)
                return value

            mcp.add_tool(child, name="independent_batch_child", structured_output=False)
            arguments = {
                "board_id": "contract-board",
                "actions": [
                    {
                        "tool_name": "independent_batch_child",
                        "arguments": {"board_id": "contract-board", "value": value},
                    }
                    for value in (
                        '{"marker":"first"}',
                        '{"status":"refused","reason":"stop"}',
                        "must not run",
                    )
                ],
            }
            mcp.configure_monitor(None)
            baseline = asyncio.run(protocol_call(mcp, "action_batch", arguments))
            self.assertEqual(
                effects, ['{"marker":"first"}', '{"status":"refused","reason":"stop"}']
            )
            effects.clear()
            mcp.configure_monitor(observer)
            actual = asyncio.run(protocol_call(mcp, "action_batch", arguments))
            self.assertEqual(actual["content"].pop()["text"], CHECKIN_PROMPT)
            self.assertEqual(actual, baseline)
            self.assertEqual(
                effects, ['{"marker":"first"}', '{"status":"refused","reason":"stop"}']
            )
            self.assertEqual(observer.consumed, 1)
            self.assertEqual(
                observer.observed,
                ["action_batch", "independent_batch_child", "independent_batch_child"],
            )
            # Reuse the same public result under an observer failure: no replay.
            observer.broken = True
            effects.clear()
            actual = asyncio.run(protocol_call(mcp, "action_batch", arguments))
            self.assertEqual(actual, baseline)
            self.assertEqual(len(effects), 2)
            self.assertEqual(observer.consumed, 2)


class IndependentConsoleCadence(unittest.IsolatedAsyncioTestCase):
    async def test_health_first_block_is_intact_across_two_real_stdio_due_boundaries(self):
        project_root = Path(__file__).resolve().parents[1]
        traffic = []
        with tempfile.TemporaryDirectory(prefix="byo-independent-wire-") as temporary:
            private = Path(temporary)
            environment = dict(
                os.environ,
                BYO_MCP_MONITOR_ROOT=str(private / "monitor"),
                BYO_MCP_ARTIFACT_ROOT=str(private / "artifacts"),
                BYO_MCP_SNAPSHOT_CADENCE="2",
                BYO_MCP_CHECKIN_CADENCE="4",
            )
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "pyocd_debug_mcp.server"],
                cwd=str(private),
                env=environment,
            )
            try:
                with (private / "stderr.log").open("w", encoding="utf-8") as stderr:
                    async with stdio_client(parameters, errlog=stderr) as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            handshake = await session.call_tool(
                                "initialization_handshake", {"workspace_path": str(private)}
                            )
                            traffic.append(
                                {
                                    "tool": "initialization_handshake",
                                    "result": handshake.model_dump(mode="json", by_alias=True),
                                }
                            )
                            self.assertFalse(handshake.isError)
                            for total in range(1, 10):
                                response = await session.call_tool("server_health_check", {})
                                traffic.append(
                                    {
                                        "tool": "server_health_check",
                                        "result": response.model_dump(mode="json", by_alias=True),
                                    }
                                )
                                self.assertFalse(response.isError)
                                # Parse the entire original text: no extraction or repair.
                                first = response.content[0]
                                assert isinstance(first, TextContent)
                                payload = json.loads(first.text)
                                self.assertEqual(payload["counters"]["total"], total)
                                due = total in (3, 7)
                                self.assertEqual(len(response.content), 2 if due else 1)
                                if due:
                                    advisory = response.content[1]
                                    assert isinstance(advisory, TextContent)
                                    self.assertEqual(advisory.text, CHECKIN_PROMPT)
            finally:
                evidence = os.environ.get("BYO_MANAGED_TEST_EVIDENCE")
                if evidence:
                    destination = Path(evidence) / self.id()
                    destination.mkdir(parents=True, exist_ok=True)
                    (destination / "traffic.json").write_text(
                        json.dumps(
                            {
                                "command": [sys.executable, "-m", "pyocd_debug_mcp.server"],
                                "source_project": str(project_root),
                                "calls": traffic,
                                "environment": {
                                    k: v for k, v in environment.items() if k.startswith("BYO_")
                                },
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    (destination / "stderr.log").write_bytes((private / "stderr.log").read_bytes())
