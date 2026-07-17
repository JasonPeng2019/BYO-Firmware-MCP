"""MCP tool registration modules."""

from pyocd_debug_mcp.tools.handshake import (
    build_initialization_guidance,
    register_initialization_handshake,
)

__all__ = ["build_initialization_guidance", "register_initialization_handshake"]
