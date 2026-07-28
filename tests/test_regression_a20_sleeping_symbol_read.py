"""Regression coverage for A20 lifecycle dependency and raw-read isolation."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyocd_debug_mcp import server
from pyocd_debug_mcp.tools.memory import (
    MemoryToolServices,
    _read_coherent_scalar,
    build_memory_handlers,
)


def test_scalar_read_without_lifecycle_dependencies_fails_before_target_io() -> None:
    calls: list[str] = []
    services = SimpleNamespace(
        read_target_memory=lambda *_args: calls.append("read") or 0,
        get_state=None,
        halt=None,
        resume=None,
    )

    with pytest.raises(RuntimeError, match="lifecycle operations are not configured"):
        _read_coherent_scalar(services, object(), 0x20000000, 32)

    assert calls == []


def test_raw_address_read_remains_compatible_without_lifecycle_dependencies() -> None:
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


def test_registered_symbol_read_keeps_its_public_signature() -> None:
    signature = inspect.signature(server.memory_tool_handlers["read_memory_symbol"])
    parameters = list(signature.parameters.values())

    assert [parameter.name for parameter in parameters] == [
        "board_id",
        "symbol",
        "width",
        "elf_artifact",
    ]
    assert parameters[0].default is inspect.Parameter.empty
    assert parameters[1].default is inspect.Parameter.empty
    assert parameters[2].default == 32
    assert parameters[3].default is None
