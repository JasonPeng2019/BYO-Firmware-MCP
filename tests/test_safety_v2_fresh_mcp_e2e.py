from __future__ import annotations

import json
from pathlib import Path

import mcp.types as types
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.guardrails.gate import GateManager
from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.safety.enforce import SafetyPolicy
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
from pyocd_debug_mcp.safety.refresh import SafetyRefreshRequest, SafetyRefresher
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)
from pyocd_debug_mcp.setup_flow.validate import (
    BoardValidator,
    SafetyMapSnapshot,
    ValidationBackend,
    ValidationHooks,
    ValidationInventory,
    ValidationProbe,
    ValidationRequest,
)
from pyocd_debug_mcp.tools.handshake import register_initialization_handshake
from pyocd_debug_mcp.tools.plans import register_plan_tools
from pyocd_debug_mcp.tools.setup import _load_guidance


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_ELF = ROOT / "firmware/nucleo_l476rg/reference/build/firmware.elf"
BOARD_ID = "fresh_board"
CONNECTION_ID = "fake:probe-001"
TARGET = "stm32l476rgtx"


def _contribution(name: str, kind: RegionKind, start: int, end: int) -> RegionContribution:
    return RegionContribution(
        SafetyRegion(
            name,
            kind,
            AddressRange(start, end),
            (
                Provenance(
                    SourceAuthority.RECONCILED,
                    f"reviewed-test:{name}",
                    "Fresh-root reviewed Safety Layer v2 end-to-end evidence.",
                ),
            ),
        ),
        (
            RegionSource.REVIEWED_DEVICE_SUPPORT,
            RegionSource.REVIEWED_OFFICIAL_EVIDENCE,
        ),
    )


def _reviewed_request() -> SafetyMapBuildRequest:
    return SafetyMapBuildRequest(
        BOARD_ID,
        MapIdentity("STM32L476RGT6", TARGET, "nucleo_l476rg"),
        {
            "schema_version": 2,
            "board_id": BOARD_ID,
            "board_type": "nucleo_l476rg",
            "mcu_part_number": "STM32L476RGT6",
            "mcu_family": "stm32l4",
            "probe_family": "stlink",
            "pyocd_target": TARGET,
        },
        {"source": "pinned reviewed device support"},
        {
            "source": "pinned official evidence",
            "deployment_partition_policy": "reviewed application partition",
        },
        MapGeometry(
            AddressRange(0x08000000, 0x08100000),
            AddressRange(0x20000000, 0x20018000),
            erase_origin=0x08000000,
            erase_size=0x800,
        ),
        MapPartitions(AddressRange(0x08000000, 0x08008000)),
        (
            _contribution("physical flash", RegionKind.PHYSICAL_FLASH, 0x08000000, 0x08100000),
            _contribution("physical RAM", RegionKind.PHYSICAL_RAM, 0x20000000, 0x20018000),
            _contribution("usable RAM", RegionKind.RAM, 0x20000000, 0x20018000),
            _contribution("peripherals", RegionKind.PERIPHERAL, 0x40000000, 0x60000000),
            _contribution("CPU system", RegionKind.CPU_SYSTEM, 0xE0000000, 0xE0100000),
            _contribution("option bytes", RegionKind.PROHIBITED, 0x1FFF7800, 0x1FFF7820),
        ),
    )


def _profile_repository(store: FirmStore) -> ProfileRepository:
    legacy = store.layout.project_root / "boards"
    legacy.mkdir(parents=True)
    profiles = ProfileRepository(store, legacy_board_dir=legacy)
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": BOARD_ID,
                "display_name": "Fresh Board",
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
                "silicon_id_label": "STM32 device-family identity",
            },
        )
    )
    return profiles


def _plan_fields(artifact: Path) -> dict[str, object]:
    return {
        "board_id": BOARD_ID,
        "hypothesis": "The reviewed image is wholly contained by the application partition.",
        "hypothesis_made": True,
        "strategy": "Verify the bound bytes and every runtime flash boundary before one fake call.",
        "strategy_evaluated": True,
        "expected_fail_return": "A plan or safety refusal with no backend call.",
        "expected_success_return": "One contained fake flash backend call.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": {"artifact": str(artifact)},
    }


def _ihex_record(address: int, record_type: int, data: bytes = b"") -> str:
    payload = bytes([len(data), address >> 8, address & 0xFF, record_type, *data])
    return f":{payload.hex().upper()}{(-sum(payload)) & 0xFF:02X}"


def _text(result: object) -> str:
    content = getattr(result, "content")
    assert content and isinstance(content[0], types.TextContent)
    return content[0].text


@pytest.mark.asyncio
async def test_fresh_root_mcp_safety_v2_green_gate(tmp_path: Path) -> None:
    """One fresh Server Run proves the complete safe route without real hardware mutation."""

    store = FirmStore(tmp_path / "fresh-project")
    repository = SafetyMapRepository(store)
    builder = SafetyMapBuilder(repository)
    profiles = _profile_repository(store)
    gate = GateManager()
    backend_calls: list[tuple[str, str]] = []
    validation_calls: list[str] = []

    def associate_refreshed_map(board_id: str, digest: str, identity_changed: bool) -> None:
        if identity_changed:
            gate.clear(board_id, "reviewed identity changed")
        else:
            gate.refresh_map_stamp(board_id, CONNECTION_ID, digest)

    refresher = SafetyRefresher(
        store,
        derive=lambda board_id: builder.derive(_reviewed_request()),
        has_live_identity=lambda board_id: gate.live_identity(board_id) is not None,
        on_commit=associate_refreshed_map,
    )
    policy = SafetyPolicy(repository, authority_verifier=lambda _document: None)

    def inventory() -> ValidationInventory:
        validation_calls.append("inventory")
        return ValidationInventory(
            probes=(ValidationProbe("probe-a", "Fake ST-Link", "stlink", "PROBE-001"),)
        )

    def connect(_profile: object, _probe: ValidationProbe, _timeout: float) -> object:
        validation_calls.append("connect")
        return object()

    def read_identity(_connection: object, address: int, width: int, _timeout: float) -> int:
        assert (address, width) == (0xE0042000, 32)
        validation_calls.append("read_identity")
        return 0x10016415

    validator = BoardValidator(
        profiles,
        ReportWriter(store),
        ValidationBackend(
            inventory,
            lambda target: validation_calls.append("target_supported") or target == TARGET,
            connect,
            read_identity,
            lambda *_args: pytest.fail("lean validation must never capture UART"),
            lambda _connection: validation_calls.append("close"),
        ),
        hooks=ValidationHooks(
            load_safety_map=lambda _profile: SafetyMapSnapshot(
                True, True, repository.load_current(BOARD_ID).canonical_digest
            ),
            stamp_session=lambda board_id, validation_run, _probe_id, probe_uid, observed, digest: (
                gate.stamp_validation(
                    board_id=board_id,
                    connection_id=CONNECTION_ID,
                    probe_identity=probe_uid or "probe-a",
                    observed_mcu=observed,
                    validation_run=validation_run,
                    map_digest=digest,
                )
                is not None
            ),
            record_mismatch=lambda *_args: pytest.fail("reviewed identity unexpectedly mismatched"),
        ),
    )

    run = ServerRun(run_id="run-fresh-safety-v2")
    mcp = RegistryFastMCP("fresh-safety-v2")
    engine = PlanEngine(run, mcp.registry)

    register_initialization_handshake(mcp, mcp.registry, run)

    def setup_overview(board_names: list[str] | None = None) -> str:
        """Route familiar names without touching hardware."""

        names = board_names or []
        route = "board_validate" if "Fresh Board" in names else "board_setup-plan"
        return json.dumps({"status": "setup_overview", "route": route})

    def load_setup_tool(board_id: str, tool_name: str) -> str:
        """Return the production conversational guidance for one setup route."""

        return json.dumps(_load_guidance(board_id, tool_name))

    def board_safety_refresh(board_id: str) -> str:
        """Rebuild stable reviewed map authority; never accept build artifacts or ranges."""

        return json.dumps(
            refresher.refresh(SafetyRefreshRequest(board_id, "fresh-e2e-refresh")).to_payload()
        )

    def board_validate(board_id: str, probe_id: str | None = None) -> str:
        """Lean validation for the exact three trigger categories in loaded guidance."""

        return json.dumps(validator.validate(ValidationRequest(board_id, probe_id)).to_payload())

    mcp.add_tool(setup_overview)
    mcp.add_tool(load_setup_tool)
    mcp.add_tool(board_safety_refresh)
    mcp.add_tool(board_validate)
    register_plan_tools(
        mcp,
        engine,
        (PLAN_DEFINITIONS["flash_application"],),
        lambda _board_id: None,
    )

    def flash_application(board_id: str, artifact: str) -> str:
        """Execute only after plan digest, live gate, and runtime containment checks pass."""

        selected = Path(artifact)

        def preconditions() -> None:
            digest = policy.current_aggregate(board_id)
            gate.require_write(board_id, CONNECTION_ID, digest)
            policy.check_flash(board_id, BuildRole.APPLICATION, selected, current_target=TARGET)

        engine.enforce(
            "flash_application",
            board_id,
            {"artifact": artifact},
            preconditions=preconditions,
        )
        backend_calls.append((board_id, str(selected)))
        return "fake flash completed after containment"

    mcp.add_tool(flash_application)
    mcp.registry.configure(
        "flash_application",
        hidden=True,
        locked=True,
        prerequisite="flash_application-plan",
    )

    artifact = tmp_path / "build" / "firmware.elf"
    artifact.parent.mkdir()
    artifact.write_bytes(REFERENCE_ELF.read_bytes())

    async with create_connected_server_and_client_session(mcp) as session:
        # Conversational contract evidence: handshake -> profile route -> exact validation and
        # refresh guidance. Routine build guidance proceeds collect -> flash plan, not refresh.
        handshake = _text(await session.call_tool("initialization_handshake", {}))
        handshake_prose = " ".join(handshake.split())
        assert "setup_overview" in handshake
        assert "Continue with the matching flash plan" in handshake_prose
        assert "board_safety_refresh only for an actual stable-map problem" in handshake_prose

        known = json.loads(
            _text(await session.call_tool("setup_overview", {"board_names": ["Fresh Board"]}))
        )
        unknown = json.loads(
            _text(await session.call_tool("setup_overview", {"board_names": ["New Board"]}))
        )
        assert (known["route"], unknown["route"]) == ("board_validate", "board_setup-plan")

        validate_guide = json.loads(
            _text(
                await session.call_tool(
                    "load_setup_tool",
                    {"board_id": BOARD_ID, "tool_name": "board_validate"},
                )
            )
        )["guidance"]
        assert all(
            phrase in validate_guide["when_to_use"]
            for phrase in ("no live proof", "connection identity changes", "hardware identity")
        )
        assert all(
            phrase in validate_guide["when_not_to_use"]
            for phrase in ("build", "flash", "reset/halt", "UART", "safety refresh")
        )
        refresh_guide = json.loads(
            _text(
                await session.call_tool(
                    "load_setup_tool",
                    {"board_id": BOARD_ID, "tool_name": "board_safety_refresh"},
                )
            )
        )["guidance"]
        assert "missing, malformed, old, or inconsistent map" in refresh_guide["when_to_use"]
        assert "ordinary rebuild" in refresh_guide["when_not_to_use"]
        assert "accepts no build artifacts or caller ranges" in refresh_guide["when_not_to_use"]

        refresh = json.loads(
            _text(await session.call_tool("board_safety_refresh", {"board_id": BOARD_ID}))
        )
        assert refresh["status"] == "safety_refresh_completed"
        assert refresh["observed"]["validation_required"] is True
        assert [path.name for path in repository.path(BOARD_ID).parent.iterdir()] == [
            "memory_map.yaml"
        ]

        validation = json.loads(
            _text(await session.call_tool("board_validate", {"board_id": BOARD_ID}))
        )
        assert validation["status"] == "validation_passed"
        assert validation_calls == [
            "inventory",
            "target_supported",
            "connect",
            "read_identity",
            "close",
        ]
        assert gate.snapshot(BOARD_ID) is not None

        definition = PLAN_DEFINITIONS["flash_application"]
        null_result = await session.call_tool(
            "flash_application-plan",
            {name: None for name in definition.null_field_names},
        )
        null_text = _text(null_result)
        assert "all-NULL" in null_text
        assert "artifact" in null_text

        accepted = await session.call_tool("flash_application-plan", _plan_fields(artifact))
        assert accepted.isError is not True
        assert "accepted" in _text(accepted)
        flashed = await session.call_tool(
            "flash_application", {"board_id": BOARD_ID, "artifact": str(artifact)}
        )
        assert flashed.isError is not True
        assert backend_calls == [(BOARD_ID, str(artifact))]

        # Artifact drift is rejected by the plan binding before gate, containment, budget, or
        # backend execution. Restoring bytes cannot resurrect the invalidated plan.
        artifact.write_bytes(REFERENCE_ELF.read_bytes())
        await session.call_tool("flash_application-plan", _plan_fields(artifact))
        artifact.write_bytes(b"changed after populated-plan acceptance")
        drift = await session.call_tool(
            "flash_application", {"board_id": BOARD_ID, "artifact": str(artifact)}
        )
        assert drift.isError is True
        assert "artifact changed" in _text(drift).casefold()
        assert backend_calls == [(BOARD_ID, str(artifact))]

        # A validly bound HEX plus matching ELF is still rejected when its requested load range
        # crosses the reviewed executable evidence/partition boundary; no fake burn is attempted.
        boundary_hex = artifact.with_name("boundary.hex")
        boundary_elf = boundary_hex.with_suffix(".elf")
        boundary_elf.write_bytes(REFERENCE_ELF.read_bytes())
        boundary_hex.write_text(
            "\n".join(
                (
                    _ihex_record(0, 4, bytes((0x08, 0x10))),
                    _ihex_record(0, 0, b"outside reviewed application"),
                    _ihex_record(0, 1),
                )
            )
            + "\n",
            encoding="ascii",
        )
        await session.call_tool("flash_application-plan", _plan_fields(boundary_hex))
        boundary = await session.call_tool(
            "flash_application", {"board_id": BOARD_ID, "artifact": str(boundary_hex)}
        )
        assert boundary.isError is True
        assert "absent from elf load data" in _text(boundary).casefold()
        assert backend_calls == [(BOARD_ID, str(artifact))]
