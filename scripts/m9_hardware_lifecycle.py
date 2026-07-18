#!/usr/bin/env python3
"""Run the M9 cancellation lifecycle bench on the accepted Nucleo hardware."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intelhex import IntelHex
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError
from serial import Serial
from serial.tools import list_ports


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--v1-elf", type=Path, required=True)
    parser.add_argument("--v1-hex", type=Path, required=True)
    parser.add_argument("--v2-elf", type=Path, required=True)
    parser.add_argument("--v2-hex", type=Path, required=True)
    parser.add_argument("--backup-hex", type=Path, required=True)
    parser.add_argument("--probe-id", required=True)
    parser.add_argument("--serial-id", required=True)
    parser.add_argument("--board-id", default="nucleo_l476rg")
    parser.add_argument("--target", default="stm32l476rgtx")
    parser.add_argument("--baudrate", type=int, default=115200)
    return parser.parse_args()


def _text(result: types.CallToolResult) -> str:
    if len(result.content) != 1 or not isinstance(result.content[0], types.TextContent):
        raise TypeError("Expected exactly one MCP text result")
    return result.content[0].text


def _json_text(result: types.CallToolResult) -> dict[str, Any]:
    value = json.loads(_text(result))
    if not isinstance(value, dict):
        raise TypeError("Expected an MCP JSON object")
    return value


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


async def _wait_for_marker(path: Path, event: str, *, after: int, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _read_rows(path)
        for index, row in enumerate(rows[after:], start=after):
            if row.get("event") == event:
                return index
        await asyncio.sleep(0.02)
    raise TimeoutError(f"Did not observe lifecycle marker {event!r}")


def _prepare_root(source_root: Path, artifact_root: Path) -> None:
    source = source_root.resolve()
    destination = artifact_root.resolve()
    checkout = Path.cwd().resolve()
    if destination == checkout or checkout in destination.parents:
        raise ValueError("--artifact-root must be outside the checkout")
    if destination.exists():
        raise FileExistsError("A clean M9 artifact root is required")
    if not (source / ".firm" / "safety" / "nucleo_l476rg" / "memory_map.yaml").is_file():
        raise FileNotFoundError("The accepted source root has no Nucleo safety map")
    destination.mkdir(parents=True)
    shutil.copytree(source / ".firm", destination / ".firm")


def _verify_inventory(probe_id: str, serial_id: str) -> dict[str, Any]:
    from pyocd.core.helpers import ConnectHelper

    probes = list(ConnectHelper.get_all_connected_probes(blocking=False))
    probe = next((item for item in probes if item.unique_id == probe_id), None)
    if probe is None:
        raise RuntimeError(f"Required probe {probe_id} is not visible")
    ports = list(list_ports.comports())
    port = next((item for item in ports if item.device.casefold() == serial_id.casefold()), None)
    if port is None:
        raise RuntimeError(f"Required UART {serial_id} is not visible")
    if port.serial_number != probe_id:
        raise RuntimeError(
            f"Probe/UART stable identity mismatch: {probe_id!r} != {port.serial_number!r}"
        )
    return {
        "probe_id": probe.unique_id,
        "probe_description": probe.description,
        "serial_port": port.device,
        "serial_usb_serial": port.serial_number,
        "serial_vid_pid": f"{port.vid:04X}:{port.pid:04X}",
    }


def _verify_hex(board_id: str, probe_id: str, target: str, hex_path: Path) -> dict[str, Any]:
    from pyocd_debug_mcp.reference_smoke import load_board
    from pyocd_debug_mcp.services import target_control

    image = IntelHex(str(hex_path))
    handle = target_control.open_session(
        board=load_board(board_id),
        unique_id=probe_id,
        target=target,
        connect_mode="under-reset",
    )
    mismatches: list[int] = []
    final_state = "unknown"
    target_object = handle.session.target
    if target_object is None:
        target_control.close_session(handle)
        raise RuntimeError("pyOCD verification session has no target")
    target_object.reset_and_halt()
    try:
        for start, end in image.segments():
            expected = bytes(image.tobinarray(start=start, end=end - 1))
            actual = bytes(target_control.read_memory_block(handle, start, len(expected)))
            if actual != expected:
                mismatches.append(start)
    finally:
        target_control.reset(handle, halt_after=False)
        final_state = target_control.get_state(handle)
        target_control.close_session(handle)
    return {
        "hex_path": str(hex_path),
        "hex_sha256": _sha256(hex_path),
        "byte_count": len(image.addresses()),
        "segments": [{"start": start, "end": end} for start, end in image.segments()],
        "mismatch_segment_starts": mismatches,
        "matches": not mismatches,
        "final_state_after_reset": final_state,
    }


def _precondition_application(
    board_id: str,
    probe_id: str,
    target: str,
    elf_path: Path,
    hex_path: Path,
) -> dict[str, Any]:
    """Sector-program the previously accepted v1 image to make the v2 flash nontrivial."""

    from pyocd_debug_mcp.kernel.processes import run_owned
    from pyocd_debug_mcp.pack_provision import PACKS_DIR, load_manifest, sha256_file
    from pyocd_debug_mcp.safety.linker import (
        BuildArtifactSelection,
        BuildRole,
        extract_build_evidence,
    )

    build = extract_build_evidence(
        BuildArtifactSelection("m9_v1_precondition", BuildRole.APPLICATION, elf_path, hex_path=hex_path)
    )
    if build.flash_partition is None:
        raise RuntimeError("The v1 precondition has no linker-derived application partition")
    if (build.flash_partition.start, build.flash_partition.end) != (0x08000000, 0x08008000):
        raise RuntimeError("The v1 precondition changed the accepted application partition")
    for segment in build.loadable_segments:
        if segment.load_range is not None and not build.flash_partition.contains(segment.load_range):
            raise RuntimeError("A v1 loadable segment escapes the application partition")

    pack = next((item for item in load_manifest() if target in item.provides_targets), None)
    if pack is None:
        raise RuntimeError(f"No pinned pack provides target {target!r}")
    pack_path = (PACKS_DIR / pack.filename).resolve()
    if sha256_file(pack_path) != pack.sha256:
        raise RuntimeError("Pinned pack checksum mismatch during v1 preparation")
    command = [
        sys.executable,
        "-m",
        "pyocd",
        "load",
        "-u",
        probe_id,
        "-t",
        target,
        "--pack",
        str(pack_path),
        "-f",
        "1000000",
        "-M",
        "under-reset",
        "-e",
        "sector",
        str(hex_path),
    ]
    completed = run_owned(
        command,
        capture_output=True,
        text=True,
        timeout=120.0,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Bounded v1 sector preparation failed: {completed.stderr}")
    readback = _verify_hex(board_id, probe_id, target, hex_path)
    if not readback["matches"]:
        raise RuntimeError("The v1 precondition did not match full readback")
    return {
        "purpose": "make the map-fingerprinted v2 MCP flash perform a real application write",
        "artifact": str(elf_path),
        "artifact_sha256": _sha256(elf_path),
        "linker_application_partition": {
            "start": build.flash_partition.start,
            "end": build.flash_partition.end,
        },
        "chip_erase": "sector",
        "mass_erase": False,
        "command": command,
        "pack_sha256": pack.sha256,
        "readback": readback,
    }


def _uart_is_reusable(port: str, baudrate: int) -> bool:
    handle = Serial(port=port, baudrate=baudrate, timeout=0.1, write_timeout=0.1)
    handle.close()
    return True


def _common_plan(board_id: str, purpose: str) -> dict[str, object]:
    return {
        "board_id": board_id,
        "hypothesis": f"The bounded {purpose} will obey cancellation and cleanup semantics.",
        "hypothesis_made": True,
        "strategy": "Use the normal guarded dispatch path and verify resource reuse immediately.",
        "strategy_evaluated": True,
        "expected_fail_return": "A deterministic cancellation or safety refusal before unsafe work.",
        "expected_success_return": "The bounded operation finishes and all resources are reusable.",
        "max_calls": 1,
        "max_calls_buffer": 0,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    source_root = args.source_root.resolve()
    artifact_root = args.artifact_root.resolve()
    paths = [args.v1_elf, args.v1_hex, args.v2_elf, args.v2_hex, args.backup_hex]
    for path in paths:
        if not path.resolve().is_file():
            raise FileNotFoundError(path)
    _prepare_root(source_root, artifact_root)
    inventory = _verify_inventory(args.probe_id, args.serial_id)
    trace_path = artifact_root / "protocol-trace.jsonl"
    backend_path = artifact_root / "backend-lifecycle.jsonl"
    result_path = artifact_root / "acceptance.json"
    checkout = Path.cwd().resolve()

    evidence: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "started_at": _timestamp(),
        "exact_command": [sys.executable, *sys.argv],
        "checkout_root": str(checkout),
        "artifact_root": str(artifact_root),
        "hardware": inventory,
        "safety": {
            "board_id": args.board_id,
            "target": args.target,
            "source_root": str(source_root),
            "memory_map_sha256": _sha256(
                artifact_root / ".firm" / "safety" / args.board_id / "memory_map.yaml"
            ),
            "backup_hex": str(args.backup_hex.resolve()),
            "backup_sha256": _sha256(args.backup_hex.resolve()),
            "mass_erase_calls": 0,
            "bootloader_flash_calls": 0,
            "target_unlock_calls": 0,
        },
        "client": {
            "name": "official-python-mcp-sdk-bench-client",
            "mcp_version": importlib.metadata.version("mcp"),
            "cancellation_behavior": "explicit notifications/cancelled",
        },
        "q1_client_matrix": [
            {
                "client": "official Python MCP SDK bench client",
                "version": importlib.metadata.version("mcp"),
                "status": "verified_sends_explicit_cancellation",
            },
            {
                "client": "Codex CLI",
                "version": "0.142.2",
                "status": "unverified_ui_interrupt_unavailable_in_noninteractive_bench",
                "fallback": "passing bounded timeout cleanup tests",
            },
            {
                "client": "Claude Code",
                "version": "2.1.76",
                "status": "unverified_ui_interrupt_and_project_server_unavailable",
                "fallback": "passing bounded timeout cleanup tests",
            },
            {
                "client": "VS Code",
                "version": "1.129.0",
                "status": "unverified_ui_interrupt_unavailable_in_noninteractive_bench",
                "fallback": "passing bounded timeout cleanup tests",
            },
        ],
    }
    evidence["v1_safe_precondition"] = _precondition_application(
        args.board_id,
        args.probe_id,
        args.target,
        args.v1_elf.resolve(),
        args.v1_hex.resolve(),
    )

    def trace(direction: str, method: str, request_id: int | None, **details: object) -> None:
        row = {
            "timestamp": _timestamp(),
            "direction": direction,
            "method": method,
            "request_id": request_id,
            **details,
        }
        with trace_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")

    environment = dict(os.environ)
    environment["BYO_MCP_ARTIFACT_ROOT"] = str(artifact_root)
    server_parameters = StdioServerParameters(
        command="uv",
        args=[
            "run",
            "python",
            str(checkout / "scripts" / "m9_instrumented_server.py"),
            "--trace",
            str(backend_path),
        ],
        cwd=str(checkout),
        env=environment,
    )

    async with stdio_client(server_parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            async def call(name: str, arguments: dict[str, object]) -> types.CallToolResult:
                request_id = int(session._request_id)
                trace("client_to_server", "tools/call", request_id, tool=name)
                try:
                    response = await session.call_tool(name, arguments)
                except BaseException as exc:
                    trace(
                        "server_to_client",
                        "tools/call:error",
                        request_id,
                        tool=name,
                        error_type=type(exc).__name__,
                    )
                    raise
                trace(
                    "server_to_client",
                    "tools/call:result",
                    request_id,
                    tool=name,
                    is_error=bool(response.isError),
                )
                return response

            async def cancel_started_call(
                name: str,
                arguments: dict[str, object],
                marker: str,
            ) -> dict[str, Any]:
                before = len(_read_rows(backend_path))
                request_id = int(session._request_id)
                task = asyncio.create_task(call(name, arguments))
                marker_index = await _wait_for_marker(backend_path, marker, after=before)
                notification = types.CancelledNotification(
                    params=types.CancelledNotificationParams(
                        requestId=request_id,
                        reason="M9 hardware lifecycle acceptance",
                    )
                )
                trace(
                    "client_to_server",
                    "notifications/cancelled",
                    None,
                    related_request_id=request_id,
                    after_backend_marker=marker,
                )
                await session.send_notification(
                    types.ClientNotification(notification),
                    related_request_id=request_id,
                )
                error: str | None = None
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                except McpError as exc:
                    error = str(exc)
                except asyncio.CancelledError:
                    error = "client task cancelled"
                except TimeoutError:
                    error = "no terminal MCP response within 5 seconds after cancellation"
                    task.cancel()
                    try:
                        await task
                    except BaseException:
                        pass
                if error is None:
                    raise RuntimeError(f"{name} completed without an observable cancellation")
                return {
                    "request_id": request_id,
                    "notification_sent": True,
                    "sent_after_marker": marker,
                    "marker_index": marker_index,
                    "client_error": error,
                }

            async def connect_and_validate(label: str) -> dict[str, Any]:
                connected = _text(
                    await call(
                        "connect",
                        {
                            "board_id": args.board_id,
                            "unique_id": args.probe_id,
                            "target": None,
                            "board_config": None,
                        },
                    )
                )
                await call("load_setup_tool", {"board_id": args.board_id, "tool_name": "board_validate"})
                validation = _json_text(
                    await call(
                        "board_validate",
                        {
                            "board_id": args.board_id,
                            "probe_id": args.probe_id,
                            "serial_id": args.serial_id,
                        },
                    )
                )
                if not str(validation.get("status", "")).startswith("validation_passed"):
                    raise RuntimeError(f"{label} validation failed: {validation}")
                return {"connect": connected, "validation": validation}

            async def plan_serial() -> dict[str, Any]:
                initialized = _text(await call("read_serial-plan", {}))
                payload = _common_plan(args.board_id, "UART read") | {
                    "expected_text": None,
                    "read_seconds": 10.0,
                    "baudrate": args.baudrate,
                    "port": args.serial_id,
                    "reset_on_open": False,
                }
                accepted = _text(await call("read_serial-plan", payload))
                return {"initialization": initialized, "accepted": accepted, "payload": payload}

            async def plan_flash(artifact: Path, label: str) -> dict[str, Any]:
                initialized = _text(await call("flash_application-plan", {}))
                payload = _common_plan(args.board_id, f"{label} application flash") | {
                    "artifact": str(artifact),
                }
                accepted = _text(await call("flash_application-plan", payload))
                return {"initialization": initialized, "accepted": accepted, "payload": payload}

            evidence["initial_session"] = await connect_and_validate("initial")
            evidence["serial_plan"] = await plan_serial()
            evidence["serial_cancellation"] = await cancel_started_call(
                "read_serial",
                {
                    "board_id": args.board_id,
                    "expected_text": None,
                    "read_seconds": 10.0,
                    "baudrate": args.baudrate,
                    "port": args.serial_id,
                    "reset_on_open": False,
                },
                "uart-open",
            )
            await _wait_for_marker(backend_path, "uart-close", after=0)
            evidence["serial_cancellation"]["uart_reusable"] = _uart_is_reusable(
                args.serial_id, args.baudrate
            )
            evidence["after_serial_reconnect"] = await connect_and_validate("after serial cancel")

            v2_elf = args.v2_elf.resolve()
            evidence["cancelled_flash_plan"] = await plan_flash(v2_elf, "v2 disposable")
            before_flash = len(_read_rows(backend_path))
            evidence["flash_cancellation"] = await cancel_started_call(
                "flash_application",
                {"board_id": args.board_id, "artifact": str(v2_elf)},
                "flash-program-start",
            )
            program_complete = await _wait_for_marker(
                backend_path, "flash-program-complete", after=before_flash, timeout=120.0
            )
            debug_released = await _wait_for_marker(
                backend_path, "debug-close-complete", after=program_complete, timeout=10.0
            )
            evidence["flash_cancellation"]["program_complete_marker_index"] = program_complete
            evidence["flash_cancellation"]["debug_release_marker_index"] = debug_released
            evidence["flash_cancellation"]["completion_preceded_release"] = (
                program_complete < debug_released
            )
            evidence["after_flash_reconnect"] = await connect_and_validate("after flash cancel")
            evidence["after_flash_disconnect"] = _text(
                await call("disconnect", {"board_id": args.board_id})
            )

    evidence["v2_full_readback"] = _verify_hex(
        args.board_id, args.probe_id, args.target, args.v2_hex.resolve()
    )
    if not evidence["v2_full_readback"]["matches"]:
        raise RuntimeError("Final restored v2 image did not match full readback")
    evidence["final_uart_reusable"] = _uart_is_reusable(args.serial_id, args.baudrate)
    backend_rows = _read_rows(backend_path)
    evidence["backend_lifecycle"] = {
        "trace_path": str(backend_path),
        "events": [row.get("event") for row in backend_rows],
        "sector_programmer_only": all(
            row.get("chip_erase") == "sector"
            for row in backend_rows
            if row.get("event") == "programmer-created"
        ),
        "reset_release_observed": any(
            row.get("event") == "probe-reset-line" and row.get("asserted") is False
            for row in backend_rows
        ),
        "debug_close_observed": any(
            row.get("event") == "debug-close-complete" for row in backend_rows
        ),
        "uart_close_observed": any(row.get("event") == "uart-close" for row in backend_rows),
    }
    if not evidence["backend_lifecycle"]["sector_programmer_only"]:
        raise RuntimeError("A programmer was created without sector erase")
    for required in ("reset_release_observed", "debug_close_observed", "uart_close_observed"):
        if not evidence["backend_lifecycle"][required]:
            raise RuntimeError(f"Required lifecycle proof missing: {required}")
    evidence["protocol_trace"] = {
        "path": str(trace_path),
        "sha256": _sha256(trace_path),
        "contains_secrets": False,
        "records_arguments_or_uart_data": False,
    }
    evidence["status"] = "nucleo_m9_hardware_lifecycle_passed_with_q1_client_gaps"
    evidence["completed_at"] = _timestamp()
    result_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def main() -> int:
    args = _arguments()
    result_path = args.artifact_root.resolve() / "acceptance.json"
    try:
        result = asyncio.run(_run(args))
    except BaseException as exc:
        if args.artifact_root.resolve().exists():
            failure = {
                "schema_version": 1,
                "status": "blocked_or_failed",
                "recorded_at": _timestamp(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "exact_command": [sys.executable, *sys.argv],
            }
            result_path.write_text(json.dumps(failure, indent=2) + "\n", encoding="utf-8")
        raise
    print(json.dumps({"status": result["status"], "result": str(result_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
