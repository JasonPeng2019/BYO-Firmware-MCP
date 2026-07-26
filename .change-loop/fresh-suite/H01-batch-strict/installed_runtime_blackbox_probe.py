"""Host-only installed-runtime probe for the H01 registered MCP boundary."""

from __future__ import annotations

import json

import anyio
from mcp.client.session import ClientSession

from pyocd_debug_mcp.server import mcp


async def probe() -> None:
    client_send, server_receive = anyio.create_memory_object_stream()
    server_send, client_receive = anyio.create_memory_object_stream()
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            mcp._mcp_server.run,  # type: ignore[reportPrivateUsage]
            server_receive,
            server_send,
            mcp.create_initialization_options(),
        )
        async with ClientSession(client_receive, client_send) as client:
            await client.initialize()
            listed = await client.list_tools()
            schemas = {tool.name: tool.inputSchema for tool in listed.tools}
            assert len(schemas) == 39, len(schemas)
            assert all(
                schema.get("additionalProperties") is False for schema in schemas.values()
            )
            assert "initialization_handshake" in schemas
            assert "action_batch" in schemas

            handshake = await client.call_tool(
                "initialization_handshake", {"installed_probe_extra": True}
            )
            assert handshake.isError
            handshake_text = handshake.content[0].text
            assert "installed_probe_extra" in handshake_text

            batch = await client.call_tool(
                "action_batch",
                {
                    "board_id": "h01-host-only-absent",
                    "actions": [
                        {
                            "tool_name": "read_serial",
                            "arguments": {
                                "board_id": "h01-host-only-absent",
                                "timeout": 0.1,
                            },
                        }
                    ],
                    "batch_top_extra": True,
                },
            )
            assert batch.isError
            batch_text = batch.content[0].text
            assert "batch_top_extra" in batch_text
            assert "batch_completed" not in batch_text

            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "advertised_tools": len(schemas),
                        "all_advertised_root_schemas_strict": True,
                        "handshake_extra_is_error": handshake.isError,
                        "batch_outer_extra_is_error": batch.isError,
                    },
                    sort_keys=True,
                )
            )
        tasks.cancel_scope.cancel()


if __name__ == "__main__":
    anyio.run(probe)
