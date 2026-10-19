"""ROOT-authorized public recovery continuation; never repeats mass erase."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import zipfile


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("raw", "full"), required=True)
    parser.add_argument("--prior", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    for key in ("prior", "baseline", "invocation", "execution", "evidence"):
        setattr(args, key, getattr(args, key).resolve())
    args.evidence.mkdir(parents=True, exist_ok=False)
    prior = json.loads(args.prior.read_text())
    baseline = json.loads(args.baseline.read_text())
    invocation = json.loads(args.invocation.read_text())
    assert prior["verdict"] == "FAIL" and prior["erase_reply"]
    assert prior["durable_after_attempt"]["state"] == "consumed"
    assert prior["baseline"]["sha256"] == sha(args.baseline)
    observation_path = Path(prior["restoration"]["report"])
    assert sha(observation_path) == prior["restoration"]["sha256"]
    observation = json.loads(observation_path.read_text())
    assert observation["affected_observation"]["all_defined_controlled_bytes_erased"]
    project = args.evidence / "raw-project" if args.phase == "raw" else Path(prior["project"])
    project.mkdir(exist_ok=args.phase == "full")
    os.environ.update(
        BYO_MCP_ARTIFACT_ROOT=str(project),
        PYOCD_PROJECT_DIR=str(project),
        BYO_MCP_MONITOR_ROOT=str(args.evidence / "monitor"),
        BYO_MANAGED_TEST_EVIDENCE=str(args.evidence / "managed"),
        PYOCD_MCP_RUNS_ROOT=str(args.evidence / "runs"),
    )
    for key in ("BYO_MCP_TEST_SEAMS", "BYO_MCP_TEST_FAULTS", "PYOCD_TARGET", "PYOCD_PROBE_UID"):
        os.environ.pop(key, None)
    configuration = {
        "auto_unlock": False,
        "resume_on_disconnect": False,
        "connect_mode": "halt",
        "jlink.non_interactive": True,
    }
    if args.phase == "raw":
        (project / "pyocd.yaml").write_text(json.dumps(configuration))
    else:
        assert json.loads((project / "pyocd.yaml").read_text()) == configuration
    uid = "683854191"
    original = Path(
        json.loads((Path(invocation["cwd"]) / "harness/harness-config.json").read_text())[
            "root_workspace"
        ]
    )
    saved_uicr_path = args.baseline.parent / "before-uicr.bin"
    saved_ficr_path = args.baseline.parent / "before-ficr.bin"
    saved_uicr, saved_ficr = saved_uicr_path.read_bytes(), saved_ficr_path.read_bytes()
    assert sha(saved_uicr_path) == baseline["settings"]["uicr_sha256"]
    assert sha(saved_ficr_path) == baseline["settings"]["ficr_sha256"]
    assert len(saved_uicr) == 1024 and len(saved_ficr) == 4096
    artifact = args.execution / "firmware/nrf52840/fixture.elf"
    report = dict(
        started_utc=now(),
        argv=sys.argv,
        phase=args.phase,
        cwd=str(Path.cwd()),
        prior=dict(path=str(args.prior), sha256=sha(args.prior)),
        baseline=dict(path=str(args.baseline), sha256=sha(args.baseline)),
        original_erase_plan=prior["proposal"]["plan_id"],
        project=str(project),
        verdict="PENDING",
        calls=[],
        workers={},
        leases=[],
        cleanup_errors=[],
        authority="ROOT assignment2455d6cb2ead4b3487dc6eaf569ebb91: only original PSELRESET 32-bit words via public NVMC CONFIG/READY protocol; no erase/protection/calibration writes",
    )
    destination = args.evidence / "continuation.json"

    def save():
        destination.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    with zipfile.ZipFile(
        args.evidence / "external-inputs.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        report["external_inputs"] = []
        files = [
            args.prior,
            args.baseline,
            observation_path,
            args.invocation,
            saved_uicr_path,
            saved_ficr_path,
            artifact,
            artifact.with_suffix(".hex"),
            artifact.with_suffix(".bin"),
            project / "pyocd.yaml",
        ]
        files += list(
            (Path(prior["project"]) / ".agent-workspace/runtime/manual-permissions").glob("*.json")
        )
        for index, path in enumerate(files):
            entry = f"{index:02d}-{path.name}"
            archive.write(path, entry)
            report["external_inputs"].append(dict(path=str(path), entry=entry, sha256=sha(path)))

    from elftools.elf.elffile import ELFFile
    from intelhex import IntelHex
    from tests.m02_physical_flash import creation
    from pyocd_debug_mcp.kernel.processes import _start_token, identity_matches, popen_owned

    def leases():
        status = json.loads(Path(invocation["paths"]["controller_status"]).read_text())
        assert status["run_id"] == invocation["run_id"] and status["controller_state"] == "running"
        controller, provider = status["controller_identity"], status["provider_state"]
        assert creation(controller["pid"]) == controller["creation_time"]
        assert creation(provider["pid"]) == provider["creation_time"]
        required = {"hardware:nrf52840:683854191", "hardware:stm32l476:066FFF514988525067233337"}
        assert required.issubset(invocation["exclusive_resources"])
        rows = []
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
                rows.append(row | dict(sha256=sha(path)))
        assert {row["resource_id"] for row in rows} == required
        report["leases"].append(dict(at=now(), leases=rows))

    leases()
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
    expected = {a + i: byte for a, data in segments for i, byte in enumerate(data)}
    assert expected == IntelHex(str(artifact.with_suffix(".hex"))).todict()
    image = bytes(expected[a] for a in sorted(expected))
    assert image == artifact.with_suffix(".bin").read_bytes()
    assert hashlib.sha256(image).hexdigest() == prior["fixture"]["image_sha256"]
    os.chdir(project)
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.adapters.swd_process import _WorkerClient
    from tests.m05_public_support import NULL_PLAN, PublicSetup
    from tests.tiered_acceptance_support import public_text

    workers = {}
    codes = {popen_owned.__code__, _WorkerClient.__init__.__code__, _WorkerClient.close.__code__}

    def observe(frame, event, value):
        if frame.f_code not in codes or event != "return":
            return
        if frame.f_code is popen_owned.__code__:
            if isinstance(value, tuple):
                process, marker = value
                report["workers"].setdefault(
                    str(process.pid),
                    dict(pid=process.pid, token=_start_token(process.pid), argv=process.args),
                )
            return
        worker = frame.f_locals.get("self")
        if hasattr(worker, "_process"):
            workers[worker._process.pid] = worker

    previous_profile = threading.getprofile()
    threading.setprofile(observe)
    board = (
        "m14_bounded_nrf_recovery"
        if args.phase == "raw"
        else prior["proposal"]["live_identity"]["board_id"]
    )

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
        response = flow.json(name, {"board_id": board, **parameters})
        assert response["status"] == "ok", response
        return response["result"]

    def read(address, width=32, length=None):
        if args.phase == "raw":
            value = raw("read_memory_raw", address=address, width=width, length=length)
            return bytes.fromhex(value) if length else int(value, 0)
        parameters = dict(address=address, width=width, length=length)
        flow.call("read_memory_address-plan", NULL_PLAN)
        plan = flow.plan("read_memory_address", board, parameters)
        assert plan["status"] == "plan_accepted", plan
        reply = call("read_memory_address", {"board_id": board, **parameters})
        return (
            bytes.fromhex(reply.splitlines()[0].split(":", 1)[-1].strip())
            if length
            else int(reply.splitlines()[0], 16)
        )

    def block(address, count):
        return b"".join(
            read(address + offset, length=min(4096, count - offset))
            for offset in range(0, count, 4096)
        )

    def write(address, value):
        return raw("write_memory_raw", address=address, value=value, width=32)

    def ready():
        start = time.monotonic()
        deadline = start + 5.0  # Recovery-helper bound; installed pyOCD uses 15 seconds.
        polls = []
        while True:
            value = read(0x4001E400)
            polls.append(dict(at=now(), value=value))
            if value != 0:
                report.setdefault("ready_polls", []).append(
                    dict(started_monotonic=start, polls=polls)
                )
                return
            if time.monotonic() >= deadline:
                report.setdefault("ready_polls", []).append(
                    dict(started_monotonic=start, polls=polls)
                )
                raise TimeoutError(
                    "Public NVMC READY polling exceeded the helper's five-second bound"
                )

    try:
        if args.phase == "raw":
            call("connect", {"board_id": board, "probe_uid": uid, "target": "nrf52840"})
        else:
            name = "M14 independent physical nrf52840"
            overview = flow.overview(name)
            if overview["status"] == "setup_assignment_required":
                selected = next(
                    c
                    for c in overview["connections"]
                    if c["connection_id"].rsplit(":", 1)[-1] == uid
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
            report["fresh_validation"] = flow.json(
                loaded["next_call"]["tool"], loaded["next_call"]["arguments"]
            )
            assert report["fresh_validation"]["status"] == "validation_passed"
        connection = server.connection_manager.connection_for(board)
        assert connection.handle.metadata.probe_uid == uid and connection.handle.worker is not None
        report["connection"] = dict(
            session_id=connection.runtime_session.session_id, metadata=connection.handle.metadata
        )
        call("halt", {"board_id": board})
        assert call("get_state", {"board_id": board}).splitlines()[0] == "HALTED"
        if args.phase == "raw":
            identity = dict(
                cpuid=read(0xE000ED00),
                part=read(0x10000100),
                variant=read(0x10000104),
                package=read(0x10000108),
                ram_kib=read(0x1000010C),
                flash_kib=read(0x10000110),
                page_size=read(0x10000010),
                code_pages=read(0x10000014),
                device_id=[read(0x10000060), read(0x10000064)],
            )
            identity["variant_ascii"] = identity["variant"].to_bytes(4, "big").decode("ascii")
            report["fresh_identity"] = identity
            assert identity == baseline["physical_identity"]
            current_uicr, current_ficr = block(0x10001000, 1024), block(0x10000000, 4096)
            (args.evidence / "before-uicr.bin").write_bytes(current_uicr)
            (args.evidence / "before-ficr.bin").write_bytes(current_ficr)
            assert current_ficr == saved_ficr
            assert (
                current_uicr[:0x200] == saved_uicr[:0x200]
                and current_uicr[0x208:] == saved_uicr[0x208:]
            )
            before_image = b"".join(block(a, len(data)) for a, data in segments)
            (args.evidence / "before-fixture.bin").write_bytes(before_image)
            for offset in (0x200, 0x204):
                current = int.from_bytes(current_uicr[offset : offset + 4], "little")
                desired = int.from_bytes(saved_uicr[offset : offset + 4], "little")
                assert current & desired == desired, "Restoration would require erase; stop"
            for offset in (0x200, 0x204):
                desired = int.from_bytes(saved_uicr[offset : offset + 4], "little")
                if read(0x10001000 + offset) == desired:
                    continue
                try:
                    write(0x4001E504, 1)
                    ready()
                    write(0x10001000 + offset, desired)
                    ready()
                finally:
                    write(0x4001E504, 0)
                    ready()
            after_uicr, after_ficr = block(0x10001000, 1024), block(0x10000000, 4096)
            (args.evidence / "after-uicr.bin").write_bytes(after_uicr)
            (args.evidence / "after-ficr.bin").write_bytes(after_ficr)
            assert after_uicr == saved_uicr and after_ficr == saved_ficr
            report["complete_settings_restored"] = dict(
                uicr_bytes=1024,
                uicr_sha256=sha(args.evidence / "after-uicr.bin"),
                ficr_bytes=4096,
                ficr_sha256=sha(args.evidence / "after-ficr.bin"),
            )
            if before_image != image:
                restored = raw("flash_raw", artifact=str(artifact))
                assert restored["programming"]["status"] == "verified" and restored["programming"][
                    "verified_bytes"
                ] == len(image)
                report["necessary_fixture_reprogramming"] = restored
                call("halt", {"board_id": board})
            raw("reset_and_halt_raw")
        actual_image = b"".join(block(a, len(data)) for a, data in segments)
        (args.evidence / "after-fixture.bin").write_bytes(actual_image)
        assert actual_image == image
        report["complete_fixture_verified"] = dict(
            bytes=len(image), sha256=sha(args.evidence / "after-fixture.bin")
        )
        before = read(symbols["repair_counter"])
        call("resume", {"board_id": board})
        time.sleep(0.1)
        report["actual_running_state"] = call("get_state", {"board_id": board})
        assert report["actual_running_state"].splitlines()[0] == "RUNNING"
        call("halt", {"board_id": board})
        after = read(symbols["repair_counter"])
        assert after != before
        report["workload_progress"] = dict(
            before=before,
            after=after,
            qualification="Observed RAM counter progress; no UART or power-cycle startup claim",
        )
        call("resume", {"board_id": board})
        report["final_state"] = call("get_state", {"board_id": board})
        assert report["final_state"].splitlines()[0] == "RUNNING"
        report["verdict"] = "PASS"
    except Exception:
        report["verdict"] = "FAIL"
        report["traceback"] = traceback.format_exc()
    finally:
        if server.connection_manager.maybe_connection(board):
            try:
                call("disconnect", {"board_id": board})
            except Exception:
                report["cleanup_errors"].append(traceback.format_exc())
        threading.setprofile(previous_profile)
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
        report["imports"] = [
            dict(
                module=name, path=str(Path(module.__file__).resolve()), sha256=sha(module.__file__)
            )
            for name, module in sorted(sys.modules.items())
            if name.startswith(("pyocd_debug_mcp", "tests.", "pyocd."))
            and getattr(module, "__file__", None)
            and Path(module.__file__).is_file()
        ]
        if report["cleanup_errors"]:
            report["verdict"] = "FAIL"
        report["finished_utc"] = now()
        save()
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in (
                    "verdict",
                    "traceback",
                    "cleanup_errors",
                    "complete_settings_restored",
                    "complete_fixture_verified",
                    "actual_running_state",
                )
            }
        )
    )
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
