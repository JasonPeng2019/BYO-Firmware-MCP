"""Software-only regressions for owned children under a real MCP stdio host."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from firmware_mcp.kernel.processes import identity_matches
from firmware_mcp.kernel.registry import _EOFObservingReceiveStream


_SERVER = r"""
import anyio
from pathlib import Path
import sys

from firmware_mcp.kernel.processes import run_owned
from firmware_mcp.kernel.registry import RegistryFastMCP
from firmware_mcp.kernel.operations import current_operation, operation_manager
from firmware_mcp.native_build import build_firmware

mcp = RegistryFastMCP("hil-r6-stdio")

@mcp.tool()
def read_owned_stdin():
    completed = run_owned(
        [sys.executable, "-c", "import sys; print('EOF' if sys.stdin.buffer.read() == b'' else 'DATA')"],
        capture_output=True,
        text=True,
    )
    return {"returncode": completed.returncode, "stdout": completed.stdout}

@mcp.tool()
def build_without_protocol_stdin(project_dir: str, build_dir: str):
    return build_firmware(
        project_dir,
        build_dir,
        [sys.executable, "-c", "print('native build child completed')"],
    )

@mcp.tool()
def hold_owned_child(board_id: str, ready_path: str, request_id_path: str):
    operation = current_operation()
    assert operation is not None
    Path(request_id_path).write_text(operation.request_id, encoding="utf-8")
    return run_owned(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys, time; Path(sys.argv[1]).write_text('ready'); time.sleep(30)",
            ready_path,
        ],
        capture_output=True,
        text=True,
    ).returncode

@mcp.tool()
def peer_operation(board_id: str):
    return {"peer": "still-live"}

@mcp.tool()
def cancel_owned_child(board_id: str, request_id: str):
    return {"cancelled": operation_manager.cancel_request(request_id, "test MCP cancellation")}

anyio.run(mcp.run_stdio_async)
"""


class RegistryFastMCPStdioChildIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the actual stdio transport instead of a mocked registry call."""

    @staticmethod
    def _result_document(result: object) -> dict[str, object]:
        content = getattr(result, "content")
        text = content[0].text
        parsed = json.loads(text)
        assert isinstance(parsed, dict)
        return parsed

    @staticmethod
    async def _wait_for_file(path: Path) -> None:
        # This is test-only synchronization for the child-created readiness
        # evidence; production cancellation has no polling or deadline.
        async with asyncio.timeout(5):
            while not path.exists():
                await asyncio.sleep(0.01)

    @staticmethod
    async def _wait_for_no_markers(runs_root: Path) -> None:
        async with asyncio.timeout(5):
            while list((runs_root / "owned-processes").glob("*.json")):
                await asyncio.sleep(0.01)

    @staticmethod
    async def _wait_for_owned_marker(runs_root: Path) -> Path:
        """Return the sole marker created by the live owned child."""

        markers_root = runs_root / "owned-processes"
        async with asyncio.timeout(5):
            while True:
                markers = list(markers_root.glob("*.json"))
                if len(markers) == 1:
                    return markers[0]
                await asyncio.sleep(0.01)

    @staticmethod
    def _parameters(runs_root: Path) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-c", _SERVER],
            cwd=Path(__file__).parents[1],
            env={**os.environ, "BYO_FIRMWARE_MCP_RUNS_ROOT": str(runs_root)},
        )

    async def test_owned_child_reads_eof_and_returns_over_real_mcp_stdio(self) -> None:
        with TemporaryDirectory() as temporary:
            async with stdio_client(self._parameters(Path(temporary))) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    result = await session.call_tool("read_owned_stdin", {})

        self.assertFalse(result.isError)
        self.assertEqual(self._result_document(result), {"returncode": 0, "stdout": "EOF\n"})

    async def test_real_native_build_returns_exact_success_evidence_over_mcp_stdio(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            async with stdio_client(self._parameters(root / "runs")) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "build_without_protocol_stdin",
                        {"project_dir": str(project), "build_dir": str(root / "build")},
                    )

        evidence = self._result_document(result)
        self.assertFalse(result.isError)
        self.assertEqual(evidence["status"], "build_succeeded")
        self.assertEqual(evidence["exit_code"], 0)
        self.assertEqual(evidence["stdout"], "native build child completed\n")
        self.assertEqual(evidence["artifacts"], [])

    async def test_mcp_cancellation_cleans_only_the_owned_child_and_preserves_peer_result(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            async with stdio_client(self._parameters(root / "runs")) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    # Repeat against one live RegistryFastMCP process: this is
                    # scheduling coverage for the cancellation/worker handoff,
                    # not a server-side retry or call budget.
                    for iteration in range(3):
                        ready = root / f"owned-child-ready-{iteration}"
                        request_id_path = root / f"owned-request-id-{iteration}"
                        pending = asyncio.create_task(
                            session.call_tool(
                                "hold_owned_child",
                                {
                                    "board_id": "board-a",
                                    "ready_path": str(ready),
                                    "request_id_path": str(request_id_path),
                                },
                            )
                        )
                        await self._wait_for_file(ready)
                        marker = await self._wait_for_owned_marker(root / "runs")
                        marker_evidence = json.loads(marker.read_text(encoding="utf-8"))
                        child_pid = marker_evidence["pid"]
                        child_start_token = marker_evidence["start_token"]
                        peer = await session.call_tool("peer_operation", {"board_id": "board-b"})
                        self.assertFalse(peer.isError)
                        self.assertEqual(self._result_document(peer), {"peer": "still-live"})
                        cancellation = await session.call_tool(
                            "cancel_owned_child",
                            {
                                "board_id": "board-b",
                                "request_id": request_id_path.read_text(encoding="utf-8"),
                            },
                        )
                        self.assertEqual(self._result_document(cancellation), {"cancelled": 1})
                        pending_result = await pending
                        self.assertTrue(pending_result.isError)
                        self.assertIsNone(pending_result.structuredContent)
                        self.assertEqual(len(pending_result.content), 1)
                        cancellation_content = pending_result.content[0]
                        self.assertIsInstance(cancellation_content, TextContent)
                        assert isinstance(cancellation_content, TextContent)
                        self.assertEqual(cancellation_content.text, "test MCP cancellation")
                        await self._wait_for_no_markers(root / "runs")
                        self.assertFalse(marker.exists())
                        self.assertFalse(identity_matches(child_pid, child_start_token))
                        peer_after_cleanup = await session.call_tool(
                            "peer_operation", {"board_id": "board-b"}
                        )
                        self.assertFalse(peer_after_cleanup.isError)
                        self.assertEqual(
                            self._result_document(peer_after_cleanup), {"peer": "still-live"}
                        )
            self.assertEqual(list((root / "runs" / "owned-processes").glob("*.json")), [])

    async def test_stdio_eof_cancels_owned_child_without_client_force_termination(
        self,
    ) -> None:
        """A real stdin close reaches the server's EOF cleanup before fallback.

        The standard client owns the transport.  Its force-termination fallback
        is deliberately made fatal here, so this proof can pass only when the
        server observes EOF, cancels the managed operation, and exits itself.
        """

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / "owned-child-ready"
            request_id_path = root / "owned-request-id"
            runs_root = root / "runs"
            pending: asyncio.Task[object] | None = None
            force_termination = AsyncMock(
                side_effect=AssertionError("client force-termination fallback was invoked")
            )

            with patch("mcp.client.stdio._terminate_process_tree", force_termination):
                async with stdio_client(self._parameters(runs_root)) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        pending = asyncio.create_task(
                            session.call_tool(
                                "hold_owned_child",
                                {
                                    "board_id": "board-a",
                                    "ready_path": str(ready),
                                    "request_id_path": str(request_id_path),
                                },
                            )
                        )
                        await self._wait_for_file(ready)
                        marker = await self._wait_for_owned_marker(runs_root)
                        marker_evidence = json.loads(marker.read_text(encoding="utf-8"))
                        child_pid = marker_evidence["pid"]
                        child_start_token = marker_evidence["start_token"]
                        self.assertIsInstance(child_pid, int)
                        self.assertIsInstance(child_start_token, str)

            # The closed client transport cannot receive a tool result.  In
            # particular it must not receive a fabricated normal completion.
            assert pending is not None
            self.assertFalse(pending.done())
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending

            force_termination.assert_not_awaited()
            await self._wait_for_no_markers(runs_root)
            self.assertFalse(marker.exists())
            self.assertFalse(identity_matches(child_pid, child_start_token))
            self.assertEqual(list((runs_root / "owned-processes").glob("*.json")), [])


class EOFObservingReceiveStreamTests(unittest.IsolatedAsyncioTestCase):
    """The stdio boundary forwards frames without a competing reader."""

    async def test_forwards_frames_and_notifies_once_only_for_eof_or_closure(self) -> None:
        sender, receiver = anyio.create_memory_object_stream[object](2)
        cancellations: list[str] = []
        stream = _EOFObservingReceiveStream(
            receiver,
            cancel_all=cancellations.append,
        )
        first = object()
        second = object()
        await sender.send(first)
        await sender.send(second)
        await sender.aclose()

        self.assertIs(await stream.receive(), first)
        self.assertIs(await stream.receive(), second)
        with self.assertRaises(anyio.EndOfStream):
            await stream.receive()
        with self.assertRaises(anyio.EndOfStream):
            await stream.receive()
        self.assertEqual(cancellations, ["stdio client EOF"])
        await receiver.aclose()

        closed_sender, closed_receiver = anyio.create_memory_object_stream[object](1)
        closed_cancellations: list[str] = []
        closed_stream = _EOFObservingReceiveStream(
            closed_receiver,
            cancel_all=closed_cancellations.append,
        )
        await closed_receiver.aclose()
        with self.assertRaises(anyio.ClosedResourceError):
            await closed_stream.receive()
        with self.assertRaises(anyio.ClosedResourceError):
            await closed_stream.receive()
        self.assertEqual(closed_cancellations, ["stdio client EOF"])
        await closed_sender.aclose()

    async def test_task_cancellation_is_not_client_eof(self) -> None:
        sender, receiver = anyio.create_memory_object_stream[object](0)
        cancellations: list[str] = []
        stream = _EOFObservingReceiveStream(receiver, cancel_all=cancellations.append)
        receive_task = asyncio.create_task(stream.receive())
        await asyncio.sleep(0)
        receive_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await receive_task
        self.assertEqual(cancellations, [])
        await sender.aclose()
        await receiver.aclose()


if __name__ == "__main__":
    unittest.main()
