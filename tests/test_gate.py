from __future__ import annotations

import dataclasses

import pytest

from pyocd_debug_mcp.guardrails.gate import GateManager, GateRefusal
from pyocd_debug_mcp.kernel.run_state import create_server_run


def stamp(
    manager: GateManager,
    board: str = "board_a",
    connection: str = "probe:a",
    digest: str = "map-a",
):
    return manager.stamp_validation(
        board_id=board,
        connection_id=connection,
        probe_identity=connection,
        observed_mcu="STM32L476RGT6",
        validation_run="validation-1",
        map_digest=digest,
    )


def test_restart_drops_all_live_identity_map_and_mismatch_state() -> None:
    first_run = create_server_run()
    first = GateManager(first_run.gates)
    stamp(first)
    first.record_mismatch(
        board_id="board_b",
        connection_id="probe:b",
        probe_identity="probe-b",
        expected_mcu="expected",
        observed_mcu="observed",
        validation_run="validation-2",
    )

    restarted = GateManager(create_server_run().gates)

    assert restarted.live_identity("board_a") is None
    assert restarted.map_stamp("board_a") is None
    assert (
        restarted.mismatch_allowance(
            board_id="board_b",
            connection_id="probe:b",
            probe_identity="probe-b",
            expected_mcu="expected",
            observed_mcu="observed",
        )
        is None
    )


def test_run_gate_state_has_no_serialization_api() -> None:
    manager = GateManager()
    stamp(manager)
    for forbidden in ("save", "load", "serialize", "restore", "to_document"):
        assert not hasattr(manager, forbidden)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        manager.live_identity("board_a").connection_id = "changed"  # type: ignore[misc,union-attr]


def test_success_stamps_distinct_live_identity_and_safety_map_concepts() -> None:
    manager = GateManager()

    stamped = stamp(manager)

    assert stamped.live_identity == manager.live_identity("board_a")
    assert stamped.safety_map == manager.map_stamp("board_a")
    assert stamped.connection_id == "probe:a"
    assert stamped.observed_mcu == "STM32L476RGT6"
    assert stamped.validation_run == "validation-1"
    assert stamped.map_digest == "map-a"
    assert stamped.validated_at.endswith("Z")


def test_connection_change_clears_live_proof_and_requires_validation() -> None:
    manager = GateManager()
    stamp(manager)

    with pytest.raises(GateRefusal) as changed:
        manager.require_validated("board_a", "probe:replacement")

    assert changed.value.code == "gate/connection-changed"
    assert changed.value.remedy == ("board_validate",)
    assert manager.live_identity("board_a") is None
    assert manager.map_stamp("board_a") is None


def test_disconnect_is_board_local_and_clears_stamp() -> None:
    manager = GateManager()
    stamp(manager, "board_a", "probe:a", "map-a")
    stamp(manager, "board_b", "probe:b", "map-b")

    removed = manager.clear_connection("probe:a", "board disconnected")

    assert removed == ("board_a",)
    assert manager.snapshot("board_a") is None
    assert manager.require_write("board_b", "probe:b", "map-b")
    with pytest.raises(GateRefusal, match="board disconnected"):
        manager.require_validated("board_a", "probe:a")


def test_map_drift_removes_only_map_stamp_and_names_refresh() -> None:
    manager = GateManager()
    stamp(manager)

    with pytest.raises(GateRefusal) as stale:
        manager.require_write("board_a", "probe:a", "map-new")

    assert stale.value.code == "gate/safety-map-stale"
    assert stale.value.remedy == ("board_safety_refresh",)
    assert manager.live_identity("board_a") is not None
    assert manager.map_stamp("board_a") is None


def test_refresh_updates_map_only_and_never_creates_identity_authority() -> None:
    manager = GateManager()
    assert manager.refresh_map_stamp("board_a", "probe:a", "map-new") is None
    assert manager.live_identity("board_a") is None
    assert manager.map_stamp("board_a") is None

    original = stamp(manager)
    refreshed = manager.refresh_map_stamp("board_a", "probe:a", "map-new")

    assert refreshed is not None
    assert refreshed.live_identity is original.live_identity
    assert refreshed.map_digest == "map-new"
    assert manager.require_write("board_a", "probe:a", "map-new") == refreshed


def test_mismatch_allowance_requires_exact_five_part_scope() -> None:
    manager = GateManager()
    allowance = manager.record_mismatch(
        board_id="board_a",
        connection_id="connection-a",
        probe_identity="probe-a",
        expected_mcu="expected-mcu",
        observed_mcu="observed-mcu",
        validation_run="validation-a",
    )

    assert (
        manager.mismatch_allowance(
            board_id="board_a",
            connection_id="connection-a",
            probe_identity="probe-a",
            expected_mcu="expected-mcu",
            observed_mcu="observed-mcu",
        )
        == allowance
    )
    assert manager.current_mismatch("board_a", "connection-a", "probe-a") == allowance
    for field, replacement in (
        ("board_id", "board-b"),
        ("connection_id", "connection-b"),
        ("probe_identity", "probe-b"),
        ("expected_mcu", "different-expected"),
        ("observed_mcu", "different-observed"),
    ):
        arguments = {
            "board_id": "board_a",
            "connection_id": "connection-a",
            "probe_identity": "probe-a",
            "expected_mcu": "expected-mcu",
            "observed_mcu": "observed-mcu",
        }
        arguments[field] = replacement
        assert manager.mismatch_allowance(**arguments) is None


def test_mismatch_allowance_clears_on_disconnect_and_successful_validation() -> None:
    manager = GateManager()
    arguments = {
        "board_id": "board_a",
        "connection_id": "connection-a",
        "probe_identity": "probe-a",
        "expected_mcu": "expected-mcu",
        "observed_mcu": "observed-mcu",
    }
    manager.record_mismatch(**arguments, validation_run="validation-a")
    manager.clear_connection("connection-a", "disconnect")
    assert manager.mismatch_allowance(**arguments) is None

    manager.record_mismatch(**arguments, validation_run="validation-b")
    stamp(manager, "board_a", "connection-a", "map-a")
    assert manager.mismatch_allowance(**arguments) is None
