from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.guardrails.flash_gate import (
    FlashArtifactIdentity,
    ResolvedFlashRequest,
)
from pyocd_debug_mcp.safety.enforce import SafetyPolicy, SafetyPolicyError
from pyocd_debug_mcp.safety.fingerprints import (
    FingerprintInputs,
    FingerprintSource,
)
from pyocd_debug_mcp.safety.linker import BuildRole
from pyocd_debug_mcp.safety.map_build import (
    RegionContribution,
    SafetyArtifactRepository,
    SafetyMapBuilder,
    SafetySetupRequest,
)
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)
from pyocd_debug_mcp.services.session_runtime import ActionContext
from pyocd_debug_mcp.services.symbols import ResolvedSymbol
from pyocd_debug_mcp.tools.breakpoints import (
    BreakpointToolServices,
    build_breakpoint_handlers,
)
from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers
from pyocd_debug_mcp.tools.memory import MemoryToolServices, build_memory_handlers
from pyocd_debug_mcp.tools.registers import RegisterToolServices, build_register_handlers

ROOT = Path(__file__).resolve().parents[1]
NUCLEO_ELF = ROOT / "firmware/nucleo_l476rg/reference/build/firmware.elf"


def _inputs(
    *,
    geometry: object | None = None,
    artifact: Path = NUCLEO_ELF,
) -> FingerprintInputs:
    selected_geometry = geometry or {"erase_origin": 0x08000000, "erase_size": 0x800}
    artifact_records: dict[str, object] = {
        artifact.suffix.removeprefix("."): {
            "path": str(artifact),
            "sha256": sha256(artifact.read_bytes()).hexdigest(),
        }
    }
    sibling_elf = artifact.with_suffix(".elf")
    if artifact.suffix.casefold() == ".hex" and sibling_elf.is_file():
        artifact_records["elf"] = {
            "path": str(sibling_elf),
            "sha256": sha256(sibling_elf.read_bytes()).hexdigest(),
        }
    return FingerprintInputs(
        profile={"board_id": "board_a"},
        part_target={"mcu_part_number": "STM32L476RGT6", "target": "stm32l476rgtx"},
        pack={
            "id": "Keil.STM32L4xx_DFP",
            "version": "2.7.0",
            "document": {"schema_version": 2},
        },
        evidence={
            "datasheet": "RM0351",
            "official_document": {"document": {"schema_version": 2}},
            "reconciliation": {
                "status": "agreement",
                "erase_geometry": selected_geometry,
            },
        },
        application_artifacts={"configuration": "reference", **artifact_records},
        bootloader_artifacts={"configuration": "reference", **artifact_records},
        geometry=selected_geometry,
        schema={"memory_map": 1, "evidence": 2, "catalog": 2},
    )


def _region(
    name: str,
    kind: RegionKind,
    start: int,
    end: int,
    *sources: FingerprintSource,
    executable: bool = False,
) -> RegionContribution:
    return RegionContribution(
        SafetyRegion(
            name,
            kind,
            AddressRange(start, end),
            (Provenance(SourceAuthority.RECONCILED, name, "Task 14 fixture evidence"),),
            executable,
        ),
        sources,
    )


def _policy(
    tmp_path: Path,
    *,
    geometry: object | None = None,
    artifact: Path = NUCLEO_ELF,
    application_end: int = 0x08008000,
) -> SafetyPolicy:
    request = SafetySetupRequest(
        "board_a",
        "task14-fixture",
        _inputs(geometry=geometry, artifact=artifact),
        (
            _region(
                "physical flash",
                RegionKind.PHYSICAL_FLASH,
                0x08000000,
                0x08100000,
                FingerprintSource.PACK,
            ),
            _region(
                "application",
                RegionKind.APPLICATION_FLASH,
                0x08000000,
                application_end,
                FingerprintSource.APPLICATION_ARTIFACTS,
                executable=True,
            ),
            _region(
                "ram",
                RegionKind.RAM,
                0x20000000,
                0x20010000,
                FingerprintSource.EVIDENCE,
            ),
            _region(
                "peripherals",
                RegionKind.PERIPHERAL,
                0x40000000,
                0x50000000,
                FingerprintSource.EVIDENCE,
            ),
            _region(
                "option control",
                RegionKind.PROHIBITED,
                0x40001000,
                0x40001010,
                FingerprintSource.EVIDENCE,
            ),
        ),
    )
    result = SafetyMapBuilder(FirmStore(tmp_path)).build(request)
    assert result.status == "safety_setup_completed"
    return SafetyPolicy(
        SafetyArtifactRepository(FirmStore(tmp_path)),
        authority_verifier=lambda _artifacts: None,
    )


def _ihex_record(address: int, record_type: int, data: bytes = b"") -> str:
    payload = bytes([len(data), (address >> 8) & 0xFF, address & 0xFF, record_type, *data])
    return f":{payload.hex().upper()}{(-sum(payload)) & 0xFF:02X}"


def test_ac_14_3_memory_writes_are_fully_contained_in_ram(tmp_path: Path) -> None:
    policy = _policy(tmp_path)

    assert policy.check_memory_write("board_a", 0x20000000, 32)
    with pytest.raises(SafetyPolicyError) as flash:
        policy.check_memory_write("board_a", 0x08000000, 32)
    assert "safety/wrong-region-kind" == flash.value.code
    with pytest.raises(SafetyPolicyError) as boundary:
        policy.check_memory_write("board_a", 0x2000FFFF, 32)
    assert boundary.value.code == "safety/unknown"


def test_gap_09_memory_reads_require_mapped_non_prohibited_containment(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path)

    for address in (0x08000000, 0x20000000, 0x40000000):
        assert policy.check_memory_read("board_a", address, 4)

    with pytest.raises(SafetyPolicyError) as unknown:
        policy.check_memory_read("board_a", 0x60000000, 4)
    assert unknown.value.code == "safety/unknown"
    assert "region kind 'unknown'" in str(unknown.value)
    assert unknown.value.remedy == ("board_safety_setup",)

    with pytest.raises(SafetyPolicyError) as prohibited:
        policy.check_memory_read("board_a", 0x40001000, 4)
    assert prohibited.value.code == "safety/prohibited"
    assert "region kind 'prohibited'" in str(prohibited.value)
    assert prohibited.value.remedy == ("choose a mapped, non-prohibited address",)


def test_memory_read_handlers_check_exact_spans_before_backend_access(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path)
    backend_calls: list[tuple[str, int, int]] = []
    checked: list[tuple[str, int, int]] = []

    def check_read(board: str, address: int, size_bytes: int) -> None:
        checked.append((board, address, size_bytes))
        policy.check_memory_read(board, address, size_bytes)

    common = {
        "runtime_for": lambda board: None,
        "active_session_id": lambda board: None,
        "duration_ms": lambda started: 1,
        "record_event": lambda *args, **kwargs: None,
        "format_refusal": lambda refusal, **kwargs: str(refusal),
    }
    symbol_address = {"value": 0x20000010}
    memory = build_memory_handlers(
        MemoryToolServices(
            **common,
            handle_for=lambda board: object(),
            symbol_artifact_for=lambda handle: NUCLEO_ELF,
            find_symbols=lambda artifact, query: (),
            resolve_symbol=lambda artifact, symbol: ResolvedSymbol(
                symbol, symbol_address["value"], 128, "STT_OBJECT"
            ),
            read_target_memory=lambda handle, address, width: (
                backend_calls.append(("scalar", address, width)) or 0
            ),
            read_target_block=lambda handle, address, length: (
                backend_calls.append(("block", address, length)) or []
            ),
            write_target_memory=lambda *args: None,
            check_memory_read=check_read,
        )
    )

    memory["read_memory_symbol"]("board_a", "buffer", 16)
    memory["read_memory_address"]("board_a", "0x08000000", 32, None)
    memory["read_memory_address"]("board_a", "0x40000000", 8, 12)
    assert checked == [
        ("board_a", 0x20000010, 2),
        ("board_a", 0x08000000, 4),
        ("board_a", 0x40000000, 12),
    ]
    assert len(backend_calls) == 3

    backend_calls.clear()
    symbol_address["value"] = 0x40001000
    with pytest.raises(SafetyPolicyError) as prohibited:
        memory["read_memory_symbol"]("board_a", "sensitive", 32)
    assert prohibited.value.code == "safety/prohibited"
    assert backend_calls == []

    with pytest.raises(SafetyPolicyError) as unknown:
        memory["read_memory_address"]("board_a", "0x60000000", 8, 16)
    assert unknown.value.code == "safety/unknown"
    assert backend_calls == []


def test_persisted_legacy_authority_map_cannot_be_restored_after_restart(
    tmp_path: Path,
) -> None:
    store = FirmStore(tmp_path)
    current = _inputs()
    legacy = FingerprintInputs(
        current.profile,
        current.part_target,
        current.pack,
        current.evidence,
        current.application_artifacts,
        current.bootloader_artifacts,
        current.geometry,
        {"memory_map": 1, "evidence": 1, "catalog": 1},
    )
    result = SafetyMapBuilder(store).build(
        SafetySetupRequest(
            "board_a",
            "legacy-map",
            legacy,
            (
                _region(
                    "legacy RAM",
                    RegionKind.RAM,
                    0x20000000,
                    0x20001000,
                    FingerprintSource.EVIDENCE,
                ),
            ),
        )
    )
    assert result.status == "safety_setup_completed"

    restarted_policy = SafetyPolicy(SafetyArtifactRepository(FirmStore(tmp_path)))
    with pytest.raises(SafetyPolicyError) as caught:
        restarted_policy.current_aggregate("board_a")

    assert caught.value.code == "safety/authority-migration-required"
    assert caught.value.remedy == ("board_setup", "board_safety_setup", "board_validate")


def test_ac_14_6_register_write_rejects_prohibited_overlap(tmp_path: Path) -> None:
    policy = _policy(tmp_path)

    assert policy.check_register_write("board_a", 0x40000000)
    with pytest.raises(SafetyPolicyError) as prohibited:
        policy.check_register_write("board_a", 0x40000FFE)
    assert prohibited.value.code == "safety/prohibited"


def test_ac_14_8_breakpoints_require_build_derived_executable_space(tmp_path: Path) -> None:
    policy = _policy(tmp_path)

    assert policy.check_breakpoint("board_a", 0x08000020)
    with pytest.raises(SafetyPolicyError) as ram:
        policy.check_breakpoint("board_a", 0x20000020)
    assert ram.value.code == "safety/wrong-region-kind"


def test_ac_14_4_and_14_10_flash_checks_target_segments_entry_vector_and_sectors(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path)
    evidence = policy.check_flash(
        "board_a",
        BuildRole.APPLICATION,
        NUCLEO_ELF,
        current_target="stm32l476rgtx",
    )
    assert evidence.entry_point == 0x08000B29
    assert evidence.vector_table == 0x08000000

    with pytest.raises(SafetyPolicyError) as target:
        policy.check_flash(
            "board_a",
            BuildRole.APPLICATION,
            NUCLEO_ELF,
            current_target="wrong-target",
        )
    assert target.value.code == "safety/target-mismatch"

    unsafe_geometry = _policy(
        tmp_path / "large-sector",
        geometry={"erase_origin": 0x08000000, "erase_size": 0x10000},
    )
    with pytest.raises(SafetyPolicyError) as erase:
        unsafe_geometry.check_flash(
            "board_a",
            BuildRole.APPLICATION,
            NUCLEO_ELF,
            current_target="stm32l476rgtx",
        )
    assert erase.value.code == "safety/erase-sector-outside-partition"


def test_flash_ignores_unselected_adjacent_linker_map(tmp_path: Path) -> None:
    artifact = tmp_path / "firmware.elf"
    artifact.write_bytes(NUCLEO_ELF.read_bytes())
    artifact.with_suffix(".map").write_text("malformed map dialect", encoding="utf-8")
    policy = _policy(tmp_path / "store", artifact=artifact)

    evidence = policy.check_flash(
        "board_a",
        BuildRole.APPLICATION,
        artifact,
        current_target="stm32l476rgtx",
    )

    assert evidence.entry_point == 0x08000B29


@pytest.mark.parametrize("case", ["partition-crossing", "erase-crossing"])
def test_ac_14_4_and_14_10_crafted_flash_rejection_has_zero_backend_calls(
    tmp_path: Path,
    case: str,
) -> None:
    artifact = NUCLEO_ELF
    if case == "partition-crossing":
        build = tmp_path / "crafted"
        build.mkdir()
        sibling = build / "firmware.elf"
        sibling.write_bytes(NUCLEO_ELF.read_bytes())
        artifact = build / "firmware.hex"
        artifact.write_text(
            "\n".join(
                (
                    _ihex_record(0, 4, bytes((0x08, 0x10))),
                    _ihex_record(0, 0, b"cross"),
                    _ihex_record(0, 1),
                )
            )
            + "\n",
            encoding="ascii",
        )
        policy = _policy(tmp_path / "store", artifact=artifact)
    else:
        policy = _policy(
            tmp_path / "store",
            geometry={"erase_origin": 0x08000000, "erase_size": 0x10000},
        )

    calls: list[str] = []
    identity = FlashArtifactIdentity(
        artifact,
        artifact.suffix,
        artifact.stat().st_size,
        sha256(artifact.read_bytes()).hexdigest(),
        "explicit",
    )

    def validate(action: str, board: str, selected: Path) -> None:
        del action
        policy.check_flash(
            board,
            BuildRole.APPLICATION,
            selected,
            current_target="stm32l476rgtx",
        )

    handlers = build_flash_handlers(
        FlashToolServices(
            runtime_for=lambda board: None,
            active_session_id=lambda board: None,
            duration_ms=lambda started: 1,
            record_event=lambda *args, **kwargs: SimpleNamespace(),
            record_blocked_event=lambda *args, **kwargs: None,
            format_refusal=lambda refusal, **kwargs: str(refusal),
            format_block=lambda blocked, **kwargs: str(blocked),
            ensure_flash_allowed=lambda runtime: None,
            action_context=lambda action, board: ActionContext("test", action, None),
            maybe_handle_for=lambda board: object(),
            handle_for=lambda board: calls.append("handle") or object(),
            resolve_request=lambda handle, selected, context: ResolvedFlashRequest(
                artifact, identity
            ),
            flash_target=lambda handle, selected: calls.append("erase/write") or selected,
            handle_mutation_event=lambda board, event: None,
            error_code=lambda exc: getattr(exc, "code", "runtime/error"),
            validate_flash=validate,
        )
    )

    with pytest.raises(SafetyPolicyError) as refusal:
        handlers["flash_application"]("board_a", str(artifact))
    assert (
        refusal.value.code
        == {
            "partition-crossing": "build/hex-outside-elf",
            "erase-crossing": "safety/erase-sector-outside-partition",
        }[case]
    )
    assert refusal.value.remedy in {
        ("select_valid_build_artifact", "board_safety_refresh"),
        ("select_correct_build", "board_safety_refresh"),
    }
    assert calls == []


def test_declared_source_artifact_drift_is_detected_per_call(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    artifact = tmp_path / "evidence.json"
    artifact.write_text("v1", encoding="utf-8")
    inputs = _inputs()
    assert isinstance(inputs.evidence, dict)
    inputs = FingerprintInputs(
        inputs.profile,
        inputs.part_target,
        inputs.pack,
        {
            **inputs.evidence,
            "path": str(artifact),
            "sha256": sha256(b"v1").hexdigest(),
        },
        inputs.application_artifacts,
        inputs.bootloader_artifacts,
        inputs.geometry,
        inputs.schema,
    )
    request = SafetySetupRequest(
        "board_a",
        "artifact-drift",
        inputs,
        (
            _region(
                "ram",
                RegionKind.RAM,
                0x20000000,
                0x20001000,
                FingerprintSource.EVIDENCE,
            ),
        ),
    )
    assert SafetyMapBuilder(store).build(request).status == "safety_setup_completed"
    policy = SafetyPolicy(
        SafetyArtifactRepository(store), authority_verifier=lambda _artifacts: None
    )
    assert policy.current_aggregate("board_a")

    artifact.write_text("v2", encoding="utf-8")
    with pytest.raises(SafetyPolicyError) as stale:
        policy.current_aggregate("board_a")
    assert stale.value.code == "safety/artifact-stale"
    assert stale.value.remedy == ("board_safety_refresh",)


def test_live_fingerprint_inputs_are_recomputed_on_every_write_check(tmp_path: Path) -> None:
    persisted = _policy(tmp_path)
    current: dict[str, FingerprintInputs] = {"value": _inputs()}
    calls: list[str] = []

    def live(board_id: str, artifacts) -> FingerprintInputs:
        del artifacts
        calls.append(board_id)
        return current["value"]

    policy = SafetyPolicy(
        persisted.repository,
        live_inputs=live,
        authority_verifier=persisted.authority_verifier,
    )
    policy.current_aggregate("board_a")
    policy.current_aggregate("board_a")
    assert calls == ["board_a", "board_a"]

    original = current["value"]
    current["value"] = FingerprintInputs(
        {"board_id": "board_a", "display_name": "Renamed"},
        original.part_target,
        original.pack,
        original.evidence,
        original.application_artifacts,
        original.bootloader_artifacts,
        original.geometry,
        original.schema,
    )
    with pytest.raises(SafetyPolicyError) as stale:
        policy.current_aggregate("board_a")
    assert stale.value.code == "safety/fingerprint-stale"
    assert stale.value.remedy == ("board_safety_setup",)


@pytest.mark.parametrize("source", ["geometry", "schema"])
def test_structural_drift_requires_full_safety_setup_then_validation(
    tmp_path: Path,
    source: str,
) -> None:
    persisted = _policy(tmp_path)
    original = _inputs()
    values = original.values()
    values[FingerprintSource.GEOMETRY if source == "geometry" else FingerprintSource.SCHEMA] = {
        "changed": source
    }
    changed = FingerprintInputs(
        values[FingerprintSource.PROFILE],
        values[FingerprintSource.PART_TARGET],
        values[FingerprintSource.PACK],
        values[FingerprintSource.EVIDENCE],
        values[FingerprintSource.APPLICATION_ARTIFACTS],
        values[FingerprintSource.BOOTLOADER_ARTIFACTS],
        values[FingerprintSource.GEOMETRY],
        values[FingerprintSource.SCHEMA],
    )
    policy = SafetyPolicy(
        persisted.repository,
        live_inputs=lambda _board, _artifacts: changed,
        authority_verifier=persisted.authority_verifier,
    )

    with pytest.raises(SafetyPolicyError) as stale:
        policy.current_aggregate("board_a")

    assert stale.value.remedy == ("board_safety_setup", "board_validate")


def test_backend_mutations_are_never_called_after_containment_refusal(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    calls: list[str] = []

    def check_memory(board: str, address: int, width: int) -> None:
        policy.check_memory_write(board, address, width)

    def check_register(board: str, address: int) -> None:
        policy.check_register_write(board, address)

    def check_breakpoint(board: str, address: int) -> None:
        policy.check_breakpoint(board, address)

    def check_flash(action: str, board: str, artifact: Path) -> None:
        del action
        policy.check_flash(
            board,
            BuildRole.APPLICATION,
            artifact,
            current_target="wrong-target",
        )

    common = {
        "runtime_for": lambda board: None,
        "active_session_id": lambda board: None,
        "duration_ms": lambda started: 1,
        "record_event": lambda *args, **kwargs: None,
        "format_refusal": lambda refusal, **kwargs: str(refusal),
    }

    memory = build_memory_handlers(
        MemoryToolServices(
            **common,
            handle_for=lambda board: object(),
            symbol_artifact_for=lambda handle: NUCLEO_ELF,
            find_symbols=lambda artifact, query: (),
            resolve_symbol=lambda artifact, symbol: ResolvedSymbol(
                symbol, 0x08000000, 4, "STT_OBJECT"
            ),
            read_target_memory=lambda handle, address, width: 0,
            read_target_block=lambda handle, address, length: [],
            write_target_memory=lambda *args: calls.append("memory-write"),
            check_memory_read=lambda board, address, size: None,
            check_memory_write=check_memory,
        )
    )
    with pytest.raises(SafetyPolicyError):
        memory["write_memory"]("board_a", "flash_symbol", 1, 32)

    registers = build_register_handlers(
        RegisterToolServices(
            supported_registers=lambda board: (),
            read_register=lambda *args: "",
            write_register=lambda *args: "",
            masked_register_write=lambda *args: calls.append("register-write") or "",
            check_register_write=check_register,
        )
    )
    with pytest.raises(SafetyPolicyError):
        registers["register_write"]("board_a", "0x40001000", "0xff", "0x1")

    breakpoints = build_breakpoint_handlers(
        BreakpointToolServices(
            **common,
            handle_for=lambda board: object(),
            symbol_artifact_for=lambda handle: NUCLEO_ELF,
            resolve_symbol=lambda artifact, symbol: ResolvedSymbol(
                symbol, 0x20000000, 2, "STT_FUNC"
            ),
            set_target_breakpoint=lambda *args: calls.append("breakpoint-set"),
            remove_target_breakpoint=lambda *args: None,
            check_breakpoint=check_breakpoint,
        )
    )
    with pytest.raises(SafetyPolicyError):
        breakpoints["set_breakpoint"]("board_a", "ram_function")

    identity = FlashArtifactIdentity(
        NUCLEO_ELF,
        ".elf",
        NUCLEO_ELF.stat().st_size,
        "a" * 64,
        "explicit",
    )
    flash = build_flash_handlers(
        FlashToolServices(
            runtime_for=lambda board: None,
            active_session_id=lambda board: None,
            duration_ms=lambda started: 1,
            record_event=lambda *args, **kwargs: SimpleNamespace(),
            record_blocked_event=lambda *args, **kwargs: None,
            format_refusal=lambda refusal, **kwargs: str(refusal),
            format_block=lambda blocked, **kwargs: str(blocked),
            ensure_flash_allowed=lambda runtime: None,
            action_context=lambda action, board: ActionContext("test", action, None),
            maybe_handle_for=lambda board: object(),
            handle_for=lambda board: calls.append("flash-handle") or object(),
            resolve_request=lambda handle, artifact, context: ResolvedFlashRequest(
                NUCLEO_ELF, identity
            ),
            flash_target=lambda handle, artifact: calls.append("flash") or artifact,
            handle_mutation_event=lambda board, event: None,
            error_code=lambda exc: getattr(exc, "code", "runtime/error"),
            validate_flash=check_flash,
        )
    )
    with pytest.raises(SafetyPolicyError):
        flash["flash_application"]("board_a", str(NUCLEO_ELF))

    assert calls == []
