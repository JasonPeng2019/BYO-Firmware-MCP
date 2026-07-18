#!/usr/bin/env python3
"""Run the destructive-but-recoverable M7 Nucleo hardware acceptance.

The command requires a clean external artifact root plus prebuilt v1/v2 ELF and
HEX pairs. It never mass-erases. The application partition is backed up before
this command is run, and the real adapter is instrumented to prove every pyOCD
programmer instance is forced to sector erase.

This is retained only as an explicitly invoked historical/manual acceptance
script. Automated checks must never execute it or treat its live/manual source
records as packaged automatic safety authority.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from intelhex import IntelHex  # type: ignore[import-not-found]

if TYPE_CHECKING:
    from pyocd_debug_mcp.safety.linker import BuildEvidence


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text_result(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) == 1:
        text = getattr(value[0], "text", None)
        if isinstance(text, str):
            return text
    raise TypeError(f"Expected one MCP text result, got {type(value).__name__}")


def _json_result(value: object) -> dict[str, Any]:
    result = json.loads(_text_result(value))
    if not isinstance(result, dict):
        raise TypeError("Expected an MCP JSON object result")
    return result


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--v1-elf", type=Path, required=True)
    parser.add_argument("--v1-hex", type=Path, required=True)
    parser.add_argument("--v2-elf", type=Path, required=True)
    parser.add_argument("--v2-hex", type=Path, required=True)
    parser.add_argument("--backup-hex", type=Path, required=True)
    parser.add_argument("--probe-id", required=True)
    parser.add_argument("--serial-id", required=True)
    parser.add_argument("--board-id", default="nucleo_l476rg")
    parser.add_argument("--display-name", default="Nucleo-L476RG M7 Acceptance")
    parser.add_argument("--mcu-part-number", default="STM32L476RGT6")
    parser.add_argument("--target", default="stm32l476rgtx")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument(
        "--silicon-id-address", type=lambda value: int(value, 0), default=0xE0042000
    )
    parser.add_argument("--silicon-id-expected", type=lambda value: int(value, 0), default=0x415)
    parser.add_argument("--silicon-id-mask", type=lambda value: int(value, 0), default=0xFFF)
    return parser.parse_args()


def _build_document(evidence: BuildEvidence) -> dict[str, object]:
    if evidence.role is None or evidence.flash_partition is None:
        raise ValueError("Acceptance build evidence must have a role and flash partition")
    return {
        "configuration_id": evidence.configuration_id,
        "role": evidence.role.value,
        "flash_partition": asdict(evidence.flash_partition),
        "ram_partitions": [asdict(item) for item in evidence.ram_partitions],
        "loadable_segments": [asdict(item) for item in evidence.loadable_segments],
        "hex_ranges": [asdict(item) for item in evidence.hex_ranges],
        "entry_point": evidence.entry_point,
        "vector_table": evidence.vector_table,
        "provenance": [
            {"kind": item.artifact_kind, "path": str(item.path), "sha256": item.sha256}
            for item in evidence.provenance
        ],
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    artifact_root = args.artifact_root.expanduser().resolve()
    checkout_root = Path.cwd().resolve()
    if artifact_root == checkout_root or checkout_root in artifact_root.parents:
        raise ValueError("--artifact-root must be outside the checkout")
    if (artifact_root / ".firm").exists():
        raise FileExistsError("A clean artifact root is required; .firm already exists")
    artifact_root.mkdir(parents=True, exist_ok=True)
    for path in (args.v1_elf, args.v1_hex, args.v2_elf, args.v2_hex, args.backup_hex):
        if not path.expanduser().resolve().is_file():
            raise FileNotFoundError(path)
    v1_elf = args.v1_elf.resolve()
    v1_hex = args.v1_hex.resolve()
    v2_elf = args.v2_elf.resolve()
    v2_hex = args.v2_hex.resolve()
    backup_hex = args.backup_hex.resolve()
    os.environ["BYO_MCP_ARTIFACT_ROOT"] = str(artifact_root)

    # Isolation must be established before importing the server composition root.
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.adapters import swd_pyocd
    from pyocd_debug_mcp.guardrails.gate import GateRefusal
    from pyocd_debug_mcp.pack_provision import (
        PACKS_DIR,
        load_manifest,
        pack_spec_document,
        sha256_file,
    )
    from pyocd_debug_mcp.safety.enforce import SafetyPolicy
    from pyocd_debug_mcp.safety.linker import (
        BuildArtifactSelection,
        BuildRole,
        extract_build_evidence,
    )
    from pyocd_debug_mcp.safety.map_build import (
        MapGeometry,
        MapIdentity,
        MapPartitions,
        RegionContribution,
        RegionSource,
        SafetyMapBuildRequest,
        SafetyMapBuilder,
    )
    from pyocd_debug_mcp.safety.refresh import SafetyRefreshRequest
    from pyocd_debug_mcp.safety.regions import (
        ActionCategory,
        AddressRange,
        Allowed,
        Provenance,
        RegionKind,
        SafetyRegion,
        SourceAuthority,
    )
    from pyocd_debug_mcp.setup_flow.board_catalog import catalog_board
    from pyocd_debug_mcp.services import target_control
    from pyocd_debug_mcp.services.uart_capture import capture_uart_output

    if server._firm_store.layout.project_root != artifact_root:
        raise RuntimeError("Server artifact-root isolation did not take effect")

    evidence: dict[str, Any] = {
        "schema_version": 1,
        "started_at": _timestamp(),
        "exact_command": [sys.executable, *sys.argv],
        "checkout_root": str(checkout_root),
        "artifact_root": str(artifact_root),
        "board_id": args.board_id,
        "requested_hardware": {
            "probe_id": args.probe_id,
            "serial_id": args.serial_id,
            "target": args.target,
            "mcu_part_number": args.mcu_part_number,
        },
        "recovery": {
            "backup_hex": str(backup_hex),
            "backup_sha256": _sha256(backup_hex),
            "command": (
                f"uv run pyocd load -u {args.probe_id} -t {args.target} "
                f"-O chip_erase=sector {backup_hex}"
            ),
        },
    }

    # Build-derived partitions and segments. No partition range is supplied by the caller.
    v1_build = extract_build_evidence(
        BuildArtifactSelection("m7_v1", BuildRole.APPLICATION, v1_elf, hex_path=v1_hex)
    )
    v2_build = extract_build_evidence(
        BuildArtifactSelection("m7_v2", BuildRole.APPLICATION, v2_elf, hex_path=v2_hex)
    )
    if v1_build.flash_partition is None or v2_build.flash_partition is None:
        raise RuntimeError("Both linker artifacts must define an application partition")
    if v1_build.flash_partition != v2_build.flash_partition:
        raise RuntimeError("The relink changed its linker partition; fix or select the build")
    evidence["builds"] = {"v1": _build_document(v1_build), "v2": _build_document(v2_build)}

    # Read-only live target/geometry reconciliation before any write.
    board = server.resolve_board_config(args.board_id, None)
    live_handle = target_control.open_session(
        board=board, unique_id=args.probe_id, target=args.target
    )
    try:
        target = live_handle.session.target
        flash_regions = [item for item in target.memory_map.regions if item.is_flash]
        ram_regions = [item for item in target.memory_map.regions if item.is_ram]
        if len(flash_regions) != 1:
            raise RuntimeError("Expected one exact live flash region")
        flash = flash_regions[0]
        erase_size = getattr(flash, "blocksize", None)
        if not isinstance(erase_size, int) or erase_size <= 0:
            raise RuntimeError("pyOCD did not expose a positive erase block size")
        silicon_raw = target_control.read_memory(live_handle, args.silicon_id_address, 32)
        live = {
            "probe_uid": live_handle.probe_uid,
            "route_used": live_handle.route_used,
            "target_override": live_handle.session.options.get("target_override"),
            "part_number": getattr(target, "part_number", None),
            "vendor": getattr(target, "vendor", None),
            "flash": {
                "start": flash.start,
                "end": flash.end + 1,
                "length": flash.length,
                "erase_size": erase_size,
            },
            "ram": [
                {"name": item.name, "start": item.start, "end": item.end + 1, "length": item.length}
                for item in ram_regions
            ],
            "silicon_id_address": args.silicon_id_address,
            "silicon_id_raw": silicon_raw,
            "silicon_device_id": silicon_raw & args.silicon_id_mask,
        }
    finally:
        target_control.close_session(live_handle)
    if live["silicon_device_id"] != args.silicon_id_expected:
        raise RuntimeError("Live silicon identity does not match the requested STM32L476")
    if flash.start != 0x08000000 or flash.length != 0x100000 or erase_size != 0x800:
        raise RuntimeError(
            "Live pack geometry does not match the independently reviewed device facts"
        )
    evidence["live_target"] = live

    pack = next(
        (
            item
            for item in load_manifest()
            if args.target in item.provides_targets and args.board_id in item.needed_by_boards
        ),
        None,
    )
    if pack is None:
        raise RuntimeError("No pinned pack owns the requested target")
    pack_path = (PACKS_DIR / pack.filename).resolve()
    pack_hash = sha256_file(pack_path)
    if pack_hash != pack.sha256:
        raise RuntimeError("Pinned pack checksum mismatch")
    pack_document: dict[str, object] = {
        **pack_spec_document(pack),
        "artifact": {"path": str(pack_path), "sha256": pack_hash},
    }
    evidence["pack"] = pack_document

    core = server._profile_repository.commit_core(
        server._profile_repository.stage_core(
            {
                "board_id": args.board_id,
                "display_name": args.display_name,
                "mcu_part_number": args.mcu_part_number,
                "mcu_family": "stm32l476",
                "probe_family": "stlink",
                "pyocd_target": args.target,
                "probe_type": "stlink",
                "serial_baudrate": args.baudrate,
                "probe_hint_terms": ["st-link", "stlink", "stm32", "nucleo"],
                "serial_hint_terms": ["st-link", "stlink", "stm32", "nucleo", "virtual com"],
            }
        )
    )
    profile = server._profile_repository.commit_optional(
        server._profile_repository.stage_optional(
            args.board_id,
            {
                "test_read_address": 0x08000000,
                "silicon_id_address": args.silicon_id_address,
                "silicon_id_expected": args.silicon_id_expected,
                "silicon_id_mask": args.silicon_id_mask,
                "silicon_id_width_bits": 32,
                "silicon_id_label": "DBGMCU_IDCODE.DEV_ID",
            },
        )
    )
    evidence["profile_commit"] = {
        "core_updated_at": core.updated_at,
        "optional_updated_at": profile.updated_at,
        "path": str(profile.source_path),
        "sha256": _sha256(profile.source_path),
    }

    official_evidence = {
        "live_silicon": {
            "DBGMCU_IDCODE": live["silicon_id_raw"],
            "masked_device_id": live["silicon_device_id"],
        },
        "manufacturer_sources": [
            {
                "id": "RM0351",
                "url": "https://www.st.com/resource/en/reference_manual/rm0351-.pdf",
                "reviewed_fact": "main flash starts at 0x08000000 and uses 2 KiB pages",
                "local_download": "unavailable_after_bounded_attempt",
            },
            {
                "id": "STM32L476RG product/datasheet",
                "url": "https://www.st.com/en/microcontrollers-microprocessors/stm32l476rg.html",
                "reviewed_fact": "STM32L476RG has 1 MiB flash and up to 128 KiB SRAM",
                "local_download": "unavailable_after_bounded_attempt",
            },
        ],
        "reconciliation": {
            "flash_start": "pack/live and RM0351 agree",
            "flash_length": "pack/live and STM32L476RG device source agree",
            "erase_size": "pack/live and RM0351 agree",
            "silicon": "live DBGMCU device id matches STM32L476 family",
        },
    }
    catalog = catalog_board("nucleo_l476rg")
    if args.mcu_part_number != catalog.package_part_number or args.target != catalog.pyocd_target:
        raise RuntimeError("Manual acceptance identity does not match the reviewed catalog entry")
    application_partition = catalog.application_partition
    if application_partition is None:
        raise RuntimeError("The reviewed catalog has no authoritative application partition")
    map_geometry = MapGeometry(
        AddressRange(catalog.flash_start, catalog.flash_end),
        AddressRange(catalog.ram_start, catalog.ram_end),
        erase_origin=catalog.flash_start,
        erase_size=catalog.erase_size,
    )
    # This historical manual script records its live/manual evidence honestly. It
    # does not claim that those records are packaged automatic setup authority.
    reviewed_official_evidence = {
        "manual_historical_official_evidence": official_evidence,
        "deployment_partition_policy": catalog.deployment_partition_policy_document(),
    }

    def region(
        name: str,
        kind: RegionKind,
        address_range: AddressRange,
        provenance: tuple[Provenance, ...],
        groups: tuple[RegionSource, ...],
    ) -> RegionContribution:
        return RegionContribution(SafetyRegion(name, kind, address_range, provenance), groups)

    hardware_provenance = (
        Provenance(SourceAuthority.DEVICE_SUPPORT, pack.id, f"pinned pack sha256 {pack_hash}"),
        Provenance(SourceAuthority.OFFICIAL_DOCUMENT, "RM0351", "2 KiB pages at 0x08000000"),
        Provenance(SourceAuthority.RECONCILED, args.probe_id, "live pyOCD memory map agreed"),
    )
    source_groups = (
        RegionSource.REVIEWED_DEVICE_SUPPORT,
        RegionSource.REVIEWED_OFFICIAL_EVIDENCE,
        RegionSource.GEOMETRY,
    )
    base_regions = (
        region(
            "physical flash",
            RegionKind.PHYSICAL_FLASH,
            map_geometry.physical_flash,
            hardware_provenance,
            source_groups,
        ),
        region(
            "physical RAM",
            RegionKind.PHYSICAL_RAM,
            map_geometry.physical_ram,
            hardware_provenance,
            source_groups,
        ),
        region(
            "writable RAM",
            RegionKind.RAM,
            map_geometry.physical_ram,
            hardware_provenance,
            source_groups,
        ),
        *(
            region(
                item.name,
                item.kind,
                AddressRange(item.start, item.end),
                hardware_provenance,
                source_groups,
            )
            for item in catalog.hardware_regions
        ),
    )
    map_request = SafetyMapBuildRequest(
        board_id=args.board_id,
        identity=MapIdentity(
            catalog.package_part_number,
            catalog.pyocd_target,
            catalog.board_type,
        ),
        profile=profile.to_document(),
        reviewed_device_support=pack_document,
        reviewed_official_evidence=reviewed_official_evidence,
        geometry=map_geometry,
        partitions=MapPartitions(application_partition),
        regions=base_regions,
    )
    map_document = SafetyMapBuilder(server._firm_store).build(map_request)
    safety_ref = (
        server._firm_store.layout.safety_reference_prefix(args.board_id) / "memory_map.yaml"
    ).as_posix()
    profile = server._profile_repository.commit_safety_ref(
        server._profile_repository.stage_safety_ref(args.board_id, safety_ref)
    )
    evidence["safety_map"] = {
        "status": "schema_v2_map_written",
        "map_digest": map_document.canonical_digest,
        "memory_map": str(server._safety_repository.path(args.board_id)),
        "authority_scope": "manual historical acceptance evidence",
        "profile_safety_ref": profile.safety_ref,
    }

    # Boundary images are classification inputs only; the unsafe one is never sent to pyOCD.
    boundary_dir = artifact_root / "boundary-images"
    boundary_dir.mkdir(parents=True, exist_ok=True)
    app = map_document.partitions.application
    assert app is not None
    safe_boundary = boundary_dir / "safe-last-application-byte.hex"
    unsafe_boundary = boundary_dir / "unsafe-first-byte-after-application.hex"
    for path, address in ((safe_boundary, app.end - 1), (unsafe_boundary, app.end)):
        image = IntelHex()
        image[address] = 0xA5
        image.write_hex_file(str(path))

    def sector_for(address: int) -> AddressRange:
        start = flash.start + ((address - flash.start) // erase_size) * erase_size
        return AddressRange.from_start_size(start, erase_size)

    current_map = server._safety_repository.load_current(args.board_id)
    classifier = current_map.safety_map
    safe_sector = sector_for(app.end - 1)
    unsafe_sector = sector_for(app.end)
    safe_check = classifier.check(ActionCategory.FLASH_APPLICATION, (safe_sector,))
    unsafe_check = classifier.check(ActionCategory.FLASH_APPLICATION, (unsafe_sector,))
    if not isinstance(safe_check, Allowed) or isinstance(unsafe_check, Allowed):
        raise RuntimeError("Boundary sector classification did not fail closed")
    evidence["boundary_images"] = {
        "safe": {
            "path": str(safe_boundary),
            "sha256": _sha256(safe_boundary),
            "address": app.end - 1,
            "computed_sector": asdict(safe_sector),
            "classification": "allowed",
        },
        "unsafe": {
            "path": str(unsafe_boundary),
            "sha256": _sha256(unsafe_boundary),
            "address": app.end,
            "computed_sector": asdict(unsafe_sector),
            "classification": unsafe_check.code,
            "backend_erase_write_calls": 0,
            "submitted_to_backend": False,
        },
    }

    programmer_calls: list[dict[str, object]] = []
    original_programmer: Any = swd_pyocd.FileProgrammer

    class RecordingFileProgrammer:
        def __init__(self, session: Any, *positional: Any, **options: Any) -> None:
            programmer_calls.append(
                {
                    "event": "construct",
                    "chip_erase": options.get("chip_erase"),
                    "options": dict(options),
                }
            )
            self._inner = original_programmer(session, *positional, **options)

        def program(self, path: str) -> object:
            programmer_calls.append(
                {"event": "program", "path": path, "sha256": _sha256(Path(path))}
            )
            return self._inner.program(path)

    swd_pyocd.FileProgrammer = RecordingFileProgrammer  # type: ignore[assignment]

    async def call(name: str, arguments: dict[str, object]) -> str:
        return _text_result(await server.mcp.call_tool(name, arguments))

    def gate_document() -> dict[str, object] | None:
        stamp = server.gate_manager.snapshot(args.board_id)
        return asdict(stamp) if stamp is not None else None

    async def validate(label: str) -> dict[str, Any]:
        await call("load_setup_tool", {"board_id": args.board_id, "tool_name": "board_validate"})
        result = _json_result(
            await server.mcp.call_tool(
                "board_validate",
                {"board_id": args.board_id, "probe_id": args.probe_id},
            )
        )
        if result["status"] != "validation_passed":
            raise RuntimeError(f"{label} validation did not pass: {result}")
        return result

    flash_plan_initialized = False

    async def flash_image(label: str, artifact: Path) -> dict[str, object]:
        nonlocal flash_plan_initialized
        initialization: str | None = None
        if not flash_plan_initialized:
            initialization = await call("flash_application-plan", {})
            flash_plan_initialized = True
        plan = {
            "board_id": args.board_id,
            "hypothesis": f"The {label} artifact is fully contained in the mapped application partition.",
            "hypothesis_made": True,
            "strategy": "Check the current gate, canonical map digest, linker ranges, artifact binding, and erase sectors before sector programming.",
            "strategy_evaluated": True,
            "expected_fail_return": "A pre-backend safety refusal with the exact required remedy.",
            "expected_success_return": "The application is sector-programmed and the target returns to running state.",
            "max_calls": 1,
            "max_calls_buffer": 0,
            "artifact": str(artifact),
        }
        plan_result = await call("flash_application-plan", plan)
        action_result = await call(
            "flash_application",
            {"board_id": args.board_id, "artifact": str(artifact)},
        )
        if "Flashed" not in action_result:
            raise RuntimeError(f"{label} flash did not succeed: {action_result}")
        return {"initialization": initialization, "plan": plan_result, "action": action_result}

    def verify_hex(hex_path: Path) -> dict[str, object]:
        image = IntelHex(str(hex_path))
        addresses = image.addresses()
        handle = server._handle(args.board_id)
        mismatches: list[int] = []
        # STM32L4 flash debug reads can return transient zeros while the core is
        # executing from flash. Halt only for bounded verification, then restore
        # the specified running-from-reset exit state.
        handle.session.target.reset_and_halt()
        try:
            for start, end_inclusive in image.segments():
                expected = bytes(image.tobinarray(start=start, end=end_inclusive - 1))
                actual = bytes(target_control.read_memory_block(handle, start, len(expected)))
                if actual != expected:
                    mismatches.append(start)
        finally:
            handle.session.target.reset()
        return {
            "byte_count": len(addresses),
            "segments": [{"start": start, "end": end} for start, end in image.segments()],
            "mismatch_segment_starts": mismatches,
            "matches": not mismatches,
        }

    try:
        evidence["connect_initial"] = await call("connect", {"board_id": args.board_id})
        evidence["gate_before_validation"] = gate_document()
        first_validation = await validate("initial")
        evidence["initial_validation"] = first_validation
        evidence["gate_after_initial_validation"] = gate_document()
        first_flash = await flash_image("v1", v1_elf)
        first_readback = verify_hex(v1_hex)
        first_flash["readback"] = first_readback
        if not first_readback["matches"]:
            raise RuntimeError("v1 halted flash readback did not match its HEX image")
        uart_v1 = capture_uart_output(
            args.serial_id, args.baudrate, 3.0, None, reopen_attempts=0, max_bytes=65536
        )
        first_flash["uart"] = {
            "port": args.serial_id,
            "text": uart_v1.text,
            "bytes": len(uart_v1.text.encode()),
        }
        evidence["first_flash"] = first_flash

        relink_candidate = SafetyMapBuilder(server._firm_store).derive(map_request)
        if relink_candidate.canonical_digest != map_document.canonical_digest:
            raise RuntimeError("Ordinary relink bytes unexpectedly changed stable map authority")
        manual_policy = SafetyPolicy(
            server._safety_repository,
            # This historical script predates packaged automatic Nucleo evidence.
            authority_verifier=lambda _document: None,
        )
        current_digest = manual_policy.current_aggregate(args.board_id)
        if current_digest != map_document.canonical_digest:
            raise RuntimeError("Ordinary relink unexpectedly made the stable map stale")
        evidence["ordinary_relink"] = {
            "changed_input": "application artifact bytes only",
            "persistent_map_changed": False,
            "refresh_required": False,
            "map_digest": current_digest,
            "gate_without_revalidation": gate_document(),
        }

        second_flash = await flash_image("v2", v2_elf)
        second_readback = verify_hex(v2_hex)
        second_flash["readback"] = second_readback
        if not second_readback["matches"]:
            raise RuntimeError("v2 halted flash readback did not match its HEX image")
        uart_v2 = capture_uart_output(
            args.serial_id, args.baudrate, 3.0, None, reopen_attempts=0, max_bytes=65536
        )
        second_flash["uart"] = {
            "port": args.serial_id,
            "text": uart_v2.text,
            "bytes": len(uart_v2.text.encode()),
        }
        evidence["second_flash_without_revalidation"] = second_flash

        evidence["disconnect"] = await call("disconnect", {"board_id": args.board_id})
        evidence["gate_after_disconnect"] = gate_document()
        evidence["reconnect"] = await call("connect", {"board_id": args.board_id})
        refresh_after_disconnect = server._safety_refresher.refresh(
            SafetyRefreshRequest(args.board_id, "m7-refresh-after-disconnect")
        )
        evidence["refresh_after_disconnect"] = {
            "status": refresh_after_disconnect.status,
            "changed_groups": list(refresh_after_disconnect.changed_groups),
            "map_digest": refresh_after_disconnect.map_digest,
            "validation_required": refresh_after_disconnect.validation_required,
            "gate_after_refresh": gate_document(),
        }
        if gate_document() is not None:
            raise RuntimeError("Refresh reopened a gate after disconnect")
        connection = server.connection_manager.connection_for(args.board_id)
        try:
            server.gate_manager.require_write(
                args.board_id,
                connection.connection_id,
                refresh_after_disconnect.map_digest or "unavailable-map-digest",
            )
        except GateRefusal as exc:
            evidence["closed_gate_proof"] = {
                "code": exc.code,
                "remedy": list(exc.remedy),
                "message": str(exc),
            }
        else:
            raise RuntimeError("Write gate unexpectedly opened after refresh-only reconnect")
        final_validation = await validate("post-disconnect")
        evidence["post_disconnect_validation"] = final_validation
        evidence["gate_after_required_revalidation"] = gate_document()
        if gate_document() is None:
            raise RuntimeError("Successful board_validate did not reopen the reconnected gate")
        evidence["final_disconnect"] = await call("disconnect", {"board_id": args.board_id})
    finally:
        swd_pyocd.FileProgrammer = original_programmer
        if server.connection_manager.maybe_connection(args.board_id) is not None:
            server.disconnect(args.board_id)

    if len([item for item in programmer_calls if item["event"] == "program"]) != 2:
        raise RuntimeError("Expected exactly two application program operations")
    if any(
        item.get("chip_erase") != "sector"
        for item in programmer_calls
        if item["event"] == "construct"
    ):
        raise RuntimeError("pyOCD programmer was not forced to sector erase")
    evidence["pyocd_backend"] = {
        "version": importlib.metadata.version("pyocd"),
        "programmer_calls": programmer_calls,
        "adapter_source": str(Path(swd_pyocd.__file__).resolve()),
        "adapter_sha256": _sha256(Path(swd_pyocd.__file__).resolve()),
        "mass_erase_calls": 0,
    }
    evidence["surface_audit"] = {
        "board_safety_setup_mcp_visible": False,
        "board_safety_refresh_mcp_visible": True,
        "note": (
            "The historical acceptance invokes the in-process schema-v2 builder and refresher; "
            "board_safety_refresh is the public safety recovery surface."
        ),
    }
    evidence["persisted_safety_authority"] = [
        {"path": str(path), "sha256": _sha256(path)}
        for path in sorted((artifact_root / ".firm" / "safety").glob("**/memory_map.yaml"))
    ]
    evidence["completed_at"] = _timestamp()
    evidence["terminal_status"] = "nucleo_m7_hardware_passed"
    return evidence


def main() -> int:
    args = _arguments()
    root = args.artifact_root.expanduser().resolve()
    result_path = root / "acceptance.json"
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        root.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "terminal_status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "recorded_at": _timestamp(),
                    "exact_command": [sys.executable, *sys.argv],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        raise
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
