from __future__ import annotations

from pathlib import Path

from pyocd_debug_mcp.services.symbols import ResolvedSymbol
from pyocd_debug_mcp.tools.breakpoints import (
    BreakpointToolServices,
    build_breakpoint_handlers,
)


def _services(
    calls: list[tuple[object, ...]],
    checked: list[tuple[str, int, Path]],
) -> BreakpointToolServices:
    return BreakpointToolServices(
        runtime_for=lambda _board: None,
        active_session_id=lambda _board: None,
        duration_ms=lambda _started: 1,
        record_event=lambda *args, **kwargs: None,
        format_refusal=lambda refusal, **kwargs: str(refusal),
        handle_for=lambda board: ("handle", board),
        resolve_symbol=lambda elf, symbol: (
            calls.append(("resolve", elf, symbol))
            or ResolvedSymbol(symbol, 0x08000100, 4, "STT_FUNC")
        ),
        set_target_breakpoint=lambda handle, address: calls.append(("set", handle, address)),
        remove_target_breakpoint=lambda handle, address: calls.append(("remove", handle, address)),
        check_breakpoint=lambda board, address, elf: checked.append((board, address, elf)),
    )


def test_set_breakpoint_uses_plan_selected_elf_for_resolution_and_containment(
    tmp_path: Path,
) -> None:
    elf = tmp_path / "current.elf"
    elf.write_bytes(b"current ELF")
    calls: list[tuple[object, ...]] = []
    checked: list[tuple[str, int, Path]] = []
    handler = build_breakpoint_handlers(_services(calls, checked))["set_breakpoint"]

    result = handler("board_a", "main", str(elf))

    assert calls == [
        ("resolve", elf.resolve(), "main"),
        ("set", ("handle", "board_a"), 0x08000100),
    ]
    assert checked == [("board_a", 0x08000100, elf.resolve())]
    assert "Breakpoint set" in result


def test_set_breakpoint_checks_explicit_address_against_selected_elf(tmp_path: Path) -> None:
    elf = tmp_path / "current.elf"
    elf.write_bytes(b"current ELF")
    calls: list[tuple[object, ...]] = []
    checked: list[tuple[str, int, Path]] = []
    handler = build_breakpoint_handlers(_services(calls, checked))["set_breakpoint"]

    handler("board_a", "0x08000200", str(elf))

    assert checked == [("board_a", 0x08000200, elf.resolve())]
    assert calls == [("set", ("handle", "board_a"), 0x08000200)]


def test_set_breakpoint_rejects_non_elf_before_backend(tmp_path: Path) -> None:
    not_elf = tmp_path / "current.hex"
    not_elf.write_text(":00000001FF", encoding="ascii")
    calls: list[tuple[object, ...]] = []
    checked: list[tuple[str, int, Path]] = []
    handler = build_breakpoint_handlers(_services(calls, checked))["set_breakpoint"]

    result = handler("board_a", "main", str(not_elf))

    assert "elf_artifact must name the current local ELF file" in result
    assert calls == []
    assert checked == []
