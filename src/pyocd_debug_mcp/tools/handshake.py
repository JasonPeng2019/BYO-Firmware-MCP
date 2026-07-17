"""Initialization handshake and advisory operating guidance."""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from pyocd_debug_mcp.kernel.registry import ToolRegistry

HANDSHAKE_TOOL_NAME = "initialization_handshake"
HANDSHAKE_DESCRIPTION = (
    "Call this first after connecting to learn the server's current safe operating workflow."
)


def build_initialization_guidance(registry: ToolRegistry) -> str:
    """Build side-effect-free guidance from the current advertised tool list."""

    visible_tools = registry.advertised()
    tool_index = "\n".join(f"- {name}" for name in visible_tools)
    return f"""Guarded Hardware Server operating guidance

The server intentionally hides some hardware-control tools at startup. The currently visible
tool list is authoritative: never guess, request, or call an unlisted tool.

Currently visible tools:
{tool_index}

Guarded actions are exposed through a corresponding *-plan tool. For each plan tool, first call
it with every parameter set to NULL. Read that response, then submit the complete plan fields and
the exact underlying action parameters. A visible tool is not proof of authorization; the server
also enforces physical handler locks and all board-specific safety checks.

At startup, ask the user in ordinary conversation for one unique, familiar name for each
connected board, or "no board". Never ask the user for structured data, board IDs, connection
IDs, or permission values. Never expose structured payloads, continuation tokens, or internal
field names to the user.

Route an existing profile name to validation, an unknown name to setup, and an incomplete or
failed profile to repair. If a physical match is ambiguous, relay only the server-provided
friendly choices and let the user choose. Never silently choose, rename, reassign, or rewrite a
profile.

Ordinary conversation is never permission. When a tool requests approval, ask clearly in plain
language and pass approval only through the exact structured parameter named by that tool.

Keep every board isolated. Never reuse another board's validation, approval, plan, parameters,
or result. After a disconnect or the end of this Server Run, repeat validation before guarded
actions.

If no board is connected, do not begin setup, validation, or hardware actions."""


def register_initialization_handshake(
    mcp: FastMCP,
    registry: ToolRegistry,
) -> Callable[[], str]:
    """Register and return the no-argument handshake handler."""

    def initialization_handshake() -> str:
        """Return current operating guidance; call this first in each Server Run."""

        return build_initialization_guidance(registry)

    mcp.add_tool(
        initialization_handshake,
        name=HANDSHAKE_TOOL_NAME,
        description=HANDSHAKE_DESCRIPTION,
        structured_output=False,
    )
    return initialization_handshake
