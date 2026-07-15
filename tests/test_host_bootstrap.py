from __future__ import annotations

from pathlib import Path

import pytest

import host_bootstrap
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.probe_inventory import ProbeInfo, ProbeResolution
from pyocd_debug_mcp.serial_resolver import SerialPortInfo, SerialResolution


def _board(board_id: str, *, probe_family: str = "jlink") -> BoardConfig:
    return BoardConfig(
        board_id=board_id,
        display_name=board_id,
        mcu_family="nrf52833" if probe_family == "jlink" else "stm32l476",
        probe_family=probe_family,
        pyocd_target="nrf52833" if probe_family == "jlink" else "stm32l476rgtx",
        pack_name="nrf52833" if probe_family == "jlink" else "stm32l476rgtx",
        probe_type="SEGGER J-Link" if probe_family == "jlink" else "ST-Link",
        probe_hint_terms=("segger", "j-link") if probe_family == "jlink" else ("st-link",),
        serial_hint_terms=("virtual com",),
        test_addr=0x10000000 if probe_family == "jlink" else 0x08000000,
        source_path=Path("/tmp") / f"{board_id}.yaml",
    )


def _port(device: str) -> SerialPortInfo:
    return SerialPortInfo(
        device=device,
        description="Virtual COM Port",
        manufacturer="Vendor",
        product="Product",
        interface="UART",
        hwid="USB VID:PID=0000:0000",
    )


def test_assess_board_attachment_statuses_passes_with_unique_probe_and_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board("nrf52833dk")
    probe = ProbeInfo(uid="685400693", description="SEGGER J-Link", raw="SEGGER J-Link")

    monkeypatch.setattr(
        host_bootstrap,
        "resolve_probe_for_board",
        lambda selected_board, *, run_cmd, allow_single_fallback: ProbeResolution(
            probe=probe,
            note="",
            probes=(probe,),
        ),
    )
    monkeypatch.setattr(
        host_bootstrap, "list_serial_ports", lambda: [_port("/dev/cu.usbmodem0006854006931")]
    )
    monkeypatch.setattr(
        host_bootstrap,
        "resolve_serial_port",
        lambda selected_board, ports, probe, override, allow_single_fallback, run_cmd, interactive: (
            SerialResolution(
                ports[0],
                "resolved via nrfjprog --com (VCOM0)",
            )
        ),
    )

    statuses = host_bootstrap.assess_board_attachment_statuses(
        [board],
        pyserial_ok=True,
        run_cmd=lambda cmd: (0, "", ""),
    )

    assert len(statuses) == 1
    assert statuses[0].ready is True
    assert statuses[0].probe_status == host_bootstrap.PASS
    assert statuses[0].serial_status == host_bootstrap.PASS
    assert "matched probe 685400693" in statuses[0].probe_message
    assert "/dev/cu.usbmodem0006854006931" in statuses[0].serial_message


def test_assess_board_attachment_statuses_fails_when_probe_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board("nrf52833dk")
    monkeypatch.setattr(
        host_bootstrap,
        "resolve_probe_for_board",
        lambda selected_board, *, run_cmd, allow_single_fallback: ProbeResolution(
            probe=None,
            note="no matching probe found",
            probes=(),
        ),
    )
    monkeypatch.setattr(
        host_bootstrap, "list_serial_ports", lambda: [_port("/dev/cu.usbmodem0006854006931")]
    )

    statuses = host_bootstrap.assess_board_attachment_statuses(
        [board],
        pyserial_ok=True,
        run_cmd=lambda cmd: (0, "", ""),
    )

    assert statuses[0].ready is False
    assert statuses[0].probe_status == host_bootstrap.FAIL
    assert "no matching probe found" in statuses[0].probe_message
    assert statuses[0].serial_status == host_bootstrap.INFO


def test_assess_board_attachment_statuses_fails_when_probe_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board("nrf52833dk")
    probe = ProbeInfo(uid="685400693", description="SEGGER J-Link", raw="SEGGER J-Link")
    monkeypatch.setattr(
        host_bootstrap,
        "resolve_probe_for_board",
        lambda selected_board, *, run_cmd, allow_single_fallback: ProbeResolution(
            probe=None,
            note="multiple matching probes found; disconnect extras or refine probe_hint_terms",
            probes=(probe, probe),
        ),
    )
    monkeypatch.setattr(
        host_bootstrap, "list_serial_ports", lambda: [_port("/dev/cu.usbmodem0006854006931")]
    )

    statuses = host_bootstrap.assess_board_attachment_statuses(
        [board],
        pyserial_ok=True,
        run_cmd=lambda cmd: (0, "", ""),
    )

    assert statuses[0].ready is False
    assert statuses[0].probe_status == host_bootstrap.FAIL
    assert "multiple matching probes found" in statuses[0].probe_message


def test_assess_board_attachment_statuses_fails_when_no_serial_ports_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board("nrf52833dk")
    probe = ProbeInfo(uid="685400693", description="SEGGER J-Link", raw="SEGGER J-Link")
    monkeypatch.setattr(
        host_bootstrap,
        "resolve_probe_for_board",
        lambda selected_board, *, run_cmd, allow_single_fallback: ProbeResolution(
            probe=probe,
            note="",
            probes=(probe,),
        ),
    )
    monkeypatch.setattr(host_bootstrap, "list_serial_ports", lambda: [])

    statuses = host_bootstrap.assess_board_attachment_statuses(
        [board],
        pyserial_ok=True,
        run_cmd=lambda cmd: (0, "", ""),
    )

    assert statuses[0].ready is False
    assert statuses[0].serial_status == host_bootstrap.FAIL
    assert "no serial ports detected" in statuses[0].serial_message


def test_board_attachment_summary_warns_on_ambiguous_serial_match(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    board = _board("nrf52833dk")
    probe = ProbeInfo(uid="685400693", description="SEGGER J-Link", raw="SEGGER J-Link")
    monkeypatch.setattr(
        host_bootstrap,
        "resolve_probe_for_board",
        lambda selected_board, *, run_cmd, allow_single_fallback: ProbeResolution(
            probe=probe,
            note="",
            probes=(probe,),
        ),
    )
    monkeypatch.setattr(
        host_bootstrap,
        "list_serial_ports",
        lambda: [_port("/dev/cu.usbmodemA"), _port("/dev/cu.usbmodemB")],
    )
    monkeypatch.setattr(
        host_bootstrap,
        "resolve_serial_port",
        lambda selected_board, ports, probe, override, allow_single_fallback, run_cmd, interactive: (
            SerialResolution(
                None,
                "multiple matching serial ports found; use --port nrf52833dk=PORT",
            )
        ),
    )

    ready = host_bootstrap.board_attachment_summary(
        [board],
        pyserial_ok=True,
        run_cmd=lambda cmd: (0, "", ""),
    )

    output = capsys.readouterr().out
    assert ready is False
    assert (
        "[WARN] nrf52833dk: multiple matching serial ports found; use --port nrf52833dk=PORT"
        in output
    )


def test_main_without_board_id_keeps_host_only_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board("nrf52833dk")
    monkeypatch.setattr(
        host_bootstrap,
        "dependency_summary",
        lambda require_yaml, install_missing: {"pyocd": True, "pyserial": True},
    )
    monkeypatch.setattr(host_bootstrap, "pyocd_summary", lambda pyocd_ok: 1)
    monkeypatch.setattr(host_bootstrap, "serial_summary", lambda pyserial_ok: 1)
    monkeypatch.setattr(
        host_bootstrap, "load_selected_board_configs", lambda *args, **kwargs: [board]
    )
    monkeypatch.setattr(host_bootstrap, "board_config_summary", lambda boards: None)
    monkeypatch.setattr(
        host_bootstrap,
        "target_pack_summary",
        lambda boards, pyocd_ok, install_packs: {board.board_id: True for board in boards},
    )
    monkeypatch.setattr(host_bootstrap, "vendor_serial_tool_summary", lambda boards: None)

    def _unexpected_board_check(*args, **kwargs):
        raise AssertionError("board_attachment_summary should not run without --board-id")

    monkeypatch.setattr(host_bootstrap, "board_attachment_summary", _unexpected_board_check)

    assert host_bootstrap.main([]) == 0
