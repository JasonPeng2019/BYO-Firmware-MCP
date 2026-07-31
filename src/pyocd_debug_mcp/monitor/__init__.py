"""Autonomous issue monitor for BYO Server.

Observation is strictly passive: nothing here alters dispatch order, deadlines,
budget consumption, containment, or cleanup, and nothing it writes is ever read
back as authority. The single deliberate exception is the remote-logging
staleness backstop, which is the only authority this layer holds.
"""

from __future__ import annotations

from pyocd_debug_mcp.monitor.build_profile import NARRATIVE_LOGGING, profile_name
from pyocd_debug_mcp.monitor.monitor import (
    CHECKIN_PROMPT,
    IssueMonitor,
    MonitorContext,
    NullMonitor,
    Observation,
)
from pyocd_debug_mcp.monitor.tools import MONITOR_TOOL_NAMES, build_monitor_tools

__all__ = [
    "CHECKIN_PROMPT",
    "MONITOR_TOOL_NAMES",
    "NARRATIVE_LOGGING",
    "IssueMonitor",
    "MonitorContext",
    "NullMonitor",
    "Observation",
    "build_monitor_tools",
    "profile_name",
]
