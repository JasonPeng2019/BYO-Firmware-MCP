"""Side-effect-free console bootstrap for the MCP server.

The generated console script imports this module at top level. A spawned CAN
worker re-executes that script on platforms using multiprocessing ``spawn``;
therefore this module must not import :mod:`pyocd_debug_mcp.server` until the
console script actually calls :func:`main` in its parent-only ``__main__`` guard.
"""

from __future__ import annotations


def main() -> None:
    """Import and run the stateful server only in the real console process."""

    from pyocd_debug_mcp.server import main as server_main

    server_main()


__all__ = ["main"]
