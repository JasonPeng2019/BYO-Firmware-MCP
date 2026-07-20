from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import mcp.types as types
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from pyocd_debug_mcp import probe_inventory, server
from pyocd_debug_mcp.firmstore.cache import CacheResolution
from pyocd_debug_mcp.pack_provision import PackProvisionError, PackSpec, VerifiedPack
from pyocd_debug_mcp.setup_flow.board_catalog import (
    ReviewedSupportNotFoundError,
    catalog_board,
)
from pyocd_debug_mcp.setup_flow.device_support import BuiltInTargetGeometryError
from pyocd_debug_mcp.setup_flow.preflight import (
    FriendlyChoice,
    PreflightDecision,
    ProbeCandidate,
    SerialCandidate,
    SetupUserInput,
)
from pyocd_debug_mcp.setup_flow.validate import (
    ValidationInventory,
    ValidationProbe,
    ValidationSerial,
)


@pytest.fixture(autouse=True)
def _resolve_support_without_coupling_inventory_tests_to_pdf_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep these tests focused on live hardware inventory and exact target routing."""

    catalog_by_package = {
        "nRF52833-QIAA": catalog_board("nrf52833dk"),
        "nRF52840-QIAA": catalog_board("nrf52840dk"),
        "STM32L476RGT6": catalog_board("nucleo_l476rg"),
    }

    def resolve(user_input: SetupUserInput) -> SimpleNamespace:
        catalog = catalog_by_package.get(user_input.mcu_part_number)
        if catalog is None:
            raise ReviewedSupportNotFoundError("No exact reviewed MCU/datasheet support.")
        return SimpleNamespace(catalog=catalog)

    monkeypatch.setattr(server, "_resolve_setup_support", resolve)


def _tool_json(result: types.CallToolResult) -> dict[str, object]:
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    return json.loads(content.text)


async def test_probe_provider_generic_and_cli_fallbacks_share_one_live_mcp_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FutureProbe:
        description = "Future Universal Probe"
        unique_id = "FUTURE-API"
        associated_board_info = None

    class FakeConnectHelper:
        phase = "api"

        @classmethod
        def get_all_connected_probes(
            cls,
            *,
            blocking: bool,
            print_wait_message: bool,
        ) -> list[FutureProbe]:
            assert blocking is False
            assert print_wait_message is False
            return [FutureProbe()] if cls.phase == "api" else []

    seen_commands: list[list[str]] = []

    def fake_run(command: list[str]) -> tuple[int, str, str]:
        seen_commands.append(command)
        if FakeConnectHelper.phase == "known_cli":
            return 0, "  0   STM32 STLink    STLINK-CLI", ""
        return 0, "  0   Future Universal Probe    FUTURE-CLI", ""

    monkeypatch.setattr(server._profile_repository, "load_all", lambda **_kwargs: ())
    monkeypatch.setattr(server, "list_serial_ports", lambda: [])
    monkeypatch.setattr(server, "_run_cmd", fake_run)
    monkeypatch.setattr(probe_inventory, "ConnectHelper", FakeConnectHelper)
    monkeypatch.setitem(probe_inventory.PROBE_CLASSES, "futureprovider", FutureProbe)
    monkeypatch.setattr(
        probe_inventory,
        "configured_probe_cli_commands",
        lambda: (("configured-probe-cli", "inventory", "--machine-safe"),),
    )

    async with create_connected_server_and_client_session(server.mcp) as session:
        generic = _tool_json(
            await session.call_tool("setup_overview", {"board_names": ["Generic Provider"]})
        )
        FakeConnectHelper.phase = "known_cli"
        configured_fallback = _tool_json(
            await session.call_tool("setup_overview", {"board_names": ["Configured Fallback"]})
        )
        FakeConnectHelper.phase = "unknown_cli"
        unknown_fallback = _tool_json(
            await session.call_tool("setup_overview", {"board_names": ["Unknown Fallback"]})
        )

    assert generic["connections"][0]["probe_family"] == "futureprovider"  # type: ignore[index]
    assert configured_fallback["connections"][0]["probe_family"] == "stlink"  # type: ignore[index]
    assert unknown_fallback["connections"][0]["probe_family"] == "unknown"  # type: ignore[index]
    assert seen_commands == [
        ["configured-probe-cli", "inventory", "--machine-safe"],
        ["configured-probe-cli", "inventory", "--machine-safe"],
    ]


def test_setup_inventory_scopes_probe_and_uart_by_stable_connection_identity(
    monkeypatch,
) -> None:
    inventory = ValidationInventory(
        (
            ValidationProbe("066ABC", "ST-Link", "stlink", "066ABC"),
            ValidationProbe("683377322", "J-Link", "jlink", "683377322"),
        ),
        (
            ValidationSerial("066ABC", "COM12", "ST UART", "066ABC", 1, 2),
            ValidationSerial("000683377322", "COM11", "J-Link UART", "000683377322", 3, 4),
        ),
    )
    monkeypatch.setattr(server, "_validation_inventory", lambda: inventory)
    monkeypatch.setattr(server, "_target_names", lambda: ("nrf52833",))
    monkeypatch.setattr(server, "load_manifest", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        server,
        "resolve_board_config",
        lambda _board_id, _path: SimpleNamespace(pyocd_target="nrf52833"),
    )
    resolutions: list[tuple[str, str, tuple[str, ...]]] = []

    def resolve_cache(board_id, probe, serial):
        resolutions.append(
            (board_id, probe.usb_serial, tuple(endpoint.port_path for endpoint in serial))
        )
        return CacheResolution(False, "no_record")

    monkeypatch.setattr(server._attachment_cache, "resolve", resolve_cache)

    result = server._setup_inventory(
        SetupUserInput(
            "nrf52833dk",
            "probe:000683377322",
            "nRF52833 DK",
            "nRF52833-QIAA",
            115200,
        )
    )

    assert [probe.probe_id for probe in result.probes] == ["683377322"]
    assert [serial.serial_id for serial in result.serial_ports] == ["000683377322"]
    assert result.serial_ports[0].external_adapter is False
    assert result.serial_ports[0].provably_mapped is True
    assert result.exact_detected_targets == ("nrf52833",)
    assert resolutions == [("nrf52833dk", "683377322", ("COM11",))]


def test_setup_inventory_rejects_a_different_sole_uart_from_explicit_binding(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            (ValidationProbe("683377322", "J-Link", "jlink", "683377322"),),
            (ValidationSerial("OTHER-UART", "COM99", "Other UART", "OTHER-UART", 1, 2),),
        ),
    )
    monkeypatch.setattr(server, "_target_names", lambda: ("nrf52840",))
    monkeypatch.setattr(server, "load_manifest", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(server, "resolve_board_config", lambda *_args: None)

    result = server._setup_inventory(
        SetupUserInput(
            "nf_board",
            "683377322",
            "NF Board",
            "nRF52840-QIAA",
            115200,
            serial_id="683377322",
        )
    )

    assert result.serial_ports == ()


def test_setup_inventory_marks_unassociated_selected_uart_as_external(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            (ValidationProbe("PROBE-1", "Probe", "cmsisdap", "PROBE-1"),),
            (ValidationSerial("UART-9", "COM9", "USB UART", "UART-9", 1, 2),),
        ),
    )
    monkeypatch.setattr(server, "_target_names", lambda: ("future_target",))
    monkeypatch.setattr(server, "load_manifest", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(server, "resolve_board_config", lambda *_args: None)

    result = server._setup_inventory(
        SetupUserInput(
            "future_board",
            "PROBE-1",
            "Future board",
            "PART-1",
            115200,
            serial_id="UART-9",
        )
    )

    assert len(result.serial_ports) == 1
    assert result.serial_ports[0].external_adapter is True
    assert result.serial_ports[0].provably_mapped is False


def test_setup_inventory_rejects_wrong_single_probe_and_port_path_as_uart_id(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            (ValidationProbe("OTHER-PROBE", "Other J-Link", "jlink", "OTHER-PROBE"),),
            (ValidationSerial("683377322", "COM11", "J-Link UART", "683377322", 1, 2),),
        ),
    )
    monkeypatch.setattr(server, "_target_names", lambda: ("nrf52840",))
    monkeypatch.setattr(server, "load_manifest", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(server, "resolve_board_config", lambda *_args: None)

    result = server._setup_inventory(
        SetupUserInput(
            "nf_board",
            "EXPECTED-PROBE",
            "NF Board",
            "nRF52840-QIAA",
            115200,
            serial_id="COM11",
        )
    )

    assert result.probes == ()
    assert result.serial_ports == ()


def test_reviewed_catalog_mapping_ignores_legacy_profile_target_authority(monkeypatch) -> None:
    monkeypatch.setattr(server, "_validation_inventory", lambda: ValidationInventory())
    monkeypatch.setattr(server, "_target_names", lambda: ("stm32l476rgtx",))
    monkeypatch.setattr(server, "load_manifest", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        server,
        "resolve_board_config",
        lambda _board_id, _path: SimpleNamespace(pyocd_target="stm32l476rgtx"),
    )

    matching = server._setup_inventory(
        SetupUserInput(
            "nucleo_l476rg",
            "probe:missing",
            "Nucleo-L476RG",
            "STM32L476RGT6",
            115200,
        )
    )
    mismatched = server._setup_inventory(
        SetupUserInput(
            "nucleo_l476rg",
            "probe:missing",
            "Nucleo-L476RG",
            "STM32F401RE",
            115200,
        )
    )

    assert matching.exact_detected_targets == ("stm32l476rgtx",)
    assert mismatched.exact_detected_targets == ()


def test_fresh_reviewed_catalog_maps_exact_package_to_builtin_target_without_research(
    monkeypatch,
) -> None:
    monkeypatch.setattr(server, "_validation_inventory", lambda: ValidationInventory())
    monkeypatch.setattr(server, "_target_names", lambda: ("nrf52840", "nrf52833"))
    monkeypatch.setattr(server, "load_manifest", lambda *_args, **_kwargs: ())

    def no_existing_profile(_board_id, _path):
        raise server.ConfigError("fresh profile has no board YAML")

    monkeypatch.setattr(server, "resolve_board_config", no_existing_profile)

    exact = server._setup_inventory(
        SetupUserInput(
            "nf_board",
            "probe:missing",
            "NF Board",
            "nRF52840-QIAA",
            115200,
        )
    )
    family_only = server._setup_inventory(
        SetupUserInput(
            "nf_board",
            "probe:missing",
            "NF Board",
            "nRF52840",
            115200,
        )
    )
    wrong_suffix = server._setup_inventory(
        SetupUserInput(
            "nf_board",
            "probe:missing",
            "NF Board",
            "nRF52840-EVIL",
            115200,
        )
    )

    assert exact.exact_detected_targets == ("nrf52840",)
    assert family_only.exact_detected_targets == ()
    assert wrong_suffix.exact_detected_targets == ()


def test_fresh_reviewed_catalog_uses_verified_repository_pack_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_validation_inventory", lambda: ValidationInventory())
    monkeypatch.setattr(server, "_target_names", lambda: ())
    catalog = catalog_board("nucleo_l476rg")
    assert catalog.pyocd_pack_filename is not None
    assert catalog.pyocd_pack_sha256 is not None
    spec = PackSpec(
        id="Keil.STM32L4xx_DFP",
        version="3.1.0",
        filename=catalog.pyocd_pack_filename,
        url="https://example.invalid/stm32.pack",
        sha256=catalog.pyocd_pack_sha256,
        provides_targets=("stm32l476rgtx",),
        needed_by_boards=("nucleo_l476rg",),
    )
    monkeypatch.setattr(
        server,
        "load_manifest",
        lambda path=None: (spec,) if path is None else (),
    )
    selected = VerifiedPack(path=Path("stm32.pack"), spec=spec, payload=b"pack")
    monkeypatch.setattr(server, "verified_pack_for_target", lambda _target: selected)

    result = server._setup_inventory(
        SetupUserInput(
            "stm32_board",
            "probe:missing",
            "STM32 Board",
            "STM32L476RGT6",
            115200,
        )
    )

    assert result.manifest_targets == ("stm32l476rgtx",)
    assert result.exact_detected_targets == ("stm32l476rgtx",)

    def invalid_pack(_target: str) -> object:
        raise PackProvisionError("tampered")

    monkeypatch.setattr(server, "verified_pack_for_target", invalid_pack)
    refused = server._setup_inventory(
        SetupUserInput(
            "stm32_board",
            "probe:missing",
            "STM32 Board",
            "STM32L476RGT6",
            115200,
        )
    )
    assert refused.exact_detected_targets == ()
    assert refused.blocking_error is not None
    assert refused.blocking_error.code == "setup/reviewed-pack-unavailable"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("filename", "wrong.pack"),
        ("sha256", "2" * 64),
        ("provides_targets", ("other-target",)),
        ("needed_by_boards", ("other-board",)),
    ],
)
def test_fresh_reviewed_catalog_rejects_pack_not_pinned_for_board(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    monkeypatch.setattr(server, "_validation_inventory", lambda: ValidationInventory())
    monkeypatch.setattr(server, "_target_names", lambda: ())
    catalog = catalog_board("nucleo_l476rg")
    assert catalog.pyocd_pack_filename is not None
    assert catalog.pyocd_pack_sha256 is not None
    expected = PackSpec(
        id="Keil.STM32L4xx_DFP",
        version="3.1.0",
        filename=catalog.pyocd_pack_filename,
        url="https://example.invalid/stm32.pack",
        sha256=catalog.pyocd_pack_sha256,
        provides_targets=("stm32l476rgtx",),
        needed_by_boards=("nucleo_l476rg",),
    )
    actual = replace(expected, **{field: replacement})
    monkeypatch.setattr(
        server, "load_manifest", lambda path=None: (expected,) if path is None else ()
    )
    monkeypatch.setattr(
        server,
        "verified_pack_for_target",
        lambda _target: VerifiedPack(path=Path("selected.pack"), spec=actual, payload=b"pack"),
    )

    result = server._setup_inventory(
        SetupUserInput(
            "stm32_board",
            "probe:missing",
            "STM32 Board",
            "STM32L476RGT6",
            115200,
        )
    )

    assert result.exact_detected_targets == ()
    assert result.blocking_error is not None
    assert result.blocking_error.code == "setup/reviewed-pack-unavailable"


def test_validation_target_support_uses_verified_pack_not_manifest_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def verified(target: str) -> object:
        calls.append(target)
        return object()

    monkeypatch.setattr(server, "_built_in_target_names", lambda: {"builtin-target"})
    monkeypatch.setattr(server, "verified_pack_for_target", verified)
    assert server._validation_target_supported("builtin-target") is True
    assert calls == []

    monkeypatch.setattr(server, "_built_in_target_names", lambda: set())
    assert server._validation_target_supported("stm32l476rgtx") is True
    assert calls == ["stm32l476rgtx"]

    monkeypatch.setattr(server, "verified_pack_for_target", lambda _target: None)
    assert server._validation_target_supported("not-a-builtin-target") is False

    def invalid_pack(_target: str) -> object:
        raise PackProvisionError("changed pack bytes")

    monkeypatch.setattr(server, "verified_pack_for_target", invalid_pack)
    assert server._validation_target_supported("not-a-builtin-target") is False


@pytest.mark.parametrize("failure", ["missing", "checksum-invalid", "ambiguous"])
def test_pack_backed_exact_override_never_bypasses_verified_provider(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(server, "_validation_inventory", lambda: ValidationInventory())
    # Simulate a stale process-global target previously registered from any pack.
    monkeypatch.setattr(server, "_target_names", lambda: ("stm32l476rgtx",))
    spec = PackSpec(
        id="Keil.STM32L4xx_DFP",
        version="3.1.0",
        filename="stm32.pack",
        url="https://example.invalid/stm32.pack",
        sha256="1" * 64,
        provides_targets=("stm32l476rgtx",),
        needed_by_boards=("nucleo_l476rg",),
    )
    monkeypatch.setattr(server, "load_manifest", lambda path=None: (spec,) if path is None else ())

    def unavailable(_target: str) -> object | None:
        if failure == "missing":
            return None
        raise PackProvisionError(failure)

    monkeypatch.setattr(server, "verified_pack_for_target", unavailable)
    server._setup_target_overrides["stm32_board"] = "stm32l476rgtx"
    try:
        result = server._setup_inventory(
            SetupUserInput(
                "stm32_board",
                "probe:missing",
                "STM32 Board",
                "STM32L476RGT6",
                115200,
            )
        )
    finally:
        server._setup_target_overrides.pop("stm32_board", None)

    assert result.exact_detected_targets == ()
    assert result.blocking_error is not None
    assert result.blocking_error.code == "setup/reviewed-pack-unavailable"


def test_broad_supported_prefix_is_not_part_target_evidence(monkeypatch) -> None:
    monkeypatch.setattr(server, "_validation_inventory", lambda: ValidationInventory())
    monkeypatch.setattr(server, "_target_names", lambda: ("nrf5",))
    monkeypatch.setattr(server, "load_manifest", lambda *_args, **_kwargs: ())

    result = server._setup_inventory(
        SetupUserInput(
            "nf_board",
            "probe:missing",
            "NF Board",
            "nRF52840-QIAA",
            115200,
        )
    )

    assert result.exact_detected_targets == ()


def test_validation_inventory_includes_server_owned_active_probe(monkeypatch) -> None:
    handle = SimpleNamespace(
        probe_uid="066ACTIVE",
        board=SimpleNamespace(display_name="Nucleo", probe_family="stlink"),
        session=SimpleNamespace(probe=SimpleNamespace(description="ST-Link active session")),
    )
    connection = SimpleNamespace(handle=handle)
    manager = SimpleNamespace(
        assigned_board_ids=lambda: ("nucleo_l476rg",),
        connection_for=lambda board_id: connection,
    )
    monkeypatch.setattr(server, "connection_manager", manager)
    monkeypatch.setattr(server, "list_connected_probes", lambda _run: [])
    monkeypatch.setattr(server, "probe_family_from_pyocd_probe", lambda _probe: "stlink")
    monkeypatch.setattr(server, "list_serial_ports", lambda: [])

    inventory = server._validation_inventory()

    assert inventory.probes == (
        ValidationProbe("066ACTIVE", "ST-Link active session", "stlink", "066ACTIVE"),
    )


def test_public_setup_continuation_validates_and_routes_target_research(monkeypatch) -> None:
    user_input = SetupUserInput("nf_board", "probe:683377322", "NF Board", "nRF52840-QIAA", 115200)
    monkeypatch.setattr(
        server._setup_workflow,
        "continuation_context",
        lambda _token: (user_input, "setup_research_required", None),
    )
    monkeypatch.setattr(server, "_target_names", lambda: ("nrf52840",))
    monkeypatch.setattr(server, "load_manifest", lambda *_args, **_kwargs: ())
    server._setup_target_overrides.pop("nf_board", None)

    result = server._setup_continue(
        "nf_board",
        "continue-1",
        {
            "pyocd_target": "nrf52840",
            "evidence": [{"source": "official pyOCD", "claim": "nRF52840 target"}],
            "reasoning_summary": "The official target exactly matches the supplied MCU.",
        },
    )

    assert result["status"] == "setup_continuation_accepted"
    assert server._setup_target_overrides.pop("nf_board") == "nrf52840"

    with pytest.raises(ValueError, match="does not belong"):
        server._setup_continue(
            "other_board",
            "continue-1",
            {
                "pyocd_target": "nrf52840",
                "evidence": [{"source": "official", "claim": "target"}],
                "reasoning_summary": "Exact match.",
            },
        )


def test_generic_target_research_accepts_installed_builtin_without_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = "builtin_only_board"
    user_input = SetupUserInput(
        board_id, "probe:683377322", "Unknown carrier", "nRF52840-QIAA", 115200
    )
    monkeypatch.setattr(
        server._setup_workflow,
        "continuation_context",
        lambda _token: (user_input, "setup_research_required", None),
    )
    monkeypatch.setattr(
        server,
        "_resolve_setup_support",
        lambda _input: (_ for _ in ()).throw(PackProvisionError("no pack")),
    )
    monkeypatch.setattr(
        server,
        "_live_test_builtin_setup_target",
        lambda *, probe_uid, candidate, requested_policy: (
            candidate.with_identity_proof(0x410FC241),
            requested_policy,
        ),
    )
    monkeypatch.setattr(
        server,
        "resolve_device_support_geometry",
        lambda _candidate: SimpleNamespace(peripheral_regions=(object(),)),
    )
    server._setup_builtin_candidates.pop(board_id, None)

    result = server._setup_continue(
        board_id,
        "builtin-continuation",
        {
            "pyocd_target": "nrf52840",
            "evidence": [{"source": "official pyOCD", "claim": "installed target"}],
            "reasoning_summary": "The exact part is supported by the installed built-in target.",
            "debug_protocol": "swd",
            "debug_connect_mode": "attach",
            "debug_clock_hz": 2_000_000,
        },
    )

    candidate = server._setup_builtin_candidates.pop(board_id)
    assert result["status"] == "setup_continuation_accepted"
    assert candidate.pyocd_target == "nrf52840"
    assert candidate.identity_proof is not None
    assert server._setup_attachment_overrides.pop(board_id) == (
        "swd",
        "attach",
        2_000_000,
    )
    server._setup_target_overrides.pop(board_id, None)


def test_builtin_with_incomplete_static_geometry_routes_to_pack_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = "builtin_needs_pack"
    user_input = SetupUserInput(
        board_id, "probe:probe-1", "Unknown carrier", "PART-1", None, requires_uart=False
    )
    monkeypatch.setattr(
        server._setup_workflow,
        "continuation_context",
        lambda _token: (user_input, "setup_research_required", None),
    )
    monkeypatch.setattr(
        server,
        "_resolve_setup_support",
        lambda _input: (_ for _ in ()).throw(PackProvisionError("no pack")),
    )
    monkeypatch.setattr(
        server,
        "resolve_builtin_target_support",
        lambda *_args: (_ for _ in ()).throw(
            BuiltInTargetGeometryError(
                "built-in target lacks physical flash required by the current map schema"
            )
        ),
    )

    result = server._setup_continue(
        board_id,
        "builtin-needs-pack",
        {
            "pyocd_target": "installed-but-incomplete",
            "evidence": [{"source": "official pyOCD", "claim": "installed target"}],
            "reasoning_summary": "The installed target matches the exact part.",
        },
    )

    assert result["status"] == "setup_research_required"
    assert "CMSIS-Pack" in cast(str, result["agent_prompt"])
    assert "pack_id" in cast(list[str], result["exact_response_fields"])


def test_invalid_target_with_attachment_does_not_poison_later_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = "invalid_attachment_board"
    user_input = SetupUserInput(
        board_id, "probe:probe-1", "Unknown board", "PART-1", None, requires_uart=False
    )
    monkeypatch.setattr(
        server._setup_workflow,
        "continuation_context",
        lambda _token: (user_input, "setup_research_required", None),
    )
    monkeypatch.setattr(
        server,
        "_resolve_setup_support",
        lambda _input: (_ for _ in ()).throw(PackProvisionError("no pack")),
    )
    server._setup_attachment_overrides.pop(board_id, None)

    result = server._setup_continue(
        board_id,
        "attachment-continuation",
        {
            "pyocd_target": "not-an-installed-target",
            "debug_protocol": "jtag",
            "debug_connect_mode": "pre-reset",
            "debug_clock_hz": 500_000,
            "evidence": [{"source": "probe manual", "claim": "required attach policy"}],
            "reasoning_summary": "The probe and target documentation require this policy.",
        },
    )

    assert result["status"] == "setup_research_required"
    assert board_id not in server._setup_attachment_overrides


def test_public_setup_continuation_accepts_only_a_returned_friendly_choice(monkeypatch) -> None:
    user_input = SetupUserInput("nf_board", "probe:x", "NF Board", "nRF52840", 115200)
    decision = PreflightDecision(
        "setup_needs_user_input",
        "setup/probe-selection-required",
        "Choose a probe.",
        choices=(FriendlyChoice("probe-a", "Probe A", "First probe"),),
    )
    monkeypatch.setattr(
        server._setup_workflow,
        "continuation_context",
        lambda _token: (user_input, "setup_needs_user_input", decision),
    )
    server._setup_selections_by_board.pop("nf_board", None)

    with pytest.raises(ValueError, match="friendly choices"):
        server._setup_continue("nf_board", "continue-1", {"choice_id": "probe-b"})

    accepted = server._setup_continue("nf_board", "continue-1", {"choice_id": "probe-a"})
    assert accepted["status"] == "setup_continuation_accepted"
    assert server._setup_selections_by_board.pop("nf_board").probe_id == "probe-a"


def test_friendly_choice_continuation_rejects_pack_reply_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_input = SetupUserInput("nf_board", "probe:x", "NF Board", "nRF52840", 115200)
    decision = PreflightDecision(
        "setup_needs_user_input",
        "setup/probe-selection-required",
        "Choose a probe.",
        choices=(FriendlyChoice("probe-a", "Probe A", "First probe"),),
    )
    monkeypatch.setattr(
        server._setup_workflow,
        "continuation_context",
        lambda _token: (user_input, "setup_needs_user_input", decision),
    )
    monkeypatch.setattr(
        server,
        "_setup_pack_pipeline",
        lambda *_args: pytest.fail("out-of-sequence research must not stage or attach"),
    )

    with pytest.raises(ValueError, match="friendly choice, not research"):
        server._setup_continue(
            "nf_board",
            "continue-1",
            {
                "pack_id": "Vendor.Device",
                "version": "1.0",
                "filename": "Vendor.Device.pack",
                "url": "https://vendor.example/Device.pack",
                "source_path": "C:/candidate/Device.pack",
                "official_sha256": None,
                "evidence": [{"source": "official", "claim": "device support"}],
                "reasoning_summary": "Official device support.",
            },
        )


def test_production_loaded_validation_guidance_uses_current_assignment() -> None:
    previous = server.assignment_store.bindings()
    board_id = "probe_guidance_integration_board"
    try:
        server.assignment_store.replace({"probe:PROBE-GUIDANCE": board_id})

        payload = json.loads(
            server.setup_tool_handlers["load_setup_tool"](board_id, "board_validate")
        )

        assert payload["next_call"] == {
            "tool": "board_validate",
            "arguments": {"board_id": board_id, "probe_id": "PROBE-GUIDANCE"},
        }
    finally:
        server.assignment_store.replace(previous)


def test_public_setup_continuation_accepts_real_external_adapter_confirmation_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_input = SetupUserInput("nf_board", "probe:x", "NF Board", "nRF52840", 115200)
    decision = PreflightDecision(
        "setup_needs_user_input",
        "setup/external-adapter-confirmation-required",
        "Confirm the adapter.",
        choices=(
            FriendlyChoice("confirm_external_adapter", "Confirm adapter", "Selected adapter"),
        ),
        selected_probe=ProbeCandidate("probe-a", "Adapter probe", "cmsis-dap"),
        selected_serial=SerialCandidate("uart-a", "COM9", "External adapter"),
    )
    monkeypatch.setattr(
        server._setup_workflow,
        "continuation_context",
        lambda _token: (user_input, "setup_needs_user_input", decision),
    )
    server._setup_selections_by_board.pop("nf_board", None)

    accepted = server._setup_continue(
        "nf_board", "continue-1", {"choice_id": "confirm_external_adapter"}
    )

    selection = server._setup_selections_by_board.pop("nf_board")
    assert accepted["status"] == "setup_continuation_accepted"
    assert selection.probe_id == "probe-a"
    assert selection.serial_id == "uart-a"
    assert selection.external_adapter_confirmed is True
