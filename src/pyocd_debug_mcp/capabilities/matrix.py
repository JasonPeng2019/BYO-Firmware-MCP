"""The code-owned tier route inventory used for discovery and dispatch parity.

The operator matrix explains these rows, but it is deliberately not an input:
adding a public tool without classifying it here is an integration error.  The
``guard`` value identifies whether the route is raw, contained/planned, a
direct hardware path, an indirect contained path, or non-hardware metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


RouteGuard = Literal["raw", "safe", "direct", "indirect", "metadata"]


@dataclass(frozen=True, slots=True)
class TierRoute:
    name: str
    guard: RouteGuard
    family: str | None = None
    raw_tool: str | None = None
    safe_tool: str | None = None
    requires_plan: bool = False
    personal_only: bool = False


def _routes() -> tuple[TierRoute, ...]:
    # Every currently registered public tool is represented.  The entries are
    # intentionally explicit rather than inferred from a name suffix: aliases,
    # finalizer/batch paths, and setup/maintenance routes need review too.
    rows = (
        TierRoute("action_batch", "indirect", "batch"),
        TierRoute("board_fix_setup", "metadata", "setup"),
        TierRoute("board_safety_refresh", "indirect", "maintenance"),
        TierRoute("board_setup", "metadata", "setup"),
        TierRoute("board_setup-plan", "metadata", "setup"),
        TierRoute("board_validate", "indirect", "identity-validation"),
        TierRoute("collect_build_artifacts", "metadata"),
        TierRoute("connect", "direct", "connection"),
        TierRoute("connect_override", "safe", "connection", requires_plan=True),
        TierRoute("connect_override-plan", "metadata", "connection"),
        TierRoute("connect_under_reset", "safe", "connection-under-reset", requires_plan=True),
        TierRoute("connect_under_reset-plan", "metadata", "connection-under-reset"),
        TierRoute("continue_setup", "metadata", "setup"),
        TierRoute("disconnect", "direct", "connection"),
        TierRoute("downgrade", "indirect", "downgrade"),
        TierRoute("find_symbol", "metadata"),
        TierRoute(
            "flash_application", "safe", "flash-application", "flash_raw", "flash_application", True
        ),
        TierRoute("flash_application-plan", "metadata", "flash-application"),
        TierRoute(
            "flash_bootloader", "safe", "flash-bootloader", "flash_raw", "flash_bootloader", True
        ),
        TierRoute("flash_bootloader-plan", "metadata", "flash-bootloader"),
        TierRoute("flash_raw", "raw", "flash", "flash_raw", "flash_application"),
        TierRoute("get_board_info", "direct", "board-info"),
        TierRoute("get_capabilities", "metadata", "capability-discovery"),
        TierRoute("get_discovery_hook_contract", "metadata"),
        TierRoute("get_setup_status", "metadata", "setup-status"),
        TierRoute("get_state", "direct", "state"),
        TierRoute("halt", "direct", "execution-control"),
        TierRoute("initialization_handshake", "metadata", "capability-discovery"),
        TierRoute("load_setup_tool", "metadata", "setup"),
        TierRoute("read_cpu_register", "direct", "cpu-register-read"),
        TierRoute("read_execution_state", "direct", "execution-control"),
        TierRoute(
            "read_memory_address",
            "safe",
            "memory-read",
            "read_memory_raw",
            "read_memory_address",
            True,
        ),
        TierRoute("read_memory_address-plan", "metadata", "memory-read"),
        TierRoute(
            "read_memory_raw", "raw", "memory-read", "read_memory_raw", "read_memory_address"
        ),
        TierRoute("read_memory_symbol", "indirect", "memory-read"),
        TierRoute("read_serial", "safe", "serial-read", "read_serial_raw", "read_serial", True),
        TierRoute("read_serial-plan", "metadata", "serial-read"),
        TierRoute("read_serial_raw", "raw", "serial-read", "read_serial_raw", "read_serial"),
        TierRoute("refresh_discovery_hooks", "metadata"),
        TierRoute("register_remote_probe", "metadata"),
        TierRoute(
            "register_write", "safe", "register-write", "register_write_raw", "register_write", True
        ),
        TierRoute("register_write-plan", "metadata", "register-write"),
        TierRoute(
            "register_write_raw", "raw", "register-write", "register_write_raw", "register_write"
        ),
        TierRoute("remove_breakpoint", "direct", "breakpoint"),
        TierRoute("report_agent_issue", "metadata"),
        TierRoute(
            "reset_and_halt", "safe", "reset-halt", "reset_and_halt_raw", "reset_and_halt", True
        ),
        TierRoute("reset_and_halt-plan", "metadata", "reset-halt"),
        TierRoute(
            "reset_and_halt_raw", "raw", "reset-halt", "reset_and_halt_raw", "reset_and_halt"
        ),
        TierRoute("reset_and_run", "direct", "execution-control"),
        TierRoute("resume", "direct", "execution-control"),
        TierRoute(
            "serial_exchange",
            "safe",
            "serial-exchange",
            "serial_exchange_raw",
            "serial_exchange",
            True,
        ),
        TierRoute("serial_exchange-plan", "metadata", "serial-exchange"),
        TierRoute(
            "serial_exchange_raw",
            "raw",
            "serial-exchange",
            "serial_exchange_raw",
            "serial_exchange",
        ),
        TierRoute("server_health_check", "metadata"),
        TierRoute(
            "set_breakpoint", "safe", "breakpoint", "set_breakpoint_raw", "set_breakpoint", True
        ),
        TierRoute("set_breakpoint-plan", "metadata", "breakpoint"),
        TierRoute(
            "set_breakpoint_raw", "raw", "breakpoint", "set_breakpoint_raw", "set_breakpoint"
        ),
        TierRoute(
            "set_execution_state",
            "safe",
            "execution-control",
            "set_execution_state_raw",
            "set_execution_state",
            True,
        ),
        TierRoute("set_execution_state-plan", "metadata", "execution-control"),
        TierRoute(
            "set_execution_state_raw",
            "raw",
            "execution-control",
            "set_execution_state_raw",
            "set_execution_state",
        ),
        TierRoute("setup_overview", "metadata", "setup"),
        TierRoute("step", "direct", "execution-control"),
        TierRoute("submit_routine_checkin", "metadata", personal_only=True),
        TierRoute(
            "target_unlock", "safe", "target-recovery", "target_unlock_raw", "target_unlock", True
        ),
        TierRoute("target_unlock-plan", "metadata", "target-recovery"),
        TierRoute(
            "target_unlock_raw", "raw", "target-recovery", "target_unlock_raw", "target_unlock"
        ),
        TierRoute("unlock_operator", "metadata"),
        TierRoute("unregister_remote_probe", "metadata"),
        TierRoute("wait", "metadata"),
        TierRoute(
            "write_cpu_register",
            "safe",
            "cpu-register-write",
            "write_cpu_register_raw",
            "write_cpu_register",
            True,
        ),
        TierRoute("write_cpu_register-plan", "metadata", "cpu-register-write"),
        TierRoute(
            "write_cpu_register_raw",
            "raw",
            "cpu-register-write",
            "write_cpu_register_raw",
            "write_cpu_register",
        ),
        TierRoute("write_memory", "safe", "memory-write", "write_memory_raw", "write_memory", True),
        TierRoute("write_memory-plan", "metadata", "memory-write"),
        TierRoute("write_memory_raw", "raw", "memory-write", "write_memory_raw", "write_memory"),
        TierRoute("write_serial", "safe", "serial-write", "write_serial_raw", "write_serial", True),
        TierRoute("write_serial-plan", "metadata", "serial-write"),
        TierRoute("write_serial_raw", "raw", "serial-write", "write_serial_raw", "write_serial"),
    )
    names = [row.name for row in rows]
    if len(names) != len(set(names)):
        raise RuntimeError("tier route inventory contains duplicate public tool names")
    return rows


TIER_ROUTE_MATRIX = _routes()
ROUTE_BY_NAME = {route.name: route for route in TIER_ROUTE_MATRIX}


def route_names(*guards: RouteGuard) -> tuple[str, ...]:
    wanted = set(guards)
    return tuple(route.name for route in TIER_ROUTE_MATRIX if route.guard in wanted)


def applicable_route_names(*, narrative_logging: bool) -> tuple[str, ...]:
    """Return the public inventory for the selected shipped build profile."""

    return tuple(
        route.name for route in TIER_ROUTE_MATRIX if narrative_logging or not route.personal_only
    )


def raw_tool_for(operation: str) -> str:
    route = ROUTE_BY_NAME.get(operation)
    if route is not None and route.raw_tool is not None:
        return route.raw_tool
    return f"{operation}_raw"


def safe_tool_for(operation: str) -> str:
    route = ROUTE_BY_NAME.get(operation)
    if route is not None and route.safe_tool is not None:
        return route.safe_tool
    return operation.removesuffix("_raw")


__all__ = [
    "ROUTE_BY_NAME",
    "TIER_ROUTE_MATRIX",
    "TierRoute",
    "applicable_route_names",
    "raw_tool_for",
    "route_names",
    "safe_tool_for",
]
