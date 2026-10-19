"""Opt-in R11 STM remote outage/local override through genuine public FULL setup.

No source, profile, policy, validation or transport implementation is replaced.
No erase, programming, protection changes or target memory writes are requested.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
import zipfile
import xml.etree.ElementTree as ET

import yaml
import psutil


UID = "066FFF514988525067233337"
TARGET = "stm32l476rgtx"
NAME = "M12 owned STM remote recovery"
REQUIRED = {"hardware:nrf52840:683854191", "hardware:stm32l476:" + UID}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--resume-project", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--normal-profile-reference", type=Path)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    project = args.resume_project.resolve() if args.resume_project else evidence / "project"
    if args.resume_project:
        assert project.is_relative_to(evidence.parent.parent)
        assert (project / ".firm").is_dir(), "resume only an existing genuine public project"
    else:
        project.mkdir()
    os.environ.update(
        BYO_MCP_ARTIFACT_ROOT=str(project),
        BYO_MCP_MONITOR_ROOT=str(evidence / "monitor"),
        BYO_MANAGED_TEST_EVIDENCE=str(evidence / "managed"),
        PYOCD_PROJECT_DIR=str(project),
        PYOCD_MCP_RUNS_ROOT=str(evidence / "runs"),
        PYTHONIOENCODING="utf-8",
    )
    for variable in (
        "BYO_MCP_TEST_SEAMS",
        "BYO_MCP_TEST_FAULTS",
        "PYOCD_PROBE_UID",
        "PYOCD_TARGET",
    ):
        os.environ.pop(variable, None)
    invocation = json.loads(args.invocation.read_text(encoding="utf-8"))
    original = Path(
        json.loads((Path(invocation["cwd"]) / "harness/harness-config.json").read_text())[
            "root_workspace"
        ]
    )
    report = dict(
        started_at=now(),
        verdict="PENDING",
        argv=sys.argv,
        invocation=invocation,
        invocation_sha256=sha(args.invocation),
        calls=[],
        leases=[],
        workers=[],
        cleanup=[],
        project=str(project),
        helper_sha256=sha(__file__),
        qualification="STM order code is supplied by the user. Fresh live DBGMCU device group and flash size are independent chip facts; CPUID is core evidence. These observations do not prove exact package or chip unique ID. No flash/application/preservation claim.",
    )
    destination = evidence / "physical.json"

    def save():
        destination.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    from pyocd_debug_mcp.kernel.processes import (
        ProcessMarkerStore,
        _start_token,
        identity_matches,
        popen_owned,
        terminate_process_group,
    )
    from tests.m02_physical_flash import creation
    from tests.m12_process_evidence import ChildExits

    def leases():
        status = json.loads(Path(invocation["paths"]["controller_status"]).read_text())
        assert status["run_id"] == invocation["run_id"] and status["controller_state"] == "running"
        controller = status["controller_identity"]
        provider = status["provider_state"]
        assert creation(controller["pid"]) == controller["creation_time"]
        assert creation(provider["pid"]) == provider["creation_time"]
        assert set(invocation["exclusive_resources"]) == REQUIRED
        owned = []
        for path in (original / ".harness-runtime/resources/leases").glob("*.lease"):
            record = json.loads(path.read_text())
            if record["resource_id"] in REQUIRED:
                assert (
                    record["run_id"] == invocation["run_id"]
                    and record["lane_id"] == invocation["lane_id"]
                )
                assert (
                    record["pid"] == controller["pid"]
                    and record["creation_time"] == controller["creation_time"]
                )
                owned.append(record | {"file_sha256": sha(path)})
        assert {r["resource_id"] for r in owned} == REQUIRED
        report["leases"].append(
            dict(at=now(), controller=controller, provider=provider, records=owned)
        )
        save()

    leases()  # Before importing discovery, starting the endpoint or any target I/O.
    if args.resume_project:
        with zipfile.ZipFile(
            evidence / "resumed-project-before.zip", "w", zipfile.ZIP_DEFLATED
        ) as retained:
            for path in sorted(project.rglob("*")):
                if path.is_file():
                    retained.write(path, path.relative_to(project))
        report["resumed_project_input"] = dict(
            path=str(evidence / "resumed-project-before.zip"),
            sha256=sha(evidence / "resumed-project-before.zip"),
        )
    pack = args.execution / "packs/Keil.STM32L4xx_DFP.3.1.0.pack"
    cache = json.loads((args.execution / "packs/stm32-pack-cache.json").read_text())
    assert (
        sha(pack)
        == cache["sha256"]
        == "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
    )
    with zipfile.ZipFile(pack) as archive:
        pdsc = archive.read("Keil.STM32L4xx_DFP.pdsc")
    assert hashlib.sha256(pdsc).hexdigest() == cache["pdsc_sha256"]
    device = next(
        e for e in ET.fromstring(pdsc).iter("device") if e.get("Dname") == "STM32L476RGTx"
    )
    report["pack"] = dict(
        path=str(pack),
        sha256=sha(pack),
        pdsc_sha256=hashlib.sha256(pdsc).hexdigest(),
        device=device.attrib,
        memory=[e.attrib for e in device.findall("memory")],
        algorithms=[e.attrib for e in device.findall("algorithm")],
        provenance=cache,
    )
    from pyocd.target.pack.cmsis_pack import CmsisPack

    exact_device = next(
        d
        for d in CmsisPack(io.BytesIO(pack.read_bytes())).devices
        if d.part_number == "STM32L476RGTx"
    )
    assert any(
        r.start == 0x08000000 and r.length == 0x100000
        for r in exact_device.memory_map
        if r.is_flash
    )
    assert any(
        r.start == 0x20000000 and r.length == 0x18000 for r in exact_device.memory_map if r.is_ram
    )
    pdf = args.execution / "vendor-documents/STM32L476xx-DS10198.pdf"
    assert sha(pdf) == "a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65"
    report["datasheet"] = dict(path=str(pdf), sha256=sha(pdf))
    if not (project / "pyocd.yaml").exists():
        (project / "pyocd.yaml").write_text(
            json.dumps(
                {
                    "auto_unlock": False,
                    "resume_on_disconnect": False,
                    "connect_mode": "halt",
                    "jlink.non_interactive": True,
                }
            ),
            encoding="utf-8",
        )
    report["provider_config"] = json.loads((project / "pyocd.yaml").read_text())
    prior_cwd = Path.cwd()
    os.chdir(project)
    endpoint = None
    endpoint_token = None
    endpoint_marker = None
    child_exits = ChildExits()
    server = None
    registered = False
    board = None
    profile = None
    original_profile = None
    workers = []
    observed_workers = set()
    previous_profile = threading.getprofile()
    stdout = (evidence / "endpoint.stdout.log").open("wb")
    stderr = (evidence / "endpoint.stderr.log").open("wb")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", args.port or 0))
        port = reserved.getsockname()[1]
    selector = f"remote:127.0.0.1:{port}"

    def listening():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            return False

    def stop_endpoint():
        if endpoint is None:
            return
        if endpoint.poll() is None:
            assert identity_matches(endpoint.pid, endpoint_token), "endpoint PID creation changed"
            leases()
        assert terminate_process_group(endpoint, grace_seconds=10), (
            "owned endpoint Job cleanup failed"
        )
        ProcessMarkerStore().remove(endpoint_marker)
        assert endpoint.returncode is not None
        assert not identity_matches(endpoint.pid, endpoint_token)
        listener_deadline = time.monotonic() + 10
        while listening() or any(
            c.status == psutil.CONN_LISTEN and c.laddr.port == port
            for c in psutil.net_connections(kind="tcp")
        ):
            assert time.monotonic() < listener_deadline, "owned endpoint listener remains"
            time.sleep(0.1)
        report["endpoint_stopped"] = dict(
            at=now(), raw_exit=endpoint.returncode, exact_identity_absent=True, listener_absent=True
        )
        save()

    def preserve():
        if profile is not None:
            assert profile.read_bytes() == original_profile, "normal profile bytes changed"
            report.setdefault("profile_comparisons", []).append(
                dict(at=now(), sha256=sha(profile), unchanged=True)
            )

    try:
        server = importlib.import_module("pyocd_debug_mcp.server")
        if args.normal_profile_reference:
            original_profile = args.normal_profile_reference.read_bytes()
            reference = yaml.safe_load(original_profile)
            profile = server._profile_repository.load(reference["board_id"]).source_path
            preserve()
            report["prior_normal_profile_reference"] = dict(
                path=str(args.normal_profile_reference.resolve()),
                sha256=sha(args.normal_profile_reference),
            )
        from pyocd_debug_mcp.adapters import swd_process
        from pyocd_debug_mcp.services.connections import parse_probe_connection_id
        from tests.m05_public_support import NULL_PLAN, PublicSetup
        from tests.tiered_acceptance_support import public_text

        codes = {
            swd_process._WorkerClient.__init__.__code__,
            swd_process._WorkerClient.close.__code__,
            popen_owned.__code__,
        }
        code_ids = {id(code) for code in codes}

        def observe(frame, event, value):
            # Filter exact code objects before reading any frame locals.
            if event != "return" or id(frame.f_code) not in code_ids:
                return
            if frame.f_code is popen_owned.__code__:
                if not isinstance(value, tuple):
                    return
                process = value[0]
                if "pyocd_debug_mcp.adapters.provider_worker" not in process.args:
                    return
                worker = None
            else:
                worker = frame.f_locals["self"]
                process = getattr(worker, "_process", None)
            if process is None:
                return
            token = _start_token(process.pid)
            if process.pid in observed_workers:
                if worker is not None:
                    for index, (_, prior, prior_token) in enumerate(workers):
                        if prior is process:
                            workers[index] = (worker, process, prior_token)
                if token is not None:
                    child_exits.capture(process.pid, "provider worker")
                return
            if token is None:
                return  # Unavailable historical creation identity remains unavailable.
            observed_workers.add(process.pid)
            workers.append((worker, process, token))
            report["workers"].append(
                dict(pid=process.pid, token=token, argv=process.args, at=now())
            )
            child_exits.capture(process.pid, "provider worker")

        threading.setprofile(observe)

        def call(name, arguments):
            leases()
            preserve()
            row = dict(tool=name, arguments=arguments, at=now())
            report["calls"].append(row)
            try:
                row["reply"] = public_text(server, name, arguments)
                return row["reply"]
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                row["completed_at"] = now()
                preserve()
                save()

        flow = PublicSetup(server, call=call)

        def disconnect(logical):
            if server.connection_manager.maybe_connection(logical) is not None:
                call("disconnect", {"board_id": logical})
            assert server.connection_manager.maybe_connection(logical) is None

        def validate():
            loaded = flow.json(
                "load_setup_tool", {"board_id": board, "tool_name": "board_validate"}
            )
            if loaded["status"] != "setup_tool_loaded":
                return loaded  # Follow an assignment remedy without inventing next_call.
            next_call = loaded["next_call"]
            result = flow.json(next_call["tool"], next_call["arguments"])
            report.setdefault("exact_loader_validations", []).append(
                dict(next_call=next_call, result=result)
            )
            return result

        def safe_read():
            call("read_memory_address-plan", NULL_PLAN)
            plan = flow.plan(
                "read_memory_address", board, {"address": 0x20000000, "width": 32, "length": None}
            )
            assert plan["status"] == "plan_accepted", plan
            result = flow.execute(plan["stable_client_fallback"])
            assert result["status"] == "batch_completed", result
            response = result["completed"][0]["result"]
            if isinstance(response, list):
                response = response[0]["text"]
            value = int(response.split("\n", 1)[0], 0)
            assert 0 <= value <= 0xFFFFFFFF
            return dict(plan=plan, batch=result, value=value)

        endpoint_argv = [sys.executable, "-B", "-m", "pyocd", "server", "-p", str(port), "-u", UID]
        leases()
        endpoint, endpoint_marker = popen_owned(endpoint_argv, stdout=stdout, stderr=stderr)
        endpoint_token = _start_token(endpoint.pid)
        assert endpoint_token and identity_matches(endpoint.pid, endpoint_token)
        report["endpoint"] = dict(
            pid=endpoint.pid, token=endpoint_token, argv=endpoint_argv, selector=selector
        )
        deadline = time.monotonic() + 30
        while not listening():
            assert endpoint.poll() is None, "endpoint exited before listening"
            assert time.monotonic() < deadline, "endpoint startup deadline"
            time.sleep(0.2)
        child_exits.capture(endpoint.pid, "owned pyOCD loopback endpoint")
        listeners = [
            c
            for c in psutil.net_connections(kind="tcp")
            if c.status == psutil.CONN_LISTEN and c.laddr.port == port
        ]
        owned_endpoint_pids = {endpoint.pid} | {
            r["pid"] for r in child_exits.rows if r["role"] == "owned pyOCD loopback endpoint"
        }
        assert listeners and all(
            c.pid in owned_endpoint_pids and c.laddr.ip in {"127.0.0.1", "::1"} for c in listeners
        ), listeners
        report["owned_loopback_listeners"] = [c._asdict() for c in listeners]
        registration = flow.json(
            "register_remote_probe",
            {"host": "127.0.0.1", "port": port, "description": "Owned M12-T loopback"},
        )
        registered = registration["status"] == "remote_probe_registered"
        assert registered and registration["reachable"] is True
        call("initialization_handshake", {})

        # Independent fresh diagnostic measurements, never a preexisting firmware report.
        diagnostic = "m12_raw_chip_facts"
        call("connect", {"board_id": diagnostic, "probe_uid": selector, "target": "cortex_m"})
        raw_identity = {}
        for label, address, width in (
            ("cpuid", 0xE000ED00, 32),
            ("device_group", 0xE0042000, 32),
            ("flash_kib", 0x1FFF75E0, 16),
        ):
            result = flow.json(
                "read_memory_raw", {"board_id": diagnostic, "address": address, "width": width}
            )
            assert result["status"] == "ok", result
            raw_identity[label] = int(result["result"], 0)
        assert raw_identity["device_group"] & 0xFFF == 0x415
        assert raw_identity["flash_kib"] == 1024
        report["fresh_chip_facts"] = raw_identity
        call("resume", {"board_id": diagnostic})
        assert "running" in call("get_state", {"board_id": diagnostic}).lower()
        disconnect(diagnostic)

        overview = flow.overview(NAME)
        remote = next(
            c
            for c in overview["connections"]
            if parse_probe_connection_id(c["connection_id"]) == ("remote", selector)
        )
        overview = flow.json(
            "setup_overview",
            {"board_names": [NAME], "connection_assignments": {NAME: remote["connection_id"]}},
        )
        route = overview["routes"][0]
        board = route["board_id"]
        report["entry_route"] = route
        if args.resume_project:
            fresh = flow.json("get_setup_status", {"board_id": board})
            report["fresh_retained_setup_status"] = fresh
            assert fresh["configuration_ready"] and not fresh["ready_for_code"]
            assert not fresh["live_session_ready"]
            # Supported public reconnect avoids opening a new validation worker
            # during this retry; the unchanged production deadline still applies.
            arguments = {"board_id": board}
            if route["route"] == "no-setup":
                arguments.update(probe_uid=selector, target=TARGET)
            call("connect", arguments)
        if route["route"] == "no-setup" and "plan_action_parameters_template" not in route:
            escalation = route["optional_escalation"]
            assert "setup-full" in escalation["target_tier_choices"]
            tool = escalation["next_tool"]
            assert tool == "board_setup-plan"
            flow.json("load_setup_tool", {"board_id": board, "tool_name": tool})
            call(tool, NULL_PLAN)
            parameters = dict(
                mode="setup",
                target_tier="setup-full",
                connection_id=remote["connection_id"],
                display_name=route["display_name"],
                mcu_part_number="STM32L476RGT6",
                requires_uart=False,
                serial_baudrate=None,
                serial_id=None,
                datasheet_path=str(pdf),
            )
            accepted = flow.plan("board_setup", board, parameters, permission="one-time")
            assert accepted["status"] == "plan_accepted", accepted
        elif route["route"] == "validate":
            accepted = None
        else:
            _, accepted = flow.accept(route, pdf, "STM32L476RGT6", tier="setup-full")
        pending = flow.start(accepted) if accepted else None
        if pending is not None and pending["status"] == "setup_research_required":
            response = dict(pending["accepted_response"]["response"])
            response.update(
                pack_id="Keil.STM32L4xx_DFP",
                version="3.1.0",
                filename=pack.name,
                url=cache["publisher_download"],
                source_path=str(pack),
                official_sha256=sha(pack),
                evidence=[
                    {
                        "source": "Official Keil PDSC and ST DS10198",
                        "claim": "Exact STM32L476RGTx device leaf supplies selected target and SRAM geometry for the supplied STM32L476RGT6 part.",
                    }
                ],
                reasoning_summary="Verified official local pack bytes and exact device leaf; distinguish chip group and core facts from user-supplied package identity.",
            )
            assert flow.answer(board, pending, response)["status"] == "setup_continuation_accepted"
            pending = flow.fix(accepted)
        if pending is not None:
            report["setup_result"] = pending
            assert pending["status"] == "setup_completed", pending
        else:
            report["retained_full_route"] = route
            capabilities = flow.json("get_capabilities", {"board_id": board})
            report["retained_full_capabilities"] = capabilities
            assert capabilities["tier"] == "setup-full", capabilities
        first_validation = validate()
        report["baseline_validation"] = first_validation
        assert first_validation["status"] == "validation_passed", first_validation
        profile = server._profile_repository.load(board).source_path
        original_profile = profile.read_bytes()
        (evidence / "normal-profile-original.bin").write_bytes(original_profile)
        report["normal_profile"] = dict(
            path=str(profile), sha256=sha(profile), profile=yaml.safe_load(original_profile)
        )
        assert yaml.safe_load(original_profile)["probe_family"] == "remote"
        report["baseline_read"] = safe_read()
        call("resume", {"board_id": board})
        assert "running" in call("get_state", {"board_id": board}).lower()
        disconnect(board)
        stop_endpoint()
        registry = server._remote_probes_registry_path()
        report["retained_registration_after_outage"] = dict(
            path=str(registry), bytes=registry.read_text()
        )
        assert selector in {entry.selector for entry in server.load_remote_probes(registry)}
        try:
            call("connect", {"board_id": board})
        except Exception as exc:
            report["actual_normal_failure"] = str(exc)
        else:
            raise AssertionError(
                "ordinary remote profile unexpectedly connected after owned endpoint outage"
            )
        assert server.connection_manager.maybe_connection(board) is None
        failed_validation = validate()
        report["outage_validation"] = failed_validation
        assert failed_validation["status"] != "validation_passed", failed_validation
        overview = flow.overview(NAME)
        local = next(
            c
            for c in overview["connections"]
            if c["connection_id"].casefold().endswith(UID.casefold())
        )
        report["reviewed_local_connection"] = local
        report["reviewed_local_route"] = flow.json(
            "setup_overview",
            {"board_names": [NAME], "connection_assignments": {NAME: local["connection_id"]}},
        )
        # Ordinary profile routing may correctly refuse a replacement. The
        # exceptional plan below owns its exact reviewed local identifiers.
        instructions = call("connect_override-plan", NULL_PLAN)
        assert "does not require ready_for_code=true" in instructions
        exceptional = flow.json(
            "connect_override-plan",
            {
                "board_id": board,
                "hypothesis": "Normal saved remote connection failed after owned endpoint stopped: "
                + report["actual_normal_failure"],
                "strategy": "Use the current exact local ST-Link UID and verified STM32L476RGTx target. Preserve saved remote profile, then require fresh compatible validation before planned SRAM read.",
                "hypothesis_made": True,
                "strategy_evaluated": True,
                "expected_fail_return": "A truthful scoped refusal leaves protected SRAM unavailable and preserves the saved remote connection profile.",
                "expected_success_return": "The exact local STM connection opens disconnected, followed by fresh compatible validation and a planned SRAM word read.",
                "max_calls": 1,
                "max_calls_buffer": 0,
                "action_parameters": {
                    "probe_uid": UID,
                    "target_override": TARGET,
                    "external_board_config": None,
                },
            },
        )
        assert exceptional["status"] == "plan_accepted", exceptional
        executed = flow.execute(exceptional["stable_client_fallback"])
        report["exceptional_execution"] = executed
        assert executed["status"] == "batch_completed", executed
        connection = server.connection_manager.connection_for(board)
        report["exceptional_connection"] = asdict(connection.handle.metadata)
        assert connection.handle.metadata.probe_uid.casefold() == UID.casefold()
        assert (
            connection.handle.metadata.target_override == TARGET
            and connection.handle.worker is not None
        )
        assert server.gate_manager.live_identity(board) is None
        call("read_memory_address-plan", NULL_PLAN)
        blocked_read = flow.plan(
            "read_memory_address", board, {"address": 0x20000000, "width": 32, "length": None}
        )
        denied = flow.execute(blocked_read["stable_client_fallback"])
        report["prevalidation_read_refusal"] = denied
        assert denied["status"] == "batch_failed" and "board_validate" in json.dumps(denied)
        final_validation = validate()
        report["exceptional_validation"] = final_validation
        assert final_validation["status"] == "validation_passed", final_validation
        report["exceptional_read"] = safe_read()
        call("resume", {"board_id": board})
        assert "running" in call("get_state", {"board_id": board}).lower()
        preserve()
        report["verdict"] = "PASS"
    except BaseException as exc:
        report.update(
            verdict="FAIL", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc()
        )
    finally:
        if server is not None:
            for logical in server.connection_manager.assigned_board_ids():
                try:
                    # Resume is an ordinary public cleanup operation; no memory mutation.
                    call("resume", {"board_id": logical})
                    report["cleanup"].append(
                        dict(board=logical, state=call("get_state", {"board_id": logical}))
                    )
                except Exception as exc:
                    report["cleanup"].append(dict(board=logical, resume_error=str(exc)))
                try:
                    call("disconnect", {"board_id": logical})
                except Exception as exc:
                    report["cleanup"].append(dict(board=logical, disconnect_error=str(exc)))
            if registered:
                try:
                    removed = flow.json(
                        "unregister_remote_probe", {"host": "127.0.0.1", "port": port}
                    )
                    report["registration_cleanup"] = removed
                    assert selector not in {
                        entry.selector
                        for entry in server.load_remote_probes(
                            server._remote_probes_registry_path()
                        )
                    }
                except Exception as exc:
                    report["cleanup"].append(dict(registration_error=str(exc)))
        try:
            stop_endpoint()
        except Exception as exc:
            report["cleanup"].append(dict(endpoint_error=str(exc)))
        threading.setprofile(previous_profile)
        for worker, process, token in workers:
            try:
                if process.poll() is None:
                    assert identity_matches(process.pid, token)
                    if worker is not None:
                        worker.close()
                    else:
                        assert terminate_process_group(process, grace_seconds=10)
                process.wait(timeout=10)
                row = dict(
                    pid=process.pid,
                    token=token,
                    raw_exit=process.returncode,
                    exact_identity_absent=not identity_matches(process.pid, token),
                )
                report.setdefault("worker_cleanup", []).append(row)
                assert row["exact_identity_absent"] and row["raw_exit"] is not None
            except Exception as exc:
                report["cleanup"].append(dict(worker_error=str(exc), pid=process.pid))
        try:
            report["actual_child_exits"] = child_exits.reap()
        except Exception as exc:
            report["cleanup"].append(dict(child_exit_error=str(exc)))
        try:
            preserve()
            leases()
            if server is not None:
                assert not server.connection_manager.assigned_board_ids()
                server._monitor.closeout("M12 physical cleanup")
        except Exception as exc:
            report["cleanup"].append(dict(final_error=str(exc)))
        stdout.close()
        stderr.close()
        os.chdir(prior_cwd)
        report["finished_at"] = now()
        report["cleanup_proven"] = not any(
            any(key.endswith("error") for key in row) for row in report["cleanup"]
        )
        if not report["cleanup_proven"]:
            report["verdict"] = "FAIL"
        save()
    print(
        json.dumps(
            dict(
                report=str(destination),
                verdict=report["verdict"],
                error=report.get("error"),
                cleanup_proven=report["cleanup_proven"],
            )
        )
    )
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
