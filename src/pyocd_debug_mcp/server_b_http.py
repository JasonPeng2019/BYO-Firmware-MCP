"""Loopback streamable-HTTP entry point for the shared guarded Server B manager."""

from __future__ import annotations

import os

from pyocd_debug_mcp import server


def main() -> None:
    host = os.environ.get("BYO_SERVER_B_HOST", "127.0.0.1").strip()
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("Server B HTTP host must remain loopback-only")
    try:
        port = int(os.environ.get("BYO_SERVER_B_PORT", "8765"))
    except ValueError as exc:
        raise RuntimeError("BYO_SERVER_B_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("BYO_SERVER_B_PORT must be in 1..65535")
    server.mcp.settings.host = host
    server.mcp.settings.port = port
    server.mcp.settings.streamable_http_path = "/mcp"
    server.run_server_b("streamable-http")


if __name__ == "__main__":  # pragma: no cover
    main()
