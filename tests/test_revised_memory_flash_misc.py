from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.guardrails.flash_gate import (
    FlashArtifactIdentity,
    ResolvedFlashRequest,
)
from pyocd_debug_mcp.guardrails.plan_defs import BudgetMode, PermissionMode, PLAN_DEFINITIONS
from pyocd_debug_mcp.kernel.operations import SAFE_EXIT_REMINDER
from pyocd_debug_mcp.services.convergence_watcher import ConvergenceWatcher, FLASH_TOOL
from pyocd_debug_mcp.services.session_runtime import (
    ActionContext,
    InMemorySessionStore,
    ToolEvent,
    ToolOutcome,
    utc_now_text,
)
from pyocd_debug_mcp.services.symbols import ResolvedSymbol, find_symbols, resolve_symbol
from pyocd_debug_mcp.target_errors import SymbolLookupError
from pyocd_debug_mcp.tools.breakpoints import (
    BreakpointToolServices,
    build_breakpoint_handlers,
)
from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers
from pyocd_debug_mcp.tools.memory import (
    MAX_ADDRESS_READ_BYTES,
    MemoryToolServices,
    build_memory_handlers,
)
from pyocd_debug_mcp.tools.misc import MiscToolServices, build_misc_handlers
from pyocd_debug_mcp.tools.serial import SerialToolServices, build_serial_handlers


def _format_refusal(refusal, **kwargs) -> str:
    del kwargs
    return f"Refused [{refusal.code}]: {refusal.message}"


def _memory_handlers(
    tmp_path: Path,
    *,
    check_memory_read=None,
    symbol_artifact_for=None,
):
    artifact = tmp_path / "firmware.elf"
    artifact.write_bytes(b"elf")
    calls: list[tuple[str, object]] = []
    handle = object()
    resolved = ResolvedSymbol("counter", 0x20000010, 4, "STT_OBJECT")
    services = MemoryToolServices(
        runtime_for=lambda board: None,
        active_session_id=lambda board: None,
        duration_ms=lambda started: 1,
        record_event=lambda *args, **kwargs: None,
        format_refusal=_format_refusal,
        handle_for=lambda board: handle,
        symbol_artifact_for=symbol_artifact_for or (lambda selected: artifact),
        find_symbols=lambda selected, query: (resolved,),
        resolve_symbol=lambda selected, name: resolved,
        read_target_memory=lambda selected, address, width: (
            calls.append(("read", (selected, address, width))) or 0x12
        ),
        read_target_block=lambda selected, address, length: (
            calls.append(("block", (selected, address, length))) or [0xAA, 0x55]
        ),
        write_target_memory=lambda selected, address, value, width: calls.append(
            ("write", (selected, address, value, width))
        ),
        check_memory_read=check_memory_read or (lambda board, address, size: None),
    )
    return build_memory_handlers(services), calls


def test_task8_surface_visibility_and_legacy_retirement() -> None:
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    advertised = set(server.tool_registry.advertised())
    always = {
        "find_symbol",
        "read_memory_symbol",
        "remove_breakpoint",
        "wait",
        "target_unlock-plan",
    }
    guarded = set(server.TASK8_GUARDED_ACTIONS)

    assert always <= advertised
    assert guarded <= registered
    assert guarded.isdisjoint(advertised)
    assert {f"{name}-plan" for name in guarded} <= advertised
    assert {
        "read_memory",
        "read_memory_block",
        "read_symbol_u32",
        "flash_firmware",
    }.isdisjoint(registered)
    assert "unlock_recover" not in registered
    assert "target_unlock" in registered
    assert "target_unlock" not in advertised


def test_task8_budget_and_permission_table() -> None:
    fixed = {"write_memory", "set_breakpoint", "flash_application", "flash_bootloader"}
    flexible = {"read_memory_address", "read_serial", "write_serial"}

    assert all(PLAN_DEFINITIONS[name].budget_mode is BudgetMode.FIXED for name in fixed)
    assert all(PLAN_DEFINITIONS[name].budget_mode is BudgetMode.FLEXIBLE for name in flexible)
    assert PLAN_DEFINITIONS["flash_bootloader"].permission_mode is PermissionMode.REQUIRED
    for name in (fixed | flexible) - {"flash_bootloader"}:
        assert PLAN_DEFINITIONS[name].permission_mode is PermissionMode.NONE


def test_memory_symbol_search_reads_and_raw_cap(tmp_path: Path) -> None:
    handlers, calls = _memory_handlers(tmp_path)

    assert "counter@0x20000010" in handlers["find_symbol"]("board_b", "count")
    symbol_result = handlers["read_memory_symbol"]("board_b", "counter", 32)
    assert "value=0x00000012" in symbol_result
    assert SAFE_EXIT_REMINDER in symbol_result
    assert "AA 55" in handlers["read_memory_address"](
        "board_b", "0x20000000", 8, MAX_ADDRESS_READ_BYTES
    )
    calls.clear()
    refused = handlers["read_memory_address"](
        "board_b", "0x20000000", 8, MAX_ADDRESS_READ_BYTES + 1
    )
    assert "memory/invalid-length" in refused
    assert SAFE_EXIT_REMINDER in refused
    assert calls == []


def test_memory_reads_check_scalar_block_and_symbol_bytes_before_backend(
    tmp_path: Path,
) -> None:
    checked: list[tuple[str, int, int]] = []
    handlers, calls = _memory_handlers(
        tmp_path,
        check_memory_read=lambda board, address, size: checked.append((board, address, size)),
    )

    handlers["read_memory_symbol"]("board_b", "counter", 16)
    handlers["read_memory_address"]("board_b", "0x20000000", 32, None)
    handlers["read_memory_address"]("board_b", "0x40000000", 8, 17)

    assert checked == [
        ("board_b", 0x20000010, 2),
        ("board_b", 0x20000000, 4),
        ("board_b", 0x40000000, 17),
    ]
    assert [call[0] for call in calls] == ["read", "read", "block"]


def test_missing_current_symbol_artifact_refuses_before_backend(tmp_path: Path) -> None:
    def missing(_handle):
        raise RuntimeError("current ELF missing")

    handlers, calls = _memory_handlers(tmp_path, symbol_artifact_for=missing)

    assert "memory/symbol-artifact-unavailable" in handlers["find_symbol"]("board_b", "counter")
    assert "memory/symbol-artifact-unavailable" in handlers["read_memory_symbol"](
        "board_b", "counter", 32
    )
    assert "memory/symbol-artifact-unavailable" in handlers["write_memory"](
        "board_b", "counter", 1, 32, False, None
    )
    assert calls == []


def test_real_symbol_parser_failure_is_refused_before_backend(tmp_path: Path) -> None:
    artifact = tmp_path / "bad.elf"
    artifact.write_bytes(b"not-an-elf")
    handle = object()
    failing = build_memory_handlers(
        MemoryToolServices(
            runtime_for=lambda board: None,
            active_session_id=lambda board: None,
            duration_ms=lambda started: 1,
            record_event=lambda *args, **kwargs: None,
            format_refusal=_format_refusal,
            handle_for=lambda board: handle,
            symbol_artifact_for=lambda selected: artifact,
            find_symbols=find_symbols,
            resolve_symbol=resolve_symbol,
            read_target_memory=lambda *args: pytest.fail("backend read reached"),
            read_target_block=lambda *args: pytest.fail("backend block read reached"),
            write_target_memory=lambda *args: pytest.fail("backend write reached"),
            check_memory_read=lambda *args: None,
        )
    )

    assert "memory/symbol-artifact-unavailable" in failing["find_symbol"](
        "board_b", "counter"
    )
    assert "memory/symbol-artifact-unavailable" in failing["read_memory_symbol"](
        "board_b", "counter", 32
    )


def test_missing_symbol_has_distinct_refusal(tmp_path: Path) -> None:
    artifact = tmp_path / "firmware.elf"
    artifact.write_bytes(b"fixture")
    handlers = build_memory_handlers(
        MemoryToolServices(
            runtime_for=lambda board: None,
            active_session_id=lambda board: None,
            duration_ms=lambda started: 1,
            record_event=lambda *args, **kwargs: None,
            format_refusal=_format_refusal,
            handle_for=lambda board: object(),
            symbol_artifact_for=lambda selected: artifact,
            find_symbols=lambda selected, query: (),
            resolve_symbol=lambda selected, name: (_ for _ in ()).throw(
                SymbolLookupError(f"Symbol '{name}' was not found")
            ),
            read_target_memory=lambda *args: pytest.fail("backend read reached"),
            read_target_block=lambda *args: pytest.fail("backend block read reached"),
            write_target_memory=lambda *args: pytest.fail("backend write reached"),
            check_memory_read=lambda *args: None,
        )
    )

    assert "memory/symbol-not-found" in handlers["read_memory_symbol"](
        "board_b", "absent", 32
    )


def test_disappearing_symbol_artifact_is_not_reported_as_missing_symbol(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "firmware.elf"
    artifact.write_bytes(b"fixture")

    def selected_artifact(_handle):
        if not artifact.is_file():
            raise RuntimeError("artifact disappeared")
        return artifact

    def disappearing_lookup(_artifact, name):
        artifact.unlink()
        raise SymbolLookupError(f"ELF artifact does not exist while resolving {name}")

    handlers = build_memory_handlers(
        MemoryToolServices(
            runtime_for=lambda board: None,
            active_session_id=lambda board: None,
            duration_ms=lambda started: 1,
            record_event=lambda *args, **kwargs: None,
            format_refusal=_format_refusal,
            handle_for=lambda board: object(),
            symbol_artifact_for=selected_artifact,
            find_symbols=lambda selected, query: (),
            resolve_symbol=disappearing_lookup,
            read_target_memory=lambda *args: pytest.fail("backend read reached"),
            read_target_block=lambda *args: pytest.fail("backend block read reached"),
            write_target_memory=lambda *args: pytest.fail("backend write reached"),
            check_memory_read=lambda *args: None,
        )
    )

    result = handlers["read_memory_symbol"]("board_b", "counter", 32)
    assert "memory/symbol-artifact-unavailable" in result
    assert "memory/symbol-not-found" not in result


@pytest.mark.parametrize("operation", ["read", "write"])
def test_symbol_change_during_resolution_blocks_backend(
    tmp_path: Path,
    operation: str,
) -> None:
    artifact = tmp_path / "firmware.elf"
    stable = b"stable"
    artifact.write_bytes(stable)
    backend_calls: list[str] = []

    def selected_artifact(_handle):
        if artifact.read_bytes() != stable:
            raise RuntimeError("artifact changed")
        return artifact

    def changing_resolver(_artifact, name):
        artifact.write_bytes(b"changed")
        return ResolvedSymbol(name, 0x20000010, 4, "STT_OBJECT")

    handlers = build_memory_handlers(
        MemoryToolServices(
            runtime_for=lambda board: None,
            active_session_id=lambda board: None,
            duration_ms=lambda started: 1,
            record_event=lambda *args, **kwargs: None,
            format_refusal=_format_refusal,
            handle_for=lambda board: object(),
            symbol_artifact_for=selected_artifact,
            find_symbols=lambda selected, query: (),
            resolve_symbol=changing_resolver,
            read_target_memory=lambda *args: backend_calls.append("read") or 0,
            read_target_block=lambda *args: [],
            write_target_memory=lambda *args: backend_calls.append("write"),
            check_memory_read=lambda *args: backend_calls.append("check-read"),
            check_memory_write=lambda *args: backend_calls.append("check-write"),
        )
    )

    if operation == "read":
        result = handlers["read_memory_symbol"]("board_b", "counter", 32)
    else:
        result = handlers["write_memory"]("board_b", "counter", 1, 32, False, None)

    assert "memory/symbol-artifact-unavailable" in result
    assert backend_calls == []


def test_raw_memory_read_containment_is_a_central_pre_execution_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        server,
        "_safety_policy",
        SimpleNamespace(
            check_memory_read=lambda board, address, size: checked.append((board, address, size))
        ),
    )

    server._enforce_action_containment(
        "read_memory_address",
        "board_b",
        {"address": "0x20000000", "width": 16, "length": None},
    )
    server._enforce_action_containment(
        "read_memory_address",
        "board_b",
        {"address": "0x40000000", "width": 8, "length": 33},
    )

    assert checked == [
        ("board_b", 0x20000000, 2),
        ("board_b", 0x40000000, 33),
    ]


def test_write_memory_is_symbol_first_and_reports_ram_containment(tmp_path: Path) -> None:
    handlers, calls = _memory_handlers(tmp_path)

    symbol = handlers["write_memory"]("board_b", "counter", "0x12", 32, False, None)
    assert "mapped RAM" in symbol
    assert calls[-1][1][1:] == (0x20000010, 0x12, 32)  # type: ignore[index]

    calls.clear()
    guidance = handlers["write_memory"]("board_b", "0x20000010", 1, 32, False, None)
    assert "Try a symbol first" in guidance
    assert calls == []
    reason = handlers["write_memory"]("board_b", "0x20000010", 1, 32, True, " ")
    assert "address-fallback-reason-required" in reason
    assert calls == []
    raw = handlers["write_memory"](
        "board_b", "0x20000010", 1, 32, True, "pointer-derived allocation"
    )
    assert "mapped RAM" in raw
    assert calls[-1][1][1:] == (0x20000010, 1, 32)  # type: ignore[index]


def _flash_handlers(
    tmp_path: Path,
    *,
    prepare_symbol_artifact=None,
    bind_symbol_artifact=None,
    flash_target=None,
):
    artifact = tmp_path / "firmware.hex"
    artifact.write_text(":00000001FF\n", encoding="ascii")
    identity = FlashArtifactIdentity(
        path=artifact,
        suffix=".hex",
        size_bytes=artifact.stat().st_size,
        sha256="a" * 64,
        source="explicit",
    )
    request = ResolvedFlashRequest(artifact, identity)
    calls: list[tuple[str, object]] = []
    services = FlashToolServices(
        runtime_for=lambda board: None,
        active_session_id=lambda board: None,
        duration_ms=lambda started: 1,
        record_event=lambda *args, **kwargs: SimpleNamespace(),
        record_blocked_event=lambda *args, **kwargs: None,
        format_refusal=_format_refusal,
        format_block=lambda blocked, **kwargs: str(blocked),
        ensure_flash_allowed=lambda runtime: None,
        action_context=lambda action, board: ActionContext("server", action, None),
        maybe_handle_for=lambda board: object(),
        handle_for=lambda board: object(),
        resolve_request=lambda handle, selected, context: request,
        flash_target=flash_target
        or (lambda handle, selected: (calls.append(("flash", (handle, selected))) or selected)),
        handle_mutation_event=lambda board, event: None,
        error_code=lambda exc: "runtime/error",
        prepare_symbol_artifact=prepare_symbol_artifact,
        bind_symbol_artifact=bind_symbol_artifact,
    )
    return build_flash_handlers(services), calls, artifact


def test_split_flash_actions_report_safety_map_validation(tmp_path: Path) -> None:
    handlers, calls, artifact = _flash_handlers(tmp_path)

    for name in ("flash_application", "flash_bootloader"):
        result = handlers[name]("board_b", str(artifact))
        assert "mapped partition" in result
        assert SAFE_EXIT_REMINDER in result
    assert len(calls) == 2


def test_successful_application_flash_binds_symbols_but_bootloader_does_not(
    tmp_path: Path,
) -> None:
    order: list[tuple[str, object]] = []
    prepared = object()
    handlers, _calls, artifact = _flash_handlers(
        tmp_path,
        prepare_symbol_artifact=lambda tool, board, selected: (
            order.append(("prepare", (tool, board, selected))) or prepared
        ),
        bind_symbol_artifact=lambda board, binding: order.append(
            ("bind", (board, binding))
        ),
        flash_target=lambda handle, selected: (
            order.append(("flash", selected)) or selected
        ),
    )

    handlers["flash_bootloader"]("board_b", str(artifact))
    assert order == [("flash", artifact)]
    order.clear()
    handlers["flash_application"]("board_b", str(artifact))

    assert order == [
        ("prepare", ("flash_application", "board_b", artifact)),
        ("flash", artifact),
        ("bind", ("board_b", prepared)),
    ]


def test_run_scoped_symbol_binding_verifies_current_elf_digest(tmp_path: Path) -> None:
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"current-elf")
    handle = cast(
        TargetSessionHandle,
        SimpleNamespace(board=SimpleNamespace(board_id="board_b")),
    )
    server._current_symbol_artifacts.clear()
    try:
        binding = server._prepare_flashed_symbol_artifact(
            "flash_application", "board_b", elf
        )
        server._bind_flashed_symbol_artifact("board_b", binding)
        assert server._symbol_artifact_for_handle(handle) == elf.resolve()

        elf.write_bytes(b"changed")
        with pytest.raises(RuntimeError, match="changed after flash"):
            server._symbol_artifact_for_handle(handle)
    finally:
        server._current_symbol_artifacts.clear()


def test_hex_symbol_binding_is_prepared_from_same_stem_elf(tmp_path: Path) -> None:
    selected = tmp_path / "firmware.hex"
    companion = tmp_path / "firmware.elf"
    selected.write_text(":00000001FF\n", encoding="ascii")
    companion.write_bytes(b"companion")

    artifact, _digest = server._prepare_flashed_symbol_artifact(
        "flash_application", "board_b", selected
    )

    assert artifact == companion.resolve()


def test_convergence_watcher_tracks_renamed_bootloader_flash(tmp_path: Path) -> None:
    store = InMemorySessionStore(tmp_path / "runs")
    session = store.start_session(
        board_id="board_b",
        connection_id="probe:7",
        probe_uid="7",
        route_used="fake",
    )
    watcher = ConvergenceWatcher()
    decision = None
    for index in range(2):
        event = ToolEvent(
            event_id=f"event-{index}",
            session_id=session.session_id,
            timestamp=utc_now_text(),
            tool_name="flash_bootloader",
            board_id="board_b",
            probe_uid="7",
            route_used="fake",
            normalized_args={"artifact_sha256": "a" * 64},
            outcome_kind=ToolOutcome.FAILED,
            error_code="target/connection-failure",
            duration_ms=1,
        )
        store.append_event(session, event)
        decision = watcher.observe_event(session, event)

    assert decision is not None
    assert decision.action_family == FLASH_TOOL
    assert decision.code == "watch/flash-repetition"


@pytest.mark.asyncio
async def test_flash_bootloader_is_physically_locked_without_permission_plan() -> None:
    with pytest.raises(ToolError, match="flash_bootloader-plan") as caught:
        await server.mcp.call_tool(
            "flash_bootloader",
            {"board_id": "board_b", "artifact": "firmware.hex"},
        )
    assert SAFE_EXIT_REMINDER in str(caught.value)


def test_breakpoint_symbol_and_address_paths_are_wrapped(tmp_path: Path) -> None:
    artifact = tmp_path / "firmware.elf"
    artifact.write_bytes(b"elf")
    calls: list[tuple[str, int]] = []
    checked: list[tuple[str, int, Path]] = []
    handlers = build_breakpoint_handlers(
        BreakpointToolServices(
            runtime_for=lambda board: None,
            active_session_id=lambda board: None,
            duration_ms=lambda started: 1,
            record_event=lambda *args, **kwargs: None,
            format_refusal=_format_refusal,
            handle_for=lambda board: object(),
            resolve_symbol=lambda selected, name: ResolvedSymbol(name, 0x08000100, 4, "STT_FUNC"),
            set_target_breakpoint=lambda handle, address: calls.append(("set", address)),
            remove_target_breakpoint=lambda handle, address: calls.append(("remove", address)),
            check_breakpoint=lambda board, address, elf: checked.append((board, address, elf)),
        )
    )

    set_result = handlers["set_breakpoint"]("board_b", "main", str(artifact))
    remove_result = handlers["remove_breakpoint"]("board_b", "0x08000100")
    assert calls == [("set", 0x08000100), ("remove", 0x08000100)]
    assert checked == [("board_b", 0x08000100, artifact.resolve())]
    assert "executable space" in set_result
    assert SAFE_EXIT_REMINDER in set_result
    assert SAFE_EXIT_REMINDER in remove_result


@pytest.mark.parametrize("ms", [0, -1, 60_001, True])
def test_wait_rejects_out_of_bounds(ms: int) -> None:
    sleeps: list[float] = []
    handlers = build_misc_handlers(
        MiscToolServices(
            runtime_for=lambda board: None,
            duration_ms=lambda started: 1,
            record_event=lambda *args, **kwargs: None,
            sleep=sleeps.append,
        )
    )

    result = handlers["wait"]("board_b", ms)
    assert "wait/out-of-range" in result
    assert SAFE_EXIT_REMINDER in result
    assert sleeps == []


def test_wait_accepts_both_documented_boundaries() -> None:
    sleeps: list[float] = []
    handlers = build_misc_handlers(
        MiscToolServices(
            runtime_for=lambda board: None,
            duration_ms=lambda started: 1,
            record_event=lambda *args, **kwargs: None,
            sleep=sleeps.append,
        )
    )

    assert SAFE_EXIT_REMINDER in handlers["wait"]("board_b", 1)
    assert SAFE_EXIT_REMINDER in handlers["wait"]("board_b", 60_000)
    assert sleeps == [0.001, 60.0]


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("read_serial", {"read_seconds": 0.0}, "uart/invalid-read-seconds"),
        ("read_serial", {"read_seconds": 1.0, "baudrate": 0}, "uart/invalid-baudrate"),
        ("write_serial", {"text": "x", "timeout_seconds": 0.0}, "uart/invalid-timeout"),
        ("write_serial", {"text": "x", "baudrate": 0}, "uart/invalid-baudrate"),
    ],
)
def test_final_serial_handlers_preserve_bounds_and_wrap_failures(
    name: str,
    arguments: dict[str, object],
    code: str,
) -> None:
    services = SerialToolServices(
        runtime_for=lambda board: None,
        active_session_id=lambda board: None,
        duration_ms=lambda started: 1,
        record_event=lambda *args, **kwargs: None,
        record_blocked_event=lambda *args, **kwargs: None,
        format_refusal=_format_refusal,
        format_block=lambda blocked, **kwargs: str(blocked),
        ensure_uart_allowed=lambda runtime: None,
        handle_for=lambda board: pytest.fail("invalid serial call reached hardware"),
        resolve_port=lambda *args, **kwargs: None,
        capture_uart=lambda *args, **kwargs: None,
        write_uart=lambda *args, **kwargs: None,
        exchange_uart=lambda *args, **kwargs: None,
        reset_target=lambda handle: None,
        handle_mutation_event=lambda board, event: None,
        no_board_config_message="no board",
    )
    handler = build_serial_handlers(services)[name]

    result = handler("board_b", **arguments)
    assert code in result
    assert SAFE_EXIT_REMINDER in result
