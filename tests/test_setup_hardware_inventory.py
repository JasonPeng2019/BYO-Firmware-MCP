from __future__ import annotations

from types import SimpleNamespace

from pyocd_debug_mcp import server
from pyocd_debug_mcp.firmstore.cache import CacheResolution
from pyocd_debug_mcp.setup_flow.preflight import SetupUserInput
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
    monkeypatch.setattr(server, "load_manifest", lambda: ())
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


def test_official_target_mapping_requires_part_consistency(monkeypatch) -> None:
    monkeypatch.setattr(server, "_validation_inventory", lambda: ValidationInventory())
    monkeypatch.setattr(server, "_target_names", lambda: ("stm32l476rgtx",))
    monkeypatch.setattr(server, "load_manifest", lambda: ())
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
