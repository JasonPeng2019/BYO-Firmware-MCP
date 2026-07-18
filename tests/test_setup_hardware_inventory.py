from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyocd_debug_mcp import server
from pyocd_debug_mcp.firmstore.cache import CacheResolution
from pyocd_debug_mcp.setup_flow.preflight import (
    FriendlyChoice,
    PreflightDecision,
    SetupUserInput,
)
from pyocd_debug_mcp.setup_flow.validate import (
    ValidationInventory,
    ValidationProbe,
    ValidationSerial,
)


def test_setup_inventory_scopes_probe_and_uart_by_stable_connection_identity(
    monkeypatch,
) -> None:
    inventory = ValidationInventory(
        (
            ValidationProbe("066ABC", "ST-Link", "stlink", "066ABC"),
            ValidationProbe("683377322", "J-Link", "jlink", "683377322"),
        ),
        (
            ValidationSerial("COM12", "COM12", "ST UART", "066ABC", 1, 2),
            ValidationSerial("COM11", "COM11", "J-Link UART", "000683377322", 3, 4),
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
    assert [serial.serial_id for serial in result.serial_ports] == ["COM11"]
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
            board_type="nrf52840dk",
            serial_id="683377322",
            serial_port="COM11",
        )
    )

    assert result.serial_ports == ()


def test_setup_inventory_rejects_wrong_single_probe_and_port_path_as_uart_id(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            (ValidationProbe("OTHER-PROBE", "Other J-Link", "jlink", "OTHER-PROBE"),),
            (ValidationSerial("COM11", "COM11", "J-Link UART", "683377322", 1, 2),),
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
            board_type="nrf52840dk",
            serial_id="COM11",
            serial_port="COM11",
        )
    )

    assert result.probes == ()
    assert result.serial_ports == ()


def test_official_target_mapping_requires_part_consistency(monkeypatch) -> None:
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
            board_type="nrf52840dk",
        )
    )
    family_only = server._setup_inventory(
        SetupUserInput(
            "nf_board",
            "probe:missing",
            "NF Board",
            "nRF52840",
            115200,
            board_type="nrf52840dk",
        )
    )
    wrong_suffix = server._setup_inventory(
        SetupUserInput(
            "nf_board",
            "probe:missing",
            "NF Board",
            "nRF52840-EVIL",
            115200,
            board_type="nrf52840dk",
        )
    )

    assert exact.exact_detected_targets == ("nrf52840",)
    assert family_only.exact_detected_targets == ("nrf52840",)
    assert wrong_suffix.exact_detected_targets == ()


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
    monkeypatch.setattr(server, "list_serial_ports", lambda: [])

    inventory = server._validation_inventory()

    assert inventory.probes == (
        ValidationProbe("066ACTIVE", "ST-Link active session", "stlink", "066ACTIVE"),
    )


def test_public_setup_continuation_validates_and_routes_target_research(monkeypatch) -> None:
    user_input = SetupUserInput(
        "nf_board", "probe:683377322", "NF Board", "nRF52840", 115200, board_type="nrf52840dk"
    )
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


def test_public_setup_continuation_accepts_only_a_returned_friendly_choice(monkeypatch) -> None:
    user_input = SetupUserInput("nf_board", "probe:x", "NF Board", "nRF52840", 115200)
    decision = PreflightDecision(
        "setup_needs_user_input",
        "setup/ambiguous-probe",
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
