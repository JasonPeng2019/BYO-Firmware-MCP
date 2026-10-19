"""Opt-in M02 public production-worker programming/readback under native leases.

No seams, fake policies, manual grants or direct programming. Exact-target raw
programming is distinct from guarded setup prerequisites and from application
behavior. All output, project state, failures and binary reads are retained.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import traceback
import zipfile

from elftools.elf.elffile import ELFFile
from intelhex import IntelHex
import psutil
from pyocd.target.pack.cmsis_pack import CmsisPack


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def creation(pid):
    """Exact native Windows creation identity, avoiding PID-only lease checks."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess")
    times = [wintypes.FILETIME() for _ in range(4)]
    try:
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (
            ctypes.POINTER(wintypes.FILETIME),
        ) * 4
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise OSError(ctypes.get_last_error(), "GetProcessTimes")
        value = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return f"windows-filetime:{value}"
    finally:
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", choices=("nrf52840", "stm32l476"), required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    invocation = json.loads(args.invocation.read_text(encoding="utf-8"))
    workspace = Path(invocation["cwd"])
    native_root = Path(
        json.loads((workspace / "harness/harness-config.json").read_text())["root_workspace"]
    )
    output = args.output.resolve()
    assert not output.exists(), "retain every prior physical attempt"
    output.parent.mkdir(parents=True, exist_ok=True)
    root = output.with_name(output.stem + "-project")
    root.mkdir()
    uid = {"nrf52840": "683854191", "stm32l476": "066FFF514988525067233337"}[args.board]
    target = "nrf52840" if args.board == "nrf52840" else "stm32l476rgtx"
    report = dict(
        board=args.board,
        uid=uid,
        target=target,
        started_at=now(),
        verdict="PENDING",
        invocation=invocation,
        calls=[],
        workers=[],
        project_root=str(root),
        cleanup_errors=[],
        lease_checks=[],
        qualification="Raw supported programming and addressed-byte readback only. "
        "STM device group/size plus vendor leaf is not exact package/order proof. "
        "Two complete outside sectors are measured, not whole-chip integrity or zero bus-write proof. "
        "Reset/RUNNING is not application or UART evidence. No guarded setup is fabricated.",
    )
    source = Path(__file__).read_bytes()
    snapshot = output.with_suffix(".script.py")
    snapshot.write_bytes(source)
    report["script"] = {
        "path": str(Path(__file__).resolve()),
        "snapshot": str(snapshot),
        "sha256": sha(source),
    }

    def save():
        output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    def leases():
        required = {"hardware:nrf52840:683854191", "hardware:stm32l476:066FFF514988525067233337"}
        assert required.issubset(invocation["exclusive_resources"])
        status = json.loads(Path(invocation["paths"]["controller_status"]).read_text())
        assert status["run_id"] == invocation["run_id"] and status["controller_state"] == "running"
        rows = []
        for path in (native_root / ".harness-runtime/resources/leases").glob("*.lease"):
            item = json.loads(path.read_text())
            if item["run_id"] == invocation["run_id"]:
                assert item["lane_id"] == invocation["lane_id"]
                assert item["pid"] == status["controller_identity"]["pid"]
                assert item["creation_time"] == creation(item["pid"])
                rows.append(item | {"file_sha256": sha(path.read_bytes())})
        assert required.issubset({r["resource_id"] for r in rows})
        report["lease_checks"].append({"at": now(), "leases": rows})
        save()

    leases()
    artifact = args.firmware / args.board / "fixture.elf"
    with artifact.open("rb") as stream:
        elf = ELFFile(stream)
        loads = tuple(
            (int(s["p_paddr"]), s.data())
            for s in elf.iter_segments()
            if s["p_type"] == "PT_LOAD" and s["p_filesz"]
        )
    # Independent ELF/HEX/BIN cross-check. None uses production FlashImage or
    # programmer-return data, and the manifest is not itself the byte oracle.
    expected = {address + i: value for address, data in loads for i, value in enumerate(data)}
    reference_hex = IntelHex(str(artifact.with_suffix(".hex")))
    assert expected == reference_hex.todict()
    assert expected and sorted(expected) == list(range(min(expected), max(expected) + 1))
    expected_bytes = bytes(expected[a] for a in sorted(expected))
    assert expected_bytes == artifact.with_suffix(".bin").read_bytes()
    report["artifact"] = {
        "path": str(artifact),
        "sha256": sha(artifact.read_bytes()),
        "addressed_sha256": sha(expected_bytes),
        "addressed_bytes": len(expected),
        "source_sha256": sha(artifact.parents[1].joinpath("fixture.c").read_bytes()),
        "segments": [{"start": a, "size": len(d), "sha256": sha(d)} for a, d in loads],
    }
    config = {
        "resume_on_disconnect": False,
        "auto_unlock": False,
        "connect_mode": "halt",
        "jlink.non_interactive": True,
    }
    flash_base = min(expected)
    if args.board == "stm32l476":
        cache = json.loads((args.execution / "packs/stm32-pack-cache.json").read_text())
        pack = args.execution / "packs/Keil.STM32L4xx_DFP.3.1.0.pack"
        pack_bytes = pack.read_bytes()
        assert sha(pack_bytes) == cache["sha256"]
        with zipfile.ZipFile(io.BytesIO(pack_bytes)) as archive:
            pdsc = archive.read(cache["pdsc_name"])
        assert sha(pdsc) == cache["pdsc_sha256"] and cache["pdsc_device"] == "STM32L476RGTx"
        device = next(
            d for d in CmsisPack(io.BytesIO(pack_bytes)).devices if d.part_number == "STM32L476RGTx"
        )
        region = next(r for r in device.memory_map.regions if r.is_flash and r.start == flash_base)
        sector_table = list(region.flm.sector_sizes)
        assert sector_table == [(0, 0x800)], sector_table
        page_size, flash_size = 0x800, int(region.length)
        report["geometry"] = {
            "source": "Pinned vendor PDSC + FLM",
            "sector_sizes": sector_table,
            "flash_base": flash_base,
            "flash_size": flash_size,
            "flm_sha256": sha(bytes(region.flm.algo_data)),
        }
        pack_root = root / ".firm/packs"
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
        config["pack"] = [str(pack)]
        report["pack"] = cache | {"fresh_sha256": sha(pack_bytes), "fresh_pdsc_sha256": sha(pdsc)}
    else:
        from pyocd.target.builtin.target_nRF52840_xxAA import NRF52840

        region = next(
            r for r in NRF52840.MEMORY_MAP.regions if r.is_flash and r.start == flash_base
        )
        page_size, flash_size = int(region.sector_size), int(region.length)
        assert page_size == 0x1000 and flash_size == 0x100000
        report["geometry"] = {
            "source": "Installed nRF52840 built-in map, corroborated by live FICR below",
            "sector_size": page_size,
            "flash_size": flash_size,
            "flash_base": flash_base,
        }
    (root / "pyocd.yaml").write_text(json.dumps(config), encoding="utf-8")
    report["provider_config"] = config
    os.environ.update(
        BYO_MCP_ARTIFACT_ROOT=str(root),
        BYO_MCP_MONITOR_ROOT=str(root / "monitor"),
        BYO_MANAGED_TEST_EVIDENCE=str(root / "managed-analysis"),
        PYOCD_PROJECT_DIR=str(root),
    )
    os.environ.pop("BYO_MCP_TEST_SEAMS", None)
    os.environ.pop("BYO_MCP_TEST_FAULTS", None)
    prior_cwd = Path.cwd()
    workers, server = [], None
    safe_resume = False
    saved_pages, dirty_pages = {}, set()
    boundaries = []
    try:
        os.chdir(root)
        server = importlib.import_module("pyocd_debug_mcp.server")
        from pyocd_debug_mcp.services import target_control
        from tests.tiered_acceptance_support import public_text

        report["imports"] = {
            name: {
                "path": str(Path(importlib.import_module(name).__file__).resolve()),
                "sha256": sha(Path(importlib.import_module(name).__file__).read_bytes()),
            }
            for name in (
                "pyocd_debug_mcp.server",
                "pyocd_debug_mcp.adapters.swd_pyocd",
                "pyocd_debug_mcp.services.target_control",
                "pyocd_debug_mcp.flash_image",
            )
        }

        def call(tool, arguments, *, structured=False):
            row = {"tool": tool, "arguments": arguments, "at": now()}
            report["calls"].append(row)
            try:
                result = public_text(server, tool, arguments)
                row["result"] = json.loads(result) if structured else result
                return row["result"]
            except Exception as exc:
                row["exception"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                row["completed_at"] = now()
                save()

        def connect(selected):
            leases()
            reply = call("connect", {"board_id": args.board, "probe_uid": uid, "target": selected})
            assert "Connected" in reply, reply
            connection = server.connection_manager.connection_for(args.board)
            worker = connection.handle.worker
            assert worker and worker._process.poll() is None
            assert connection.handle.metadata.probe_uid == uid
            assert connection.handle.metadata.target_override == selected
            workers.append(worker)
            report["workers"].append(
                {
                    "pid": worker._process.pid,
                    "creation_time": creation(worker._process.pid),
                    "metadata": asdict(connection.handle.metadata),
                    "connection_id": connection.connection_id,
                    "session_id": connection.runtime_session.session_id,
                }
            )
            assert "halted" in call("get_state", {"board_id": args.board}).lower()
            qualification = identity()
            report.setdefault("session_qualification", []).append(
                {"at": now(), "selected": selected, "identity": qualification}
            )
            if "fresh_identity" in report:
                assert qualification == report["fresh_identity"]
            save()
            return connection

        def disconnect():
            call("disconnect", {"board_id": args.board})
            assert server.connection_manager.maybe_connection(args.board) is None

        def read_word(address, width=32):
            reply = call(
                "read_memory_raw",
                {"board_id": args.board, "address": address, "width": width},
                structured=True,
            )
            assert reply["status"] == "ok", reply
            return int(reply["result"], 16)

        def identity():
            leases()
            data = {"cpuid": read_word(0xE000ED00)}
            if args.board == "nrf52840":
                data.update(
                    part=read_word(0x10000100),
                    variant=read_word(0x10000104),
                    package=read_word(0x10000108),
                    codepagesize=read_word(0x10000010),
                    codesize=read_word(0x10000014),
                    deviceid=[read_word(0x10000060), read_word(0x10000064)],
                )
                assert data["part"] == 0x52840 and data["codepagesize"] == page_size
                assert data["codesize"] * page_size == flash_size
            else:
                # Recheck pinned offline inputs as well as live group/size/DBANK
                # at every session and programming boundary. These do not prove
                # the board's exact STM package or ordering suffix.
                current_pack = pack.read_bytes()
                assert sha(current_pack) == cache["sha256"]
                with zipfile.ZipFile(io.BytesIO(current_pack)) as archive:
                    assert sha(archive.read(cache["pdsc_name"])) == cache["pdsc_sha256"]
                data.update(
                    dbgmcu_idcode=read_word(0xE0042000),
                    flash_size_kib=read_word(0x1FFF75E0, 16),
                    uid_words=[read_word(0x1FFF7590 + i * 4) for i in range(3)],
                    flash_optr_readonly=read_word(0x40022020),
                )
                assert data["dbgmcu_idcode"] & 0xFFF == 0x415 and data["flash_size_kib"] == 1024
                # The pinned STM32L4xx_1024 FLM uses 2 KiB sectors; require the
                # live L476 dual-bank configuration before using that geometry.
                assert data["flash_optr_readonly"] & (1 << 21), (
                    "2 KiB dual-bank page geometry unconfirmed"
                )
            return data

        def program(path, count, *, base=None, purpose):
            assert (
                server.connection_manager.connection_for(args.board).handle.metadata.target_override
                == target
            )
            qualified = identity()
            assert qualified == report["fresh_identity"]
            report.setdefault("program_qualification", []).append(
                {
                    "at": now(),
                    "purpose": purpose,
                    "identity": qualified,
                    "artifact": str(path),
                    "artifact_sha256": sha(path.read_bytes()),
                }
            )
            arguments = {"board_id": args.board, "artifact": str(path)}
            if base is not None:
                arguments["base_address"] = base
            reply = call("flash_raw", arguments, structured=True)
            assert reply["status"] == "ok", reply
            assert reply["result"]["programming"]["status"] == "verified", reply
            assert reply["result"]["programming"]["verified_bytes"] == count, reply
            call("halt", {"board_id": args.board})
            return reply

        def restore_pages():
            """Use supported public BIN programming, then compare every saved byte."""
            if not dirty_pages:
                return
            if server.connection_manager.maybe_connection(args.board) is not None:
                disconnect()
            connect(target)
            for index in sorted(dirty_pages):
                address, count = boundaries[index]
                backup = Path(report["snapshots"][f"boundary-before-{index}"]["path"])
                assert backup.read_bytes() == saved_pages[index]
                program(backup, count, base=address, purpose=f"restore-outside-{index}")
                assert (
                    read_range(f"boundary-restored-{index}", address, count) == saved_pages[index]
                )
            disconnect()
            connect(target)
            for index in sorted(dirty_pages):
                address, count = boundaries[index]
                assert (
                    read_range(f"boundary-restored-reconnect-{index}", address, count)
                    == saved_pages[index]
                )
            report["complete_saved_pages_restored_after_reconnect"] = True
            dirty_pages.clear()
            save()

        def read_range(phase, address, count):
            actual = bytearray()
            for offset in range(0, count, 4096):
                reply = call(
                    "read_memory_raw",
                    {
                        "board_id": args.board,
                        "address": address + offset,
                        "length": min(4096, count - offset),
                    },
                    structured=True,
                )
                assert reply["status"] == "ok", reply
                actual.extend(bytes.fromhex(reply["result"]))
            assert len(actual) == count
            path = output.with_name(f"{output.stem}-{phase}.bin")
            assert not path.exists()
            path.write_bytes(actual)
            report.setdefault("snapshots", {})[phase] = {
                "path": str(path),
                "start": address,
                "size": count,
                "sha256": sha(actual),
            }
            save()
            return bytes(actual)

        def addressed(phase):
            return b"".join(read_range(f"{phase}-{i}", a, len(d)) for i, (a, d) in enumerate(loads))

        overview = call("setup_overview", {}, structured=True)
        assert any(uid.casefold() in r["connection_id"].casefold() for r in overview["connections"])
        connect(target)
        report["fresh_identity"] = identity()
        before = addressed("exact-before")
        assert before != expected_bytes, (
            "positive programming must begin with DIFFERENT target bytes"
        )
        report["prior_differing_bytes"] = sum(a != b for a, b in zip(before, expected_bytes))
        touched_end = (
            (max(expected) - flash_base + page_size) // page_size
        ) * page_size + flash_base
        report["affected_sector_scope"] = {
            "start": flash_base,
            "end": touched_end,
            "sector_size": page_size,
        }
        tail_before = read_range(
            "affected-tail-before", max(expected) + 1, touched_end - max(expected) - 1
        )
        boundaries = [(touched_end, page_size), (flash_base + flash_size - page_size, page_size)]
        outside_before = [
            read_range(f"boundary-before-{i}", a, n) for i, (a, n) in enumerate(boundaries)
        ]
        saved_pages.update(enumerate(outside_before))
        disconnect()
        generic = connect("cortex_m")
        caps = call("get_capabilities", {"board_id": args.board}, structured=True)
        assert caps["identity"] == {"assertion": "not-asserted", "capability": None}
        assert addressed("generic-before") == before
        report["state_before_generic"] = call("get_state", {"board_id": args.board})
        reply = call(
            "flash_raw", {"board_id": args.board, "artifact": str(artifact)}, structured=True
        )
        report["generic_reply"] = reply
        assert reply["status"] == "refused" and reply["code"] == "flash/unsupported-target", reply
        assert "nothing was reset, erased or programmed" in reply["message"]
        assert server.connection_manager.connection_for(args.board) is generic
        report["state_after_generic"] = call("get_state", {"board_id": args.board})
        assert report["state_before_generic"] == report["state_after_generic"]
        assert addressed("generic-after") == before
        report["generic_refusal_before_mutation"] = True
        disconnect()
        connection = connect(target)
        assert identity() == report["fresh_identity"]
        assert addressed("before-program") == before
        leases()
        target_control.check_flash_support(connection.handle, artifact)
        report["production_worker_live_support_preflight"] = {
            "completed_at": now(),
            "status": "supported",
        }
        sentinels = []
        for index, (address, count) in enumerate(boundaries):
            # Independent formula: all bytes are non-FF. Neither the production
            # expected-image oracle nor a preparation manifest supplies these.
            seed = (17, 101)[index]
            data = bytearray((i * 37 + (i >> 4) + seed) % 251 for i in range(count))
            prefix = f"BYO M02-T R01 {args.board} outside-{index}\n".encode("ascii")
            data[: len(prefix)] = prefix
            data = bytes(data)
            assert all(value != 0xFF for value in data)
            assert address >= touched_end and address + count <= flash_base + flash_size
            assert (address - flash_base) % page_size == 0 and count == page_size
            sentinel = output.with_name(f"{output.stem}-sentinel-{index}.bin")
            assert not sentinel.exists()
            sentinel.write_bytes(data)
            dirty_pages.add(index)  # Restore even if programming or its oracle raises.
            save()
            program(sentinel, count, base=address, purpose=f"seed-outside-{index}")
            assert read_range(f"boundary-seeded-{index}", address, count) == data
            sentinels.append(data)
        disconnect()
        connect(target)
        assert addressed("after-seeding-before-main") == before
        for i, (a, n) in enumerate(boundaries):
            assert read_range(f"boundary-seeded-reconnect-{i}", a, n) == sentinels[i]
        report["sentinel_erasure_sensitivity"] = [
            {
                "index": i,
                "non_erased_bytes": sum(b != 0xFF for b in data),
                "whole_page_sha256": sha(data),
                "all_ff_sha256": sha(bytes([0xFF]) * len(data)),
                "all_ff_replacement_rejected": data != bytes([0xFF]) * len(data),
            }
            for i, data in enumerate(sentinels)
        ]
        programmed = program(artifact, len(expected), purpose="main-image-only")
        report["programming_reply"] = programmed
        call("halt", {"board_id": args.board})
        assert addressed("after-program") == expected_bytes
        report["affected_tail_unchanged"] = (
            read_range("affected-tail-after", max(expected) + 1, len(tail_before)) == tail_before
        )
        for i, (a, n) in enumerate(boundaries):
            assert read_range(f"boundary-after-{i}", a, n) == sentinels[i]
        disconnect()
        connect(target)
        report["reconnected_identity"] = identity()
        assert report["reconnected_identity"] == report["fresh_identity"]
        assert addressed("after-reconnect") == expected_bytes
        for i, (a, n) in enumerate(boundaries):
            assert read_range(f"boundary-reconnect-{i}", a, n) == sentinels[i]
        report["complete_addressed_equality_after_reconnect"] = True
        report["outside_scope_boundaries_unchanged"] = True
        restore_pages()
        assert addressed("main-after-restoration-reconnect") == expected_bytes
        report["main_complete_after_outside_restoration"] = True
        safe_resume = True
        report["verdict"] = "PASS"
    except Exception as exc:
        report["obstacle"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        if dirty_pages:
            try:
                restore_pages()
                if addressed("main-after-failure-restoration") == expected_bytes:
                    safe_resume = True
            except Exception as exc:
                report["cleanup_errors"].append(f"outside-page restoration: {exc}")
                report["restoration_traceback"] = traceback.format_exc()
                report["remaining_dirty_outside_pages"] = sorted(dirty_pages)
                report["verdict"] = "FAIL"
        if (
            server is not None
            and server.connection_manager.maybe_connection(args.board) is not None
        ):
            try:
                if safe_resume:
                    call("resume", {"board_id": args.board})
                    report["final_observed_state"] = call("get_state", {"board_id": args.board})
            except Exception as exc:
                report["cleanup_errors"].append(f"resume: {exc}")
            finally:
                try:
                    disconnect()
                except Exception as exc:
                    report["cleanup_errors"].append(f"disconnect: {exc}")
        report["disconnected"] = (
            server is not None and server.connection_manager.maybe_connection(args.board) is None
        )
        for worker, row in zip(workers, report["workers"]):
            pid = worker._process.pid
            exists = psutil.pid_exists(pid)
            same_process = exists and creation(pid) == row["creation_time"]
            row.update(
                exit_code=worker._process.poll(),
                cleanup_confirmed=worker._cleanup_confirmed,
                pid_exists=exists,
                exact_identity_absent=not same_process,
            )
        report["cleanup_proven"] = (
            report["disconnected"]
            and not report["cleanup_errors"]
            and all(
                r["exit_code"] is not None and r["cleanup_confirmed"] and r["exact_identity_absent"]
                for r in report["workers"]
            )
        )
        os.chdir(prior_cwd)
        report["completed_at"] = now()
        report["raw_exit"] = 0 if report["verdict"] == "PASS" and report["cleanup_proven"] else 1
        save()
    print(
        json.dumps(
            {
                k: report.get(k)
                for k in ("board", "verdict", "obstacle", "cleanup_proven", "raw_exit")
            }
        )
    )
    return report["raw_exit"]


if __name__ == "__main__":
    raise SystemExit(main())
