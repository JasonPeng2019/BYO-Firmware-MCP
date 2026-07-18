"""Compatibility names for the former SWD-specific backend contract."""

from pyocd_debug_mcp.adapters.target_backend import TargetBackend, TargetSessionHandle

SWDInterface = TargetBackend

__all__ = ["SWDInterface", "TargetBackend", "TargetSessionHandle"]
