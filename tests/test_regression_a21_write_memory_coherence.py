"""Regression tests for A21 coherent scalar-write blast-radius edges."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pyocd_debug_mcp import server
from pyocd_debug_mcp.services.session_runtime import ToolOutcome
from pyocd_debug_mcp.services.symbols import ResolvedSymbol
from pyocd_debug_mcp.tools.memory import (
    MemoryToolServices,
    _write_coherent_scalar,
    build_memory_handlers,
)


def test_coherent_write_requires_lifecycle_before_any_mutation() -> None:
    calls: list[str] = []
    services = SimpleNamespace(
        get_state=None,
        halt=None,
        resume=None,
        write_target_memory=lambda *_args: calls.append("write"),
        read_target_memory=lambda *_args: calls.append("read") or 1,
    )

    with pytest.raises(RuntimeError, match="lifecycle operations are not configured"):
        _write_coherent_scalar(services, object(), 0x20000000, 1, 32)

    assert calls == []


def test_verified_write_with_failed_restoration_never_records_success() -> None:
    calls: list[str] = []
    outcomes: list[ToolOutcome] = []
    handle = object()
    services = MemoryToolServices(
        runtime_for=lambda _board: None,
        active_session_id=lambda _board: None,
        duration_ms=lambda _started: 0,
        record_event=lambda *_args, **kwargs: outcomes.append(kwargs["outcome_kind"]),
        format_refusal=lambda refusal, **_kwargs: refusal.message,
        handle_for=lambda _board: handle,
        symbol_artifact_for=lambda _handle: Path("unused.elf"),
        find_symbols=lambda _artifact, _query: (),
        resolve_symbol=lambda _artifact, symbol: ResolvedSymbol(symbol, 0x20000000, 4, "STT_OBJECT"),
        read_target_memory=lambda _handle, address, width: calls.append(
            f"read:{address:#x}:{width}"
        )
        or 0x1234,
        read_target_block=lambda _handle, _address, _length: [],
        write_target_memory=lambda _handle, address, value, width: calls.append(
            f"write:{address:#x}:{value:#x}:{width}"
        ),
        check_memory_read=lambda _board, _address, _length: None,
        check_memory_write=lambda _board, _address, _width: None,
        get_state=lambda _handle: calls.append("state") or "RUNNING",
        halt=lambda _handle: calls.append("halt"),
        resume=lambda _handle: calls.append("resume")
        or (_ for _ in ()).throw(OSError("resume lost")),
    )

    with pytest.raises(OSError, match="resume lost"):
        build_memory_handlers(services)["write_memory"](
            "board", 0x20000000, 0x1234, 32, allow_address_fallback=True, reason="unsymbolized"
        )

    assert calls == [
        "state",
        "halt",
        "write:0x20000000:0x1234:32",
        "read:0x20000000:32",
        "resume",
    ]
    assert outcomes == []


def test_raw_read_keeps_its_lifecycle_independent_contract() -> None:
    calls: list[str] = []
    services = MemoryToolServices(
        runtime_for=lambda _board: None,
        active_session_id=lambda _board: None,
        duration_ms=lambda _started: 0,
        record_event=lambda *_args, **_kwargs: None,
        format_refusal=lambda refusal, **_kwargs: refusal.message,
        handle_for=lambda _board: object(),
        symbol_artifact_for=lambda _handle: Path("unused.elf"),
        find_symbols=lambda _artifact, _query: (),
        resolve_symbol=lambda _artifact, _symbol: None,  # type: ignore[return-value]
        read_target_memory=lambda _handle, address, width: calls.append(
            f"read:{address:#x}:{width}"
        )
        or 0xABCD,
        read_target_block=lambda _handle, _address, _length: [],
        write_target_memory=lambda _handle, _address, _value, _width: None,
        check_memory_read=lambda _board, _address, _length: None,
    )

    result = build_memory_handlers(services)["read_memory_address"]("board", 0x20000000, 32)

    assert "0x0000ABCD" in result
    assert calls == ["read:0x20000000:32"]


def test_registered_fastmcp_help_teaches_the_complete_write_contract() -> None:
    """Discovery must expose the complete public contract, not only handler details."""

    description = server.mcp._tool_manager.get_tool("write_memory").description.lower()

    for phrase in (
        "board_id",
        "symbol_or_address",
        "allow_address_fallback=true",
        "write_memory(",
        "returns the normal layer-2 response",
        "wrote 0x",
        "invalid widths or values",
        "missing raw-fallback justification",
        "unmapped or prohibited memory",
        "inspect or reconnect",
        "deliberately halted",
    ):
        assert phrase in description
