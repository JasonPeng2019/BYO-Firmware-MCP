"""Opt-in AT10 real-MCP evidence runner for nRF52840 and STM32L476RTG.

Nothing in this module imports pyOCD, enumerates a probe, or starts an MCP
server at import time. ``--execute`` is the only live path. It requires a
private schema-v2 fixture containing a separate isolated project root for
each HW01--HW06 case and exact calls with expected response fragments. The
runner never chooses a probe, infers a target, manufactures a plan, creates a
manual grant, or executes target unlock/mass erase.

The fixture is an evidence script, not a board database: every hardware call
has an exact binding, arguments, and expected result. The transcript records
expected and actual public MCP responses. A missing fixture/proof is BLOCKED
(exit 2); a response mismatch is FAIL (exit 1), never a pass.

HW04 additionally needs --allow-write-flash and per-board RAM/flash approval.
HW05 pauses for the human to invoke the shipped manual-only skills, then reads
the resulting project-local records; it never writes or synthesizes a grant.
It checks mass-erase grant/restart state only and never calls target_unlock.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BOARD_NAMES = ("nrf52840", "stm32l476rtg")
ALL_CASES = ("HW01", "HW02", "HW03", "HW04", "HW05", "HW06")
BOARD_KEYS = frozenset(
    {
        "board_id",
        "model",
        "probe_uid",
        "target",
        "serial_binding",
        "expected_tier",
        "safe_ram",
        "flash_fixture",
        "restoration",
        "full_evidence",
        "disruption_approval",
    }
)
_BOARD_KEYS_WITH_CONNECTION_ID = BOARD_KEYS | frozenset({"connection_id"})
_FLASH_LANES = frozenset({"raw", "safe"})
_LEGACY_FLASH_FIXTURE_FIELDS = frozenset(
    {
        "artifact",
        "restore_artifact",
        "address",
        "erase_footprint",
        "post_flash_readback",
        "post_restore_readback",
    }
)
_FLASH_FIXTURE_FIELDS = _LEGACY_FLASH_FIXTURE_FIELDS | frozenset({"integrity"})
_SHA256_HEX_LENGTH = 64
_PUBLIC_SAFE_EXIT = (
    "Safe exit: leave the board in the intended run state, then disconnect when hardware "
    "work is complete."
)
_PUBLIC_MEMORY_RESULT = re.compile(
    r"(?:0x(?:[0-9A-Fa-f]{2}|[0-9A-Fa-f]{4}|[0-9A-Fa-f]{8})|"
    r"[0-9A-Fa-f]{2}(?: [0-9A-Fa-f]{2})*)"
)
_PUBLIC_FLASH_RESULT = re.compile(
    r"Flashed .+ as flash_application within its mapped partition; "
    r"(?:target left (?:running|halted)\.|"
    r"final reset state is unconfirmed; reconnect and check target state before use\.)"
)
_HW04_RAW_PREFIX_MASK = bytes((0xA5, 0x5A, 0x3C, 0xC3) * 4)
_HW04_EXECUTABLE_CONTENT = {
    "nrf52840": {
        "raw": {
            "ranges": ((0x000FF000, 0x00100000),),
            "delta": {"kind": "xor", "start": 0x000FF000, "mask": _HW04_RAW_PREFIX_MASK},
        },
        "safe": {
            "ranges": (
                (0x00000000, 0x00001000),
                (0x00012000, 0x00013000),
                (0x000FF000, 0x00100000),
            ),
            "readback_address": 0x000FF000,
            "delta": {
                "kind": "replace",
                "start": 0x000FF000,
                "before": b"\xff" * 4,
                "after": b"\xa5" * 4,
            },
        },
    },
    "stm32l476rtg": {
        "raw": {
            "ranges": ((0x080FF800, 0x08100000),),
            "delta": {"kind": "xor", "start": 0x080FF800, "mask": _HW04_RAW_PREFIX_MASK},
        },
        "safe": {
            "ranges": ((0x08000000, 0x08000800), (0x08000800, 0x08001000)),
            "delta": {
                "kind": "replace",
                "start": 0x08000D94,
                "before": b"\xff" * 4,
                "after": b"\xa5" * 4,
            },
        },
    },
}
_HW04_RAW_LIFECYCLE = (
    "raw_connect",
    "initial_ram_read",
    "raw_ram_write",
    "raw_ram_readback",
    "raw_ram_restore",
    "ram_restored_readback",
    "initial_flash_readback",
    "raw_flash",
    "raw_flash_readback",
    "raw_restore",
    "raw_restored_readback",
    "final_disconnect",
)
_HW04_RAW_FLASH_ONLY_LIFECYCLE = (
    "raw_connect",
    "initial_flash_readback",
    "raw_flash",
    "raw_flash_readback",
    "raw_restore",
    "raw_restored_readback",
    "final_disconnect",
)
_HW04_FULL_LIFECYCLE = (
    "full_assign",
    "full_connect",
    "full_load_validation_tool",
    "full_board_validate",
    "safe_flash_plan_guide",
    "safe_flash_plan_accept",
    "safe_flash",
    "safe_containment_refusal_plan_guide",
    "safe_containment_refusal_plan_accept",
    "safe_containment_refusal",
    "safe_flash_readback_plan_guide",
    "safe_flash_readback_plan_accept",
    "safe_flash_readback",
    "safe_restore_plan_guide",
    "safe_restore_plan_accept",
    "safe_restore",
    "safe_restored_readback_plan_guide",
    "safe_restored_readback_plan_accept",
    "safe_restored_readback",
    "final_disconnect",
)
_HW04_SAFE_PREFLIGHT_PAGE_BASES = tuple(f"safe_preflight_page_{index}" for index in range(3))
_HW04_SAFE_RESTORED_PAGE_BASES = tuple(f"safe_restored_page_{index}" for index in range(3))
_HW04_SAFE_PAGE_PROOF_BASES = (
    *_HW04_SAFE_PREFLIGHT_PAGE_BASES,
    *_HW04_SAFE_RESTORED_PAGE_BASES,
)
_HW04_SAFE_PAGE_PROOF_IDS = frozenset(
    identifier
    for base in _HW04_SAFE_PAGE_PROOF_BASES
    for identifier in (f"{base}_plan_guide", f"{base}_plan_accept", base)
)
REQUIRED_STEP_IDS = {
    "HW01": frozenset(
        {
            "discover",
            "assign",
            "connect",
            "read_register",
            "read_memory",
            "disconnect",
            "reconnect",
            "final_disconnect",
        }
    ),
    "HW02": frozenset(
        {
            "setup_overview",
            "load_setup_tool",
            "lite_setup_plan_guide",
            "lite_setup_plan_accept",
            "lite_setup",
            "lite_target_confirmation",
            "lite_confirmation",
            "lite_repair",
            "connect",
            "capabilities",
            "safe_read_plan_guide",
            "safe_read_plan_accept",
            "safe_read",
            "out_of_map_safe_refusal_plan_guide",
            "out_of_map_safe_refusal_plan_accept",
            "out_of_map_safe_refusal",
            "raw_read",
            "non_map_operation",
        }
    ),
    "HW03": frozenset(
        {
            "capabilities",
            "assign",
            "connect",
            "load_setup_tool",
            "board_validate",
            "safe_operation_plan_guide",
            "safe_operation_plan_accept",
            "safe_operation",
            "raw_refusal",
        }
    ),
    "HW04": frozenset(set()),
    "HW06": frozenset({"capabilities", "connect", "route_read", "disconnect"}),
}
HW04_RAW_IDS = frozenset(
    {
        "raw_connect",
        "initial_ram_read",
        "raw_ram_write",
        "raw_ram_readback",
        "raw_ram_restore",
        "ram_restored_readback",
        "initial_flash_readback",
        "raw_flash",
        "raw_flash_readback",
        "raw_restore",
        "raw_restored_readback",
        "final_disconnect",
    }
)
HW04_FULL_IDS = frozenset(
    {
        "full_assign",
        "full_connect",
        "full_load_validation_tool",
        "full_board_validate",
        "safe_flash_plan_guide",
        "safe_flash_plan_accept",
        "safe_flash",
        "safe_containment_refusal_plan_guide",
        "safe_containment_refusal_plan_accept",
        "safe_containment_refusal",
        "safe_flash_readback_plan_guide",
        "safe_flash_readback_plan_accept",
        "safe_flash_readback",
        "safe_restore_plan_guide",
        "safe_restore_plan_accept",
        "safe_restore",
        "safe_restored_readback_plan_guide",
        "safe_restored_readback_plan_accept",
        "safe_restored_readback",
        "final_disconnect",
    }
)
HW05_PRE_IDS = frozenset(
    {
        "capabilities",
        "locked_downgrade_refusal",
        "manual_mass_erase_pause",
        "mass_erase_plan_guide",
        "mass_erase_disclosure",
        "mass_erase_reservation",
        "manual_downgrade_pause",
        "unlock_operator",
        "downgrade",
        "inactive_evidence",
    }
)
HW05_POST_IDS = frozenset(
    {
        "restart_capabilities",
        "restart_stale_downgrade_refusal",
        "restart_stale_mass_erase_refusal",
    }
)
_MANUAL_ROOT = Path(".agent-workspace") / "runtime" / "manual-permissions"
_FORBIDDEN_TOOLS = frozenset({"target_unlock", "target_unlock_raw", "mass_erase"})
_LITE_SETUP_CONTINUATION = "$capture.lite_setup.completed.0.result.continuation_id"
_VALIDATION_PROBE_ID = "$capture.validation_loader.next_call.arguments.probe_id"

# Every guarded operation recorded below uses the public three-part plan
# protocol: the complete NULL guide, the populated accepted plan, and the
# server-returned one-child action_batch fallback.  Keeping this table here
# makes a fixture unable to smuggle a direct safe action past a missing plan.
_PLAN_ENVELOPE = frozenset(
    {
        "board_id",
        "hypothesis",
        "strategy",
        "hypothesis_made",
        "strategy_evaluated",
        "expected_fail_return",
        "expected_success_return",
        "max_calls",
        "max_calls_buffer",
        "action_parameters",
        "user_permission",
    }
)
_PLAN_GUIDE_ARGUMENTS = {field: None for field in _PLAN_ENVELOPE}
_PLAN_PROTOCOLS: dict[str, dict[str, dict[str, object]]] = {
    "HW02": {
        "lite_setup": {
            "plan_tool": "board_setup-plan",
            "action": "board_setup",
            "permission": "one-time",
            "parameters": frozenset(
                {
                    "target_tier",
                    "mode",
                    "connection_id",
                    "display_name",
                    "mcu_part_number",
                    "requires_uart",
                    "serial_baudrate",
                    "serial_id",
                    "datasheet_path",
                }
            ),
        },
        "safe_read": {
            "plan_tool": "read_memory_address-plan",
            "action": "read_memory_address",
            "permission": None,
            "parameters": frozenset({"address", "width", "length"}),
        },
        "out_of_map_safe_refusal": {
            "plan_tool": "read_memory_address-plan",
            "action": "read_memory_address",
            "permission": None,
            "parameters": frozenset({"address", "width", "length"}),
        },
    },
    "HW03": {
        "safe_operation": {
            "plan_tool": "read_memory_address-plan",
            "action": "read_memory_address",
            "permission": None,
            "parameters": frozenset({"address", "width", "length"}),
        },
    },
    "HW04": {
        "safe_flash": {
            "plan_tool": "flash_application-plan",
            "action": "flash_application",
            "permission": None,
            "parameters": frozenset({"artifact"}),
        },
        "safe_containment_refusal": {
            "plan_tool": "write_memory-plan",
            "action": "write_memory",
            "permission": None,
            "parameters": frozenset(
                {
                    "symbol_or_address",
                    "value",
                    "width",
                    "allow_address_fallback",
                    "reason",
                    "elf_artifact",
                }
            ),
        },
        "safe_flash_readback": {
            "plan_tool": "read_memory_address-plan",
            "action": "read_memory_address",
            "permission": None,
            "parameters": frozenset({"address", "width", "length"}),
        },
        "safe_restore": {
            "plan_tool": "flash_application-plan",
            "action": "flash_application",
            "permission": None,
            "parameters": frozenset({"artifact"}),
        },
        "safe_restored_readback": {
            "plan_tool": "read_memory_address-plan",
            "action": "read_memory_address",
            "permission": None,
            "parameters": frozenset({"address", "width", "length"}),
        },
    },
    "HW06": {
        "route_read": {
            "plan_tool": "read_memory_address-plan",
            "action": "read_memory_address",
            "permission": None,
            "parameters": frozenset({"address", "width", "length"}),
        },
    },
}
for _page_proof_base in _HW04_SAFE_PAGE_PROOF_BASES:
    _PLAN_PROTOCOLS["HW04"][_page_proof_base] = {
        "plan_tool": "read_memory_address-plan",
        "action": "read_memory_address",
        "permission": None,
        "parameters": frozenset({"address", "width", "length"}),
    }

# A checklist ID is evidence for one precise public behavior, not a free-form
# transcript label.  These contracts intentionally name the caller-visible
# MCP tool, its minimum binding fields, and the result fragment that proves
# the behavior.  A private fixture may add arguments/fragments, but it cannot
# substitute an unrelated observation such as get_capabilities for a memory
# containment check.
_STEP_CONTRACTS: dict[str, dict[str, dict[str, object]]] = {
    "HW01": {
        "discover": {
            "tool": "setup_overview",
            "arguments": {"board_names"},
            "text": ("$board_id", "$probe_uid"),
        },
        "assign": {
            "tool": "setup_overview",
            "arguments": {"board_names", "connection_assignments"},
            "text": ("$board_id", "$probe_uid"),
        },
        "connect": {
            "tool": "connect",
            "arguments": {"board_id", "probe_uid"},
            "probe_uid": True,
            "without": {"target"},
            "text": ("$board_id", "$probe_uid", "$target"),
        },
        "read_register": {
            "tool": "read_cpu_register",
            "arguments": {"board_id", "name"},
            "text": ("0x",),
        },
        "read_memory": {
            "tool": "read_memory_raw",
            "arguments": {"board_id", "address"},
            "payload": {"status": "ok", "tier": "no-setup", "raw": True},
        },
        "disconnect": {"tool": "disconnect", "arguments": {"board_id"}, "text": ("$board_id",)},
        "reconnect": {
            "tool": "connect",
            "arguments": {"board_id", "probe_uid"},
            "probe_uid": True,
            "target": True,
            "text": ("$board_id", "$probe_uid", "$target"),
        },
        "final_disconnect": {
            "tool": "disconnect",
            "arguments": {"board_id"},
            "text": ("$board_id",),
        },
    },
    "HW02": {
        "setup_overview": {
            "tool": "setup_overview",
            "arguments": {"board_names"},
            "text": ("$board_id", "$probe_uid"),
        },
        "load_setup_tool": {
            "tool": "load_setup_tool",
            "arguments": {"board_id", "tool_name"},
        },
        "lite_setup": {
            "tool": "action_batch",
            "arguments": {"board_id", "actions"},
            "payload": {"status": "batch_completed", "completed": [{"tool_name": "board_setup"}]},
        },
        "lite_target_confirmation": {
            "tool": "continue_setup",
            "arguments": {"board_id", "continuation_id", "response"},
            "payload": {
                "status": "setup_continuation_accepted",
                "accepted": "target",
                "pyocd_target": "nrf52840",
            },
        },
        "lite_confirmation": {
            "tool": "continue_setup",
            "arguments": {"board_id", "continuation_id", "response"},
        },
        "lite_repair": {
            "tool": "action_batch",
            "arguments": {"board_id", "actions"},
            "payload": {
                "status": "batch_completed",
                "completed": [{"tool_name": "board_fix_setup"}],
            },
        },
        "connect": {
            "tool": "connect",
            "arguments": {"board_id"},
            "text": ("$board_id", "$probe_uid"),
        },
        "capabilities": {
            "tool": "get_capabilities",
            "arguments": {"board_id"},
            "payload": {"status": "capability_status", "tier": "setup-lite"},
        },
        "safe_read": {
            "tool": "action_batch",
            "arguments": {"board_id", "actions"},
            "payload": {
                "status": "batch_completed",
                "completed": [{"tool_name": "read_memory_address"}],
            },
        },
        "out_of_map_safe_refusal": {
            "tool": "action_batch",
            "arguments": {"board_id", "actions"},
            "payload": {
                "status": "batch_completed",
                "completed": [{"tool_name": "read_memory_address"}],
            },
        },
        "raw_read": {
            "tool": "read_memory_raw",
            "arguments": {"board_id", "address"},
            "payload": {
                "status": "ok",
                "tier": "setup-lite",
                "raw": True,
                "warning": {"code": "tier/lite-containment-bypassed", "display_to_human": True},
            },
        },
        "non_map_operation": {
            "tool": "read_cpu_register",
            "arguments": {"board_id", "name"},
            "text": ("0x",),
        },
    },
    "HW03": {
        "capabilities": {
            "tool": "get_capabilities",
            "arguments": {"board_id"},
            "payload": {"status": "capability_status", "tier": "setup-full"},
        },
        "assign": {
            "tool": "setup_overview",
            "arguments": {"board_names", "connection_assignments"},
            "text": ("$board_id", "$probe_uid"),
        },
        "connect": {
            "tool": "connect",
            "arguments": {"board_id"},
            "text": ("$board_id", "$probe_uid"),
        },
        "load_setup_tool": {
            "tool": "load_setup_tool",
            "arguments": {"board_id", "tool_name"},
            "payload": {"status": "setup_tool_loaded", "tool_name": "board_validate"},
            "capture": "validation_loader",
        },
        "board_validate": {
            "tool": "board_validate",
            "arguments": {"board_id", "probe_id"},
            "payload": {"status": "validation_passed", "code": "validation/passed"},
            "reference": {"probe_id": _VALIDATION_PROBE_ID},
        },
        "safe_operation": {
            "tool": "action_batch",
            "arguments": {"board_id", "actions"},
            "payload": {
                "status": "batch_completed",
                "completed": [{"tool_name": "read_memory_address"}],
            },
        },
        "raw_refusal": {
            "tool": "read_memory_raw",
            "arguments": {"board_id", "address"},
            "mcp_error": True,
            "payload": {
                "status": "refused",
                "code": "tier/wrong-route",
                "operation": "read_memory_raw",
            },
        },
    },
    "HW04": {
        "raw_connect": {
            "tool": "connect",
            "arguments": {"board_id", "probe_uid", "target"},
            "probe_uid": True,
            "target": True,
            "text": ("$probe_uid",),
        },
        "full_connect": {
            "tool": "connect",
            "arguments": {"board_id"},
            "text": ("$probe_uid",),
        },
        "full_assign": {
            "tool": "setup_overview",
            "arguments": {"board_names", "connection_assignments"},
            "payload": {"status": "setup_routes_ready"},
            "text": ("$board_id",),
        },
        "full_load_validation_tool": {
            "tool": "load_setup_tool",
            "arguments": {"board_id", "tool_name"},
            "payload": {"status": "setup_tool_loaded", "tool_name": "board_validate"},
            "capture": "full_validation_loader",
        },
        "full_board_validate": {
            "tool": "board_validate",
            "arguments": {"board_id", "probe_id"},
            "payload": {"status": "validation_passed", "code": "validation/passed"},
            "reference": {
                "probe_id": "$capture.full_validation_loader.next_call.arguments.probe_id"
            },
        },
        "initial_ram_read": {
            "tool": "read_memory_raw",
            "arguments": {"board_id", "address"},
            "payload": {"status": "ok", "operation": "read_memory_raw", "tier": "no-setup"},
            "capture": "initial_ram",
        },
        "raw_ram_write": {
            "tool": "write_memory_raw",
            "arguments": {"board_id", "address", "value"},
            "payload": {"status": "ok", "operation": "write_memory_raw", "tier": "no-setup"},
        },
        "raw_ram_readback": {
            "tool": "read_memory_raw",
            "arguments": {"board_id", "address"},
            "payload": {"status": "ok", "operation": "read_memory_raw", "tier": "no-setup"},
        },
        "raw_ram_restore": {
            "tool": "write_memory_raw",
            "arguments": {"board_id", "address", "value"},
            "payload": {"status": "ok", "operation": "write_memory_raw", "tier": "no-setup"},
            "reference": {"value": "$capture.initial_ram.result"},
        },
        "ram_restored_readback": {
            "tool": "read_memory_raw",
            "arguments": {"board_id", "address"},
            "payload": {"status": "ok", "operation": "read_memory_raw", "tier": "no-setup"},
        },
        "initial_flash_readback": {
            "tool": "read_memory_raw",
            "arguments": {"board_id", "address"},
            "payload": {"status": "ok", "operation": "read_memory_raw", "tier": "no-setup"},
            "capture": "raw_flash_initial",
        },
        "raw_flash": {
            "tool": "flash_raw",
            "arguments": {"board_id", "artifact"},
            "payload": {"status": "ok", "operation": "flash_raw", "tier": "no-setup"},
        },
        "raw_flash_readback": {
            "tool": "read_memory_raw",
            "arguments": {"board_id", "address"},
            "payload": {"status": "ok", "operation": "read_memory_raw", "tier": "no-setup"},
            "capture": "raw_flash_observed",
        },
        "raw_restore": {
            "tool": "flash_raw",
            "arguments": {"board_id", "artifact"},
            "payload": {"status": "ok", "operation": "flash_raw", "tier": "no-setup"},
        },
        "raw_restored_readback": {
            "tool": "read_memory_raw",
            "arguments": {"board_id", "address"},
            "payload": {"status": "ok", "operation": "read_memory_raw", "tier": "no-setup"},
            "capture": "raw_flash_restored",
        },
        "safe_flash": {
            "tool": "action_batch",
            "arguments": {"board_id", "actions"},
            "payload": {
                "status": "batch_completed",
                "completed": [{"tool_name": "flash_application"}],
            },
        },
        "safe_containment_refusal": {
            "tool": "action_batch",
            "arguments": {"board_id", "actions"},
            "payload": {
                "status": "batch_completed",
                "completed": [{"tool_name": "write_memory"}],
            },
        },
        "safe_flash_readback": {
            "tool": "action_batch",
            "arguments": {"board_id", "actions"},
            "payload": {
                "status": "batch_completed",
                "completed": [{"tool_name": "read_memory_address"}],
            },
        },
        "safe_restore": {
            "tool": "action_batch",
            "arguments": {"board_id", "actions"},
            "payload": {
                "status": "batch_completed",
                "completed": [{"tool_name": "flash_application"}],
            },
        },
        "safe_restored_readback": {
            "tool": "action_batch",
            "arguments": {"board_id", "actions"},
            "payload": {
                "status": "batch_completed",
                "completed": [{"tool_name": "read_memory_address"}],
            },
            "capture": "safe_flash_restored",
        },
        "final_disconnect": {
            "tool": "disconnect",
            "arguments": {"board_id"},
            "text": ("$board_id",),
        },
    },
    "HW05": {
        "capabilities": {
            "tool": "get_capabilities",
            "arguments": {"board_id"},
            "payload": {"status": "capability_status", "tier": "setup-full"},
        },
        "locked_downgrade_refusal": {
            "tool": "downgrade",
            "arguments": {"board_id", "permission"},
            "payload": {"status": "refused", "action": "downgrade"},
        },
        "mass_erase_disclosure": {
            "tool": "target_unlock-plan",
            "arguments": {"board_id", "action_parameters"},
            "payload": {"status": "unlock_permission_requested"},
            "capture": "mass_erase_disclosure",
        },
        "mass_erase_reservation": {
            "tool": "target_unlock-plan",
            "arguments": {"board_id", "action_parameters", "user_permission"},
            "payload": {"status": "unlock_plan_approved", "underlying_tool": "target_unlock"},
            "capture": "mass_erase_approval",
        },
        "unlock_operator": {
            "tool": "unlock_operator",
            "arguments": {"board_id", "action", "grant_id"},
            "payload": {"status": "operator_permission_issued", "action": "downgrade"},
            "capture": "downgrade_permission",
            "reference": {"grant_id": "$capture.downgrade_grant.grant_id"},
        },
        "downgrade": {
            "tool": "downgrade",
            "arguments": {"board_id", "permission"},
            "payload": {"status": "downgraded", "tier": "no-setup", "setup_incomplete": True},
            "reference": {"permission": "$capture.downgrade_permission.permission"},
        },
        "inactive_evidence": {
            "tool": "get_capabilities",
            "arguments": {"board_id"},
            "payload": {
                "status": "capability_status",
                "tier": "no-setup",
                "setup_incomplete": True,
            },
        },
        "restart_capabilities": {
            "tool": "get_capabilities",
            "arguments": {"board_id"},
            "payload": {
                "status": "capability_status",
                "tier": "no-setup",
                "setup_incomplete": True,
            },
        },
        "restart_stale_downgrade_refusal": {
            "tool": "unlock_operator",
            "arguments": {"board_id", "action", "grant_id"},
            "payload": {"status": "refused", "action": "downgrade"},
            "reference": {"grant_id": "$capture.downgrade_grant.grant_id"},
        },
        "restart_stale_mass_erase_refusal": {
            "tool": "target_unlock-plan",
            "arguments": {"board_id", "action_parameters", "user_permission"},
            "payload": {"status": "refused"},
        },
    },
    "HW06": {
        "capabilities": {
            "tool": "get_capabilities",
            "arguments": {"board_id"},
            "payload": {"status": "capability_status"},
        },
        "assign": {
            "tool": "setup_overview",
            "arguments": {"board_names", "connection_assignments"},
            "text": ("$board_id", "$probe_uid"),
        },
        "connect": {"tool": "connect", "arguments": {"board_id"}, "text": ("$board_id",)},
        "load_setup_tool": {
            "tool": "load_setup_tool",
            "arguments": {"board_id", "tool_name"},
            "payload": {"status": "setup_tool_loaded", "tool_name": "board_validate"},
            "capture": "validation_loader",
        },
        "board_validate": {
            "tool": "board_validate",
            "arguments": {"board_id", "probe_id"},
            "payload": {"status": "validation_passed", "code": "validation/passed"},
            "reference": {"probe_id": _VALIDATION_PROBE_ID},
        },
        "route_read": {
            "tool": "action_batch",
            "arguments": {"board_id", "actions"},
            "payload": {
                "status": "batch_completed",
                "completed": [{"tool_name": "read_memory_address"}],
            },
        },
        "disconnect": {"tool": "disconnect", "arguments": {"board_id"}, "text": ("$board_id",)},
    },
}


class FixtureError(ValueError):
    """The private fixture is incomplete, ambiguous, or unsafe to execute."""


class CheckFailed(RuntimeError):
    """One actual MCP response did not meet the explicit hardware oracle."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        # This transcript stays inside the opt-in fixture result.  It is never
        # printed while executing hardware and lets the caller retain the
        # completed calls plus the failed public response.
        self.rows: list[dict[str, Any]] = []


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FixtureError(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixtureError(f"{label} must be an explicit non-empty string")
    return value.strip()


def _provider_qualified_connection_id(value: object, label: str) -> str:
    """Require the public server's canonical, provider-qualified connection token shape."""

    connection_id = _text(value, label)
    if not connection_id.casefold().startswith("probeid:"):
        raise FixtureError(f"{label} must be a provider-qualified server-issued token")
    provider, separator, opaque_id = connection_id[8:].partition(":")
    if not separator or not provider or not opaque_id:
        raise FixtureError(f"{label} must be a provider-qualified server-issued token")
    return connection_id


def _absolute(value: object, label: str) -> Path:
    path = Path(_text(value, label)).expanduser()
    if not path.is_absolute():
        raise FixtureError(f"{label} must be an absolute path")
    return path.resolve()


def _project_root(value: object, label: str) -> Path:
    """Accept only an existing private project with a physical local runtime."""

    root = _absolute(value, label)
    workspace = root / ".agent-workspace"
    runtime = workspace / "runtime"
    if not root.is_dir() or root.is_symlink():
        raise FixtureError(f"{label} must be an existing physical project directory")
    if (
        not workspace.is_dir()
        or workspace.is_symlink()
        or workspace.resolve() != workspace.absolute()
    ):
        raise FixtureError(f"{label} must contain a physical project-local .agent-workspace")
    if not runtime.is_dir() or runtime.is_symlink() or runtime.resolve() != runtime.absolute():
        raise FixtureError(
            f"{label} must contain a physical project-local .agent-workspace/runtime"
        )
    return root


def _require_distinct_projects(roots: Sequence[Path]) -> None:
    """Reject aliases and runtimes shared by otherwise different case roots."""

    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            if (
                first == second
                or first.samefile(second)
                or (first / ".agent-workspace" / "runtime").samefile(
                    second / ".agent-workspace" / "runtime"
                )
            ):
                raise FixtureError(
                    "all AT10 case roots and runtimes must be distinct physical projects"
                )


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise FixtureError(
            f"{label} has missing={sorted(expected.difference(value))}, extra={sorted(set(value).difference(expected))}"
        )


def _validate_flash_lane(
    raw: object, label: str, *, require_integrity: bool = True
) -> dict[str, Any]:
    """Validate one executable HW04 flash lane without choosing a target route."""

    flash = dict(_mapping(raw, label))
    _exact_keys(
        flash,
        _FLASH_FIXTURE_FIELDS if require_integrity else _LEGACY_FLASH_FIXTURE_FIELDS,
        label,
    )
    for field in ("artifact", "restore_artifact"):
        flash[field] = str(_absolute(flash[field], f"{label}.{field}"))
    if (
        not isinstance(flash["address"], int)
        or isinstance(flash["address"], bool)
        or flash["address"] < 0
    ):
        raise FixtureError(f"{label}.address must be a non-negative integer")
    if not isinstance(flash["erase_footprint"], list) or not flash["erase_footprint"]:
        raise FixtureError(f"{label}.erase_footprint must be a non-empty list")
    for index, span in enumerate(flash["erase_footprint"]):
        row = _mapping(span, f"{label}.erase_footprint[{index}]")
        _exact_keys(row, frozenset({"start", "end"}), f"{label}.erase_footprint[{index}]")
        if any(not isinstance(item, int) or isinstance(item, bool) for item in row.values()):
            raise FixtureError(f"{label}.erase_footprint[{index}] bounds must be integers")
        if row["start"] < 0 or row["end"] <= row["start"]:
            raise FixtureError(f"{label}.erase_footprint[{index}] is invalid")
    for field in ("post_flash_readback", "post_restore_readback"):
        _mapping(flash[field], f"{label}.{field}")
    if require_integrity:
        flash["integrity"] = _validate_flash_integrity(flash["integrity"], f"{label}.integrity")
    return flash


def _sha256_hex(value: object, label: str) -> str:
    digest = _text(value, label)
    if len(digest) != _SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise FixtureError(f"{label} must be a lowercase SHA-256 hex digest")
    return digest


def _integrity_ranges(value: object, label: str) -> list[dict[str, int]]:
    if not isinstance(value, list) or not value:
        raise FixtureError(f"{label} must be a non-empty list")
    ranges: list[dict[str, int]] = []
    previous_end = -1
    for index, raw_span in enumerate(value):
        span = _mapping(raw_span, f"{label}[{index}]")
        _exact_keys(span, frozenset({"start", "end"}), f"{label}[{index}]")
        start, end = span["start"], span["end"]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or start < previous_end
        ):
            raise FixtureError(f"{label}[{index}] must be ordered non-empty integer bounds")
        ranges.append({"start": start, "end": end})
        previous_end = end
    return ranges


def _hex_bytes(value: object, label: str) -> bytes:
    text = _text(value, label)
    if len(text) % 2 or any(character not in "0123456789abcdefABCDEF" for character in text):
        raise FixtureError(f"{label} must be an even-length hexadecimal byte string")
    return bytes.fromhex(text)


def _validate_flash_integrity(raw: object, label: str) -> dict[str, Any]:
    """Validate immutable content declarations without reading any artifact yet."""

    integrity = dict(_mapping(raw, label))
    core = frozenset({"artifact_sha256", "restore_artifact_sha256", "approved_ranges", "delta"})
    companion = frozenset({"artifact_companion_sha256", "restore_companion_sha256"})
    if set(integrity) not in {core, core | companion}:
        raise FixtureError(
            f"{label} must contain immutable artifact digests, approved ranges, and one delta rule"
        )
    for field in ("artifact_sha256", "restore_artifact_sha256"):
        integrity[field] = _sha256_hex(integrity[field], f"{label}.{field}")
    if companion & set(integrity):
        for field in companion:
            integrity[field] = _sha256_hex(integrity[field], f"{label}.{field}")
    integrity["approved_ranges"] = _integrity_ranges(
        integrity["approved_ranges"], f"{label}.approved_ranges"
    )
    delta = dict(_mapping(integrity["delta"], f"{label}.delta"))
    kind = delta.get("kind")
    if kind == "xor":
        _exact_keys(delta, frozenset({"kind", "start", "mask"}), f"{label}.delta")
        if not isinstance(delta["start"], int) or isinstance(delta["start"], bool):
            raise FixtureError(f"{label}.delta.start must be an integer")
        delta["mask"] = _hex_bytes(delta["mask"], f"{label}.delta.mask").hex().upper()
    elif kind == "replace":
        _exact_keys(
            delta,
            frozenset({"kind", "start", "before", "after"}),
            f"{label}.delta",
        )
        if not isinstance(delta["start"], int) or isinstance(delta["start"], bool):
            raise FixtureError(f"{label}.delta.start must be an integer")
        before = _hex_bytes(delta["before"], f"{label}.delta.before")
        after = _hex_bytes(delta["after"], f"{label}.delta.after")
        if not before or len(before) != len(after):
            raise FixtureError(
                f"{label}.delta replacement bytes must be non-empty and equal length"
            )
        delta["before"], delta["after"] = before.hex().upper(), after.hex().upper()
    else:
        raise FixtureError(f"{label}.delta.kind must be 'xor' or 'replace'")
    integrity["delta"] = delta
    return integrity


def _normalize_flash_fixture(raw: object, label: str) -> dict[str, dict[str, Any]]:
    """Normalize schema-v2 HW04 flash inputs to distinct raw and safe lanes.

    Callers bind every ``raw_*`` HW04 step (including the initial raw read) to
    ``raw`` and every ``safe_*`` plan/action/readback to ``safe``.  Fixture
    authors may retain the legacy six-field object, which is validated once
    and represented as equal raw/safe lanes.  New fixtures use exactly
    ``{"raw": lane, "safe": lane}``, allowing independent artifacts,
    addresses, read-backs, and footprints.  This normalization only validates
    declared evidence binding; it does not prove artifact contents or issue a
    hardware operation.
    """

    fixture = _mapping(raw, label)
    if set(fixture) == _LEGACY_FLASH_FIXTURE_FIELDS:
        lane = _validate_flash_lane(fixture, label, require_integrity=False)
        return {"raw": dict(lane), "safe": dict(lane)}
    if set(fixture) != _FLASH_LANES:
        raise FixtureError(
            f"{label} must be either the legacy flash fields or exactly raw/safe lanes"
        )
    return {
        lane: _validate_flash_lane(fixture[lane], f"{label}.{lane}")
        for lane in sorted(_FLASH_LANES)
    }


def _flash_lane(board: Mapping[str, Any], lane: str) -> Mapping[str, Any]:
    """Read a validated lane, accepting legacy fixtures in direct unit calls."""

    if lane not in _FLASH_LANES:
        raise FixtureError(f"unknown HW04 flash lane {lane!r}")
    flash = _mapping(board["flash_fixture"], "board.flash_fixture")
    if set(flash) == _LEGACY_FLASH_FIXTURE_FIELDS:
        return flash
    if set(flash) != _FLASH_LANES:
        raise FixtureError("board.flash_fixture is not a legacy or raw/safe lane fixture")
    return _mapping(flash[lane], f"board.flash_fixture.{lane}")


def _parsed_intel_hex(path: Path, label: str) -> dict[int, int]:
    """Read one checksum-valid Intel HEX artifact without importing a target backend."""

    try:
        rendered = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise FixtureError(f"{label} is not readable ASCII Intel HEX: {path}") from exc
    image: dict[int, int] = {}
    upper = 0
    saw_eof = False
    for number, line in enumerate(rendered.splitlines(), start=1):
        if saw_eof or not line.startswith(":"):
            raise FixtureError(f"{label} has malformed Intel HEX record {number}")
        try:
            raw = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise FixtureError(f"{label} has non-hex record {number}") from exc
        if len(raw) < 5 or len(raw) != raw[0] + 5 or sum(raw) & 0xFF:
            raise FixtureError(f"{label} has invalid Intel HEX record {number}")
        length, high, low, kind = raw[:4]
        payload = raw[4:-1]
        offset = (high << 8) | low
        if kind == 0:
            for index, value in enumerate(payload):
                address = (upper << 16) + offset + index
                if address in image:
                    raise FixtureError(f"{label} overlaps Intel HEX byte 0x{address:X}")
                image[address] = value
        elif kind == 4 and length == 2:
            upper = int.from_bytes(payload, "big")
        elif kind == 1 and length == 0:
            saw_eof = True
        else:
            raise FixtureError(f"{label} has unsupported Intel HEX record {number}")
    if not saw_eof:
        raise FixtureError(f"{label} is missing its Intel HEX EOF record")
    return image


def _range_bytes(image: Mapping[int, int], ranges: Sequence[tuple[int, int]], label: str) -> bytes:
    expected = {address for start, end in ranges for address in range(start, end)}
    if set(image) != expected:
        raise FixtureError(f"{label} does not contain exactly the approved byte ranges")
    return bytes(image[address] for start, end in ranges for address in range(start, end))


def _artifact_sha256(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise FixtureError(f"{label} cannot be read for SHA-256 verification: {path}") from exc


def _preflight_hw04_lane(name: str, board: Mapping[str, Any], lane: str) -> dict[str, object]:
    """Prove one ready HW04 lane's immutable bytes before any session can start."""

    contract = _HW04_EXECUTABLE_CONTENT.get(name, {}).get(lane)
    if contract is None:
        raise FixtureError(f"HW04 {name} {lane} lane has no approved executable content rule")
    flash = _flash_lane(board, lane)
    integrity = flash.get("integrity")
    if not isinstance(integrity, Mapping):
        raise FixtureError(f"HW04 {name} {lane} lane requires immutable integrity evidence")
    expected_ranges = contract["ranges"]
    assert isinstance(expected_ranges, tuple)
    declared_ranges = tuple(
        (span["start"], span["end"])
        for span in _integrity_ranges(integrity.get("approved_ranges"), "integrity.approved_ranges")
    )
    if declared_ranges != expected_ranges:
        raise FixtureError(
            f"HW04 {name} {lane} approved_ranges must match the exact approved spans"
        )
    expected_address = contract.get("readback_address", expected_ranges[0][0])
    if flash["address"] != expected_address:
        raise FixtureError(f"HW04 {name} {lane} address must match its exact read-back address")
    declared_footprint = tuple(
        (span["start"], span["end"])
        for span in flash["erase_footprint"]
        if isinstance(span, Mapping)
    )
    if declared_footprint != expected_ranges:
        raise FixtureError(
            f"HW04 {name} {lane} erase_footprint must match the exact approved ranges"
        )
    artifact = Path(_text(flash["artifact"], f"HW04 {name} {lane}.artifact"))
    restore = Path(_text(flash["restore_artifact"], f"HW04 {name} {lane}.restore_artifact"))
    if artifact.suffix.casefold() != ".hex" or restore.suffix.casefold() != ".hex":
        raise FixtureError(f"HW04 {name} {lane} artifacts must be exact Intel HEX files")
    if _artifact_sha256(artifact, f"HW04 {name} {lane}.artifact") != integrity.get(
        "artifact_sha256"
    ):
        raise FixtureError(f"HW04 {name} {lane}.artifact_sha256 does not match artifact bytes")
    if _artifact_sha256(restore, f"HW04 {name} {lane}.restore_artifact") != integrity.get(
        "restore_artifact_sha256"
    ):
        raise FixtureError(
            f"HW04 {name} {lane}.restore_artifact_sha256 does not match artifact bytes"
        )
    test_bytes = _range_bytes(
        _parsed_intel_hex(artifact, f"HW04 {name} {lane}.artifact"),
        expected_ranges,
        f"HW04 {name} {lane}.artifact",
    )
    restore_bytes = _range_bytes(
        _parsed_intel_hex(restore, f"HW04 {name} {lane}.restore_artifact"),
        expected_ranges,
        f"HW04 {name} {lane}.restore_artifact",
    )
    declared_delta = _mapping(integrity.get("delta"), "integrity.delta")
    expected_delta = contract["delta"]
    assert isinstance(expected_delta, Mapping)
    if (
        declared_delta.get("kind") != expected_delta["kind"]
        or declared_delta.get("start") != expected_delta["start"]
    ):
        raise FixtureError(f"HW04 {name} {lane} delta must match the exact approved mutation")
    if lane == "raw":
        mask = expected_delta["mask"]
        assert isinstance(mask, bytes)
        if declared_delta.get("mask") != mask.hex().upper():
            raise FixtureError(
                f"HW04 {name} raw delta must use the exact deterministic prefix mask"
            )
        expected_test = bytes(
            value ^ mask[index] if index < len(mask) else value
            for index, value in enumerate(restore_bytes)
        )
        if test_bytes != expected_test:
            raise FixtureError(f"HW04 {name} raw delta changes bytes outside its exact prefix rule")
    else:
        before, after = expected_delta["before"], expected_delta["after"]
        assert isinstance(before, bytes) and isinstance(after, bytes)
        if (
            declared_delta.get("before") != before.hex().upper()
            or declared_delta.get("after") != after.hex().upper()
        ):
            raise FixtureError(
                f"HW04 {name} safe delta must match the exact erased-word replacement"
            )
        delta_start = expected_delta["start"]
        assert isinstance(delta_start, int)
        offset = 0
        for start, end in expected_ranges:
            if start <= delta_start and delta_start + len(before) <= end:
                offset += delta_start - start
                break
            offset += end - start
        else:
            raise FixtureError(f"HW04 {name} safe delta is outside its approved ranges")
        expected_test = restore_bytes[:offset] + after + restore_bytes[offset + len(after) :]
        if restore_bytes[offset : offset + len(before)] != before or test_bytes != expected_test:
            raise FixtureError(f"HW04 {name} safe delta is not exactly the approved erased word")
        for path, digest_field in (
            (artifact.with_suffix(".elf"), "artifact_companion_sha256"),
            (restore.with_suffix(".elf"), "restore_companion_sha256"),
        ):
            if not path.is_file() or _artifact_sha256(
                path, f"HW04 {name} safe companion"
            ) != integrity.get(digest_field):
                raise FixtureError(
                    f"HW04 {name} safe {digest_field} does not match its ELF companion"
                )
            if path.read_bytes()[:4] != b"\x7fELF":
                raise FixtureError(f"HW04 {name} safe companion is not an ELF file")
    return {
        "artifact_sha256": integrity["artifact_sha256"],
        "restore_artifact_sha256": integrity["restore_artifact_sha256"],
        "approved_ranges": [{"start": start, "end": end} for start, end in expected_ranges],
        "delta": dict(declared_delta),
    }


def _hw04_contract_name(board: Mapping[str, Any]) -> str:
    """Resolve the immutable HW04 content contract from the bound target."""

    target = _text(board.get("target"), "board.target").casefold()
    if target == "nrf52840":
        return "nrf52840"
    if target.startswith("stm32l476"):
        return "stm32l476rtg"
    raise FixtureError(f"HW04 target {target!r} has no approved executable content rule")


def _revalidate_hw04_dispatch_artifact(identifier: str, board: Mapping[str, Any]) -> None:
    """Re-read exact artifact bytes immediately before every flash dispatch."""

    lane = {
        "raw_flash": "raw",
        "raw_restore": "raw",
        "safe_flash": "safe",
        "safe_restore": "safe",
    }.get(identifier)
    if lane is not None and "flash_fixture" in board and "target" in board:
        _preflight_hw04_lane(_hw04_contract_name(board), board, lane)


def _hw04_restore_range_digest(
    board: Mapping[str, Any], lane: str, index: int
) -> tuple[int, int, str]:
    """Return one exact restore-artifact span and its byte digest."""

    flash = _flash_lane(board, lane)
    integrity = _mapping(flash.get("integrity"), f"HW04 {lane}.integrity")
    ranges = _integrity_ranges(
        integrity.get("approved_ranges"), f"HW04 {lane}.integrity.approved_ranges"
    )
    try:
        span = ranges[index]
    except IndexError as exc:
        raise FixtureError(f"HW04 {lane} has no approved restore range {index}") from exc
    start, end = span["start"], span["end"]
    path = Path(_text(flash["restore_artifact"], f"HW04 {lane}.restore_artifact"))
    image = _parsed_intel_hex(path, f"HW04 {lane}.restore_artifact")
    try:
        contents = bytes(image[address] for address in range(start, end))
    except KeyError as exc:
        raise FixtureError(
            f"HW04 {lane}.restore_artifact does not cover approved range {index}"
        ) from exc
    return start, end, hashlib.sha256(contents).hexdigest()


def _hw04_page_proof(base: str) -> tuple[str, int] | None:
    """Return the proof phase and approved-range index for a page proof base."""

    for phase, bases in (
        ("preflight", _HW04_SAFE_PREFLIGHT_PAGE_BASES),
        ("restored", _HW04_SAFE_RESTORED_PAGE_BASES),
    ):
        if base in bases:
            return phase, bases.index(base)
    return None


def _hw04_full_lifecycle(require_page_proof: bool) -> tuple[str, ...]:
    """Build the exact safe lifecycle, including Nordic whole-page proofs when required."""

    if not require_page_proof:
        return _HW04_FULL_LIFECYCLE
    preflight = tuple(
        identifier
        for base in _HW04_SAFE_PREFLIGHT_PAGE_BASES
        for identifier in (f"{base}_plan_guide", f"{base}_plan_accept", base)
    )
    restored = tuple(
        identifier
        for base in _HW04_SAFE_RESTORED_PAGE_BASES
        for identifier in (f"{base}_plan_guide", f"{base}_plan_accept", base)
    )
    flash_index = _HW04_FULL_LIFECYCLE.index("safe_flash_plan_guide")
    restored_index = _HW04_FULL_LIFECYCLE.index("safe_restored_readback_plan_guide")
    return (
        *_HW04_FULL_LIFECYCLE[:flash_index],
        *preflight,
        *_HW04_FULL_LIFECYCLE[flash_index:restored_index],
        *restored,
        *_HW04_FULL_LIFECYCLE[restored_index:],
    )


def _requires_hw04_page_proof(name: str, board: Mapping[str, Any]) -> bool:
    """Require whole-page binding for the Nordic three-page sparse safe artifact."""

    if name != "nrf52840":
        return False
    integrity = _mapping(_flash_lane(board, "safe").get("integrity"), "HW04 safe.integrity")
    ranges = tuple(
        (span["start"], span["end"])
        for span in _integrity_ranges(
            integrity.get("approved_ranges"), "HW04 safe.integrity.approved_ranges"
        )
    )
    return ranges == _HW04_EXECUTABLE_CONTENT["nrf52840"]["safe"]["ranges"]


def _validate_hw04_lifecycle(
    field: str,
    steps: Sequence[Mapping[str, Any]],
    *,
    require_page_proof: bool = False,
    retained_ram: bool = False,
    retained_raw: bool = False,
) -> None:
    """Make restoration order an executable fixture contract, not set membership."""

    expected = (
        (
            ()
            if retained_raw
            else _HW04_RAW_FLASH_ONLY_LIFECYCLE
            if retained_ram
            else _HW04_RAW_LIFECYCLE
        )
        if field == "raw_steps"
        else _hw04_full_lifecycle(require_page_proof)
    )
    observed = tuple(_text(step["id"], f"HW04 {field}.id") for step in steps)
    if observed != expected:
        raise FixtureError(
            f"HW04 {field} lifecycle must be exactly ordered through restoration readback then disconnect"
        )


def _common_board(name: str, raw: object) -> dict[str, Any]:
    board = dict(_mapping(raw, f"boards.{name}"))
    if frozenset(board) not in {BOARD_KEYS, _BOARD_KEYS_WITH_CONNECTION_ID}:
        raise FixtureError(
            f"boards.{name} must contain exactly {sorted(BOARD_KEYS)} with an optional connection_id"
        )
    for field in (
        "board_id",
        "model",
        "probe_uid",
        "target",
        "serial_binding",
        "expected_tier",
    ):
        _text(board[field], f"boards.{name}.{field}")
    if board["expected_tier"] not in {"no-setup", "setup-lite", "setup-full"}:
        raise FixtureError(f"boards.{name}.expected_tier must be a supported tier")
    if "connection_id" in board:
        board["connection_id"] = _provider_qualified_connection_id(
            board["connection_id"], f"boards.{name}.connection_id"
        )
    ram = _mapping(board["safe_ram"], f"boards.{name}.safe_ram")
    _exact_keys(ram, frozenset({"start", "length", "write_value"}), f"boards.{name}.safe_ram")
    for field in ram:
        if not isinstance(ram[field], int) or isinstance(ram[field], bool) or ram[field] < 0:
            raise FixtureError(f"boards.{name}.safe_ram.{field} must be a non-negative integer")
    if ram["length"] < 4:
        raise FixtureError(f"boards.{name}.safe_ram.length must cover at least one word")
    if ram["write_value"] >= 1 << 32:
        raise FixtureError(f"boards.{name}.safe_ram.write_value must fit one 32-bit raw write")
    board["flash_fixture"] = _normalize_flash_fixture(
        board["flash_fixture"], f"boards.{name}.flash_fixture"
    )
    restoration = _mapping(board["restoration"], f"boards.{name}.restoration")
    _exact_keys(
        restoration, frozenset({"procedure", "evidence_path"}), f"boards.{name}.restoration"
    )
    _text(restoration["procedure"], f"boards.{name}.restoration.procedure")
    evidence_path = _absolute(
        restoration["evidence_path"], f"boards.{name}.restoration.evidence_path"
    )
    if not evidence_path.parent.is_dir():
        raise FixtureError(
            f"boards.{name}.restoration.evidence_path must have an existing destination"
        )
    restoration["evidence_path"] = str(evidence_path)
    full = _mapping(board["full_evidence"], f"boards.{name}.full_evidence")
    if full.get("status") == "available":
        _exact_keys(
            full,
            frozenset({"status", "tier", "reference"}),
            f"boards.{name}.full_evidence",
        )
        if full["tier"] != "setup-full":
            raise FixtureError(f"boards.{name}.full_evidence.tier must be exactly 'setup-full'")
        _text(full["reference"], f"boards.{name}.full_evidence.reference")
    elif full.get("status") == "blocked":
        _exact_keys(full, frozenset({"status", "reason"}), f"boards.{name}.full_evidence")
        _text(full["reason"], f"boards.{name}.full_evidence.reason")
    else:
        raise FixtureError(
            f"boards.{name}.full_evidence must be available/setup-full or blocked with reason"
        )
    approvals = _mapping(board["disruption_approval"], f"boards.{name}.disruption_approval")
    _exact_keys(
        approvals,
        frozenset({"ram_write", "flash", "mass_erase"}),
        f"boards.{name}.disruption_approval",
    )
    for field, value in approvals.items():
        approval = _mapping(value, f"boards.{name}.disruption_approval.{field}")
        _exact_keys(
            approval,
            frozenset({"approved", "scope", "reference"}),
            f"boards.{name}.disruption_approval.{field}",
        )
        scope = _text(approval["scope"], f"boards.{name}.disruption_approval.{field}.scope")
        if field == "mass_erase":
            # HW05 proves only the public disclosure/reservation lifecycle and
            # post-restart invalidation.  A true execution approval here would
            # be dangerously misleading because this runner never executes it.
            if approval["approved"] is not False:
                raise FixtureError(
                    f"boards.{name}.disruption_approval.mass_erase.approved must be false: "
                    "AT10 never authorizes or executes mass erase"
                )
            if scope != "non-executing-disclosure-reservation":
                raise FixtureError(
                    f"boards.{name}.disruption_approval.mass_erase.scope must be "
                    "'non-executing-disclosure-reservation'"
                )
        else:
            if not isinstance(approval["approved"], bool):
                raise FixtureError(
                    f"boards.{name}.disruption_approval.{field}.approved must be boolean"
                )
            expected_scope = field if approval["approved"] else "declined-or-pending"
            if scope != expected_scope:
                raise FixtureError(
                    f"boards.{name}.disruption_approval.{field}.scope must be "
                    f"{expected_scope!r} for its explicit approval state"
                )
        _text(approval["reference"], f"boards.{name}.disruption_approval.{field}.reference")
    return board


def _step(raw: object, label: str, *, allow_pause: bool) -> dict[str, Any]:
    step = dict(_mapping(raw, label))
    kind = step.get("kind", "call")
    if kind == "operator_pause":
        if not allow_pause:
            raise FixtureError(f"{label}.kind operator_pause is permitted only in HW05")
        _exact_keys(step, frozenset({"id", "kind", "action", "capture", "expect_record"}), label)
        if step["action"] not in {"downgrade", "mass-erase"}:
            raise FixtureError(f"{label}.action must be downgrade or mass-erase")
        _text(step["id"], f"{label}.id")
        _text(step["capture"], f"{label}.capture")
        _mapping(step["expect_record"], f"{label}.expect_record")
        return step
    if kind != "call":
        raise FixtureError(f"{label}.kind must be call or operator_pause")
    if not {"id", "tool", "arguments", "expect"}.issubset(step) or not set(step).issubset(
        {"id", "kind", "tool", "arguments", "expect", "capture", "fallback_from"}
    ):
        raise FixtureError(f"{label} requires id, tool, arguments, expect, and optional capture")
    _text(step["id"], f"{label}.id")
    tool = _text(step["tool"], f"{label}.tool")
    if tool in _FORBIDDEN_TOOLS:
        raise FixtureError(f"{label}.tool {tool} is forbidden: AT10 never executes erase/unlock")
    _mapping(step["arguments"], f"{label}.arguments")
    expect = _mapping(step["expect"], f"{label}.expect")
    if not ({"payload", "text_contains"} & set(expect)):
        raise FixtureError(f"{label}.expect must specify payload and/or text_contains")
    if "payload" in expect:
        _mapping(expect["payload"], f"{label}.expect.payload")
    if "child_payload" in expect:
        _mapping(expect["child_payload"], f"{label}.expect.child_payload")
    if "child_text_contains" in expect and (
        not isinstance(expect["child_text_contains"], list)
        or not expect["child_text_contains"]
        or not all(isinstance(item, str) for item in expect["child_text_contains"])
    ):
        raise FixtureError(f"{label}.expect.child_text_contains must be a non-empty string list")
    if "child_hex_bytes" in expect and (
        not isinstance(expect["child_hex_bytes"], int)
        or isinstance(expect["child_hex_bytes"], bool)
        or expect["child_hex_bytes"] <= 0
    ):
        raise FixtureError(f"{label}.expect.child_hex_bytes must be a positive integer")
    for field in ("hex_sha256", "child_hex_sha256"):
        if field in expect:
            digest = _mapping(expect[field], f"{label}.expect.{field}")
            _exact_keys(
                digest,
                frozenset({"byte_count", "sha256"}),
                f"{label}.expect.{field}",
            )
            count = digest["byte_count"]
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                raise FixtureError(f"{label}.expect.{field}.byte_count must be a positive integer")
            _sha256_hex(digest["sha256"], f"{label}.expect.{field}.sha256")
    if "text_contains" in expect and (
        not isinstance(expect["text_contains"], list)
        or not expect["text_contains"]
        or not all(isinstance(item, str) for item in expect["text_contains"])
    ):
        raise FixtureError(f"{label}.expect.text_contains must be a non-empty string list")
    if "mcp_error" in expect and not isinstance(expect["mcp_error"], bool):
        raise FixtureError(f"{label}.expect.mcp_error must be boolean")
    if "capture" in step:
        _text(step["capture"], f"{label}.capture")
    if "fallback_from" in step:
        _text(step["fallback_from"], f"{label}.fallback_from")
    return step


def _require_expected_fragment(
    expected: Mapping[str, Any],
    required: Mapping[str, object],
    label: str,
) -> None:
    """Require a fixture oracle to contain a stable public response fragment."""

    payload = expected.get("payload")
    if not isinstance(payload, Mapping):
        raise FixtureError(f"{label}.expect must contain a JSON payload oracle")
    for key, value in required.items():
        if key not in payload:
            raise FixtureError(f"{label}.expect.payload must assert {key!r}")
        current = payload[key]
        if isinstance(value, Mapping):
            if not isinstance(current, Mapping):
                raise FixtureError(f"{label}.expect.payload.{key} must be an object")
            _require_expected_fragment({"payload": current}, value, f"{label}.expect.payload.{key}")
        elif current != value:
            raise FixtureError(f"{label}.expect.payload.{key} must be {value!r}, got {current!r}")


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for child in value.values() for text in _strings(child)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [text for child in value for text in _strings(child)]
    return []


def _protocol_for_step(
    case: str, identifier: str, tier: object
) -> tuple[str, str, Mapping[str, object]] | None:
    """Return (base-id, phase, protocol) for a planned public action step."""

    if case == "HW06" and tier == "no-setup":
        return None
    for base, protocol in _PLAN_PROTOCOLS.get(case, {}).items():
        if identifier == f"{base}_plan_guide":
            return base, "guide", protocol
        if identifier == f"{base}_plan_accept":
            return base, "accept", protocol
        if identifier == base:
            return base, "action", protocol
    return None


def _required_step_ids(case: str, tier: object) -> frozenset[str]:
    """Return the exact checklist IDs for the board's real tier route."""

    required = set(REQUIRED_STEP_IDS[case])
    if case == "HW04":
        return frozenset()
    if case == "HW06" and tier != "no-setup":
        required.update(
            {
                "assign",
                "load_setup_tool",
                "board_validate",
                "route_read_plan_guide",
                "route_read_plan_accept",
            }
        )
    return frozenset(required)


def _validate_plan_protocol_step(
    base: str,
    phase: str,
    protocol: Mapping[str, object],
    step: Mapping[str, Any],
    board: Mapping[str, Any],
    label: str,
) -> None:
    """Validate one precise public guide/accept/fallback protocol leg."""

    tool = _text(step["tool"], f"{label}.tool")
    arguments = _mapping(step["arguments"], f"{label}.arguments")
    expected = _mapping(step["expect"], f"{label}.expect")
    plan_tool = _text(protocol["plan_tool"], f"{label}.plan_tool")
    action = _text(protocol["action"], f"{label}.action")
    board_id = _text(board["board_id"], "board.board_id")
    if phase == "guide":
        if tool != plan_tool:
            raise FixtureError(f"{label}.tool must be {plan_tool!r} for the NULL plan guide")
        if dict(arguments) != _PLAN_GUIDE_ARGUMENTS:
            raise FixtureError(f"{label}.arguments must be the complete all-NULL plan envelope")
        text = expected.get("text_contains")
        required_text = f"Plan initialization for {plan_tool}"
        if not isinstance(text, list) or required_text not in text:
            raise FixtureError(f"{label}.expect.text_contains must assert {required_text!r}")
        return
    if phase == "accept":
        if tool != plan_tool:
            raise FixtureError(f"{label}.tool must be {plan_tool!r} for populated plan acceptance")
        expected_fields = set(_PLAN_ENVELOPE)
        if protocol["permission"] is None:
            expected_fields.remove("user_permission")
        if set(arguments) != expected_fields:
            raise FixtureError(
                f"{label}.arguments must contain exactly the populated plan envelope fields"
            )
        if arguments.get("board_id") != board_id:
            raise FixtureError(f"{label}.arguments.board_id must bind exactly to {board_id!r}")
        for field in (
            "hypothesis",
            "strategy",
            "expected_fail_return",
            "expected_success_return",
        ):
            _text(arguments.get(field), f"{label}.arguments.{field}")
        if (
            arguments.get("hypothesis_made") is not True
            or arguments.get("strategy_evaluated") is not True
        ):
            raise FixtureError(f"{label}.arguments must record evaluated hypothesis and strategy")
        for field in ("max_calls", "max_calls_buffer"):
            value = arguments.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise FixtureError(f"{label}.arguments.{field} must be a non-negative integer")
        if (
            protocol["permission"] is not None
            and arguments.get("user_permission") != protocol["permission"]
        ):
            raise FixtureError(
                f"{label}.arguments.user_permission must be the plan's exact approval"
            )
        parameters = _mapping(
            arguments.get("action_parameters"), f"{label}.arguments.action_parameters"
        )
        required_parameters = protocol["parameters"]
        assert isinstance(required_parameters, frozenset)
        if set(parameters) != required_parameters:
            raise FixtureError(
                f"{label}.arguments.action_parameters must contain exactly "
                f"{sorted(required_parameters)}"
            )
        if base == "lite_setup":
            if parameters.get("target_tier") != "setup-lite" or parameters.get("mode") not in {
                "setup",
                "repair",
            }:
                raise FixtureError(
                    f"{label}.action_parameters must request setup-lite setup or repair"
                )
            _text(parameters.get("connection_id"), f"{label}.action_parameters.connection_id")
            _text(parameters.get("display_name"), f"{label}.action_parameters.display_name")
            _text(parameters.get("mcu_part_number"), f"{label}.action_parameters.mcu_part_number")
            _text(parameters.get("datasheet_path"), f"{label}.action_parameters.datasheet_path")
        if (
            base == "safe_flash"
            and parameters.get("artifact") != _flash_lane(board, "safe")["artifact"]
        ):
            raise FixtureError(
                f"{label}.action_parameters.artifact must bind the approved safe flash artifact"
            )
        if (
            base == "safe_restore"
            and parameters.get("artifact") != _flash_lane(board, "safe")["restore_artifact"]
        ):
            raise FixtureError(
                f"{label}.action_parameters.artifact must bind the approved safe restoration artifact"
            )
        if (
            base in {"safe_flash_readback", "safe_restored_readback"}
            and parameters.get("address") != _flash_lane(board, "safe")["address"]
        ):
            raise FixtureError(f"{label}.action_parameters.address must bind safe flash address")
        page_proof = _hw04_page_proof(base)
        if page_proof is not None:
            _phase, range_index = page_proof
            start, end, _digest = _hw04_restore_range_digest(board, "safe", range_index)
            if dict(parameters) != {"address": start, "width": 8, "length": end - start}:
                raise FixtureError(
                    f"{label}.action_parameters must read the exact approved safe page"
                )
        _require_expected_fragment(
            expected,
            {"status": "plan_accepted", "underlying_action": action},
            label,
        )
        if step.get("capture") != f"{base}_plan":
            raise FixtureError(f"{label}.capture must be {base + '_plan'!r}")
        return
    if tool != "action_batch":
        raise FixtureError(f"{label}.tool must execute the accepted action_batch fallback")
    if step.get("fallback_from") != f"{base}_plan":
        raise FixtureError(f"{label}.fallback_from must be {base + '_plan'!r}")
    expected_payload = expected.get("payload")
    if (
        base == "safe_containment_refusal"
        and isinstance(expected_payload, Mapping)
        and expected_payload.get("status") == "batch_failed"
    ):
        _require_expected_fragment(
            expected,
            {"status": "batch_failed", "board_id": board_id, "completed": []},
            label,
        )
        failure = expected_payload.get("failure")
        if not isinstance(failure, Mapping):
            raise FixtureError(f"{label}.expect.payload.failure must record the denied child")
        if failure.get("tool_name") != action or failure.get("error_type") != "ToolError":
            raise FixtureError(
                f"{label}.expect.payload.failure must identify the {action!r} ToolError"
            )
        message = failure.get("message")
        expected_flash_kind = (
            "application_flash"
            if str(board.get("target", "")).casefold() == "nrf52840"
            else "physical_flash"
        )
        if not isinstance(message, str) or not all(
            marker in message
            for marker in ("unavailable", expected_flash_kind, "board_safety_refresh")
        ):
            raise FixtureError(
                f"{label}.expect.payload.failure.message must prove flash containment"
            )
        return
    _require_expected_fragment(
        expected,
        {"status": "batch_completed", "board_id": board_id, "completed": [{"tool_name": action}]},
        label,
    )
    if base in {"safe_operation", "route_read"}:
        if expected.get("child_hex_bytes") != 4:
            raise FixtureError(
                f"{label}.expect.child_hex_bytes must prove exactly four returned hex bytes"
            )
    elif not ({"child_payload", "child_text_contains", "child_hex_sha256"} & set(expected)):
        raise FixtureError(
            f"{label}.expect must assert the underlying {action!r} result, not only batch acceptance"
        )
    child_payload = expected.get("child_payload")
    if base == "lite_setup":
        if step.get("capture") != "lite_setup":
            raise FixtureError(f"{label}.capture must be 'lite_setup'")
        if not isinstance(child_payload, Mapping) or not isinstance(
            child_payload.get("status"), str
        ):
            raise FixtureError(
                f"{label}.expect.child_payload must record the setup continuation status"
            )
    elif base in {"out_of_map_safe_refusal", "safe_containment_refusal"}:
        if not isinstance(child_payload, Mapping) or child_payload.get("status") != "refused":
            raise FixtureError(
                f"{label}.expect.child_payload must prove the contained action refused"
            )
        if child_payload.get("operation") != action:
            raise FixtureError(f"{label}.expect.child_payload.operation must be {action!r}")
    elif isinstance(child_payload, Mapping):
        _require_expected_fragment(
            {"payload": child_payload}, {"status": "ok", "operation": action}, label
        )
    if base in {"safe_flash", "safe_restore"}:
        text = expected.get("child_text_contains")
        success_marker = "as flash_application within its mapped partition"
        if not isinstance(text, list) or success_marker not in text:
            raise FixtureError(
                f"{label}.expect.child_text_contains must assert the public flash success result"
            )
    if base in {"safe_flash_readback", "safe_restored_readback"}:
        if not isinstance(child_payload, Mapping):
            raise FixtureError(f"{label}.expect.child_payload must prove the safe flash read-back")
        flash = _flash_lane(board, "safe")
        expected_result = flash[
            "post_flash_readback" if base == "safe_flash_readback" else "post_restore_readback"
        ]
        assert isinstance(expected_result, Mapping)
        _require_expected_fragment({"payload": child_payload}, expected_result, label)
    page_proof = _hw04_page_proof(base)
    if page_proof is not None:
        _phase, range_index = page_proof
        start, end, digest = _hw04_restore_range_digest(board, "safe", range_index)
        declared = expected.get("child_hex_sha256")
        if declared != {"byte_count": end - start, "sha256": digest}:
            raise FixtureError(
                f"{label}.expect.child_hex_sha256 must bind the exact restore-page digest"
            )


def _validate_plan_lifecycles(
    case: str,
    steps: Sequence[Mapping[str, Any]],
    board: Mapping[str, Any],
) -> None:
    """Ensure every fallback is the exact output of its preceding accepted plan."""

    indexed = {_text(step["id"], "step.id"): (index, step) for index, step in enumerate(steps)}
    tier = board["expected_tier"]
    if case == "HW06" and tier == "no-setup":
        return
    for base, protocol in _PLAN_PROTOCOLS.get(case, {}).items():
        guide_id, accept_id = f"{base}_plan_guide", f"{base}_plan_accept"
        if base in _HW04_SAFE_PAGE_PROOF_BASES and not {
            guide_id,
            accept_id,
            base,
        }.intersection(indexed):
            continue
        if guide_id not in indexed or accept_id not in indexed or base not in indexed:
            raise FixtureError(
                f"{case}.{base} must include guide, accepted plan, and fallback action"
            )
        guide_index, _ = indexed[guide_id]
        accept_index, accept = indexed[accept_id]
        action_index, action_step = indexed[base]
        if not guide_index < accept_index < action_index:
            raise FixtureError(
                f"{case}.{base} must run NULL guide, accepted plan, then fallback action"
            )
        accepted_arguments = _mapping(accept["arguments"], f"{case}.{accept_id}.arguments")
        action_arguments = _mapping(action_step["arguments"], f"{case}.{base}.arguments")
        parameters = _mapping(
            accepted_arguments["action_parameters"], f"{case}.{accept_id}.action_parameters"
        )
        expected_fallback = {
            "board_id": board["board_id"],
            "actions": [
                {
                    "tool_name": protocol["action"],
                    "arguments": {"board_id": board["board_id"], **parameters},
                }
            ],
        }
        if dict(action_arguments) != expected_fallback:
            raise FixtureError(
                f"{case}.{base}.arguments must be the exact one-child action_batch fallback"
            )
    if case == "HW02":
        overview_index, _ = indexed["setup_overview"]
        loader_index, _ = indexed["load_setup_tool"]
        setup_guide_index, _ = indexed["lite_setup_plan_guide"]
        confirmation_index, _ = indexed["lite_confirmation"]
        repair_index, repair = indexed["lite_repair"]
        connect_index, _ = indexed["connect"]
        capabilities_index, _ = indexed["capabilities"]
        safe_read_index, _ = indexed["safe_read"]
        out_of_map_index, _ = indexed["out_of_map_safe_refusal"]
        setup_action_index, setup_action = indexed["lite_setup"]
        if not overview_index < loader_index < setup_guide_index < setup_action_index:
            raise FixtureError(
                "HW02 must route setup_overview, load board_setup-plan, then run its NULL guide"
            )
        target_confirmation_index = (
            indexed["lite_target_confirmation"][0]
            if "lite_target_confirmation" in indexed
            else setup_action_index
        )
        if not (
            setup_action_index <= target_confirmation_index < confirmation_index < repair_index
        ):
            raise FixtureError(
                "HW02 must resolve any target, confirm the map, then use its paired repair"
            )
        if not repair_index < connect_index < capabilities_index:
            raise FixtureError(
                "HW02 must reconnect after setup repair and before capability/safe-read evidence"
            )
        if not (connect_index < safe_read_index and connect_index < out_of_map_index):
            raise FixtureError("HW02 must reconnect before every planned safe read")
        repair_arguments = _mapping(repair["arguments"], "HW02.lite_repair.arguments")
        setup_arguments = _mapping(setup_action["arguments"], "HW02.lite_setup.arguments")
        primary_children = setup_arguments.get("actions")
        if (
            not isinstance(primary_children, list)
            or len(primary_children) != 1
            or not isinstance(primary_children[0], Mapping)
        ):
            raise FixtureError("HW02.lite_setup must contain one primary setup fallback child")
        primary_arguments = _mapping(
            primary_children[0].get("arguments"), "HW02.lite_setup.arguments.actions[0].arguments"
        )
        paired_parameters = {
            key: value for key, value in primary_arguments.items() if key != "board_id"
        }
        expected_repair = {
            "board_id": board["board_id"],
            "actions": [
                {
                    "tool_name": "board_fix_setup",
                    "arguments": {"board_id": board["board_id"], **paired_parameters},
                }
            ],
        }
        if dict(repair_arguments) != expected_repair:
            raise FixtureError(
                "HW02.lite_repair.arguments must be the paired board_fix_setup fallback"
            )
    if case == "HW03":
        try:
            capabilities_index, _ = indexed["capabilities"]
            assignment_index, _ = indexed["assign"]
            connect_index, _ = indexed["connect"]
            loader_index, _ = indexed["load_setup_tool"]
            validation_index, _ = indexed["board_validate"]
            guide_index, _ = indexed["safe_operation_plan_guide"]
            accept_index, _ = indexed["safe_operation_plan_accept"]
            action_index, _ = indexed["safe_operation"]
        except KeyError as exc:
            raise FixtureError(
                "HW03 must include the setup_overview assignment before connect and validation"
            ) from exc
        if not (
            capabilities_index
            < assignment_index
            < connect_index
            < loader_index
            < validation_index
            < guide_index
            < accept_index
            < action_index
        ):
            raise FixtureError(
                "HW03 must confirm full capability, assign, connect, load validation, then run the planned safe action"
            )
    if case == "HW06":
        try:
            capabilities_index, _ = indexed["capabilities"]
            assignment_index, _ = indexed["assign"]
            connect_index, _ = indexed["connect"]
            loader_index, _ = indexed["load_setup_tool"]
            validation_index, _ = indexed["board_validate"]
            guide_index, _ = indexed["route_read_plan_guide"]
            accept_index, _ = indexed["route_read_plan_accept"]
            action_index, _ = indexed["route_read"]
        except KeyError as exc:
            raise FixtureError(
                "HW06 must include the setup_overview assignment before protected routing"
            ) from exc
        if not (
            capabilities_index
            < assignment_index
            < connect_index
            < loader_index
            < validation_index
            < guide_index
            < accept_index
            < action_index
        ):
            raise FixtureError(
                "HW06 must confirm capability, assign, connect, load validation, then run the planned route"
            )


def _validate_unlock_plan_step(
    step: Mapping[str, Any], board: Mapping[str, Any], label: str
) -> bool:
    """Validate HW05's disclosure-only target_unlock plan protocol.

    The final destructive action remains forbidden.  These calls only obtain
    a server-derived disclosure and prove its approval is invalid after a
    restart.
    """

    identifier = _text(step["id"], f"{label}.id")
    if identifier not in {
        "mass_erase_plan_guide",
        "mass_erase_disclosure",
        "mass_erase_reservation",
        "restart_stale_mass_erase_refusal",
    }:
        return False
    if _text(step["tool"], f"{label}.tool") != "target_unlock-plan":
        raise FixtureError(f"{label}.tool must be 'target_unlock-plan'")
    arguments = _mapping(step["arguments"], f"{label}.arguments")
    expected = _mapping(step["expect"], f"{label}.expect")
    if identifier == "mass_erase_plan_guide":
        if dict(arguments) != _PLAN_GUIDE_ARGUMENTS:
            raise FixtureError(f"{label}.arguments must be the complete all-NULL plan envelope")
        text = expected.get("text_contains")
        required_text = "Plan initialization for target_unlock-plan"
        if not isinstance(text, list) or required_text not in text:
            raise FixtureError(f"{label}.expect.text_contains must assert {required_text!r}")
        return True
    if set(arguments) != set(_PLAN_ENVELOPE):
        raise FixtureError(
            f"{label}.arguments must be the complete populated target-unlock envelope"
        )
    if arguments["board_id"] != board["board_id"]:
        raise FixtureError(f"{label}.arguments.board_id must bind exactly to this board")
    for field in ("hypothesis", "strategy", "expected_fail_return", "expected_success_return"):
        _text(arguments[field], f"{label}.arguments.{field}")
    if arguments["hypothesis_made"] is not True or arguments["strategy_evaluated"] is not True:
        raise FixtureError(f"{label}.arguments must record evaluated hypothesis and strategy")
    for field in ("max_calls", "max_calls_buffer"):
        if not isinstance(arguments[field], int) or isinstance(arguments[field], bool):
            raise FixtureError(f"{label}.arguments.{field} must be an integer")
    parameters = _mapping(arguments["action_parameters"], f"{label}.arguments.action_parameters")
    if set(parameters) != {"recovery_mechanism"}:
        raise FixtureError(
            f"{label}.arguments.action_parameters must contain only recovery_mechanism"
        )
    if identifier == "mass_erase_disclosure":
        if arguments["user_permission"] is not None:
            raise FixtureError(f"{label}.arguments.user_permission must be null for disclosure")
        _require_expected_fragment(expected, {"status": "unlock_permission_requested"}, label)
        if step.get("capture") != "mass_erase_disclosure":
            raise FixtureError(f"{label}.capture must be 'mass_erase_disclosure'")
    elif identifier == "mass_erase_reservation":
        if arguments["user_permission"] != "one-time":
            raise FixtureError(f"{label}.arguments.user_permission must be one-time")
        _require_expected_fragment(
            expected, {"status": "unlock_plan_approved", "underlying_tool": "target_unlock"}, label
        )
        if step.get("capture") != "mass_erase_approval":
            raise FixtureError(f"{label}.capture must be 'mass_erase_approval'")
    else:
        if arguments["user_permission"] != "one-time" or expected.get("mcp_error") is not True:
            raise FixtureError(
                f"{label} must prove the old one-time approval is refused after restart"
            )
    return True


def _validate_unlock_lifecycle(steps: Sequence[Mapping[str, Any]]) -> None:
    indexed = {_text(step["id"], "step.id"): (index, step) for index, step in enumerate(steps)}
    guide = indexed["mass_erase_plan_guide"]
    disclosure = indexed["mass_erase_disclosure"]
    reservation = indexed["mass_erase_reservation"]
    if not guide[0] < disclosure[0] < reservation[0]:
        raise FixtureError("HW05 must run target-unlock NULL guide, disclosure, then reservation")
    disclosed = _mapping(disclosure[1]["arguments"], "HW05.mass_erase_disclosure.arguments")
    approved = _mapping(reservation[1]["arguments"], "HW05.mass_erase_reservation.arguments")
    if {key: value for key, value in disclosed.items() if key != "user_permission"} != {
        key: value for key, value in approved.items() if key != "user_permission"
    }:
        raise FixtureError("HW05 mass-erase reservation must reuse the exact disclosed plan bytes")


def _validate_restart_unlock_refusal(
    pre_restart: Sequence[Mapping[str, Any]], post_restart: Sequence[Mapping[str, Any]]
) -> None:
    """Ensure the second server receives the exact stale mass-erase approval bytes."""

    pre = {_text(step["id"], "pre_restart.id"): step for step in pre_restart}
    post = {_text(step["id"], "post_restart.id"): step for step in post_restart}
    reserved = _mapping(
        pre["mass_erase_reservation"]["arguments"], "HW05.mass_erase_reservation.arguments"
    )
    stale = _mapping(
        post["restart_stale_mass_erase_refusal"]["arguments"],
        "HW05.restart_stale_mass_erase_refusal.arguments",
    )
    if dict(stale) != dict(reserved):
        raise FixtureError(
            "HW05 restart stale mass-erase refusal must replay the exact pre-restart approval bytes"
        )


def _validate_step_contract(
    case: str,
    step: Mapping[str, Any],
    board: Mapping[str, Any],
    other_boards: Sequence[Mapping[str, Any]],
    label: str,
) -> None:
    """Bind one checklist label to its exact tool, board, and evidence oracle."""

    identifier = _text(step["id"], f"{label}.id")
    if step.get("kind", "call") == "operator_pause":
        expected_action = "mass-erase" if identifier == "manual_mass_erase_pause" else "downgrade"
        if step["action"] != expected_action:
            raise FixtureError(f"{label}.action must be {expected_action!r}")
        _require_expected_fragment(
            {"payload": _mapping(step["expect_record"], f"{label}.expect_record")},
            {"action": expected_action, "state": "unlocked", "board_id": board["board_id"]},
            label,
        )
        expected_capture = (
            "mass_erase_grant" if expected_action == "mass-erase" else "downgrade_grant"
        )
        if step["capture"] != expected_capture:
            raise FixtureError(f"{label}.capture must be {expected_capture!r}")
        return
    arguments_for_binding = _mapping(step["arguments"], f"{label}.arguments")
    foreign: set[str] = set()
    for other in other_boards:
        foreign.update(
            (
                _text(other["board_id"], "other.board_id"),
                _text(other["probe_uid"], "other.probe_uid"),
                _text(other["target"], "other.target"),
                _text(other["serial_binding"], "other.serial_binding"),
            )
        )
        if "connection_id" in other:
            foreign.add(_text(other["connection_id"], "other.connection_id"))
    if foreign.intersection(_strings(arguments_for_binding)):
        raise FixtureError(f"{label}.arguments reference a different fixture board or probe")
    if case == "HW05" and _validate_unlock_plan_step(step, board, label):
        return
    protocol_step = _protocol_for_step(case, identifier, board["expected_tier"])
    if protocol_step is not None:
        base, phase, protocol = protocol_step
        _validate_plan_protocol_step(base, phase, protocol, step, board, label)
        return
    if case == "HW06" and identifier == "route_read" and board["expected_tier"] == "no-setup":
        if _text(step["tool"], f"{label}.tool") != "read_memory_raw":
            raise FixtureError(f"{label}.tool must be read_memory_raw for HW06 no-setup routing")
        arguments = _mapping(step["arguments"], f"{label}.arguments")
        if arguments.get("board_id") != board["board_id"] or "address" not in arguments:
            raise FixtureError(f"{label}.arguments must bind no-setup raw routing to this board")
        _require_expected_fragment(
            _mapping(step["expect"], f"{label}.expect"),
            {"status": "ok", "tier": "no-setup", "raw": True},
            label,
        )
        return
    try:
        contract = _STEP_CONTRACTS[case][identifier]
    except KeyError as exc:  # pragma: no cover - exact ID sets are checked by caller
        raise FixtureError(f"{label} has no AT10 public-call contract") from exc
    tool = _text(step["tool"], f"{label}.tool")
    if tool != contract["tool"]:
        raise FixtureError(
            f"{label}.tool must be {contract['tool']!r} for {identifier}, got {tool!r}"
        )
    arguments = _mapping(step["arguments"], f"{label}.arguments")
    if case == "HW04" and identifier == "raw_connect":
        if set(arguments) != {"board_id", "probe_uid", "target"}:
            raise FixtureError(
                f"{label}.arguments must contain the exact STM binding and no profile override"
            )
    if case == "HW04" and identifier == "full_assign":
        if set(arguments) != {"board_names", "connection_assignments"}:
            raise FixtureError(
                f"{label}.arguments must contain only the configured full-profile assignment"
            )
        if arguments.get("board_names") != [board["board_id"]]:
            raise FixtureError(
                f"{label}.arguments.board_names must contain only the configured board"
            )
        connection_id = _text(board.get("connection_id"), "board.connection_id")
        assignments = _mapping(
            arguments.get("connection_assignments"), f"{label}.arguments.connection_assignments"
        )
        if assignments != {board["board_id"]: connection_id}:
            raise FixtureError(
                f"{label}.arguments.connection_assignments must bind the exact fixture connection ID"
            )
    if case == "HW04" and identifier == "full_connect":
        if set(arguments) != {"board_id"}:
            raise FixtureError(
                f"{label}.arguments must contain only board_id for the configured full-profile route"
            )
    if case == "HW04" and identifier == "full_load_validation_tool":
        if (
            set(arguments) != {"board_id", "tool_name"}
            or arguments.get("tool_name") != "board_validate"
        ):
            raise FixtureError(f"{label}.arguments must load 'board_validate'")
    if case == "HW04" and identifier == "full_board_validate":
        if set(arguments) != {"board_id", "probe_id"}:
            raise FixtureError(
                f"{label}.arguments must contain only board_id and the captured probe_id"
            )
    if case in {"HW02", "HW03"} and identifier == "connect" and set(arguments) != {"board_id"}:
        raise FixtureError(
            f"{label}.arguments must contain only board_id for the configured profile"
        )
    if case == "HW06" and identifier == "connect":
        if board["expected_tier"] == "no-setup":
            if set(arguments) != {"board_id", "probe_uid", "target"}:
                raise FixtureError(
                    f"{label}.arguments must contain board_id, probe_uid and target for no-setup"
                )
            if arguments.get("probe_uid") != board["probe_uid"]:
                raise FixtureError(
                    f"{label}.arguments.probe_uid must bind exactly to the fixture probe UID"
                )
            if arguments.get("target") != board["target"]:
                raise FixtureError(
                    f"{label}.arguments.target must bind exactly to the fixture target"
                )
        elif set(arguments) != {"board_id"}:
            raise FixtureError(
                f"{label}.arguments must contain only board_id for the configured profile"
            )
    if case in {"HW03", "HW06"} and identifier == "load_setup_tool":
        if set(arguments) != {"board_id", "tool_name"}:
            raise FixtureError(f"{label}.arguments must contain only board_id and tool_name")
        if arguments.get("tool_name") != "board_validate":
            raise FixtureError(f"{label}.arguments.tool_name must load 'board_validate'")
    if case in {"HW03", "HW06"} and identifier == "board_validate":
        if set(arguments) != {"board_id", "probe_id"}:
            raise FixtureError(
                f"{label}.arguments must contain only board_id and the captured probe_id"
            )
        if arguments.get("probe_id") != _VALIDATION_PROBE_ID:
            raise FixtureError(
                f"{label}.arguments.probe_id must be the loader-returned opaque connection token"
            )
    required = contract["arguments"]
    assert isinstance(required, set)
    missing = sorted(required.difference(arguments))
    if missing:
        raise FixtureError(f"{label}.arguments must contain {missing}")
    forbidden = contract.get("without", set())
    assert isinstance(forbidden, set)
    supplied_forbidden = sorted(forbidden.intersection(arguments))
    if supplied_forbidden:
        raise FixtureError(f"{label}.arguments must not supply {supplied_forbidden}")
    board_id = _text(board["board_id"], "board.board_id")
    if "board_id" in required and arguments["board_id"] != board_id:
        raise FixtureError(f"{label}.arguments.board_id must bind exactly to {board_id!r}")
    if contract.get("probe_uid") and arguments.get("probe_uid") != board["probe_uid"]:
        raise FixtureError(
            f"{label}.arguments.probe_uid must bind exactly to the fixture probe UID"
        )
    if contract.get("target") and arguments.get("target") != board["target"]:
        raise FixtureError(f"{label}.arguments.target must bind exactly to the fixture target")
    if identifier in {"discover", "assign"} and arguments.get("board_names") != [board_id]:
        raise FixtureError(f"{label}.arguments.board_names must contain only {board_id!r}")
    if case == "HW02" and identifier == "load_setup_tool":
        if arguments.get("tool_name") != "board_setup-plan":
            raise FixtureError(f"{label}.arguments.tool_name must load 'board_setup-plan'")
    if identifier == "assign":
        assignments = _mapping(
            arguments.get("connection_assignments"), f"{label}.arguments.connection_assignments"
        )
        if set(assignments) != {board_id} or not isinstance(assignments[board_id], str):
            raise FixtureError(
                f"{label}.arguments.connection_assignments must bind only {board_id!r} to "
                "one server-issued opaque connection ID"
            )
    expected = _mapping(step["expect"], f"{label}.expect")
    payload = contract.get("payload")
    if payload is not None:
        assert isinstance(payload, Mapping)
        _require_expected_fragment(expected, payload, label)
    required_mcp_error = contract.get("mcp_error")
    if required_mcp_error is not None and expected.get("mcp_error") is not required_mcp_error:
        raise FixtureError(f"{label}.expect.mcp_error must be {required_mcp_error!r}")
    if case in {"HW03", "HW06"} and identifier == "load_setup_tool":
        _require_expected_fragment(expected, {"board_id": board_id}, label)
    if case == "HW06" and identifier in {"capabilities", "route_read"}:
        _require_expected_fragment(expected, {"tier": board["expected_tier"]}, label)
    for fragment in contract.get("text", ()):
        assert isinstance(fragment, str)
        if (
            case == "HW06"
            and identifier == "connect"
            and board["expected_tier"] == "no-setup"
            and fragment == "$board_id"
        ):
            expected_fragment = board["probe_uid"]
        else:
            expected_fragment = {
                "$board_id": board_id,
                "$probe_uid": board["probe_uid"],
                "$target": board["target"],
            }.get(fragment, fragment)
        text = expected.get("text_contains")
        if not isinstance(text, list) or expected_fragment not in text:
            raise FixtureError(f"{label}.expect.text_contains must assert {expected_fragment!r}")
    capture = contract.get("capture")
    if capture is not None and step.get("capture") != capture:
        raise FixtureError(f"{label}.capture must be {capture!r}")
    references = contract.get("reference", {})
    assert isinstance(references, Mapping)
    for key, reference in references.items():
        if arguments.get(key) != reference:
            raise FixtureError(f"{label}.arguments.{key} must be {reference!r}")
    if case == "HW02" and identifier == "non_map_operation":
        if tool != "read_cpu_register":
            raise FixtureError(f"{label}.tool must prove a CPU-register non-map operation")
    if case == "HW02" and identifier == "lite_confirmation":
        _require_expected_fragment(expected, {"accepted": "lite_confirmation"}, label)
        if arguments.get("continuation_id") != _LITE_SETUP_CONTINUATION:
            raise FixtureError(
                f"{label}.arguments.continuation_id must be the parsed lite-setup child capture"
            )
    if case == "HW02" and identifier == "lite_target_confirmation":
        if board_id != "nrf52840":
            raise FixtureError(f"{label} is permitted only for the Nordic built-in target route")
        if arguments.get("continuation_id") != _LITE_SETUP_CONTINUATION:
            raise FixtureError(
                f"{label}.arguments.continuation_id must reuse the lite-setup child capture"
            )
        response = _mapping(arguments.get("response"), f"{label}.arguments.response")
        if set(response) != {"pyocd_target", "evidence", "reasoning_summary"}:
            raise FixtureError(f"{label}.arguments.response must use the exact target schema")
        if response.get("pyocd_target") != "nrf52840":
            raise FixtureError(f"{label}.arguments.response must bind target nrf52840")
    if case == "HW02" and identifier == "lite_repair":
        if step.get("fallback_from") != "lite_setup_plan":
            raise FixtureError(f"{label}.fallback_from must be 'lite_setup_plan'")
        _require_expected_fragment(
            expected,
            {
                "status": "batch_completed",
                "completed": [{"tool_name": "board_fix_setup"}],
            },
            label,
        )
        child = expected.get("child_payload")
        if not isinstance(child, Mapping) or child.get("status") != "setup_completed":
            raise FixtureError(f"{label}.expect.child_payload must prove setup-lite completed")
    if case == "HW05":
        if identifier == "mass_erase_disclosure":
            if arguments.get("user_permission") not in {None}:
                raise FixtureError(
                    f"{label}.arguments.user_permission must be null/omitted for disclosure"
                )
        if identifier in {"mass_erase_reservation", "restart_stale_mass_erase_refusal"}:
            if arguments.get("user_permission") != "one-time":
                raise FixtureError(
                    f"{label}.arguments.user_permission must be the explicit one-time approval"
                )
        if identifier == "restart_stale_mass_erase_refusal":
            if expected.get("mcp_error") is not True:
                raise FixtureError(f"{label}.expect.mcp_error must assert stale approval refusal")
    if case == "HW04":
        raw_flash = _flash_lane(board, "raw")
        safe_flash = _flash_lane(board, "safe")
        ram = _mapping(board["safe_ram"], "board.safe_ram")
        if (
            identifier
            in {
                "initial_ram_read",
                "raw_ram_write",
                "raw_ram_readback",
                "raw_ram_restore",
                "ram_restored_readback",
            }
            and arguments.get("address") != ram["start"]
        ):
            raise FixtureError(
                f"{label}.arguments.address must bind exactly to approved scratch RAM"
            )
        if identifier == "raw_ram_write" and arguments.get("value") != ram["write_value"]:
            raise FixtureError(f"{label}.arguments.value must be the approved scratch-RAM value")
        if identifier == "raw_ram_readback":
            _require_expected_fragment(
                expected,
                {"result": f"0x{ram['write_value']:08X}"},
                label,
            )
        if identifier == "ram_restored_readback":
            _require_expected_fragment(
                expected,
                {"result": "$capture.initial_ram.result"},
                label,
            )
        if (
            identifier
            in {
                "initial_flash_readback",
                "raw_flash_readback",
                "raw_restored_readback",
            }
            and arguments.get("address") != raw_flash["address"]
        ):
            raise FixtureError(
                f"{label}.arguments.address must bind exactly to the raw flash address"
            )
        if (
            identifier in {"safe_flash_readback", "safe_restored_readback"}
            and arguments.get("address") != safe_flash["address"]
        ):
            raise FixtureError(
                f"{label}.arguments.address must bind exactly to the safe flash address"
            )
        if identifier == "raw_flash" and arguments.get("artifact") != raw_flash["artifact"]:
            raise FixtureError(
                f"{label}.arguments.artifact must be the approved raw flash artifact"
            )
        if (
            identifier == "raw_restore"
            and arguments.get("artifact") != raw_flash["restore_artifact"]
        ):
            raise FixtureError(
                f"{label}.arguments.artifact must be the approved raw restoration artifact"
            )
        if identifier == "raw_flash_readback":
            expected_result = raw_flash["post_flash_readback"]
            assert isinstance(expected_result, Mapping)
            _require_expected_fragment(expected, expected_result, label)
        if identifier == "safe_flash_readback":
            expected_result = safe_flash["post_flash_readback"]
            assert isinstance(expected_result, Mapping)
            _require_expected_fragment(expected, expected_result, label)
        if identifier == "raw_restored_readback":
            expected_result = raw_flash["post_restore_readback"]
            assert isinstance(expected_result, Mapping)
            _require_expected_fragment(expected, expected_result, label)
        if identifier == "safe_restored_readback":
            expected_result = safe_flash["post_restore_readback"]
            assert isinstance(expected_result, Mapping)
            _require_expected_fragment(expected, expected_result, label)
        if str(board.get("target", "")).casefold() == "nrf52840" and identifier in {
            "initial_flash_readback",
            "raw_restored_readback",
        }:
            start, end, digest = _hw04_restore_range_digest(board, "raw", 0)
            if dict(arguments) != {
                "board_id": board["board_id"],
                "address": start,
                "width": 8,
                "length": end - start,
            }:
                raise FixtureError(
                    f"{label}.arguments must read the complete Nordic raw restore page"
                )
            if expected.get("hex_sha256") != {
                "byte_count": end - start,
                "sha256": digest,
            }:
                raise FixtureError(
                    f"{label}.expect.hex_sha256 must bind the complete raw restore page"
                )
        if identifier in {"safe_flash", "safe_restore"}:
            text = expected.get("text_contains")
            if not isinstance(text, list):
                raise FixtureError(
                    f"{label}.expect must acknowledge containment and erase footprint"
                )
            for span in safe_flash["erase_footprint"]:
                assert isinstance(span, Mapping)
                rendered = f"0x{span['start']:X}-0x{span['end']:X}"
                if rendered not in text:
                    raise FixtureError(
                        f"{label}.expect.text_contains must acknowledge erase span {rendered}"
                    )


def _validate_retained_hw04_ram_evidence(
    raw: object, board: Mapping[str, Any], label: str
) -> dict[str, str]:
    """Bind a flash-only resume to the immutable accepted RAM transcript."""

    evidence = _mapping(raw, label)
    _exact_keys(evidence, frozenset({"path", "sha256"}), label)
    path = _absolute(evidence["path"], f"{label}.path")
    digest = _sha256_hex(evidence["sha256"], f"{label}.sha256")
    if not path.is_file() or _artifact_sha256(path, label) != digest:
        raise FixtureError(f"{label} does not match the retained RAM evidence bytes")
    document: Mapping[str, Any] | None = None
    try:
        encoded = path.read_bytes()
        encoding = "utf-16" if encoded.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
        lines = encoded.decode(encoding).splitlines()
    except (OSError, UnicodeError) as exc:
        raise FixtureError(f"{label} is not readable text evidence") from exc
    for line in reversed(lines):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, Mapping):
            document = candidate
            break
    if document is None or document.get("status") != "ram_proof_complete":
        raise FixtureError(f"{label} lacks the accepted RAM proof document")
    rows = document.get("boards")
    if not isinstance(rows, list):
        raise FixtureError(f"{label}.boards must be a list")
    board_id = _text(board["board_id"], "board.board_id")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("board_id") == board_id]
    if len(matches) != 1:
        raise FixtureError(f"{label} must contain exactly one row for {board_id!r}")
    row = matches[0]
    ram = _mapping(board["safe_ram"], "board.safe_ram")
    expected_test = f"0x{ram['write_value']:08X}"
    cleanup = row.get("cleanup")
    if (
        row.get("status") != "pass"
        or row.get("address") != ram["start"]
        or row.get("length") != 4
        or row.get("test_value") != expected_test
        or row.get("test_readback") != expected_test
        or row.get("initial_value") != row.get("restored_readback")
        or not isinstance(cleanup, Mapping)
        or cleanup.get("attempted") is not True
        or "disconnected" not in str(cleanup.get("response", "")).casefold()
    ):
        raise FixtureError(f"{label} does not prove exact RAM write, restore, and disconnect")
    return {"path": str(path), "sha256": digest}


def _validate_retained_hw04_raw_evidence(
    raw: object, board: Mapping[str, Any], label: str
) -> dict[str, str]:
    """Bind a safe-only resume to an exact passing raw flash transcript."""

    evidence = _mapping(raw, label)
    _exact_keys(evidence, frozenset({"path", "sha256"}), label)
    path = _absolute(evidence["path"], f"{label}.path")
    digest = _sha256_hex(evidence["sha256"], f"{label}.sha256")
    if not path.is_file() or _artifact_sha256(path, label) != digest:
        raise FixtureError(f"{label} does not match the retained raw evidence bytes")
    try:
        document = _mapping(json.loads(path.read_text(encoding="utf-8")), label)
        case = _mapping(_mapping(document["case_results"], f"{label}.case_results")["HW04"], label)
        board_result = _mapping(
            _mapping(case["boards"], f"{label}.boards")[_text(board["board_id"], "board.board_id")],
            f"{label}.board",
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FixtureError(f"{label} is not a complete HW04 transcript") from exc
    phases = board_result.get("phases")
    if not isinstance(phases, list):
        raise FixtureError(f"{label}.phases must be a list")
    raw_phases = [
        phase for phase in phases if isinstance(phase, Mapping) and phase.get("name") == "raw"
    ]
    if len(raw_phases) != 1 or raw_phases[0].get("status") != "pass":
        raise FixtureError(f"{label} must contain exactly one passing raw phase")
    phase = raw_phases[0]
    rows = phase.get("steps")
    if not isinstance(rows, list):
        raise FixtureError(f"{label}.raw.steps must be a list")
    identifiers = tuple(row.get("id") for row in rows if isinstance(row, Mapping))
    if identifiers != _HW04_RAW_FLASH_ONLY_LIFECYCLE or any(
        not isinstance(row, Mapping) or row.get("passed") is not True for row in rows
    ):
        raise FixtureError(f"{label} must prove every flash-only raw lifecycle step passed")
    restored = next(
        row for row in rows if isinstance(row, Mapping) and row.get("id") == "raw_restored_readback"
    )
    actual = restored.get("actual")
    payload = actual.get("payload") if isinstance(actual, Mapping) else None
    result = payload.get("result") if isinstance(payload, Mapping) else None
    start, end, expected_digest = _hw04_restore_range_digest(board, "raw", 0)
    try:
        restored_bytes = _public_hex_bytes(
            result if isinstance(result, str) else "",
            end - start,
            f"{label}.raw_restored_readback",
        )
    except CheckFailed as exc:
        raise FixtureError(f"{label} lacks the complete raw restoration bytes") from exc
    if hashlib.sha256(restored_bytes).hexdigest() != expected_digest:
        raise FixtureError(f"{label} raw restoration digest does not match the restore artifact")
    sessions = phase.get("physical_sessions")
    if not isinstance(sessions, list) or len(sessions) != 3:
        raise FixtureError(f"{label} must retain all three raw physical sessions")
    final_disconnect = next(
        row for row in rows if isinstance(row, Mapping) and row.get("id") == "final_disconnect"
    )
    final_actual = final_disconnect.get("actual")
    logical_disconnect_passed = (
        final_disconnect.get("passed") is True
        and isinstance(final_actual, Mapping)
        and "disconnected" in str(final_actual.get("text", "")).casefold()
    )
    for index, session in enumerate(sessions):
        cleanup = session.get("cleanup") if isinstance(session, Mapping) else None
        response = cleanup.get("response") if isinstance(cleanup, Mapping) else None
        cleanup_passed = (
            isinstance(response, Mapping)
            and "disconnected" in str(response.get("text", "")).casefold()
        )
        if not cleanup_passed and not (index == len(sessions) - 1 and logical_disconnect_passed):
            raise FixtureError(f"{label}.physical_sessions[{index}] did not disconnect")
    return {"path": str(path), "sha256": digest}


def _case_board(
    case: str,
    name: str,
    raw: object,
    all_boards: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    result = dict(_mapping(raw, f"cases.{case}.boards.{name}"))
    label = f"cases.{case}.boards.{name}"
    if result.get("status") == "blocked":
        _exact_keys(result, frozenset({"status", "reason"}), label)
        reason = _text(result["reason"], f"{label}.reason")
        if case == "HW02" and "CPU register" not in reason and "serial" not in reason:
            raise FixtureError(
                f"{label}.reason must state the unsupported CPU-register or serial limitation"
            )
        return result
    full_evidence = _mapping(all_boards[name]["full_evidence"], f"boards.{name}.full_evidence")
    if case == "HW03" and full_evidence["status"] != "available":
        raise FixtureError(f"{label} must be blocked while setup-full evidence is unavailable")
    if case == "HW04":
        approvals = _mapping(
            all_boards[name]["disruption_approval"], f"boards.{name}.disruption_approval"
        )
        if "connection_id" not in all_boards[name]:
            raise FixtureError(
                f"{label} must be blocked without a provider-qualified full-profile connection ID"
            )
        if full_evidence["status"] != "available" or any(
            _mapping(approvals[field], f"boards.{name}.disruption_approval.{field}")["approved"]
            is not True
            for field in ("ram_write", "flash")
        ):
            raise FixtureError(
                f"{label} must be blocked without available setup-full evidence and explicit "
                "RAM/flash approval"
            )
    if case == "HW05":
        _exact_keys(
            result,
            frozenset({"status", "pre_restart_steps", "post_restart_steps", "restart_records"}),
            label,
        )
        fields = (
            ("pre_restart_steps", HW05_PRE_IDS, True),
            ("post_restart_steps", HW05_POST_IDS, False),
        )
    elif case == "HW04":
        ordinary_keys = frozenset({"status", "raw_steps", "full_steps"})
        retained_ram_keys = ordinary_keys | frozenset({"retained_ram_evidence"})
        retained_raw_keys = retained_ram_keys | frozenset({"retained_raw_evidence"})
        if frozenset(result) not in {ordinary_keys, retained_ram_keys, retained_raw_keys}:
            raise FixtureError(
                f"{label} must contain HW04 lifecycles and ordered retained evidence"
            )
        retained_ram = "retained_ram_evidence" in result
        retained_raw = "retained_raw_evidence" in result
        if retained_raw and not retained_ram:
            raise FixtureError(f"{label}.retained_raw_evidence requires retained RAM evidence")
        if retained_ram:
            result["retained_ram_evidence"] = _validate_retained_hw04_ram_evidence(
                result["retained_ram_evidence"],
                all_boards[name],
                f"{label}.retained_ram_evidence",
            )
        if retained_raw:
            result["retained_raw_evidence"] = _validate_retained_hw04_raw_evidence(
                result["retained_raw_evidence"],
                all_boards[name],
                f"{label}.retained_raw_evidence",
            )
        require_page_proof = _requires_hw04_page_proof(name, all_boards[name])
        full_ids = (
            HW04_FULL_IDS | _HW04_SAFE_PAGE_PROOF_IDS if require_page_proof else HW04_FULL_IDS
        )
        raw_ids = (
            frozenset()
            if retained_raw
            else frozenset(_HW04_RAW_FLASH_ONLY_LIFECYCLE)
            if retained_ram
            else HW04_RAW_IDS
        )
        fields = (("raw_steps", raw_ids, False), ("full_steps", full_ids, False))
    else:
        _exact_keys(result, frozenset({"status", "steps"}), label)
        required = set(_required_step_ids(case, all_boards[name]["expected_tier"]))
        if case == "HW02" and name != "nrf52840":
            required.discard("lite_target_confirmation")
        fields = (("steps", frozenset(required), False),)
    if result.get("status") != "ready":
        raise FixtureError(f"{label}.status must be ready or blocked")
    for field, required, pauses in fields:
        values = result[field]
        if not isinstance(values, list):
            raise FixtureError(f"{label}.{field} must be a list")
        steps = [
            _step(value, f"{label}.{field}[{index}]", allow_pause=pauses)
            for index, value in enumerate(values)
        ]
        ids = [step["id"] for step in steps]
        if len(ids) != len(set(ids)) or set(ids) != required:
            raise FixtureError(f"{label}.{field} must name exactly {sorted(required)}")
        if case == "HW04":
            _validate_hw04_lifecycle(
                field,
                steps,
                require_page_proof=field == "full_steps" and require_page_proof,
                retained_ram=field == "raw_steps" and retained_ram,
                retained_raw=field == "raw_steps" and retained_raw,
            )
        other_boards = [board for board_name, board in all_boards.items() if board_name != name]
        for index, step in enumerate(steps):
            _validate_step_contract(
                case,
                step,
                all_boards[name],
                other_boards,
                f"{label}.{field}[{index}]",
            )
        if case in _PLAN_PROTOCOLS and field != "raw_steps":
            _validate_plan_lifecycles(case, steps, all_boards[name])
        if case == "HW05" and field == "pre_restart_steps":
            _validate_unlock_lifecycle(steps)
        result[field] = steps
    if case == "HW05" and (
        not isinstance(result["restart_records"], list)
        or set(result["restart_records"]) != {"downgrade", "mass-erase"}
    ):
        raise FixtureError(f"{label}.restart_records must name downgrade and mass-erase")
    if case == "HW05":
        _validate_restart_unlock_refusal(result["pre_restart_steps"], result["post_restart_steps"])
    return result


def validate_fixture_document(document: object) -> dict[str, Any]:
    """Validate the schema-v2 public-call evidence contract without live imports."""

    fixture = dict(_mapping(document, "fixture"))
    if fixture.get("schema_version") != 2:
        raise FixtureError(
            "fixture.schema_version must be 2; schema 1 cannot express complete AT10 evidence"
        )
    _exact_keys(
        fixture, frozenset({"schema_version", "case_project_roots", "boards", "cases"}), "fixture"
    )
    roots = _mapping(fixture["case_project_roots"], "fixture.case_project_roots")
    if set(roots) != set(ALL_CASES):
        raise FixtureError("fixture.case_project_roots must contain every HW01--HW06 root")
    validated_roots: dict[str, Any] = {}
    physical_roots: list[Path] = []
    for case in ALL_CASES:
        if case != "HW04":
            root = _project_root(roots[case], f"fixture.case_project_roots.{case}")
            validated_roots[case] = str(root)
            physical_roots.append(root)
            continue
        scoped = _mapping(roots[case], "fixture.case_project_roots.HW04")
        _exact_keys(scoped, frozenset({"raw", "full"}), "fixture.case_project_roots.HW04")
        raw_root = _project_root(scoped["raw"], "fixture.case_project_roots.HW04.raw")
        full_root = _project_root(scoped["full"], "fixture.case_project_roots.HW04.full")
        validated_roots[case] = {"raw": str(raw_root), "full": str(full_root)}
        physical_roots.extend((raw_root, full_root))
    _require_distinct_projects(physical_roots)
    fixture["case_project_roots"] = validated_roots
    boards = _mapping(fixture["boards"], "fixture.boards")
    if set(boards) != set(BOARD_NAMES):
        raise FixtureError("fixture.boards must name exactly nrf52840 and stm32l476rtg")
    fixture["boards"] = {name: _common_board(name, boards[name]) for name in BOARD_NAMES}
    if (
        len({fixture["boards"][name]["board_id"] for name in BOARD_NAMES}) != 2
        or len({fixture["boards"][name]["probe_uid"] for name in BOARD_NAMES}) != 2
        or len({fixture["boards"][name]["target"] for name in BOARD_NAMES}) != 2
        or len({fixture["boards"][name]["expected_tier"] for name in BOARD_NAMES}) != 2
    ):
        raise FixtureError(
            "HW06 requires distinct board, probe, target, and expected-tier bindings"
        )
    cases = _mapping(fixture["cases"], "fixture.cases")
    if set(cases) != set(ALL_CASES):
        raise FixtureError("fixture.cases must contain every HW01--HW06 record")
    fixture["cases"] = {}
    for case in ALL_CASES:
        container = dict(_mapping(cases[case], f"fixture.cases.{case}"))
        _exact_keys(container, frozenset({"boards"}), f"fixture.cases.{case}")
        scoped = _mapping(container["boards"], f"fixture.cases.{case}.boards")
        if set(scoped) != set(BOARD_NAMES):
            raise FixtureError(f"fixture.cases.{case}.boards must name both physical boards")
        fixture["cases"][case] = {
            "boards": {
                name: _case_board(case, name, scoped[name], fixture["boards"])
                for name in BOARD_NAMES
            }
        }
    return fixture


def load_fixture(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FixtureError(f"unable to read fixtures: {path}") from exc
    return validate_fixture_document(document)


def selected_cases(raw: str) -> tuple[str, ...]:
    selected = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    if not selected:
        raise FixtureError("--cases must select at least one HW01--HW06 checklist case")
    unknown = sorted(set(selected).difference(ALL_CASES))
    if unknown:
        raise FixtureError(f"unknown hardware case(s): {', '.join(unknown)}")
    return selected


def _response(value: Any) -> dict[str, Any]:
    content = getattr(value, "content", value)
    values = content if isinstance(content, list) else [content]
    texts = [getattr(item, "text", item) for item in values]
    text = "\n".join(item for item in texts if isinstance(item, str))
    payload: object | None = None
    if len(texts) == 1 and isinstance(texts[0], str):
        try:
            payload = json.loads(texts[0].split("\n", 1)[0])
        except json.JSONDecodeError:
            pass
    return {
        "mcp_error": bool(getattr(value, "isError", getattr(value, "is_error", False))),
        "text": text,
        "payload": payload,
    }


def _strict_public_result_first_line(result: str, pattern: re.Pattern[str]) -> str | None:
    lines = result.splitlines()
    if len(lines) != 2 or lines[1] != _PUBLIC_SAFE_EXIT:
        return None
    return lines[0] if pattern.fullmatch(lines[0]) is not None else None


def _batch_child_result(actual: Mapping[str, object], identifier: str) -> tuple[object, str]:
    """Extract the sole child result from the required one-child plan fallback."""

    payload = actual.get("payload")
    if not isinstance(payload, Mapping):
        raise CheckFailed(f"{identifier}.batch response is not a JSON payload")
    completed = payload.get("completed")
    if (
        not isinstance(completed, list)
        or len(completed) != 1
        or not isinstance(completed[0], Mapping)
    ):
        raise CheckFailed(f"{identifier}.batch response must contain exactly one child result")
    result = completed[0].get("result")
    if isinstance(result, str):
        try:
            return json.loads(result.split("\n", 1)[0]), result
        except json.JSONDecodeError:
            tool_name = completed[0].get("tool_name")
            first_line = (
                _strict_public_result_first_line(result, _PUBLIC_MEMORY_RESULT)
                if tool_name == "read_memory_address"
                else None
            )
            if first_line is not None:
                return {
                    "status": "ok",
                    "operation": tool_name,
                    "result": first_line,
                }, result
            if (
                tool_name == "flash_application"
                and _strict_public_result_first_line(result, _PUBLIC_FLASH_RESULT) is not None
            ):
                return {"status": "ok", "operation": tool_name}, result
            return None, result
    if isinstance(result, Mapping):
        return result, json.dumps(result, sort_keys=True)
    return result, str(result)


def _public_hex_bytes(text: str, count: int, identifier: str) -> bytes:
    """Parse the product's exact plain first-line hexadecimal byte shape."""

    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    tokens = first_line.split()

    def is_hex_byte(token: str) -> bool:
        return len(token) == 2 and all(character in "0123456789abcdefABCDEF" for character in token)

    if len(tokens) != count or not all(is_hex_byte(token) for token in tokens):
        raise CheckFailed(f"{identifier} first line must contain exactly {count} hex bytes")
    return bytes(int(token, 16) for token in tokens)


def _require_hex_byte_child_text(child_text: str, count: int, identifier: str) -> None:
    """Require the product's plain first-line hexadecimal byte read-back shape."""

    _public_hex_bytes(child_text, count, f"{identifier}.child_result")


def _require_hex_sha256(text: str, expectation: object, identifier: str) -> None:
    """Require an exact byte count and digest without embedding a full page in JSON."""

    expected = _mapping(expectation, f"{identifier}.hex_sha256")
    count = expected.get("byte_count")
    digest = expected.get("sha256")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise CheckFailed(f"{identifier}.hex_sha256.byte_count must be a positive integer")
    if not isinstance(digest, str):
        raise CheckFailed(f"{identifier}.hex_sha256.sha256 must be a digest")
    actual = hashlib.sha256(_public_hex_bytes(text, count, identifier)).hexdigest()
    if actual != digest:
        raise CheckFailed(f"{identifier} SHA-256 expected {digest}, got {actual}")


def _contains(actual: object, expected: object, label: str) -> None:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            raise CheckFailed(f"{label} expected object, got {actual!r}")
        for key, value in expected.items():
            if key not in actual:
                raise CheckFailed(f"{label} missing key {key!r}: {actual!r}")
            _contains(actual[key], value, f"{label}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) < len(expected):
            raise CheckFailed(f"{label} expected at least {expected!r}, got {actual!r}")
        for index, value in enumerate(expected):
            _contains(actual[index], value, f"{label}[{index}]")
    elif actual != expected:
        raise CheckFailed(f"{label} expected {expected!r}, got {actual!r}")


def _resolve(value: object, captures: Mapping[str, object]) -> object:
    if isinstance(value, str) and value.startswith("$capture."):
        current: object = captures
        parts = value.split(".")[1:]
        index = 0
        while index < len(parts):
            part = parts[index]
            if isinstance(current, Mapping) and part in current:
                current = current[part]
                index += 1
                continue
            if isinstance(current, list) and part.isdecimal():
                item_index = int(part)
                if item_index < len(current):
                    current = current[item_index]
                    index += 1
                    continue
            if isinstance(current, str):
                try:
                    parsed = json.loads(current.split("\n", 1)[0])
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, (Mapping, list)):
                    current = parsed
                    continue
            if not isinstance(current, Mapping) or part not in current:
                raise CheckFailed(f"unresolved fixture reference {value!r}")
        return current
    if isinstance(value, Mapping):
        return {str(key): _resolve(item, captures) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, captures) for item in value]
    return value


def _manual_record(project_root: Path, action: str) -> dict[str, object]:
    path = project_root / _MANUAL_ROOT / f"{action}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CheckFailed(
            f"manual {action} record is absent or malformed after operator action: {path}"
        ) from exc
    if not isinstance(record, dict) or record.get("action") != action:
        raise CheckFailed(f"manual {action} record has invalid action/schema: {record!r}")
    return record


async def _default_pause(message: str) -> None:
    response = await asyncio.to_thread(
        input, f"\nPAUSED: {message}\nType CONTINUE only after the human step: "
    )
    if response.strip() != "CONTINUE":
        raise CheckFailed("operator did not confirm the manual-only step; runner created no grant")


class _HW04RestoreState:
    """Conservative state for one test-artifact flash and its exact restoration."""

    def __init__(self, lane: str) -> None:
        self.lane = lane
        self.test_flash_dispatched = False
        self.dispatch_step: str | None = None
        self.restoration_readback_verified = False
        self.transport_uncertain = False

    @property
    def emergency_required(self) -> bool:
        return self.test_flash_dispatched and not self.restoration_readback_verified


async def run_steps(
    session: Any,
    project_root: Path,
    board: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    *,
    pause: Callable[[str], Awaitable[None]],
    captures: dict[str, object] | None = None,
    hw04_restore: _HW04RestoreState | None = None,
) -> tuple[list[dict[str, Any]], dict[str, object]]:
    """Run exact public MCP calls, storing expected-versus-actual rows."""

    values = {} if captures is None else captures
    rows: list[dict[str, Any]] = []
    board_id = _text(board["board_id"], "board_id")
    for step in steps:
        identifier = _text(step["id"], "step.id")
        if step.get("kind", "call") == "operator_pause":
            action = _text(step["action"], f"{identifier}.action")
            await pause(
                f"Ask the human to invoke ${action} for board={board_id} in {project_root}; "
                "use exact current bindings and do not emulate the skill. "
                f"Observed prerequisite evidence: {json.dumps(values, sort_keys=True)}"
            )
            record = _manual_record(project_root, action)
            _contains(record, step["expect_record"], f"{identifier}.manual_record")
            values[_text(step["capture"], f"{identifier}.capture")] = record
            rows.append(
                {
                    "id": identifier,
                    "kind": "operator_pause",
                    "expected": step["expect_record"],
                    "actual": record,
                    "passed": True,
                }
            )
            continue
        arguments = _resolve(step["arguments"], values)
        if not isinstance(arguments, Mapping):
            raise CheckFailed(f"{identifier}.arguments resolved to a non-object")
        fallback_from = step.get("fallback_from")
        if fallback_from is not None:
            source = values.get(_text(fallback_from, f"{identifier}.fallback_from"))
            if not isinstance(source, Mapping):
                raise CheckFailed(f"{identifier} has no captured accepted plan fallback")
            fallback = source.get("stable_client_fallback")
            if identifier == "lite_repair":
                paired = source.get("paired_action_fallbacks")
                if (
                    not isinstance(paired, list)
                    or len(paired) != 1
                    or not isinstance(paired[0], Mapping)
                ):
                    raise CheckFailed(
                        f"{identifier} accepted setup plan omitted paired repair fallback"
                    )
                fallback = paired[0].get("call")
            if not isinstance(fallback, Mapping):
                raise CheckFailed(f"{identifier} accepted plan omitted stable_client_fallback")
            if fallback.get("tool_name") != step["tool"] or fallback.get("arguments") != arguments:
                raise CheckFailed(
                    f"{identifier} arguments are not the exact server-returned one-child fallback"
                )
        expected = _resolve(step["expect"], values)
        if not isinstance(expected, Mapping):
            raise CheckFailed(f"{identifier}.expect resolved to a non-object")
        tool = _text(step["tool"], f"{identifier}.tool")
        _revalidate_hw04_dispatch_artifact(identifier, board)
        if hw04_restore is not None and identifier == f"{hw04_restore.lane}_flash":
            # An await may fail after the request reached the server.  Mark before
            # dispatch so exception handling never mistakes uncertainty for safety.
            hw04_restore.test_flash_dispatched = True
            hw04_restore.dispatch_step = identifier
        try:
            actual = _response(await session.call_tool(tool, dict(arguments)))
        except Exception as exc:  # noqa: BLE001 - preserve a possible dispatched flash
            if hw04_restore is not None and hw04_restore.emergency_required:
                hw04_restore.transport_uncertain = True
            message = f"{identifier}.call_tool raised after possible dispatch: {type(exc).__name__}: {exc}"
            rows.append(
                {
                    "id": identifier,
                    "tool": tool,
                    "arguments": dict(arguments),
                    "expected": expected,
                    "actual": {"call_exception": f"{type(exc).__name__}: {exc}"},
                    "passed": False,
                    "failure": message,
                }
            )
            failure = CheckFailed(message)
            failure.rows = list(rows)
            raise failure from exc
        try:
            if actual["mcp_error"] != expected.get("mcp_error", False):
                raise CheckFailed(
                    f"{identifier}.mcp_error expected {expected.get('mcp_error', False)!r}, got {actual['mcp_error']!r}"
                )
            if "payload" in expected:
                _contains(actual["payload"], expected["payload"], f"{identifier}.payload")
            if "hex_sha256" in expected:
                payload = actual["payload"]
                if not isinstance(payload, Mapping) or not isinstance(payload.get("result"), str):
                    raise CheckFailed(f"{identifier}.payload.result must contain hexadecimal bytes")
                _require_hex_sha256(payload["result"], expected["hex_sha256"], identifier)
            for text in expected.get("text_contains", []):
                if text.casefold() not in actual["text"].casefold():
                    raise CheckFailed(f"{identifier}.text missing {text!r}: {actual['text']!r}")
            if {
                "child_payload",
                "child_text_contains",
                "child_hex_bytes",
                "child_hex_sha256",
            } & set(expected):
                child_payload, child_text = _batch_child_result(actual, identifier)
                if "child_payload" in expected:
                    _contains(
                        child_payload, expected["child_payload"], f"{identifier}.child_payload"
                    )
                for text in expected.get("child_text_contains", []):
                    if text.casefold() not in child_text.casefold():
                        raise CheckFailed(
                            f"{identifier}.child_result missing {text!r}: {child_text!r}"
                        )
                if "child_hex_bytes" in expected:
                    count = expected["child_hex_bytes"]
                    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                        raise CheckFailed(
                            f"{identifier}.expect.child_hex_bytes must be a positive integer"
                        )
                    _require_hex_byte_child_text(child_text, count, identifier)
                if "child_hex_sha256" in expected:
                    _require_hex_sha256(
                        child_text,
                        expected["child_hex_sha256"],
                        f"{identifier}.child_result",
                    )
        except CheckFailed as exc:
            rows.append(
                {
                    "id": identifier,
                    "tool": step["tool"],
                    "arguments": dict(arguments),
                    "expected": expected,
                    "actual": actual,
                    "passed": False,
                    "failure": str(exc),
                }
            )
            exc.rows = list(rows)
            raise
        if "capture" in step:
            values[_text(step["capture"], f"{identifier}.capture")] = (
                actual["payload"] if actual["payload"] is not None else actual["text"]
            )
        if hw04_restore is not None and identifier == f"{hw04_restore.lane}_restored_readback":
            hw04_restore.restoration_readback_verified = True
        rows.append(
            {
                "id": identifier,
                "tool": step["tool"],
                "arguments": dict(arguments),
                "expected": expected,
                "actual": actual,
                "passed": True,
            }
        )
    return rows, values


def _hw04_emergency_steps(
    lane: str, steps: Sequence[Mapping[str, Any]]
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    """Select only exact restore/read-back fixture legs; never reissue a test flash."""

    indexed = {_text(step["id"], "HW04 emergency step.id"): step for step in steps}
    safe_page_readbacks = tuple(
        identifier
        for base in _HW04_SAFE_RESTORED_PAGE_BASES
        for identifier in (f"{base}_plan_guide", f"{base}_plan_accept", base)
        if identifier in indexed
    )
    required = (
        (("raw_restore",), ("raw_restored_readback",))
        if lane == "raw"
        else (
            ("safe_restore_plan_guide", "safe_restore_plan_accept", "safe_restore"),
            (
                *safe_page_readbacks,
                "safe_restored_readback_plan_guide",
                "safe_restored_readback_plan_accept",
                "safe_restored_readback",
            ),
        )
    )
    groups: list[tuple[Mapping[str, Any], ...]] = []
    for identifiers in required:
        missing = [identifier for identifier in identifiers if identifier not in indexed]
        if missing:
            raise CheckFailed(f"HW04 {lane} emergency restoration lacks fixture steps {missing!r}")
        groups.append(tuple(indexed[identifier] for identifier in identifiers))
    return groups[0], groups[1]


def _hw04_safe_recovery_validation_steps(
    steps: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Select the fresh full-route validation bootstrap before safe restoration."""

    required = ("full_load_validation_tool", "full_board_validate")
    indexed = {_text(step["id"], "HW04 recovery step.id"): step for step in steps}
    missing = [identifier for identifier in required if identifier not in indexed]
    if missing:
        raise CheckFailed(f"HW04 safe recovery lacks validation steps {missing!r}")
    return tuple(indexed[identifier] for identifier in required)


def _hw04_safe_recovery_assignment_steps(
    steps: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Select the re-assignment that must precede a fresh safe reconnect."""

    indexed = {_text(step["id"], "HW04 recovery step.id"): step for step in steps}
    try:
        return (indexed["full_assign"],)
    except KeyError as exc:
        raise CheckFailed("HW04 safe recovery lacks full_assign") from exc


async def _emergency_hw04_restoration(
    session: Any,
    project_root: Path,
    board: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    *,
    lane: str,
    trigger: str,
    pause: Callable[[str], Awaitable[None]],
) -> dict[str, object]:
    """Attempt the exact restore then a fresh read-back, retaining each failure separately."""

    result: dict[str, object] = {"required": True, "lane": lane, "trigger": trigger}
    captures: dict[str, object] = {}
    try:
        restore_steps, readback_steps = _hw04_emergency_steps(lane, steps)
    except Exception as exc:  # noqa: BLE001 - transcript must retain unusable emergency fixture evidence
        failure = f"{type(exc).__name__}: {exc}"
        result["restore"] = {"attempted": False, "status": "failed", "failure": failure}
        result["readback"] = {"attempted": False, "status": "failed", "failure": failure}
        result["status"] = "failed"
        return result

    async def attempt(group: Sequence[Mapping[str, Any]]) -> dict[str, object]:
        nonlocal captures
        record: dict[str, object] = {"attempted": True, "steps": []}
        failures: list[str] = []
        for step in group:
            try:
                rows, captures = await run_steps(
                    session, project_root, board, [step], pause=pause, captures=captures
                )
                record["steps"].extend(rows)
            except Exception as exc:  # noqa: BLE001 - continue to independent fresh read-back plan
                if isinstance(exc, CheckFailed):
                    record["steps"].extend(exc.rows)
                failures.append(f"{type(exc).__name__}: {exc}")
        if failures:
            record["status"] = "failed"
            record["failure"] = "; ".join(failures)
        else:
            record["status"] = "pass"
        return record

    result["restore"] = await attempt(restore_steps)
    result["readback"] = await attempt(readback_steps)
    restore = _mapping(result["restore"], "emergency.restore")
    readback = _mapping(result["readback"], "emergency.readback")
    result["status"] = (
        "pass" if restore["status"] == "pass" and readback["status"] == "pass" else "failed"
    )
    return result


async def _emergency_reconnect(
    session: Any, board: Mapping[str, Any], *, lane: str
) -> dict[str, object]:
    """Reconnect by the lane's public route before emergency restoration."""

    try:
        board_id = _text(board["board_id"], "board.board_id")
        if lane == "raw":
            arguments = {
                "board_id": board_id,
                "probe_uid": _text(board["probe_uid"], "board.probe_uid"),
                "target": _text(board["target"], "board.target"),
            }
        elif lane == "safe":
            arguments = {"board_id": board_id}
        else:
            raise FixtureError(f"unknown HW04 emergency restoration lane: {lane!r}")
        actual = _response(await session.call_tool("connect", arguments))
        if actual["mcp_error"] or "connected" not in actual["text"].casefold():
            raise CheckFailed(f"fresh emergency reconnect was not accepted: {actual!r}")
    except Exception as exc:  # noqa: BLE001 - preserve the failed recovery session evidence
        return {
            "attempted": True,
            "status": "failed",
            "failure": f"{type(exc).__name__}: {exc}",
        }
    return {"attempted": True, "status": "pass", "arguments": arguments, "actual": actual}


async def _fresh_emergency_hw04_restoration(
    session_factory: SessionFactory,
    project_root: Path,
    board: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    *,
    lane: str,
    trigger: str,
    pause: Callable[[str], Awaitable[None]],
) -> dict[str, object]:
    """Close an uncertain transport, then restore only through a fresh MCP session."""

    result: dict[str, object] = {"required": True, "lane": lane, "trigger": trigger}
    try:
        async with session_factory(project_root) as session:
            recovery: dict[str, object] = {"started": True}
            result["recovery_session"] = recovery
            assignment: dict[str, object] | None = None
            if lane == "safe":
                assignment = {"attempted": True, "steps": []}
                recovery["assignment"] = assignment
                try:
                    assignment_steps = _hw04_safe_recovery_assignment_steps(steps)
                    rows, _captures = await run_steps(
                        session,
                        project_root,
                        board,
                        assignment_steps,
                        pause=pause,
                    )
                    assignment["steps"] = rows
                    assignment["status"] = "pass"
                except Exception as exc:  # noqa: BLE001 - do not reconnect without re-assignment
                    if isinstance(exc, CheckFailed):
                        assignment["steps"] = exc.rows
                    assignment["status"] = "failed"
                    assignment["failure"] = f"{type(exc).__name__}: {exc}"
            if assignment is not None and assignment["status"] != "pass":
                failure = _text(assignment.get("failure"), "emergency assignment failure")
                result["restore"] = {
                    "attempted": False,
                    "status": "failed",
                    "failure": f"fresh recovery assignment failed: {failure}",
                }
                result["readback"] = {
                    "attempted": False,
                    "status": "failed",
                    "failure": f"fresh recovery assignment failed: {failure}",
                }
                result["status"] = "failed"
                recovery["cleanup"] = {
                    "attempted": False,
                    "reason": "full_assign failed before any fresh connection",
                }
                return result
            reconnect = await _emergency_reconnect(session, board, lane=lane)
            recovery["reconnect"] = reconnect
            if reconnect["status"] != "pass":
                failure = _text(reconnect.get("failure"), "emergency reconnect failure")
                result["restore"] = {
                    "attempted": False,
                    "status": "failed",
                    "failure": f"fresh recovery reconnect failed: {failure}",
                }
                result["readback"] = {
                    "attempted": False,
                    "status": "failed",
                    "failure": f"fresh recovery reconnect failed: {failure}",
                }
                result["status"] = "failed"
            else:
                validation: dict[str, object] | None = None
                if lane == "safe":
                    validation = {"attempted": True, "steps": []}
                    recovery["validation"] = validation
                    try:
                        validation_steps = _hw04_safe_recovery_validation_steps(steps)
                        rows, _captures = await run_steps(
                            session,
                            project_root,
                            board,
                            validation_steps,
                            pause=pause,
                        )
                        validation["steps"] = rows
                        validation["status"] = "pass"
                    except Exception as exc:  # noqa: BLE001 - no safe restore without fresh validation
                        if isinstance(exc, CheckFailed):
                            validation["steps"] = exc.rows
                        validation["status"] = "failed"
                        validation["failure"] = f"{type(exc).__name__}: {exc}"
                if validation is None or validation["status"] == "pass":
                    result.update(
                        await _emergency_hw04_restoration(
                            session,
                            project_root,
                            board,
                            steps,
                            lane=lane,
                            trigger=trigger,
                            pause=pause,
                        )
                    )
                else:
                    failure = _text(validation.get("failure"), "safe recovery validation failure")
                    result["restore"] = {
                        "attempted": False,
                        "status": "failed",
                        "failure": f"fresh recovery validation failed: {failure}",
                    }
                    result["readback"] = {
                        "attempted": False,
                        "status": "failed",
                        "failure": f"fresh recovery validation failed: {failure}",
                    }
                    result["status"] = "failed"
                result["recovery_session"] = recovery
            recovery["cleanup"] = await _cleanup(
                session, _text(board["board_id"], "board.board_id")
            )
    except Exception as exc:  # noqa: BLE001 - the original test failure remains in its phase
        failure = f"{type(exc).__name__}: {exc}"
        recovery = result.get("recovery_session")
        if isinstance(recovery, dict) and recovery.get("started") is True:
            recovery["context_exit_error"] = failure
            if "restore" not in result:
                result["restore"] = {"attempted": False, "status": "failed", "failure": failure}
                result["readback"] = {
                    "attempted": False,
                    "status": "failed",
                    "failure": failure,
                }
                result["status"] = "failed"
        else:
            result["recovery_session"] = {"started": False, "failure": failure}
            result["restore"] = {"attempted": False, "status": "failed", "failure": failure}
            result["readback"] = {"attempted": False, "status": "failed", "failure": failure}
            result["status"] = "failed"
    return result


async def _cleanup(session: Any, board_id: str) -> dict[str, object]:
    try:
        result = await session.call_tool("disconnect", {"board_id": board_id})
    except Exception as exc:  # noqa: BLE001
        return {"attempted": True, "error": f"{type(exc).__name__}: {exc}"}
    return {"attempted": True, "response": _response(result)}


def _hw04_step_index(steps: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Index one already-validated HW04 logical lifecycle."""

    return {_text(step["id"], "HW04 step.id"): step for step in steps}


def _hw04_steps(value: object, label: str) -> tuple[Mapping[str, Any], ...]:
    """Return an already schema-validated HW04 lifecycle with useful typing."""

    if not isinstance(value, list):
        raise FixtureError(f"{label} must be a list")
    return tuple(_mapping(step, f"{label}[{index}]") for index, step in enumerate(value))


def _hw04_select_steps(
    steps: Sequence[Mapping[str, Any]], identifiers: Sequence[str]
) -> tuple[Mapping[str, Any], ...]:
    """Select exact logical steps without manufacturing a replacement call."""

    indexed = _hw04_step_index(steps)
    missing = [identifier for identifier in identifiers if identifier not in indexed]
    if missing:
        raise CheckFailed(f"HW04 lifecycle lacks required verification steps {missing!r}")
    return tuple(indexed[identifier] for identifier in identifiers)


def _hw04_verification_bootstrap(
    lane: str, steps: Sequence[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], ...]:
    """Return the exact fresh-session bootstrap for one HW04 verification lane."""

    identifiers = (
        ("raw_connect",)
        if lane == "raw"
        else ("full_assign", "full_connect", "full_load_validation_tool", "full_board_validate")
    )
    return _hw04_select_steps(steps, identifiers)


async def _run_hw04_physical_session(
    session_factory: SessionFactory,
    project_root: Path,
    board: Mapping[str, Any],
    *,
    name: str,
    bootstrap: Sequence[Mapping[str, Any]],
    logical_steps: Sequence[Mapping[str, Any]],
    pause: Callable[[str], Awaitable[None]],
    restore_state: _HW04RestoreState | None = None,
) -> tuple[dict[str, object], list[dict[str, Any]], dict[str, object], Exception | None]:
    """Close one physical MCP session while preserving rows and context failures.

    The logical fixture remains ordered once.  Fresh verification sessions replay
    only their required bootstrap internally, and record that physical replay
    separately rather than adding duplicate logical IDs to the phase transcript.
    """

    record: dict[str, object] = {
        "name": name,
        "started": False,
        "bootstrap_step_ids": [_text(step["id"], "HW04 bootstrap.id") for step in bootstrap],
        "logical_step_ids": [
            _text(step["id"], "HW04 physical logical.id") for step in logical_steps
        ],
        "steps": [],
    }
    context: contextlib.AbstractAsyncContextManager[Any] | None = None
    session: Any | None = None
    rows: list[dict[str, Any]] = []
    captures: dict[str, object] = {}
    error: Exception | None = None
    try:
        context = session_factory(project_root)
        session = await context.__aenter__()
        record["started"] = True
        rows, captures = await run_steps(
            session,
            project_root,
            board,
            (*bootstrap, *logical_steps),
            pause=pause,
            hw04_restore=restore_state,
        )
        record["steps"] = rows
    except Exception as exc:  # noqa: BLE001 - retain every failure before recovery
        error = exc
        if isinstance(exc, CheckFailed):
            rows = exc.rows
            record["steps"] = rows
        record["failure"] = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            final_disconnect_completed = (
                bool(rows)
                and rows[-1].get("id") == "final_disconnect"
                and rows[-1].get("passed") is True
            )
            if final_disconnect_completed:
                record["cleanup"] = {
                    "attempted": False,
                    "reason": "final_disconnect completed as the logical lifecycle terminus",
                }
            elif restore_state is not None and restore_state.transport_uncertain:
                record["cleanup"] = {
                    "attempted": False,
                    "reason": "possible post-dispatch transport failure; close without reusing session",
                }
            else:
                record["cleanup"] = await _cleanup(
                    session, _text(board["board_id"], "board.board_id")
                )
        if context is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - recovery still gets a fresh context
                record["context_exit_error"] = f"{type(exc).__name__}: {exc}"
                if error is None:
                    error = exc
    return record, rows, captures, error


def _record_hw04_session_cleanup_error(phase: dict[str, Any], record: Mapping[str, object]) -> None:
    """Retain disconnect/context-close failures without replacing the phase failure."""

    errors: list[str] = []
    cleanup = record.get("cleanup")
    if isinstance(cleanup, Mapping) and isinstance(cleanup.get("error"), str):
        errors.append(cleanup["error"])
    if isinstance(record.get("context_exit_error"), str):
        errors.append(record["context_exit_error"])
    if not errors:
        return
    entry = {"session": record.get("name"), "errors": errors}
    phase.setdefault("session_cleanup_errors", []).append(entry)
    if record.get("name") == "mutation":
        phase["failed_session_cleanup"] = {"error": "; ".join(errors)}


def _hw04_logical_rows(
    rows: Sequence[dict[str, Any]], bootstrap: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Remove a physical session's internal bootstrap from logical phase rows."""

    return list(rows[len(bootstrap) :]) if len(rows) >= len(bootstrap) else []


async def _fresh_emergency_hw04_restoration(
    session_factory: SessionFactory,
    project_root: Path,
    board: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    *,
    lane: str,
    trigger: str,
    pause: Callable[[str], Awaitable[None]],
) -> dict[str, object]:
    """Restore in one fresh session and verify exact bytes in a second one."""

    result: dict[str, object] = {
        "required": True,
        "lane": lane,
        "trigger": trigger,
        "physical_sessions": [],
    }
    try:
        bootstrap = _hw04_verification_bootstrap(lane, steps)
        restore_steps, readback_steps = _hw04_emergency_steps(lane, steps)
    except Exception as exc:  # noqa: BLE001 - record invalid emergency material without a session
        failure = f"{type(exc).__name__}: {exc}"
        result["restore"] = {"attempted": False, "status": "failed", "failure": failure}
        result["readback"] = {"attempted": False, "status": "failed", "failure": failure}
        result["status"] = "failed"
        return result

    (
        restore_record,
        _restore_rows,
        _restore_captures,
        restore_error,
    ) = await _run_hw04_physical_session(
        session_factory,
        project_root,
        board,
        name="emergency_restore",
        bootstrap=bootstrap,
        logical_steps=restore_steps,
        pause=pause,
    )
    verify_record, _verify_rows, _verify_captures, verify_error = await _run_hw04_physical_session(
        session_factory,
        project_root,
        board,
        name="emergency_restore_verification",
        bootstrap=bootstrap,
        logical_steps=readback_steps,
        pause=pause,
    )
    result["physical_sessions"] = [restore_record, verify_record]
    result["restore"] = {
        "attempted": True,
        "status": "pass" if restore_error is None else "failed",
        "steps": _hw04_logical_rows(_restore_rows, bootstrap),
    }
    result["readback"] = {
        "attempted": True,
        "status": "pass" if verify_error is None else "failed",
        "steps": _hw04_logical_rows(_verify_rows, bootstrap),
    }
    if restore_error is not None:
        result["restore"]["failure"] = f"{type(restore_error).__name__}: {restore_error}"
    if verify_error is not None:
        result["readback"]["failure"] = f"{type(verify_error).__name__}: {verify_error}"
    result["status"] = (
        "pass"
        if result["restore"]["status"] == "pass" and result["readback"]["status"] == "pass"
        else "failed"
    )
    return result


async def _execute_hw04_lane(
    session_factory: SessionFactory,
    project_root: Path,
    board: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    *,
    lane: str,
    phase_name: str,
    pause: Callable[[str], Awaitable[None]],
) -> dict[str, Any]:
    """Execute one logical HW04 lane with isolated post-flash verification sessions."""

    phase: dict[str, Any] = {
        "name": phase_name,
        "project_root": str(project_root),
        "steps": [],
        "physical_sessions": [],
    }
    flash_id = f"{lane}_flash"
    if not steps:
        phase["status"] = "pass"
        phase["cleanup"] = {"attempted": False, "reason": "empty direct-test phase"}
        return phase
    indexed = _hw04_step_index(steps)
    if flash_id not in indexed:
        record, rows, _captures, error = await _run_hw04_physical_session(
            session_factory,
            project_root,
            board,
            name="mutation",
            bootstrap=(),
            logical_steps=steps,
            pause=pause,
        )
        phase["physical_sessions"].append(record)
        _record_hw04_session_cleanup_error(phase, record)
        phase["steps"] = rows
        phase["cleanup"] = record.get("cleanup", {"attempted": False})
        if error is None:
            phase["status"] = "pass"
        else:
            phase["status"] = "failed"
            phase["failure"] = f"{type(error).__name__}: {error}"
        return phase

    mutation_ids = (
        (
            "raw_connect",
            "initial_ram_read",
            "raw_ram_write",
            "raw_ram_readback",
            "raw_ram_restore",
            "ram_restored_readback",
            "initial_flash_readback",
            "raw_flash",
        )
        if lane == "raw" and "initial_ram_read" in indexed
        else (
            "raw_connect",
            "initial_flash_readback",
            "raw_flash",
        )
        if lane == "raw"
        else (
            "full_assign",
            "full_connect",
            "full_load_validation_tool",
            "full_board_validate",
            *(
                identifier
                for base in _HW04_SAFE_PREFLIGHT_PAGE_BASES
                for identifier in (f"{base}_plan_guide", f"{base}_plan_accept", base)
                if identifier in indexed
            ),
            "safe_flash_plan_guide",
            "safe_flash_plan_accept",
            "safe_flash",
            "safe_containment_refusal_plan_guide",
            "safe_containment_refusal_plan_accept",
            "safe_containment_refusal",
        )
    )
    test_ids = (
        ("raw_flash_readback", "raw_restore")
        if lane == "raw"
        else (
            "safe_flash_readback_plan_guide",
            "safe_flash_readback_plan_accept",
            "safe_flash_readback",
            "safe_restore_plan_guide",
            "safe_restore_plan_accept",
            "safe_restore",
        )
    )
    restored_ids = (
        ("raw_restored_readback", "final_disconnect")
        if lane == "raw"
        else (
            *(
                identifier
                for base in _HW04_SAFE_RESTORED_PAGE_BASES
                for identifier in (f"{base}_plan_guide", f"{base}_plan_accept", base)
                if identifier in indexed
            ),
            "safe_restored_readback_plan_guide",
            "safe_restored_readback_plan_accept",
            "safe_restored_readback",
            "final_disconnect",
        )
    )
    try:
        mutation_steps = _hw04_select_steps(steps, mutation_ids)
        test_steps = _hw04_select_steps(steps, test_ids)
        restored_steps = _hw04_select_steps(steps, restored_ids)
        bootstrap = _hw04_verification_bootstrap(lane, steps)
    except Exception as exc:  # pragma: no cover - public fixtures validate exact lifecycles first
        phase["status"] = "failed"
        phase["failure"] = f"{type(exc).__name__}: {exc}"
        phase["cleanup"] = {"attempted": False, "reason": "incomplete HW04 direct-test fixture"}
        return phase

    state = _HW04RestoreState(lane)
    (
        mutation_record,
        mutation_rows,
        _mutation_captures,
        mutation_error,
    ) = await _run_hw04_physical_session(
        session_factory,
        project_root,
        board,
        name="mutation",
        bootstrap=(),
        logical_steps=mutation_steps,
        pause=pause,
        restore_state=state,
    )
    phase["physical_sessions"].append(mutation_record)
    _record_hw04_session_cleanup_error(phase, mutation_record)
    phase["steps"].extend(mutation_rows)

    async def fail_and_recover(error: Exception) -> dict[str, Any]:
        phase["status"] = "failed"
        phase["failure"] = f"{type(error).__name__}: {error}"
        if state.emergency_required:
            phase["emergency_restoration"] = await _fresh_emergency_hw04_restoration(
                session_factory,
                project_root,
                board,
                steps,
                lane=lane,
                trigger=state.dispatch_step or f"{flash_id} possible dispatch",
                pause=pause,
            )
        phase["cleanup"] = mutation_record.get("cleanup", {"attempted": False})
        return phase

    if mutation_error is not None:
        return await fail_and_recover(mutation_error)

    test_record, test_rows, _test_captures, test_error = await _run_hw04_physical_session(
        session_factory,
        project_root,
        board,
        name="test_verification_and_restore",
        bootstrap=bootstrap,
        logical_steps=test_steps,
        pause=pause,
    )
    phase["physical_sessions"].append(test_record)
    _record_hw04_session_cleanup_error(phase, test_record)
    phase["steps"].extend(_hw04_logical_rows(test_rows, bootstrap))
    if test_error is not None:
        return await fail_and_recover(test_error)

    (
        restored_record,
        restored_rows,
        _restored_captures,
        restored_error,
    ) = await _run_hw04_physical_session(
        session_factory,
        project_root,
        board,
        name="restoration_verification",
        bootstrap=bootstrap,
        logical_steps=restored_steps,
        pause=pause,
    )
    phase["physical_sessions"].append(restored_record)
    _record_hw04_session_cleanup_error(phase, restored_record)
    logical_restored_rows = _hw04_logical_rows(restored_rows, bootstrap)
    phase["steps"].extend(logical_restored_rows)
    required_restoration_ids = (
        {"raw_restored_readback"}
        if lane == "raw"
        else {base for base in _HW04_SAFE_RESTORED_PAGE_BASES if base in indexed}
        or {"safe_restored_readback"}
    )
    passed_restoration_ids = {
        row.get("id") for row in logical_restored_rows if row.get("passed") is True
    }
    if required_restoration_ids.issubset(passed_restoration_ids):
        state.restoration_readback_verified = True
    if restored_error is not None:
        return await fail_and_recover(restored_error)
    phase["status"] = "pass"
    phase["cleanup"] = restored_record.get("cleanup", {"attempted": False})
    return phase


async def _execute_hw04_case(
    fixture: Mapping[str, Any],
    root_spec: Mapping[str, Any],
    specs: Mapping[str, Any],
    session_factory: SessionFactory,
    *,
    pause: Callable[[str], Awaitable[None]],
) -> dict[str, Any]:
    """Run raw and full HW04 logical lifecycles without sharing a flash verifier."""

    result: dict[str, Any] = {"project_root": root_spec, "boards": {}}
    for name in BOARD_NAMES:
        spec = _mapping(specs[name], f"HW04.cases.boards.{name}")
        board = _mapping(fixture["boards"][name], f"HW04.boards.{name}")
        if spec["status"] == "blocked":
            result["boards"][name] = {"status": "blocked", "reason": spec["reason"]}
            continue
        outcome: dict[str, Any] = {"board_id": board["board_id"], "phases": []}
        result["boards"][name] = outcome
        if "retained_ram_evidence" in spec:
            outcome["retained_ram_evidence"] = _validate_retained_hw04_ram_evidence(
                spec["retained_ram_evidence"],
                board,
                f"HW04.{name}.retained_ram_evidence",
            )
        if "retained_raw_evidence" in spec:
            outcome["retained_raw_evidence"] = _validate_retained_hw04_raw_evidence(
                spec["retained_raw_evidence"],
                board,
                f"HW04.{name}.retained_raw_evidence",
            )
        raw = await _execute_hw04_lane(
            session_factory,
            Path(root_spec["raw"]),
            board,
            _hw04_steps(spec["raw_steps"], f"HW04.{name}.raw_steps"),
            lane="raw",
            phase_name="raw",
            pause=pause,
        )
        outcome["phases"].append(raw)
        if raw["status"] != "pass":
            outcome["status"] = "failed"
            outcome["phases"].append(
                {
                    "name": "full",
                    "project_root": str(root_spec["full"]),
                    "steps": [],
                    "physical_sessions": [],
                    "status": "blocked",
                    "reason": "previous HW04 raw lane failed; later safe flash was skipped",
                    "cleanup": {"attempted": False, "reason": "no board call was issued"},
                }
            )
            continue
        full = await _execute_hw04_lane(
            session_factory,
            Path(root_spec["full"]),
            board,
            _hw04_steps(spec["full_steps"], f"HW04.{name}.full_steps"),
            lane="safe",
            phase_name="full",
            pause=pause,
        )
        outcome["phases"].append(full)
        outcome["status"] = "pass" if full["status"] == "pass" else "failed"
    states = [result["boards"].get(name, {}).get("status") for name in BOARD_NAMES]
    result["status"] = (
        "failed" if "failed" in states else "blocked" if "blocked" in states else "pass"
    )
    return result


SessionFactory = Callable[[Path], contextlib.AbstractAsyncContextManager[Any]]


def _stdio_server_parameters(project_root: Path) -> Any:
    """Build the real opt-in server command without starting a transport or probe."""

    from mcp import StdioServerParameters

    environment = dict(os.environ)
    resolved_root = project_root.resolve()
    environment["BYO_MCP_ARTIFACT_ROOT"] = str(resolved_root)
    environment["BYO_MCP_MONITOR_ROOT"] = str(
        resolved_root / ".agent-workspace" / "runtime" / "at10-monitor"
    )
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pyocd_debug_mcp.server"],
        env=environment,
    )


async def execute_with_session_factory(
    fixture: Mapping[str, Any],
    cases: Sequence[str],
    session_factory: SessionFactory,
    *,
    pause: Callable[[str], Awaitable[None]],
    allow_write_flash: bool,
) -> dict[str, Any]:
    """Execute validated cases through fresh real or deterministic MCP sessions."""

    hw04_preflight: dict[str, dict[str, object]] = {}
    if "HW04" in cases:
        if not allow_write_flash:
            raise FixtureError("HW04 requires --allow-write-flash in addition to fixture approvals")
        for name, board in fixture["boards"].items():
            hw04_spec = fixture["cases"]["HW04"]["boards"][name]
            if hw04_spec["status"] != "ready":
                continue
            approvals = _mapping(board["disruption_approval"], f"boards.{name}.disruption_approval")
            ram_approval = _mapping(
                approvals["ram_write"], f"boards.{name}.disruption_approval.ram_write"
            )
            flash_approval = _mapping(
                approvals["flash"], f"boards.{name}.disruption_approval.flash"
            )
            if ram_approval["approved"] is not True or flash_approval["approved"] is not True:
                raise FixtureError(f"HW04 requires explicit RAM and flash approval for {name}")
            for lane in sorted(_FLASH_LANES):
                flash = _flash_lane(board, lane)
                for field in ("artifact", "restore_artifact"):
                    label = f"boards.{name}.flash_fixture.{lane}.{field}"
                    if not Path(_text(flash[field], label)).is_file():
                        raise FixtureError(
                            f"HW04 {name} {lane} {field} is not a readable local artifact"
                        )
                hw04_preflight.setdefault(name, {})[lane] = _preflight_hw04_lane(name, board, lane)
    transcript: dict[str, Any] = {
        "schema_version": 2,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cases": list(cases),
        "case_results": {},
    }
    if hw04_preflight:
        transcript["hw04_preflight"] = hw04_preflight
    for case in cases:
        root_spec = fixture["case_project_roots"][case]
        specs = fixture["cases"][case]["boards"]
        result: dict[str, Any] = {"project_root": root_spec, "boards": {}}
        if all(specs[name]["status"] == "blocked" for name in BOARD_NAMES):
            result["status"] = "blocked"
            result["boards"] = {
                name: {"status": "blocked", "reason": specs[name]["reason"]} for name in BOARD_NAMES
            }
            transcript["case_results"][case] = result
            continue
        if case == "HW06":
            # Both boards remain connected through one shared client/server
            # session while the two route reads occur.  Sequential isolated
            # sessions could never reveal cross-board routing/cleanup defects.
            root = Path(root_spec)
            async with session_factory(root) as session:
                captures_by_board: dict[str, dict[str, object]] = {}
                for identifier in (
                    "capabilities",
                    "assign",
                    "connect",
                    "load_setup_tool",
                    "board_validate",
                    "route_read_plan_guide",
                    "route_read_plan_accept",
                    "route_read",
                    "disconnect",
                ):
                    for name in BOARD_NAMES:
                        spec = specs[name]
                        board = fixture["boards"][name]
                        outcome = result["boards"].setdefault(
                            name,
                            {
                                "board_id": board["board_id"],
                                "shared_session": True,
                                "phases": [
                                    {"name": "concurrent", "project_root": str(root), "steps": []}
                                ],
                            },
                        )
                        if spec["status"] == "blocked":
                            outcome.update({"status": "blocked", "reason": spec["reason"]})
                            continue
                        if (
                            identifier
                            in {
                                "assign",
                                "load_setup_tool",
                                "board_validate",
                                "route_read_plan_guide",
                                "route_read_plan_accept",
                            }
                            and board["expected_tier"] == "no-setup"
                        ):
                            continue
                        step = next(item for item in spec["steps"] if item["id"] == identifier)
                        phase = outcome["phases"][0]
                        try:
                            rows, captures = await run_steps(
                                session,
                                root,
                                board,
                                [step],
                                pause=pause,
                                captures=captures_by_board.setdefault(name, {}),
                            )
                            phase["steps"].extend(rows)
                            captures_by_board[name] = captures
                            if outcome.get("status") != "failed":
                                outcome["status"] = "pass"
                        except Exception as exc:  # noqa: BLE001
                            if isinstance(exc, CheckFailed):
                                phase["steps"].extend(exc.rows)
                            outcome["status"] = "failed"
                            phase["failure"] = f"{type(exc).__name__}: {exc}"
                for name in BOARD_NAMES:
                    outcome = result["boards"].get(name)
                    if outcome is not None:
                        outcome["phases"][0]["cleanup"] = await _cleanup(
                            session, fixture["boards"][name]["board_id"]
                        )
            states = [result["boards"].get(name, {}).get("status") for name in BOARD_NAMES]
            result["status"] = (
                "failed" if "failed" in states else "blocked" if "blocked" in states else "pass"
            )
            transcript["case_results"][case] = result
            continue
        if case == "HW04":
            if not isinstance(root_spec, Mapping):
                raise FixtureError("HW04 requires distinct raw and full project roots")
            transcript["case_results"][case] = await _execute_hw04_case(
                fixture,
                root_spec,
                specs,
                session_factory,
                pause=pause,
            )
            continue
        fields = (
            ("pre_restart_steps", "post_restart_steps")
            if case == "HW05"
            else ("raw_steps", "full_steps")
            if case == "HW04"
            else ("steps",)
        )
        # HW05 deliberately starts a fresh MCP process after the manual grant
        # was observed, but its stale-call proof must replay the exact old
        # identifiers.  Captures are evidence, not server authority; only the
        # second server decides whether they are stale.
        restart_captures: dict[str, dict[str, object]] = {} if case == "HW05" else {}
        for phase_index, field in enumerate(fields):
            root = Path(
                root_spec["raw" if phase_index == 0 else "full"] if case == "HW04" else root_spec
            )
            fresh_recoveries: list[
                tuple[
                    dict[str, Any],
                    Mapping[str, Any],
                    Sequence[Mapping[str, Any]],
                    _HW04RestoreState,
                ]
            ] = []
            session_unusable = False
            session_context = session_factory(root)
            session = await session_context.__aenter__()
            try:
                for name in BOARD_NAMES:
                    spec = specs[name]
                    board = fixture["boards"][name]
                    outcome = result["boards"].setdefault(
                        name, {"board_id": board["board_id"], "phases": []}
                    )
                    if spec["status"] == "blocked":
                        outcome.update({"status": "blocked", "reason": spec["reason"]})
                        continue
                    if case == "HW04" and session_unusable:
                        outcome.update(
                            {
                                "status": "blocked",
                                "reason": "previous HW04 transport failed; shared session was closed",
                            }
                        )
                        outcome["phases"].append(
                            {
                                "name": "full" if phase_index else "raw",
                                "project_root": str(root),
                                "steps": [],
                                "status": "blocked",
                                "reason": "no board call was issued after an uncertain shared transport",
                                "cleanup": {
                                    "attempted": False,
                                    "reason": "the failed shared session was not reused",
                                },
                            }
                        )
                        continue
                    if case == "HW04" and outcome.get("status") == "failed":
                        outcome["phases"].append(
                            {
                                "name": "full" if phase_index else "raw",
                                "project_root": str(root),
                                "steps": [],
                                "status": "blocked",
                                "reason": "previous HW04 lane failed; later test flash was skipped",
                                "cleanup": {
                                    "attempted": False,
                                    "reason": "no board call was issued in skipped phase",
                                },
                            }
                        )
                        continue
                    phase: dict[str, Any] = {
                        "name": (
                            "post_restart"
                            if case == "HW05" and phase_index
                            else "full"
                            if case == "HW04" and phase_index
                            else "raw"
                            if case == "HW04"
                            else "initial"
                        ),
                        "project_root": str(root),
                        "steps": [],
                    }
                    outcome["phases"].append(phase)
                    restore_state = (
                        _HW04RestoreState("safe" if phase_index else "raw")
                        if case == "HW04"
                        else None
                    )
                    try:
                        if case == "HW05" and phase_index:
                            phase["restart_records"] = {}
                            for action in spec["restart_records"]:
                                record = _manual_record(root, action)
                                _contains(
                                    record,
                                    {"action": action, "state": "locked"},
                                    f"restart.{action}",
                                )
                                phase["restart_records"][action] = record
                        rows, captures = await run_steps(
                            session,
                            root,
                            board,
                            spec[field],
                            pause=pause,
                            captures=restart_captures.setdefault(name, {})
                            if case == "HW05"
                            else None,
                            hw04_restore=restore_state,
                        )
                        phase["steps"] = rows
                        if case == "HW05":
                            restart_captures[name] = captures
                        if restore_state is not None and restore_state.emergency_required:
                            raise CheckFailed(
                                f"HW04 {restore_state.lane} completed without an exact restoration readback"
                            )
                        outcome["status"] = "pass"
                    except Exception as exc:  # noqa: BLE001 - transcript must retain the failed assertion
                        if isinstance(exc, CheckFailed):
                            phase["steps"] = exc.rows
                        outcome["status"] = "failed"
                        phase["failure"] = f"{type(exc).__name__}: {exc}"
                    finally:
                        if restore_state is not None and restore_state.emergency_required:
                            if restore_state.transport_uncertain:
                                phase["failed_session_cleanup"] = {
                                    "attempted": True,
                                    "method": "context_exit",
                                    "reason": "possible post-dispatch transport failure; no same-session restore",
                                }
                                fresh_recoveries.append((phase, board, spec[field], restore_state))
                                session_unusable = True
                            else:
                                phase["emergency_restoration"] = await _emergency_hw04_restoration(
                                    session,
                                    root,
                                    board,
                                    spec[field],
                                    lane=restore_state.lane,
                                    trigger=restore_state.dispatch_step
                                    or f"{restore_state.lane}_flash possible dispatch",
                                    pause=pause,
                                )
                        final_disconnect_completed = (
                            case == "HW04"
                            and bool(phase["steps"])
                            and phase["steps"][-1].get("id") == "final_disconnect"
                            and phase["steps"][-1].get("passed") is True
                        )
                        if final_disconnect_completed:
                            phase["cleanup"] = {
                                "attempted": False,
                                "reason": "final_disconnect completed as the HW04 lifecycle terminus",
                            }
                        elif not (restore_state is not None and restore_state.transport_uncertain):
                            phase["cleanup"] = await _cleanup(session, board["board_id"])
            finally:
                try:
                    await session_context.__aexit__(None, None, None)
                except Exception as exc:  # noqa: BLE001 - a fresh emergency restore must still run
                    for phase, _board, _steps, _restore_state in fresh_recoveries:
                        cleanup = _mapping(
                            phase["failed_session_cleanup"], "failed_session_cleanup"
                        )
                        cleanup["error"] = f"{type(exc).__name__}: {exc}"
                    if not fresh_recoveries:
                        raise
                finally:
                    for phase, board, phase_steps, restore_state in fresh_recoveries:
                        phase["emergency_restoration"] = await _fresh_emergency_hw04_restoration(
                            session_factory,
                            root,
                            board,
                            phase_steps,
                            lane=restore_state.lane,
                            trigger=restore_state.dispatch_step
                            or f"{restore_state.lane}_flash possible dispatch",
                            pause=pause,
                        )
        states = [result["boards"].get(name, {}).get("status") for name in BOARD_NAMES]
        result["status"] = (
            "failed" if "failed" in states else "blocked" if "blocked" in states else "pass"
        )
        transcript["case_results"][case] = result
    statuses = [result["status"] for result in transcript["case_results"].values()]
    transcript["overall_status"] = (
        "failed" if "failed" in statuses else "blocked" if "blocked" in statuses else "pass"
    )
    transcript["finished_at"] = datetime.now(timezone.utc).isoformat()
    return transcript


async def execute(
    fixture: Mapping[str, Any], cases: Sequence[str], *, allow_write_flash: bool
) -> dict[str, Any]:
    """Run selected cases through actual stdio MCP transport/server processes."""

    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    @contextlib.asynccontextmanager
    async def live_session(project_root: Path) -> AsyncIterator[Any]:
        parameters = _stdio_server_parameters(project_root)
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    return await execute_with_session_factory(
        fixture, cases, live_session, pause=_default_pause, allow_write_flash=allow_write_flash
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures",
        type=Path,
        required=True,
        help="private schema-v2 fixture with exact HW01--HW06 calls",
    )
    parser.add_argument(
        "--validate-fixtures",
        action="store_true",
        help="validate only; never start a server or inspect hardware",
    )
    parser.add_argument(
        "--execute", action="store_true", help="start actual stdio MCP server sessions"
    )
    parser.add_argument(
        "--cases", default="", help="comma-separated HW01--HW06 cases; required with --execute"
    )
    parser.add_argument(
        "--allow-write-flash",
        action="store_true",
        help="required with fixture approval before HW04 write/flash",
    )
    parser.add_argument(
        "--transcript", type=Path, help="private path for expected-vs-actual transcript"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        fixture = load_fixture(args.fixtures)
        if args.validate_fixtures and not args.execute:
            print(json.dumps({"status": "fixture_valid", "schema_version": 2}, indent=2))
            return 0
        if not args.execute:
            raise FixtureError("select --validate-fixtures or explicit --execute")
        if args.transcript is None:
            raise FixtureError("--execute requires --transcript for private full evidence")
        transcript = asyncio.run(
            execute(fixture, selected_cases(args.cases), allow_write_flash=args.allow_write_flash)
        )
        rendered = json.dumps(transcript, indent=2, sort_keys=True, default=str)
        args.transcript.parent.mkdir(parents=True, exist_ok=True)
        args.transcript.write_text(rendered + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "overall_status": transcript["overall_status"],
                    "cases": {
                        case: result["status"]
                        for case, result in transcript["case_results"].items()
                    },
                },
                sort_keys=True,
            )
        )
        return {"pass": 0, "failed": 1, "blocked": 2}[transcript["overall_status"]]
    except FixtureError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
