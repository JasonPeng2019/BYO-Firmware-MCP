"""Opt-in M05 public setup/restart proof with production workers and native leases.

No policy/profile/grant edits, test seams, flash/erase, UART or protection writes.
The only target write is a guarded scratch-RAM word, restored in finally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import traceback
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", choices=("nrf52840", "stm32l476"), required=True)
    parser.add_argument("--stage", choices=("discover", "setup", "restart"), required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--attempt", default="")
    args = parser.parse_args()
    args.invocation = args.invocation.resolve()
    args.evidence = args.evidence.resolve()
    args.evidence.mkdir(parents=True, exist_ok=True)
    project = args.evidence / (args.board + "-project")
    project.mkdir(exist_ok=True)
    os.environ.update(
        BYO_MCP_ARTIFACT_ROOT=str(project),
        BYO_MCP_MONITOR_ROOT=str(project / "monitor"),
        BYO_MANAGED_TEST_EVIDENCE=str(args.evidence / "managed"),
        PYOCD_PROJECT_DIR=str(project),
    )
    for name in ("BYO_MCP_TEST_SEAMS", "BYO_MCP_TEST_FAULTS"):
        os.environ.pop(name, None)
    # Normal user-facing pyOCD options; no pack/support, map, profile or authority is pre-staged.
    config = project / "pyocd.yaml"
    if not config.exists():
        config.write_text(
            json.dumps(
                {
                    "auto_unlock": False,
                    "resume_on_disconnect": False,
                    "connect_mode": "halt",
                    "jlink.non_interactive": True,
                }
            )
        )
    invocation = json.loads(args.invocation.read_text())
    runtime = (
        Path(
            json.loads(
                (args.invocation.parents[1] / "harness" / "harness-config.json").read_text()
            )["root_workspace"]
        )
        / ".harness-runtime"
    )
    uid = {"nrf52840": "683854191", "stm32l476": "066FFF514988525067233337"}[args.board]
    resource = f"hardware:{args.board}:{uid}"
    assert resource in invocation["exclusive_resources"]
    report = {
        "started_at": now(),
        "stage": args.stage,
        "board": args.board,
        "uid": uid,
        "pid": os.getpid(),
        "invocation": invocation,
        "project": str(project),
        "helper": {"path": __file__, "sha256": sha(__file__)},
        "calls": [],
        "leases": [],
        "workers": [],
        "verdict": "PENDING",
        "scope": "RAM-only partial LITE; no flash/recovery/application/UART claim. STM exact order code is user supplied; chip group/size/pack do not prove package.",
        "argv": sys.argv,
        "cwd": str(Path.cwd()),
        "native_session": os.environ.get("CODEX_SESSION_ID"),
    }
    suffix = "-" + args.attempt if args.attempt else ""
    destination = args.evidence / f"{args.board}-{args.stage}{suffix}.json"

    def save():
        destination.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    from pyocd_debug_mcp.kernel.processes import identity_matches, _start_token
    from tests.m05_public_support import NULL_PLAN, PublicSetup, child
    from tests.tiered_acceptance_support import public_text

    report["creation_time"] = _start_token(os.getpid())

    def audit():
        status = json.loads((args.invocation.parent / "controller.status.json").read_text())
        controller = status["controller_identity"]
        assert status["run_id"] == invocation["run_id"]
        assert status["controller_state"] == "running"
        native_birth = int(controller["creation_time"].split(":", 1)[1])
        assert identity_matches(controller["pid"], f"win:{native_birth:016x}")
        provider = status["provider_state"]
        provider_birth = int(provider["creation_time"].split(":", 1)[1])
        assert identity_matches(provider["pid"], f"win:{provider_birth:016x}")
        leases = [
            json.loads(p.read_text()) for p in (runtime / "resources" / "leases").glob("*.lease")
        ]
        owned = [
            next(lease for lease in leases if lease["resource_id"] == selected)
            for selected in invocation["exclusive_resources"]
        ]
        for lease in owned:
            assert (
                lease["run_id"] == invocation["run_id"]
                and lease["lane_id"] == invocation["lane_id"]
            )
            assert (
                lease["pid"] == controller["pid"]
                and lease["creation_time"] == controller["creation_time"]
            )
        report["leases"].append(
            {
                "at": now(),
                "leases": owned,
                "controller": controller,
                "provider": provider,
                "controller_alive": True,
                "provider_alive": True,
            }
        )

    audit()
    docs = args.execution / "vendor-documents"
    pdf = docs / ("nRF52840-PS-v1.1.pdf" if args.board == "nrf52840" else "STM32L476xx-DS10198.pdf")
    expected_pdf = {
        "nrf52840": "c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422",
        "stm32l476": "a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65",
    }[args.board]
    assert sha(pdf) == expected_pdf
    text_path = args.evidence / f"{args.board}-datasheet.txt"
    extract = subprocess.run(
        ["pdftotext", "-layout", str(pdf), str(text_path)], capture_output=True, text=True
    )
    report["document"] = {
        "path": str(pdf),
        "bytes": pdf.stat().st_size,
        "sha256": sha(pdf),
        "extract_argv": extract.args,
        "extract_exit": extract.returncode,
        "extract_stderr": extract.stderr,
    }
    assert extract.returncode == 0
    report["document"]["text_sha256"] = sha(text_path)
    pages = text_path.read_text(encoding="utf-8").split("\f")
    report["document"]["ram_pages"] = [
        {"pdf_page": i + 1, "text": p}
        for i, p in enumerate(pages)
        if ("0x2000" in p or "0x20000000" in p or "0x2001" in p or "0x2004" in p)
    ]
    if args.board == "stm32l476":
        pack = args.execution / "packs" / "Keil.STM32L4xx_DFP.3.1.0.pack"
        assert sha(pack) == "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
        with zipfile.ZipFile(pack) as archive:
            pdsc = archive.read("Keil.STM32L4xx_DFP.pdsc")
        assert (
            hashlib.sha256(pdsc).hexdigest()
            == "7493b22352571fc8a3efb2257d3f4c94dc932cee03ecf60ba76eb699ead1518c"
        )
        leaf = next(
            device
            for device in ET.fromstring(pdsc).iter("device")
            if device.get("Dname") == "STM32L476RGTx"
        )
        report["pack"] = {
            "path": str(pack),
            "sha256": sha(pack),
            "pdsc_sha256": hashlib.sha256(pdsc).hexdigest(),
            "device": "STM32L476RGTx",
            "exact_order": "STM32L476RGT6",
            "order_source": "ROOT/user-confirmed physical part; not inferred from chip group",
            "device_leaf_xml": ET.tostring(leaf, encoding="unicode"),
        }
    save()
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.adapters.swd_interface import session_metadata

    workers = {}
    previous_profile = threading.getprofile()

    def observe_worker(frame, event, arg):
        if event != "return" or frame.f_code.co_name not in {"__init__", "close"}:
            return
        instance = frame.f_locals.get("self")
        if type(instance).__name__ != "_WorkerClient" or not hasattr(instance, "_process"):
            return
        process = instance._process
        worker_key = (process.pid, getattr(process, "_m05_birth", None))
        token = _start_token(process.pid)
        if token is not None:
            worker_key = (process.pid, token)
        existing = next((k for k in workers if k[0] == process.pid), worker_key)
        entry = workers.setdefault(
            existing, {"pid": process.pid, "start_token": token, "observed_at": now()}
        )
        if frame.f_code.co_name == "close":
            entry.update(
                close_return_at=now(),
                close_exit_code=process.poll(),
                cleanup_confirmed=instance._cleanup_confirmed,
            )

    # Read-only Python call observation records actual production worker exits.
    # It does not replace a method, mutate authority, or alter a worker reply.
    threading.setprofile(observe_worker)

    def capture_workers():
        # Read production worker metadata, without changing dispatch/backend behavior.
        for path in project.rglob("*.json"):
            if "owned" not in str(path) and "process" not in str(path):
                continue
            try:
                payload = json.loads(path.read_text())
                if isinstance(payload, dict) and "pid" in payload and "start_token" in payload:
                    workers[(payload["pid"], payload["start_token"])] = payload
            except (OSError, ValueError):
                pass

    def call(name, arguments):
        audit()
        item = {"tool": name, "arguments": arguments, "started_at": now()}
        report["calls"].append(item)
        save()
        try:
            result = public_text(server, name, arguments)
            item["raw_reply"] = result
            return result
        except Exception as exc:
            item["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            item["finished_at"] = now()
            capture_workers()
            save()

    flow = PublicSetup(server, call=call)
    board = None
    original = None
    restored = False
    write_attempted = False
    scratch = 0x20001000
    try:
        overview = flow.overview("M05 physical " + args.board)
        if overview["status"] == "setup_assignment_required":
            chosen = next(
                row
                for row in overview["connections"]
                if row["connection_id"].rsplit(":", 1)[-1].casefold() == uid.casefold()
            )
            overview = flow.json(
                "setup_overview",
                {
                    "board_names": ["M05 physical " + args.board],
                    "connection_assignments": {
                        "M05 physical " + args.board: chosen["connection_id"]
                    },
                },
            )
        assert overview["status"] == "setup_routes_ready", overview
        route = overview["routes"][0]
        report["route"] = route
        board = route["board_id"]
        if args.stage == "discover":
            report["verdict"] = "DISCOVERY_ONLY"
            return 0
        part = "nRF52840-QIAA" if args.board == "nrf52840" else "STM32L476RGT6"
        target = "nrf52840" if args.board == "nrf52840" else "stm32l476rgtx"
        if args.stage == "setup":
            assert not list((project / ".firm" / "boards").glob("*")), "fresh project required"
            if args.board == "nrf52840":
                call("connect", {"board_id": board, "probe_uid": uid, "target": target})
            _, plan = flow.accept(route, pdf, part)
            pending = flow.start(plan)
            assert pending["status"] == "setup_research_required", pending
            if args.board == "nrf52840":
                response = flow.target_answer(pending, target)
            else:
                response = dict(pending["accepted_response"]["response"])
                response.update(
                    pack_id="Keil.STM32L4xx_DFP",
                    version="3.1.0",
                    filename=pack.name,
                    url="https://www.keil.com/pack/Keil.STM32L4xx_DFP.3.1.0.pack",
                    source_path=str(pack),
                    official_sha256=sha(pack),
                    evidence=[
                        {
                            "source": "Keil published STM32L4xx_DFP 3.1.0 PDSC",
                            "claim": "STM32L476RGTx supports supplied STM32L476RGT6; SRAM1 starts at 0x20000000.",
                        }
                    ],
                    reasoning_summary="Pinned official pack leaf and user-supplied order code agree; core identity alone does not prove package.",
                )
            assert flow.answer(board, pending, response)["status"] == "setup_continuation_accepted"
            pending = flow.fix(plan)
            assert pending["first_unverified_phase"] == "safety_research", pending
            # Only a 4-KiB subrange of actual SRAM is confirmed, never all device geometry.
            ram_pages = [p["pdf_page"] for p in report["document"]["ram_pages"]]
            assert ram_pages, "independently inspect source RAM evidence before confirmation"
            response = flow.ram_answer(
                pending,
                start=0x20001000,
                end=0x20002000,
                pages=ram_pages,
                note="Documented SRAM subrange only; unknown flash/recovery omitted. See retained datasheet page extraction.",
            )
            assert flow.answer(board, pending, response)["accepted"] == "lite_confirmation"
            completed = flow.fix(plan)
            assert completed["status"] == "setup_completed", completed
            report["completion"] = completed
            report["setup_plan"] = plan
        # Every process independently reconnects and validates through the public route.
        overview = flow.overview("M05 physical " + args.board)
        route = overview["routes"][0]
        assert route["route"] == "lite", route
        load = route["load_call"]
        loaded = flow.json(load["tool"], load["arguments"])
        assert loaded["next_call"] == route["next_call"], (loaded, route)
        report["validation_load"] = loaded
        validation = flow.json(loaded["next_call"]["tool"], loaded["next_call"]["arguments"])
        assert validation["status"] == "validation_passed", validation
        report["validation"] = validation
        connection = server.connection_manager.maybe_connection(board)
        report["connection_metadata"] = asdict(session_metadata(connection.handle))
        assert session_metadata(connection.handle).probe_uid.casefold() == uid.casefold()
        report["session_id"] = server._active_session_id(board)
        observed = validation["observed"]
        assert validation["code"] == "validation/lite-binding-checked"
        assert observed["tier"] == "setup-lite" and observed["map_kind"] == "partial"
        assert (
            observed["identity_assertion"] == "trusted-not-proven"
            and observed["identity_capability"] is None
        )
        assert observed["server_run_id"] == server.server_run.run_id
        assert observed["connection_id"] == connection.connection_id
        assert observed["session_id"] == report["session_id"]
        assert observed["probe_id"] == route["next_call"]["arguments"]["probe_id"]
        assert (
            observed["target"].casefold()
            == session_metadata(connection.handle).target_override.casefold()
            == target
        )
        assert observed["policy_digest"] == server._capability_policies.resolve(board).policy_digest
        assert server.gate_manager.live_identity(board) is None
        assert server.plan_engine.active_plan("board_setup", board) is None
        assert server.setup_tool_loader.allowance_for(board) is None
        assert server.permission_store.active_grant("board_setup", board) is None
        identity = {}
        identity_addresses = [("cpuid", 0xE000ED00, 32)]
        if args.board == "nrf52840":
            identity_addresses += [
                ("part", 0x10000100, 32),
                ("variant", 0x10000104, 32),
                ("package", 0x10000108, 32),
            ]
        else:
            identity_addresses += [("group", 0xE0042000, 32), ("flash_kib", 0x1FFF75E0, 16)]
        for key, address, width in identity_addresses:
            result = flow.json(
                "read_memory_raw", {"board_id": board, "address": address, "width": width}
            )
            assert result["status"] == "ok", result
            identity[key] = result
        report["live_identity"] = identity
        if args.board == "nrf52840":
            assert int(identity["part"]["result"], 0) == 0x52840
        else:
            assert int(identity["group"]["result"], 0) & 0xFFF == 0x415
            assert int(identity["flash_kib"]["result"], 0) == 1024
        capabilities = flow.json("get_capabilities", {"board_id": board})
        assert capabilities["tier"] == "setup-lite", capabilities
        assert capabilities["identity"]["assertion"] == "trusted-not-proven"
        families = {row["family"]: row for row in capabilities["capabilities"]}
        for family in (
            "identity-validation",
            "flash-application",
            "flash-bootloader",
            "target-recovery",
        ):
            assert not families[family]["safe"], families[family]
        assert families["target-recovery"]["recovery_mode"] == "unavailable"
        report["capabilities"] = capabilities
        # Capture the public guarded read/write schemas before using the plans.
        for action in ("read_memory_address", "write_memory"):
            call(action + "-plan", NULL_PLAN)

        def guarded(action, parameters):
            plan = flow.plan(action, board, parameters)
            assert plan["status"] == "plan_accepted", plan
            batch = flow.execute(plan["stable_client_fallback"])
            assert batch["status"] == "batch_completed", batch
            result = child(batch)
            assert result["status"] == "ok", result
            return result

        call("halt", {"board_id": board})
        original = guarded("read_memory_address", {"address": scratch, "width": 32, "length": None})
        report["ram_original"] = original
        # The reply shape is checked from actual public output before any target write.
        value = original.get("result")
        assert isinstance(value, (str, int)), original
        original_value = int(value, 0) if isinstance(value, str) else value
        write_args = {
            "symbol_or_address": scratch,
            "width": 32,
            "allow_address_fallback": True,
            "reason": "Recoverable M05 scratch RAM test; restore exact saved word",
            "elf_artifact": None,
        }
        report["ram_saved_value"] = original_value
        (args.evidence / f"{args.board}-{args.stage}{suffix}-scratch-original.bin").write_bytes(
            original_value.to_bytes(4, "little")
        )
        # Denied requests run before any permitted target write. No flash or
        # recovery is attempted; their capability limit is reported above.
        report["denied_boundaries"] = []
        for action, parameters in (
            ("read_memory_address", {"address": 0x20002000, "width": 32, "length": None}),
            ("read_memory_address", {"address": 0x20001FFE, "width": 32, "length": None}),
            ("write_memory", {**write_args, "symbol_or_address": 0x20002000, "value": 0xCAFE4321}),
            ("write_memory", {**write_args, "symbol_or_address": 0x20001FFE, "value": 0xCAFE4321}),
            ("write_memory", {**write_args, "symbol_or_address": 0, "value": 0xCAFE4321}),
        ):
            denied_plan = flow.plan(action, board, parameters)
            assert denied_plan["status"] == "plan_accepted", denied_plan
            denied = flow.execute(denied_plan["stable_client_fallback"])
            assert denied["status"] == "batch_failed" and child(denied)["status"] == "refused", (
                denied
            )
            report["denied_boundaries"].append(
                {"action": action, "parameters": parameters, "result": denied}
            )
        assert (
            guarded("read_memory_address", {"address": scratch, "width": 32, "length": None})[
                "result"
            ]
            == value
        )
        try:
            write_attempted = True
            guarded("write_memory", {**write_args, "value": original_value ^ 0x5A35C69B})
            report["ram_changed"] = guarded(
                "read_memory_address", {"address": scratch, "width": 32, "length": None}
            )
            assert int(report["ram_changed"]["result"], 0) == original_value ^ 0x5A35C69B
        finally:
            guarded("write_memory", {**write_args, "value": original_value})
            report["ram_restored"] = guarded(
                "read_memory_address", {"address": scratch, "width": 32, "length": None}
            )
            restored = report["ram_restored"]["result"] == original["result"]
            (args.evidence / f"{args.board}-{args.stage}{suffix}-scratch-restored.bin").write_bytes(
                int(report["ram_restored"]["result"], 0).to_bytes(4, "little")
            )
            assert restored
        report["verdict"] = "PASS"
        return 0
    except Exception:
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr)
        report["verdict"] = "FAIL"
        return 1
    finally:
        report["ram_restoration"] = {"write_attempted": write_attempted, "restored": restored}
        if board is not None and server.connection_manager.maybe_connection(board) is not None:
            try:
                call("resume", {"board_id": board})
                report["final_state"] = call("get_state", {"board_id": board})
            except Exception:
                report["final_state_error"] = traceback.format_exc()
            try:
                call("disconnect", {"board_id": board})
            except Exception:
                report["disconnect_error"] = traceback.format_exc()
        threading.setprofile(previous_profile)
        report["workers"] = [
            {
                **worker,
                "identity_absent_after_disconnect": token is not None
                and not identity_matches(pid, token),
            }
            for (pid, token), worker in workers.items()
        ]
        report["connected_after_finally"] = (
            board is not None and server.connection_manager.maybe_connection(board) is not None
        )
        report["imports"] = [
            {
                "module": name,
                "path": str(Path(module.__file__).resolve()),
                "sha256": sha(module.__file__),
            }
            for name, module in sorted(sys.modules.items())
            if name.startswith(("pyocd_debug_mcp", "tests."))
            and getattr(module, "__file__", None)
            and Path(module.__file__).is_file()
        ]
        report["finished_at"] = now()
        save()


if __name__ == "__main__":
    sys.exit(main())
