"""One isolated compatibility boundary for FastMCP schemas the public API cannot express."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase


def register_dynamic_tool(
    mcp: FastMCP,
    fn: Callable[..., Any],
    *,
    name: str,
    description: str | None,
    arg_model: type[ArgModelBase],
    parameters: Mapping[str, object],
) -> None:
    """Register one tool whose unbounded top-level fields require a custom schema."""

    tool = mcp._tool_manager.add_tool(  # type: ignore[reportPrivateUsage]
        fn,
        name=name,
        description=description,
    )
    tool.fn_metadata.arg_model = arg_model
    tool.parameters = dict(parameters)


def replace_tool_parameters(
    mcp: FastMCP, name: str, parameters: Mapping[str, object]
) -> None:
    """Replace only advertised JSON Schema while retaining normal FastMCP dispatch."""

    tool = mcp._tool_manager.get_tool(name)  # type: ignore[reportPrivateUsage]
    if tool is None:
        raise RuntimeError(f"FastMCP tool is not registered: {name}")
    tool.parameters = dict(parameters)


def tool_parameters(mcp: FastMCP, name: str) -> dict[str, object]:
    """Read one registered schema inside the same pinned-SDK compatibility boundary."""

    tool = mcp._tool_manager.get_tool(name)  # type: ignore[reportPrivateUsage]
    if tool is None:
        raise RuntimeError(f"FastMCP tool is not registered: {name}")
    return dict(tool.parameters)
