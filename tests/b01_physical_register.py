"""Explicit opt-in B01 physical proof; no target test seams, grants or writes.

Run only with the two native exclusive probe resources reserved. This script
uses fresh temporary projects, production workers and public MCP dispatch.
HALT is allowed by this assignment; no valid register mutation/flash/erase occurs.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from tests.tiered_acceptance_support import public_call, public_text


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "started_at": now(),
        "qualification": (
            "Raw explicit target intent; chip/core evidence is recorded separately. "
            "CPUID and backend target names do not establish exact order code/package. "
            "No physical write tracing is claimed; deterministic journal tests prove zero register writes."
        ),
        "boards": [],
    }
    for board, uid, target, addresses in (
        ("nrf52840", "683854191", "nrf52840", (0xE000ED00, 0x10000100, 0x10000104, 0x10000108)),
        ("stm32l476", "066FFF514988525067233337", "cortex_m", (0xE000ED00, 0xE0042000)),
    ):
        record = {
            "board": board,
            "resource": f"hardware:{board}:{uid}",
            "probe_uid": uid,
            "explicit_target": target,
            "started_at": now(),
            "calls": [],
            "verdict": "PENDING",
        }
        report["boards"].append(record)
        with tempfile.TemporaryDirectory(prefix=f"b01-physical-{board}-") as directory:
            root = Path(directory)
            os.environ["BYO_MCP_ARTIFACT_ROOT"] = str(root)
            os.environ["BYO_MCP_MONITOR_ROOT"] = str(root / "monitor")
            os.environ.pop("BYO_MCP_TEST_SEAMS", None)
            os.environ.pop("BYO_MCP_TEST_FAULTS", None)
            from pyocd_debug_mcp.monitor import paths

            paths._reset_cache()
            from pyocd_debug_mcp import server as server_module

            server = importlib.reload(server_module)

            def call(tool, arguments, *, structured=False):
                row = {"tool": tool, "arguments": arguments, "at": now()}
                record["calls"].append(row)
                try:
                    row["result"] = (public_call if structured else public_text)(
                        server, tool, arguments
                    )
                except Exception as error:
                    row["error"] = f"{type(error).__name__}: {error}"
                    raise
                return row["result"]

            try:
                # Real inventory before connection; no backend or discovery seam.
                inventory = server._hardware_inventory.snapshot()
                record["inventory"] = asdict(inventory)
                assert any(row.unique_id == uid for row in inventory.probes), (
                    "reserved probe absent from live inventory"
                )
                call("connect", {"board_id": board, "probe_uid": uid, "target": target})
                connection = server.connection_manager.connection_for(board)
                handle = connection.handle
                assert handle.metadata.probe_uid == uid
                assert handle.worker is not None and handle.worker._process.poll() is None
                record["session"] = {
                    "connection_id": connection.connection_id,
                    "session_id": connection.runtime_session.session_id,
                    "metadata": asdict(handle.metadata),
                    "worker_pid": handle.worker._process.pid,
                }
                for address in addresses:
                    reply = call(
                        "read_memory_raw",
                        {"board_id": board, "address": hex(address)},
                        structured=True,
                    )
                    assert reply["status"] == "ok", reply
                    value = int(reply["result"], 16)
                    if address == 0x10000100:
                        assert value == 0x52840, "live Nordic PART does not match nRF52840"
                    if address == 0xE0042000:
                        assert value & 0xFFF == 0x415, (
                            "live STM device ID does not match L47x/L48x group"
                        )
                state = call("get_state", {"board_id": board})
                if "halted" not in state.lower():
                    call("halt", {"board_id": board})
                for tool, name in (
                    ("write_cpu_register_raw", "r0"),
                    ("set_execution_state_raw", "xpsr"),
                ):
                    refused = call(
                        tool, {"board_id": board, "name": name, "value": -1}, structured=True
                    )
                    assert (
                        refused["status"] == "error" and refused["code"] == "input/invalid-argument"
                    ), refused
                    assert server.connection_manager.connection_for(board) is connection
                    assert connection.runtime_session.closed_at is None
                    assert handle.worker._process.poll() is None
                    result = call("read_cpu_register", {"board_id": board, "name": "r0"})
                    assert "0x" in result and "Refused" not in result, result
                    assert server.connection_manager.connection_for(board) is connection
                record["session_after"] = {
                    "connection_id": connection.connection_id,
                    "session_id": connection.runtime_session.session_id,
                    "metadata": asdict(handle.metadata),
                    "worker_pid": handle.worker._process.pid,
                }
                assert record["session_after"] == record["session"]
                record["verdict"] = "PASS"
            except Exception as error:
                record["obstacle"] = f"{type(error).__name__}: {error}"
            finally:
                try:
                    call("disconnect", {"board_id": board})
                    assert server.connection_manager.maybe_connection(board) is None
                    record["disconnected"] = True
                except Exception as error:
                    record["disconnect_error"] = f"{type(error).__name__}: {error}"
                    record["verdict"] = "PENDING"
                record["completed_at"] = now()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(record, default=str), flush=True)
    report["completed_at"] = now()
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return 0 if all(row["verdict"] == "PASS" for row in report["boards"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
