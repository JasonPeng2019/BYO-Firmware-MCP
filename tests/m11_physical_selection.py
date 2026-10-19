"""Opt-in M11 public production-worker evidence under both native leases.

No programming or erase success is required or inferred. The only flash call
is the required negative generic-target request. Readback covers that artifact's
addressed bytes, not an entire chip. An unexpected success remains a failure
even if those bytes are unchanged. Each open/disconnect uses registered MCP.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import traceback
import zipfile

from elftools.elf.elffile import ELFFile
import psutil

from tests.tiered_acceptance_support import public_text


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", choices=("nrf52840", "stm32l476"), required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    uid = {"nrf52840": "683854191", "stm32l476": "066FFF514988525067233337"}[args.board]
    required = {"hardware:nrf52840:683854191", "hardware:stm32l476:066FFF514988525067233337"}
    invocation = json.loads(args.invocation.read_text(encoding="utf-8"))
    assert required.issubset(invocation["exclusive_resources"]), "both exact native leases required"
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "board": args.board,
        "uid": uid,
        "started_at": now(),
        "verdict": "PENDING",
        "invocation": invocation,
        "invocation_sha256": digest(args.invocation.read_bytes()),
        "calls": [],
        "workers": [],
        "cleanup_errors": [],
        "qualification": "Live chip group/geometry is separate from selected target/package. "
        "Addressed-byte equality does not establish whole-chip preservation or zero bus writes. "
        "No physical wrong-board mutation or successful erase/flash is claimed.",
    }
    script_bytes = Path(__file__).read_bytes()
    script_snapshot = args.output.with_suffix(".script.py")
    script_snapshot.write_bytes(script_bytes)
    report["executed_helper"] = {
        "path": str(Path(__file__).resolve()),
        "snapshot": str(script_snapshot),
        "sha256": digest(script_bytes),
    }
    config_path = Path(__file__).resolve().parents[3] / "harness" / "harness-config.json"
    native_root = Path(json.loads(config_path.read_text(encoding="utf-8"))["root_workspace"])
    leases = []
    for path in (native_root / ".harness-runtime" / "resources" / "leases").glob("*.lease"):
        lease = json.loads(path.read_text(encoding="utf-8"))
        if lease.get("run_id") == invocation["run_id"]:
            leases.append(lease | {"file_sha256": digest(path.read_bytes())})
    assert required.issubset({lease["resource_id"] for lease in leases})
    report["fresh_native_leases"] = leases

    def save():
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    negative = args.execution / "firmware" / "m02-distinct" / args.board / "fixture.elf"
    manifest_path = negative.with_name("fixture-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report["negative_artifact"] = {
        "path": str(negative),
        "sha256": digest(negative.read_bytes()),
        "manifest_sha256": digest(manifest_path.read_bytes()),
    }
    with negative.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [
            (int(s["p_paddr"]), s.data())
            for s in elf.iter_segments()
            if s["p_type"] == "PT_LOAD" and int(s["p_filesz"]) > 0
        ]
    assert [
        {"start": start, "size": len(data), "sha256": digest(data)} for start, data in segments
    ] == manifest["segments"]
    report["addressed_segments"] = manifest["segments"]
    target = "nrf52840" if args.board == "nrf52840" else "stm32l476rgtx"
    workers = []
    prior_cwd = Path.cwd()
    server = None
    safe_resume = False
    with tempfile.TemporaryDirectory(prefix="m11-live-") as temporary:
        root = Path(temporary)
        report["project_root"] = str(root)
        config = {
            "resume_on_disconnect": False,
            "auto_unlock": False,
            "connect_mode": "halt",
            "jlink.non_interactive": True,
        }
        if args.board == "stm32l476":
            cache_path = args.execution / "packs" / "stm32-pack-cache.json"
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            pack = args.execution / "packs" / "Keil.STM32L4xx_DFP.3.1.0.pack"
            assert digest(pack.read_bytes()) == cache["sha256"]
            with zipfile.ZipFile(pack) as archive:
                pdsc = archive.read(cache["pdsc_name"])
            assert digest(pdsc) == cache["pdsc_sha256"]
            assert cache["pdsc_device"] == "STM32L476RGTx"
            config["pack"] = [str(pack)]
            # Normal project-local pinned pack registry, derived from retained
            # vendor bytes/provenance. It supplies pack origin, not board policy.
            pack_root = root / ".firm" / "packs"
            (pack_root / "files").mkdir(parents=True)
            shutil.copyfile(pack, pack_root / "files" / pack.name)
            registry = {
                "packs": [
                    {
                        "id": "Keil.STM32L4xx_DFP",
                        "version": "3.1.0",
                        "filename": pack.name,
                        "url": cache["publisher_download"],
                        "sha256": cache["sha256"],
                        "provides_targets": [target],
                    }
                ]
            }
            (pack_root / "manifest.yaml").write_text(json.dumps(registry), encoding="utf-8")
            report["project_pack_registry"] = registry
            report["pack_provenance"] = cache | {
                "fresh_file_sha256": digest(pack.read_bytes()),
                "fresh_pdsc_sha256": digest(pdsc),
            }
        (root / "pyocd.yaml").write_text(json.dumps(config), encoding="utf-8")
        report["provider_config"] = config
        os.environ.update(
            BYO_MCP_ARTIFACT_ROOT=str(root),
            BYO_MCP_MONITOR_ROOT=str(root / "monitor"),
            PYOCD_PROJECT_DIR=str(root),
        )
        os.environ.pop("BYO_MCP_TEST_SEAMS", None)
        os.environ.pop("BYO_MCP_TEST_FAULTS", None)
        os.chdir(root)
        try:
            server = importlib.import_module("pyocd_debug_mcp.server")
            report["imports"] = {
                "server": str(Path(server.__file__).resolve()),
                "server_sha256": digest(Path(server.__file__).read_bytes()),
            }

            def call(tool, arguments, *, structured=False, expected_error=False):
                row = {"tool": tool, "arguments": arguments, "at": now()}
                report["calls"].append(row)
                try:
                    result = public_text(server, tool, arguments)
                    row["result"] = json.loads(result) if structured else result
                except Exception as error:
                    row["error"] = f"{type(error).__name__}: {error}"
                    if not expected_error:
                        raise
                finally:
                    row["completed_at"] = now()
                    save()
                return row.get("result")

            def connect(selected_target):
                result = call(
                    "connect", {"board_id": args.board, "probe_uid": uid, "target": selected_target}
                )
                assert "Connected" in result, result
                connection = server.connection_manager.connection_for(args.board)
                worker = connection.handle.worker
                assert worker is not None and worker._process.poll() is None
                assert connection.handle.metadata.probe_uid == uid
                assert connection.handle.metadata.target_override == selected_target
                workers.append(worker)
                report["workers"].append(
                    {
                        "target": selected_target,
                        "pid": worker._process.pid,
                        "creation_time": psutil.Process(worker._process.pid).create_time(),
                        "metadata": asdict(connection.handle.metadata),
                        "connection_id": connection.connection_id,
                        "session_id": connection.runtime_session.session_id,
                    }
                )
                assert "halted" in call("get_state", {"board_id": args.board}).lower()
                return connection

            def disconnect():
                if server.connection_manager.maybe_connection(args.board) is not None:
                    call("disconnect", {"board_id": args.board})
                assert server.connection_manager.maybe_connection(args.board) is None

            def read(address, width=32):
                reply = call(
                    "read_memory_raw",
                    {"board_id": args.board, "address": address, "width": width},
                    structured=True,
                )
                assert reply["status"] == "ok", reply
                return int(reply["result"], 16)

            def addressed(phase):
                snapshots = []
                for index, (start, data) in enumerate(segments):
                    actual = bytearray()
                    for offset in range(0, len(data), 4096):
                        reply = call(
                            "read_memory_raw",
                            {
                                "board_id": args.board,
                                "address": start + offset,
                                "length": min(4096, len(data) - offset),
                            },
                            structured=True,
                        )
                        assert reply["status"] == "ok", reply
                        actual.extend(bytes.fromhex(reply["result"]))
                    assert len(actual) == len(data)
                    output = args.output.with_name(f"{args.output.stem}-{phase}-{index}.bin")
                    output.write_bytes(actual)
                    snapshots.append(
                        {
                            "start": start,
                            "size": len(actual),
                            "sha256": digest(actual),
                            "path": str(output),
                            "matches_requested_image": bytes(actual) == data,
                        }
                    )
                report[phase] = snapshots
                save()
                return [s["sha256"] for s in snapshots]

            def identity():
                observed = {"cpuid": read(0xE000ED00)}
                if args.board == "nrf52840":
                    observed.update(
                        part=read(0x10000100), variant=read(0x10000104), package=read(0x10000108)
                    )
                    assert observed["part"] == 0x52840
                else:
                    observed.update(
                        dbgmcu_idcode=read(0xE0042000), flash_size_kib=read(0x1FFF75E0, 16)
                    )
                    assert observed["dbgmcu_idcode"] & 0xFFF == 0x415
                    assert observed["flash_size_kib"] == 1024
                return observed

            overview = call("setup_overview", {}, structured=True)
            report["public_inventory"] = overview
            assert any(
                uid.casefold() in row["connection_id"].casefold() for row in overview["connections"]
            )
            # These negative public calls must not create a production worker.
            for selector in (None, "null", "mistyped-" + uid):
                assert (
                    call(
                        "connect",
                        {"board_id": "unassigned", "probe_uid": selector, "target": "cortex_m"},
                        expected_error=True,
                    )
                    is None
                )
                assert server.connection_manager.maybe_connection("unassigned") is None
            connect(target)
            report["fresh_identity"] = identity()
            exact_before = addressed("exact-before")
            assert not all(s["matches_requested_image"] for s in report["exact-before"]), (
                "negative image must differ from current addressed bytes"
            )
            disconnect()
            generic = connect("cortex_m")
            caps = call("get_capabilities", {"board_id": args.board}, structured=True)
            assert caps["identity"] == {"assertion": "not-asserted", "capability": None}
            generic_before = addressed("generic-before")
            assert generic_before == exact_before
            report["state_before_negative"] = call("get_state", {"board_id": args.board})
            reply = call(
                "flash_raw", {"board_id": args.board, "artifact": str(negative)}, structured=True
            )
            report["generic_refusal_satisfied"] = reply.get("status") in {"error", "refused"}
            report["negative_reply"] = reply
            report["same_session_after_negative"] = (
                server.connection_manager.maybe_connection(args.board) is generic
            )
            report["state_after_negative"] = call("get_state", {"board_id": args.board})
            addressed("generic-after")
            disconnect()
            connect(target)
            report["reconnected_identity"] = identity()
            exact_after = addressed("exact-after")
            report["addressed_bytes_unchanged_after_reconnect"] = exact_before == exact_after
            assert report["fresh_identity"] == report["reconnected_identity"]
            assert exact_before == exact_after, (
                "negative flash request changed addressed physical bytes"
            )
            safe_resume = True
            report["selection_workflow"] = "PASS"
            report["verdict"] = "PASS" if report["generic_refusal_satisfied"] else "PENDING_M02"
        except Exception as error:
            report["obstacle"] = f"{type(error).__name__}: {error}"
            report["traceback"] = traceback.format_exc()
        finally:
            if server is not None:
                if server.connection_manager.maybe_connection(args.board) is not None:
                    try:
                        report["final_state_before_cleanup"] = call(
                            "get_state", {"board_id": args.board}
                        )
                        if safe_resume:
                            call("resume", {"board_id": args.board})
                            report["final_observed_state"] = call(
                                "get_state", {"board_id": args.board}
                            )
                    except Exception as error:
                        report["cleanup_errors"].append(f"state/resume: {error}")
                    finally:
                        try:
                            call("disconnect", {"board_id": args.board})
                        except Exception as error:
                            report["cleanup_errors"].append(f"disconnect: {error}")
                report["disconnected"] = (
                    server.connection_manager.maybe_connection(args.board) is None
                )
            for worker, row in zip(workers, report["workers"]):
                row.update(
                    exit_code=worker._process.poll(),
                    cleanup_confirmed=worker._cleanup_confirmed,
                    pid_exists=psutil.pid_exists(worker._process.pid),
                )
            report["cleanup_proven"] = (
                report.get("disconnected") is True
                and not report["cleanup_errors"]
                and all(
                    row["exit_code"] is not None
                    and row["cleanup_confirmed"]
                    and not row["pid_exists"]
                    for row in report["workers"]
                )
            )
            os.chdir(prior_cwd)
            report["completed_at"] = now()
            report["raw_exit"] = (
                0 if report["verdict"] == "PASS" and report["cleanup_proven"] else 1
            )
            save()
    print(
        json.dumps(
            {
                k: report.get(k)
                for k in (
                    "board",
                    "verdict",
                    "selection_workflow",
                    "generic_refusal_satisfied",
                    "cleanup_proven",
                    "obstacle",
                    "raw_exit",
                )
            }
        )
    )
    return report["raw_exit"]


if __name__ == "__main__":
    raise SystemExit(main())
