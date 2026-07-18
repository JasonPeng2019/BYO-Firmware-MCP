from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.guardrails.flash_gate import FlashArtifactIdentity, ResolvedFlashRequest
from pyocd_debug_mcp.safety.enforce import SafetyPolicy, SafetyPolicyError
from pyocd_debug_mcp.safety.linker import BuildRole
from pyocd_debug_mcp.safety.map_build import (
    MapGeometry,
    MapIdentity,
    MapPartitions,
    RegionContribution,
    RegionSource,
    SafetyMapBuildRequest,
    SafetyMapBuilder,
    SafetyMapRepository,
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
from pyocd_debug_mcp.tools.breakpoints import BreakpointToolServices, build_breakpoint_handlers
from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers
from pyocd_debug_mcp.tools.memory import MemoryToolServices, build_memory_handlers
from pyocd_debug_mcp.tools.registers import RegisterToolServices, build_register_handlers

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_BUILD = ROOT / "firmware/nucleo_l476rg/reference/build"
NUCLEO_ELF = REFERENCE_BUILD / "firmware.elf"
NUCLEO_HEX = REFERENCE_BUILD / "firmware.hex"


def _contribution(
    name: str,
    kind: RegionKind,
    start: int,
    end: int,
    *,
    executable: bool = False,
) -> RegionContribution:
    return RegionContribution(
        SafetyRegion(
            name,
            kind,
            AddressRange(start, end),
            (
                Provenance(
                    SourceAuthority.RECONCILED,
                    f"fixture:{name}",
                    "Schema-v2 server-owned reviewed fixture evidence.",
                ),
            ),
            executable,
        ),
        (RegionSource.REVIEWED_OFFICIAL_EVIDENCE,),
    )


def _request(
    *,
    erase_size: int = 0x800,
    application_end: int = 0x08008000,
    application_authority: bool = True,
) -> SafetyMapBuildRequest:
    geometry = MapGeometry(
        AddressRange(0x08000000, 0x08100000),
        AddressRange(0x20000000, 0x20010000),
        erase_origin=0x08000000,
        erase_size=erase_size,
    )
    return SafetyMapBuildRequest(
        board_id="board_a",
        identity=MapIdentity("STM32L476RGT6", "stm32l476rgtx", "nucleo_l476rg"),
        profile={
            "board_id": "board_a",
            "mcu_part_number": "STM32L476RGT6",
            "pyocd_target": "stm32l476rgtx",
            "board_type": "nucleo_l476rg",
        },
        reviewed_device_support={"fixture": "device-support-v2"},
        reviewed_official_evidence={"fixture": "official-evidence-v2"},
        geometry=geometry,
        partitions=MapPartitions(
            AddressRange(0x08000000, application_end) if application_authority else None
        ),
        regions=(
            _contribution(
                "physical flash", RegionKind.PHYSICAL_FLASH, 0x08000000, 0x08100000
            ),
            _contribution("physical RAM", RegionKind.PHYSICAL_RAM, 0x20000000, 0x20010000),
            _contribution("usable RAM", RegionKind.RAM, 0x20000000, 0x20010000),
            _contribution("peripherals", RegionKind.PERIPHERAL, 0x40000000, 0x50000000),
            _contribution("option control", RegionKind.PROHIBITED, 0x40001000, 0x40001010),
        ),
    )


def _policy(
    root: Path,
    *,
    erase_size: int = 0x800,
    application_end: int = 0x08008000,
    application_authority: bool = True,
) -> SafetyPolicy:
    repository = SafetyMapRepository(FirmStore(root))
    SafetyMapBuilder(repository).build(
        _request(
            erase_size=erase_size,
            application_end=application_end,
            application_authority=application_authority,
        )
    )
    return SafetyPolicy(repository, authority_verifier=lambda _document: None)


def _ihex_record(address: int, record_type: int, data: bytes = b"") -> str:
    payload = bytes([len(data), (address >> 8) & 0xFF, address & 0xFF, record_type, *data])
    return f":{payload.hex().upper()}{(-sum(payload)) & 0xFF:02X}"


def test_memory_writes_and_reads_are_fully_map_contained(tmp_path: Path) -> None:
    policy = _policy(tmp_path)

    assert policy.check_memory_write("board_a", 0x20000000, 32)
    for address in (0x08000000, 0x20000000, 0x40000000):
        assert policy.check_memory_read("board_a", address, 4)

    with pytest.raises(SafetyPolicyError) as flash:
        policy.check_memory_write("board_a", 0x08000000, 32)
    assert flash.value.code == "safety/wrong-region-kind"
    with pytest.raises(SafetyPolicyError) as boundary:
        policy.check_memory_write("board_a", 0x2000FFFF, 32)
    assert boundary.value.code == "safety/unknown"
    with pytest.raises(SafetyPolicyError) as unknown:
        policy.check_memory_read("board_a", 0x60000000, 4)
    assert unknown.value.code == "safety/unknown"
    assert unknown.value.remedy == ("board_safety_refresh",)
    with pytest.raises(SafetyPolicyError) as prohibited:
        policy.check_memory_read("board_a", 0x40001000, 4)
    assert prohibited.value.code == "safety/prohibited"
    assert prohibited.value.remedy == ("choose a mapped, non-prohibited address",)


def test_memory_read_handlers_check_exact_spans_before_backend_access(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    backend_calls: list[tuple[str, int, int]] = []
    checked: list[tuple[str, int, int]] = []

    def check_read(board: str, address: int, size_bytes: int) -> None:
        checked.append((board, address, size_bytes))
        policy.check_memory_read(board, address, size_bytes)

    symbol_address = {"value": 0x20000010}
    memory = build_memory_handlers(
        MemoryToolServices(
            runtime_for=lambda _board: None,
            active_session_id=lambda _board: None,
            duration_ms=lambda _started: 1,
            record_event=lambda *args, **kwargs: None,
            format_refusal=lambda refusal, **kwargs: str(refusal),
            handle_for=lambda _board: object(),
            symbol_artifact_for=lambda _handle: NUCLEO_ELF,
            find_symbols=lambda _artifact, _query: (),
            resolve_symbol=lambda _artifact, symbol: ResolvedSymbol(
                symbol, symbol_address["value"], 128, "STT_OBJECT"
            ),
            read_target_memory=lambda _handle, address, width: (
                backend_calls.append(("scalar", address, width)) or 0
            ),
            read_target_block=lambda _handle, address, length: (
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
    with pytest.raises(SafetyPolicyError):
        memory["read_memory_symbol"]("board_a", "sensitive", 32)
    with pytest.raises(SafetyPolicyError):
        memory["read_memory_address"]("board_a", "0x60000000", 8, 16)
    assert backend_calls == []


def test_old_or_malformed_map_routes_only_to_refresh(tmp_path: Path) -> None:
    repository = SafetyMapRepository(FirmStore(tmp_path))
    path = repository.path("board_a")
    path.parent.mkdir(parents=True)
    path.write_text("schema_version: 1\nboard_id: board_a\n", encoding="utf-8")

    policy = SafetyPolicy(repository, authority_verifier=lambda _document: None)
    with pytest.raises(SafetyPolicyError) as caught:
        policy.current_aggregate("board_a")
    assert caught.value.code == "safety/map-refresh-required"
    assert caught.value.remedy == ("board_safety_refresh",)


def test_register_write_rejects_prohibited_overlap(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    assert policy.check_register_write("board_a", 0x40000000)
    with pytest.raises(SafetyPolicyError) as prohibited:
        policy.check_register_write("board_a", 0x40000FFE)
    assert prohibited.value.code == "safety/prohibited"


def test_breakpoint_requires_current_elf_executable_evidence(tmp_path: Path) -> None:
    policy = _policy(tmp_path)

    assert policy.check_breakpoint("board_a", 0x08000B28, NUCLEO_ELF)
    with pytest.raises(SafetyPolicyError) as non_executable:
        policy.check_breakpoint("board_a", 0x080046A8, NUCLEO_ELF)
    assert non_executable.value.code == "safety/not-executable"
    assert non_executable.value.remedy == ("select_current_elf",)
    with pytest.raises(SafetyPolicyError) as ram:
        policy.check_breakpoint("board_a", 0x20000020, NUCLEO_ELF)
    assert ram.value.code == "safety/breakpoint-outside-partition"


def test_flash_checks_target_segments_entry_vector_and_erase_sectors(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    evidence = policy.check_flash(
        "board_a", BuildRole.APPLICATION, NUCLEO_ELF, current_target="stm32l476rgtx"
    )
    assert evidence.entry_point == 0x08000B29
    assert evidence.vector_table == 0x08000000

    with pytest.raises(SafetyPolicyError) as target:
        policy.check_flash(
            "board_a", BuildRole.APPLICATION, NUCLEO_ELF, current_target="wrong-target"
        )
    assert target.value.code == "safety/target-mismatch"

    unsafe_geometry = _policy(tmp_path / "large-sector", erase_size=0x10000)
    with pytest.raises(SafetyPolicyError) as erase:
        unsafe_geometry.check_flash(
            "board_a", BuildRole.APPLICATION, NUCLEO_ELF, current_target="stm32l476rgtx"
        )
    assert erase.value.code == "safety/erase-sector-outside-partition"


def test_flash_ignores_unselected_adjacent_linker_map(tmp_path: Path) -> None:
    artifact = tmp_path / "firmware.elf"
    artifact.write_bytes(NUCLEO_ELF.read_bytes())
    artifact.with_suffix(".map").write_text("malformed map dialect", encoding="utf-8")
    policy = _policy(tmp_path / "store")

    evidence = policy.check_flash(
        "board_a", BuildRole.APPLICATION, artifact, current_target="stm32l476rgtx"
    )

    assert evidence.entry_point == 0x08000B29


def test_flash_requires_reviewed_application_partition_authority(tmp_path: Path) -> None:
    policy = _policy(tmp_path, application_authority=False)
    with pytest.raises(SafetyPolicyError) as unavailable:
        policy.check_flash(
            "board_a", BuildRole.APPLICATION, NUCLEO_ELF, current_target="stm32l476rgtx"
        )
    assert unavailable.value.code == "safety/partition-authority-unavailable"
    assert unavailable.value.remedy == ("board_safety_refresh",)


def test_hex_requires_matching_elf_companion(tmp_path: Path) -> None:
    selected = tmp_path / "firmware.hex"
    selected.write_bytes(NUCLEO_HEX.read_bytes())
    policy = _policy(tmp_path / "store")

    with pytest.raises(SafetyPolicyError) as missing:
        policy.check_flash(
            "board_a", BuildRole.APPLICATION, selected, current_target="stm32l476rgtx"
        )
    assert missing.value.code == "safety/hex-elf-companion-required"
    assert missing.value.remedy == ("collect_matching_elf_and_hex",)

    selected.with_suffix(".elf").write_bytes(NUCLEO_ELF.read_bytes())
    evidence = policy.check_flash(
        "board_a", BuildRole.APPLICATION, selected, current_target="stm32l476rgtx"
    )
    assert evidence.hex_ranges
    assert evidence.entry_point == 0x08000B29


def test_new_build_bytes_do_not_stale_map_or_unrelated_access(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "store")
    artifact = tmp_path / "new-build.elf"
    artifact.write_bytes(NUCLEO_ELF.read_bytes())
    digest_before = policy.current_aggregate("board_a")

    artifact.write_bytes(b"ordinary newly rebuilt bytes")

    assert policy.current_aggregate("board_a") == digest_before
    assert policy.check_memory_read("board_a", 0x20000000, 4)
    assert policy.check_memory_write("board_a", 0x20000000, 32)


@pytest.mark.parametrize("case", ["partition-crossing", "erase-crossing"])
def test_crafted_flash_rejection_has_zero_backend_calls(tmp_path: Path, case: str) -> None:
    artifact = NUCLEO_ELF
    if case == "partition-crossing":
        build = tmp_path / "crafted"
        build.mkdir()
        (build / "firmware.elf").write_bytes(NUCLEO_ELF.read_bytes())
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
        policy = _policy(tmp_path / "store")
    else:
        policy = _policy(tmp_path / "store", erase_size=0x10000)

    calls: list[str] = []
    identity = FlashArtifactIdentity(
        artifact,
        artifact.suffix,
        artifact.stat().st_size,
        sha256(artifact.read_bytes()).hexdigest(),
        "explicit",
    )

    def validate(_action: str, board: str, selected: Path) -> None:
        policy.check_flash(
            board, BuildRole.APPLICATION, selected, current_target="stm32l476rgtx"
        )

    handlers = build_flash_handlers(
        FlashToolServices(
            runtime_for=lambda _board: None,
            active_session_id=lambda _board: None,
            duration_ms=lambda _started: 1,
            record_event=lambda *args, **kwargs: SimpleNamespace(),
            record_blocked_event=lambda *args, **kwargs: None,
            format_refusal=lambda refusal, **kwargs: str(refusal),
            format_block=lambda blocked, **kwargs: str(blocked),
            ensure_flash_allowed=lambda _runtime: None,
            action_context=lambda action, _board: ActionContext("test", action, None),
            maybe_handle_for=lambda _board: object(),
            handle_for=lambda _board: calls.append("handle") or object(),
            resolve_request=lambda _handle, _selected, _context: ResolvedFlashRequest(
                artifact, identity
            ),
            flash_target=lambda _handle, selected: calls.append("erase/write") or selected,
            handle_mutation_event=lambda _board, _event: None,
            error_code=lambda exc: getattr(exc, "code", "runtime/error"),
            validate_flash=validate,
        )
    )

    with pytest.raises(SafetyPolicyError) as refusal:
        handlers["flash_application"]("board_a", str(artifact))
    assert refusal.value.code == {
        "partition-crossing": "build/hex-outside-elf",
        "erase-crossing": "safety/erase-sector-outside-partition",
    }[case]
    assert calls == []


def test_backend_mutations_never_run_after_containment_refusal(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    calls: list[str] = []

    def check_memory_write(board: str, address: int, width: int) -> None:
        policy.check_memory_write(board, address, width)

    def check_register_write(board: str, address: int) -> None:
        policy.check_register_write(board, address)

    def check_breakpoint(board: str, address: int, elf: Path) -> None:
        policy.check_breakpoint(board, address, elf)

    def reject_flash_target(_action: str, board: str, artifact: Path) -> None:
        policy.check_flash(
            board, BuildRole.APPLICATION, artifact, current_target="wrong-target"
        )

    common = {
        "runtime_for": lambda _board: None,
        "active_session_id": lambda _board: None,
        "duration_ms": lambda _started: 1,
        "record_event": lambda *args, **kwargs: None,
        "format_refusal": lambda refusal, **kwargs: str(refusal),
    }

    memory = build_memory_handlers(
        MemoryToolServices(
            **common,
            handle_for=lambda _board: object(),
            symbol_artifact_for=lambda _handle: NUCLEO_ELF,
            find_symbols=lambda _artifact, _query: (),
            resolve_symbol=lambda _artifact, symbol: ResolvedSymbol(
                symbol, 0x08000000, 4, "STT_OBJECT"
            ),
            read_target_memory=lambda _handle, _address, _width: 0,
            read_target_block=lambda _handle, _address, _length: [],
            write_target_memory=lambda *args: calls.append("memory-write"),
            check_memory_read=lambda _board, _address, _size: None,
            check_memory_write=check_memory_write,
        )
    )
    with pytest.raises(SafetyPolicyError):
        memory["write_memory"]("board_a", "flash_symbol", 1, 32)

    registers = build_register_handlers(
        RegisterToolServices(
            supported_registers=lambda _board: (),
            read_register=lambda *args: "",
            write_register=lambda *args: "",
            masked_register_write=lambda *args: calls.append("register-write") or "",
            check_register_write=check_register_write,
        )
    )
    with pytest.raises(SafetyPolicyError):
        registers["register_write"]("board_a", "0x40001000", "0xff", "0x1")

    breakpoints = build_breakpoint_handlers(
        BreakpointToolServices(
            **common,
            handle_for=lambda _board: object(),
            resolve_symbol=lambda _artifact, symbol: ResolvedSymbol(
                symbol, 0x20000000, 2, "STT_FUNC"
            ),
            set_target_breakpoint=lambda *args: calls.append("breakpoint-set"),
            remove_target_breakpoint=lambda *args: None,
            check_breakpoint=check_breakpoint,
        )
    )
    with pytest.raises(SafetyPolicyError):
        breakpoints["set_breakpoint"]("board_a", "ram_function", str(NUCLEO_ELF))

    identity = FlashArtifactIdentity(
        NUCLEO_ELF,
        ".elf",
        NUCLEO_ELF.stat().st_size,
        sha256(NUCLEO_ELF.read_bytes()).hexdigest(),
        "explicit",
    )
    flash = build_flash_handlers(
        FlashToolServices(
            runtime_for=lambda _board: None,
            active_session_id=lambda _board: None,
            duration_ms=lambda _started: 1,
            record_event=lambda *args, **kwargs: SimpleNamespace(),
            record_blocked_event=lambda *args, **kwargs: None,
            format_refusal=lambda refusal, **kwargs: str(refusal),
            format_block=lambda blocked, **kwargs: str(blocked),
            ensure_flash_allowed=lambda _runtime: None,
            action_context=lambda action, _board: ActionContext("test", action, None),
            maybe_handle_for=lambda _board: object(),
            handle_for=lambda _board: calls.append("flash-handle") or object(),
            resolve_request=lambda _handle, _artifact, _context: ResolvedFlashRequest(
                NUCLEO_ELF, identity
            ),
            flash_target=lambda _handle, artifact: calls.append("flash") or artifact,
            handle_mutation_event=lambda _board, _event: None,
            error_code=lambda exc: getattr(exc, "code", "runtime/error"),
            validate_flash=reject_flash_target,
        )
    )
    with pytest.raises(SafetyPolicyError):
        flash["flash_application"]("board_a", str(NUCLEO_ELF))

    assert calls == []
