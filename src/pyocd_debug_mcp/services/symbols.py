"""Shared ELF symbol-resolution helpers for server tools."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.project_paths import resolve_project_path
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
    path = resolve_project_path(elf_path)
    if not path.exists():
        raise SymbolLookupError(f"ELF artifact does not exist: {path}")
    return path


def _defined_symbols(path: Path) -> tuple[ResolvedSymbol, ...]:
    # pyOCD's name-keyed decoder has already discarded duplicate local names.
    # Read the original tables; table membership is not definition identity.
    definitions: dict[tuple[ResolvedSymbol, int | str, str], ResolvedSymbol] = {}
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        for table in elf.iter_sections():
            if not isinstance(table, SymbolTableSection):
                continue
            for symbol in table.iter_symbols():
                symbol_type = symbol["st_info"]["type"]
                if symbol_type not in {"STT_FUNC", "STT_OBJECT"}:
                    continue
                section = symbol["st_shndx"]
                # Imports have no definition; COMMON values are alignment, not addresses.
                if section in {"SHN_UNDEF", "SHN_COMMON"}:
                    continue
                resolved = ResolvedSymbol(
                    name=symbol.name,
                    address=int(symbol["st_value"]),
                    size=int(symbol["st_size"]),
                    type=symbol_type,
                )
                # Keep aliases (different names) and distinct sections/bindings.
                # Identical symtab/dynsym representations describe one definition.
                definitions[(resolved, section, symbol["st_info"]["bind"])] = resolved
    return tuple(
        sorted(
            definitions.values(),
            key=lambda item: (item.name.casefold(), item.address, item.name, item.size, item.type),
        )
    )


def resolve_symbol(elf_path: Path | str, name: str) -> ResolvedSymbol:
    """Resolve one exact name, refusing ambiguity before any target operation."""

    path = _normalize_elf_path(elf_path)
    matches = [symbol for symbol in _defined_symbols(path) if symbol.name == name]
    if not matches:
        raise SymbolLookupError(f"Symbol '{name}' was not found in {path}")
    if len(matches) > 1:
        candidates = ", ".join(
            f"0x{symbol.address:08X} size={symbol.size} type={symbol.type}" for symbol in matches
        )
        raise SymbolLookupError(
            f"Symbol '{name}' is ambiguous in {path}: {candidates}. "
            "Use find_symbol to inspect the candidates, then use an explicit address with "
            "the address-based memory or breakpoint operation and its required safety plan."
        )
    return matches[0]


def find_symbols(
    elf_path: Path | str,
    query: str,
) -> tuple[ResolvedSymbol, ...]:
    """Return every deterministic case-insensitive ELF symbol match."""

    normalized_query = query.strip().casefold()
    if not normalized_query:
        raise SymbolLookupError("Symbol query must not be empty.")
    path = _normalize_elf_path(elf_path)
    return tuple(
        symbol for symbol in _defined_symbols(path) if normalized_query in symbol.name.casefold()
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
