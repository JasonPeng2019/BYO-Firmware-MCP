"""Shared ELF symbol-resolution helpers for Stage 1 harnesses and later tools."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pyocd.debug.elf.elf import ELFBinaryFile  # type: ignore[import-untyped]

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.target_errors import SymbolLookupError


@dataclass(frozen=True)
class ResolvedSymbol:
    name: str
    address: int
    size: int
    type: str
    value_u32: int | None = None


def _normalize_elf_path(elf_path: Path | str) -> Path:
    path = Path(elf_path).expanduser().resolve()
    if not path.exists():
        raise SymbolLookupError(f"ELF artifact does not exist: {path}")
    return path


def resolve_symbol(elf_path: Path | str, name: str) -> ResolvedSymbol:
    path = _normalize_elf_path(elf_path)
    elf = ELFBinaryFile(str(path))
    try:
        symbol: Any = elf.symbol_decoder.get_symbol_for_name(name)
    finally:
        elf.close()

    if symbol is None:
        raise SymbolLookupError(f"Symbol '{name}' was not found in {path}")

    return ResolvedSymbol(
        name=str(symbol.name),
        address=int(symbol.address),
        size=int(symbol.size),
        type=str(symbol.type),
    )


def read_symbol_u32(
    handle: TargetSessionHandle,
    elf_path: Path | str,
    name: str,
) -> ResolvedSymbol:
    resolved = resolve_symbol(elf_path, name)
    prior_state = target_control.get_state(handle).upper()
    should_resume = prior_state != "HALTED"
    if should_resume:
        target_control.halt(handle)
    try:
        value = target_control.read_memory(handle, resolved.address, 32)
    finally:
        if should_resume:
            target_control.resume(handle)
    return replace(resolved, value_u32=value)
