from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import mcp.types as types
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from pyocd_debug_mcp import server
from pyocd_debug_mcp.firmstore.cache import AttachmentCache
from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.guardrails.flash_gate import resolve_flash_request
from pyocd_debug_mcp.guardrails.gate import GateManager
from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.kernel.run_state import create_server_run
from pyocd_debug_mcp.safety.enforce import SafetyPolicy
from pyocd_debug_mcp.safety.fingerprints import FingerprintInputs, FingerprintSource
from pyocd_debug_mcp.safety.linker import BuildRole
from pyocd_debug_mcp.safety.map_build import (
    RegionContribution,
    SafetyArtifactRepository,
)
from pyocd_debug_mcp.safety.refresh import SafetyRefreshRequest, SafetyRefresher
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)
from pyocd_debug_mcp.services.session_runtime import ActionContext
from pyocd_debug_mcp.setup_flow.validate import (
    BoardValidator,
    Layer0Snapshot,
    ValidationBackend,
    ValidationHooks,
    ValidationInventory,
    ValidationProbe,
    ValidationRequest,
)
from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers
from pyocd_debug_mcp.tools.plans import register_plan_tools

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_ELF = ROOT / "firmware/nucleo_l476rg/reference/build/firmware.elf"
BOARD_ID = "safety_v2_e2e"
SESSION_ID = "session:safety-v2-e2e"
CONNECTION_ID = "connection:safety-v2-e2e"
TARGET = "stm32l476rgtx"


def _text(result: types.CallToolResult) -> str:
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    return content.text


def _region(name: str, kind: RegionKind, start: int, end: int) -> RegionContribution:
    return RegionContribution(
        SafetyRegion(
            name,
            kind,
            AddressRange(start, end),
            (Provenance(SourceAuthority.RECONCILED, name, "Safety v2 E2E fixture"),),
        ),
        (FingerprintSource.EVIDENCE, FingerprintSource.GEOMETRY),
    )


def _ihex_record(address: int, record_type: int, data: bytes = b"") -> str:
    payload = bytes([len(data), (address >> 8) & 0xFF, address & 0xFF, record_type, *data])
    return f":{payload.hex().upper()}{(-sum(payload)) & 0xFF:02X}"


@pytest.mark.asyncio
async def test_fresh_root_mcp_refresh_validate_plan_and_flash_containment(
    tmp_path: Path,
) -> None:
    """Exercise the complete v2 authority path through an in-process MCP client."""

    store = FirmStore(tmp_path)
    profiles = ProfileRepository(store, legacy_board_dir=tmp_path / "legacy-boards")
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": BOARD_ID,
                "display_name": "Safety v2 E2E board",
                "mcu_part_number": "STM32L476RGT6",
                "mcu_family": "stm32l4",
                "probe_family": "stlink",
                "pyocd_target": TARGET,
            }
        )
    )
    profiles.commit_optional(
        profiles.stage_optional(
            BOARD_ID,
            {
                "silicon_id_address": 0xE0042000,
                "silicon_id_expected": 0x415,
                "silicon_id_mask": 0xFFF,
                "silicon_id_width_bits": 32,
                "silicon_id_label": "STM32 device family",
            },
        )
    )
    profile = profiles.load(BOARD_ID, include_legacy=False)
    inputs = FingerprintInputs(
        profile.to_document(),
        {
            "board_type": "nucleo_l476rg",
            "mcu_part_number": "STM32L476RGT6",
            "target": TARGET,
        },
        {"id": "Keil.STM32L4xx_DFP", "version": "reviewed-test"},
        {
            "official_document": {"revision": "reviewed-test"},
            "reconciliation": {"status": "agreement"},
            "deployment_policy": {
                "application_start": 0x08000000,
                "application_end": 0x08100000,
                "application_authoritative": True,
                "bootloader_authoritative": False,
            },
        },
        {"configuration": None, "artifacts": []},
        {"configuration": None, "artifacts": []},
        {
            "flash_start": 0x08000000,
            "flash_end": 0x08100000,
            "ram_start": 0x20000000,
            "ram_end": 0x20018000,
            "erase_origin": 0x08000000,
            "erase_size": 0x800,
        },
        {"memory_map": 2, "evidence": 2, "catalog": 2},
    )
    contributions = (
        _region("physical flash", RegionKind.PHYSICAL_FLASH, 0x08000000, 0x08100000),
        _region("physical RAM", RegionKind.PHYSICAL_RAM, 0x20000000, 0x20018000),
        _region("usable RAM", RegionKind.RAM, 0x20000000, 0x20018000),
        _region("peripherals", RegionKind.PERIPHERAL, 0x40000000, 0x60000000),
        _region("system control", RegionKind.CPU_SYSTEM, 0xE0000000, 0xE0100000),
        _region("option bytes", RegionKind.PROHIBITED, 0x1FFF7800, 0x1FFF8000),
    )
    refresh_request = SafetyRefreshRequest(
        BOARD_ID,
        "safety-v2-e2e-refresh",
        inputs,
        (
            FingerprintSource.PROFILE,
            FingerprintSource.PART_TARGET,
            FingerprintSource.PACK,
            FingerprintSource.EVIDENCE,
            FingerprintSource.GEOMETRY,
            FingerprintSource.SCHEMA,
        ),
        contributions,
    )
    repository = SafetyArtifactRepository(store)
    policy = SafetyPolicy(repository)
    run = create_server_run()
    gate = GateManager(run.gates)
    refresher = SafetyRefresher(store)

    class FakeValidationBackend:
        def inventory(self) -> ValidationInventory:
            return ValidationInventory(
                probes=(ValidationProbe("probe-a", "Fake ST-Link", "stlink", "PROBE-A"),)
            )

        def connect(self, _profile, _probe, _timeout: float) -> object:
            return object()

        def read_memory(
            self, _connection: object, address: int, width: int, _timeout: float
        ) -> int:
            assert (address, width) == (0xE0042000, 32)
            return 0x415

        @staticmethod
        def close(_connection: object) -> None:
            return None

    validation_backend = FakeValidationBackend()

    def load_layer0(_profile) -> Layer0Snapshot:
        current = repository.load_current(BOARD_ID)
        return Layer0Snapshot(True, True, current.map_digest, "current single-file map")

    def stamp(
        board_id: str,
        hardware_result: str,
        _probe_id: str,
        probe_uid: str | None,
        observed_identity: str,
        map_digest: str,
    ) -> bool:
        gate.stamp_validation(
            board_id=board_id,
            connection_id=CONNECTION_ID,
            hardware_result=hardware_result,
            probe_identity=probe_uid or "PROBE-A",
            observed_identity=observed_identity,
            aggregate_fingerprint=map_digest,
        )
        return True

    validator = BoardValidator(
        profiles,
        ReportWriter(store),
        ValidationBackend(
            validation_backend.inventory,
            lambda _target: True,
            validation_backend.connect,
            validation_backend.read_memory,
            lambda *_args: pytest.fail("lean validation must not capture UART"),
            validation_backend.close,
        ),
        cache=AttachmentCache(store),
        hooks=ValidationHooks(load_layer0, stamp),
    )

    mcp = RegistryFastMCP("safety-v2-e2e")
    definition = PLAN_DEFINITIONS["flash_application"]
    engine = PlanEngine(
        run,
        mcp.registry,
        scope_validator=lambda _definition, _board, _session: None,
        binding_provider=server._bind_plan_resources,
        binding_validator=server._validate_plan_artifact_binding,
    )
    backend_mutations: list[Path] = []

    def validate_flash(_action: str, board: str, artifact: Path) -> None:
        policy.check_flash(
            board,
            BuildRole.APPLICATION,
            artifact,
            current_target=TARGET,
        )

    flash_handlers = build_flash_handlers(
        FlashToolServices(
            runtime_for=lambda _board: None,
            active_session_id=lambda _board: SESSION_ID,
            duration_ms=lambda _started: 0,
            record_event=lambda *_args, **_kwargs: SimpleNamespace(),
            record_blocked_event=lambda *_args, **_kwargs: SimpleNamespace(),
            format_refusal=lambda refusal, **_kwargs: str(refusal),
            format_block=lambda blocked, **_kwargs: str(blocked),
            ensure_flash_allowed=lambda _runtime: None,
            action_context=lambda action, _board: ActionContext("mcp", action, SESSION_ID),
            maybe_handle_for=lambda _board: object(),
            handle_for=lambda _board: object(),
            resolve_request=lambda handle, artifact, context: resolve_flash_request(
                handle,
                explicit_path=artifact,
                action_context=context,
            ),
            flash_target=lambda _handle, artifact: (
                backend_mutations.append(artifact) or artifact
            ),
            handle_mutation_event=lambda _board, _event: None,
            error_code=lambda exc: f"test/{type(exc).__name__}",
            validate_flash=validate_flash,
        )
    )

    def refresh_tool(board_id: str) -> str:
        assert board_id == BOARD_ID
        result = refresher.refresh(refresh_request)
        expected_ref = (store.layout.safety_reference_prefix(board_id) / "memory_map.yaml").as_posix()
        current_profile = profiles.load(board_id, include_legacy=False)
        if current_profile.safety_ref != expected_ref:
            profiles.commit_safety_ref(profiles.stage_safety_ref(board_id, expected_ref))
        return json.dumps(result.to_payload())

    def validate_tool(board_id: str) -> str:
        return json.dumps(validator.validate(ValidationRequest(board_id)).to_payload())

    mcp.add_tool(refresh_tool, name="board_safety_refresh", structured_output=False)
    mcp.add_tool(validate_tool, name="board_validate", structured_output=False)
    mcp.add_tool(
        flash_handlers["flash_application"],
        name="flash_application",
        structured_output=False,
    )
    mcp.registry.configure(
        "flash_application",
        hidden=True,
        locked=True,
        prerequisite=definition.plan_tool_name,
    )
    register_plan_tools(mcp, engine, (definition,), lambda _board: SESSION_ID)
    def enforce_flash(
        action: str, board_id: str, arguments: Mapping[str, object]
    ) -> None:
        artifact = str(arguments["artifact"])

        def preconditions() -> None:
            gate.require_write(board_id, CONNECTION_ID, policy.current_aggregate(board_id))
            policy.check_flash(
                board_id,
                BuildRole.APPLICATION,
                Path(artifact),
                current_target=TARGET,
            )

        engine.enforce(
            action,
            board_id,
            {"artifact": artifact},
            session_id=SESSION_ID,
            preconditions=preconditions,
        )

    mcp.configure_guarded_dispatch(
        "flash_application",
        guard=enforce_flash,
    )

    safe_elf = tmp_path / "safe.elf"
    safe_bytes = REFERENCE_ELF.read_bytes()
    safe_elf.write_bytes(safe_bytes)
    unsafe_elf = tmp_path / "unsafe.elf"
    unsafe_elf.write_bytes(safe_bytes)
    unsafe_hex = tmp_path / "unsafe.hex"
    unsafe_hex.write_text(
        "\n".join(
            (
                _ihex_record(0, 4, bytes.fromhex("0810")),
                _ihex_record(0, 0, b"outside partition"),
                _ihex_record(0, 1),
            )
        )
        + "\n",
        encoding="ascii",
    )

    null_plan = {name: None for name in definition.null_field_names}

    def populated_plan(artifact: Path) -> dict[str, object]:
        return {
            "board_id": BOARD_ID,
            "hypothesis": "The selected reviewed application image fits the stable partition.",
            "hypothesis_made": True,
            "strategy": "Verify plan-bound bytes and containment before one fake backend write.",
            "strategy_evaluated": True,
            "expected_fail_return": "A typed refusal before any fake backend mutation.",
            "expected_success_return": "One contained fake application flash result.",
            "max_calls": 1,
            "max_calls_buffer": 0,
            "action_parameters": {"artifact": str(artifact)},
        }

    async with create_connected_server_and_client_session(mcp) as session:
        refreshed = await session.call_tool("board_safety_refresh", {"board_id": BOARD_ID})
        refresh_payload = json.loads(_text(refreshed))
        assert refresh_payload["status"] == "safety_refresh_completed"
        assert {path.name for path in store.layout.safety_board(BOARD_ID).iterdir()} == {
            "memory_map.yaml"
        }

        validated = await session.call_tool("board_validate", {"board_id": BOARD_ID})
        validation_payload = json.loads(_text(validated))
        assert validation_payload["status"] == "validation_passed"
        assert gate.snapshot(BOARD_ID) is not None

        initialized = await session.call_tool(definition.plan_tool_name, null_plan)
        assert initialized.isError is not True
        accepted = await session.call_tool(definition.plan_tool_name, populated_plan(safe_elf))
        assert accepted.isError is not True
        safe_elf.write_bytes(b"changed after plan acceptance")
        changed = await session.call_tool(
            "flash_application", {"board_id": BOARD_ID, "artifact": str(safe_elf)}
        )
        assert changed.isError is True
        assert "changed" in _text(changed).casefold()
        assert backend_mutations == []

        safe_elf.write_bytes(safe_bytes)
        stale = await session.call_tool(
            "flash_application", {"board_id": BOARD_ID, "artifact": str(safe_elf)}
        )
        assert stale.isError is True
        assert "flash_application-plan" in _text(stale)
        assert backend_mutations == []

        accepted_again = await session.call_tool(
            definition.plan_tool_name, populated_plan(safe_elf)
        )
        assert accepted_again.isError is not True
        succeeded = await session.call_tool(
            "flash_application", {"board_id": BOARD_ID, "artifact": str(safe_elf)}
        )
        assert succeeded.isError is not True
        assert backend_mutations == [safe_elf]

        accepted_unsafe = await session.call_tool(
            definition.plan_tool_name, populated_plan(unsafe_hex)
        )
        assert accepted_unsafe.isError is not True
        rejected = await session.call_tool(
            "flash_application", {"board_id": BOARD_ID, "artifact": str(unsafe_hex)}
        )
        assert rejected.isError is True
        assert backend_mutations == [safe_elf]
