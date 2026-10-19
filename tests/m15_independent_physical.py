"""Opt-in M15 U02 phases in distinct real server processes under native leases."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import traceback
import zipfile


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", choices=("nrf52840", "stm32l476"), required=True)
    parser.add_argument(
        "--phase",
        choices=("inspect", "setup", "recover", "validate", "final-inspect"),
        required=True,
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--attempt")
    parser.add_argument("--continue-setup", action="store_true")
    parser.add_argument("--setup-record", type=Path)
    parser.add_argument("--recovery-record", type=Path)
    args = parser.parse_args()
    evidence, execution = args.evidence.resolve(), args.execution.resolve()
    phase_root = evidence / (args.attempt or args.phase)
    phase_root.mkdir(parents=True, exist_ok=False)
    raw_phase = args.phase in {"inspect", "final-inspect"}
    project = phase_root / "project" if raw_phase else evidence / "full-project"
    project.mkdir(exist_ok=args.phase in {"recover", "validate"} or args.continue_setup)
    environment = {}
    for key, path in (
        ("BYO_MCP_ARTIFACT_ROOT", project),
        ("PYOCD_PROJECT_DIR", project),
        ("BYO_MCP_MONITOR_ROOT", phase_root / "monitor"),
        ("BYO_MANAGED_TEST_EVIDENCE", phase_root / "managed"),
        ("PYOCD_MCP_RUNS_ROOT", phase_root / "runs"),
        ("TEMP", phase_root / "temp"),
        ("TMP", phase_root / "temp"),
    ):
        path.mkdir(exist_ok=True)
        environment[key] = str(path)
    os.environ.update(environment)
    for key in (
        "BYO_MCP_TEST_SEAMS",
        "BYO_MCP_TEST_FAULTS",
        "BYO_MCP_TIER_FAULT",
        "PYOCD_TARGET",
        "PYOCD_PROBE_UID",
    ):
        os.environ.pop(key, None)
    invocation = json.loads(args.invocation.read_text())
    original = Path(
        json.loads((Path(invocation["cwd"]) / "harness/harness-config.json").read_text())[
            "root_workspace"
        ]
    )
    uid = {"nrf52840": "683854191", "stm32l476": "066FFF514988525067233337"}[args.board]
    target = "nrf52840" if args.board == "nrf52840" else "stm32l476rgtx"
    name = "M15 independent physical " + args.board
    board = "m15_raw_" + args.board if raw_phase else None
    report = dict(
        started_utc=now(),
        argv=sys.argv,
        cwd=str(Path.cwd()),
        phase=args.phase,
        board=args.board,
        project=str(project),
        environment=environment,
        invocation=invocation,
        calls=[],
        leases=[],
        workers={},
        worker_rpc=[],
        cleanup_errors=[],
        verdict="PENDING",
        qualification="Measured controlled ranges only. No whole-flash/RAM/settings, UART, power-boot, or zero-bus-write claim. STM group/size/UID/pack target do not independently prove exact package.",
    )
    report["phase_path"] = str(phase_root)
    destination = phase_root / "physical.json"

    def save():
        destination.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    from tests.m02_physical_flash import creation
    from pyocd_debug_mcp.kernel.processes import _start_token, identity_matches, popen_owned

    report["process"] = dict(pid=os.getpid(), token=_start_token(os.getpid()))

    def leases():
        status = json.loads(Path(invocation["paths"]["controller_status"]).read_text())
        assert status["run_id"] == invocation["run_id"] and status["controller_state"] == "running"
        controller, provider = status["controller_identity"], status["provider_state"]
        assert creation(controller["pid"]) == controller["creation_time"]
        assert creation(provider["pid"]) == provider["creation_time"]
        required = {"hardware:nrf52840:683854191", "hardware:stm32l476:066FFF514988525067233337"}
        assert required.issubset(invocation["exclusive_resources"])
        owned = []
        for path in (original / ".harness-runtime/resources/leases").glob("*.lease"):
            row = json.loads(path.read_text())
            if row["resource_id"] in required:
                assert (
                    row["lane_id"] == invocation["lane_id"]
                    and row["run_id"] == invocation["run_id"]
                )
                assert (
                    row["pid"] == controller["pid"]
                    and row["creation_time"] == controller["creation_time"]
                )
                owned.append(row | dict(path=str(path), sha256=sha(path)))
        assert {row["resource_id"] for row in owned} == required
        report["leases"].append(
            dict(at=now(), owned=owned, controller=controller, provider=provider)
        )

    leases()
    config = dict(auto_unlock=False, resume_on_disconnect=False, connect_mode="halt")
    config["jlink.non_interactive"] = True
    pack = execution / "packs/Keil.STM32L4xx_DFP.3.1.0.pack"
    pack_metadata = json.loads((execution / "packs/stm32-pack-cache.json").read_text())
    if raw_phase or args.phase == "setup" and not args.continue_setup:
        (project / "pyocd.yaml").write_text(json.dumps(config), encoding="utf-8")
    if raw_phase and args.board == "stm32l476":
        assert sha(pack) == pack_metadata["sha256"]
        files = project / ".firm/packs/files"
        files.mkdir(parents=True)
        shutil.copyfile(pack, files / pack.name)
        (project / ".firm/packs/manifest.yaml").write_text(
            json.dumps(
                dict(
                    packs=[
                        dict(
                            id="Keil.STM32L4xx_DFP",
                            version="3.1.0",
                            filename=pack.name,
                            sha256=sha(pack),
                            url=pack_metadata["publisher_download"],
                            provides_targets=[target],
                        )
                    ]
                )
            ),
            encoding="utf-8",
        )
        config["pack"] = [str(pack)]
        (project / "pyocd.yaml").write_text(json.dumps(config), encoding="utf-8")
    pdf = (
        execution
        / "vendor-documents"
        / ("nRF52840-PS-v1.1.pdf" if args.board == "nrf52840" else "STM32L476xx-DS10198.pdf")
    )
    artifact = execution / "firmware" / args.board / "fixture.elf"
    files = [
        args.invocation.resolve(),
        pdf,
        artifact,
        artifact.with_suffix(".bin"),
        artifact.with_suffix(".hex"),
        artifact.parent / "fixture-manifest.json",
    ]
    if args.board == "stm32l476":
        files += [pack, execution / "packs/stm32-pack-cache.json"]
    report["external_inputs"] = []
    with zipfile.ZipFile(phase_root / "external-inputs.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for i, path in enumerate(files):
            member = f"{i:02d}-{path.name}"
            archive.write(path, member)
            report["external_inputs"].append(
                dict(path=str(path), member=member, bytes=path.stat().st_size, sha256=sha(path))
            )
    from elftools.elf.elffile import ELFFile
    from intelhex import IntelHex

    with artifact.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [
            (int(s["p_paddr"]), s.data())
            for s in elf.iter_segments()
            if s["p_type"] == "PT_LOAD" and s["p_filesz"]
        ]
        symbols = {
            n: int(elf.get_section_by_name(".symtab").get_symbol_by_name(n)[0]["st_value"])
            for n in ("repair_signature", "repair_counter")
        }
    expected = {start + i: value for start, data in segments for i, value in enumerate(data)}
    assert expected == IntelHex(str(artifact.with_suffix(".hex"))).todict()
    image = b"".join(data for _, data in segments)
    assert image == artifact.with_suffix(".bin").read_bytes()
    manifest = json.loads((artifact.parent / "fixture-manifest.json").read_text())
    assert sha(artifact) == manifest["files"]["fixture.elf"]
    report["fixture"] = dict(
        ranges=[dict(start=a, end=a + len(d)) for a, d in segments],
        addressed_bytes=len(expected),
        image_sha256=hashlib.sha256(image).hexdigest(),
        symbols=symbols,
    )
    if args.board == "stm32l476":
        with zipfile.ZipFile(pack) as archive:
            pdsc = archive.read("Keil.STM32L4xx_DFP.pdsc")
        assert hashlib.sha256(pdsc).hexdigest() == pack_metadata["pdsc_sha256"]
        report["pack"] = dict(
            sha256=sha(pack),
            pdsc_sha256=hashlib.sha256(pdsc).hexdigest(),
            selected_device="STM32L476RGTx",
            publisher=pack_metadata["publisher_download"],
        )
    setup_record = (
        args.setup_record.resolve() if args.setup_record else evidence / "setup/physical.json"
    )
    baseline = None
    backup = evidence / "selected-generation-backup.json"
    if args.phase in {"recover", "validate"}:
        baseline = json.loads(setup_record.read_text())
        assert baseline["verdict"] == "PASS" and baseline["board"] == args.board
        board = baseline["logical_board"]
        pointer = Path(baseline["selected"]["pointer"])
        generation = Path(baseline["selected"]["generation"])
        assert sha(backup) == baseline["selected"]["generation_sha256"]
        assert sha(pointer) == baseline["selected"]["pointer_sha256"]
        if args.phase == "recover":
            assert sha(generation) == sha(backup)
            generation.unlink()  # Exact owned selected file; backup remains untouched.
            report["loss_before_fresh_import"] = dict(
                generation=str(generation), absent=not generation.exists()
            )
    os.chdir(project)
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.adapters.swd_process import _WorkerClient
    from pyocd_debug_mcp.setup_flow import board_catalog
    from tests.m05_public_support import NULL_PLAN, PublicSetup
    from tests.tiered_acceptance_support import public_text
    from mcp.server.fastmcp.exceptions import ToolError

    report["server_run_id"] = server.server_run.run_id
    assert board_catalog._CATALOG == {}
    workers = {}
    owned_processes = {}
    codes = {
        _WorkerClient.__init__.__code__,
        _WorkerClient.close.__code__,
        _WorkerClient.call.__code__,
        popen_owned.__code__,
    }

    def observe(frame, event, value):
        if frame.f_code not in codes:
            return
        if frame.f_code is popen_owned.__code__:
            if event == "return" and isinstance(value, tuple):
                process, marker = value
                owned_processes[process.pid] = process
                report["workers"].setdefault(
                    str(process.pid),
                    dict(
                        pid=process.pid,
                        token=_start_token(process.pid),
                        argv=process.args,
                        marker=json.loads(Path(marker).read_text())
                        if marker and Path(marker).exists()
                        else None,
                    ),
                )
            return
        if frame.f_code is _WorkerClient.call.__code__ and event == "call":
            report["worker_rpc"].append(
                dict(
                    at=now(),
                    operation=frame.f_locals.get("operation"),
                    arguments=frame.f_locals.get("arguments"),
                )
            )
        if event != "return":
            return
        worker = frame.f_locals.get("self")
        if not hasattr(worker, "_process"):
            return
        process = worker._process
        workers[process.pid] = worker
        entry = report["workers"].setdefault(
            str(process.pid),
            dict(pid=process.pid, token=_start_token(process.pid), argv=process.args),
        )
        if "actual_environment" not in entry:
            import psutil

            try:
                p = psutil.Process(process.pid)
                env = p.environ()
                entry["actual_environment"] = {
                    k: env.get(k) for k in environment | {"PYTHONPATH": ""}
                }
                entry["listeners"] = [
                    list(c.laddr) for c in p.net_connections(kind="inet") if c.status == "LISTEN"
                ]
            except (psutil.Error, OSError) as exc:
                entry["metadata_unavailable"] = str(exc)

    prior = threading.getprofile()
    threading.setprofile(observe)

    def call(tool, arguments):
        leases()
        row = dict(tool=tool, arguments=arguments, started_utc=now())
        report["calls"].append(row)
        try:
            row["reply"] = public_text(server, tool, arguments)
            return row["reply"]
        except ToolError as exc:
            row["error"] = str(exc)
            raise
        finally:
            row["finished_utc"] = now()
            save()

    flow = PublicSetup(server, call=call)

    def raw_read(address, width=32, length=None):
        reply = flow.json(
            "read_memory_raw", dict(board_id=board, address=address, width=width, length=length)
        )
        assert reply["status"] == "ok", reply
        return bytes.fromhex(reply["result"]) if length else int(reply["result"], 0)

    def safe_read(address, width=32, length=None):
        flow.call("read_memory_address-plan", NULL_PLAN)
        plan = flow.plan(
            "read_memory_address", board, dict(address=address, width=width, length=length)
        )
        assert plan["status"] == "plan_accepted", plan
        reply = call(
            "read_memory_address", dict(board_id=board, address=address, width=width, length=length)
        )
        return (
            bytes.fromhex(reply.splitlines()[0].split(":", 1)[-1].strip())
            if length
            else int(reply.splitlines()[0], 16)
        )

    def read_block(reader, start, size):
        return b"".join(
            reader(start + offset, length=min(4096, size - offset))
            for offset in range(0, size, 4096)
        )

    def overview():
        result = flow.overview(name)
        if result["status"] == "setup_assignment_required":
            selected = next(
                c
                for c in result["connections"]
                if c["connection_id"].rsplit(":", 1)[-1].casefold() == uid.casefold()
            )
            result = flow.json(
                "setup_overview",
                dict(board_names=[name], connection_assignments={name: selected["connection_id"]}),
            )
        return result

    def validate():
        loaded = flow.json("load_setup_tool", dict(board_id=board, tool_name="board_validate"))
        result = flow.json(loaded["next_call"]["tool"], loaded["next_call"]["arguments"])
        assert result["status"] == "validation_passed", result
        report.setdefault("validations", []).append(result)
        connection = server.connection_manager.connection_for(board)
        report["connection"] = dict(
            metadata=asdict(connection.handle.metadata),
            connection_id=connection.connection_id,
            session_id=connection.runtime_session.session_id,
        )
        assert connection.handle.metadata.probe_uid == uid and connection.handle.worker is not None
        return result

    def controlled(reader, prefix):
        values = {}
        ranges = [
            ("workload", segments[0][0], len(image)),
            (
                "outside",
                0x80000 if args.board == "nrf52840" else 0x08080000,
                4096 if args.board == "nrf52840" else 2048,
            ),
        ]
        if not raw_phase:
            ranges.append(("sram", 0x20000018, 32))
        for label, start, count in ranges:
            data = read_block(reader, start, count)
            (phase_root / f"{prefix}-{label}.bin").write_bytes(data)
            values[label] = dict(
                start=start,
                end=start + count,
                sha256=hashlib.sha256(data).hexdigest(),
                bytes=len(data),
            )
        return values

    try:
        if raw_phase:
            call("connect", dict(board_id=board, probe_uid=uid, target=target))
            call("halt", dict(board_id=board))
            connection = server.connection_manager.connection_for(board)
            assert (
                connection.handle.metadata.probe_uid == uid and connection.handle.worker is not None
            )
            report["connection"] = dict(
                metadata=asdict(connection.handle.metadata),
                connection_id=connection.connection_id,
                session_id=connection.runtime_session.session_id,
            )
            identity = dict(cpuid=raw_read(0xE000ED00))
            if args.board == "nrf52840":
                identity.update(
                    part=raw_read(0x10000100),
                    variant=raw_read(0x10000104),
                    package=raw_read(0x10000108),
                    flash_kib=raw_read(0x10000110),
                    ram_kib=raw_read(0x1000010C),
                    device_id=[raw_read(0x10000060), raw_read(0x10000064)],
                )
                assert identity["part"] == 0x52840 and identity["flash_kib"] == 1024
                settings = {
                    "uicr": raw_read(0x10001000, length=1024),
                    "ficr": raw_read(0x10000000, length=4096),
                }
            else:
                identity.update(
                    device_group=raw_read(0xE0042000),
                    flash_kib=raw_read(0x1FFF75E0, width=16),
                    device_id=[raw_read(0x1FFF7590 + i * 4) for i in range(3)],
                )
                assert identity["device_group"] & 0xFFF == 0x415 and identity["flash_kib"] == 1024
                settings = {"flash-option-registers": raw_read(0x40022020, length=0x30)}
            report["physical_identity"] = identity
            report["settings"] = {}
            for label, data in settings.items():
                path = phase_root / (label + ".bin")
                path.write_bytes(data)
                report["settings"][label] = dict(bytes=len(data), sha256=sha(path))
                if args.phase == "final-inspect":
                    assert data == (evidence / "inspect" / path.name).read_bytes(), label
            report["controlled"] = controlled(raw_read, "observed")
            if args.phase == "final-inspect":
                first = json.loads((evidence / "inspect/physical.json").read_text())
                assert identity == first["physical_identity"]
                assert (phase_root / "observed-outside.bin").read_bytes() == (
                    evidence / "inspect/observed-outside.bin"
                ).read_bytes()
                setup_baseline = json.loads(setup_record.read_text())
                assert (phase_root / "observed-workload.bin").read_bytes() == (
                    Path(setup_baseline["phase_path"]) / "baseline-workload.bin"
                ).read_bytes()
        elif args.phase == "setup":
            route_info = overview()
            assert route_info["status"] == "setup_routes_ready", route_info
            route = route_info["routes"][0]
            board = route["board_id"]
            if args.continue_setup:
                call("connect", dict(board_id=board, probe_uid=uid, target=target))
            part = "nRF52840-QIAA" if args.board == "nrf52840" else "STM32L476RGT6"
            if route["route"] == "no-setup" and "plan_action_parameters_template" not in route:
                assert "setup-full" in route["optional_escalation"]["target_tier_choices"], route
                selected = next(
                    c
                    for c in route_info["connections"]
                    if c["connection_id"].rsplit(":", 1)[-1].casefold() == uid.casefold()
                )
                flow.json("load_setup_tool", dict(board_id=board, tool_name="board_setup-plan"))
                flow.call("board_setup-plan", NULL_PLAN)
                plan = flow.plan(
                    "board_setup",
                    board,
                    dict(
                        mode="setup",
                        target_tier="setup-full",
                        connection_id=selected["connection_id"],
                        display_name=route["display_name"],
                        mcu_part_number=part,
                        requires_uart=False,
                        serial_baudrate=None,
                        serial_id=None,
                        datasheet_path=str(pdf),
                    ),
                    permission="one-time",
                )
                assert plan["status"] == "plan_accepted", plan
            else:
                _, plan = flow.accept(route, pdf, part, tier="setup-full")
            pending = flow.start(plan)
            if pending["status"] == "setup_research_required":
                if args.board == "nrf52840":
                    response = flow.target_answer(pending, target)
                else:
                    response = dict(pending["accepted_response"]["response"])
                    response.update(
                        pack_id="Keil.STM32L4xx_DFP",
                        version="3.1.0",
                        filename=pack.name,
                        url=pack_metadata["publisher_download"],
                        source_path=str(pack),
                        official_sha256=sha(pack),
                        evidence=[
                            dict(
                                source="Retained official publisher PDSC/FLM bytes",
                                claim="STM32L476RGTx supports supplied STM32L476RGT6; live group/size do not prove package.",
                            )
                        ],
                        reasoning_summary="Verify pack bytes and qualify physical identity separately.",
                    )
                accepted = flow.answer(board, pending, response)
                assert accepted["status"] == "setup_continuation_accepted", accepted
                pending = flow.fix(plan)
            assert pending["status"] == "setup_completed", pending
            validate()
            call("halt", dict(board_id=board))
            current = read_block(safe_read, segments[0][0], len(image))
            report["fixture_before_programming"] = dict(
                matches=current == image, sha256=hashlib.sha256(current).hexdigest()
            )
            (phase_root / "before-programming.bin").write_bytes(current)
            flow.call("flash_application-plan", NULL_PLAN)
            plan = flow.plan("flash_application", board, dict(artifact=str(artifact)))
            assert plan["status"] == "plan_accepted", plan
            flashed = call("flash_application", dict(board_id=board, artifact=str(artifact)))
            assert f"read back {len(expected)} matching addressed bytes" in flashed, flashed
            call("halt", dict(board_id=board))
            validate()
            report["controlled"] = controlled(safe_read, "baseline")
            assert (phase_root / "baseline-workload.bin").read_bytes() == image
            assert (phase_root / "baseline-outside.bin").read_bytes() == (
                evidence / "inspect/observed-outside.bin"
            ).read_bytes()
            state = server._capability_policies.resolve(board)
            assert (
                state.tier.value == "setup-full"
                and state.map_snapshot["deployment_policy"]["kind"] != "none"
            )
            pointer = server._capability_policies.pointer_path(board)
            pointer_data = pointer.read_bytes()
            selected = json.loads(pointer_data)
            generation = pointer.parent / "generations" / (selected["generation_id"] + ".json")
            backup.write_bytes(generation.read_bytes())
            (evidence / "original-pointer.json").write_bytes(pointer_data)
            report["selected"] = dict(
                pointer=str(pointer),
                pointer_sha256=sha(pointer),
                generation=str(generation),
                generation_sha256=sha(generation),
                backup=str(backup),
                document=json.loads(backup.read_bytes()),
            )
            with zipfile.ZipFile(
                evidence / "original-setup-records.zip", "w", zipfile.ZIP_DEFLATED
            ) as archive:
                for p in sorted((project / ".firm").rglob("*")):
                    if p.is_file():
                        archive.write(p, p.relative_to(project).as_posix())
        elif args.phase == "recover":
            assert (
                report["worker_rpc"] == []
                and server.connection_manager.maybe_connection(board) is None
            )
            for tool in ("read_memory_address", "read_memory_raw"):
                try:
                    refused = call(
                        tool, dict(board_id=board, address=0x20000018, width=32, length=None)
                    )
                except ToolError as exc:
                    refused = str(exc)
                assert "corrupt" in refused.lower(), refused
            caps = flow.json("get_capabilities", dict(board_id=board))
            flow.json("get_setup_status", dict(board_id=board))
            route_info = overview()
            assert route_info["status"] == "setup_policy_recovery_required", route_info
            for tool in ("board_setup-plan", "board_safety_refresh", "board_validate"):
                old = flow.json("load_setup_tool", dict(board_id=board, tool_name=tool))
                assert old["status"] == "setup_policy_recovery_required", old
            recovery = caps["recovery"]
            assert recovery["generation_digest"] == sha(backup)
            flow.json(recovery["load_call"]["tool"], recovery["load_call"]["arguments"])
            result = flow.json(
                "board_policy_restore", dict(board_id=board, generation_backup_path=str(backup))
            )
            assert (
                result["status"] == "policy_restore_completed"
                and not result["ready_for_code"]
                and result["validation_required"]
            ), result
            # setup_overview may enumerate probes in an owned CLI process.
            # Target sessions/RPC are the hardware boundary for this assertion.
            assert report["worker_rpc"] == [] and workers == {}
            report["restoration"] = result
            assert generation.read_bytes() == backup.read_bytes()
            assert sha(pointer) == baseline["selected"]["pointer_sha256"]
            assert json.loads(generation.read_bytes()) == baseline["selected"]["document"]
        else:
            recovery_record = (
                args.recovery_record.resolve()
                if args.recovery_record
                else evidence / "recover/physical.json"
            )
            recovered = json.loads(recovery_record.read_text())
            assert recovered["verdict"] == "PASS"
            assert (
                recovered["process"]["pid"] != os.getpid()
                and recovered["server_run_id"] != server.server_run.run_id
            )
            status = flow.json("get_setup_status", dict(board_id=board))
            assert not status["ready_for_code"] and not status["live_session_ready"], status
            route_info = overview()
            assert route_info["status"] == "setup_routes_ready", route_info
            call("connect", dict(board_id=board))
            flow.call("read_memory_address-plan", NULL_PLAN)
            plan = flow.plan(
                "read_memory_address", board, dict(address=0x20000018, width=32, length=None)
            )
            assert plan["status"] == "plan_accepted", plan
            before = len(report["worker_rpc"])
            denied = flow.execute(plan["stable_client_fallback"])
            assert denied["status"] == "batch_failed" and "validat" in json.dumps(denied).lower(), (
                denied
            )
            assert len(report["worker_rpc"]) == before
            validate()
            call("halt", dict(board_id=board))
            report["controlled"] = controlled(safe_read, "after")
            for label in ("workload", "outside", "sram"):
                assert (phase_root / f"after-{label}.bin").read_bytes() == (
                    Path(baseline["phase_path"]) / f"baseline-{label}.bin"
                ).read_bytes(), label
            assert sha(pointer) == baseline["selected"]["pointer_sha256"]
            assert generation.read_bytes() == backup.read_bytes()
            assert json.loads(generation.read_bytes()) == baseline["selected"]["document"]
            report["safe_sram_value"] = safe_read(0x20000018)
        report["logical_board"] = board
        report["verdict"] = "PASS"
    except Exception:
        report["verdict"] = "FAIL"
        report["traceback"] = traceback.format_exc()
    finally:
        if board and server.connection_manager.maybe_connection(board):
            try:
                if args.phase in {"validate", "final-inspect"} or report["verdict"] == "FAIL":
                    call("resume", dict(board_id=board))
                    report["final_state"] = call("get_state", dict(board_id=board))
                    assert report["final_state"].splitlines()[0] == "RUNNING"
                else:
                    report["final_state"] = call("get_state", dict(board_id=board))
                    assert report["final_state"].splitlines()[0] == "HALTED"
                call("disconnect", dict(board_id=board))
            except Exception:
                report["cleanup_errors"].append(traceback.format_exc())
        threading.setprofile(prior)
        for pid, worker in workers.items():
            row = report["workers"][str(pid)]
            row.update(
                raw_exit=worker._process.poll(),
                cleanup_confirmed=worker._cleanup_confirmed,
                exact_identity_absent=bool(
                    row["token"] and not identity_matches(pid, row["token"])
                ),
            )
            if (
                row["raw_exit"] is None
                or not row["cleanup_confirmed"]
                or not row["exact_identity_absent"]
            ):
                report["cleanup_errors"].append(f"Worker {pid} cleanup is not proven")
        for pid, process in owned_processes.items():
            row = report["workers"][str(pid)]
            row.setdefault("kind", "target-worker" if pid in workers else "owned command process")
            row.update(
                raw_exit=process.poll(),
                exact_identity_absent=bool(
                    row["token"] and not identity_matches(pid, row["token"])
                ),
            )
            if row["raw_exit"] is None or not row["exact_identity_absent"]:
                report["cleanup_errors"].append(f"Owned command {pid} cleanup is not proven")
        report["imports"] = []
        for module_name, module in sorted(sys.modules.items()):
            file = getattr(module, "__file__", None)
            if (
                module_name.startswith(("pyocd_debug_mcp", "pyocd.", "tests."))
                and file
                and Path(file).is_file()
            ):
                report["imports"].append(
                    dict(module=module_name, path=str(Path(file).resolve()), sha256=sha(file))
                )
        from tests.m15_independent_evidence import capture_imports

        capture_imports()
        if report["cleanup_errors"]:
            report["verdict"] = "FAIL"
        leases()
        report["finished_utc"] = now()
        save()
    print(
        json.dumps(
            {
                k: report.get(k)
                for k in (
                    "board",
                    "phase",
                    "verdict",
                    "server_run_id",
                    "traceback",
                    "cleanup_errors",
                )
            },
            indent=2,
        )
    )
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
