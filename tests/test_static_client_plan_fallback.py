from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from typing import Any

import pytest
from mcp.types import TextContent

from pyocd_debug_mcp.guardrails.permissions import PermissionStore
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanRefusal
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.tools.batch import build_batch_handlers


BOARD_ID = "board_a"


def _common(action_parameters: dict[str, object], *, permission: str | None = None) -> dict[str, object]:
    fields: dict[str, object] = {
        "board_id": BOARD_ID,
        "hypothesis": "The exact bounded observation tests the current firmware state.",
        "strategy": "Execute once and compare the returned observation to the expected text.",
        "hypothesis_made": True,
        "strategy_evaluated": True,
        "expected_fail_return": "A typed refusal or a result without the expected observation.",
        "expected_success_return": "The exact expected observation is returned.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": action_parameters,
    }
    if permission is not None:
        fields["user_permission"] = permission
    return fields


def _read_parameters() -> dict[str, object]:
    return {
        "expected_text": "READY",
        "read_seconds": 1.0,
        "baudrate": 115200,
        "port": "COM11",
        "reset_on_open": False,
    }


def _text(result: Any) -> str:
    assert isinstance(result, list) and len(result) == 1
    item = result[0]
    assert isinstance(item, TextContent)
    return item.text


def _install_batch(mcp: RegistryFastMCP) -> None:
    handler = build_batch_handlers(
        mcp.call_tool,
        tool_exists=mcp.registry.is_registered,
    )["action_batch"]
    mcp.add_tool(handler, name="action_batch", description=handler.__doc__, structured_output=False)
    mcp.configure_layer2("action_batch")


@pytest.mark.asyncio
async def test_static_client_can_execute_exact_accepted_fallback_without_refresh() -> None:
    mcp = RegistryFastMCP("static-client")
    calls: list[tuple[str, str | None]] = []

    def read_serial(
        board_id: str,
        expected_text: str | None,
        read_seconds: float,
        baudrate: int,
        port: str | None,
        reset_on_open: bool,
    ) -> str:
        calls.append((board_id, expected_text))
        return "READY"

    mcp.add_tool(read_serial, name="read_serial", description="test", structured_output=False)
    mcp.registry.configure(
        "read_serial", hidden=True, locked=True, prerequisite="read_serial-plan"
    )
    _install_batch(mcp)
    startup_snapshot = mcp.registry.advertised()
    assert "action_batch" in startup_snapshot
    assert "read_serial" not in startup_snapshot

    run = ServerRun(run_id="static-client-run")
    engine = PlanEngine(run, mcp.registry)
    engine.null_response("read_serial-plan")
    accepted = engine.submit("read_serial-plan", _common(_read_parameters()))
    payload = json.loads(accepted.message)

    assert payload["preferred_call"] == {
        "tool_name": "read_serial",
        "arguments": {"board_id": BOARD_ID, **_read_parameters()},
    }
    fallback = payload["stable_client_fallback"]
    assert fallback["tool_name"] == "action_batch"
    assert len(fallback["arguments"]["actions"]) == 1

    def guard(name: str, board_id: str, arguments: Mapping[str, object]) -> None:
        parameters = {key: value for key, value in arguments.items() if key != "board_id"}
        engine.enforce(name, board_id, parameters, session_id=None)

    board_lock = threading.Lock()
    mcp.configure_guarded_dispatch(
        "read_serial", guard=guard, lock_for_board=lambda _board_id: board_lock
    )
    result = await mcp.call_tool("action_batch", fallback["arguments"])

    batch_payload = json.loads(_text(result).splitlines()[0])
    assert batch_payload["status"] == "batch_completed", batch_payload
    assert calls == [(BOARD_ID, "READY")]
    assert engine.active_plan("read_serial", BOARD_ID) is None
    assert not mcp.registry.is_unlocked("read_serial", BOARD_ID)


def test_setup_accepted_payload_has_separate_conditioned_paired_repair() -> None:
    run = ServerRun(run_id="static-setup")
    mcp = RegistryFastMCP("static-setup")
    for name in ("board_setup", "board_fix_setup"):
        mcp.registry.register(name, hidden=True, locked=True, prerequisite="board_setup-plan")
    permissions = PermissionStore(run)
    engine = PlanEngine(run, mcp.registry, permission_provider=permissions)
    permissions.set_revocation_handler(engine.invalidate)
    engine.null_response("board_setup-plan")
    parameters = {
        "mode": "setup",
        "connection_id": "probe:377322",
        "display_name": "NF Board",
        "board_type": "nrf52840dk",
        "mcu_part_number": "nRF52840",
        "serial_baudrate": 115200,
        "serial_id": "683377322",
        "datasheet_path": "Nano_BLE_MCU-nRF52840_PS_v1.1.pdf",
        "datasheet_sha256": "c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422",
    }
    payload = json.loads(
        engine.submit(
            "board_setup-plan",
            _common(parameters, permission="one-time"),
        ).message
    )

    assert payload["stable_client_fallback"]["arguments"]["actions"] == [
        {"tool_name": "board_setup", "arguments": {"board_id": BOARD_ID, **parameters}}
    ]
    paired = payload["paired_action_fallbacks"]
    assert len(paired) == 1
    assert paired[0]["action"] == "board_fix_setup"
    assert "eligible" in paired[0]["use_only_when"]
    assert paired[0]["call"]["arguments"]["actions"] == [
        {"tool_name": "board_fix_setup", "arguments": {"board_id": BOARD_ID, **parameters}}
    ]
    assert "user_permission" not in json.dumps(payload["stable_client_fallback"])


def test_null_and_rejected_plans_never_return_execution_fallbacks() -> None:
    run = ServerRun(run_id="static-rejections")
    mcp = RegistryFastMCP("static-rejections")
    mcp.registry.register("read_serial", hidden=True, locked=True, prerequisite="read_serial-plan")
    engine = PlanEngine(run, mcp.registry)

    initialized = engine.null_response("read_serial-plan")
    assert "stable_client_fallback" not in initialized.message
    with pytest.raises(PlanRefusal):
        engine.submit(
            "read_serial-plan",
            _common(_read_parameters()) | {"hypothesis_made": False},
        )

    assert engine.active_plan("read_serial", BOARD_ID) is None
    assert not mcp.registry.is_unlocked("read_serial", BOARD_ID)
