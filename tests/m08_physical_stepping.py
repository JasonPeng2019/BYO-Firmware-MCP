"""Opt-in M08-T decoded instruction proof through public tools and real workers.

Requires both exact native leases. Uses a fresh public LITE setup, existing
firmware-v2, and a pinned public pack continuation. No test seams, copied policy,
manual grants, programming, reset, UART, or protection/configuration writes.
The measured PUSH effects are read independently and its stack/context restored.
The step control removes its BP explicitly. A second ordinary public session
disconnects with our BP still installed. Read-only native AP observations at
core-disconnect return distinguish actual cleanup from later initialization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import zipfile
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
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    campaign, evidence = args.campaign.resolve(), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    project = evidence / "project"
    project.mkdir()
    # Select every mutable root BEFORE any product or test-support imports.
    os.environ.update(BYO_MCP_ARTIFACT_ROOT=str(project),
                      BYO_MCP_MONITOR_ROOT=str(evidence / "monitor"),
                      BYO_MANAGED_TEST_EVIDENCE=str(evidence / "managed"),
                      PYOCD_PROJECT_DIR=str(project))
    close_config = evidence / "close-observation-config.json"
    close_output = evidence / "native-close-observations"
    os.environ["M08T_PHYSICAL_CLOSE_CONFIG"] = str(close_config)
    close_config.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    os.environ.pop("BYO_MCP_TEST_SEAMS", None)
    os.environ.pop("BYO_MCP_TEST_FAULTS", None)
    # Ordinary local pyOCD session options, with no authority or pack pre-staging.
    (project / "pyocd.yaml").write_text(json.dumps({
        "auto_unlock": False, "resume_on_disconnect": True, "connect_mode": "halt",
        "jlink.non_interactive": True}), encoding="utf-8")
    invocation = json.loads(args.invocation.read_text(encoding="utf-8"))
    runtime = campaign.parents[1]
    uid = {"nrf52840": "683854191", "stm32l476": "066FFF514988525067233337"}[args.board]
    required = {"hardware:nrf52840:683854191", "hardware:stm32l476:066FFF514988525067233337"}
    assert set(invocation["exclusive_resources"]) == required
    report = {"started_utc": now(), "argv": sys.argv, "cwd": str(Path.cwd()),
              "helper": {"path": __file__, "sha256": sha(__file__)}, "invocation": invocation,
              "native_session": os.environ.get("CODEX_SESSION_ID"), "board": args.board,
              "environment_before_product_imports": {key: os.environ.get(key) for key in (
                  "BYO_MCP_ARTIFACT_ROOT", "BYO_MCP_MONITOR_ROOT", "BYO_MANAGED_TEST_EVIDENCE",
                  "PYOCD_PROJECT_DIR", "M08T_PHYSICAL_CLOSE_CONFIG", "UV_PROJECT_ENVIRONMENT",
                  "PYTHONPATH", "M08T_OBSERVE_PHYSICAL")},
              "uid": uid, "calls": [], "leases": [], "steps": [], "verdict": "PENDING",
              "qualification": "STM group/size/pack supports compatibility, not exact package/order. "
              "RUNNING proves no application/UART health or whole-chip preservation. "
              "C01 is tested on AP boundaries, with no deliberate physical fault induction."}

    def save():
        (evidence / "proof.json").write_text(json.dumps(report, indent=2, default=str) + "\n",
                                             encoding="utf-8")

    from pyocd_debug_mcp.kernel.processes import identity_matches, _start_token

    def audit():
        status = json.loads((args.invocation.parent / "controller.status.json").read_text())
        assert status["run_id"] == invocation["run_id"]
        assert status["controller_state"] == status["provider_state"]["state"] == "running"
        controller, provider = status["controller_identity"], status["provider_state"]
        for process in (controller, provider):
            birth = int(process["creation_time"].split(":", 1)[1])
            assert identity_matches(process["pid"], f"win:{birth:016x}")
        leases = [json.loads(p.read_text()) for p in (runtime / "resources/leases").glob("*.lease")]
        assert {lease["resource_id"] for lease in leases} == required
        for lease in leases:
            assert lease["run_id"] == invocation["run_id"]
            assert lease["lane_id"] == invocation["lane_id"]
            assert lease["pid"] == controller["pid"]
            assert lease["creation_time"] == controller["creation_time"]
        report["leases"].append({"utc": now(), "controller": controller, "provider": provider,
                                 "both_alive": True, "leases": leases})

    audit()
    save()
    # Independently compare ELF load bytes, HEX addressed bytes and BIN, without
    # using the product's image derivation or trusting the manifest's symbols.
    from elftools.elf.elffile import ELFFile
    from intelhex import IntelHex

    firmware = campaign / "checks/m02-validate-r01-fallback/firmware-v2" / args.board
    manifest = json.loads((firmware / "fixture-manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert sha(firmware / name) == expected
    with (firmware / "fixture.elf").open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(int(s["p_paddr"]), s.data()) for s in elf.iter_segments()
                    if s["p_type"] == "PT_LOAD" and int(s["p_filesz"]) > 0]
        symbols = {s.name: int(s["st_value"]) for s in elf.get_section_by_name(".symtab").iter_symbols()}
        code = elf.get_section_by_name(".text")
        text_address, text_bytes = int(code["sh_addr"]), code.data()
    addressed = {a + i: byte for a, data in segments for i, byte in enumerate(data)}
    hex_image = IntelHex(str(firmware / "fixture.hex"))
    assert {a: hex_image[a] for a in hex_image.addresses()} == addressed
    assert len(addressed) == 5692
    assert bytes(addressed[a] for a in range(min(addressed), max(addressed) + 1)) == (
        firmware / "fixture.bin").read_bytes()
    probe_pc = symbols["repair_step_probe"] & ~1
    encoding = int.from_bytes(bytes(addressed[probe_pc + i] for i in range(2)), "little")
    assert encoding & 0xFE00 == 0xB400, "this helper measures Thumb PUSH T1 only"
    pushed = [f"r{r}" for r in range(8) if encoding >> r & 1]
    if encoding & 0x100:
        pushed.append("lr")
    assert pushed == ["r7", "lr"] and encoding == 0xB580
    expected_pc, stack_size = probe_pc + 2, 4 * len(pushed)
    signature = sum(addressed[symbols["repair_signature"] + i] << (i * 8) for i in range(4))
    assert signature == 0xB10F0301
    report["firmware"] = {"manifest_sha256": sha(firmware / "fixture-manifest.json"),
        "files": {name: sha(firmware / name) for name in manifest["files"]},
        "source_sha256": sha(firmware.parent / "fixture.c"), "addressed_bytes": len(addressed),
        "symbols": {n: symbols[n] for n in ("repair_step_probe", "repair_signature", "repair_counter")},
        "signature": signature, "segments": [{"start": a, "length": len(b),
        "sha256": hashlib.sha256(b).hexdigest()} for a, b in segments]}
    report["decode"] = {"pc": probe_pc, "encoding": f"{encoding:04X}", "size": 2,
                        "instruction": "push {r7, lr}", "pushed": pushed,
                        "expected_pc": expected_pc, "sp_delta": -stack_size,
                        "reference": "Arm DDI0403E.e A7.7.101 PUSH T1"}

    def close_phase(phase, *, enabled=True):
        close_config.write_text(json.dumps({"enabled": enabled, "phase": phase,
            "output": str(close_output), "segments": report["firmware"]["segments"]}),
            encoding="utf-8")

    def closed_target_observation(phase, live_tree, expected_slots):
        rows = [json.loads(line) for path in close_output.glob("*-close.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()]
        rows = [row for row in rows if row["phase"] == phase]
        assert len(rows) == 1, rows
        row = rows[0]
        assert any(p["pid"] == row["pid"] and p["creation_time"] == row["creation_time"]
                   for p in live_tree), (row, live_tree)
        assert row["observed"] and row["resume_argument"] is True, row
        assert row["comparators"] == {str(k): v for k, v in expected_slots.items()}, row
        assert row["pending_addresses"] == row["realized_addresses"] == [], row
        assert row["dhcsr"] & ((1 << 17) | 1) == 0, row  # S_HALT=0, C_DEBUGEN=0
        assert all(image["matches_expected"] for image in row["addressed_images"]), row
        return row
    (evidence / "text-before-expected.bin").write_bytes(text_bytes)
    save()

    # Vendor evidence is input to a real public setup continuation.
    execution = campaign / "execution"
    pdf = execution / "vendor-documents" / (
        "nRF52840-PS-v1.1.pdf" if args.board == "nrf52840" else "STM32L476xx-DS10198.pdf")
    assert sha(pdf) == {"nrf52840": "c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422",
                        "stm32l476": "a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65"}[args.board]
    text_path = evidence / "vendor-datasheet.txt"
    extract_argv = ["pdftotext", "-layout", str(pdf), str(text_path)]
    extract_start = now()
    extract = subprocess.run(extract_argv, capture_output=True, text=True, check=False)
    report["document"] = {"path": str(pdf), "sha256": sha(pdf), "argv": extract_argv,
        "started_utc": extract_start, "ended_utc": now(), "raw_exit": extract.returncode,
        "stdout": extract.stdout, "stderr": extract.stderr}
    assert extract.returncode == 0
    pages = text_path.read_text(encoding="utf-8").split("\f")
    ram_pages = [i + 1 for i, page in enumerate(pages)
                 if any(a in page for a in ("0x2000", "0x20000000", "0x2001", "0x2004"))]
    assert ram_pages
    report["document"].update(text_sha256=sha(text_path),
                             ram_pages=[{"pdf_page": p, "text": pages[p - 1]} for p in ram_pages])
    pack = execution / "packs/Keil.STM32L4xx_DFP.3.1.0.pack"
    if args.board == "stm32l476":
        assert sha(pack) == "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
        with zipfile.ZipFile(pack) as archive:
            pdsc = archive.read("Keil.STM32L4xx_DFP.pdsc")
        assert hashlib.sha256(pdsc).hexdigest() == "7493b22352571fc8a3efb2257d3f4c94dc932cee03ecf60ba76eb699ead1518c"
        report["pack"] = {"path": str(pack), "sha256": sha(pack),
                          "pdsc_sha256": hashlib.sha256(pdsc).hexdigest(),
                          "selected_leaf": "STM32L476RGTx", "exact_package_proven": False}

    from tests.m05_public_support import PublicSetup
    from tests.tiered_acceptance_support import public_text
    from pyocd_debug_mcp import server
    import psutil

    observed_workers = {}
    previous_profile = threading.getprofile()

    def observe_worker(frame, event, arg):
        if event != "return" or frame.f_code.co_name not in {"__init__", "close"}:
            return
        instance = frame.f_locals.get("self")
        if type(instance).__name__ != "_WorkerClient" or not hasattr(instance, "_process"):
            return
        process = instance._process
        record = observed_workers.setdefault(process.pid, {"pid": process.pid,
            "creation_time": _start_token(process.pid), "observed_utc": now()})
        if frame.f_code.co_name == "close":
            record.update(raw_close_exit=process.poll(), cleanup_confirmed=instance._cleanup_confirmed,
                          closed_utc=now())

    threading.setprofile(observe_worker)

    board, connection, worker = None, None, None
    worker_token = None
    owned, comparator_addresses = [], []
    context, saved_stack, baseline = None, None, None
    restored, exercise_complete = False, False

    def call(tool, arguments):
        audit()
        row = {"tool": tool, "arguments": arguments, "started_utc": now()}
        report["calls"].append(row)
        save()
        try:
            row["reply"] = public_text(server, tool, arguments)
            return row["reply"]
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            row["ended_utc"] = now()
            save()

    def raw(tool, **arguments):
        reply = json.loads(call(tool, {"board_id": board, **arguments}).splitlines()[0])
        assert reply["status"] == "ok", reply
        return reply

    def read(address, width=32):
        return int(raw("read_memory_raw", address=address, width=width)["result"], 0)

    def block(address, length):
        data = bytearray()
        for offset in range(0, length, 4096):
            data.extend(bytes.fromhex(raw("read_memory_raw", address=address + offset,
                                         length=min(length - offset, 4096))["result"]))
        assert len(data) == length
        return bytes(data)

    def reg(name):
        tool = "read_cpu_register" if re.fullmatch(r"r\d+", name) else "read_execution_state"
        return int(re.search(r"0x[0-9a-fA-F]+", call(tool, {"board_id": board, "name": name}))[0], 16)

    def comparators():
        return {address: read(address) for address in comparator_addresses}

    def state():
        return call("get_state", {"board_id": board}).splitlines()[0].strip()

    def restore_context():
        nonlocal restored
        if context is None or saved_stack is None:
            return
        call("halt", {"board_id": board})
        for offset in range(0, stack_size, 4):
            raw("write_memory_raw", address=context["sp"] - stack_size + offset,
                value=int.from_bytes(saved_stack[offset:offset + 4], "little"), width=32)
        raw("write_cpu_register_raw", name="sp", value=context["sp"])
        raw("write_cpu_register_raw", name="pc", value=context["pc"])
        assert {name: reg(name) for name in context} == context
        assert block(context["sp"] - stack_size, stack_size) == saved_stack
        restored = True

    flow = PublicSetup(server, call=call)
    try:
        route = flow.overview("M08 independent " + args.board)
        if route["status"] == "setup_assignment_required":
            chosen = next(p for p in route["connections"]
                          if p["connection_id"].rsplit(":", 1)[-1].casefold() == uid.casefold())
            route = flow.json("setup_overview", {
                "board_names": ["M08 independent " + args.board], "connection_assignments": {
                    "M08 independent " + args.board: chosen["connection_id"]}})
        assert route["status"] == "setup_routes_ready", route
        route = route["routes"][0]
        board = route["board_id"]
        target = "nrf52840" if args.board == "nrf52840" else "stm32l476rgtx"
        if args.board == "nrf52840":
            call("connect", {"board_id": board, "probe_uid": uid, "target": target})
        _, plan = flow.accept(route, pdf, "nRF52840-QIAA" if args.board == "nrf52840" else "STM32L476RGT6")
        pending = flow.start(plan)
        assert pending["status"] == "setup_research_required", pending
        if args.board == "nrf52840":
            response = flow.target_answer(pending, target)
        else:
            response = dict(pending["accepted_response"]["response"])
            response.update(pack_id="Keil.STM32L4xx_DFP", version="3.1.0", filename=pack.name,
                url="https://www.keil.com/pack/Keil.STM32L4xx_DFP.3.1.0.pack", source_path=str(pack),
                official_sha256=sha(pack), evidence=[{"source": "Pinned Keil PDSC device leaf",
                "claim": "STM32L476RGTx is compatible with the supplied STM32L476RGT6 selection; exact package is not inferred."}],
                reasoning_summary="Use retained supported pack; qualify group/size separately from package.")
        assert flow.answer(board, pending, response)["status"] == "setup_continuation_accepted"
        pending = flow.fix(plan)
        assert pending["first_unverified_phase"] == "safety_research", pending
        response = flow.ram_answer(pending, start=0x20001000, end=0x20002000,
            pages=ram_pages,
            note="Documented SRAM subrange for public LITE setup only; instruction tests explicitly use public raw controls.")
        assert flow.answer(board, pending, response)["accepted"] == "lite_confirmation"
        assert flow.fix(plan)["status"] == "setup_completed"
        route = flow.overview("M08 independent " + args.board)["routes"][0]
        loaded = flow.json(route["load_call"]["tool"], route["load_call"]["arguments"])
        validation = flow.json(loaded["next_call"]["tool"], loaded["next_call"]["arguments"])
        assert validation["status"] == "validation_passed", validation
        report["validation"] = validation
        connection = server.connection_manager.connection_for(board)
        worker = connection.handle.worker
        worker_token = _start_token(worker._process.pid)
        assert worker_token is not None
        report["worker"] = {"pid": worker._process.pid, "creation_time": worker_token,
            "metadata": asdict(connection.handle.metadata), "connection_id": connection.connection_id,
            "session_id": connection.runtime_session.session_id}
        report["worker"]["live_tree"] = [{"pid": p.pid, "creation_time": _start_token(p.pid),
            "executable": p.exe(), "argv": p.cmdline()}
            for p in [psutil.Process(worker._process.pid),
                      *psutil.Process(worker._process.pid).children(recursive=True)]]
        assert connection.handle.metadata.probe_uid.casefold() == uid.casefold()
        assert connection.handle.metadata.target_override.casefold() == target
        call("halt", {"board_id": board})
        assert state() == "HALTED"
        identity = {"cpuid": read(0xE000ED00)}
        if args.board == "nrf52840":
            identity.update(part=read(0x10000100), variant=read(0x10000104),
                            package=read(0x10000108), device_id=[read(0x10000060), read(0x10000064)])
            assert identity["part"] == 0x52840
        else:
            identity.update(group=read(0xE0042000), flash_kib=read(0x1FFF75E0, 16),
                            device_id=[read(0x1FFF7590 + 4 * i) for i in range(3)])
            assert identity["group"] & 0xFFF == 0x415 and identity["flash_kib"] == 1024
        report["live_identity"] = identity
        for address, expected in segments:
            actual = block(address, len(expected))
            (evidence / f"image-before-{address:08x}.bin").write_bytes(actual)
            assert actual == expected, "current deployed firmware-v2 must match before execution"
        assert read(symbols["repair_signature"]) == 0xB10F0301
        assert block(probe_pc, 2) == encoding.to_bytes(2, "little")
        fp_ctrl = read(0xE0002000)
        count = (fp_ctrl >> 4) & 15 | (fp_ctrl >> 8) & 0x70
        assert count > 0
        comparator_addresses = [0xE0002008 + 4 * i for i in range(count)]
        baseline = comparators()
        assert not any(baseline.values()), "do not touch another owner's installed comparator"
        report["fpb"] = {"control": fp_ctrl, "baseline": baseline}
        owned.append(probe_pc)
        raw("set_breakpoint_raw", address=probe_pc)
        installed = comparators()
        assert sum(installed[a] != baseline[a] for a in baseline) == 1
        call("resume", {"board_id": board})
        deadline = time.monotonic() + 3
        while state() != "HALTED":
            assert time.monotonic() < deadline, "owned breakpoint did not halt"
            time.sleep(0.01)
        assert reg("pc") == probe_pc
        assert read(0xE000ED30) & 2  # DFSR.BKPT before the measured step
        context = {name: reg(name) for name in ("pc", "sp", "r7", "lr", "xpsr")}
        assert context["xpsr"] & 0x1FF == 0, "measure ordinary thread instruction only"
        assert 0x20000000 + stack_size <= context["sp"] <= (
            0x20040000 if args.board == "nrf52840" else 0x20018000)
        saved_stack = block(context["sp"] - stack_size, stack_size)
        report["context_before"] = context
        (evidence / "stack-original.bin").write_bytes(saved_stack)
        save()
        for with_breakpoint in (True, False):
            assert state() == "HALTED" and reg("pc") == probe_pc
            before_slots = comparators()
            if not with_breakpoint:
                assert before_slots == baseline
            restored = False
            step = {"owned_breakpoint": with_breakpoint, "started_utc": now(),
                    "before": {n: reg(n) for n in context}, "comparators_before": before_slots}
            report["steps"].append(step)
            step["reply"] = call("step", {"board_id": board})
            step["after"] = {n: reg(n) for n in context}
            step["state"] = state()
            step["dfsr"], step["dhcsr"] = read(0xE000ED30), read(0xE000EDF0)
            step["comparators_after"] = comparators()
            stack = block(context["sp"] - stack_size, stack_size)
            (evidence / f"stack-after-owned-{with_breakpoint}.bin").write_bytes(stack)
            step["stack_hex"] = stack.hex()
            step["ended_utc"] = now()
            save()
            assert step["reply"].splitlines()[0] == (
                f"Stepped. pc=0x{expected_pc:08X} state=HALTED stop_reason=single_step")
            assert step["state"] == "HALTED" and step["dhcsr"] & (1 << 17)
            assert step["dfsr"] == 1
            assert step["after"] == {**context, "pc": expected_pc, "sp": context["sp"] - stack_size}
            assert stack == b"".join(context[r].to_bytes(4, "little") for r in pushed)
            assert step["comparators_after"] == before_slots
            restore_context()
            if with_breakpoint:
                call("remove_breakpoint", {"board_id": board, "address": probe_pc})
                owned.remove(probe_pc)
                assert comparators() == baseline
        assert block(text_address, len(text_bytes)) == text_bytes
        for address, expected in segments:
            actual = block(address, len(expected))
            (evidence / f"image-after-{address:08x}.bin").write_bytes(actual)
            assert actual == expected
        report["code_and_addressed_image_unchanged"] = True
        report["comparators_clean"] = comparators() == baseline
        assert report["comparators_clean"]
        exercise_complete = True
        report["verdict"] = "PASS"
    except Exception:
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr)
        report["verdict"] = "FAIL"
    finally:
        if board is not None and server.connection_manager.maybe_connection(board) is not None:
            # Preserve the original failure; never turn cleanup into a step result.
            try:
                if context is not None and not restored:
                    restore_context()
                for address in list(owned):
                    call("remove_breakpoint", {"board_id": board, "address": address})
                    owned.remove(address)
                if baseline is not None:
                    assert comparators() == baseline
                if exercise_complete and restored:
                    call("resume", {"board_id": board})
                    report["final_state"] = state()
                    assert report["final_state"] == "RUNNING"
                else:
                    call("halt", {"board_id": board})
                    report["final_state"] = state()
            except Exception:
                report["cleanup_error"] = traceback.format_exc()
                report["verdict"] = "FAIL"
            try:
                close_phase("no-breakpoint-control")
                call("disconnect", {"board_id": board})
                report["no_breakpoint_disconnect"] = closed_target_observation(
                    "no-breakpoint-control", report["worker"]["live_tree"], baseline)
                close_phase("between-workflows", enabled=False)
            except Exception:
                report["disconnect_error"] = traceback.format_exc()
                report["verdict"] = "FAIL"
        if worker is not None:
            report["worker"].update(raw_close_exit=worker._process.poll(),
                cleanup_confirmed=worker._cleanup_confirmed,
                exact_identity_absent=not identity_matches(worker._process.pid, worker_token))
            if (worker._process.poll() is None or not worker._cleanup_confirmed
                    or not report["worker"]["exact_identity_absent"]):
                report["verdict"] = "FAIL"
            for process in report["worker"]["live_tree"]:
                process["exact_identity_absent"] = not identity_matches(process["pid"], process["creation_time"])
                if not process["exact_identity_absent"]:
                    report["verdict"] = "FAIL"
        threading.setprofile(previous_profile)
        report["all_observed_workers"] = [dict(record, exact_identity_absent=(
            record["creation_time"] is not None and not identity_matches(pid, record["creation_time"])))
            for pid, record in observed_workers.items()]
        for record in report["all_observed_workers"]:
            if (not record["exact_identity_absent"] or not record.get("cleanup_confirmed")
                    or record.get("raw_close_exit") is None):
                report["verdict"] = "FAIL"
        report["context_and_stack_restored"] = restored
        report["connected_after"] = board is not None and server.connection_manager.maybe_connection(board) is not None
        report["finished_utc"] = now()
        save()

    # The first session establishes decoded stepping and explicit-remove/no-BP
    # controls. This fresh ordinary session measures implicit owned-BP removal.
    if report["verdict"] == "PASS":
        implicit_worker, implicit_token = None, None
        implicit = {"started_utc": now(), "explicit_remove_before_disconnect": False}
        report["implicit_owned_disconnect"] = implicit
        threading.setprofile(observe_worker)
        implicit_owns_bp = False
        try:
            route = flow.overview("M08 independent " + args.board)["routes"][0]
            loaded = flow.json(route["load_call"]["tool"], route["load_call"]["arguments"])
            validation = flow.json(loaded["next_call"]["tool"], loaded["next_call"]["arguments"])
            assert validation["status"] == "validation_passed", validation
            implicit["fresh_validation"] = validation
            connection = server.connection_manager.connection_for(board)
            implicit_worker = connection.handle.worker
            implicit_token = _start_token(implicit_worker._process.pid)
            assert implicit_token is not None
            implicit["worker"] = {"pid": implicit_worker._process.pid,
                "creation_time": implicit_token, "metadata": asdict(connection.handle.metadata),
                "session_id": connection.runtime_session.session_id,
                "live_tree": [{"pid": p.pid, "creation_time": _start_token(p.pid),
                               "executable": p.exe(), "argv": p.cmdline()}
                              for p in [psutil.Process(implicit_worker._process.pid),
                                        *psutil.Process(implicit_worker._process.pid).children(recursive=True)]]}
            assert connection.handle.metadata.probe_uid.casefold() == uid.casefold()
            assert connection.handle.metadata.target_override.casefold() == target
            call("halt", {"board_id": board})
            assert state() == "HALTED"
            assert comparators() == baseline
            assert block(probe_pc, 2) == encoding.to_bytes(2, "little")
            implicit_owns_bp = True
            raw("set_breakpoint_raw", address=probe_pc)
            implicit["installed_comparators"] = comparators()
            assert sum(implicit["installed_comparators"][a] != baseline[a] for a in baseline) == 1
            call("resume", {"board_id": board})
            deadline = time.monotonic() + 3
            while state() != "HALTED":
                assert time.monotonic() < deadline, "implicit-close owned breakpoint did not halt"
                time.sleep(0.01)
            assert reg("pc") == probe_pc
            implicit["state_before"] = state()
            implicit["pc_before"] = reg("pc")
            implicit["dfsr_before"] = read(0xE000ED30)
            assert implicit["dfsr_before"] & 2
            assert comparators() == implicit["installed_comparators"]
            close_phase("implicit-owned-breakpoint")
            call("disconnect", {"board_id": board})
            implicit_owns_bp = False
            implicit["post_core_disconnect"] = closed_target_observation(
                "implicit-owned-breakpoint", implicit["worker"]["live_tree"], baseline)
            implicit["final_known_state"] = "RUNNING, debug disabled (post-disconnect AP observation)"
            implicit["connected_after"] = server.connection_manager.maybe_connection(board) is not None
            assert not implicit["connected_after"]
            implicit["verdict"] = "PASS"
        except Exception:
            implicit["error"] = traceback.format_exc()
            report["verdict"] = implicit["verdict"] = "FAIL"
            print(implicit["error"], file=sys.stderr)
        finally:
            close_phase("finished", enabled=False)
            if server.connection_manager.maybe_connection(board) is not None:
                try:
                    if implicit_owns_bp:
                        call("remove_breakpoint", {"board_id": board, "address": probe_pc})
                        implicit["failure_cleanup_explicit_remove"] = True
                    assert comparators() == baseline
                    call("resume", {"board_id": board})
                    assert state() == "RUNNING"
                    call("disconnect", {"board_id": board})
                except Exception:
                    implicit["failure_cleanup_error"] = traceback.format_exc()
                    report["verdict"] = "FAIL"
            if implicit_worker is not None:
                implicit["worker"].update(raw_close_exit=implicit_worker._process.poll(),
                    cleanup_confirmed=implicit_worker._cleanup_confirmed,
                    exact_identity_absent=not identity_matches(implicit_worker._process.pid, implicit_token))
                for process in implicit["worker"]["live_tree"]:
                    process["exact_identity_absent"] = not identity_matches(process["pid"], process["creation_time"])
                if (implicit_worker._process.poll() is None or not implicit_worker._cleanup_confirmed
                        or not all(p["exact_identity_absent"] for p in implicit["worker"]["live_tree"])):
                    report["verdict"] = "FAIL"
            threading.setprofile(previous_profile)
            report["all_observed_workers"] = [dict(record, exact_identity_absent=(
                record["creation_time"] is not None and not identity_matches(pid, record["creation_time"])))
                for pid, record in observed_workers.items()]
            if not all(row["exact_identity_absent"] and row.get("cleanup_confirmed")
                       and row.get("raw_close_exit") is not None for row in report["all_observed_workers"]):
                report["verdict"] = "FAIL"
            implicit["finished_utc"] = report["finished_utc"] = now()
            report["connected_after"] = server.connection_manager.maybe_connection(board) is not None
            save()
    print(json.dumps({key: report.get(key) for key in ("board", "verdict", "error", "cleanup_error", "worker")}))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
