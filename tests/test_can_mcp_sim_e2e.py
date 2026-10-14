"""MCP stdio -> public validation/plans -> real simulator -> retained capture.

The policy is fixture-seeded, and SWD hardware is explicitly simulated. This
qualifies the software chain, not physical onboarding or a physical CAN bus.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.test_can_native_sim import FRAME, ROOT, NativeSimulatorFixture, recv_exact


class CanMcpSimulationE2E(NativeSimulatorFixture):
    def test_public_validation_plans_capture_artifact_and_clean_eof(self) -> None:
        self.profile.write_text(self.profile.read_text().replace("nucleo_l476rg", "legacy_full"))
        evidence: dict[str, object] = {"physical_hardware": False, "setup_policy": "fixture-seeded"}

        async def scenario() -> None:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "tests.can_sim_mcp_server"],
                cwd=str(ROOT),
                env={
                    **os.environ,
                    "BYO_MCP_TEST_SEAMS": "1",
                    "PYOCD_MCP_RUNS_ROOT": str(self.root / "owned-runs"),
                    "BYO_MCP_ARTIFACT_ROOT": str(self.root),
                    "BYO_MCP_MONITOR_ROOT": str(self.root / "monitor"),
                    "PYOCD_CAN_MODE": "available",
                    "PYOCD_CAN_CONFIG": str(self.profile),
                    "PYOCD_CAN_CONFIG_SHA256": "",
                    "PYOCD_CAN_RUNTIME_SHA256": "",
                    "PYOCD_CAN_READ_SECONDS": "",
                },
            )
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()

                    async def call(name: str, arguments: dict) -> dict:
                        response = await session.call_tool(name, arguments)
                        self.assertFalse(response.isError, f"{name}: {response}")
                        text = response.content[0].text
                        try:
                            payload = json.loads(text)
                        except json.JSONDecodeError:
                            payload = {"text": text}
                        return payload

                    board = {"board_id": "legacy_full"}
                    overview = await call("setup_overview", {"board_names": ["Legacy full board"]})
                    self.assertEqual(overview["status"], "setup_routes_ready", overview)
                    self.assertEqual(overview["routes"][0]["board_id"], "legacy_full", overview)
                    connected = await call("connect", board)
                    self.assertIn("SIMULATED-PROBE", connected["text"])
                    loader = await call("load_setup_tool", {**board, "tool_name": "board_validate"})
                    validated = await call("board_validate", loader["next_call"]["arguments"])
                    self.assertEqual(validated["status"], "validation_passed", validated)
                    evidence["validation_status"] = validated["status"]
                    attached = await call("connect_can", board)
                    self.assertEqual(
                        attached["status"],
                        "connected",
                        f"{attached}\n{(self.root / 'startup-threads.log').read_text()}",
                    )
                    self.assertEqual(attached["can"]["capabilities"]["driver"], "tcp_sim")
                    common = {
                        **board,
                        "hypothesis": "The software simulator exchanges exact frames.",
                        "strategy": "Send then capture a bounded peer response.",
                        "hypothesis_made": True,
                        "strategy_evaluated": True,
                        "expected_fail_return": "Structured refusal or transport failure.",
                        "expected_success_return": "Exact frame with retained evidence.",
                        "max_calls": 1,
                        "max_calls_buffer": 0,
                    }
                    null_plan = {
                        key: None for key in (*common, "action_parameters", "user_permission")
                    }
                    send = {
                        "arbitration_id": 0x123,
                        "data_hex": "CAFE",
                        "extended": False,
                        "timeout_seconds": 0.2,
                    }
                    await call("send_can-plan", null_plan)
                    await call("send_can-plan", {**common, "action_parameters": send})
                    sent = await call("send_can", {**board, **send})
                    self.assertEqual(sent["status"], "sent", sent)
                    packet = await asyncio.to_thread(recv_exact, self.peer, FRAME.size + 1)
                    self.assertEqual(
                        FRAME.unpack(packet[1:]), (0x123, 0, 2, b"\xca\xfe" + bytes(6))
                    )
                    read = {"read_seconds": 0.5, "max_frames": 1}
                    await call("read_can-plan", null_plan)
                    await call("read_can-plan", {**common, "action_parameters": read})
                    self.peer.sendall(FRAME.pack(0x321, 0, 4, b"PASS"))
                    captured = await call("read_can", {**board, **read})
                    self.assertEqual(captured["status"], "captured", captured)
                    self.assertEqual(captured["returned_frames"], 1)
                    self.assertNotIn("data_hex", captured["frames"][0])
                    artifact = captured["artifact"]
                    paths = list(self.root.glob(f".firm/runs/*/can-captures/{artifact['name']}"))
                    self.assertEqual(len(paths), 1)
                    data = paths[0].read_bytes()
                    self.assertEqual(len(data), artifact["bytes"])
                    self.assertEqual(hashlib.sha256(data).hexdigest(), artifact["sha256"])
                    document = json.loads(data)
                    self.assertTrue(document["software_simulation"])
                    self.assertEqual(document["frames"][0]["data_hex"], b"PASS".hex())
                    self.assertEqual(document["profile_sha256"], attached["can"]["profile_sha256"])
                    self.assertEqual(captured["can"]["budget"]["capture_artifacts"], 1)
                    # Consumed plans cannot be silently reused.
                    reused = await session.call_tool("send_can", {**board, **send})
                    self.assertTrue(reused.isError)
                    evidence.update(capture=document, artifact=artifact, plan_reuse_refused=True)
                    disconnected = await call("disconnect_can", board)
                    self.assertEqual(disconnected["can"]["phase"], "closed")
                    await call("disconnect", board)
            receipt = json.loads((self.root / "shutdown-receipt.json").read_text())
            self.assertTrue(receipt["main_returned"])
            self.assertEqual(receipt["can_phase"], "closed")
            self.assertEqual(receipt["active_can_workers"], [])
            self.assertEqual(receipt["assigned_boards"], [])
            evidence["shutdown"] = receipt
            evidence["stdio_closed"] = True

        asyncio.run(asyncio.wait_for(scenario(), timeout=40))
        destination = os.environ.get("BYO_CAN_ACCEPTANCE_EVIDENCE_DIR")
        if destination:
            path = Path(destination)
            path.mkdir(parents=True, exist_ok=True)
            (path / "mcp-simulation-e2e.json").write_text(json.dumps(evidence, indent=2) + "\n")
