"""Opt-in independent M17 U05 using real public dispatch and native board leases.

Run phases serially. All persistent state belongs to the supplied evidence root;
no policy/grant/gate is constructed here. User approval is recorded by the actual
permission-carrying public plan under the standing campaign authorization.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import threading
import time
import traceback
import zipfile


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", choices=("nrf52840", "stm32l476"), required=True)
    parser.add_argument(
        "--phase",
        choices=("setup", "prepare", "deploy", "handoff", "restore", "final"),
        required=True,
    )
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--continue-setup", action="store_true")
    parser.add_argument("--restoration", choices=("uart", "ram"), default="uart")
    parser.add_argument("--admission-record", type=Path)
    args = parser.parse_args()
    evidence = args.evidence.resolve() / args.board
    phase = evidence / args.attempt
    phase.mkdir(parents=True, exist_ok=False)
    project = evidence / "project"
    project.mkdir(exist_ok=args.phase != "setup" or args.continue_setup)
    environment = {}
    for key, path in (
        ("BYO_MCP_ARTIFACT_ROOT", project),
        ("PYOCD_PROJECT_DIR", project),
        ("BYO_MCP_MONITOR_ROOT", phase / "monitor"),
        ("BYO_MANAGED_TEST_EVIDENCE", phase / "managed"),
        ("PYOCD_MCP_RUNS_ROOT", phase / "runs"),
        ("TEMP", phase / "temp"),
        ("TMP", phase / "temp"),
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
    lane = Path(invocation["cwd"])
    original = Path(
        json.loads((lane / "harness/harness-config.json").read_text())["root_workspace"]
    )
    execution = args.execution.resolve()
    nrf = args.board == "nrf52840"
    uid = "683854191" if nrf else "066FFF514988525067233337"
    target = "nrf52840" if nrf else "stm32l476rgtx"
    base, app_offset, sector = (0, 0x4000, 0x1000) if nrf else (0x08000000, 0x8000, 0x800)
    outside = base + 0x80000
    name, board = "M17 independent physical " + args.board, None
    report = dict(
        started_utc=now(),
        argv=sys.argv,
        cwd=str(Path.cwd()),
        phase=args.phase,
        board=args.board,
        environment=environment,
        invocation=invocation,
        calls=[],
        leases=[],
        workers={},
        worker_rpc=[],
        cleanup_errors=[],
        verdict="PENDING",
        limits="Only fully measured ranges; no whole-chip/zero-bus-write/power-cycle claim. Exact supported-unit replay does not prove STM package suffix.",
    )
    destination = phase / "physical.json"

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
        assert set(invocation["exclusive_resources"]) == required
        current = json.loads((original / ".harness-runtime/CURRENT_EPOCH.json").read_text())
        assert lane.parent.name == current["epoch_id"]
        owned = []
        for path in (original / ".harness-runtime/resources/leases").glob("*.lease"):
            row = json.loads(path.read_text())
            assert row["lane_id"] == invocation["lane_id"] and row["run_id"] == invocation["run_id"]
            assert (
                row["pid"] == controller["pid"]
                and row["creation_time"] == controller["creation_time"]
            )
            owned.append(row | dict(path=str(path), sha256=sha(path)))
        assert {r["resource_id"] for r in owned} == required
        report["leases"].append(
            dict(at=now(), owned=owned, controller=controller, provider=provider)
        )

    leases()
    firmware = execution / "firmware"
    boot = firmware / "extended" / args.board / "bootloader/fixture.elf"
    app = firmware / "extended" / args.board / "boot-application/fixture.elf"
    restore = (
        firmware / "extended" / args.board / "uart-application/fixture.elf"
        if args.restoration == "uart"
        else firmware / args.board / "fixture.elf"
    )
    report["restoration_kind"] = args.restoration
    qualified = json.loads(args.qualification.read_text())
    qualified_images = {r["path"]: r for r in qualified["images"]}
    for path in (boot, app, restore):
        assert qualified_images[str(path)]["sha256"] == sha(path)
    docs = execution / "vendor-documents"
    datasheet = docs / ("nRF52840-PS-v1.1.pdf" if nrf else "STM32L476xx-DS10198.pdf")
    reference = (
        datasheet
        if nrf
        else docs / "m17-reference-retrieval-20261017T1200Z/STM32-RM0351-third-party-mirror.pdf"
    )
    for path in (datasheet, reference):
        record = next(r for r in qualified["documents"] if r["path"] == str(path))
        assert record["sha256"] == sha(path)
    pack = execution / "packs/Keil.STM32L4xx_DFP.3.1.0.pack"
    assert sha(pack) == "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
    pack_metadata = json.loads((execution / "packs/stm32-pack-cache.json").read_text())
    with zipfile.ZipFile(pack) as archive:
        pdsc = archive.read("Keil.STM32L4xx_DFP.pdsc")
    assert hashlib.sha256(pdsc).hexdigest() == pack_metadata["pdsc_sha256"]
    report["pack"] = dict(
        path=str(pack),
        sha256=sha(pack),
        pdsc_sha256=hashlib.sha256(pdsc).hexdigest(),
        selected_device="STM32L476RGTx",
        publisher=pack_metadata["publisher_download"],
    )
    config = dict(auto_unlock=False, resume_on_disconnect=False, connect_mode="halt")
    config["jlink.non_interactive"] = True
    if args.phase == "setup" and not args.continue_setup:
        (project / "pyocd.yaml").write_text(json.dumps(config), encoding="utf-8")
    external = [
        args.invocation.resolve(),
        args.qualification.resolve(),
        datasheet,
        reference,
        pack,
        execution / "packs/stm32-pack-cache.json",
        project / "pyocd.yaml",
    ]
    for path in (boot, app, restore):
        external += [p for p in path.parent.iterdir() if p.is_file()]
    report["external_inputs"] = []
    with zipfile.ZipFile(phase / "external-inputs.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for i, path in enumerate(dict.fromkeys(external)):
            member = f"{i:03}-{path.name}"
            archive.write(path, member)
            report["external_inputs"].append(
                dict(path=str(path), member=member, sha256=sha(path), bytes=path.stat().st_size)
            )
    os.chdir(project)
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.adapters.swd_process import _WorkerClient
    from pyocd_debug_mcp.setup_flow import board_catalog
    from tests.m02_independent_support import pattern, separate_loads, write_artifact
    from tests.m05_public_support import NULL_PLAN, PublicSetup
    from tests.tiered_acceptance_support import public_text
    import psutil

    assert board_catalog._CATALOG == {}
    report["server_run_id"] = server.server_run.run_id
    workers, processes, observed_children = {}, {}, {}
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
                processes[process.pid] = process
                report["workers"].setdefault(
                    str(process.pid),
                    dict(pid=process.pid, token=_start_token(process.pid), argv=process.args),
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
        if event == "return":
            worker = frame.f_locals.get("self")
            if hasattr(worker, "_process"):
                process = worker._process
                workers[process.pid] = worker
                report["workers"].setdefault(
                    str(process.pid),
                    dict(pid=process.pid, token=_start_token(process.pid), argv=process.args),
                )
                if frame.f_code is _WorkerClient.__init__.__code__:
                    try:
                        for child in [
                            psutil.Process(process.pid),
                            *psutil.Process(process.pid).children(recursive=True),
                        ]:
                            observed_children.setdefault(
                                child.pid,
                                dict(
                                    pid=child.pid,
                                    token=_start_token(child.pid),
                                    argv=child.cmdline(),
                                    listeners=[
                                        list(c.laddr)
                                        for c in child.net_connections(kind="inet")
                                        if c.status == "LISTEN"
                                    ],
                                ),
                            )
                    except (psutil.Error, OSError) as exc:
                        report["workers"][str(process.pid)]["child_metadata_unavailable"] = str(exc)

    prior = threading.getprofile()
    threading.setprofile(observe)

    def call(tool, arguments):
        leases()
        row = dict(tool=tool, arguments=arguments, started_utc=now())
        report["calls"].append(row)
        try:
            row["reply"] = public_text(server, tool, arguments)
            return row["reply"]
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            row["finished_utc"] = now()
            save()

    flow = PublicSetup(server, call=call)

    def validate():
        loaded = flow.json("load_setup_tool", dict(board_id=board, tool_name="board_validate"))
        result = flow.json(loaded["next_call"]["tool"], loaded["next_call"]["arguments"])
        assert result["status"] == "validation_passed", result
        report.setdefault("validations", []).append(result)
        connection = server.connection_manager.connection_for(board)
        assert connection.handle.metadata.probe_uid == uid and connection.handle.worker is not None
        report["connection"] = dict(
            metadata=asdict(connection.handle.metadata),
            connection_id=connection.connection_id,
            session_id=connection.runtime_session.session_id,
        )
        return result

    def read(address, size):
        value = bytearray()
        # The public contract permits a fully contained positive block length.
        # Read these same complete fixture ranges in at most 32-KiB requests;
        # containment and the unchanged operation timeout apply to each request.
        for offset in range(0, size, 32768):
            count = min(32768, size - offset)
            parameters = dict(address=address + offset, width=8, length=count)
            flow.call("read_memory_address-plan", NULL_PLAN)
            accepted = flow.plan("read_memory_address", board, parameters)
            assert accepted["status"] == "plan_accepted", accepted
            reply = call("read_memory_address", dict(board_id=board, **parameters))
            data = bytes.fromhex(reply.splitlines()[0].split(":", 1)[-1].strip())
            assert len(data) == count
            value.extend(data)
        return bytes(value)

    def word(address):
        return int.from_bytes(read(address, 4), "little")

    def write_zero(address):
        parameters = dict(
            symbol_or_address=address,
            value=0,
            width=32,
            allow_address_fallback=True,
            reason="Clear the fixture-only SRAM execution marker before observing fresh boot handoff.",
            elf_artifact=None,
        )
        flow.call("write_memory-plan", NULL_PLAN)
        accepted = flow.plan("write_memory", board, parameters)
        assert accepted["status"] == "plan_accepted", accepted
        call("write_memory", dict(board_id=board, **parameters))
        assert word(address) == 0

    def program(role, path):
        tool = "flash_" + role
        flow.call(tool + "-plan", NULL_PLAN)
        accepted = flow.plan(
            tool,
            board,
            dict(artifact=str(path)),
            permission="one-time" if role == "bootloader" else None,
        )
        assert accepted["status"] == "plan_accepted", accepted
        report.setdefault("permission_plans", []).append(
            dict(
                at=now(),
                role=role,
                standing_authority="Current user-owned recoverable campaign programming, including user bootloader; no permanent protection changes.",
                plan=accepted,
            )
        )
        reply = call(tool, dict(board_id=board, artifact=str(path)))
        loads = separate_loads(path)
        assert f"read back {sum(len(d) for _, d in loads)} matching addressed bytes" in reply, reply
        call("halt", dict(board_id=board))
        rows = []
        for address, expected in loads:
            actual = read(address, len(expected))
            assert actual == expected, hex(address)
            blob = phase / f"readback-{role}-{address:x}.bin"
            blob.write_bytes(actual)
            rows.append(
                dict(start=address, end=address + len(actual), bytes=len(actual), sha256=sha(blob))
            )
        report.setdefault("programming", []).append(
            dict(
                role=role,
                artifact=str(path),
                sha256=sha(path),
                reply=reply,
                independent_addressed_readback=rows,
            )
        )

    def snapshot(label, ranges):
        result = {}
        for name_, address, size in ranges:
            data = read(address, size)
            path = phase / f"{label}-{name_}.bin"
            path.write_bytes(data)
            result[name_] = dict(
                start=address, end=address + size, path=str(path), sha256=sha(path)
            )
        report[label] = result
        return result

    def compare(saved, label):
        measured = snapshot(
            label, [(n, row["start"], row["end"] - row["start"]) for n, row in saved.items()]
        )
        for key in saved:
            assert (
                Path(measured[key]["path"]).read_bytes() == Path(saved[key]["path"]).read_bytes()
            ), key

    def uart(label):
        from serial import Serial
        from serial.tools import list_ports

        matches = [
            p
            for p in list_ports.comports()
            if p.serial_number and uid.casefold() in p.serial_number.casefold()
        ]
        assert len(matches) == 1, [(p.device, p.serial_number) for p in list_ports.comports()]
        payload = f"M17-{args.board}-{label}\r\n".encode()
        with Serial(matches[0].device, 115200, timeout=2, write_timeout=2) as port:
            port.dtr = True
            # Allow the USB bridge to observe DTR before the first byte. This
            # does not change any product/worker operation deadline.
            time.sleep(0.25)
            port.reset_input_buffer()
            assert port.write(payload) == len(payload)
            port.flush()
            actual = port.read(len(payload))
        report.setdefault("uart", []).append(
            dict(
                label=label,
                port=matches[0].device,
                serial_number=matches[0].serial_number,
                sent=payload.hex(),
                received=actual.hex(),
                owned_port_closed=not port.is_open,
                dtr_settle_seconds=0.25,
            )
        )
        save()
        assert actual == payload, (payload, actual)

    def running_counter(label):
        call("resume", dict(board_id=board))
        address = qualified_images[str(restore)]["symbols"]["repair_counter"]
        first, second = word(address), word(address)
        state = call("get_state", dict(board_id=board)).splitlines()[0]
        report.setdefault("running_counter", []).append(
            dict(
                label=label,
                address=address,
                first=first,
                second=second,
                delta=(second - first) & 0xFFFFFFFF,
                state=state,
            )
        )
        assert first != second and state == "RUNNING", report["running_counter"][-1]

    setup_file, deploy_file = evidence / "setup-record.json", evidence / "deploy-record.json"

    def prepare_workload():
        call("halt", dict(board_id=board))
        backup = snapshot(
            "backup",
            [
                ("boot", base, app_offset),
                ("app", base + app_offset, 0x4000),
                ("outside", outside, sector),
            ],
        )
        app_runs = separate_loads(app)
        sentinel = pattern(sector, 97 if nrf else 143)
        controlled = write_artifact(
            project / "controlled-application.elf", (*app_runs, (outside, sentinel))
        )
        data = bytearray(controlled.read_bytes())
        struct.pack_into("<I", data, 24, qualified_images[str(app)]["entry"])
        controlled.write_bytes(data)
        program("application", controlled)
        validate()
        state = server._capability_policies.resolve(board)
        policy = dict(state.map_snapshot["deployment_policy"])
        before = snapshot(
            "before", [("app", base + app_offset, 0x4000), ("outside", outside, sector)]
        )
        assert read(outside, sector) == sentinel
        report["application_policy"] = policy
        setup_file.write_text(
            json.dumps(
                dict(
                    board_id=board,
                    backup=backup,
                    before=before,
                    application_policy=policy,
                    bootloader_policy=state.map_snapshot["bootloader_policy"],
                ),
                indent=2,
            ),
            encoding="utf-8",
        )

    try:
        info = flow.overview(name)
        if info["status"] == "setup_assignment_required":
            chosen = next(
                c
                for c in info["connections"]
                if c["connection_id"].rsplit(":", 1)[-1].casefold() == uid.casefold()
            )
            info = flow.json(
                "setup_overview",
                dict(board_names=[name], connection_assignments={name: chosen["connection_id"]}),
            )
        route = info["routes"][0]
        board = route["board_id"]
        report["logical_board"] = board
        if args.phase == "setup":
            part = "nRF52840-QIAA" if nrf else "STM32L476RGT6"
            if args.continue_setup:
                call("connect", dict(board_id=board, probe_uid=uid, target=target))
            if route["route"] == "no-setup" and "plan_action_parameters_template" not in route:
                chosen = next(
                    c
                    for c in info["connections"]
                    if c["connection_id"].rsplit(":", 1)[-1].casefold() == uid.casefold()
                )
                flow.json("load_setup_tool", dict(board_id=board, tool_name="board_setup-plan"))
                flow.call("board_setup-plan", NULL_PLAN)
                accepted = flow.plan(
                    "board_setup",
                    board,
                    dict(
                        mode="setup",
                        target_tier="setup-full",
                        connection_id=chosen["connection_id"],
                        display_name=route["display_name"],
                        mcu_part_number=part,
                        requires_uart=False,
                        serial_baudrate=None,
                        serial_id=None,
                        datasheet_path=str(datasheet),
                    ),
                    permission="one-time",
                )
                assert accepted["status"] == "plan_accepted", accepted
            else:
                _, accepted = flow.accept(route, datasheet, part, tier="setup-full")
            pending = flow.start(accepted)
            if pending["status"] == "setup_research_required":
                if nrf:
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
                            dict(
                                source="Retained Keil 3.1.0 PDSC/FLM bytes",
                                claim="Supported STM32L476RGTx geometry; order/package coverage separately qualified.",
                            )
                        ],
                        reasoning_summary="Replay pinned support bytes, then observe documented factory group/size/UID on the actual unit.",
                    )
                assert (
                    flow.answer(board, pending, response)["status"] == "setup_continuation_accepted"
                )
                pending = flow.fix(accepted)
            assert pending["status"] == "setup_completed", pending
            validate()
            registers = (
                [
                    (0x10000100, 32, "device", 0x52840, 0xFFFFFFFF, None, [34], "4.4.1.8"),
                    (0x10000110, 32, "flash_size", None, 0xFFFFFFFF, 1024, [35], "4.4.1.12"),
                    (0x10000060, 32, "unique", None, 0xFFFFFFFF, None, [33], "4.4.1.3"),
                    (0x10000064, 32, "unique", None, 0xFFFFFFFF, None, [33], "4.4.1.3"),
                ]
                if nrf
                else [
                    (0xE0042000, 32, "device", 0x415, 0xFFF, None, [1827], "48.6.1 printed1826"),
                    (0x1FFF75E0, 16, "flash_size", None, 0xFFFF, 1024, [1854], "49.2 printed1853"),
                    (0x1FFF7590, 32, "unique", None, 0xFFFFFFFF, None, [1853], "49.1 printed1852"),
                    (0x1FFF7594, 32, "unique", None, 0xFFFFFFFF, None, [1854], "49.1 printed1853"),
                    (0x1FFF7598, 32, "unique", None, 0xFFFFFFFF, None, [1854], "49.1 printed1853"),
                ]
            )
            source_url = (
                "https://docs.nordicsemi.com/r/bundle/ps_nrf52840/page/ficr.html"
                if nrf
                else "https://raw.githubusercontent.com/RT-Thread/IoT_Board/master/docs/board/STM32L4/RM0351%20STM32L4x5%20and%20STM32L4x6%20Reference%20manual.pdf"
            )
            request = dict(
                start=base,
                end=base + app_offset,
                source_path=str(reference),
                source_url=source_url,
                source_pages=[31, 33, 34, 35] if nrf else [1, 1827, 1853, 1854],
                source_note="Nordic PS4413_417 v1.1 factory FICR table; older cached publication."
                if nrf
                else "ST-authored RM0351 Rev6 April2018, RT-Thread mirror; not current vendor-origin authenticated retrieval. Sections48.6.1/49.1/49.2; PDF/printed page offset+1.",
                identity_coverage="Supported model/size/unique-unit/backend replay; package suffix not asserted."
                if nrf
                else "STM32L475/476/486 device group, observed flash size, factory96bit UID, Keil support geometry and same-unit replay; package/order suffix not independently proven.",
                registers=[
                    dict(
                        address=a,
                        width_bits=w,
                        role=r,
                        expected=e,
                        mask=m,
                        unit_bytes=u,
                        read_only=True,
                        factory=True,
                        source_pages=p,
                        source_note=f"Factory read-only register, section{s}.",
                    )
                    for a, w, r, e, m, u, p, s in registers
                ],
            )
            flow.json("load_setup_tool", dict(board_id=board, tool_name="board_safety_refresh"))
            admitted = flow.json(
                "board_safety_refresh", dict(board_id=board, user_bootloader=request)
            )
            assert admitted["status"] == "safety_refresh_completed", admitted
            assert server.gate_manager.snapshot(board) is None
            validate()
            caps = flow.json("get_capabilities", dict(board_id=board))
            assert caps["identity"]["capability"] == "exact", caps
            state = server._capability_policies.resolve(board)
            report["partition_policy"] = state.map_snapshot
            prepare_workload()
        elif args.phase == "prepare":
            assert args.admission_record is not None
            prior_admission = json.loads(args.admission_record.read_text())
            assert prior_admission["board"] == args.board and prior_admission["verdict"] == "FAIL"
            assert prior_admission["invocation"]["run_id"] == invocation["run_id"]
            assert any(
                c["tool"] == "board_safety_refresh"
                and c["arguments"].get("user_bootloader", {}).get("start") == base
                and c["arguments"]["user_bootloader"]["end"] == base + app_offset
                for c in prior_admission["calls"]
            )
            call("connect", dict(board_id=board))
            validate()
            caps = flow.json("get_capabilities", dict(board_id=board))
            assert caps["identity"]["capability"] == "exact", caps
            state = server._capability_policies.resolve(board)
            proof = state.map_snapshot["bootloader_policy"]["factory_identity"]
            assert proof["source"]["sha256"] == sha(reference)
            assert proof["probe_identity"] == uid
            assert state.map_snapshot["deployment_policy"] == {"kind": "none"}
            report["partition_policy"] = state.map_snapshot
            report["admission_recovered_after_uncertain_reply"] = dict(
                path=str(args.admission_record.resolve()),
                sha256=sha(args.admission_record),
                qualification="Prior public admission timed out; current persisted authority is established by fresh public exact validation, not by the failed reply.",
            )
            prepare_workload()
        else:
            setup = json.loads(setup_file.read_text())
            assert setup["board_id"] == board
            call("connect", dict(board_id=board))
            validate()
            caps = flow.json("get_capabilities", dict(board_id=board))
            assert caps["identity"]["capability"] == "exact"
            state = server._capability_policies.resolve(board)
            assert state.map_snapshot["deployment_policy"] == setup["application_policy"]
            assert state.map_snapshot["bootloader_policy"] == setup["bootloader_policy"]
            call("halt", dict(board_id=board))
            if args.phase == "deploy":
                preserved = dict(setup["before"])
                boot_backup = Path(setup["backup"]["boot"]["path"])
                assert sha(boot_backup) == setup["backup"]["boot"]["sha256"]
                preserved_boot = phase / "expected-unaffected-boot-sectors.bin"
                preserved_boot.write_bytes(boot_backup.read_bytes()[sector:])
                preserved["unaffected-boot-sectors"] = dict(
                    start=base + sector,
                    end=base + app_offset,
                    path=str(preserved_boot),
                    sha256=sha(preserved_boot),
                )
                (evidence / "deploy-preservation.json").write_text(
                    json.dumps(preserved, indent=2), encoding="utf-8"
                )
                compare(preserved, "pre-deploy")
                write_zero(0x2000F000)
                write_zero(qualified_images[str(app)]["symbols"]["repair_boot_seen"])
                program("bootloader", boot)
                validate()
                compare(preserved, "post-deploy")
                marker = word(0x2000F000)
                seen = word(qualified_images[str(app)]["symbols"]["repair_boot_seen"])
                assert marker == seen == 0xB00710AD, (marker, seen)
                report["handoff"] = dict(marker=marker, application_observed_marker=seen)
                call("resume", dict(board_id=board))
                uart("handoff")
                call("halt", dict(board_id=board))
                report["handoff"]["counter"] = word(
                    qualified_images[str(app)]["symbols"]["repair_counter"]
                )
                deploy_file.write_text(
                    json.dumps(dict(physical=str(destination), verdict="PASS")), encoding="utf-8"
                )
            elif args.phase == "handoff":
                # Continue observations of the already-programmed image. Never
                # replay the consumed flash approval or reflash for a UART check.
                for address, expected in separate_loads(boot):
                    assert read(address, len(expected)) == expected
                report["before_handoff_diagnostic"] = {
                    key: word(qualified_images[str(app)]["symbols"][key])
                    for key in (
                        "repair_counter",
                        "repair_boot_seen",
                        "repair_uart_ready",
                        "repair_uart_errors",
                    )
                }
                flow.call("reset_and_halt-plan", NULL_PLAN)
                accepted = flow.plan("reset_and_halt", board, {})
                assert accepted["status"] == "plan_accepted", accepted
                call("reset_and_halt", dict(board_id=board))
                write_zero(0x2000F000)
                write_zero(qualified_images[str(app)]["symbols"]["repair_boot_seen"])
                call("resume", dict(board_id=board))
                try:
                    uart("fresh-handoff")
                finally:
                    call("halt", dict(board_id=board))
                    report["after_handoff_diagnostic"] = {
                        key: word(qualified_images[str(app)]["symbols"][key])
                        for key in (
                            "repair_counter",
                            "repair_boot_seen",
                            "repair_uart_ready",
                            "repair_uart_errors",
                        )
                    }
                    report["handoff_marker"] = word(0x2000F000)
                assert (
                    report["handoff_marker"]
                    == report["after_handoff_diagnostic"]["repair_boot_seen"]
                    == 0xB00710AD
                )
                compare(
                    json.loads((evidence / "deploy-preservation.json").read_text()),
                    "handoff-preservation",
                )
                deploy_file.write_text(
                    json.dumps(dict(physical=str(destination), verdict="PASS")), encoding="utf-8"
                )
            elif args.phase == "restore":
                program("bootloader", restore)
                validate()
                # Recreate the compatible handoff application and restore the
                # original controlled outside sector through unchanged ownership.
                # Compare all measured app bytes to their pre-bootloader state.
                restored_runs = (
                    *separate_loads(app),
                    (outside, Path(setup["backup"]["outside"]["path"]).read_bytes()),
                )
                restored = write_artifact(project / "restore-unrelated.elf", restored_runs)
                data = bytearray(restored.read_bytes())
                struct.pack_into("<I", data, 24, qualified_images[str(app)]["entry"])
                restored.write_bytes(data)
                report["unrelated_restore_artifact"] = dict(
                    path=str(restored), sha256=sha(restored)
                )
                compare(setup["before"], "pre-unrelated-restore")
                program("application", restored)
                validate()
                final_expected = json.loads((evidence / "deploy-preservation.json").read_text())
                final_expected["outside"] = setup["backup"]["outside"]
                # Restoration can address more boot sectors than the tiny boot
                # image. Preserve every measured sector outside its own actual
                # affected set and verify all restoration-addressed bytes above.
                restore_end = max(a + len(d) for a, d in separate_loads(restore))
                unaffected_start = (restore_end + sector - 1) // sector * sector
                prior_boot = final_expected["unaffected-boot-sectors"]
                unaffected = phase / "expected-after-restoration-boot-sectors.bin"
                unaffected.write_bytes(
                    Path(prior_boot["path"]).read_bytes()[unaffected_start - prior_boot["start"] :]
                )
                final_expected["unaffected-boot-sectors"] = dict(
                    start=unaffected_start,
                    end=prior_boot["end"],
                    path=str(unaffected),
                    sha256=sha(unaffected),
                )
                report["restoration_affected_boot_sector_range"] = dict(
                    start=base, end=unaffected_start
                )
                compare(final_expected, "restored-unrelated")
                (evidence / "final-expected.json").write_text(
                    json.dumps(final_expected, indent=2), encoding="utf-8"
                )
                if args.restoration == "uart":
                    call("resume", dict(board_id=board))
                    uart("restored-workload")
                    call("halt", dict(board_id=board))
                    report["restored_execution"] = dict(
                        counter=word(qualified_images[str(restore)]["symbols"]["repair_counter"]),
                        uart_ready=word(
                            qualified_images[str(restore)]["symbols"]["repair_uart_ready"]
                        ),
                    )
                    assert report["restored_execution"]["uart_ready"] == 1
                else:
                    running_counter("restored RAM-only campaign workload")
            else:
                for address, expected in separate_loads(restore):
                    assert read(address, len(expected)) == expected
                compare(
                    json.loads((evidence / "final-expected.json").read_text()), "final-unrelated"
                )
                if args.restoration == "uart":
                    call("resume", dict(board_id=board))
                    uart("final-fresh-validation")
                else:
                    running_counter("fresh-restart validation of RAM-only campaign workload")
            after = server._capability_policies.resolve(board)
            assert after.map_snapshot["deployment_policy"] == setup["application_policy"]
            assert after.map_snapshot["bootloader_policy"] == setup["bootloader_policy"]
            report["unchanged_application_policy"] = after.map_snapshot["deployment_policy"]
            report["unchanged_bootloader_policy"] = after.map_snapshot["bootloader_policy"]
        report["verdict"] = "PASS"
    except Exception:
        report["verdict"] = "FAIL"
        report["traceback"] = traceback.format_exc()
    finally:
        if board and server.connection_manager.maybe_connection(board):
            try:
                call("resume", dict(board_id=board))
                report["final_state"] = call("get_state", dict(board_id=board))
                assert report["final_state"].splitlines()[0] == "RUNNING"
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
                report["cleanup_errors"].append(f"Worker {pid} cleanup unproven")
        for pid, process in processes.items():
            row = report["workers"][str(pid)]
            row.update(
                raw_exit=process.poll(),
                exact_identity_absent=bool(
                    row["token"] and not identity_matches(pid, row["token"])
                ),
            )
            if row["raw_exit"] is None or not row["exact_identity_absent"]:
                report["cleanup_errors"].append(f"Owned process {pid} cleanup unproven")
        for pid, row in observed_children.items():
            row["exact_identity_absent"] = bool(
                row["token"] and not identity_matches(pid, row["token"])
            )
            if not row["exact_identity_absent"]:
                report["cleanup_errors"].append(f"Observed descendant {pid} still present")
        report["observed_children"] = list(observed_children.values())
        report["project_state"] = []
        with zipfile.ZipFile(phase / "project-state.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted((project / ".firm").rglob("*")):
                if path.is_file():
                    member = path.relative_to(project).as_posix()
                    archive.write(path, member)
                    report["project_state"].append(
                        dict(
                            path=str(path),
                            member=member,
                            bytes=path.stat().st_size,
                            sha256=sha(path),
                        )
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
                for k in ("board", "phase", "verdict", "traceback", "cleanup_errors")
            },
            indent=2,
        )
    )
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
