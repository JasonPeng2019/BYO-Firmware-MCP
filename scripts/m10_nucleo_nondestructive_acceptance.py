#!/usr/bin/env python3
"""Run the non-destructive Task 20 Nucleo phase with exact hardware identity."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) == 1:
        text = getattr(value[0], "text", None)
        if isinstance(text, str):
            return text
    raise TypeError(f"Expected one text result, got {type(value).__name__}")


def _json(value: object) -> dict[str, Any]:
    result = json.loads(_text(value))
    if not isinstance(result, dict):
        raise TypeError("Expected a JSON object")
    return result


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--preserved-m7-root", type=Path, required=True)
    parser.add_argument("--probe-id", required=True)
    parser.add_argument("--serial-id", required=True)
    parser.add_argument("--board-id", required=True)
    parser.add_argument("--target", required=True)
    return parser.parse_args()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def _run(args: argparse.Namespace) -> dict[str, object]:
    started = time.perf_counter()
    artifact_root = args.artifact_root.resolve()
    checkout = Path.cwd().resolve()
    if artifact_root == checkout or checkout in artifact_root.parents:
        raise ValueError("--artifact-root must be outside the checkout")
    if artifact_root.exists():
        raise FileExistsError("A clean Task 20 Nucleo artifact root is required")
    preserved_root = args.preserved_m7_root.resolve()
    preserved_result = preserved_root / "acceptance.json"
    preserved_firm = preserved_root / ".firm"
    if not preserved_result.is_file() or not preserved_firm.is_dir():
        raise FileNotFoundError("The accepted M7 evidence root is incomplete")
    preserved = json.loads(preserved_result.read_text(encoding="utf-8"))
    if preserved.get("terminal_status") != "nucleo_m7_hardware_passed":
        raise RuntimeError("The preserved M7 result is not an accepted pass")

    artifact_root.mkdir(parents=True)
    shutil.copytree(preserved_firm, artifact_root / ".firm")
    os.environ["BYO_MCP_ARTIFACT_ROOT"] = str(artifact_root)

    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.guardrails.gate import ValidationStamp

    if server._firm_store.layout.project_root != artifact_root:
        raise RuntimeError("Task 20 artifact-root isolation did not take effect")

    calls: list[dict[str, object]] = []

    async def call(name: str, arguments: dict[str, object]) -> object:
        began = time.perf_counter()
        result = await server.mcp.call_tool(name, arguments)
        calls.append(
            {
                "tool": name,
                "arguments": arguments,
                "elapsed_seconds": time.perf_counter() - began,
                "result": _text(result),
            }
        )
        return result

    # This run validates an already-preserved profile/map. It must enumerate live
    # connections without re-entering initial setup or requiring setup-only PDF evidence.
    inventory = server._validation_inventory()
    matching_probe = [item for item in inventory.probes if item.probe_id == args.probe_id]
    matching_uart = [item for item in inventory.serial_ports if item.serial_id == args.serial_id]
    if len(matching_probe) != 1:
        raise RuntimeError("The exact Nucleo probe is not uniquely visible")
    if len(matching_uart) != 1:
        raise RuntimeError("The exact Nucleo UART is not uniquely visible")
    if matching_uart[0].usb_serial != args.probe_id:
        raise RuntimeError("Probe and UART stable identities do not match")

    preserved_hashes = _tree_hashes(preserved_firm)
    copied_hashes = _tree_hashes(artifact_root / ".firm")
    if copied_hashes != preserved_hashes:
        raise RuntimeError("The isolated M7 safety/profile fixture copy changed")

    await call("initialization_handshake", {})
    gate_before = server.gate_manager.snapshot(args.board_id)
    await call(
        "connect",
        {
            "board_id": args.board_id,
            "unique_id": args.probe_id,
            "target": args.target,
            "board_config": None,
        },
    )
    connection = server.connection_manager.connection_for(args.board_id)
    if connection.handle.probe_uid != args.probe_id:
        raise RuntimeError("The active connection does not match the requested probe")

    await call(
        "load_setup_tool",
        {"board_id": args.board_id, "tool_name": "board_safety_refresh"},
    )
    refresh = _json(await call("board_safety_refresh", {"board_id": args.board_id}))
    if refresh.get("status") != "safety_refresh_completed":
        raise RuntimeError(f"Current safety refresh failed: {refresh}")

    await call(
        "load_setup_tool",
        {"board_id": args.board_id, "tool_name": "board_validate"},
    )
    validation = _json(
        await call(
            "board_validate",
            {
                "board_id": args.board_id,
                "probe_id": args.probe_id,
                "serial_id": args.serial_id,
            },
        )
    )
    if validation.get("status") != "validation_passed_uart_not_configured":
        raise RuntimeError(f"Nucleo validation did not pass: {validation}")
    gate_after = server.gate_manager.snapshot(args.board_id)
    if not isinstance(gate_after, ValidationStamp):
        raise RuntimeError("Successful validation did not stamp the Nucleo gate")

    await call("reset_and_halt-plan", {})
    await call(
        "reset_and_halt-plan",
        {
            "board_id": args.board_id,
            "hypothesis": "The connected Nucleo can halt safely for bounded register reads.",
            "hypothesis_made": True,
            "strategy": "Reset and halt once, read PC and R0, then immediately reset and run.",
            "strategy_evaluated": True,
            "expected_fail_return": "A bounded reset or halt refusal.",
            "expected_success_return": "The core halts for two read-only register observations.",
            "max_calls": 3,
            "max_calls_buffer": 0,
        },
    )
    reset_and_halt = _text(await call("reset_and_halt", {"board_id": args.board_id}))
    execution_state = _text(
        await call("read_execution_state", {"board_id": args.board_id, "name": "pc"})
    )
    reset_and_halt_r0 = _text(
        await call("reset_and_halt", {"board_id": args.board_id})
    )
    cpu_register = _text(
        await call("read_cpu_register", {"board_id": args.board_id, "name": "r0"})
    )
    reset_and_halt_memory = _text(
        await call("reset_and_halt", {"board_id": args.board_id})
    )

    read_parameters = {"address": 0x08000000, "width": 32, "length": None}
    await call("read_memory_address-plan", {})
    await call(
        "read_memory_address-plan",
        {
            "board_id": args.board_id,
            "hypothesis": "The validated Nucleo can perform one bounded flash-base read.",
            "hypothesis_made": True,
            "strategy": "Read one 32-bit value from the profiled safe test address.",
            "strategy_evaluated": True,
            "expected_fail_return": "A pre-read validation or map refusal.",
            "expected_success_return": "One 32-bit value and the safe-exit reminder.",
            "max_calls": 1,
            "max_calls_buffer": 0,
            **read_parameters,
        },
    )
    memory_read = _text(
        await call("read_memory_address", {"board_id": args.board_id, **read_parameters})
    )
    if not memory_read.startswith("0x20001180"):
        raise RuntimeError(f"Halted flash-base read did not match the v2 vector: {memory_read}")
    reset_and_run = _text(await call("reset_and_run", {"board_id": args.board_id}))

    serial_parameters = {
        "expected_text": None,
        "read_seconds": 1.0,
        "baudrate": 115200,
        "port": args.serial_id,
        "reset_on_open": False,
    }
    await call("read_serial-plan", {})
    await call(
        "read_serial-plan",
        {
            "board_id": args.board_id,
            "hypothesis": "The selected Nucleo UART can be captured for one bounded second.",
            "hypothesis_made": True,
            "strategy": "Capture only the exact COM12 endpoint without resetting the board.",
            "strategy_evaluated": True,
            "expected_fail_return": "A bounded UART or validation refusal.",
            "expected_success_return": "Captured bytes, possibly empty, and a safe-exit reminder.",
            "max_calls": 1,
            "max_calls_buffer": 0,
            **serial_parameters,
        },
    )
    serial_read = _text(
        await call("read_serial", {"board_id": args.board_id, **serial_parameters})
    )

    await call("disconnect", {"board_id": args.board_id})
    if server.gate_manager.snapshot(args.board_id) is not None:
        raise RuntimeError("Disconnect did not close the Nucleo gate")
    await call(
        "connect",
        {
            "board_id": args.board_id,
            "unique_id": args.probe_id,
            "target": args.target,
            "board_config": None,
        },
    )
    if server.gate_manager.snapshot(args.board_id) is not None:
        raise RuntimeError("Reconnect unexpectedly restored the gate")
    await call(
        "load_setup_tool",
        {"board_id": args.board_id, "tool_name": "board_validate"},
    )
    post_reconnect_validation = _json(
        await call(
            "board_validate",
            {
                "board_id": args.board_id,
                "probe_id": args.probe_id,
                "serial_id": args.serial_id,
            },
        )
    )
    if post_reconnect_validation.get("status") != "validation_passed_uart_not_configured":
        raise RuntimeError("Post-reconnect validation did not pass")
    await call("disconnect", {"board_id": args.board_id})

    accepted_m7_hash = _sha256(preserved_result)
    return {
        "schema_version": 1,
        "phase": "nucleo_l476rg_setup_safety_validation_actions",
        "status": "pass",
        "started_at": calls[0].get("started_at", _timestamp()) if calls else _timestamp(),
        "completed_at": _timestamp(),
        "elapsed_seconds": time.perf_counter() - started,
        "exact_command": [sys.executable, *sys.argv],
        "identity": {
            "board_id": args.board_id,
            "probe_id": args.probe_id,
            "serial_id": args.serial_id,
            "uart_usb_serial": matching_uart[0].usb_serial,
            "target": args.target,
        },
        "validation_inventory": {
            "probes": [asdict(item) for item in inventory.probes],
            "serial_ports": [asdict(item) for item in inventory.serial_ports],
        },
        "preserved_fixture": {
            "root": str(preserved_root),
            "acceptance_sha256": accepted_m7_hash,
            "copied_file_count": len(copied_hashes),
            "copy_hashes_match": True,
        },
        "safety_refresh": refresh,
        "validation": validation,
        "gate_before_connect": asdict(gate_before) if gate_before else None,
        "gate_after_validation": asdict(gate_after),
        "always_available_actions": {
            "reset_and_halt": reset_and_halt,
            "read_execution_state": execution_state,
            "reset_and_halt_r0": reset_and_halt_r0,
            "read_cpu_register": cpu_register,
            "reset_and_halt_memory": reset_and_halt_memory,
            "reset_and_run": reset_and_run,
        },
        "guarded_actions": {
            "read_memory_address": memory_read,
            "read_serial": serial_read,
        },
        "application_flash_containment": {
            "execution": "not_repeated",
            "preserved_m7_result": str(preserved_result),
            "preserved_m7_sha256": accepted_m7_hash,
            "program_calls": sum(
                item.get("event") == "program"
                for item in preserved.get("pyocd_backend", {}).get("programmer_calls", [])
            ),
            "mass_erase_calls": preserved.get("pyocd_backend", {}).get("mass_erase_calls"),
            "unsafe_boundary": preserved.get("boundary_images", {}).get("unsafe"),
        },
        "post_reconnect_validation": post_reconnect_validation,
        "final_gate": None,
        "calls": calls,
        "new_reports": [
            {"path": str(path), "sha256": _sha256(path)}
            for path in sorted((artifact_root / ".firm").glob("**/report.json"))
        ],
    }


def main() -> None:
    args = _arguments()
    artifact_root = args.artifact_root.resolve()
    result_path = artifact_root / "acceptance.json"
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        artifact_root.mkdir(parents=True, exist_ok=True)
        result = {
            "schema_version": 1,
            "phase": "nucleo_l476rg_setup_safety_validation_actions",
            "status": "fail",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "recorded_at": _timestamp(),
            "exact_command": [sys.executable, *sys.argv],
        }
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "pass", "result": str(result_path)}))


if __name__ == "__main__":
    main()
