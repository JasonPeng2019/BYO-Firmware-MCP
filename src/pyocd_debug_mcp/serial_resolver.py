from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class BoardLike(Protocol):
    @property
    def board_id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    @property
    def mcu_family(self) -> str: ...

    @property
    def probe_family(self) -> str: ...

    @property
    def serial_hint_terms(self) -> tuple[str, ...]: ...


class ProbeLike(Protocol):
    @property
    def uid(self) -> str: ...


@dataclass(frozen=True)
class SerialPortInfo:
    device: str
    description: str
    manufacturer: str
    product: str
    interface: str
    hwid: str
    serial_number: str = ""
    location: str = ""
    vid: int | None = None
    pid: int | None = None

    @property
    def searchable_text(self) -> str:
        return " ".join(
            [
                self.device,
                self.description,
                self.manufacturer,
                self.product,
                self.interface,
                self.hwid,
                self.serial_number,
                self.location,
                "" if self.vid is None else f"{self.vid:04x}",
                "" if self.pid is None else f"{self.pid:04x}",
            ]
        ).lower()


@dataclass(frozen=True)
class SerialResolution:
    port: SerialPortInfo | None
    note: str


@dataclass(frozen=True)
class NordicComEntry:
    probe_serial: str
    port: str
    label: str


@dataclass(frozen=True)
class StlinkComEntry:
    probe_serial: str
    port: str
    location: str
    description: str
    manufacturer: str


RunCommand = Callable[[list[str]], tuple[int, str, str]]


# External USB-to-serial bridge chips commonly used to expose a custom PCB's
# UART when the board has no onboard virtual COM port (the decoupled case: an
# external debug probe plus a separate USB-serial adapter). These signatures let
# discovery surface such an adapter even though it shares no probe serial and no
# board-name hint with the board. Onboard dev-board VCPs (J-Link VID 0x1366,
# ST-Link VID 0x0483) are deliberately NOT here — those are handled by the probe
# link and the vendor-helper paths.
USB_SERIAL_BRIDGE_VIDS: frozenset[int] = frozenset(
    {
        0x0403,  # VENDOR-FIXED (FTDI)
        0x10C4,  # VENDOR-FIXED (Silicon Labs CP210x)
        0x1A86,  # VENDOR-FIXED (WCH CH340/CH341/CH9102)
        0x067B,  # VENDOR-FIXED (Prolific PL2303)
    }
)
USB_SERIAL_BRIDGE_HINT_TERMS: tuple[str, ...] = (
    "ftdi",
    "ft232",
    "ft231",
    "cp210",
    "cp2102",
    "cp2104",
    "cp2105",
    "silicon labs",
    "silabs",
    "ch340",
    "ch341",
    "ch9102",
    "pl2303",
    "prolific",
    "usb-serial",
    "usb serial",
    "usb uart",
    "usb-uart",
)


@dataclass(frozen=True, slots=True)
class SerialFallbackSpec:
    provider_id: str
    probe_families: tuple[str, ...]
    executable: str
    executable_env: str
    argv: tuple[str, ...]
    parser: str
    preferred_label: str | None = None


_SUPPORTED_FALLBACK_PARSERS = frozenset({"nrfjprog_com", "stm32_programmer_list"})


def _load_serial_fallbacks(path: Path) -> tuple[SerialFallbackSpec, ...]:
    """Load an optional operator-supplied helper registry strictly."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Serial fallback registry is unreadable: {path}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise RuntimeError("Serial fallback registry must use schema_version 1")
    rows = document.get("providers")
    if not isinstance(rows, list):
        raise RuntimeError("Serial fallback registry providers must be a list")
    specs: list[SerialFallbackSpec] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Each serial fallback provider must be an object")
        required = ("provider_id", "executable", "executable_env", "parser")
        if not all(isinstance(row.get(name), str) and row[name] for name in required):
            raise RuntimeError("Serial fallback string fields must be non-empty")
        provider_id = str(row["provider_id"])
        parser = str(row["parser"])
        families = row.get("probe_families")
        argv = row.get("argv")
        if provider_id in seen:
            raise RuntimeError(f"Duplicate serial fallback provider: {provider_id}")
        if parser not in _SUPPORTED_FALLBACK_PARSERS:
            raise RuntimeError(f"Unsupported serial fallback parser: {parser}")
        if (
            not isinstance(families, list)
            or not families
            or not all(isinstance(item, str) and item for item in families)
        ):
            raise RuntimeError("Serial fallback probe_families must be a non-empty string list")
        if not isinstance(argv, list) or not all(isinstance(item, str) and item for item in argv):
            raise RuntimeError("Serial fallback argv must be a string list")
        preferred = row.get("preferred_label")
        if preferred is not None and (not isinstance(preferred, str) or not preferred):
            raise RuntimeError("Serial fallback preferred_label must be a non-empty string")
        seen.add(provider_id)
        specs.append(
            SerialFallbackSpec(
                provider_id,
                tuple(item.casefold() for item in families),
                str(row["executable"]),
                str(row["executable_env"]),
                tuple(argv),
                parser,
                preferred,
            )
        )
    return tuple(specs)


_configured_fallback_registry = os.environ.get("PYOCD_SERIAL_FALLBACK_REGISTRY", "").strip()
SERIAL_FALLBACKS = (
    _load_serial_fallbacks(Path(_configured_fallback_registry).expanduser().resolve())
    if _configured_fallback_registry
    else ()
)


def resolve_command_path(name: str, env_var: str | None = None) -> str | None:
    if env_var:
        configured = os.environ.get(env_var, "").strip()
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_file():
                return str(candidate.resolve())
    resolved = shutil.which(name)
    return resolved or None


def command_exists(name: str) -> bool:
    return resolve_command_path(name) is not None


def list_serial_ports() -> list[SerialPortInfo] | None:
    try:
        from serial.tools import list_ports  # type: ignore[import-untyped]
    except ImportError:
        return None

    ports: list[SerialPortInfo] = []
    for port in list_ports.comports():
        ports.append(
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
        )
    return ports


def is_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def normalize_port_name(port: str) -> str:
    normalized = port.strip()
    if normalized.startswith("\\\\.\\"):
        normalized = normalized[4:]
    return normalized.lower()


def _normalized_probe_uid(uid: str) -> str:
    return re.sub(r"[^0-9a-z]", "", uid.lower())


def _normalized_serial(serial: str) -> str:
    return re.sub(r"[^0-9a-z]", "", serial.lower())


def _numeric_serial_variants(value: str) -> set[str]:
    normalized = _normalized_serial(value)
    variants = {normalized}
    digits = re.sub(r"\D", "", normalized)
    if digits:
        variants.add(digits)
        variants.add(digits.lstrip("0") or "0")
    return {item for item in variants if item}


def probe_uid_matches_serial(uid: str, serial: str) -> bool:
    if not uid or not serial:
        return False

    uid_variants = _numeric_serial_variants(uid)
    serial_variants = _numeric_serial_variants(serial)
    if uid_variants & serial_variants:
        return True

    normalized_uid = _normalized_probe_uid(uid)
    normalized_serial = _normalized_serial(serial)
    return normalized_uid.endswith(normalized_serial) or normalized_serial.endswith(normalized_uid)


def score_terms(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for term in terms if term in text)


def looks_like_usb_serial_bridge(port: SerialPortInfo) -> bool:
    """True when a port looks like a standalone USB-to-serial bridge chip.

    Matches on the known external-adapter USB vendor IDs first (most reliable),
    then on chip/description hints in the port's searchable text. Used to surface
    an external UART adapter on a custom PCB whose serial path is decoupled from
    the debug probe.
    """
    if port.vid is not None and port.vid in USB_SERIAL_BRIDGE_VIDS:
        return True
    text = port.searchable_text
    return any(term in text for term in USB_SERIAL_BRIDGE_HINT_TERMS)


def parse_nrfjprog_com_output(text: str) -> list[NordicComEntry]:
    entries: list[NordicComEntry] = []
    pattern = re.compile(
        r"^(?P<serial>\S+)\s+(?P<port>\S+)\s+(?P<label>VCOM\d+)\s*$", re.IGNORECASE
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = pattern.match(line)
        if not match:
            continue
        entries.append(
            NordicComEntry(
                probe_serial=match.group("serial"),
                port=match.group("port"),
                label=match.group("label").upper(),
            )
        )
    return entries


def parse_stm32_programmer_list_output(text: str) -> list[StlinkComEntry]:
    entries: list[StlinkComEntry] = []
    in_uart_section = False
    current: dict[str, str] = {}
    known_keys = {"ST-LINK SN", "Port", "Location", "Description", "Manufacturer"}

    def flush() -> None:
        if current.get("ST-LINK SN") and current.get("Port"):
            entries.append(
                StlinkComEntry(
                    probe_serial=current["ST-LINK SN"],
                    port=current["Port"],
                    location=current.get("Location", ""),
                    description=current.get("Description", ""),
                    manufacturer=current.get("Manufacturer", ""),
                )
            )
        current.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("====="):
            if in_uart_section:
                flush()
            in_uart_section = line.lower().startswith("===== uart interface")
            continue
        if not in_uart_section or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key not in known_keys:
            continue
        if key == "ST-LINK SN" and current.get("ST-LINK SN") and current.get("Port"):
            flush()
        current[key] = value

    if in_uart_section:
        flush()

    return entries


def _find_port_by_name(ports: list[SerialPortInfo], port_name: str) -> SerialPortInfo | None:
    normalized = normalize_port_name(port_name)
    for port in ports:
        if normalize_port_name(port.device) == normalized:
            return port
    return None


def _candidate_list_text(candidates: list[SerialPortInfo]) -> str:
    lines = []
    for idx, port in enumerate(candidates, start=1):
        detail = port.description or "(no description)"
        extras = [part for part in [port.manufacturer, port.serial_number, port.location] if part]
        if extras:
            detail = f"{detail} :: {' :: '.join(extras)}"
        lines.append(f"      {idx}. {port.device} :: {detail}")
    return "\n".join(lines)


def _manual_rerun_guidance(board: BoardLike, candidates: list[SerialPortInfo]) -> str:
    guidance = f"use --port {board.board_id}=PORT"
    if candidates:
        guidance += f"; candidate ports:\n{_candidate_list_text(candidates)}"
    return guidance


def _prompt_for_candidate(board: BoardLike, candidates: list[SerialPortInfo]) -> SerialResolution:
    print(f"  Multiple candidate serial ports remain for {board.display_name}:")
    print(_candidate_list_text(candidates))
    print(f"  Choose a port for {board.board_id} or press Enter to abort.")
    try:
        response = input("  Selection: ").strip()
    except EOFError:
        response = ""

    if not response:
        return SerialResolution(
            None, f"selection aborted; {_manual_rerun_guidance(board, candidates)}"
        )
    if not response.isdigit():
        return SerialResolution(
            None, f"invalid selection '{response}'; {_manual_rerun_guidance(board, candidates)}"
        )

    selected_index = int(response)
    if not 1 <= selected_index <= len(candidates):
        return SerialResolution(
            None,
            f"selection {selected_index} is out of range; {_manual_rerun_guidance(board, candidates)}",
        )

    selected = candidates[selected_index - 1]
    return SerialResolution(selected, f"user selected {selected.device}")


def _conflicts_with_probe_family(board: BoardLike, port: SerialPortInfo) -> bool:
    family = board.probe_family.lower()
    text = port.searchable_text
    identifies_jlink = any(marker in text for marker in ("j-link", "jlink", "segger"))
    identifies_stlink = any(
        marker in text for marker in ("st-link", "stlink", "stmicroelectronics")
    )
    if family == "jlink":
        return identifies_stlink and not identifies_jlink
    if family == "stlink":
        return identifies_jlink and not identifies_stlink
    return False


def _generic_candidates(
    board: BoardLike, ports: list[SerialPortInfo], probe: ProbeLike | None
) -> list[SerialPortInfo]:
    scored: list[tuple[int, SerialPortInfo]] = []
    for port in ports:
        if _conflicts_with_probe_family(board, port):
            continue
        score = score_terms(port.searchable_text, board.serial_hint_terms)
        if probe and probe.uid:
            if probe_uid_matches_serial(probe.uid, port.serial_number):
                score += 100
            elif (
                _normalized_probe_uid(probe.uid)
                and _normalized_probe_uid(probe.uid) in port.searchable_text
            ):
                score += 10
        # Weak boost so an external USB-serial adapter on a decoupled custom PCB
        # surfaces as a candidate when nothing stronger matches. Stays below a
        # probe link or a real board hint, so it never overrides them.
        if looks_like_usb_serial_bridge(port):
            score += 1
        if score > 0:
            scored.append((score, port))

    if not scored:
        return []

    best_score = max(score for score, _ in scored)
    return [port for score, port in scored if score == best_score]


def _resolve_nordic_serial(
    spec: SerialFallbackSpec,
    board: BoardLike,
    ports: list[SerialPortInfo],
    probe: ProbeLike | None,
    run_cmd: RunCommand,
) -> SerialResolution | None:
    if board.probe_family.casefold() not in spec.probe_families:
        return None
    nrfjprog_path = resolve_command_path(spec.executable, spec.executable_env)
    if nrfjprog_path is None:
        return None

    rc, out, _ = run_cmd([nrfjprog_path, *spec.argv])
    if rc != 0:
        return None

    entries = parse_nrfjprog_com_output(out)
    if not entries:
        return None

    matching = entries
    if probe and probe.uid:
        probe_matches = [
            entry for entry in entries if probe_uid_matches_serial(probe.uid, entry.probe_serial)
        ]
        if probe_matches:
            matching = probe_matches

    matching = sorted(
        matching,
        key=lambda entry: (
            0
            if spec.preferred_label and entry.label.upper() == spec.preferred_label.upper()
            else 1,
            normalize_port_name(entry.port),
        ),
    )
    for entry in matching:
        port = _find_port_by_name(ports, entry.port)
        if port:
            return SerialResolution(port, f"resolved via nrfjprog --com ({entry.label})")
    return None


def _resolve_stlink_serial(
    spec: SerialFallbackSpec,
    board: BoardLike,
    ports: list[SerialPortInfo],
    probe: ProbeLike | None,
    run_cmd: RunCommand,
) -> SerialResolution | None:
    if board.probe_family.casefold() not in spec.probe_families:
        return None
    stm32_cli_path = resolve_command_path(spec.executable, spec.executable_env)
    if stm32_cli_path is None:
        return None

    rc, out, _ = run_cmd([stm32_cli_path, *spec.argv])
    if rc != 0:
        return None

    entries = parse_stm32_programmer_list_output(out)
    if not entries:
        return None

    matching = entries
    if probe and probe.uid:
        probe_matches = [
            entry for entry in entries if probe_uid_matches_serial(probe.uid, entry.probe_serial)
        ]
        if probe_matches:
            matching = probe_matches

    resolved: list[SerialPortInfo] = []
    for entry in matching:
        port = _find_port_by_name(ports, entry.port)
        if port:
            resolved.append(port)

    unique: dict[str, SerialPortInfo] = {
        normalize_port_name(port.device): port for port in resolved
    }
    if len(unique) == 1:
        return SerialResolution(next(iter(unique.values())), "resolved via STM32_Programmer_CLI -l")
    return None


def resolve_serial_port(
    board: BoardLike,
    ports: list[SerialPortInfo],
    probe: ProbeLike | None,
    override: str | None,
    allow_single_fallback: bool,
    run_cmd: RunCommand,
    interactive: bool,
) -> SerialResolution:
    if override:
        port = _find_port_by_name(ports, override)
        if port:
            return SerialResolution(port, "")
        return SerialResolution(None, f"override port '{override}' not found")

    candidates = _generic_candidates(board, ports, probe)
    if len(candidates) == 1 and probe and probe.uid:
        candidate = candidates[0]
        if probe_uid_matches_serial(probe.uid, candidate.serial_number) or (
            _normalized_probe_uid(probe.uid)
            and _normalized_probe_uid(probe.uid) in candidate.searchable_text
        ):
            return SerialResolution(candidate, "resolved from serial-port metadata")

    if len(candidates) == 1 and not looks_like_usb_serial_bridge(candidates[0]):
        return SerialResolution(candidates[0], "resolved from generic serial-port metadata")

    for fallback in SERIAL_FALLBACKS:
        vendor_resolution = None
        if fallback.parser == "nrfjprog_com":
            vendor_resolution = _resolve_nordic_serial(fallback, board, ports, probe, run_cmd)
        elif fallback.parser == "stm32_programmer_list":
            vendor_resolution = _resolve_stlink_serial(fallback, board, ports, probe, run_cmd)
        if vendor_resolution is not None:
            return vendor_resolution

    if len(candidates) == 1:
        return SerialResolution(candidates[0], "resolved from generic serial-port metadata")

    if (
        allow_single_fallback
        and len(ports) == 1
        and not _conflicts_with_probe_family(board, ports[0])
    ):
        return SerialResolution(ports[0], "single connected serial port assumed for this board")

    if candidates:
        if interactive:
            return _prompt_for_candidate(board, candidates)
        return SerialResolution(
            None,
            f"multiple matching serial ports found; {_manual_rerun_guidance(board, candidates)}",
        )

    if interactive and ports:
        return _prompt_for_candidate(board, ports)
    if ports:
        return SerialResolution(
            None, f"no matching serial port found; {_manual_rerun_guidance(board, ports)}"
        )
    return SerialResolution(None, "no serial ports detected")
