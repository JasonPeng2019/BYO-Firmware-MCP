from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.services import symbols
from pyocd_debug_mcp.target_errors import SymbolLookupError


class FakeSymbol:
    def __init__(self, name: str, address: int, size: int, type_name: str) -> None:
        self.name = name
        self.address = address
        self.size = size
        self.type = type_name


class FakeDecoder:
    def __init__(self, symbol: FakeSymbol | None) -> None:
        self._symbol = symbol

    def get_symbol_for_name(self, name: str) -> FakeSymbol | None:
        if self._symbol is not None and self._symbol.name == name:
            return self._symbol
        return None


class FakeELF:
    def __init__(self, path: str, symbol: FakeSymbol | None) -> None:
        self.path = path
        self.symbol_decoder = FakeDecoder(symbol)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_resolve_symbol_returns_backend_neutral_shape(monkeypatch, tmp_path: Path) -> None:
    elf_path = tmp_path / "firmware.elf"
    elf_path.write_text("placeholder", encoding="utf-8")
    fake_symbol = FakeSymbol("stage1_known_value", 0x20000010, 4, "STT_OBJECT")
    captured: dict[str, FakeELF] = {}

    def fake_loader(path: str) -> FakeELF:
        elf = FakeELF(path, fake_symbol)
        captured["elf"] = elf
        return elf

    monkeypatch.setattr(symbols, "ELFBinaryFile", fake_loader)

    resolved = symbols.resolve_symbol(elf_path, "stage1_known_value")

    assert resolved.name == "stage1_known_value"
    assert resolved.address == 0x20000010
    assert resolved.size == 4
    assert resolved.type == "STT_OBJECT"
    assert resolved.value_u32 is None
    assert captured["elf"].closed is True


def test_resolve_symbol_raises_for_missing_symbol(monkeypatch, tmp_path: Path) -> None:
    elf_path = tmp_path / "firmware.elf"
    elf_path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(symbols, "ELFBinaryFile", lambda path: FakeELF(path, None))

    with pytest.raises(SymbolLookupError, match="missing_symbol"):
        symbols.resolve_symbol(elf_path, "missing_symbol")


def test_read_symbol_u32_halts_and_restores_running_target(monkeypatch, tmp_path: Path) -> None:
    elf_path = tmp_path / "firmware.elf"
    elf_path.write_text("placeholder", encoding="utf-8")
    handle = cast(TargetSessionHandle, object())
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        symbols,
        "resolve_symbol",
        lambda path, name: symbols.ResolvedSymbol(
            name="stage1_known_value",
            address=0x08003EC8,
            size=4,
            type="STT_OBJECT",
        ),
    )
    monkeypatch.setattr(symbols.target_control, "get_state", lambda handle_arg: "RUNNING")
    monkeypatch.setattr(
        symbols.target_control,
        "halt",
        lambda handle_arg: calls.append(("halt", handle_arg)),
    )
    monkeypatch.setattr(
        symbols.target_control,
        "read_memory",
        lambda handle_arg, address, width_bits: (
            calls.append(("read_memory", handle_arg)),
            0x1234ABCD,
        )[1],
    )
    monkeypatch.setattr(
        symbols.target_control,
        "resume",
        lambda handle_arg: calls.append(("resume", handle_arg)),
    )

    resolved = symbols.read_symbol_u32(handle, elf_path, "stage1_known_value")

    assert resolved.value_u32 == 0x1234ABCD
    assert calls == [("halt", handle), ("read_memory", handle), ("resume", handle)]


def test_read_symbol_u32_leaves_halted_target_halted(monkeypatch, tmp_path: Path) -> None:
    elf_path = tmp_path / "firmware.elf"
    elf_path.write_text("placeholder", encoding="utf-8")
    handle = cast(TargetSessionHandle, object())
    calls: list[str] = []

    monkeypatch.setattr(
        symbols,
        "resolve_symbol",
        lambda path, name: symbols.ResolvedSymbol(
            name="stage1_known_value",
            address=0x08003EC8,
            size=4,
            type="STT_OBJECT",
        ),
    )
    monkeypatch.setattr(symbols.target_control, "get_state", lambda handle_arg: "HALTED")
    monkeypatch.setattr(
        symbols.target_control,
        "read_memory",
        lambda handle_arg, address, width_bits: (
            calls.append("read_memory"),
            0x1234ABCD,
        )[1],
    )
    monkeypatch.setattr(
        symbols.target_control,
        "halt",
        lambda handle_arg: calls.append("halt"),
    )
    monkeypatch.setattr(
        symbols.target_control,
        "resume",
        lambda handle_arg: calls.append("resume"),
    )

    resolved = symbols.read_symbol_u32(handle, elf_path, "stage1_known_value")

    assert resolved.value_u32 == 0x1234ABCD
    assert calls == ["read_memory"]
