"""Versioned identity verification for the guarded Server B endpoint."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from pyocd_debug_mcp.tools.handshake import (
    SERVER_B_CONTRACT_VERSION,
    SERVER_B_PRODUCT_ID,
    SERVER_B_PRODUCT_PREFIX,
)

_REQUIRED_TOOLS = frozenset(
    {"initialization_handshake", "setup_overview", "board_validate", "action_batch"}
)
_RUN_ID_PATTERN = re.compile(r"^- run_id:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ServerBIdentity:
    product_id: str
    contract_version: int
    run_id: str


def identity_from_handshake(text: str) -> ServerBIdentity | None:
    """Parse the versioned identity emitted by the real Server B handshake."""

    if not text.startswith(SERVER_B_PRODUCT_PREFIX):
        return None
    match = _RUN_ID_PATTERN.search(text)
    if match is None:
        return None
    return ServerBIdentity(
        SERVER_B_PRODUCT_ID,
        SERVER_B_CONTRACT_VERSION,
        match.group(1),
    )


async def probe_server_b(url: str, *, timeout_seconds: float = 2.0) -> ServerBIdentity | None:
    """Return a verified identity, never a name-only endpoint guess."""

    try:
        async with streamable_http_client(url, terminate_on_close=False) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout_seconds)
                tools = await asyncio.wait_for(session.list_tools(), timeout=timeout_seconds)
                names = {tool.name for tool in tools.tools}
                if not _REQUIRED_TOOLS <= names:
                    return None
                result = await asyncio.wait_for(
                    session.call_tool("initialization_handshake", {}), timeout=timeout_seconds
                )
                if result.isError or len(result.content) != 1:
                    return None
                content = result.content[0]
                if not isinstance(content, TextContent):
                    return None
                return identity_from_handshake(content.text)
    except Exception:
        return None


def verify_server_b(url: str) -> ServerBIdentity | None:
    return asyncio.run(probe_server_b(url))
