from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from scripts.run_fresh_workspace_e2e import (
    RunnerConfig,
    SetupBarrierError,
    _parser,
    execute_setup_only,
)


def _result(value: str | dict[str, object], *, error: bool = False) -> object:
    text = value if isinstance(value, str) else json.dumps(value)
    return SimpleNamespace(
        isError=error,
        content=[SimpleNamespace(text=text)],
    )


def _config(tmp_path: Path) -> RunnerConfig:
    datasheet = tmp_path / "datasheet.pdf"
    datasheet.write_bytes(b"%PDF-1.7\nreviewed fixture\n")
    return RunnerConfig(
        artifact_root=tmp_path / "artifacts",
        board_id="nf_board",
        display_name="Nordic Bench",
        board_type="nrf52840dk",
        mcu_part_number="nRF52840-QIAA",
        probe_uid="683377322",
        uart_id="683377322",
        uart_port="COM11",
        baudrate=115200,
        datasheet_path=datasheet,
        setup_authorized=True,
    )


class FakeClient:
    def __init__(
        self,
        *,
        setup_status: str = "setup_completed",
        validation_status: str = "validation_passed_uart_not_configured",
        ready: bool = True,
    ) -> None:
        self.setup_status = setup_status
        self.validation_status = validation_status
        self.ready = ready
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, arguments))
        if name == "initialization_handshake":
            return _result("run_id: run-test\nstarted_at: 2026-07-17T00:00:00+00:00")
        if name == "load_setup_tool":
            return _result({"status": "setup_tool_loaded"})
        if name == "board_setup-plan":
            if not arguments:
                return _result(
                    "Ask for the board, exact MCU, authoritative datasheet, then board_validate."
                )
            return _result("Accepted setup plan exactly.")
        if name == "board_setup":
            return _result(
                {
                    "status": self.setup_status,
                    "continuation_id": "continuation-1",
                    "choices": [],
                }
            )
        if name == "board_validate":
            return _result({"status": self.validation_status})
        if name == "get_setup_status":
            return _result(
                {
                    "status": "setup_ready" if self.ready else "setup_not_ready",
                    "board_id": "nf_board",
                    "configuration_ready": self.ready,
                    "live_session_ready": self.ready,
                    "ready_for_code": self.ready,
                    "resolved_uart": {
                        "serial_id": "683377322",
                        "usb_serial": "683377322",
                        "port_path": "COM11",
                    },
                    "resolved_probe": {
                        "probe_uid": "683377322",
                        "connection_id": "probe:683377322",
                        "probe_family": "jlink",
                    },
                }
            )
        raise AssertionError(f"unexpected tool call: {name}")


def test_setup_only_runner_reaches_exact_readiness_without_a_code_surface(
    tmp_path: Path,
) -> None:
    client = FakeClient()

    evidence = asyncio.run(execute_setup_only(client, _config(tmp_path)))

    assert evidence["status"] == "pass"
    assert evidence["code_phase_started"] is False
    assert evidence["identity"]["probe_uid"] == "683377322"
    assert evidence["identity"]["uart_port"] == "COM11"
    assert evidence["setup_authorization_present"] is True
    assert "user_permission" not in json.dumps(evidence)
    names = [name for name, _arguments in client.calls]
    assert names == [
        "initialization_handshake",
        "load_setup_tool",
        "board_setup-plan",
        "board_setup-plan",
        "board_setup",
        "load_setup_tool",
        "board_validate",
        "get_setup_status",
    ]
    assert not set(names).intersection(
        {"flash_application", "flash_bootloader", "write_serial", "serial_exchange"}
    )


@pytest.mark.parametrize(
    "terminal_status",
    [
        "setup_needs_user_input",
        "setup_blocked",
        "setup_unresolved",
        "setup_research_required",
        "setup_connection_failed",
        "setup_validation_failed",
        "setup_safety_incomplete",
    ],
)
def test_every_non_success_setup_status_stops_before_validation_or_code(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    client = FakeClient(setup_status=terminal_status)

    with pytest.raises(SetupBarrierError) as captured:
        asyncio.run(execute_setup_only(client, _config(tmp_path)))

    assert captured.value.evidence["status"] == "failed"
    assert captured.value.evidence["code_phase_started"] is False
    names = [name for name, _arguments in client.calls]
    assert names[-1] == "board_setup"
    assert "board_validate" not in names
    assert "get_setup_status" not in names


def test_validation_refusal_stops_before_readiness_and_code(tmp_path: Path) -> None:
    client = FakeClient(validation_status="validation_failed")

    with pytest.raises(SetupBarrierError) as captured:
        asyncio.run(execute_setup_only(client, _config(tmp_path)))

    assert captured.value.evidence["code_phase_started"] is False
    assert [name for name, _arguments in client.calls][-1] == "board_validate"


def test_false_readiness_is_a_terminal_failure_not_a_code_handoff(tmp_path: Path) -> None:
    client = FakeClient(ready=False)

    with pytest.raises(SetupBarrierError) as captured:
        asyncio.run(execute_setup_only(client, _config(tmp_path)))

    assert captured.value.evidence["code_phase_started"] is False
    assert "readiness barrier" in str(captured.value)


def test_different_single_uart_cannot_satisfy_explicit_runner_identity(tmp_path: Path) -> None:
    client = FakeClient()
    original = client.call_tool

    async def mismatched_uart(name: str, arguments: dict[str, object]) -> object:
        result = await original(name, arguments)
        if name == "get_setup_status":
            content = getattr(result, "content")
            payload = json.loads(getattr(content[0], "text"))
            payload["resolved_uart"] = {
                "serial_id": "OTHER-UART",
                "usb_serial": "OTHER-UART",
                "port_path": "COM99",
            }
            return _result(payload)
        return result

    client.call_tool = mismatched_uart  # type: ignore[method-assign]

    with pytest.raises(SetupBarrierError) as captured:
        asyncio.run(execute_setup_only(client, _config(tmp_path)))

    assert captured.value.evidence["code_phase_started"] is False
    assert "does not match" in str(captured.value)


def test_different_resolved_probe_cannot_satisfy_explicit_runner_identity(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    original = client.call_tool

    async def mismatched_probe(name: str, arguments: dict[str, object]) -> object:
        result = await original(name, arguments)
        if name == "get_setup_status":
            content = getattr(result, "content")
            payload = json.loads(getattr(content[0], "text"))
            payload["resolved_probe"]["probe_uid"] = "DIFFERENT-PROBE"
            return _result(payload)
        return result

    client.call_tool = mismatched_probe  # type: ignore[method-assign]

    with pytest.raises(SetupBarrierError) as captured:
        asyncio.run(execute_setup_only(client, _config(tmp_path)))

    assert captured.value.evidence["code_phase_started"] is False
    assert "probe identity" in str(captured.value)


def test_runner_cli_has_explicit_identity_and_no_arbitrary_execution_option() -> None:
    parser = _parser()
    destinations = {action.dest for action in parser._actions}

    assert {
        "artifact_root",
        "board_id",
        "display_name",
        "board_type",
        "mcu_part_number",
        "probe_uid",
        "uart_id",
        "uart_port",
        "datasheet_path",
        "authorize_setup",
    }.issubset(destinations)
    assert destinations.isdisjoint(
        {"command", "callback", "shell", "argv", "code", "build", "flash"}
    )
