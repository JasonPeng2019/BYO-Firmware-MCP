"""Provider-neutral symbol resolution with ELF as the bundled implementation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol

from pyocd.debug.elf.elf import ELFBinaryFile  # type: ignore[import-untyped]

from pyocd_debug_mcp.adapters.target_backend import TargetSessionHandle
from pyocd_debug_mcp.artifact_formats import FirmwareFormat, detect_firmware_format
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.target_errors import SymbolLookupError

ENTRY_POINT_GROUP = "pyocd_debug_mcp.symbol_providers"


@dataclass(frozen=True)
class ResolvedSymbol:
    name: str
    address: int
    size: int
    type: str
    value_u32: int | None = None


class SymbolProvider(Protocol):
    """Resolve symbols from one toolchain's content-addressed artifact format."""

    name: str

    def supports(self, path: Path) -> bool: ...
    def resolve(self, path: Path, name: str) -> ResolvedSymbol: ...
    def find(self, path: Path, query: str, limit: int) -> tuple[ResolvedSymbol, ...]: ...


def _existing_path(path: Path | str) -> Path:
    selected = Path(path).expanduser().resolve()
    if not selected.is_file():
        raise SymbolLookupError(f"Symbol artifact does not exist: {selected}")
    return selected


class ElfSymbolProvider:
    name = "elf"

    def supports(self, path: Path) -> bool:
        return detect_firmware_format(path) is FirmwareFormat.ELF

    def resolve(self, path: Path, name: str) -> ResolvedSymbol:
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

    def find(self, path: Path, query: str, limit: int) -> tuple[ResolvedSymbol, ...]:
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
                if query in str(symbol.name).casefold()
            ]
        finally:
            elf.close()
        matches.sort(key=lambda item: (item.name.casefold(), item.address, item.name))
        return tuple(matches[:limit])


def symbol_providers() -> tuple[SymbolProvider, ...]:
    providers: list[SymbolProvider] = [ElfSymbolProvider()]
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        loaded = entry.load()
        provider = loaded() if isinstance(loaded, type) else loaded
        if not all(callable(getattr(provider, name, None)) for name in ("supports", "resolve", "find")):
            raise RuntimeError(f"symbol provider {entry.name!r} is invalid")
        providers.append(provider)
    return tuple(providers)


def _provider(path: Path) -> SymbolProvider:
    for provider in symbol_providers():
        if provider.supports(path):
            return provider
    raise SymbolLookupError(
        f"No installed symbol provider recognizes {path}; install the toolchain's "
        f"'{ENTRY_POINT_GROUP}' provider or select a supported debug artifact."
    )


def resolve_symbol(artifact_path: Path | str, name: str) -> ResolvedSymbol:
    path = _existing_path(artifact_path)
    return _provider(path).resolve(path, name)


def find_symbols(
    artifact_path: Path | str,
    query: str,
    *,
    limit: int = 20,
) -> tuple[ResolvedSymbol, ...]:
    """Return a deterministic bounded case-insensitive symbol search."""

    normalized_query = query.strip().casefold()
    if not normalized_query:
        raise SymbolLookupError("Symbol query must not be empty.")
    if limit < 1:
        raise ValueError("limit must be positive")
    path = _existing_path(artifact_path)
    return _provider(path).find(path, normalized_query, limit)


def read_symbol_u32(
    handle: TargetSessionHandle,
    artifact_path: Path | str,
    name: str,
) -> ResolvedSymbol:
    resolved = resolve_symbol(artifact_path, name)
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
