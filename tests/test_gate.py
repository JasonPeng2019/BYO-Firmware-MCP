from __future__ import annotations

import pytest

from pyocd_debug_mcp.guardrails.gate import GateManager, GateRefusal
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.kernel.run_state import create_server_run
from pyocd_debug_mcp import server


def stamp(
    manager: GateManager,
    board_id: str = "board_a",
    connection_id: str = "probe:a",
    fingerprint: str = "aggregate-a",
):
    return manager.stamp_validation(
        board_id=board_id,
        connection_id=connection_id,
        hardware_result="validation_passed",
        probe_identity=connection_id.removeprefix("probe:"),
        aggregate_fingerprint=fingerprint,
    )


def test_ac_13_1_restart_and_new_manager_are_default_closed() -> None:
    first_run = create_server_run()
    first = GateManager(first_run.gates)
    stamp(first)
    assert first.require_write("board_a", "probe:a", "aggregate-a")

    restarted = create_server_run()
    second = GateManager(restarted.gates)
    assert restarted.gates == {}
    with pytest.raises(GateRefusal) as refusal:
        second.require_write("board_a", "probe:a", "aggregate-a")
    assert refusal.value.code == "gate/validation-required"
    assert refusal.value.remedy == ("board_validate",)


def test_ac_13_4_disk_artifacts_never_restore_gate_authority(tmp_path) -> None:
    store = FirmStore(tmp_path)
    map_path = store.layout.safety_board("board_a") / "memory_map.yaml"
    store.atomic_write_yaml(
        map_path,
        {
            "schema_version": 2,
            "board_id": "board_a",
            "identity": {"target": "example"},
        },
    )
    first = GateManager(create_server_run().gates)
    stamp(first)
    assert first.snapshot("board_a") is not None

    restarted = GateManager(create_server_run().gates)
    assert map_path.is_file()
    with pytest.raises(GateRefusal) as refusal:
        restarted.require_write("board_a", "probe:a", "aggregate-a")
    assert refusal.value.code == "gate/validation-required"
    assert refusal.value.remedy == ("board_validate",)


def test_ac_13_2_no_registered_tool_can_open_a_gate_directly() -> None:
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert not {
        "open_gate",
        "gate_open",
        "set_gate",
        "stamp_validation",
    }.intersection(registered)


@pytest.mark.parametrize(
    "result",
    [
        "validation_failed",
        "validation_blocked",
        "validation_incomplete",
        "validation_research_required",
    ],
)
def test_ac_12_1_only_successful_board_validate_can_stamp(result: str) -> None:
    manager = GateManager()
    with pytest.raises(ValueError, match="successful board_validate"):
        manager.stamp_validation(
            board_id="board_a",
            connection_id="probe:a",
            hardware_result=result,
            probe_identity="a",
            aggregate_fingerprint="aggregate-a",
        )
    assert manager.snapshot("board_a") is None


def test_ac_12_1_stamp_binds_board_connection_probe_result_and_fingerprint() -> None:
    manager = GateManager()
    stamped = stamp(manager)

    assert stamped.board_id == "board_a"
    assert stamped.connection_id == "probe:a"
    assert stamped.probe_identity == "a"
    assert stamped.hardware_result == "validation_passed"
    assert stamped.aggregate_fingerprint == "aggregate-a"
    assert stamped.validated_at.endswith("Z")

    with pytest.raises(GateRefusal) as changed:
        manager.require_validated("board_a", "probe:replacement")
    assert changed.value.code == "gate/connection-changed"
    assert changed.value.remedy == ("board_validate",)
    assert manager.snapshot("board_a") is None


def test_ac_13_3_disconnect_is_board_local_and_clears_stamp() -> None:
    manager = GateManager()
    stamp(manager, "board_a", "probe:a", "aggregate-a")
    stamp(manager, "board_b", "probe:b", "aggregate-b")

    removed = manager.clear("board_a", "board disconnected")

    assert removed is not None
    assert manager.snapshot("board_a") is None
    assert manager.require_write("board_b", "probe:b", "aggregate-b")
    with pytest.raises(GateRefusal, match="board disconnected"):
        manager.require_validated("board_a", "probe:a")


def test_ac_11_6_map_digest_change_closes_gate_and_names_refresh() -> None:
    manager = GateManager()
    stamp(manager)

    with pytest.raises(GateRefusal) as stale:
        manager.require_write("board_a", "probe:a", "aggregate-new")

    assert stale.value.code == "gate/configuration-stale"
    assert stale.value.remedy == ("board_safety_refresh",)
    assert manager.snapshot("board_a") is None


def test_refresh_updates_only_an_existing_valid_stamp_and_never_opens_gate() -> None:
    manager = GateManager()
    assert manager.refresh_fingerprint("board_a", "probe:a", "aggregate-new") is None
    assert manager.snapshot("board_a") is None

    stamp(manager)
    refreshed = manager.refresh_fingerprint("board_a", "probe:a", "aggregate-new")
    assert refreshed is not None
    assert refreshed.aggregate_fingerprint == "aggregate-new"
    assert manager.require_write("board_a", "probe:a", "aggregate-new") == refreshed
