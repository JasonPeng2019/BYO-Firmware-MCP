from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.guardrails import flash_gate, recover_gate
from pyocd_debug_mcp.services.convergence_watcher import (
    ConvergenceWatcher,
    FLASH_TOOL,
    RECOVER_TOOL,
    UART_TOOL,
)
from pyocd_debug_mcp.services import session_runtime
from pyocd_debug_mcp.services.session_runtime import (
    ActionContext,
    InMemorySessionStore,
    SessionRecord,
    ToolEvent,
    ToolOutcome,
)


def make_nordic_board() -> BoardConfig:
    return BoardConfig(
        board_id="nrf52833dk",
        display_name="nRF52833 DK",
        mcu_family="nrf52833",
        probe_family="jlink",
        pyocd_target="nrf52833",
        pack_name="nrf52833",
        probe_type="SEGGER J-Link",
        probe_hint_terms=("jlink", "segger"),
        serial_hint_terms=("jlink", "segger", "virtual com"),
        test_addr=0x10000000,
        silicon_id_addr=0x10000100,
        silicon_id_expected=0x00052833,
        silicon_id_label="FICR.INFO.PART",
        default_baudrate=115200,
        requires_recover_validation=True,
        recover_mode="nrf_pyocd_unlock",
        expected_uart_substring="boot ok",
    )


def make_stm32_board() -> BoardConfig:
    return BoardConfig(
        board_id="nucleo_l476rg",
        display_name="Nucleo-L476RG",
        mcu_family="stm32l476",
        probe_family="stlink",
        pyocd_target="stm32l476rgtx",
        pack_name="stm32l476",
        probe_type="ST-Link",
        probe_hint_terms=("st-link", "stlink"),
        serial_hint_terms=("st-link", "stlink", "virtual com"),
        test_addr=0x08000000,
        silicon_id_addr=None,
        silicon_id_expected=None,
        silicon_id_label="",
        default_baudrate=115200,
        requires_recover_validation=False,
        recover_mode=None,
        expected_uart_substring="boot ok",
    )


def make_handle(board: BoardConfig | None) -> TargetSessionHandle:
    session_board = type(
        "SessionBoard", (), {"name": board.display_name if board else "Raw Target"}
    )()
    session = type("Session", (), {"board": session_board if board else None})()
    return TargetSessionHandle(
        session=session,
        board=board,
        probe_uid="probe-123",
        route_used="pyocd-native",
        target_override=board.pyocd_target if board else "raw-target",
    )


def make_event(
    session: SessionRecord,
    *,
    tool_name: str,
    outcome_kind: ToolOutcome,
    error_code: str | None,
    normalized_args: dict[str, object],
) -> ToolEvent:
    return ToolEvent(
        event_id=f"evt-{len(session.events) + 1}",
        session_id=session.session_id,
        timestamp="2026-06-18T00:00:00Z",
        tool_name=tool_name,
        board_id=session.board_id,
        probe_uid=session.probe_uid,
        route_used=session.route_used,
        normalized_args=normalized_args,
        outcome_kind=outcome_kind,
        error_code=error_code,
        duration_ms=12,
        details={},
    )


def test_runtime_runs_root_points_at_repo_runs_dir() -> None:
    expected_runs_root = Path(__file__).resolve().parents[1] / "runs"
    assert session_runtime.RUNS_ROOT == expected_runs_root


def test_session_store_writes_jsonl_and_summary(tmp_path: Path) -> None:
    store = InMemorySessionStore(tmp_path / "runs")
    session = store.start_session(
        board_id="nrf52833dk",
        probe_uid="probe-123",
        route_used="pyocd-native",
    )

    event = make_event(
        session,
        tool_name="connect",
        outcome_kind=ToolOutcome.SUCCESS,
        error_code=None,
        normalized_args={"board_id": "nrf52833dk"},
    )
    store.append_event(session, event)
    store.set_block(session, UART_TOOL, "watch/uart-miss-repetition", "Repeated UART misses.")
    store.mark_recover_completed(session)
    store.close_session(session)

    lines = session.log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == session.session_id
    assert record["tool_name"] == "connect"
    assert record["outcome_kind"] == "success"

    summary = json.loads(session.summary_path.read_text(encoding="utf-8"))
    assert summary["session_id"] == session.session_id
    assert summary["event_count"] == 1
    assert summary["recover_completed"] is True
    assert summary["blocked_actions"][UART_TOOL]["code"] == "watch/uart-miss-repetition"
    assert summary["closed_at"] is not None


def test_new_session_starts_without_prior_block_state(tmp_path: Path) -> None:
    store = InMemorySessionStore(tmp_path / "runs")
    first = store.start_session(
        board_id="nrf52833dk", probe_uid="probe-123", route_used="pyocd-native"
    )
    store.set_block(first, FLASH_TOOL, "watch/flash-repetition", "Repeated flash failures.")
    store.close_session(first)

    second = store.start_session(
        board_id="nrf52833dk", probe_uid="probe-123", route_used="pyocd-native"
    )

    assert second.blocked_actions == {}


def test_flash_gate_resolves_default_artifact(monkeypatch, tmp_path: Path) -> None:
    board = make_nordic_board()
    handle = make_handle(board)
    artifact = tmp_path / "firmware.hex"
    artifact.write_text(":0100000000FF\n", encoding="utf-8")

    monkeypatch.setattr(
        flash_gate,
        "resolve_reference_artifacts",
        lambda board_arg: type("Pair", (), {"flash_artifact": artifact})(),
    )

    request = flash_gate.resolve_flash_request(
        handle,
        explicit_path=None,
        action_context=ActionContext(source="test", action_name="flash"),
    )

    assert request.artifact_path == artifact
    assert request.identity.source == "default"
    assert request.identity.suffix == ".hex"


@pytest.mark.parametrize("suffix", [".elf", ".hex"])
def test_flash_gate_allows_existing_local_elf_and_hex(tmp_path: Path, suffix: str) -> None:
    board = make_nordic_board()
    handle = make_handle(board)
    artifact = tmp_path / f"firmware{suffix}"
    artifact.write_text("artifact", encoding="utf-8")

    request = flash_gate.resolve_flash_request(
        handle,
        explicit_path=artifact,
        action_context=ActionContext(source="test", action_name="flash"),
    )

    assert request.artifact_path == artifact.resolve()
    assert request.identity.source == "explicit"


def test_flash_gate_refuses_missing_file(tmp_path: Path) -> None:
    board = make_nordic_board()
    handle = make_handle(board)
    missing = tmp_path / "missing.elf"

    with pytest.raises(RuntimeError, match="Flash artifact does not exist"):
        flash_gate.resolve_flash_request(
            handle,
            explicit_path=missing,
            action_context=ActionContext(source="test", action_name="flash"),
        )


@pytest.mark.parametrize(
    "url",
    ["https://example.invalid/firmware.hex", "file:///tmp/firmware.hex"],
)
def test_flash_gate_refuses_url_inputs(url: str) -> None:
    handle = make_handle(make_nordic_board())

    with pytest.raises(session_runtime.PolicyRefusal) as exc_info:
        flash_gate.resolve_flash_request(
            handle,
            explicit_path=url,
            action_context=ActionContext(source="test", action_name="flash"),
        )

    assert exc_info.value.code == "flash/non-local-path"
    assert exc_info.value.message == "Flash path must be a local filesystem path."


def test_flash_gate_refuses_directory(tmp_path: Path) -> None:
    handle = make_handle(make_nordic_board())

    with pytest.raises(session_runtime.PolicyRefusal) as exc_info:
        flash_gate.resolve_flash_request(
            handle,
            explicit_path=tmp_path,
            action_context=ActionContext(source="test", action_name="flash"),
        )

    assert exc_info.value.code == "flash/not-a-file"
    assert exc_info.value.message == f"Flash artifact must be a file, not a directory: {tmp_path}"


def test_flash_gate_refuses_bin_suffix(tmp_path: Path) -> None:
    board = make_nordic_board()
    handle = make_handle(board)
    artifact = tmp_path / "firmware.bin"
    artifact.write_text("bin", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Unsupported flash artifact type"):
        flash_gate.resolve_flash_request(
            handle,
            explicit_path=artifact,
            action_context=ActionContext(source="test", action_name="flash"),
        )


def test_flash_gate_refuses_default_resolution_without_board_config() -> None:
    handle = make_handle(None)

    with pytest.raises(RuntimeError, match="loaded board config"):
        flash_gate.resolve_flash_request(
            handle,
            explicit_path=None,
            action_context=ActionContext(source="test", action_name="flash"),
        )


def test_recover_gate_refuses_without_confirmation() -> None:
    handle = make_handle(make_nordic_board())

    with pytest.raises(RuntimeError, match="confirm=True"):
        recover_gate.authorize_recover(
            handle,
            confirm=False,
            recover_already_completed=False,
            action_context=ActionContext(source="test", action_name="recover"),
        )


def test_recover_gate_allows_supported_nordic_recover() -> None:
    request = recover_gate.authorize_recover(
        make_handle(make_nordic_board()),
        confirm=True,
        recover_already_completed=False,
        action_context=ActionContext(source="test", action_name="recover"),
    )

    assert request.board_id == "nrf52833dk"
    assert request.recover_mode == "nrf_pyocd_unlock"


def test_recover_gate_refuses_unsupported_stm32_recover() -> None:
    with pytest.raises(RuntimeError, match="does not define a recover mode"):
        recover_gate.authorize_recover(
            make_handle(make_stm32_board()),
            confirm=True,
            recover_already_completed=False,
            action_context=ActionContext(source="test", action_name="recover"),
        )


def test_recover_gate_refuses_second_recover_in_same_session() -> None:
    with pytest.raises(RuntimeError, match="Disconnect and reconnect"):
        recover_gate.authorize_recover(
            make_handle(make_nordic_board()),
            confirm=True,
            recover_already_completed=True,
            action_context=ActionContext(source="test", action_name="recover"),
        )


def test_convergence_watcher_blocks_after_two_identical_flash_failures(tmp_path: Path) -> None:
    store = InMemorySessionStore(tmp_path / "runs")
    session = store.start_session(
        board_id="nrf52833dk", probe_uid="probe-123", route_used="pyocd-native"
    )
    watcher = ConvergenceWatcher()

    event1 = make_event(
        session,
        tool_name=FLASH_TOOL,
        outcome_kind=ToolOutcome.FAILED,
        error_code="flash/missing-file",
        normalized_args={"artifact_sha256": "sha", "artifact_path": "/tmp/fw.elf"},
    )
    store.append_event(session, event1)
    assert watcher.observe_event(session, event1) is None

    event2 = make_event(
        session,
        tool_name=FLASH_TOOL,
        outcome_kind=ToolOutcome.FAILED,
        error_code="flash/missing-file",
        normalized_args={"artifact_sha256": "sha", "artifact_path": "/tmp/fw.elf"},
    )
    store.append_event(session, event2)
    decision = watcher.observe_event(session, event2)

    assert decision is not None
    assert decision.action_family == FLASH_TOOL
    assert decision.code == "watch/flash-repetition"


def test_convergence_watcher_blocks_after_three_identical_uart_misses(tmp_path: Path) -> None:
    store = InMemorySessionStore(tmp_path / "runs")
    session = store.start_session(
        board_id="nrf52833dk", probe_uid="probe-123", route_used="pyocd-native"
    )
    watcher = ConvergenceWatcher()

    for _ in range(2):
        event = make_event(
            session,
            tool_name=UART_TOOL,
            outcome_kind=ToolOutcome.FAILED,
            error_code="uart/no-match",
            normalized_args={
                "port": "/dev/cu.usbmodem0001",
                "baudrate": 115200,
                "expected_text": "boot ok",
            },
        )
        store.append_event(session, event)
        assert watcher.observe_event(session, event) is None

    event3 = make_event(
        session,
        tool_name=UART_TOOL,
        outcome_kind=ToolOutcome.FAILED,
        error_code="uart/no-match",
        normalized_args={
            "port": "/dev/cu.usbmodem0001",
            "baudrate": 115200,
            "expected_text": "boot ok",
        },
    )
    store.append_event(session, event3)
    decision = watcher.observe_event(session, event3)

    assert decision is not None
    assert decision.action_family == UART_TOOL
    assert decision.code == "watch/uart-miss-repetition"


def test_convergence_watcher_blocks_after_two_identical_recover_failures(tmp_path: Path) -> None:
    store = InMemorySessionStore(tmp_path / "runs")
    session = store.start_session(
        board_id="nrf52833dk", probe_uid="probe-123", route_used="pyocd-native"
    )
    watcher = ConvergenceWatcher()

    event1 = make_event(
        session,
        tool_name=RECOVER_TOOL,
        outcome_kind=ToolOutcome.FAILED,
        error_code="target/locked",
        normalized_args={},
    )
    store.append_event(session, event1)
    assert watcher.observe_event(session, event1) is None

    event2 = make_event(
        session,
        tool_name=RECOVER_TOOL,
        outcome_kind=ToolOutcome.FAILED,
        error_code="target/locked",
        normalized_args={},
    )
    store.append_event(session, event2)
    decision = watcher.observe_event(session, event2)

    assert decision is not None
    assert decision.action_family == RECOVER_TOOL
    assert decision.code == "watch/recover-repetition"


def test_convergence_watcher_never_blocks_read_only_tools(tmp_path: Path) -> None:
    store = InMemorySessionStore(tmp_path / "runs")
    session = store.start_session(
        board_id="nrf52833dk", probe_uid="probe-123", route_used="pyocd-native"
    )
    watcher = ConvergenceWatcher()

    event = make_event(
        session,
        tool_name="read_memory",
        outcome_kind=ToolOutcome.FAILED,
        error_code="target/connection-failure",
        normalized_args={"address": "0x10000000"},
    )
    store.append_event(session, event)

    assert watcher.observe_event(session, event) is None
