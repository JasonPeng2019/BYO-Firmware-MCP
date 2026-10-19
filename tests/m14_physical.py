"""Opt-in M14 U04 client. Real public setup, workers and per-operation authority."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
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
        "--stage", choices=("raw-inspect", "inspect", "workflow", "restore"), required=True
    )
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--resume-report", type=Path)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    args.invocation = args.invocation.resolve()
    args.execution = args.execution.resolve()
    if args.baseline:
        args.baseline = args.baseline.resolve()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    resumed = None
    if args.resume_report:
        args.resume_report = args.resume_report.resolve()
        resumed = json.loads(args.resume_report.read_text())
        assert args.stage == "workflow" and resumed["verdict"] == "FAIL"
        assert resumed["board"] == args.board and not resumed.get("erase_reply")
        project = Path(resumed["project"]).resolve()
        assert project.is_dir() and project.is_relative_to(evidence.parent)
    else:
        project = evidence / "project"
        project.mkdir()
    os.environ.update(
        BYO_MCP_ARTIFACT_ROOT=str(project),
        PYOCD_PROJECT_DIR=str(project),
        BYO_MCP_MONITOR_ROOT=str(evidence / "monitor"),
        BYO_MANAGED_TEST_EVIDENCE=str(evidence / "managed"),
        PYOCD_MCP_RUNS_ROOT=str(evidence / "runs"),
    )
    for key in ("BYO_MCP_TEST_SEAMS", "BYO_MCP_TEST_FAULTS", "PYOCD_TARGET", "PYOCD_PROBE_UID"):
        os.environ.pop(key, None)
    invocation = json.loads(args.invocation.read_text(encoding="utf-8"))
    original = Path(
        json.loads((Path(invocation["cwd"]) / "harness/harness-config.json").read_text())[
            "root_workspace"
        ]
    )
    uid = {"nrf52840": "683854191", "stm32l476": "066FFF514988525067233337"}[args.board]
    target = "nrf52840" if args.board == "nrf52840" else "stm32l476rgtx"
    report = dict(
        started_utc=now(),
        stage=args.stage,
        board=args.board,
        argv=sys.argv,
        cwd=str(Path.cwd()),
        invocation=invocation,
        project=str(project),
        calls=[],
        workers={},
        leases=[],
        cleanup_errors=[],
        verdict="PENDING",
        qualification="Actual measured ranges only. CPUID is core evidence; STM group/size/backend part do not prove exact package. No UART, all-flash, zero bus-write or RAM restoration claim.",
    )
    destination = evidence / "physical.json"
    if resumed:
        report["resume_source"] = dict(
            path=str(args.resume_report),
            sha256=sha(args.resume_report),
            project=str(project),
            purpose="One explicit public reconnect/setup continuation after the recorded validation deadline",
        )
        with zipfile.ZipFile(
            evidence / "before-continuation-project.zip", "w", zipfile.ZIP_DEFLATED
        ) as archive:
            for path in sorted((project / ".firm").rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(project).as_posix())

    def save():
        destination.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    from tests.m02_physical_flash import creation
    from pyocd_debug_mcp.kernel.processes import _start_token, identity_matches

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
                    row["run_id"] == invocation["run_id"]
                    and row["lane_id"] == invocation["lane_id"]
                )
                assert (
                    row["pid"] == controller["pid"]
                    and row["creation_time"] == controller["creation_time"]
                )
                owned.append(row | {"sha256": sha(path)})
        assert {r["resource_id"] for r in owned} == required
        report["leases"].append(
            dict(at=now(), owned=owned, controller=controller, provider=provider)
        )

    leases()
    config = {
        "auto_unlock": False,
        "resume_on_disconnect": False,
        "connect_mode": "halt",
        "jlink.non_interactive": True,
    }
    (project / "pyocd.yaml").write_text(json.dumps(config), encoding="utf-8")
    pdf = (
        args.execution
        / "vendor-documents"
        / ("nRF52840-PS-v1.1.pdf" if args.board == "nrf52840" else "STM32L476xx-DS10198.pdf")
    )
    artifact = args.execution / "firmware" / args.board / "fixture.elf"
    pack = args.execution / "packs/Keil.STM32L4xx_DFP.3.1.0.pack"
    external = [
        pdf,
        artifact,
        artifact.with_suffix(".bin"),
        artifact.with_suffix(".hex"),
        args.execution / "firmware/fixture.c",
        args.invocation,
        args.execution / "m14-erase-backend-preread.json",
        args.execution / "nrf52840-official-identity-preread.json",
    ]
    if args.board == "stm32l476":
        external += [pack, args.execution / "packs/stm32-pack-cache.json"]
    if args.baseline:
        external.append(args.baseline)
        external += list(args.baseline.parent.glob("before-*.bin"))
    if args.resume_report:
        external.append(args.resume_report)
    report["external_inputs"] = []
    with zipfile.ZipFile(evidence / "external-inputs.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for index, path in enumerate(external):
            entry = f"{index:02d}-{path.name}"
            archive.write(path, entry)
            report["external_inputs"].append(
                dict(path=str(path), entry=entry, bytes=path.stat().st_size, sha256=sha(path))
            )
    assert (
        sha(args.execution / "m14-erase-backend-preread.json")
        == "30eb0baea5a7d975e4858662e1a1153bd043320918dbf59ed73dc76cc90bddf0"
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
            name: int(elf.get_section_by_name(".symtab").get_symbol_by_name(name)[0]["st_value"])
            for name in ("repair_signature", "repair_counter")
        }
    expected = {a + i: value for a, data in segments for i, value in enumerate(data)}
    assert expected == IntelHex(str(artifact.with_suffix(".hex"))).todict()
    assert sorted(expected) == list(range(min(expected), max(expected) + 1))
    image = bytes(expected[a] for a in sorted(expected))
    assert image == artifact.with_suffix(".bin").read_bytes()
    report["fixture"] = dict(
        elf_sha256=sha(artifact),
        ranges=[dict(start=a, end=a + len(d)) for a, d in segments],
        addressed_bytes=len(image),
        image_sha256=hashlib.sha256(image).hexdigest(),
        symbols=symbols,
        qualification="ELF/HEX/BIN bytes independently agree; source file alone does not prove build provenance or executed behavior.",
    )
    if args.board == "stm32l476":
        from pyocd.target.pack.cmsis_pack import CmsisPack

        metadata = json.loads((args.execution / "packs/stm32-pack-cache.json").read_text())
        assert sha(pack) == "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
        with zipfile.ZipFile(pack) as archive:
            pdsc = archive.read("Keil.STM32L4xx_DFP.pdsc")
        assert (
            hashlib.sha256(pdsc).hexdigest()
            == "7493b22352571fc8a3efb2257d3f4c94dc932cee03ecf60ba76eb699ead1518c"
        )
        device = next(
            d
            for d in CmsisPack(io.BytesIO(pack.read_bytes())).devices
            if d.part_number == "STM32L476RGTx"
        )
        report["pack"] = dict(
            sha256=sha(pack),
            pdsc_sha256=hashlib.sha256(pdsc).hexdigest(),
            selected_device=device.part_number,
            memory=[
                dict(name=r.name, start=r.start, end=r.end + 1, flash=r.is_flash)
                for r in device.memory_map.regions
            ],
        )
        if args.stage in {"raw-inspect", "restore"}:
            # Ordinary support-data installation for the independent NO_SETUP
            # reader only. FULL uses a fresh project and its public pack form.
            import shutil

            packs = project / ".firm/packs"
            (packs / "files").mkdir(parents=True)
            shutil.copyfile(pack, packs / "files" / pack.name)
            (packs / "manifest.yaml").write_text(
                json.dumps(
                    {
                        "packs": [
                            {
                                "id": "Keil.STM32L4xx_DFP",
                                "version": "3.1.0",
                                "filename": pack.name,
                                "url": metadata["publisher_download"],
                                "sha256": sha(pack),
                                "provides_targets": [target],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config["pack"] = [str(pack)]
            (project / "pyocd.yaml").write_text(json.dumps(config), encoding="utf-8")
    save()
    observer = evidence / "worker-observer"
    observer.mkdir()
    observer_code = (
        "import atexit, hashlib, json, os, sys\nfrom pathlib import Path\n"
        "def record():\n"
        " rows=[]\n"
        " for name,module in sorted(sys.modules.items()):\n"
        "  file=getattr(module,'__file__',None)\n"
        "  if name.startswith(('pyocd_debug_mcp','pyocd.','tests.')) and file and Path(file).is_file():\n"
        "   path=Path(file).resolve(); data=path.read_bytes()\n"
        "   rows.append(dict(module=name,path=str(path),bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))\n"
        f" destination=Path({str(evidence / 'child-imports')!r}); destination.mkdir(exist_ok=True)\n"
        " (destination/(str(os.getpid())+'.json')).write_text(json.dumps(rows,indent=2),encoding='utf-8')\n"
        "atexit.register(record)\n"
    )
    if args.board == "stm32l476":
        observer_code += (
            "if 'pyocd_debug_mcp.adapters.provider_worker' in getattr(sys,'orig_argv',sys.argv):\n"
            " from pyocd_debug_mcp.adapters import swd_pyocd\n"
            " from pyocd_debug_mcp import pack_provision\n"
            " from pyocd_debug_mcp.kernel.processes import _start_token\n"
            " codes={pack_provision.verified_pack_for_target.__code__,swd_pyocd._cmsis_pack_for.__code__,swd_pyocd._quarantined_cmsis_pack.__code__}\n"
            f" destination=Path({str(evidence / 'actual-worker-pack')!r}); destination.mkdir(exist_ok=True)\n"
            " def observe(frame,event,value):\n"
            "  if frame.f_code not in codes or event!='return' or value is None: return\n"
            "  name=frame.f_code.co_name\n"
            "  if name=='_quarantined_cmsis_pack':\n"
            "   path=frame.f_locals['path']; payload=frame.f_locals['payload']; expected=frame.f_locals['expected_sha256']\n"
            "  else:\n"
            "   selected=value if name=='verified_pack_for_target' else frame.f_locals['selected']\n"
            "   path=selected.path; payload=selected.payload; expected=selected.spec.sha256\n"
            "  row=dict(pid=os.getpid(),token=_start_token(os.getpid()),function=name,path=str(path),expected=expected,payload_sha256=hashlib.sha256(payload).hexdigest())\n"
            "  with (destination/(str(os.getpid())+'.jsonl')).open('a',encoding='utf-8') as stream: stream.write(json.dumps(row)+'\\n')\n"
            " sys.setprofile(observe)\n"
        )
    (observer / "sitecustomize.py").write_text(observer_code, encoding="utf-8")
    os.environ["PYTHONPATH"] = str(observer) + os.pathsep + os.environ["PYTHONPATH"]
    os.environ["B02_PACK_SELECTION_EVIDENCE"] = str(evidence / "actual-worker-pack")
    report["executed_observer"] = dict(
        path=str(observer / "sitecustomize.py"),
        sha256=sha(observer / "sitecustomize.py"),
        pythonpath=os.environ["PYTHONPATH"],
    )
    os.chdir(project)
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.adapters.swd_process import _WorkerClient
    from pyocd_debug_mcp.kernel.processes import popen_owned
    from pyocd_debug_mcp.setup_flow import board_catalog
    from tests.m05_public_support import NULL_PLAN, PublicSetup
    from tests.tiered_acceptance_support import public_text

    worker_codes = {
        _WorkerClient.__init__.__code__,
        _WorkerClient.close.__code__,
        _WorkerClient.call.__code__,
        popen_owned.__code__,
    }
    workers = {}
    report["worker_rpc"] = []

    def observe(frame, event, value):
        if frame.f_code not in worker_codes:
            return
        if frame.f_code is popen_owned.__code__:
            if event == "return" and isinstance(value, tuple):
                process, marker = value
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
                process_info = psutil.Process(process.pid)
                environment = process_info.environ()
                entry["actual_environment"] = {
                    key: environment.get(key)
                    for key in (
                        "BYO_MCP_ARTIFACT_ROOT",
                        "BYO_MCP_MONITOR_ROOT",
                        "BYO_MANAGED_TEST_EVIDENCE",
                        "PYOCD_PROJECT_DIR",
                        "PYTHONPATH",
                    )
                }
                entry["observed_inet_listeners"] = [
                    dict(address=list(c.laddr), status=c.status)
                    for c in process_info.net_connections(kind="inet")
                    if c.status == "LISTEN"
                ]
            except (psutil.Error, OSError) as exc:
                entry["process_metadata_unavailable"] = f"{type(exc).__name__}: {exc}"

    prior_profile = threading.getprofile()
    threading.setprofile(observe)
    board = None
    attempted = False
    restoration = None

    def call(name, arguments):
        leases()
        row = dict(tool=name, arguments=arguments, started_utc=now())
        report["calls"].append(row)
        try:
            row["reply"] = public_text(server, name, arguments)
            return row["reply"]
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            row["finished_utc"] = now()
            save()

    flow = PublicSetup(server, call=call)

    def raw(name, **parameters):
        result = flow.json(name, {"board_id": board, **parameters})
        assert result["status"] == "ok", result
        return result["result"]

    def raw_read(address, width=32, length=None):
        value = raw("read_memory_raw", address=address, width=width, length=length)
        return bytes.fromhex(value) if length else int(value, 0)

    def setup_full():
        nonlocal board
        assert board_catalog._CATALOG == {}
        name = "M14 independent physical " + args.board
        overview = flow.overview(name)
        if overview["status"] == "setup_assignment_required":
            connection = next(
                c
                for c in overview["connections"]
                if c["connection_id"].rsplit(":", 1)[-1].casefold() == uid.casefold()
            )
            overview = flow.json(
                "setup_overview",
                {
                    "board_names": [name],
                    "connection_assignments": {name: connection["connection_id"]},
                },
            )
        assert overview["status"] == "setup_routes_ready", overview
        route = overview["routes"][0]
        board = route["board_id"]
        part = "nRF52840-QIAA" if args.board == "nrf52840" else "STM32L476RGT6"
        if resumed:
            call("connect", {"board_id": board, "probe_uid": uid, "target": target})
        if route["route"] == "no-setup" and "plan_action_parameters_template" not in route:
            escalation = route["optional_escalation"]
            assert "setup-full" in escalation["target_tier_choices"]
            assert escalation["next_tool"] == "board_setup-plan"
            flow.json("load_setup_tool", {"board_id": board, "tool_name": "board_setup-plan"})
            flow.call("board_setup-plan", NULL_PLAN)
            selected = next(
                c
                for c in overview["connections"]
                if c["connection_id"].rsplit(":", 1)[-1].casefold() == uid.casefold()
            )
            parameters = dict(
                mode="setup",
                target_tier="setup-full",
                connection_id=selected["connection_id"],
                display_name=route["display_name"],
                mcu_part_number=part,
                requires_uart=False,
                serial_baudrate=None,
                serial_id=None,
                datasheet_path=str(pdf),
            )
            plan = flow.plan("board_setup", board, parameters, permission="one-time")
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
                    url=metadata["publisher_download"],
                    source_path=str(pack),
                    official_sha256=sha(pack),
                    evidence=[
                        {
                            "source": "Actual retained publisher PDSC/FLM bytes",
                            "claim": "STM32L476RGTx supports user-supplied STM32L476RGT6; live group and size do not prove package.",
                        }
                    ],
                    reasoning_summary="Use exact sealed pack and qualify hardware identity separately.",
                )
            assert flow.answer(board, pending, response)["status"] == "setup_continuation_accepted"
            pending = flow.fix(plan)
        assert pending["status"] == "setup_completed", pending
        loaded = flow.json("load_setup_tool", {"board_id": board, "tool_name": "board_validate"})
        validation = flow.json(loaded["next_call"]["tool"], loaded["next_call"]["arguments"])
        assert validation["status"] == "validation_passed", validation
        report["initial_validation"] = validation
        connection = server.connection_manager.connection_for(board)
        report["connection"] = dict(
            metadata=asdict(connection.handle.metadata),
            connection_id=connection.connection_id,
            session_id=connection.runtime_session.session_id,
        )
        assert connection.handle.metadata.probe_uid == uid
        assert connection.handle.worker is not None

    def safe_read(address, width=32, length=None):
        parameters = dict(address=address, width=width, length=length)
        flow.call("read_memory_address-plan", NULL_PLAN)
        accepted = flow.plan("read_memory_address", board, parameters)
        assert accepted["status"] == "plan_accepted", accepted
        reply = call("read_memory_address", {"board_id": board, **parameters})
        return (
            bytes.fromhex(reply.splitlines()[0].split(":", 1)[-1].strip())
            if length
            else int(reply.splitlines()[0], 16)
        )

    def block(reader, address, size):
        return b"".join(
            reader(address + offset, length=min(4096, size - offset))
            for offset in range(0, size, 4096)
        )

    def program_raw(path, count, base=None):
        parameters = dict(artifact=str(path))
        if base is not None:
            parameters["base_address"] = base
        reply = raw("flash_raw", **parameters)
        assert reply["programming"]["status"] == "verified", reply
        assert reply["programming"]["verified_bytes"] == count, reply
        call("halt", {"board_id": board})
        return reply

    def flash_safe():
        flow.call("flash_application-plan", NULL_PLAN)
        plan = flow.plan("flash_application", board, {"artifact": str(artifact)})
        assert plan["status"] == "plan_accepted", plan
        reply = call("flash_application", {"board_id": board, "artifact": str(artifact)})
        assert f"read back {len(expected)} matching addressed bytes" in reply, reply
        call("halt", {"board_id": board})
        actual = b"".join(block(safe_read, a, len(data)) for a, data in segments)
        assert actual == image
        return dict(bytes=len(actual), actual_sha256=hashlib.sha256(actual).hexdigest())

    def restore_child():
        nonlocal restoration
        if restoration is not None:
            return
        argv = [
            sys.executable,
            "-B",
            "-m",
            "tests.m14_physical",
            "--board",
            args.board,
            "--stage",
            "restore",
            "--execution",
            str(args.execution),
            "--invocation",
            str(args.invocation),
            "--baseline",
            str(args.baseline),
            "--evidence",
            str(evidence / "independent-observer-restore"),
        ]
        report["restore_command"] = dict(
            argv=argv, cwd=str(project), started_utc=now(), helper_sha256=sha(__file__)
        )
        save()
        process = subprocess.Popen(
            argv, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        token = _start_token(process.pid)
        report["restore_command"].update(pid=process.pid, token=token)
        stdout, stderr = process.communicate()
        (evidence / "restore.stdout.log").write_bytes(stdout)
        (evidence / "restore.stderr.log").write_bytes(stderr)
        report["restore_command"].update(
            raw_exit=process.returncode,
            finished_utc=now(),
            exact_identity_absent=bool(token and not identity_matches(process.pid, token)),
        )
        path = evidence / "independent-observer-restore/physical.json"
        restoration = json.loads(path.read_text()) if path.exists() else {}
        report["restoration"] = dict(
            report=str(path),
            sha256=sha(path) if path.exists() else None,
            verdict=restoration.get("verdict"),
        )
        assert process.returncode == 0 and restoration.get("verdict") == "PASS", report[
            "restoration"
        ]

    try:
        if args.stage in {"raw-inspect", "restore"}:
            board = "m14_independent_identity_" + args.board
            call("connect", {"board_id": board, "probe_uid": uid, "target": target})
            connection = server.connection_manager.connection_for(board)
            report["connection"] = dict(
                metadata=asdict(connection.handle.metadata),
                connection_id=connection.connection_id,
                session_id=connection.runtime_session.session_id,
            )
            assert (
                connection.handle.metadata.probe_uid == uid and connection.handle.worker is not None
            )
        else:
            assert args.baseline is not None
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            assert baseline["verdict"] == "PASS" and baseline["board"] == args.board
            report["baseline"] = dict(
                path=str(args.baseline),
                sha256=sha(args.baseline),
                identity=baseline["physical_identity"],
                settings=baseline["settings"],
            )
            setup_full()
        call("halt", {"board_id": board})
        identity = dict(
            cpuid=(
                raw_read(0xE000ED00)
                if args.stage in {"raw-inspect", "restore"}
                else safe_read(0xE000ED00)
            )
        )
        if args.stage in {"raw-inspect", "restore"} and args.board == "nrf52840":
            identity.update(
                part=raw_read(0x10000100),
                variant=raw_read(0x10000104),
                package=raw_read(0x10000108),
                ram_kib=raw_read(0x1000010C),
                flash_kib=raw_read(0x10000110),
                page_size=raw_read(0x10000010),
                code_pages=raw_read(0x10000014),
                device_id=[raw_read(0x10000060), raw_read(0x10000064)],
            )
            identity["variant_ascii"] = identity["variant"].to_bytes(4, "big").decode("ascii")
            assert identity["part"] == 0x52840
            settings = raw_read(0x10001000, length=0x400)
            factory = raw_read(0x10000000, length=0x1000)
            (evidence / "before-uicr.bin").write_bytes(settings)
            (evidence / "before-ficr.bin").write_bytes(factory)
            report["settings"] = dict(
                uicr_sha256=hashlib.sha256(settings).hexdigest(),
                uicr_non_ff=[dict(offset=i, value=v) for i, v in enumerate(settings) if v != 255],
                ficr_sha256=hashlib.sha256(factory).hexdigest(),
            )
        elif args.stage in {"raw-inspect", "restore"}:
            identity.update(
                device_group=raw_read(0xE0042000),
                flash_kib=raw_read(0x1FFF75E0, width=16),
                device_id=[raw_read(0x1FFF7590 + i * 4) for i in range(3)],
            )
            settings = raw_read(0x40022020, length=0x30)
            (evidence / "before-flash-options.bin").write_bytes(settings)
            report["settings"] = dict(
                register_range=dict(start=0x40022020, end=0x40022050),
                bytes_hex=settings.hex(),
                sha256=hashlib.sha256(settings).hexdigest(),
                qualification="Read-only option/configuration register baseline; no option writes or claim of all nonvolatile settings.",
            )
            assert identity["device_group"] & 0xFFF == 0x415 and identity["flash_kib"] == 1024
        report["physical_identity"] = identity
        report["catalog_empty"] = board_catalog._CATALOG == {}
        if args.stage not in {"raw-inspect", "restore"}:
            assert identity["cpuid"] == baseline["physical_identity"]["cpuid"]
            report["map"] = server._safety_repository.load_current(board).to_document()
        if args.stage == "workflow":
            # Recovery must be suitable before a fresh human grant is written.
            if args.board == "nrf52840":
                chip = baseline["physical_identity"]
                assert chip["variant_ascii"][2:] <= "DZZ", (
                    "Newer protection needs a suitable recovery fixture"
                )
                assert chip["package"] == 0x2004 and chip["flash_kib"] == 1024
                saved = (args.baseline.parent / "before-uicr.bin").read_bytes()
                # This physical fixture has only reconstructable reset-pin
                # settings. No calibration/provisioning/protection is rewritten.
                assert saved[:0x200] == b"\xff" * 0x200
                assert saved[0x208:] == b"\xff" * (0x400 - 0x208)
                assert saved[0x200:0x208] == bytes.fromhex("1200000012000000")
            else:
                options = bytes.fromhex(baseline["settings"]["bytes_hex"])
                optr = int.from_bytes(options[:4], "little")
                assert optr & 255 == 0xAA and optr & (1 << 21), (
                    "Require unprotected dual-bank configuration"
                )
            report["controlled_before"] = flash_safe()
            fields = dict(
                board_id=board,
                hypothesis="Intentionally erase this recoverable owned target once.",
                hypothesis_made=True,
                strategy="Execute the exact disclosed backend mass erase once, then restore and validate.",
                strategy_evaluated=True,
                expected_fail_return="Spent grant and invalid readiness; independently restore.",
                expected_success_return="Disconnected, complete controlled bytes erased, then recoverable restoration.",
                max_calls=1,
                max_calls_buffer=0,
                action_parameters={"recovery_mechanism": "backend_mass_erase"},
                user_permission=None,
            )
            flow.call("target_unlock-plan", NULL_PLAN)
            proposal = flow.json("target_unlock-plan", fields)
            assert proposal["status"] == "unlock_permission_requested", proposal
            report["proposal"] = proposal
            # Derive independent expected disclosure from the committed setup:
            # sector coverage determines the listed physical main flash only.
            flash_ranges = report["map"]["geometry"]["physical_flash"]
            rows = report["map"]["geometry"]["erase"]["sectors"]
            expected_ranges = []
            for region in flash_ranges:
                contained = [
                    r for r in rows if r["start"] >= region["start"] and r["end"] <= region["end"]
                ]
                if contained:
                    assert min(r["start"] for r in contained) == region["start"]
                    assert max(r["end"] for r in contained) == region["end"]
                    assert (
                        sum(r["end"] - r["start"] for r in contained)
                        == region["end"] - region["start"]
                    )
                    expected_ranges.append((region["start"], region["end"]))
            disclosure = proposal["disclosure"]
            assert [(r["start"], r["end"]) for r in disclosure["erased_ranges"]] == expected_ranges
            assert disclosure["unknown_effects"] and disclosure["all_nonvolatile_erased"] is False
            manual = proposal["manual_grant"]
            assert manual["project_root"] == str(project) and manual["state"] == "locked"
            from pyocd_debug_mcp.capabilities.manual_permission_helper import unlock_manual_grant

            leases()
            report["human_authorization"] = dict(
                source="Standing user's assigned M14-T recoverable owned-board erase authorization",
                task_card_sha256=sha(Path(invocation["cwd"]) / ".agent-workspace/task-card.json"),
                at=now(),
                current_plan=proposal["plan_id"],
                live_identity=proposal["live_identity"],
                disclosure=disclosure,
                policy_digest=manual["policy_digest"],
                binding_digest=manual["binding_digest"],
            )
            unlocked = unlock_manual_grant(
                project_root=project,
                action="mass-erase",
                board_id=board,
                policy_digest=manual["policy_digest"],
                binding_digest=manual["binding_digest"],
            )
            report["human_protocol_result"] = unlocked
            assert unlocked["status"] == "manual_grant_unlocked", unlocked
            approved = flow.json("target_unlock-plan", {**fields, "user_permission": "one-time"})
            assert approved["status"] == "unlock_plan_approved", approved
            attempted = True
            report["erase_reply"] = call(
                "target_unlock", {"board_id": board, "recovery_mechanism": "backend_mass_erase"}
            )
            report["durable_after_attempt"] = json.loads(
                (
                    project / ".agent-workspace/runtime/manual-permissions/mass-erase.json"
                ).read_text()
            )
            assert report["durable_after_attempt"]["state"] == "consumed"
            assert server.gate_manager.snapshot(board) is None
            assert server.connection_manager.maybe_connection(board) is None
            rpc_before = len(report["worker_rpc"])
            flow.call("read_memory_address-plan", NULL_PLAN)
            blocked_plan = flow.plan(
                "read_memory_address", board, dict(address=segments[0][0], width=32, length=None)
            )
            refused = flow.execute(blocked_plan["stable_client_fallback"])
            assert refused["status"] == "batch_failed", refused
            assert len(report["worker_rpc"]) == rpc_before
            report["old_readiness_refusal"] = refused
            recover_rpc = [r for r in report["worker_rpc"] if r["operation"] == "recover"]
            assert len(recover_rpc) == 1, recover_rpc
            restore_child()
            # Rebind the same public FULL project to the present physical probe.
            name = "M14 independent physical " + args.board
            overview = flow.overview(name)
            if overview["status"] == "setup_assignment_required":
                selected = next(
                    c
                    for c in overview["connections"]
                    if c["connection_id"].rsplit(":", 1)[-1].casefold() == uid.casefold()
                )
                overview = flow.json(
                    "setup_overview",
                    {
                        "board_names": [name],
                        "connection_assignments": {name: selected["connection_id"]},
                    },
                )
            assert overview["routes"][0]["board_id"] == board
            call("connect", {"board_id": board})
            loaded = flow.json(
                "load_setup_tool", {"board_id": board, "tool_name": "board_validate"}
            )
            result = flow.json(loaded["next_call"]["tool"], loaded["next_call"]["arguments"])
            assert result["status"] == "validation_passed", result
            report["fresh_validation"] = result
            call("halt", {"board_id": board})
            assert b"".join(block(safe_read, a, len(data)) for a, data in segments) == image
            signature = safe_read(symbols["repair_signature"])
            expected_signature = int.from_bytes(
                bytes(expected[symbols["repair_signature"] + i] for i in range(4)), "little"
            )
            assert signature == expected_signature
            before = safe_read(symbols["repair_counter"])
            call("resume", {"board_id": board})
            time.sleep(0.1)
            call("halt", {"board_id": board})
            after = safe_read(symbols["repair_counter"])
            assert after != before
            report["workload"] = dict(
                signature=signature,
                before_counter=before,
                after_counter=after,
                qualification="Observed RAM counter progress after resume; no UART or boot-from-power proof.",
            )
        elif args.stage == "restore":
            assert args.baseline is not None
            baseline = json.loads(args.baseline.read_text())
            assert baseline["verdict"] == "PASS" and baseline["board"] == args.board
            assert identity == baseline["physical_identity"], (
                "Exact current measured chip facts changed"
            )
            observation_error = None
            try:
                measured = b"".join(block(raw_read, a, len(data)) for a, data in segments)
                (evidence / "complete-controlled-after-erase.bin").write_bytes(measured)
                assert measured == b"\xff" * len(image), (
                    "Complete controlled image range not erased"
                )
                report["affected_observation"] = dict(
                    bytes=len(measured),
                    sha256=hashlib.sha256(measured).hexdigest(),
                    all_defined_controlled_bytes_erased=True,
                )
                if args.board == "nrf52840":
                    assert factory == (args.baseline.parent / "before-ficr.bin").read_bytes()
                    assert settings == b"\xff" * 0x400
                    report["factory_4096bytes_preserved"] = True
                    report["uicr_1024bytes_observed_erased"] = True
                else:
                    assert settings == bytes.fromhex(baseline["settings"]["bytes_hex"])
                    report["defined_options_48bytes_unchanged"] = True
            except Exception:
                observation_error = traceback.format_exc()
                report["observation_error"] = observation_error
            # Restoration is attempted even if the independent observation fails.
            report["restored_programming"] = program_raw(artifact, len(image))
            assert b"".join(block(raw_read, a, len(data)) for a, data in segments) == image
            if args.board == "nrf52840":
                saved = (args.baseline.parent / "before-uicr.bin").read_bytes()
                assert saved[:0x200] == b"\xff" * 0x200 and saved[0x208:] == b"\xff" * (
                    0x400 - 0x208
                )
                reset_settings = evidence / "recoverable-reset-settings.hex"
                reset_hex = IntelHex()
                for offset, byte in enumerate(saved[0x200:0x208]):
                    reset_hex[0x10001200 + offset] = byte
                reset_hex.write_hex_file(str(reset_settings))
                report["restored_reset_settings"] = program_raw(reset_settings, 8)
                assert raw_read(0x10001000, length=0x400) == saved
                assert raw_read(0x10000000, length=0x1000) == factory
                report["uicr_complete_baseline_restored"] = True
            if observation_error:
                raise AssertionError("Restored workload, but independent erase observation failed")
        report["verdict"] = "PASS"
    except Exception:
        report["verdict"] = "FAIL"
        report["traceback"] = traceback.format_exc()
    finally:
        if attempted and restoration is None:
            try:
                if board and server.connection_manager.maybe_connection(board):
                    call("disconnect", {"board_id": board})
                restore_child()
            except Exception:
                report["cleanup_errors"].append(traceback.format_exc())
        if board and server.connection_manager.maybe_connection(board):
            try:
                call("resume", {"board_id": board})
                report["final_state"] = call("get_state", {"board_id": board})
                if report["final_state"].splitlines()[0] != "RUNNING":
                    report["cleanup_errors"].append("Intended RUNNING state was not observed")
                call("disconnect", {"board_id": board})
            except Exception:
                report["cleanup_errors"].append(traceback.format_exc())
        threading.setprofile(prior_profile)
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
                report["cleanup_errors"].append(f"Worker {pid} cleanup not proven")
        # Observe actual still-loaded server imports before interpreter unload.
        report["imports"] = []
        for name, module in sorted(sys.modules.items()):
            file = getattr(module, "__file__", None)
            if (
                name.startswith(("pyocd_debug_mcp", "tests.", "pyocd."))
                and file
                and Path(file).is_file()
            ):
                report["imports"].append(
                    dict(module=name, path=str(Path(file).resolve()), sha256=sha(file))
                )
        if report["cleanup_errors"]:
            report["verdict"] = "FAIL"
        leases()
        report["finished_utc"] = now()
        save()
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in (
                    "verdict",
                    "physical_identity",
                    "settings",
                    "traceback",
                    "cleanup_errors",
                )
            }
        )
    )
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
