"""Vendor-neutral serial-port evidence and selection.

Serial association is only claimed from observed metadata, a caller-selected
port, or a single-port fallback.  Probe and UART vendors are not an authority
for one another: an unfamiliar adapter remains usable without source changes.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Callable, Protocol


class BoardLike(Protocol):
    board_id: str
    display_name: str
    serial_hint_terms: tuple[str, ...]


class ProbeLike(Protocol):
    uid: str | None


@dataclass(frozen=True)
class SerialPortInfo:
    device: str
    description: str = ""
    manufacturer: str = ""
    product: str = ""
    interface: str = ""
    hwid: str = ""
    serial_number: str = ""
    location: str = ""
    vid: int | None = None
    pid: int | None = None

    @property
    def searchable_text(self) -> str:
        return " ".join(
            value
            for value in (
                self.device,
                self.description,
                self.manufacturer,
                self.product,
                self.interface,
                self.hwid,
                self.serial_number,
                self.location,
            )
            if value
        ).casefold()


@dataclass(frozen=True)
class SerialResolution:
    port: SerialPortInfo | None
    note: str


RunCommand = Callable[[list[str]], tuple[int, str, str]]


def list_serial_ports() -> list[SerialPortInfo] | None:
    try:
        from serial.tools import list_ports  # type: ignore[import-untyped]
    except ImportError:
        return None
    return [
        SerialPortInfo(
            device=port.device,
            description=port.description or "",
            manufacturer=getattr(port, "manufacturer", "") or "",
            product=getattr(port, "product", "") or "",
            interface=getattr(port, "interface", "") or "",
            hwid=port.hwid or "",
            serial_number=getattr(port, "serial_number", "") or "",
            location=getattr(port, "location", "") or "",
            vid=getattr(port, "vid", None),
            pid=getattr(port, "pid", None),
        )
        for port in list_ports.comports()
    ]


def is_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def normalize_port_name(port: str) -> str:
    value = port.strip()
    return (value[4:] if value.startswith("\\\\.\\") else value).casefold()


def _normalised(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", value.casefold())


def probe_uid_matches_serial(uid: str, serial: str) -> bool:
    """Match only observed UID/serial metadata, including harmless formatting."""

    left, right = _normalised(uid), _normalised(serial)
    return bool(left and right and (left == right or left.endswith(right) or right.endswith(left)))


def _find_port(ports: list[SerialPortInfo], name: str) -> SerialPortInfo | None:
    wanted = normalize_port_name(name)
    return next((port for port in ports if normalize_port_name(port.device) == wanted), None)


def _candidate_text(candidates: list[SerialPortInfo]) -> str:
    return "\n".join(
        f"      {index}. {port.device} :: {port.description or '(no description)'}"
        for index, port in enumerate(candidates, start=1)
    )


def _guidance(board: BoardLike, candidates: list[SerialPortInfo]) -> str:
    suffix = f"; candidate ports:\n{_candidate_text(candidates)}" if candidates else ""
    return f"select one explicitly with port for {board.board_id}{suffix}"


def _prompt(board: BoardLike, candidates: list[SerialPortInfo]) -> SerialResolution:
    print(f"Multiple serial ports remain for {board.display_name}:\n{_candidate_text(candidates)}")
    try:
        choice = input("Selection: ").strip()
    except EOFError:
        choice = ""
    if not choice.isdigit() or not 1 <= int(choice) <= len(candidates):
        return SerialResolution(None, _guidance(board, candidates))
    selected = candidates[int(choice) - 1]
    return SerialResolution(selected, f"caller selected {selected.device}")


def _candidates(
    board: BoardLike, ports: list[SerialPortInfo], probe: ProbeLike | None
) -> list[SerialPortInfo]:
    """Return equally best metadata-backed candidates; never infer a vendor route."""

    scored: list[tuple[int, SerialPortInfo]] = []
    for port in ports:
        score = sum(term.casefold() in port.searchable_text for term in board.serial_hint_terms)
        if (
            probe is not None
            and probe.uid
            and probe_uid_matches_serial(probe.uid, port.serial_number)
        ):
            score += 100
        if score:
            scored.append((score, port))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [port for score, port in scored if score == best]


def resolve_serial_port(
    board: BoardLike,
    ports: list[SerialPortInfo],
    probe: ProbeLike | None,
    override: str | None,
    allow_single_fallback: bool,
    run_cmd: RunCommand,
    interactive: bool,
) -> SerialResolution:
    """Resolve a port from caller choice or observable serial metadata.

    ``run_cmd`` remains an adapter argument for the existing call surface; no
    provider-specific external parser is invoked by this generic resolver.
    """

    del run_cmd
    if override:
        found = _find_port(ports, override)
        return SerialResolution(
            found,
            "caller selected serial port" if found else f"override port {override!r} not found",
        )
    candidates = _candidates(board, ports, probe)
    if len(candidates) == 1:
        candidate = candidates[0]
        if (
            probe is not None
            and probe.uid
            and probe_uid_matches_serial(probe.uid, candidate.serial_number)
        ):
            return SerialResolution(candidate, "resolved from exact probe UID and serial metadata")
        return SerialResolution(candidate, "resolved from caller-supplied serial hint metadata")
    if allow_single_fallback and len(ports) == 1:
        return SerialResolution(
            ports[0], "single connected serial port selected without probe mapping"
        )
    options = candidates or ports
    if len(options) > 1 and interactive:
        return _prompt(board, options)
    if options:
        return SerialResolution(None, _guidance(board, options))
    return SerialResolution(None, "no serial ports detected")
