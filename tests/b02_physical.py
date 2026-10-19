"""Opt-in B02 physical public workflows; unmodified production workers, native leases.

Run prepare/full/restore serially for one board. Prepare saves one ordinary main
flash sector and installs non-erased controlled bytes outside the fixture image.
Restore is required even when full fails. No protection/calibration region is used.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
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
    parser.add_argument("--stage", choices=("prepare", "full", "restore"), required=True)
    parser.add_argument("--retry", type=int, nargs="?", const=1, default=0,
                        help="Numbered public reconnect/retry in this attempt's existing FULL project")
    parser.add_argument("--existing-full-project", type=Path,
                        help="Reuse a genuine earlier FULL project with fresh preparation/evidence")
    parser.add_argument("--board", choices=("nrf52840", "stm32l476"), required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    assert not args.retry or args.stage == "full"
    phase_label = args.stage + (f"-retry-{args.retry}" if args.retry else "")
    destination = evidence / (phase_label + ".json")
    assert not destination.exists(), "retain failed attempts under distinct evidence roots"
    assert args.existing_full_project is None or args.retry
    project = (args.existing_full_project.resolve() if args.existing_full_project
               else evidence / (args.stage + "-project"))
    retry_source = None
    if args.retry:
        previous = project.parent / "full.json"
        assert project.is_dir() and previous.is_file()
        assert json.loads(previous.read_text())["verdict"] == "FAIL"
        retry_source = dict(project=str(project), previous_report=str(previous), sha256=sha(previous))
        shutil.copytree(project / ".firm", evidence / f"before-retry-{args.retry}-public-state")
    else:
        project.mkdir(exist_ok=False)
    os.environ.update(BYO_MCP_ARTIFACT_ROOT=str(project),
                      BYO_MCP_MONITOR_ROOT=str(project / "monitor"),
                      BYO_MANAGED_TEST_EVIDENCE=str(evidence / "managed"),
                      PYOCD_PROJECT_DIR=str(project))
    # Preserve the command recorder's import observer, then observe selection
    # inside real worker children. This records returns without replacing calls.
    observer = evidence / (phase_label + "-child-observer")
    observer.mkdir()
    previous_observer = getattr(sys.modules.get("sitecustomize"), "__file__", None)
    (observer / "sitecustomize.py").write_text(
        "import runpy\n"
        + (f"runpy.run_path({str(previous_observer)!r})\n" if previous_observer else "")
        + "from tests.b02_pack_observer import install\ninstall()\n", encoding="utf-8")
    os.environ["PYTHONPATH"] = os.pathsep.join((str(observer), os.environ.get("PYTHONPATH", "")))
    os.environ["B02_PACK_SELECTION_EVIDENCE"] = str(evidence / (phase_label + "-pack-selection"))
    for name in ("BYO_MCP_TEST_SEAMS", "BYO_MCP_TEST_FAULTS"):
        os.environ.pop(name, None)
    invocation = json.loads(args.invocation.read_text())
    original_root = Path(json.loads((Path(invocation["cwd"]) / "harness/harness-config.json").read_text())["root_workspace"])
    uid = {"nrf52840": "683854191", "stm32l476": "066FFF514988525067233337"}[args.board]
    target = "nrf52840" if args.board == "nrf52840" else "stm32l476rgtx"
    sector_start, sector_size = (0x80000, 0x1000) if args.board == "nrf52840" else (0x08080000, 0x800)
    report = dict(started_at=now(), stage=args.stage, retry=args.retry, board=args.board, argv=sys.argv,
                  invocation=invocation, calls=[], workers={}, rpc=[], leases=[], verdict="PENDING",
                  project=str(project), helper_sha256=sha(__file__), cleanup_errors=[],
                  limitation="Only the complete controlled outside sector is a preservation oracle. No whole-chip, UART, application, or exact STM package proof is inferred.")
    if retry_source:
        report["retry_source"] = retry_source

    def save():
        destination.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    from tests.m02_physical_flash import creation

    def leases():
        status = json.loads(Path(invocation["paths"]["controller_status"]).read_text())
        assert status["run_id"] == invocation["run_id"] and status["controller_state"] == "running"
        controller = status["controller_identity"]
        assert creation(controller["pid"]) == controller["creation_time"]
        provider = status["provider_state"]
        assert creation(provider["pid"]) == provider["creation_time"]
        required = {"hardware:nrf52840:683854191", "hardware:stm32l476:066FFF514988525067233337"}
        assert required.issubset(invocation["exclusive_resources"])
        owned = []
        for path in (original_root / ".harness-runtime/resources/leases").glob("*.lease"):
            lease = json.loads(path.read_text())
            if lease["resource_id"] in required:
                assert lease["run_id"] == invocation["run_id"] and lease["lane_id"] == invocation["lane_id"]
                assert lease["pid"] == controller["pid"] and lease["creation_time"] == controller["creation_time"]
                owned.append(lease | {"file_sha256": sha(path)})
        assert required == {r["resource_id"] for r in owned}
        report["leases"].append(dict(at=now(), leases=owned, controller=controller, provider=provider))
        save()

    leases()
    config = {"auto_unlock": False, "resume_on_disconnect": False,
              "connect_mode": "halt", "jlink.non_interactive": True}
    if args.board == "stm32l476":
        pack = args.execution / "packs/Keil.STM32L4xx_DFP.3.1.0.pack"
        metadata = json.loads((args.execution / "packs/stm32-pack-cache.json").read_text())
        assert sha(pack) == metadata["sha256"]
        with zipfile.ZipFile(pack) as archive:
            pdsc = archive.read(metadata["pdsc_name"])
        assert hashlib.sha256(pdsc).hexdigest() == metadata["pdsc_sha256"]
        import io
        from pyocd.target.pack.cmsis_pack import CmsisPack
        device = next(d for d in CmsisPack(io.BytesIO(pack.read_bytes())).devices if d.part_number == "STM32L476RGTx")
        region = device.memory_map.get_region_for_address(sector_start)
        assert region.is_flash and list(region.flm.sector_sizes) == [(0, 0x800)]
        report["pack"] = metadata | {"actual_sha256": sha(pack), "actual_pdsc_sha256": hashlib.sha256(pdsc).hexdigest(),
                                     "sector_sizes": list(region.flm.sector_sizes)}
        if args.stage != "full":
            # Only raw preparation/restoration installs ordinary support data.
            # Fresh FULL setup must qualify it through the public pack form;
            # copying this raw registry there would conflict with that receipt.
            pack_dir = project / ".firm/packs"
            (pack_dir / "files").mkdir(parents=True)
            shutil.copyfile(pack, pack_dir / "files" / pack.name)
            (pack_dir / "manifest.yaml").write_text(json.dumps({"packs": [{
                "id": "Keil.STM32L4xx_DFP", "version": "3.1.0", "filename": pack.name,
                "url": metadata["publisher_download"], "sha256": sha(pack), "provides_targets": [target]}]}))
            config["pack"] = [str(pack)]
    configuration = project / "pyocd.yaml"
    if configuration.exists():
        assert json.loads(configuration.read_text()) == config
    else:
        configuration.write_text(json.dumps(config))
    os.chdir(project)
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.kernel.processes import _start_token, identity_matches, popen_owned
    from pyocd_debug_mcp.adapters.swd_process import _WorkerClient
    import psutil
    from tests.m05_public_support import PublicSetup, NULL_PLAN
    from tests.tiered_acceptance_support import public_text
    from elftools.elf.elffile import ELFFile
    from intelhex import IntelHex

    artifact = (args.firmware / args.board / "fixture.elf").resolve()
    with artifact.open("rb") as stream:
        elf = ELFFile(stream)
        loads = [(int(s["p_paddr"]), s.data()) for s in elf.iter_segments() if s["p_type"] == "PT_LOAD" and s["p_filesz"]]
        symbols = {n: int(elf.get_section_by_name(".symtab").get_symbol_by_name(n)[0]["st_value"])
                   for n in ("repair_signature", "repair_counter", "repair_step_probe")}
    expected = {a+i: value for a, data in loads for i, value in enumerate(data)}
    assert expected == IntelHex(str(artifact.with_suffix(".hex"))).todict()
    assert sorted(expected) == list(range(min(expected), max(expected)+1))
    image = bytes(expected[a] for a in sorted(expected))
    assert image == artifact.with_suffix(".bin").read_bytes()
    assert all(not (a <= sector_start < a+len(d) or sector_start <= a < sector_start+sector_size) for a, d in loads)
    report["artifact"] = dict(path=str(artifact), sha256=sha(artifact), addressed_bytes=len(expected),
                              addressed_sha256=hashlib.sha256(image).hexdigest(), symbols=symbols,
                              source_sha256=sha(args.firmware / "fixture.c"))
    artifact_snapshot = evidence / (phase_label + "-artifact-inputs")
    artifact_snapshot.mkdir()
    for path in (artifact, artifact.with_suffix(".hex"), artifact.with_suffix(".bin"), args.firmware / "fixture.c"):
        shutil.copyfile(path, artifact_snapshot / path.name)
    workers = {}
    children = {}
    report["known_children"] = {}
    owned_code = popen_owned.__code__
    worker_codes = {id(_WorkerClient.__init__.__code__), id(_WorkerClient.call.__code__), id(_WorkerClient.close.__code__)}

    def observe(frame, event, arg):
        if frame.f_code is owned_code:
            if event == "call":
                leases()
            elif event == "return" and isinstance(arg, tuple) and len(arg) == 2:
                process, marker = arg
                token = _start_token(process.pid)
                record = dict(pid=process.pid, token=token, observed_at=now(), argv=process.args)
                try:
                    actual_environment = psutil.Process(process.pid).environ()
                    record["actual_environment"] = {key: actual_environment.get(key) for key in (
                        "BYO_MCP_ARTIFACT_ROOT", "BYO_MCP_MONITOR_ROOT", "BYO_MANAGED_TEST_EVIDENCE", "PYOCD_PROJECT_DIR")}
                except (psutil.Error, OSError) as exc:
                    record["environment_unavailable"] = f"{type(exc).__name__}: {exc}"
                if marker and Path(marker).exists():
                    record["marker"] = json.loads(Path(marker).read_text())
                children[process.pid] = process
                report["known_children"][str(process.pid)] = record
        if id(frame.f_code) not in worker_codes:
            return
        instance = frame.f_locals.get("self")
        if type(instance).__name__ != "_WorkerClient":
            return
        name = frame.f_code.co_name
        if event == "call" and name == "call":
            leases()
            report["rpc"].append(dict(at=now(), operation=frame.f_locals.get("operation"), arguments=frame.f_locals.get("arguments")))
        if event == "return" and name in {"__init__", "close"} and hasattr(instance, "_process"):
            process = instance._process
            token = _start_token(process.pid)
            entry = report["workers"].setdefault(str(process.pid), dict(pid=process.pid, token=token, observed_at=now()))
            workers[process.pid] = instance
            if name == "close":
                entry.update(closed_at=now(), raw_exit=process.poll(), cleanup_confirmed=instance._cleanup_confirmed)

    old_profile = threading.getprofile()
    threading.setprofile(observe)
    board = None
    dirty = False

    def call(name, arguments):
        leases()
        row = dict(tool=name, arguments=arguments, started_at=now())
        report["calls"].append(row)
        try:
            row["reply"] = public_text(server, name, arguments)
            return row["reply"]
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            row["finished_at"] = now()
            save()

    flow = PublicSetup(server, call=call)

    def raw(name, **args):
        value = flow.json(name, {"board_id": board, **args})
        assert value["status"] == "ok", value
        return value["result"]

    def read(address, width=32, length=None):
        params = dict(address=address, width=width, length=length)
        if args.stage == "full":
            flow.call("read_memory_address-plan", NULL_PLAN)
            accepted = flow.plan("read_memory_address", board, params)
            assert accepted["status"] == "plan_accepted", accepted
            reply = call("read_memory_address", {"board_id": board, **params})
            if length is None:
                return int(re.findall(r"0x[0-9a-fA-F]+", reply.splitlines()[0])[-1], 16)
            # Production block response is hex bytes, retained in raw wire.
            return bytes.fromhex(reply.splitlines()[0].split(":", 1)[-1].strip())
        value = raw("read_memory_raw", **params)
        return bytes.fromhex(value) if length else int(value, 0)

    def block(address, size):
        return b"".join(read(address+offset, length=min(4096, size-offset)) for offset in range(0, size, 4096))

    def breakpoint_plan():
        flow.call("set_breakpoint-plan", NULL_PLAN)
        accepted = flow.plan("set_breakpoint", board, {"symbol_or_address": "repair_step_probe", "elf_artifact": str(artifact)})
        assert accepted["status"] == "plan_accepted", accepted
        return accepted

    try:
        if args.stage in {"prepare", "restore"}:
            board = "b02_raw_" + args.board
            assert "Connected" in call("connect", {"board_id": board, "probe_uid": uid, "target": target})
        else:
            name = "B02 physical " + args.board
            overview = flow.overview(name)
            if overview["status"] == "setup_assignment_required":
                chosen = next(c for c in overview["connections"] if c["connection_id"].rsplit(":", 1)[-1].casefold() == uid.casefold())
                overview = flow.json("setup_overview", {"board_names": [name], "connection_assignments": {name: chosen["connection_id"]}})
            assert overview["status"] == "setup_routes_ready", overview
            route = overview["routes"][0]
            board = route["board_id"]
            report["entry_route"] = route
            if route["route"] == "validate":
                assert "Connected" in call("connect", {"board_id": board})
            elif args.retry or args.board == "nrf52840":
                # Installed-target setup can promote this public connection.
                # STM requests its qualified pack through disconnected setup.
                call("connect", {"board_id": board, "probe_uid": uid, "target": target})
            pdf = args.execution / "vendor-documents" / ("nRF52840-PS-v1.1.pdf" if args.board == "nrf52840" else "STM32L476xx-DS10198.pdf")
            report["datasheet"] = dict(path=str(pdf), sha256=sha(pdf))
            if route["route"] != "validate":
                part = "nRF52840-QIAA" if args.board == "nrf52840" else "STM32L476RGT6"
                if route["route"] == "no-setup" and "plan_action_parameters_template" not in route:
                    # Existing explicit raw profiles advertise optional escalation.
                    # Copy ordinary action data from the live public overview and
                    # all-NULL plan contract, without editing retained authority.
                    escalation = route["optional_escalation"]
                    assert "setup-full" in escalation["target_tier_choices"]
                    tool = escalation["next_tool"]
                    assert tool == "board_setup-plan"
                    flow.json("load_setup_tool", {"board_id": board, "tool_name": tool})
                    call(tool, NULL_PLAN)
                    chosen = next(c for c in overview["connections"]
                                  if c["connection_id"].rsplit(":", 1)[-1].casefold() == uid.casefold())
                    parameters = dict(mode="setup", target_tier="setup-full", connection_id=chosen["connection_id"],
                                      display_name=route["display_name"], mcu_part_number=part,
                                      requires_uart=False, serial_baudrate=None, serial_id=None, datasheet_path=str(pdf))
                    accepted = flow.plan("board_setup", board, parameters, permission="one-time")
                    assert accepted["status"] == "plan_accepted", accepted
                else:
                    _, accepted = flow.accept(route, pdf, part, tier="setup-full")
                pending = flow.start(accepted)
                if pending["status"] == "setup_research_required":
                    if args.board == "nrf52840":
                        response = flow.target_answer(pending, target)
                    else:
                        response = dict(pending["accepted_response"]["response"])
                        response.update(pack_id="Keil.STM32L4xx_DFP", version="3.1.0", filename=pack.name,
                                        url=metadata["publisher_download"], source_path=str(pack),
                                        official_sha256=sha(pack), evidence=[{
                                            "source": "Retained official Keil STM32L4xx_DFP PDSC and FLM bytes",
                                            "claim": "STM32L476RGTx supports the supplied STM32L476RGT6 selection; chip group and size do not prove its package."}],
                                        reasoning_summary="Verified publisher pack bytes and device leaf; preserve the user-supplied order code and qualify live evidence separately.")
                    assert flow.answer(board, pending, response)["status"] == "setup_continuation_accepted"
                    pending = flow.fix(accepted)
                assert pending["status"] == "setup_completed", pending
            if route["route"] == "validate":
                flow.json(route["load_call"]["tool"], route["load_call"]["arguments"])
                validation = flow.json(route["next_call"]["tool"], route["next_call"]["arguments"])
            else:
                loaded = flow.json("load_setup_tool", {"board_id": board, "tool_name": "board_validate"})
                validation = flow.json(loaded["next_call"]["tool"], loaded["next_call"]["arguments"])
            assert validation["status"] == "validation_passed", validation
            report["validation"] = validation
        connection = server.connection_manager.connection_for(board)
        report["connection"] = dict(connection_id=connection.connection_id, session_id=connection.runtime_session.session_id,
                                     metadata=asdict(connection.handle.metadata))
        assert connection.handle.metadata.probe_uid == uid
        assert connection.handle.worker is not None
        assert report["workers"]
        for pid in report["workers"]:
            assert report["known_children"][pid]["actual_environment"]["BYO_MCP_ARTIFACT_ROOT"] == str(project)
        call("halt", {"board_id": board})
        identity = {"cpuid": read(0xE000ED00)}
        if args.stage == "full":
            preparation = json.loads((evidence / "prepare.json").read_text())
            assert preparation["verdict"] == "PASS"
            assert identity["cpuid"] == preparation["physical_identity"]["cpuid"]
            identity["fresh_raw_chip_measurements"] = preparation["physical_identity"]
            report["raw_identity_source"] = dict(path=str(evidence / "prepare.json"), sha256=sha(evidence / "prepare.json"))
            report["canary_inputs"] = {name: dict(path=str(evidence / name), sha256=sha(evidence / name))
                                       for name in ("sector-original.bin", "sector-controlled.bin")}
        elif args.board == "nrf52840":
            identity.update(part=read(0x10000100), variant=read(0x10000104), package=read(0x10000108),
                            code_page_size=read(0x10000010), code_pages=read(0x10000014))
            assert identity["part"] == 0x52840
            assert identity["code_page_size"] == sector_size
        else:
            identity.update(device_id=read(0xE0042000), flash_kib=read(0x1FFF75E0, width=16))
            assert identity["device_id"] & 0xFFF == 0x415 and identity["flash_kib"] == 1024
        report["physical_identity"] = identity
        if args.stage in {"prepare", "restore"}:
            artifact_bin = evidence / ("sector-controlled.bin" if args.stage == "prepare" else "sector-original.bin")
            if args.stage == "prepare":
                original = block(sector_start, sector_size)
                (evidence / "sector-original.bin").write_bytes(original)
                controlled = bytes((i*37+0x53) & 255 for i in range(sector_size))
                assert controlled != original and controlled != b"\xff" * sector_size
                artifact_bin.write_bytes(controlled)
                dirty = True
            expected_sector = artifact_bin.read_bytes()
            assert len(expected_sector) == sector_size
            raw("flash_raw", artifact=str(artifact_bin), base_address=sector_start)
            call("halt", {"board_id": board})
            actual = block(sector_start, sector_size)
            assert actual == expected_sector
            report["outside_sector"] = dict(start=sector_start, size=sector_size, actual_sha256=hashlib.sha256(actual).hexdigest(), expected_sha256=sha(artifact_bin))
        else:
            state = server._capability_policies.resolve(board)
            assert state.tier.value == "setup-full"
            assert state.map_snapshot["deployment_policy"] == {"kind": "none"}
            before_policy = state.map_snapshot
            outside_before = block(sector_start, sector_size)
            assert outside_before == (evidence / "sector-controlled.bin").read_bytes()
            before_image = [(a, block(a, len(data))) for a, data in loads]
            accepted = breakpoint_plan()
            before_rpc = len(report["rpc"])
            refusal = flow.execute(accepted["stable_client_fallback"])
            assert refusal["status"] == "batch_failed" and refusal["completed"] == [], refusal
            assert "has no application deployment allocation yet" in str(refusal), refusal
            assert "flash_application-plan" in str(refusal) and "flash_application" in str(refusal)
            assert report["rpc"][before_rpc:] == [], "refused breakpoint must not reach a production worker"
            assert server._capability_policies.resolve(board).map_snapshot == before_policy
            assert before_image == [(a, block(a, len(data))) for a, data in loads]
            flow.json("load_setup_tool", {"board_id": board, "tool_name": "board_safety_refresh"})
            assert flow.json("board_safety_refresh", {"board_id": board})["status"] == "safety_refresh_completed"
            assert server._capability_policies.resolve(board).map_snapshot["deployment_policy"] == {"kind": "none"}
            assert "has no application deployment allocation yet" in str(flow.execute(breakpoint_plan()["stable_client_fallback"]))
            flow.call("flash_application-plan", NULL_PLAN)
            accepted = flow.plan("flash_application", board, {"artifact": str(artifact)})
            assert accepted["status"] == "plan_accepted", accepted
            deployed = call("flash_application", {"board_id": board, "artifact": str(artifact)})
            assert f"read back {len(expected)} matching addressed bytes" in deployed, deployed
            call("halt", {"board_id": board})
            assert [(a, block(a, len(data))) for a, data in loads] == loads
            after = server._capability_policies.resolve(board).map_snapshot
            assert after["partitions"]["application"] is not None and after["deployment_policy"]["kind"] != "none"
            report["persisted_allocation"] = after
            # Freeze the core so the RAM scalar has stable independently read bytes.
            report["symbol_equality"] = []
            for name in ("repair_signature", "repair_counter"):
                address = symbols[name]
                address_value = read(address)
                args_symbol = {"board_id": board, "symbol": name, "width": 32, "elf_artifact": str(artifact)}
                direct = call("read_memory_symbol", args_symbol)
                direct_value = int(re.search(r"value=(0x[0-9a-fA-F]+)", direct)[1], 16)
                batched = flow.json("action_batch", {"board_id": board, "actions": [{"tool_name": "read_memory_symbol", "arguments": args_symbol}]})
                assert batched["status"] == "batch_completed", batched
                batch_value = int(re.search(r"value=(0x[0-9a-fA-F]+)", str(batched))[1], 16)
                assert direct_value == batch_value == address_value
                if name == "repair_signature":
                    assert address_value == int.from_bytes(bytes(expected[address+i] for i in range(4)), "little")
                report["symbol_equality"].append(dict(symbol=name, elf_address=address, address_value=address_value, direct_value=direct_value, batch_value=batch_value))
            accepted = breakpoint_plan()
            reply = call("set_breakpoint", {"board_id": board, "symbol_or_address": "repair_step_probe", "elf_artifact": str(artifact)})
            pc = symbols["repair_step_probe"] & ~1
            assert f"0x{pc:08X}" in reply
            call("resume", {"board_id": board})
            assert "halted" in call("get_state", {"board_id": board}).lower()
            actual_pc = call("read_execution_state", {"board_id": board, "name": "pc"})
            assert int(re.findall(r"0x[0-9a-fA-F]+", actual_pc.splitlines()[0])[-1], 16) == pc
            report["breakpoint_hit"] = dict(expected_pc=pc, actual_reply=actual_pc)
            call("remove_breakpoint", {"board_id": board, "address": pc})
            outside_after = block(sector_start, sector_size)
            assert outside_after == outside_before
            report["outside_preserved"] = dict(start=sector_start, size=sector_size, before_sha256=hashlib.sha256(outside_before).hexdigest(), after_sha256=hashlib.sha256(outside_after).hexdigest())
        call("resume", {"board_id": board})
        report["final_state"] = call("get_state", {"board_id": board})
        assert "running" in report["final_state"].lower()
        report["verdict"] = "PASS"
    except Exception:
        report["verdict"] = "FAIL"
        report["traceback"] = traceback.format_exc()
        # A failed sentinel preparation restores its own saved sector immediately.
        if dirty and server.connection_manager.maybe_connection(board):
            try:
                raw("flash_raw", artifact=str(evidence / "sector-original.bin"), base_address=sector_start)
                report["prepare_failure_restored"] = True
            except Exception:
                report["cleanup_errors"].append(traceback.format_exc())
    finally:
        if board and server.connection_manager.maybe_connection(board):
            try:
                if args.stage == "full":
                    call("remove_breakpoint", {"board_id": board, "address": symbols["repair_step_probe"] & ~1})
                call("resume", {"board_id": board})
                report["cleanup_state"] = call("get_state", {"board_id": board})
                call("disconnect", {"board_id": board})
            except Exception:
                report["cleanup_errors"].append(traceback.format_exc())
        leases()
        threading.setprofile(old_profile)
        for pid, worker in workers.items():
            entry = report["workers"][str(pid)]
            token = entry["token"]
            entry.update(raw_exit=worker._process.poll(), cleanup_confirmed=worker._cleanup_confirmed,
                         exact_identity_absent=bool(token and not identity_matches(pid, token)))
            if not entry["exact_identity_absent"] or entry["raw_exit"] is None or not entry["cleanup_confirmed"]:
                report["cleanup_errors"].append(f"Worker {pid} cleanup not proven")
        for pid, process in children.items():
            entry = report["known_children"][str(pid)]
            entry.update(raw_exit=process.poll(), exact_identity_absent=bool(entry["token"] and not identity_matches(pid, entry["token"])))
            if not entry["exact_identity_absent"] or entry["raw_exit"] is None:
                report["cleanup_errors"].append(f"Known child {pid} absence not proven")
        report["selected_worker_cleanup"] = []
        observed = [json.loads(line) for path in (evidence / (phase_label + "-pack-selection")).glob("*.jsonl")
                    for line in path.read_text().splitlines()]
        for pid, token in sorted({(row["pid"], row["token"]) for row in observed}):
            absent = bool(token and not identity_matches(pid, token))
            report["selected_worker_cleanup"].append(dict(pid=pid, token=token, exact_identity_absent=absent))
            if not absent:
                report["cleanup_errors"].append(f"Actual selected worker {pid} absence not proven")
        if report["cleanup_errors"]:
            report["verdict"] = "FAIL"
        report["finished_at"] = now()
        save()
    print(json.dumps({"report": str(destination), "verdict": report["verdict"], "traceback": report.get("traceback"), "cleanup_errors": report["cleanup_errors"]}))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
