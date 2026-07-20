from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import types
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import make_board_config
from pyocd_debug_mcp.guardrails.gate import GateManager
from pyocd_debug_mcp.kernel import registry as registry_module
from pyocd_debug_mcp.probe_inventory import ProbeInfo, ProbeResolution
from pyocd_debug_mcp.services.connections import (
    BoardNotConnectedError,
    ConnectionAssignmentError,
    ConnectionManager,
    stable_connection_identity,
)
from pyocd_debug_mcp.services.session_runtime import InMemorySessionStore


BOARD_FACING_TOOL_ARGUMENTS: dict[str, dict[str, object]] = {
    "action_batch": {
        "board_id": "board-b",
        "actions": [
            {
                "tool_name": "wait",
                "arguments": {"board_id": "board-b", "ms": 1},
            }
        ],
    },
    "connect": {"board_id": "board-b"},
    "disconnect": {"board_id": "board-b"},
    "get_board_info": {"board_id": "board-b"},
    "get_state": {"board_id": "board-b"},
    "halt": {"board_id": "board-b"},
    "resume": {"board_id": "board-b"},
    "step": {"board_id": "board-b"},
    "reset_and_run": {"board_id": "board-b"},
    "reset_and_halt": {"board_id": "board-b"},
    "connect_under_reset": {"board_id": "board-b"},
    "connect_override": {"board_id": "board-b"},
    "read_cpu_register": {"board_id": "board-b", "name": "r0"},
    "read_execution_state": {"board_id": "board-b", "name": "pc"},
    "write_cpu_register": {"board_id": "board-b", "name": "r0", "value": "1"},
    "set_execution_state": {"board_id": "board-b", "name": "pc", "value": "1"},
    "register_write": {
        "board_id": "board-b",
        "address": "0x40000000",
        "mask": "0xff",
        "value": "1",
    },
    "find_symbol": {"board_id": "board-b", "query": "value", "elf_artifact": None},
    "read_memory_symbol": {
        "board_id": "board-b",
        "symbol": "value",
        "width": 32,
        "elf_artifact": None,
    },
    "read_memory_address": {
        "board_id": "board-b",
        "address": "0x20000000",
        "width": 8,
        "length": 4,
    },
    "write_memory": {
        "board_id": "board-b",
        "symbol_or_address": "0x20000000",
        "value": "1",
        "width": 32,
        "allow_address_fallback": True,
        "reason": "The address is pointer-derived.",
        "elf_artifact": None,
    },
    "set_breakpoint": {"board_id": "board-b", "symbol_or_address": "0x08000000"},
    "remove_breakpoint": {"board_id": "board-b", "address": "0x08000000"},
    "flash_application": {
        "board_id": "board-b",
        "artifact": "firmware.hex",
    },
    "flash_bootloader": {
        "board_id": "board-b",
        "artifact": "bootloader.hex",
    },
    "read_serial": {"board_id": "board-b"},
    "write_serial": {"board_id": "board-b", "text": "ping"},
    "wait": {"board_id": "board-b", "ms": 1},
    "target_unlock": {
        "board_id": "board-b",
        "recovery_mechanism": "backend_mass_erase",
    },
}


def _handle(probe_uid: str, *, display_name: str = "Mutable label") -> TargetSessionHandle:
    board = type("SessionBoard", (), {"name": display_name})()
    session = type("Session", (), {"board": board})()
    return TargetSessionHandle(
        session=session,
        board=None,
        probe_uid=probe_uid,
        route_used="fake",
        target_override=None,
    )


def _attach(
    manager: ConnectionManager,
    store: InMemorySessionStore,
    board_id: str,
    handle: TargetSessionHandle,
):
    connection_id = stable_connection_identity(handle)
    runtime = store.start_session(
        board_id=board_id,
        connection_id=connection_id,
        probe_uid=handle.probe_uid,
        route_used=handle.route_used,
    )
    return manager.assign(
        board_id,
        handle,
        runtime,
        connection_id=connection_id,
    )


def test_assign_enforces_one_board_per_connection_and_ignores_display_labels(
    tmp_path: Path,
) -> None:
    manager = ConnectionManager()
    store = InMemorySessionStore(tmp_path / "runs")
    first = _handle("PROBE-ONE", display_name="same label")
    second = _handle("PROBE-TWO", display_name="same label")

    _attach(manager, store, "board-a", first)
    _attach(manager, store, "board-b", second)

    duplicate_wrapper = _handle("probe-one", display_name="renamed label")
    runtime = store.start_session(
        board_id="board-c",
        connection_id=stable_connection_identity(duplicate_wrapper),
        probe_uid=duplicate_wrapper.probe_uid,
        route_used=duplicate_wrapper.route_used,
    )
    with pytest.raises(ConnectionAssignmentError, match="already assigned to board 'board-a'"):
        manager.assign("board-c", duplicate_wrapper, runtime)

    with pytest.raises(ConnectionAssignmentError, match="already has an active connection"):
        manager.assign("board-a", _handle("PROBE-THREE"), runtime)


def test_clear_is_board_scoped_and_fresh_manager_has_restart_defaults(tmp_path: Path) -> None:
    manager = ConnectionManager()
    store = InMemorySessionStore(tmp_path / "runs")
    first = _attach(manager, store, "board-a", _handle("probe-a"))
    second = _attach(manager, store, "board-b", _handle("probe-b"))

    assert manager.clear("board-a") is first
    with pytest.raises(BoardNotConnectedError, match="board-a"):
        manager.handle_for("board-a")
    assert manager.handle_for("board-b") is second.handle

    restarted = ConnectionManager()
    assert restarted.assigned_board_ids() == ()
    with pytest.raises(BoardNotConnectedError, match="board-b"):
        restarted.handle_for("board-b")


@pytest.fixture
def isolated_server(tmp_path: Path):
    original_manager = server.connection_manager
    original_store = server._session_store
    server.connection_manager = ConnectionManager()
    server._session_store = InMemorySessionStore(tmp_path / "runs")
    try:
        yield server.connection_manager, server._session_store
    finally:
        server.connection_manager = original_manager
        server._session_store = original_store


def test_two_fake_boards_route_to_the_named_handle(monkeypatch, isolated_server) -> None:
    manager, store = isolated_server
    handle_a = _handle("probe-a")
    handle_b = _handle("probe-b")
    connection_a = _attach(manager, store, "board-a", handle_a)
    _attach(manager, store, "board-b", handle_b)
    monkeypatch.setattr(
        server.target_control,
        "get_state",
        lambda handle: f"state:{handle.probe_uid}",
    )

    assert server.get_state("board-a") == "state:probe-a"
    assert server.get_state("board-b") == "state:probe-b"
    assert connection_a.runtime_session.events[-1].board_id == "board-a"
    assert connection_a.runtime_session.events[-1].normalized_args["board_id"] == "board-a"

    with pytest.raises(BoardNotConnectedError, match="board-c"):
        server.get_state("board-c")
    assert store._global_event_count == 1
    global_event = json.loads(store._global_events_path.read_text(encoding="utf-8"))
    assert global_event["board_id"] == "board-c"
    assert global_event["normalized_args"]["board_id"] == "board-c"


def test_unconnected_board_b_cannot_fall_back_to_board_a(
    monkeypatch,
    isolated_server,
) -> None:
    manager, store = isolated_server
    handle_a = _handle("probe-a")
    _attach(manager, store, "board-a", handle_a)
    reached: list[TargetSessionHandle] = []

    def record_backend(handle: TargetSessionHandle) -> str:
        reached.append(handle)
        return "unexpected"

    monkeypatch.setattr(server.target_control, "get_state", record_backend)

    with pytest.raises(BoardNotConnectedError, match="board-b"):
        server.get_state("board-b")

    assert reached == []
    assert manager.handle_for("board-a") is handle_a
    event = json.loads(store._global_events_path.read_text(encoding="utf-8"))
    assert event["board_id"] == "board-b"


def test_disconnect_clears_only_the_named_board(monkeypatch, isolated_server) -> None:
    manager, store = isolated_server
    handle_a = _handle("probe-a")
    handle_b = _handle("probe-b")
    _attach(manager, store, "board-a", handle_a)
    _attach(manager, store, "board-b", handle_b)
    closed: list[TargetSessionHandle] = []
    monkeypatch.setattr(server.target_control, "close_session", closed.append)

    assert server.disconnect("board-a") == "Disconnected board 'board-a'."
    assert closed == [handle_a]
    assert manager.maybe_connection("board-a") is None
    assert manager.handle_for("board-b") is handle_b


def test_ac_13_3_disconnect_clears_only_named_assignment_stamp_and_gate(
    monkeypatch, isolated_server
) -> None:
    manager, store = isolated_server
    connection_a = _attach(manager, store, "board-a", _handle("probe-a"))
    connection_b = _attach(manager, store, "board-b", _handle("probe-b"))
    gates = GateManager()
    monkeypatch.setattr(server, "gate_manager", gates)
    monkeypatch.setattr(server.target_control, "close_session", lambda handle: None)
    for connection, fingerprint in (
        (connection_a, "aggregate-a"),
        (connection_b, "aggregate-b"),
    ):
        gates.stamp_validation(
            board_id=connection.board_id,
            connection_id=connection.connection_id,
            probe_identity=connection.handle.probe_uid or connection.connection_id,
            observed_mcu=f"observed-{connection.board_id}",
            validation_run=f"validation-{connection.board_id}",
            map_digest=fingerprint,
        )

    assert server.disconnect("board-a") == "Disconnected board 'board-a'."

    assert manager.maybe_connection("board-a") is None
    assert gates.snapshot("board-a") is None
    assert manager.connection_for("board-b") == connection_b
    assert gates.require_write("board-b", connection_b.connection_id, "aggregate-b")


def test_a4_reads_require_validation_without_freshness_and_writes_require_both(
    monkeypatch, isolated_server
) -> None:
    manager, store = isolated_server
    connection = _attach(manager, store, "board-a", _handle("probe-a"))
    calls: list[tuple[object, ...]] = []

    class RecordingGate:
        def require_validated(self, board_id: str, connection_id: str) -> None:
            calls.append(("validated", board_id, connection_id))

        def require_write(self, board_id: str, connection_id: str, aggregate: str) -> None:
            calls.append(("write", board_id, connection_id, aggregate))

    class RecordingSafety:
        def current_aggregate(self, board_id: str) -> str:
            calls.append(("freshness", board_id))
            return "aggregate-a"

    monkeypatch.setattr(server, "gate_manager", RecordingGate())
    monkeypatch.setattr(server, "_safety_policy", RecordingSafety())

    server._require_layer0("read_memory_address", "board-a")
    server._require_layer0("read_serial", "board-a")
    assert calls == [
        ("validated", "board-a", connection.connection_id),
        ("validated", "board-a", connection.connection_id),
    ]

    calls.clear()
    server._require_layer0("write_memory", "board-a")
    server._require_layer0("write_serial", "board-a")
    server._require_layer0("serial_exchange", "board-a")
    assert calls == [
        ("freshness", "board-a"),
        ("write", "board-a", connection.connection_id, "aggregate-a"),
        ("freshness", "board-a"),
        ("write", "board-a", connection.connection_id, "aggregate-a"),
        ("freshness", "board-a"),
        ("write", "board-a", connection.connection_id, "aggregate-a"),
    ]


def test_same_board_serializes_while_cross_board_operations_overlap(
    monkeypatch,
    isolated_server,
) -> None:
    manager, store = isolated_server
    _attach(manager, store, "board-a", _handle("probe-a"))
    _attach(manager, store, "board-b", _handle("probe-b"))

    active = 0
    maximum = 0
    counter_lock = threading.Lock()

    def measured_state(handle: TargetSessionHandle) -> str:
        nonlocal active, maximum
        with counter_lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with counter_lock:
            active -= 1
        return str(handle.probe_uid)

    monkeypatch.setattr(server.target_control, "get_state", measured_state)
    with ThreadPoolExecutor(max_workers=2) as executor:
        same = [executor.submit(server.get_state, "board-a") for _ in range(2)]
        assert [future.result(timeout=1) for future in same] == ["probe-a", "probe-a"]
    assert maximum == 1

    maximum = 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        cross = [
            executor.submit(server.get_state, "board-a"),
            executor.submit(server.get_state, "board-b"),
        ]
        assert {future.result(timeout=1) for future in cross} == {"probe-a", "probe-b"}
    assert maximum == 2


def test_every_board_facing_tool_requires_board_id() -> None:
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}

    assert BOARD_FACING_TOOL_ARGUMENTS.keys() <= tools.keys()
    for name in BOARD_FACING_TOOL_ARGUMENTS:
        schema = tools[name].parameters
        assert "board_id" in schema["properties"], name
        assert "board_id" in schema["required"], name


def test_public_connect_schema_is_profile_only_and_manual_override_stays_guarded() -> None:
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}

    connect_schema = tools["connect"].parameters
    assert set(connect_schema["properties"]) == {"board_id"}
    assert connect_schema["required"] == ["board_id"]
    assert connect_schema["additionalProperties"] is False
    assert "profile" in (tools["connect"].description or "").lower()
    assert "connect_override-plan" in (tools["connect"].description or "")

    override_schema = tools["connect_override"].parameters
    assert set(override_schema["properties"]) == {
        "board_id",
        "probe_uid",
        "target_override",
        "external_board_config",
    }
    assert "connect_override" not in server.tool_registry.advertised()
    assert "connect_override-plan" in server.tool_registry.advertised()


def test_public_connect_selects_profile_only_internal_mode_while_override_keeps_manual_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_connect(*args: object, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return "connected"

    monkeypatch.setattr(server, "_connect_impl", fake_connect)

    assert server.connect("board-a") == "connected"
    assert calls == [
        (("board-a",), {"allow_environment_overrides": False}),
    ]

    calls.clear()
    assert (
        server._connect_override_impl(
            "board-a",
            unique_id="probe-a",
            target="nrf52840",
            board_config="external.yaml",
        )
        == "connected"
    )
    assert calls == [
        (
            ("board-a", "probe-a", "nrf52840", "external.yaml"),
            {"allow_environment_overrides": True, "allow_missing_profile": True},
        )
    ]


@pytest.mark.asyncio
async def test_public_connect_rejects_manual_override_fields_before_backend_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        server,
        "_connect_impl",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "unexpected",
    )

    with pytest.raises(ToolError, match="unique_id|Extra inputs"):
        await server.mcp.call_tool(
            "connect",
            {
                "board_id": "board-a",
                "unique_id": "probe-a",
                "target": "nrf52840",
                "board_config": "external.yaml",
            },
        )

    assert calls == []


def test_under_reset_replays_exact_profile_pack_and_pdsc_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = make_board_config(
        {
            "board_id": "generic_under_reset",
            "display_name": "Generic Under Reset",
            "mcu_family": "generic",
            "probe_family": "cmsisdap",
            "pyocd_target": "part123",
        },
        None,
    )
    profile = SimpleNamespace(
        board=board,
        device_support={"pdsc_device": "PART123"},
    )
    selected_pack = SimpleNamespace(
        path=Path("exact.pack"), spec=SimpleNamespace(sha256="a" * 64)
    )
    calls: list[dict[str, object]] = []
    handle = SimpleNamespace(probe_uid="probe-1", route_used="pyocd-native")
    monkeypatch.setattr(server.connection_manager, "maybe_connection", lambda _board: None)
    monkeypatch.setattr(server.connection_manager, "assign", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "resolve_board_config", lambda *_args, **_kwargs: board)
    monkeypatch.setattr(server, "_resolve_probe_uid_for_connect", lambda *_args: "probe-1")
    monkeypatch.setattr(server._profile_repository, "load", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(server, "_verified_pack_for_profile", lambda _profile: selected_pack)
    monkeypatch.setattr(
        server.target_control,
        "connect_under_reset",
        lambda **kwargs: calls.append(dict(kwargs)) or handle,
    )
    monkeypatch.setattr(server, "stable_connection_identity", lambda _handle: "connection-1")
    monkeypatch.setattr(
        server._session_store,
        "start_session",
        lambda **_kwargs: SimpleNamespace(session_id="session-1"),
    )
    monkeypatch.setattr(server.gate_manager, "clear", lambda *_args: None)
    monkeypatch.setattr(server, "_record_event", lambda *_args, **_kwargs: None)

    result = server._connect_under_reset_impl("generic_under_reset", "probe-1", None)

    assert "Connected under physical reset" in result
    assert calls[0]["target"] == "part123"
    assert calls[0]["pack_path"] == selected_pack.path
    assert calls[0]["pack_sha256"] == selected_pack.spec.sha256
    assert calls[0]["pdsc_device"] == "PART123"


def test_under_reset_rejects_generic_target_override_before_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = make_board_config(
        {
            "board_id": "generic_under_reset",
            "display_name": "Generic Under Reset",
            "mcu_family": "generic",
            "probe_family": "cmsisdap",
            "pyocd_target": "part123",
        },
        None,
    )
    profile = SimpleNamespace(
        board=board,
        device_support={"pdsc_device": "PART123"},
    )
    calls: list[object] = []
    monkeypatch.setattr(server.connection_manager, "maybe_connection", lambda _board: None)
    monkeypatch.setattr(server, "resolve_board_config", lambda *_args, **_kwargs: board)
    monkeypatch.setattr(server, "_resolve_probe_uid_for_connect", lambda *_args: "probe-1")
    monkeypatch.setattr(server._profile_repository, "load", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(
        server,
        "_verified_pack_for_profile",
        lambda _profile: SimpleNamespace(
            path=Path("exact.pack"), spec=SimpleNamespace(sha256="a" * 64)
        ),
    )
    monkeypatch.setattr(
        server.target_control,
        "connect_under_reset",
        lambda **kwargs: calls.append(kwargs),
    )

    with pytest.raises(ValueError, match="exact PDSC target"):
        server._connect_under_reset_impl("generic_under_reset", "probe-1", "wrong")

    assert calls == []


@pytest.mark.asyncio
async def test_live_connect_ap_failure_is_neutral_for_generic_and_jlink_retry_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generic_board = make_board_config(
        {
            "board_id": "neutral_generic_board",
            "display_name": "Neutral Generic Board",
            "mcu_family": "generic_mcu",
            "probe_family": "cmsisdap",
            "pyocd_target": "generic_target",
        },
        None,
    )
    jlink_board = make_board_config(
        {
            "board_id": "neutral_fallback_board",
            "display_name": "Neutral Fallback Board",
            "mcu_family": "generic_mcu",
            "probe_family": "jlink",
            "pyocd_target": "generic_target",
        },
        None,
    )
    boards = {
        generic_board.board_id: generic_board,
        jlink_board.board_id: jlink_board,
    }
    probes = {
        generic_board.board_id: ProbeInfo(
            uid="generic-probe-uid",
            description="Generic API probe",
            raw="generic api probe",
            family="cmsisdap",
            family_source="pyocd_api",
        ),
        jlink_board.board_id: ProbeInfo(
            uid="fallback-probe-uid",
            description="Configured fallback probe",
            raw="configured fallback probe",
            family="jlink",
            family_source="configured_cli",
        ),
    }

    monkeypatch.setattr(
        server,
        "resolve_board_config",
        lambda board_id, *_args, **_kwargs: boards[board_id],
    )
    monkeypatch.setattr(
        server,
        "resolve_probe_for_board",
        lambda board, **_kwargs: ProbeResolution(
            probe=probes[board.board_id],
            note="exact test identity",
            probes=(probes[board.board_id],),
        ),
    )
    monkeypatch.setattr(
        swd_pyocd,
        "list_connected_probes",
        lambda _run_cmd: [probes[jlink_board.board_id]],
    )

    selected_uids: list[str | None] = []

    class FailingSession:
        def __init__(self, failure: Exception, probe_uid: str) -> None:
            self.failure = failure
            self.probe = SimpleNamespace(unique_id=probe_uid)

        def open(self) -> None:
            raise self.failure

        def close(self) -> None:
            return None

    def choose_session(
        *, probe_uid: str | None, options: dict[str, object] | None
    ) -> FailingSession:
        del options
        selected_uids.append(probe_uid)
        if probe_uid == "fallback-probe-uid":
            return FailingSession(
                RuntimeError("No emulator with serial number was found"), probe_uid
            )
        return FailingSession(KeyError(1), probe_uid or "fallback-probe-uid")

    monkeypatch.setattr(
        swd_pyocd.PyOCDSWDInterface,
        "_choose_session",
        staticmethod(choose_session),
    )

    async with create_connected_server_and_client_session(server.mcp) as session:
        generic_result = await session.call_tool("connect", {"board_id": generic_board.board_id})
        fallback_result = await session.call_tool("connect", {"board_id": jlink_board.board_id})

    def error_text(result: types.CallToolResult) -> str:
        assert result.isError is True
        assert len(result.content) == 1
        assert isinstance(result.content[0], types.TextContent)
        return result.content[0].text

    generic_error = error_text(generic_result)
    fallback_error = error_text(fallback_result)
    expected = (
        "pyOCD target initialization could not reach expected access port AP#1. "
        "Possible causes include target lock, reset or attach state, probe connectivity, "
        "or an incompatible target selection. Follow the exact setup or validation remedy; "
        "use typed target recovery only when the server identifies it."
    )
    assert expected in generic_error
    assert expected in fallback_error
    assert generic_error == fallback_error
    assert all(
        forbidden not in generic_error.casefold()
        for forbidden in ("nrf", "nordic", "j-link", "jlink")
    )
    assert selected_uids == ["generic-probe-uid", "fallback-probe-uid", None]


def test_profile_only_resolution_ignores_launch_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYOCD_BOARD_CONFIG", "C:/definitely-not-a-board-config.yaml")
    monkeypatch.setenv("PYOCD_PROBE_UID", "stale-wrong-probe")

    board = server.resolve_board_config(
        "nrf52840dk",
        None,
        allow_environment_overrides=False,
    )
    assert board is not None
    assert board.board_id == "nrf52840dk"

    monkeypatch.setattr(
        server,
        "resolve_probe_for_board",
        lambda *_args, **_kwargs: SimpleNamespace(
            probe=SimpleNamespace(uid="profile-resolved-probe"),
            note="profile match",
        ),
    )
    assert (
        server._resolve_probe_uid_for_connect(
            board,
            None,
            allow_environment_override=False,
        )
        == "profile-resolved-probe"
    )


async def test_every_board_facing_tool_threads_board_id_into_dispatch(monkeypatch) -> None:
    dispatched: list[tuple[str, str | None]] = []

    async def capture_dispatch(
        tool_name,
        board_id,
        operation,
        timeout,
        **dispatch_options,
    ):
        del operation, timeout, dispatch_options
        dispatched.append((tool_name, board_id))
        return "captured"

    monkeypatch.setattr(registry_module, "dispatch", capture_dispatch)

    try:
        for tool_name, arguments in BOARD_FACING_TOOL_ARGUMENTS.items():
            if tool_name in server.M5_GUARDED_ACTIONS + server.M8_GUARDED_ACTIONS:
                server.tool_registry.unlock(tool_name, "board-b")
            assert await server.mcp.call_tool(tool_name, arguments) == "captured"
    finally:
        for tool_name in server.M5_GUARDED_ACTIONS + server.M8_GUARDED_ACTIONS:
            server.tool_registry.relock(tool_name, "board-b")

    assert dispatched == [(name, "board-b") for name in BOARD_FACING_TOOL_ARGUMENTS]


def test_modular_backend_surface_has_no_global_session_escape_path() -> None:
    source_path = Path(server.__file__).resolve()
    source_text = source_path.read_text(encoding="utf-8")

    assert "global _session_handle" not in source_text
    assert "global _runtime_session" not in source_text
    assert "global _lock" not in source_text
    assert set(server.M5_GUARDED_ACTIONS) <= set(server.mcp._guarded_dispatch)
    assert set(server.session_tool_handlers) == {
        "connect",
        "disconnect",
        "get_board_info",
        "get_state",
        "connect_override",
    }
    assert set(server.execution_tool_handlers) == {
        "halt",
        "resume",
        "step",
        "reset_and_run",
        "reset_and_halt",
        "connect_under_reset",
    }
    assert set(server.register_tool_handlers) == {
        "read_cpu_register",
        "read_execution_state",
        "write_cpu_register",
        "set_execution_state",
        "register_write",
    }
    assert set(server.memory_tool_handlers) == {
        "find_symbol",
        "read_memory_symbol",
        "read_memory_address",
        "write_memory",
    }
    assert set(server.flash_tool_handlers) == {"flash_application", "flash_bootloader"}
    assert set(server.serial_tool_handlers) == {
        "read_serial",
        "write_serial",
        "serial_exchange",
    }
    assert set(server.breakpoint_tool_handlers) == {"set_breakpoint", "remove_breakpoint"}
    assert set(server.misc_tool_handlers) == {"wait"}
