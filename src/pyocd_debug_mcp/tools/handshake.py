"""Initialization handshake and advisory operating guidance."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from mcp.server.fastmcp import FastMCP

from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun

HANDSHAKE_TOOL_NAME = "initialization_handshake"
HANDSHAKE_DESCRIPTION = (
    "Call this first after connecting to learn the server's current safe operating workflow."
)


def build_initialization_guidance(
    registry: ToolRegistry,
    tool_descriptions: Mapping[str, str] | None = None,
    server_run: ServerRun | None = None,
) -> str:
    """Build side-effect-free guidance from the current advertised tool list."""

    visible_tools = registry.advertised()
    descriptions = tool_descriptions or {}
    tool_index = "\n".join(
        f"- {name}: {' '.join(descriptions.get(name, 'Visible MCP tool.').split())[:320]}"
        for name in visible_tools
    )
    run_evidence = ""
    if server_run is not None:
        run_evidence = f"""
Server Run identity:
- run_id: {server_run.run_id}
- started_at: {server_run.started_at_text}
These values identify this in-memory server process; they grant no authority and change after restart.
"""
    return f"""Guarded Hardware Server operating guidance
{run_evidence}

The server intentionally hides some hardware-control tools at startup. The currently visible
tool list is authoritative: never guess, request, or call an unlisted tool.

Currently visible tools:
{tool_index}

Guarded actions are exposed through a corresponding *-plan tool. A *-plan is an interactive
two-step preparation gate, not the hardware action: first call with every parameter set to NULL, read the
complete returned guide, then call the same *-plan with only its exact JSON plan object. If
accepted, the response names and exposes the real action. Never put prose, Markdown, flattened
action fields, or extra keys in a plan call. A visible tool is not proof of authorization; the
server also enforces physical handler locks and all board-specific safety checks.

Before downloading any large SDK, RTOS, toolchain, device pack, or library, perform a bounded
local-first discovery. Check explicit paths and environment variables, the current project and
its parents, and normal vendor install locations in the user's home and application directories.
This includes any existing vendor SDK, RTOS, compiler, debugger, and toolchain trees. Validate the discovered product, version, target support, and executable tools
before reuse; never trust a folder name alone and never recursively crawl the whole disk. Use a
network download only when no compatible local copy exists, and tell the user what was missing
before fetching a large dependency. Do not copy or persist unrelated files found during discovery.

Build firmware with the provider-neutral general helper argv template returned by
get_setup_status.build_guidance. First inspect the project's build files and resolve its exact
executable, argv, cwd, environment, and outputs. Prefer a compatible local installation, but when
none exists normal dependency/toolchain acquisition is allowed. Put the exact argv after `--`; the
helper executes it directly without a shell and inherits network access unless you intentionally
pass `--offline`. That flag applies best-effort environment guards for common dependency clients;
it is not an OS network sandbox, so inspect the actual process/log evidence. The server does not
detect or select a provider, SDK, compiler, target, fixed path, or output convention. After any native
build whose outputs are scattered or vendor-named, use the
always-visible collect_build_artifacts MCP tool with the
explicit paths the build actually produced. For guarded application or bootloader work, normally
collect a coherent ELF and linker map and set expected_roles to ["elf", "map"]. Do not ask the
collector to search or build. It only creates canonical, hashed provenance. Continue with the
matching flash plan and selected canonical ELF or HEX; use board_safety_refresh only for an actual
stable-map problem. Never treat collection as validation, flash permission, memory authority, or
an open hardware gate.

Dynamic clients should call the newly exposed action directly. Some MCP clients keep static callable
bindings even after notifications/tools/list_changed. If the accepted plan's action is absent
from those bindings, submit only the exact server-returned single-child action_batch fallback
unchanged. This is the sole narrow exception to the no-unlisted-call rule: never invent a hidden
child name, edit fallback arguments, or combine a primary action with a paired repair. The child
still passes through identical plan, permission, validation, gate, freshness, lock, timeout, budget,
event, and cleanup checks.

At startup, ask the user in ordinary conversation for one unique, familiar name for each board
they want to use in this project now, or "no board". Other visible debug probes may remain
unassigned. "no board" is a literal sentinel: pass it by itself, and treat it as never a candidate
board name. If it is mixed with names, clarify the answer before routing.
Never ask the user for structured data, board IDs, connection IDs, or permission values. Never
expose structured payloads, continuation tokens, or internal field names to the user.

After the user answers, call setup_overview with the familiar names. It lists known profiles,
friendly current connections, server-generated board IDs, and exact per-board next-call templates.
Copy those machine values into MCP calls; never ask the user to repeat or invent them. Route a
complete existing profile to validation with load_setup_tool(board_validate) and board_validate.
Follow the server-returned repair route for an incomplete same-identity profile, and the returned
safety-refresh route for a stable-map problem. Route an unknown name to setup through the all-NULL
board_setup-plan first; ask for the exact package-level MCU part number and the authoritative local
datasheet PDF, never a board type or digest, then load board_setup-plan and submit its populated plan
before any other hardware plan. If a physical match is
ambiguous, relay only server-provided friendly choices. Never silently choose, rename, reassign,
or rewrite a profile.

Ordinary conversation is never permission. When a tool requests approval, ask clearly in plain
language and pass approval only through the exact structured parameter named by that tool.

Keep every board isolated. Never reuse another board's validation, approval, plan, parameters,
or result. After a disconnect or the end of this Server Run, repeat validation before guarded
actions.

Validation has exactly three trigger categories: no live proof after initial setup or server
restart; connection identity change after disconnect, reconnect, probe change, or target override;
and possible hardware identity change after identity repair, mismatch, or destructive recovery.
Do not validate merely because of an ordinary build or relink, flash, reset or halt, UART work,
safety refresh or full map reconstruction, artifact collection, report, cache, or bookkeeping
change.

If no board is connected, do not begin setup, validation, or hardware actions."""


def register_initialization_handshake(
    mcp: FastMCP,
    registry: ToolRegistry,
    server_run: ServerRun | None = None,
) -> Callable[[], str]:
    """Register and return the no-argument handshake handler."""

    def initialization_handshake() -> str:
        """Return current operating guidance; call this first in each Server Run."""

        descriptions = {
            tool.name: tool.description or "Visible MCP tool."
            for tool in mcp._tool_manager.list_tools()  # type: ignore[reportPrivateUsage]
        }
        return build_initialization_guidance(registry, descriptions, server_run)

    mcp.add_tool(
        initialization_handshake,
        name=HANDSHAKE_TOOL_NAME,
        description=HANDSHAKE_DESCRIPTION,
        structured_output=False,
    )
    return initialization_handshake
