"""Explicit opt-in M07 hardware proof. Requires ROOT's exact native probe leases.

Fixture preparation uses the production native adapter with an explicit target
and (for STM) the pinned pack. Exhaustion runs public MCP/production workers.
No test seams, software flash patches, manual grants, or protected writes.
"""

import argparse
import hashlib
import importlib
import json
import os
import tempfile
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB
from elftools.elf.elffile import ELFFile

from tests.tiered_acceptance_support import public_call, public_text


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", choices=("nrf52840", "stm32l476"), required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.execution = args.execution.resolve()
    uid = {"nrf52840": "683854191", "stm32l476": "066FFF514988525067233337"}[args.board]
    resource = f"hardware:{args.board}:{uid}"
    invocation = json.loads(args.invocation.read_text())
    assert resource in invocation["exclusive_resources"], "ROOT must reserve the exact probe at launch"
    report = {"board": args.board, "resource": resource, "invocation": invocation,
              "started_at": now(), "verdict": "PENDING", "calls": [], "owned": [],
              "qualification": "Live chip group/flash-size evidence is separate from selected PDSC "
              "part. CPUID/target name do not establish package or ordering code. Physical page "
              "equality is observed; physical bus write-attempt tracing is not claimed."}

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    firmware = args.execution / "firmware" / args.board
    manifest = json.loads((firmware / "fixture-manifest.json").read_text())
    report["fixture_manifest"] = manifest
    report["fixture_manifest_sha256"] = digest((firmware / "fixture-manifest.json").read_bytes())
    for name, expected in manifest["files"].items():
        assert digest((firmware / name).read_bytes()) == expected, name
    with (firmware / "fixture.elf").open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(int(s["p_paddr"]), s.data()) for s in elf.iter_segments()
                    if s["p_type"] == "PT_LOAD" and int(s["p_filesz"]) > 0]
        executable = []
        disassembler = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
        for section in elf.iter_sections():
            if int(section["sh_flags"]) & 4:
                executable.extend(i.address for i in disassembler.disasm(
                    section.data(), int(section["sh_addr"])))
    assert [{"start": address, "size": len(data), "sha256": digest(data)}
            for address, data in segments] == manifest["segments"]

    with tempfile.TemporaryDirectory(prefix=f"m07-physical-{args.board}-") as temporary:
        root = Path(temporary)
        report["temporary_project"] = str(root)
        os.environ["BYO_MCP_ARTIFACT_ROOT"] = str(root)
        os.environ["BYO_MCP_MONITOR_ROOT"] = str(root / "monitor")
        os.environ["BYO_MANAGED_TEST_EVIDENCE"] = str(args.output.parent / "managed-analysis")
        os.environ["PYOCD_PROJECT_DIR"] = str(root)
        os.environ.pop("BYO_MCP_TEST_SEAMS", None)
        os.environ.pop("BYO_MCP_TEST_FAULTS", None)
        config = {"resume_on_disconnect": False, "auto_unlock": False, "connect_mode": "halt",
                  "jlink.non_interactive": True}
        target_name = "nrf52840"
        pack_args = {}
        if args.board == "stm32l476":
            cache = json.loads((args.execution / "packs" / "stm32-pack-cache.json").read_text())
            pack = args.execution / "packs" / "Keil.STM32L4xx_DFP.3.1.0.pack"
            assert digest(pack.read_bytes()) == cache["sha256"]
            target_name = cache["pdsc_device"].lower()
            config["pack"] = [str(pack)]
            pack_args = {"pack_path": pack, "pack_sha256": cache["sha256"],
                         "pdsc_device": cache["pdsc_device"]}
            report["pack"] = cache
        # JSON is supported YAML, and this is a normal pyOCD project config.
        (root / "pyocd.yaml").write_text(json.dumps(config), encoding="utf-8")
        report["provider_project_config"] = config
        report["explicit_target"] = target_name
        initial_cwd = Path.cwd()
        handle = None
        server = None
        connection = None
        safe_resume = False
        try:
            from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface

            adapter = PyOCDSWDInterface()
            report["preparation_route"] = {
                "adapter": "PyOCDSWDInterface.open/flash (no replacement)",
                "target": target_name, "unique_id": uid,
                "pack_arguments": pack_args, "artifact": str(firmware / "fixture.elf"),
                "halt_after_reset": True,
            }
            handle = adapter.open(board=None, unique_id=uid, target=target_name, **pack_args)
            assert handle.session.probe.unique_id == uid
            assert handle.session.options.get("resume_on_disconnect") is False
            assert handle.session.options.get("auto_unlock") is False
            native = handle.session.target
            core = native.selected_core_or_raise
            adapter.halt(handle)
            identities = {"cpuid": core.ap.read32(0xE000ED00)}
            if args.board == "nrf52840":
                for name, address in (("part", 0x10000100), ("variant", 0x10000104),
                                      ("package", 0x10000108)):
                    identities[name] = core.ap.read32(address)
                assert identities["part"] == 0x52840
            else:
                identities["dbgmcu_idcode"] = core.ap.read32(0xE0042000)
                identities["flash_size_kib"] = core.ap.read16(0x1FFF75E0)
                assert identities["dbgmcu_idcode"] & 0xFFF == 0x415
            report["preparation_identity"] = identities
            report["native_target_class"] = str(type(native))
            report["native_part_number"] = native.part_number
            report["geometry"] = []
            pages = set()
            for region in core.memory_map.regions:
                if region.is_flash:
                    report["geometry"].append({"start": region.start, "length": region.length,
                                                "blocksize": region.blocksize,
                                                "algorithm": str(region.algo)})
            for address, data in segments:
                region = core.memory_map.get_region_for_address(address)
                assert region and region.is_flash and region.contains_range(address, length=len(data))
                assert region.flash is not None, "real flash algorithm required before programming"
                if args.board == "stm32l476":
                    assert region.length == identities["flash_size_kib"] * 1024
                size = region.blocksize
                for page in range(region.start + ((address - region.start) // size) * size,
                                  address + len(data), size):
                    pages.add((page, size))
            report["preparation_started_at"] = now()
            # Authorized expendable firmware preparation. No success message is
            # accepted as proof: every PT_LOAD byte is subsequently read via AP.
            report["program_result"] = adapter.flash(
                handle, firmware / "fixture.elf", halt_after_reset=True)
            adapter.halt(handle)
            report["addressed_readback"] = []
            for address, data in segments:
                actual = bytes(core.ap.read_memory_block8(address, len(data)))
                report["addressed_readback"].append({"start": address, "size": len(data),
                    "expected_sha256": digest(data), "physical_sha256": digest(actual),
                    "equal": actual == data})
                assert actual == data, "fixture programming not proven by complete AP readback"
            report["debug_recovery"] = {
                "reset_and_halt_observed": adapter.get_state(handle),
                "restorable_image_verified": True,
                "route": "explicit selected target with real flash algorithm and complete AP readback",
                "automatic_unlock": False, "mass_erase": "not invoked",
            }
            report["preparation_completed_at"] = now()
            save()
            adapter.close(handle)
            handle = None
            os.chdir(initial_cwd)

            from pyocd_debug_mcp.monitor import paths
            paths._reset_cache()
            from pyocd_debug_mcp import server as server_module
            server = importlib.reload(server_module)

            def call(tool, arguments, structured=False):
                row = {"tool": tool, "arguments": arguments, "at": now()}
                report["calls"].append(row)
                try:
                    row["result"] = (public_call if structured else public_text)(server, tool, arguments)
                except Exception as error:
                    row["error"] = f"{type(error).__name__}: {error}"
                    save()
                    raise
                save()
                return row["result"]

            def read(address, width=32):
                reply = call("read_memory_raw", {"board_id": args.board,
                    "address": address, "width": width}, True)
                assert reply["status"] == "ok", reply
                return int(reply["result"], 16)

            def block(address, length):
                data = bytearray()
                for offset in range(0, length, 4096):
                    reply = call("read_memory_raw", {"board_id": args.board,
                        "address": address + offset, "length": min(4096, length - offset)}, True)
                    assert reply["status"] == "ok", reply
                    data.extend(bytes.fromhex(reply["result"]))
                assert len(data) == length
                return bytes(data)

            report["inventory"] = asdict(server._hardware_inventory.snapshot())
            assert any(p["unique_id"] == uid for p in report["inventory"]["probes"])
            call("connect", {"board_id": args.board, "probe_uid": uid, "target": target_name})
            connection = server.connection_manager.connection_for(args.board)
            worker = connection.handle.worker
            report["session_before"] = {
                "connection_id": connection.connection_id,
                "session_id": connection.runtime_session.session_id,
                "metadata": asdict(connection.handle.metadata), "worker_pid": worker._process.pid,
            }
            assert connection.handle.metadata.probe_uid == uid
            call("halt", {"board_id": args.board})
            assert "halted" in call("get_state", {"board_id": args.board}).lower()
            if args.board == "nrf52840":
                assert read(0x10000100) == identities["part"]
            else:
                assert read(0xE0042000) == identities["dbgmcu_idcode"]
                assert read(0x1FFF75E0, 16) == identities["flash_size_kib"]
            for address, data in segments:
                assert block(address, len(data)) == data
            control = read(0xE0002000)
            count = (control >> 4) & 15 | (control >> 8) & 0x70
            assert count > 0
            comparator_addresses = [0xE0002008 + 4 * i for i in range(count)]
            initial_comparators = {address: read(address) for address in comparator_addresses}
            report["fpb"] = {"control": control, "capacity": count,
                             "initial_comparators": initial_comparators}
            # Never remove foreign comparators; stop before installing ours.
            assert all(value == 0 for value in initial_comparators.values()), "foreign comparator present"
            before = {f"{address:08x}:{size}": digest(block(address, size))
                      for address, size in sorted(pages)}
            report["page_hashes_before"] = before
            candidates = [address for address in executable
                          if any(start <= address and address + 2 <= start + len(data)
                                 for start, data in segments)]
            assert len(candidates) > count
            report["executable_addresses"] = candidates[:count + 1]
            owned = report["owned"]
            for address in candidates[:count]:
                # Record ownership intent before dispatch, including partial failures.
                owned.append(address)
                reply = call("set_breakpoint_raw", {"board_id": args.board, "address": address}, True)
                assert reply["status"] == "ok", reply
            full = {address: read(address) for address in comparator_addresses}
            report["comparators_at_capacity"] = full
            assert all(value != 0 for value in full.values())
            for repetition in range(2):
                refused = call("set_breakpoint_raw", {"board_id": args.board,
                    "address": candidates[count]}, True)
                assert refused["status"] == "error" and "capacity exhausted" in refused["message"], refused
                assert read(candidates[count], 16) == int.from_bytes(block(candidates[count], 2), "little")
                assert {address: read(address) for address in comparator_addresses} == full
                assert {f"{address:08x}:{size}": digest(block(address, size))
                        for address, size in sorted(pages)} == before
                assert server.connection_manager.connection_for(args.board) is connection
                assert connection.runtime_session.closed_at is None and worker._process.poll() is None
            report["exhaustion_repetitions"] = 2
            report["exercise_succeeded"] = True
        except Exception as error:
            report["obstacle"] = f"{type(error).__name__}: {error}"
            report["traceback"] = traceback.format_exc()
        finally:
            # Disconnect is configured never to resume. Resume is a separate
            # explicit action only after image and comparator cleanup are proved.
            if connection is not None:
                try:
                    for address in report["owned"]:
                        call("remove_breakpoint", {"board_id": args.board, "address": address})
                    final = {address: read(address) for address in comparator_addresses}
                    report["comparators_after_cleanup"] = final
                    after = {f"{address:08x}:{size}": digest(block(address, size))
                             for address, size in sorted(pages)}
                    report["page_hashes_after_cleanup"] = after
                    safe_resume = (all(value == 0 for value in final.values())
                                   and after == report.get("page_hashes_before")
                                   and report.get("exercise_succeeded") is True)
                    report["cleanup_proven"] = safe_resume
                    report["session_after"] = {
                        "connection_id": connection.connection_id,
                        "session_id": connection.runtime_session.session_id,
                        "metadata": asdict(connection.handle.metadata),
                        "worker_pid": worker._process.pid,
                    }
                    report["state_before_disconnect"] = call("get_state", {"board_id": args.board})
                    if safe_resume:
                        assert report["session_after"] == report["session_before"]
                        call("resume", {"board_id": args.board})
                        report["resumed_state"] = call("get_state", {"board_id": args.board})
                        assert "running" in report["resumed_state"].lower()
                        report["verdict"] = "PASS"
                except Exception as error:
                    report["cleanup_error"] = f"{type(error).__name__}: {error}"
                finally:
                    try:
                        call("disconnect", {"board_id": args.board})
                        report["disconnected"] = server.connection_manager.maybe_connection(args.board) is None
                        report["worker_exit"] = worker._process.poll()
                        report["worker_cleanup_confirmed"] = worker._cleanup_confirmed
                        assert report["disconnected"] and report["worker_exit"] is not None
                    except Exception as error:
                        report["disconnect_error"] = f"{type(error).__name__}: {error}"
                        report["verdict"] = "PENDING"
            if handle is not None:
                try:
                    report["preparation_final_state"] = adapter.get_state(handle)
                    adapter.close(handle)
                    report["preparation_closed"] = True
                except Exception as error:
                    report["preparation_cleanup_error"] = str(error)
            os.chdir(initial_cwd)
            report["completed_at"] = now()
            save()
    print(json.dumps({k: report.get(k) for k in
                      ("board", "verdict", "obstacle", "cleanup_error", "completed_at")}))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
