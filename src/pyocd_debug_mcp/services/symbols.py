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


def is_elf_artifact(path: Path) -> bool:
    """Recognize ELF by its format marker, independent of toolchain suffix."""

    try:
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError:
        return False


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


def find_symbols(
    elf_path: Path | str,
    query: str,
    *,
    limit: int = 20,
) -> tuple[ResolvedSymbol, ...]:
    """Return a deterministic bounded case-insensitive ELF symbol search."""

    normalized_query = query.strip().casefold()
    if not normalized_query:
        raise SymbolLookupError("Symbol query must not be empty.")
    if limit < 1:
        raise ValueError("limit must be positive")
    path = _normalize_elf_path(elf_path)
    elf = ELFBinaryFile(str(path))
    try:
        matches = [
            ResolvedSymbol(
                name=str(symbol.name),
                address=int(symbol.address),
                size=int(symbol.size),
                type=str(symbol.type),
            )
            for symbol in elf.symbol_decoder.symbol_dict.values()
            if normalized_query in str(symbol.name).casefold()
        ]
    finally:
        elf.close()
    matches.sort(key=lambda item: (item.name.casefold(), item.address, item.name))
    return tuple(matches[:limit])


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
